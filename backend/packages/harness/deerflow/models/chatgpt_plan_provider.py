"""ChatGPT-plan OAuth provider — LangChain chat model backed by the public
OpenAI Responses API under a Sign in with ChatGPT grant.

This is the OFFICIAL ``chatgpt_plan_oauth`` provider. It is intentionally
distinct from ``openai_codex_provider.CodexChatModel``: that provider talks to
``chatgpt.com/backend-api/codex/*`` with Codex CLI credentials, while this one
talks to ``https://api.openai.com/v1/responses`` with SIWC OAuth tokens issued
per the documented open-source flow (see ``chatgpt_plan_oauth.py``).

Contract applied here (from the SIWC docs):
    - ``store: false`` and ``stream: true`` on every HTTP inference request.
    - Full conversation history is replayed in ``input`` (no
      ``previous_response_id`` over HTTP).
    - ``instructions`` / developer messages only — explicit
      ``{type: "message", role: "system"}`` items are rejected upstream.
    - Unsupported request fields are never forwarded: ``background``,
      ``conversation``, ``max_output_tokens``, ``max_tool_calls``, ``metadata``,
      ``moderation``, ``multi_agent``, ``prompt``, ``prompt_cache_retention``,
      ``safety_identifier``, ``temperature``, ``top_logprobs``, ``top_p``,
      ``truncation``, ``user``.
    - Success means ``response.completed``; ``response.failed`` /
      ``response.incomplete`` / interrupted streams are surfaced as distinct,
      correctly classified errors (subscription usage-limit failures are
      non-retriable quota errors).
    - Hosted tools (image generation, file search, Code Interpreter, native
      computer use, hosted MCP/connectors, ``tool_search``,
      ``programmatic_tool_calling``) are out of scope; only local
      function/custom tools are sent.

Cost attribution: every result stamps ``billing_source = "chatgpt_plan"`` and
``provider = "chatgpt_plan_oauth"`` into ``response_metadata``/``llm_output``
(and ``usage_metadata``) so run accounting can separate plan-based usage from
API-key billing. Token counts reported by the API are measured usage; any
``pricing`` block on the model config produces an *estimated API-equivalent*
cost, not a billed amount — subscription draw is governed by the account's
ChatGPT plan limits, not per-token charges.

Config example::

    - name: chatgpt-plan-gpt
      display_name: ChatGPT Plan (OAuth)
      description: "Billed against the signed-in account's ChatGPT plan — not an OpenAI API key"
      use: deerflow.models.chatgpt_plan_provider:ChatGPTPlanChatModel
      model: <slug from `python -m deerflow.models.chatgpt_plan_oauth models`>
      # account: user@example.com      # required once >1 account is authorized
      # credentials_dir: ~/.my-dir     # overrides CHATGPT_PLAN_CREDENTIALS_DIR
      # reasoning_effort: medium       # optional; omitted from payload if unset
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import Field, PrivateAttr

from deerflow.models.chatgpt_plan_oauth import (
    RESPONSES_URL,
    CredentialRecord,
    CredentialStore,
    get_valid_access_token,
    list_account_models,
    require_inference_scope,
)
from deerflow.models.openai_codex_provider import CodexChatModel, _build_usage_metadata

logger = logging.getLogger(__name__)

PROVIDER_ID = "chatgpt_plan_oauth"
BILLING_SOURCE = "chatgpt_plan"

# Fields that must never reach the Responses request body on this route
# (preview-limitations). Generic model config may still carry them; they are
# dropped at build time with a warning instead of silently changing behavior.
_UNSUPPORTED_REQUEST_FIELDS = (
    "background",
    "conversation",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "moderation",
    "multi_agent",
    "prompt",
    "prompt_cache_retention",
    "safety_identifier",
    "temperature",
    "top_logprobs",
    "top_p",
    "truncation",
    "user",
    # Generic DeerFlow knobs that have no meaning on this route:
    "max_tokens",
    "stop",
    "frequency_penalty",
    "presence_penalty",
    "stream_usage",
    "use_previous_response_id",
    "api_key",
    "api_base",
    "base_url",
    "timeout",
    "request_timeout",
    "extra_body",
    "default_headers",
    "default_query",
    "model_kwargs",
)

# Hosted tool types the direct route does not support (preview-limitations).
_UNSUPPORTED_HOSTED_TOOLS = (
    "image_generation",
    "file_search",
    "code_interpreter",
    "computer_use_preview",
    "mcp",
    "tool_search",
    "web_search_preview",
    "programmatic_tool_calling",
)


# ---------------------------------------------------------------------------
# Provider errors — ``code``/``status_code`` attributes feed the LLM error
# middleware's classification (quota / auth / transient).
# ---------------------------------------------------------------------------


class ChatGPTPlanProviderError(Exception):
    """Base class for ChatGPT-plan provider failures."""

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


class ChatGPTPlanAuthError(ChatGPTPlanProviderError):
    """401/403-style admission failures — credentials or permission rejected."""


class ChatGPTPlanRateLimitError(ChatGPTPlanProviderError):
    """Generic 429 rate limiting (transient — retryable by policy)."""


class ChatGPTPlanTransientError(ChatGPTPlanProviderError):
    """5xx without a specific code — retryable by policy."""


class ChatGPTPlanUsageLimitError(ChatGPTPlanProviderError):
    """``subscription_sharing_usage_limit_exceeded`` — the account's plan limit
    is exhausted. Non-retriable: the middleware maps ``usage_limit_exceeded``
    to the non-retriable ``quota`` reason."""


class ChatGPTPlanUsageUnavailableError(ChatGPTPlanProviderError):
    """``subscription_sharing_usage_unavailable`` (503) — metering unavailable;
    retry later with bounded backoff."""


class ChatGPTPlanNotEligibleError(ChatGPTPlanProviderError):
    """``subscription_sharing_user_not_eligible`` (403) — plan usage is not
    available for this user/workspace/policy. Do not loop through OAuth."""


class ChatGPTPlanUnsupportedCapabilityError(ChatGPTPlanProviderError):
    """``subscription_sharing_unsupported_capability`` (400) — the request body
    carried something this route rejects; ``error.param`` names it."""


class ChatGPTPlanRouteNotSupportedError(ChatGPTPlanProviderError):
    """``subscription_sharing_route_not_supported`` (403) — wrong endpoint."""


class ChatGPTPlanInvalidUserError(ChatGPTPlanProviderError):
    """``subscription_sharing_invalid_user`` (401) — subscriber context could
    not be validated; the user must sign in again after revocation."""


class ChatGPTPlanScopeNotAuthorizedError(ChatGPTPlanProviderError):
    """``chatpass_v2_*`` (403) — the signed permission context does not
    authorize the operation."""


class ChatGPTPlanUserUnavailableError(ChatGPTPlanProviderError):
    """``subscription_sharing_user_unavailable`` (503) — retry later."""


class ChatGPTPlanResponseFailedError(ChatGPTPlanProviderError):
    """``response.failed`` stream event with an unrecognized error code."""


class ChatGPTPlanIncompleteError(ChatGPTPlanProviderError):
    """``response.incomplete`` — generation ended early (e.g. length)."""


class ChatGPTPlanStreamInterruptedError(ChatGPTPlanProviderError):
    """The SSE stream ended without a terminal ``response.completed`` event."""


_CODE_TO_ERROR: dict[str, type[ChatGPTPlanProviderError]] = {
    "subscription_sharing_user_not_eligible": ChatGPTPlanNotEligibleError,
    "subscription_sharing_usage_limit_exceeded": ChatGPTPlanUsageLimitError,
    "subscription_sharing_usage_unavailable": ChatGPTPlanUsageUnavailableError,
    "subscription_sharing_unsupported_capability": ChatGPTPlanUnsupportedCapabilityError,
    "subscription_sharing_route_not_supported": ChatGPTPlanRouteNotSupportedError,
    "subscription_sharing_invalid_user": ChatGPTPlanInvalidUserError,
    "chatpass_v2_scope_not_authorized": ChatGPTPlanScopeNotAuthorizedError,
    "chatpass_v2_invalid_authorization_context": ChatGPTPlanScopeNotAuthorizedError,
    "subscription_sharing_user_unavailable": ChatGPTPlanUserUnavailableError,
}


def _error_for_code(code: str, message: str, *, status_code: int | None, request_id: str | None) -> ChatGPTPlanProviderError:
    error_cls = _CODE_TO_ERROR.get(code, ChatGPTPlanResponseFailedError)
    suffix = f" (request_id={request_id})" if request_id else ""
    return error_cls(f"{message}{suffix}", code=code, status_code=status_code, request_id=request_id)


def _extract_error_payload(body: Any) -> tuple[str | None, str]:
    """Return (code, message) from a Responses-layer or admission error body.

    Direct-route admission can answer ``{"detail": "..."}`` before a Responses
    request starts; the structured shape is ``{"error": {"code", "message",
    "param"}}``. Both are handled; the detail string is diagnostic only.
    """

    if not isinstance(body, dict):
        return None, str(body)[:500]
    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code") if isinstance(error.get("code"), str) else None
        message = error.get("message") if isinstance(error.get("message"), str) else None
        param = error.get("param")
        if message and param:
            message = f"{message} (param: {param})"
        return code, message or code or json.dumps(error)[:500]
    detail = body.get("detail")
    if isinstance(detail, str):
        return None, detail
    return None, json.dumps(body)[:500]


class ChatGPTPlanChatModel(CodexChatModel):
    """LangChain chat model over the public Responses API + SIWC OAuth token.

    Inherits message/tool conversion, SSE line parsing, tool-argument parsing
    and ``bind_tools`` from ``CodexChatModel``. Everything identity- and
    endpoint-related is overridden here — no Codex credential or
    ``backend-api`` URL is ever consulted.
    """

    model: str  # required: must be a slug from the account's model catalog
    reasoning_effort: str | None = None
    account: str | None = Field(default=None, description="Account selector: email, subject, or issued client_id")
    credentials_dir: str | None = Field(default=None, description="Credential store override (default: $CHATGPT_PLAN_CREDENTIALS_DIR or <runtime_home>/chatgpt-plan)")
    request_timeout_seconds: float = 300.0

    _store: CredentialStore = PrivateAttr()
    _record: CredentialRecord = PrivateAttr()
    _http_client: httpx.Client | None = PrivateAttr(default=None)

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    @property
    def _llm_type(self) -> str:
        return "chatgpt-plan-oauth"

    def model_post_init(self, __context: Any) -> None:
        self._validate_retry_config()
        self._store = CredentialStore(self.credentials_dir)
        # Resolve eagerly so construction fails fast when no authorized
        # account exists; the scope check runs again on every refresh.
        self._record = self._store.resolve(self.account)
        if not self._record.plan_enabled:
            logger.warning(
                "ChatGPT-plan account %s lacks %s — inference will fail until re-consent",
                self._record.email or self._record.subject,
                "chatgpt.tokens.use.direct",
            )
        self._warn_dropped_config()
        # Skip CodexChatModel.model_post_init entirely (no Codex CLI load).
        super(CodexChatModel, self).model_post_init(__context)

    def _warn_dropped_config(self) -> None:
        extras = getattr(self, "__pydantic_extra__", None) or {}
        dropped = sorted(k for k in extras if k in _UNSUPPORTED_REQUEST_FIELDS)
        if dropped:
            logger.warning(
                "ChatGPTPlanChatModel ignores unsupported config keys %s — the ChatGPT-plan route rejects them upstream.",
                dropped,
            )
        unknown = sorted(k for k in extras if k not in _UNSUPPORTED_REQUEST_FIELDS)
        if unknown:
            logger.warning(
                "ChatGPTPlanChatModel does not forward unrecognized config keys %s.",
                unknown,
            )

    # -- credentials --------------------------------------------------------

    def _get_access_token(self) -> str:
        """Return a usable access token, refreshing + re-checking scope."""

        token, record = get_valid_access_token(self._store, self.account or self._record.client_id, client=self._http_client)
        self._record = record
        require_inference_scope(record)
        return token

    def list_account_models(self, *, client: httpx.Client | None = None) -> list[dict[str, str]]:
        """The account's model catalog (``visibility == "list"`` entries)."""

        return list_account_models(self._get_access_token(), client=client or self._http_client)

    # -- request build ------------------------------------------------------

    def _build_payload(self, messages: list[BaseMessage], tools: list[dict] | None) -> dict[str, Any]:
        instructions, input_items = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if tools:
            payload["tools"] = self._convert_tools(tools)
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def _build_http_client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        return httpx.Client(timeout=self.request_timeout_seconds)

    # -- error mapping --------------------------------------------------------

    def _raise_for_error_body(self, status_code: int, body: Any, request_id: str | None) -> None:
        code, message = _extract_error_payload(body)
        if code and code in _CODE_TO_ERROR:
            raise _error_for_code(code, message or code, status_code=status_code, request_id=request_id)
        if status_code == 401:
            raise ChatGPTPlanAuthError(f"Unauthorized: {message}", code=code, status_code=status_code, request_id=request_id)
        if status_code == 403:
            raise ChatGPTPlanAuthError(f"Forbidden: {message}", code=code, status_code=status_code, request_id=request_id)
        if status_code == 429:
            raise ChatGPTPlanRateLimitError(f"Rate limited: {message}", code=code, status_code=status_code, request_id=request_id)
        if status_code >= 500:
            raise ChatGPTPlanTransientError(f"Responses API HTTP {status_code}: {message}", code=code, status_code=status_code, request_id=request_id)
        raise ChatGPTPlanProviderError(f"Responses API HTTP {status_code}: {message}", code=code, status_code=status_code, request_id=request_id)

    def _raise_for_stream_event(self, event: dict[str, Any]) -> None:
        """Map a terminal non-``response.completed`` event to a provider error."""

        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        request_id = response.get("id") or event.get("response_id")
        event_type = event.get("type")
        if event_type == "response.failed":
            error = response.get("error") or {}
            code = error.get("code") if isinstance(error.get("code"), str) else None
            message = error.get("message") if isinstance(error.get("message"), str) else None
            status = response.get("status_code") or event.get("status_code")
            raise _error_for_code(code or "response.failed", message or "Response failed", status_code=status if isinstance(status, int) else None, request_id=request_id)
        if event_type == "response.incomplete":
            details = response.get("incomplete_details") or {}
            reason = details.get("reason") if isinstance(details.get("reason"), str) else "unknown"
            raise ChatGPTPlanIncompleteError(f"Response incomplete: {reason}", code="response.incomplete", request_id=request_id)
        raise ChatGPTPlanProviderError(f"Unexpected terminal stream event: {event_type}", code=str(event_type))

    # -- transport ------------------------------------------------------------

    def _call_responses_api(self, messages: list[BaseMessage], tools: list[dict] | None = None) -> dict:
        """Stream one Responses request through ``response.completed``."""

        payload = self._build_payload(messages, tools)
        retryable = (ChatGPTPlanRateLimitError, ChatGPTPlanTransientError, ChatGPTPlanStreamInterruptedError)
        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                return self._stream_response(payload)
            except retryable as exc:
                if attempt >= self.retry_max_attempts:
                    raise
                wait_ms = 2000 * (1 << (attempt - 1))
                logger.warning(
                    "Responses API transient failure (%s), retrying %d/%d after %dms",
                    exc.__class__.__name__,
                    attempt,
                    self.retry_max_attempts,
                    wait_ms,
                )
                time.sleep(wait_ms / 1000)
        raise ChatGPTPlanStreamInterruptedError("unreachable")  # pragma: no cover

    def _stream_response(self, payload: dict) -> dict:
        """POST /v1/responses and collect the completed response object."""

        completed_response: dict[str, Any] | None = None
        streamed_output_items: dict[int, dict[str, Any]] = {}
        client = self._build_http_client()
        owns_client = self._http_client is None
        try:
            with client.stream("POST", RESPONSES_URL, headers=self._headers(), json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read()
                    try:
                        parsed = json.loads(body)
                    except (json.JSONDecodeError, TypeError):
                        parsed = body.decode("utf-8", errors="replace")[:500]
                    self._raise_for_error_body(resp.status_code, parsed, resp.headers.get("x-request-id"))
                for line in resp.iter_lines():
                    data = self._parse_sse_data_line(line)
                    if not data:
                        continue
                    event_type = data.get("type")
                    if event_type == "response.output_item.done":
                        output_index = data.get("output_index")
                        output_item = data.get("item")
                        if isinstance(output_index, int) and isinstance(output_item, dict):
                            streamed_output_items[output_index] = output_item
                    elif event_type == "response.completed":
                        completed_response = data.get("response")
                    elif event_type in ("response.failed", "response.incomplete"):
                        self._raise_for_stream_event(data)
        except httpx.HTTPStatusError:
            raise
        except ChatGPTPlanProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ChatGPTPlanStreamInterruptedError(f"Stream interrupted: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        if not completed_response:
            raise ChatGPTPlanStreamInterruptedError("Stream ended without response.completed")
        if not isinstance(completed_response, dict):
            raise ChatGPTPlanStreamInterruptedError("response.completed carried a malformed payload")

        # The final assistant content may only exist in stream events; merge
        # streamed output items into the completed response (same gap Codex has).
        if streamed_output_items:
            merged_output = completed_response.get("output")
            merged_output = list(merged_output) if isinstance(merged_output, list) else []
            if streamed_output_items:
                max_index = max(max(streamed_output_items), len(merged_output) - 1)
                if max_index >= 0 and len(merged_output) <= max_index:
                    merged_output.extend([None] * (max_index + 1 - len(merged_output)))
                for output_index, output_item in streamed_output_items.items():
                    if not isinstance(merged_output[output_index], dict):
                        merged_output[output_index] = output_item
            completed_response = dict(completed_response)
            completed_response["output"] = [item for item in merged_output if isinstance(item, dict)]

        return completed_response

    # -- LangChain surface ----------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if stop:
            logger.debug("ChatGPT-plan route does not accept stop sequences; ignoring %d entries", len(stop))
        tools = kwargs.get("tools")
        response = self._call_responses_api(messages, tools=tools)
        return self._parse_response(response)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Incremental streaming: text deltas, tool-call argument deltas, and a
        trailing usage chunk once ``response.completed`` arrives."""

        if stop:
            logger.debug("ChatGPT-plan route does not accept stop sequences; ignoring %d entries", len(stop))
        payload = self._build_payload(messages, kwargs.get("tools"))

        client = self._build_http_client()
        owns_client = self._http_client is None
        item_meta: dict[int, dict[str, Any]] = {}  # output_index -> {id, type, name, call_id}
        completed_response: dict[str, Any] | None = None
        try:
            with client.stream("POST", RESPONSES_URL, headers=self._headers(), json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read()
                    try:
                        parsed = json.loads(body)
                    except (json.JSONDecodeError, TypeError):
                        parsed = body.decode("utf-8", errors="replace")[:500]
                    self._raise_for_error_body(resp.status_code, parsed, resp.headers.get("x-request-id"))
                for line in resp.iter_lines():
                    data = self._parse_sse_data_line(line)
                    if not data:
                        continue
                    event_type = data.get("type")
                    output_index = data.get("output_index") if isinstance(data.get("output_index"), int) else -1

                    if event_type == "response.output_item.added":
                        item = data.get("item") or {}
                        if isinstance(item, dict):
                            item_meta[output_index] = {
                                "id": item.get("id"),
                                "type": item.get("type"),
                                "name": item.get("name"),
                                "call_id": item.get("call_id"),
                            }
                            if item.get("type") == "function_call":
                                yield ChatGenerationChunk(
                                    message=AIMessageChunk(
                                        content="",
                                        tool_call_chunks=[
                                            {
                                                "name": item.get("name"),
                                                "args": "",
                                                "id": item.get("call_id") or item.get("id"),
                                                "index": output_index if output_index >= 0 else 0,
                                            }
                                        ],
                                    )
                                )
                    elif event_type == "response.output_text.delta":
                        delta = data.get("delta")
                        if isinstance(delta, str) and delta:
                            yield ChatGenerationChunk(message=AIMessageChunk(content=delta))
                    elif event_type == "response.function_call_arguments.delta":
                        meta = item_meta.get(output_index) or {}
                        yield ChatGenerationChunk(
                            message=AIMessageChunk(
                                content="",
                                tool_call_chunks=[
                                    {
                                        "name": meta.get("name"),
                                        "args": data.get("delta") or "",
                                        "id": meta.get("call_id") or meta.get("id"),
                                        "index": output_index if output_index >= 0 else 0,
                                    }
                                ],
                            )
                        )
                    elif event_type == "response.completed":
                        completed_response = data.get("response")
                    elif event_type in ("response.failed", "response.incomplete"):
                        self._raise_for_stream_event(data)
        except ChatGPTPlanProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ChatGPTPlanStreamInterruptedError(f"Stream interrupted: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        if not isinstance(completed_response, dict):
            raise ChatGPTPlanStreamInterruptedError("Stream ended without response.completed")

        usage = completed_response.get("usage") or {}
        usage_metadata = _build_usage_metadata(usage) if usage else None
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata=usage_metadata,
                response_metadata=self._attribution_metadata(completed_response, usage),
            )
        )

    def _attribution_metadata(self, response: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": response.get("model", self.model),
            "model_name": response.get("model", self.model),
            "usage": usage,
            "provider": PROVIDER_ID,
            "billing_source": BILLING_SOURCE,
            "auth_mode": "oauth_chatgpt_plan",
        }

    def _parse_response(self, response: dict) -> ChatResult:
        result = super()._parse_response(response)
        message = result.generations[0].message
        usage = response.get("usage") or {}
        stamped = self._attribution_metadata(response, usage)
        message.response_metadata = {**getattr(message, "response_metadata", {}), **stamped}
        result.llm_output = {
            **(result.llm_output or {}),
            "provider": PROVIDER_ID,
            "billing_source": BILLING_SOURCE,
            "auth_mode": "oauth_chatgpt_plan",
        }
        return result

    # Keep the inherited ``bind_tools`` — function/custom tools are the only
    # tool class supported on this route. Hosted tool types that a caller may
    # sneak in are stripped here with a warning.
    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        kept: list[dict] = []
        dropped: list[str] = []
        for tool in tools:
            tool_type = tool.get("type") if isinstance(tool, dict) else None
            if tool_type in _UNSUPPORTED_HOSTED_TOOLS:
                dropped.append(str(tool_type))
                continue
            kept.append(tool)
        if dropped:
            logger.warning("ChatGPT-plan route does not support hosted tools %s; dropped from request", sorted(set(dropped)))
        return super()._convert_tools(kept)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text[:500]
