"""Model-visible feedback for non-schema tool-call arguments.

Measured gap (docs/vnext/GP01_TASKB_COST.md run 3, nightly N03): a weak
model emitted ``read_file(path=…, toolbench_rapidapi_key=null)``. The name
is not in the ToolArgAliasMiddleware table, and pydantic's
``extra='ignore'`` drops it silently — the model never learns the argument
it believed it passed never reached the tool, so it keeps trusting the
phantom parameter on later calls.

This middleware is the bounded recovery check for that failure class.
Contributed at ``Placement.TOOL_VISIBLE`` (outermost on the tool axis), it
sees the args exactly as the model emitted them and the result exactly as
the model will read it. When the emitted args contain names the
model-visible schema does not declare — after crediting names the alias
middleware would rename — it appends a one-line "ignored argument(s)" note
to the result and stamps the transform trail.

Bounded by construction: no retries, no argument rewriting, no blocking —
the call executes exactly as it would today; only the result text gains
the note. Observational only (``intercepting=False``): a defect here fails
open via IsolatedMiddleware rather than touching the call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, override

from deerflow.agents.middlewares.tool_arg_alias_middleware import normalize_tool_call_args
from deerflow.agents.middlewares.tool_transform_meta import append_tool_transform
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

_TRANSFORM_KIND = "arg_drift_note"
_TRANSFORM_BY = "ewcp_core"


def _schema_arg_names(tool: Any) -> set[str] | None:
    """Model-visible argument names for ``tool``; None when unknowable or unbounded."""
    schema = getattr(tool, "tool_call_schema", None)
    if schema is None or not hasattr(schema, "model_json_schema"):
        return None
    config = getattr(schema, "model_config", None)
    if isinstance(config, dict) and config.get("extra") == "allow":
        return None  # extras are retained — nothing is silently dropped
    try:
        properties = schema.model_json_schema().get("properties")
    except Exception:  # noqa: BLE001 — a hostile schema probe must never break a tool call
        return None
    return set(properties) if isinstance(properties, dict) else None


def _dropped_arg_names(tool_call: dict[str, Any], tool: Any) -> tuple[list[str], list[str]] | None:
    """Args the schema will drop + the accepted names; None when nothing is dropped."""
    args = tool_call.get("args")
    if not isinstance(args, dict) or not args:
        return None
    accepted = _schema_arg_names(tool)
    if accepted is None:
        return None
    extras = [key for key in args if key not in accepted]
    if not extras:
        return None
    # Names the alias middleware renames onto schema fields are consumed, not dropped.
    _, applied = normalize_tool_call_args(tool_call.get("name"), args)
    if applied:
        extras = [key for key in extras if key not in applied]
    if not extras:
        return None
    return sorted(extras), sorted(accepted)


class ToolArgFeedbackMiddleware(AgentMiddleware):
    """Annotate tool results when the model passed arguments the schema dropped."""

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        flagged = _dropped_arg_names(request.tool_call, request.tool)
        result = handler(request)
        if flagged is not None:
            dropped, accepted = flagged
            self._annotate_result(result, dropped, str(request.tool_call.get("name")), accepted)
        return result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        flagged = _dropped_arg_names(request.tool_call, request.tool)
        result = await handler(request)
        if flagged is not None:
            dropped, accepted = flagged
            self._annotate_result(result, dropped, str(request.tool_call.get("name")), accepted)
        return result

    def _annotate_result(
        self,
        result: ToolMessage | Command,
        dropped: list[str],
        tool_name: str,
        accepted: list[str],
    ) -> None:
        if isinstance(result, ToolMessage):
            self._annotate_message(result, dropped, tool_name, accepted)
            return
        update = getattr(result, "update", None)
        messages = update.get("messages") if isinstance(update, dict) else None
        if isinstance(messages, ToolMessage):
            self._annotate_message(messages, dropped, tool_name, accepted)
        elif isinstance(messages, (list, tuple)):
            for message in messages:
                if isinstance(message, ToolMessage):
                    self._annotate_message(message, dropped, tool_name, accepted)

    def _annotate_message(
        self,
        message: ToolMessage,
        dropped: list[str],
        tool_name: str,
        accepted: list[str],
    ) -> ToolMessage:
        if not isinstance(message.content, str):
            return message
        note = f"ignored argument(s) {', '.join(dropped)}: not in the '{tool_name}' schema — they were dropped before execution. Accepted arguments: {', '.join(accepted)}."
        if f"ignored argument(s) {', '.join(dropped)}:" in message.content:
            return message
        message.content = f"{message.content}\n\n{note}" if message.content else note
        additional_kwargs = dict(message.additional_kwargs or {})
        append_tool_transform(additional_kwargs, _TRANSFORM_KIND, by=_TRANSFORM_BY)
        message.additional_kwargs = additional_kwargs
        return message
