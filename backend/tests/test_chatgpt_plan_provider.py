"""Mocked tests for ChatGPTPlanChatModel — the official SIWC provider.

Covers the spec test matrix with fully mocked transport: payload contract,
streaming, tool calls, error classification, refresh, account isolation, and
secret hygiene. No network access.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.models import chatgpt_plan_oauth as oauth
from deerflow.models.chatgpt_plan_provider import (
    BILLING_SOURCE,
    PROVIDER_ID,
    ChatGPTPlanAuthError,
    ChatGPTPlanChatModel,
    ChatGPTPlanIncompleteError,
    ChatGPTPlanRateLimitError,
    ChatGPTPlanStreamInterruptedError,
    ChatGPTPlanTransientError,
    ChatGPTPlanUsageLimitError,
)


def make_record(**overrides) -> oauth.CredentialRecord:
    base = {
        "email": "user@example.com",
        "issuer": oauth.ISSUER,
        "subject": "sub-1",
        "client_id": "oaiapp_abc123",
        "ext_agent_host_id": "urn:uuid:host-1",
        "id_token": "idt",
        "access_token": "at-secret-1",
        "refresh_token": "rt-secret-1",
        "scopes": oauth.REQUESTED_SCOPES.split(),
        "access_expires_at": time.time() + 3600,
    }
    base.update(overrides)
    return oauth.CredentialRecord(**base)


@pytest.fixture()
def store(tmp_path):
    s = oauth.CredentialStore(tmp_path)
    s.save(make_record())
    return s


def sse_lines(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def completed_response(text: str = "Hello", tool_call: dict | None = None, usage: dict | None = None) -> dict:
    output: list[dict] = [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
    if tool_call:
        output.append(tool_call)
    return {
        "type": "response.completed",
        "response": {
            "id": "resp-1",
            "status": "completed",
            "model": "gpt-5.2-codex",
            "output": output,
            "usage": usage or {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    }


def sse_client(handler_or_events) -> httpx.Client:
    """Client whose POST /responses returns the given SSE events (or delegates
    to a custom handler for error paths)."""

    if callable(handler_or_events):
        handler = handler_or_events
    else:
        body = sse_lines(*handler_or_events)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(oauth.RESPONSES_URL):
                return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
            raise AssertionError(f"unexpected request {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture()
def model(store):
    m = ChatGPTPlanChatModel(model="gpt-5.2-codex", credentials_dir=str(store.root))
    return m


def set_transport(model: ChatGPTPlanChatModel, client: httpx.Client) -> None:
    model._http_client = client


# ---------------------------------------------------------------------------
# Construction / payload contract
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_llm_type_and_identity(self, model):
        assert model._llm_type == "chatgpt-plan-oauth"
        assert model._record.client_id == "oaiapp_abc123"

    def test_no_codex_credentials_loaded(self, store, monkeypatch):
        from deerflow.models import openai_codex_provider as codex

        monkeypatch.setattr(codex, "load_codex_cli_credential", lambda: pytest.fail("must not load Codex CLI credentials"))
        ChatGPTPlanChatModel(model="gpt-5.2-codex", credentials_dir=str(store.root))

    def test_missing_account_fails_construction(self, tmp_path):
        with pytest.raises(oauth.CredentialNotFoundError):
            ChatGPTPlanChatModel(model="gpt-5.2-codex", credentials_dir=str(tmp_path))


class TestPayloadContract:
    def test_payload_shape(self, model):
        messages = [
            SystemMessage(content="You are terse."),
            HumanMessage(content="Hi"),
            AIMessage(content="Hello.", tool_calls=[{"id": "call_1", "name": "search", "args": {"q": "x"}}]),
            ToolMessage(content="result", tool_call_id="call_1"),
            HumanMessage(content="Next"),
        ]
        payload = model._build_payload(messages, tools=None)
        assert payload["model"] == "gpt-5.2-codex"
        assert payload["store"] is False
        assert payload["stream"] is True
        assert payload["instructions"] == "You are terse."
        assert not any(item.get("role") == "system" or item.get("type") == "message" and item.get("role") == "system" for item in payload["input"])
        # function_call + function_call_output are correlated by call_id
        assert {"type": "function_call", "name": "search", "arguments": json.dumps({"q": "x"}), "call_id": "call_1"} in payload["input"]
        assert {"type": "function_call_output", "call_id": "call_1", "output": "result"} in payload["input"]
        # No unsupported field ever reaches the body
        for key in ("temperature", "top_p", "max_output_tokens", "metadata", "truncation", "user", "previous_response_id"):
            assert key not in payload

    def test_reasoning_effort_only_when_set(self, store):
        m = ChatGPTPlanChatModel(model="m", credentials_dir=str(store.root), reasoning_effort="high")
        assert m._build_payload([HumanMessage(content="x")], None)["reasoning"] == {"effort": "high"}
        m2 = ChatGPTPlanChatModel(model="m", credentials_dir=str(store.root))
        assert "reasoning" not in m2._build_payload([HumanMessage(content="x")], None)

    def test_generic_config_never_reaches_payload(self, store):
        m = ChatGPTPlanChatModel(model="m", credentials_dir=str(store.root), temperature=0.7, max_tokens=99, api_key="leak-me", extra_body={"x": 1})
        payload = m._build_payload([HumanMessage(content="x")], tools=None)
        for key in ("temperature", "max_tokens", "api_key", "extra_body"):
            assert key not in payload
            assert key not in json.dumps(payload)

    def test_hosted_tools_dropped(self, model):
        converted = model._convert_tools(
            [
                {"type": "function", "name": "search"},
                {"type": "image_generation"},
                {"type": "mcp"},
            ]
        )
        assert converted == [{"type": "function", "name": "search", "description": "", "parameters": {}}]


# ---------------------------------------------------------------------------
# Inference (mocked SSE)
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_basic_generate(self, model):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, content=sse_lines(completed_response("Hi there")), headers={"content-type": "text/event-stream"})

        set_transport(model, sse_client(handler))
        result = model.invoke([HumanMessage(content="Hello")])
        assert isinstance(result, AIMessage)
        assert result.content == "Hi there"
        # Auth contract: Bearer access token, public Responses URL, never backend-api.
        assert captured["headers"]["authorization"] == "Bearer at-secret-1"
        # Attribution
        assert result.usage_metadata["input_tokens"] == 10
        assert result.usage_metadata["output_tokens"] == 5
        assert "billing_source" not in result.usage_metadata  # UsageMetadata is int-only
        assert result.response_metadata["billing_source"] == BILLING_SOURCE
        assert result.response_metadata["provider"] == PROVIDER_ID
        assert result.response_metadata["auth_mode"] == "oauth_chatgpt_plan"

    def test_endpoint_is_public_api(self, model):
        urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return httpx.Response(200, content=sse_lines(completed_response()), headers={"content-type": "text/event-stream"})

        set_transport(model, sse_client(handler))
        model.invoke([HumanMessage(content="x")])
        assert urls == [oauth.RESPONSES_URL]
        assert "backend-api" not in urls[0]
        assert "chatgpt.com" not in urls[0]

    def test_tool_call_parse(self, model):
        tool_call = {"type": "function_call", "name": "search", "arguments": '{"q":"x"}', "call_id": "call_9"}
        set_transport(model, sse_client([completed_response("", tool_call=tool_call)]))
        result = model.invoke([HumanMessage(content="search please")])
        assert result.tool_calls == [{"name": "search", "args": {"q": "x"}, "id": "call_9", "type": "tool_call"}]

    def test_multi_step_tool_round_trip(self, model):
        """Second turn: tool result feeds back as function_call_output."""
        requests_seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(json.loads(request.content))
            return httpx.Response(200, content=sse_lines(completed_response("done")), headers={"content-type": "text/event-stream"})

        set_transport(model, sse_client(handler))
        model.invoke(
            [
                HumanMessage(content="q"),
                AIMessage(content="", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
                ToolMessage(content="out", tool_call_id="c1"),
            ]
        )
        body = requests_seen[0]
        assert body["input"][-1] == {"type": "function_call_output", "call_id": "c1", "output": "out"}


class TestStreaming:
    def test_stream_emits_text_and_usage(self, model):
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message"}},
            {"type": "response.output_text.delta", "output_index": 0, "delta": "Hel"},
            {"type": "response.output_text.delta", "output_index": 0, "delta": "lo"},
            completed_response("Hello"),
        ]
        set_transport(model, sse_client(events))
        chunks = list(model.stream([HumanMessage(content="hi")]))
        text = "".join(c.content for c in chunks if isinstance(c.content, str))
        assert text == "Hello"
        usage_chunks = [c for c in chunks if c.usage_metadata]
        assert usage_chunks, "expected a chunk carrying usage metadata"
        usage_chunk = usage_chunks[-1]
        assert usage_chunk.usage_metadata["input_tokens"] == 10
        assert "billing_source" not in usage_chunk.usage_metadata
        assert usage_chunk.response_metadata["billing_source"] == BILLING_SOURCE
        assert usage_chunk.response_metadata["provider"] == PROVIDER_ID

    def test_stream_tool_call_chunks(self, model):
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "name": "search", "call_id": "call_5", "id": "item-1"}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"q":'},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '"x"}'},
            completed_response(""),
        ]
        set_transport(model, sse_client(events))
        chunks = list(model.stream([HumanMessage(content="hi")]))
        tool_chunks = [c for c in chunks if c.tool_call_chunks]
        assert tool_chunks[0].tool_call_chunks[0]["name"] == "search"
        assert tool_chunks[0].tool_call_chunks[0]["id"] == "call_5"
        args = "".join(tc.tool_call_chunks[0]["args"] for tc in tool_chunks)
        assert json.loads(args) == {"q": "x"}

    def test_stream_eof_without_completed(self, model):
        set_transport(model, sse_client([{"type": "response.output_text.delta", "output_index": 0, "delta": "partial"}]))
        with pytest.raises(ChatGPTPlanStreamInterruptedError):
            list(model.stream([HumanMessage(content="hi")]))


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def _run(self, model, handler_or_events, **kwargs):
        set_transport(model, sse_client(handler_or_events))
        return model.invoke([HumanMessage(content="x")], **kwargs)

    def test_failed_usage_limit_is_non_retriable_quota(self, model):
        event = {
            "type": "response.failed",
            "response": {"id": "r1", "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "plan usage limit reached"}},
        }
        with pytest.raises(ChatGPTPlanUsageLimitError) as exc:
            self._run(model, [event])
        assert exc.value.code == "subscription_sharing_usage_limit_exceeded"

    def test_usage_limit_http_429_body_code(self, model):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}})

        with pytest.raises(ChatGPTPlanUsageLimitError):
            self._run(model, handler)

    def test_user_not_eligible_403(self, model):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "subscription_sharing_user_not_eligible", "message": "not eligible"}})

        from deerflow.models.chatgpt_plan_provider import ChatGPTPlanNotEligibleError

        with pytest.raises(ChatGPTPlanNotEligibleError):
            self._run(model, handler)

    def test_incomplete_response(self, model):
        event = {"type": "response.incomplete", "response": {"id": "r1", "incomplete_details": {"reason": "max_output_tokens"}}}
        with pytest.raises(ChatGPTPlanIncompleteError):
            self._run(model, [event])

    def test_401_auth(self, model):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "unauthorized"})

        with pytest.raises(ChatGPTPlanAuthError):
            self._run(model, handler)

    def test_generic_429_retries_then_succeeds(self, model):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"error": {"message": "slow down"}})
            return httpx.Response(200, content=sse_lines(completed_response("ok")), headers={"content-type": "text/event-stream"})

        set_transport(model, sse_client(handler))
        import deerflow.models.chatgpt_plan_provider as prov

        orig_sleep = prov.time.sleep
        prov.time.sleep = lambda *_: None
        try:
            result = model.invoke([HumanMessage(content="x")])
        finally:
            prov.time.sleep = orig_sleep
        assert result.content == "ok"
        assert calls["n"] == 2

    def test_generic_503_transient_then_fail(self, model):
        import deerflow.models.chatgpt_plan_provider as prov

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "unavailable"})

        set_transport(model, sse_client(handler))
        orig_sleep = prov.time.sleep
        prov.time.sleep = lambda *_: None
        try:
            with pytest.raises(ChatGPTPlanTransientError):
                model.invoke([HumanMessage(content="x")])
        finally:
            prov.time.sleep = orig_sleep


# ---------------------------------------------------------------------------
# Scope / refresh / isolation / hygiene
# ---------------------------------------------------------------------------


class TestScopeAndRefresh:
    def test_missing_inference_scope_fails_request(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record(scopes="openid profile email offline_access resource.invoke".split()))
        m = ChatGPTPlanChatModel(model="m", credentials_dir=str(store.root))
        with pytest.raises(oauth.InferenceScopeMissingError):
            m.invoke([HumanMessage(content="x")])

    def test_expired_token_refreshes_before_request(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record(access_expires_at=time.time() - 5))
        m = ChatGPTPlanChatModel(model="m", credentials_dir=str(store.root))
        seen_auth = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(oauth.TOKEN_URL):
                form = dict(urllib.parse.parse_qsl(request.content.decode()))
                assert form["grant_type"] == "refresh_token"
                return httpx.Response(200, json={"access_token": "at-2", "refresh_token": "rt-2", "token_type": "Bearer", "expires_in": 3600, "scope": oauth.REQUESTED_SCOPES})
            seen_auth["auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=sse_lines(completed_response("ok")), headers={"content-type": "text/event-stream"})

        set_transport(m, sse_client(handler))
        m.invoke([HumanMessage(content="x")])
        assert seen_auth["auth"] == "Bearer at-2"


class TestAccountIsolation:
    def test_selector_picks_right_token(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record(client_id="oaiapp_A", email="a@x.com", access_token="at-A"))
        store.save(make_record(client_id="oaiapp_B", email="b@x.com", subject="sub-2", access_token="at-B"))
        m = ChatGPTPlanChatModel(model="m", credentials_dir=str(store.root), account="b@x.com")
        assert m._record.client_id == "oaiapp_B"
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, content=sse_lines(completed_response("ok")), headers={"content-type": "text/event-stream"})

        set_transport(m, sse_client(handler))
        m.invoke([HumanMessage(content="x")])
        assert seen["auth"] == "Bearer at-B"

    def test_no_selector_with_multiple_accounts_fails(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record(client_id="oaiapp_A", email="a@x.com"))
        store.save(make_record(client_id="oaiapp_B", email="b@x.com", subject="sub-2"))
        with pytest.raises(oauth.CredentialNotFoundError, match="Multiple"):
            ChatGPTPlanChatModel(model="m", credentials_dir=str(store.root))


class TestSecretHygiene:
    def test_serialization_has_no_tokens(self, model):
        dumped = json.dumps(model.model_dump(), default=str)
        for secret in ("at-secret-1", "rt-secret-1"):
            assert secret not in dumped
        dumped_json = json.dumps(getattr(model, "to_json", lambda: {})(), default=str)
        for secret in ("at-secret-1", "rt-secret-1"):
            assert secret not in dumped_json

    def test_error_messages_do_not_leak_tokens(self, model):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"plain failure")

        import deerflow.models.chatgpt_plan_provider as prov

        set_transport(model, sse_client(handler))
        orig_sleep = prov.time.sleep
        prov.time.sleep = lambda *_: None
        try:
            with pytest.raises(Exception) as exc:
                model.invoke([HumanMessage(content="x")])
        finally:
            prov.time.sleep = orig_sleep
        assert "at-secret-1" not in str(exc.value)
        assert "rt-secret-1" not in str(exc.value)


def _middleware():
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
    from deerflow.config.app_config import AppConfig
    from deerflow.config.sandbox_config import SandboxConfig

    return LLMErrorHandlingMiddleware(app_config=AppConfig(sandbox=SandboxConfig(use="test")))


class TestErrorClassification:
    """Provider errors must classify correctly through the middleware."""

    def test_usage_limit_classifies_quota(self):
        retriable, reason = _middleware()._classify_error(ChatGPTPlanUsageLimitError("x", code="subscription_sharing_usage_limit_exceeded"))
        assert retriable is False
        assert reason == "quota"

    def test_auth_classifies_auth(self):
        retriable, reason = _middleware()._classify_error(ChatGPTPlanAuthError("Unauthorized: bad token", status_code=401))
        assert retriable is False
        assert reason == "auth"

    def test_rate_limit_classifies_transient_or_burst(self):
        _retriable, reason = _middleware()._classify_error(ChatGPTPlanRateLimitError("Rate limited: slow down", status_code=429))
        assert reason in {"transient", "burst_rate", "busy"}
