"""GovernedChatModel — the general lane's LangChain-side boundary
(M-EA3, invariant 7).

A ``BaseChatModel`` that owns no provider credential: every call
resolves the run's virtual key from the runnable config
(``configurable["ewcp_model_key"]``, minted kernel-side per WorkRun and
stamped by ``EwcpDeerFlowClient._get_runnable_config``) and routes the
completion through the kernel ``ModelBroker`` — LLMGateway precheck ->
reserve -> provider -> meter -> charge under ``BudgetGuard``'s $ cap.

Fail-closed by construction: no key, no broker, or a spent budget means
the call raises before any provider SDK is touched. Model profiles
point ``use:`` at this class so the governed profile cannot reach a
provider path at all.

Serialization: LangChain messages <-> the kernel's plain-dict chat
shape. The exact assistant payload returned by the broker is stored on
``AIMessage.additional_kwargs["ewcp_model_response"]`` so replayed
tool-call turns re-emit the provider's own parts — including Gemini
3.x ``thoughtSignature`` echoes, which the API rejects when dropped
(the ``__gemini_function_call_thought_signatures__`` map produced by
``ChatGoogleGenerativeAI`` is also honored as a fallback source).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterator, Sequence

from langchain_core.language_models.chat_models import (
    BaseChatModel,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)
from langchain_core.runnables import Runnable, ensure_config
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_function
from pydantic import ConfigDict

logger = logging.getLogger(__name__)

# same map key ChatGoogleGenerativeAI populates — reused so messages
# produced by either class replay with their signatures
_GEMINI_SIG_MAP_KEY = "__gemini_function_call_thought_signatures__"

# additional_kwargs key storing the verbatim assistant payload the
# broker returned — the faithful replay source for the next call
_EWCP_RESPONSE_KEY = "ewcp_model_response"


class ModelGovernanceError(RuntimeError):
    """No governed session for this call — deny before any provider."""


def _broker():
    """Kernel broker — imported lazily: ewcp rides on sys.path only in
    the gateway process (PYTHONPATH), not at extension import time."""
    from ewcp.runtime.model_broker import get_broker

    broker = get_broker()
    if broker is None:
        raise ModelGovernanceError("no kernel ModelBroker wired — governed lane cannot call a provider (fail-closed)")
    return broker


def _session() -> tuple[str, str]:
    """(virtual_key, workrun_id) from the runnable config — deny closed."""
    cfg = ensure_config().get("configurable") or {}
    key = cfg.get("ewcp_model_key")
    if not key:
        raise ModelGovernanceError("no ewcp_model_key in runnable config — the lane is ungoverned, denying the call (fail-closed)")
    thread_id = cfg.get("thread_id") or ""
    wid = thread_id[5:] if thread_id.startswith("ewcp-") else thread_id
    if not wid:
        raise ModelGovernanceError("no ewcp thread_id in runnable config")
    return key, wid


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in (
            "text",
            None,
        ):
            parts.append(str(block.get("text") or block.get("content") or ""))
    return "".join(parts)


def _assistant_payload(message: AIMessage) -> dict:
    """Kernel dict for an AI turn — verbatim broker payload when the
    message came from us, else a best-effort rebuild (signatures may be
    absent -> the kernel client patches the active-loop dummy)."""
    stored = message.additional_kwargs.get(_EWCP_RESPONSE_KEY)
    if isinstance(stored, dict):
        return {
            "role": "assistant",
            "content": stored.get("content"),
            "tool_calls": list(stored.get("tool_calls") or []),
        }
    sigs = message.additional_kwargs.get(_GEMINI_SIG_MAP_KEY) or {}
    calls = []
    for call in message.tool_calls or []:
        entry = {
            "id": call.get("id"),
            "name": call.get("name"),
            "args": call.get("args") or {},
        }
        sig = sigs.get(call.get("id"))
        if sig:
            entry["thought_signature"] = sig
        calls.append(entry)
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": calls,
    }


def _to_kernel_message(message: BaseMessage) -> dict:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _content_text(message.content)}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _content_text(message.content)}
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "name": message.name or "",
            "content": _content_text(message.content),
        }
    if isinstance(message, AIMessage):
        return _assistant_payload(message)
    return {"role": "user", "content": _content_text(message.content)}


def _to_ai_message(response: dict, *, model_name: str) -> AIMessage:
    blocks = response.get("blocks") or []
    if blocks:
        content: Any = [
            {
                "type": "text",
                "text": b.get("text", ""),
                **({"extras": {"signature": b["signature"]}} if b.get("signature") else {}),
            }
            if b.get("type") == "text"
            else {
                "type": "thinking",
                "thinking": b.get("thinking", ""),
                **({"signature": b["signature"]} if b.get("signature") else {}),
            }
            for b in blocks
        ]
    else:
        content = response.get("text") or ""
    tool_calls = [
        {
            "id": c.get("id") or f"call_{i}",
            "name": c.get("name"),
            "args": c.get("args") or {},
        }
        for i, c in enumerate(response.get("tool_calls") or [])
    ]
    usage = response.get("usage") or {}
    usage_metadata = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    # verbatim replay payload — next call re-emits the provider's own
    # parts (signatures included) instead of a lossy reconstruction
    replay = {
        "content": blocks if blocks else (response.get("text") or ""),
        "tool_calls": list(response.get("tool_calls") or []),
    }
    return AIMessage(
        id=f"ewcp-{uuid.uuid4().hex[:12]}",
        content=content,
        tool_calls=tool_calls,
        additional_kwargs={_EWCP_RESPONSE_KEY: replay},
        usage_metadata=usage_metadata,
        response_metadata={
            "model_name": model_name,
            "finish_reason": response.get("finish_reason"),
        },
    )


def _chunks(response: dict, *, model_name: str) -> Iterator[ChatGenerationChunk]:
    """Stream the same payload as deltas: text chunks first, then tool
    calls, then a usage/final chunk — mirroring provider chunk order."""
    for block in response.get("blocks") or []:
        if block.get("type") == "thinking":
            part = {"type": "thinking", "thinking": block.get("thinking", "")}
            if block.get("signature"):
                part["signature"] = block["signature"]
            yield ChatGenerationChunk(message=AIMessageChunk(content=[part]))
        elif block.get("type") == "text" and block.get("text"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=block["text"]))
    calls = response.get("tool_calls") or []
    for i, call in enumerate(calls):
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=[
                    tool_call_chunk(
                        index=i,
                        id=call.get("id") or f"call_{i}",
                        name=call.get("name"),
                        args=json.dumps(call.get("args") or {}),
                    )
                ],
            )
        )
    usage = response.get("usage") or {}
    replay = {
        "content": response.get("blocks") or (response.get("text") or ""),
        "tool_calls": calls,
    }
    yield ChatGenerationChunk(
        message=AIMessageChunk(
            content="",
            additional_kwargs={_EWCP_RESPONSE_KEY: replay},
            usage_metadata={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            response_metadata={
                "model_name": model_name,
                "finish_reason": response.get("finish_reason"),
            },
        )
    )


def _normalize_tool(tool: Any) -> dict:
    """BaseTool / dict / callable -> {name, description, parameters}."""
    if isinstance(tool, dict):
        if "function" in tool:  # OpenAI tool wrapper
            fn = tool["function"] or {}
            return {
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {},
            }
        if "name" in tool and "parameters" not in tool and "args" in tool:
            return convert_to_openai_function(tool)
        return {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {},
        }
    return convert_to_openai_function(tool)


class GovernedChatModel(BaseChatModel):
    """LangChain boundary for the EWCP governed lane — provider calls
    ride the kernel gateway under the run's virtual key."""

    model_config = ConfigDict(extra="allow")

    model: str = ""
    max_output_tokens: int | None = None

    @property
    def _llm_type(self) -> str:
        return "ewcp-governed"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool | Any],
        *,
        tool_choice: str | dict | None = None,
        **kwargs: Any,
    ) -> Runnable:
        formatted = [_normalize_tool(t) for t in tools]
        bound: dict[str, Any] = {"tools": formatted}
        if tool_choice is not None:
            bound["tool_choice"] = tool_choice
        bound.update(kwargs)
        return self.bind(**bound)

    def _call_broker(self, messages: list[BaseMessage], **kwargs: Any) -> dict:
        key, wid = _session()
        max_out = self.max_output_tokens
        if max_out is None:
            raw = kwargs.get("max_tokens") or kwargs.get("max_output_tokens")
            max_out = int(raw) if raw else None
        return _broker().complete(
            virtual_key=key,
            workrun_id=wid,
            model=self.model,
            messages=[_to_kernel_message(m) for m in messages],
            tools=kwargs.get("tools"),
            tool_choice=kwargs.get("tool_choice"),
            max_output_tokens=max_out,
            **{k: v for k, v in kwargs.items() if k not in ("tools", "tool_choice", "max_tokens", "max_output_tokens")},
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if stop:
            kwargs = {**kwargs, "stop": stop}
        resp = self._call_broker(messages, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=_to_ai_message(resp, model_name=self.model))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        if stop:
            kwargs = {**kwargs, "stop": stop}
        resp = self._call_broker(messages, **kwargs)
        yield from _chunks(resp, model_name=self.model)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        import asyncio

        return await asyncio.to_thread(self._generate, messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        import asyncio

        chunks = await asyncio.to_thread(lambda: list(self._stream(messages, stop, run_manager, **kwargs)))
        for chunk in chunks:
            yield chunk
