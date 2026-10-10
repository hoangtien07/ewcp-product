"""A3 Task 4 — EgressPolicy tests: allow/deny matrix, fail-closed, tenancy.

Channels under test (hook inventory verified on product/vnext):
- model   — awrap_model_call at Placement.MODEL_PHYSICAL (per physical call)
- tool    — awrap_tool_call at Placement.TOOL_RAW (adjacent to the callable)
- sandbox — capability gate evaluated from runtime.context['app_config']
            (sandbox.use provider class + sandbox.network mode) — the
            middleware policy is NOT network isolation; the sandbox seam is.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ewcp_core.egress_policy import (
    APPROVED_CLOUD,
    CHANNEL_MODEL,
    CHANNEL_SANDBOX_NET,
    CHANNEL_TOOL,
    LOCAL_ONLY,
    RESTRICTED,
    EgressDeniedError,
    EgressMiddlewareContributor,
    EgressPolicy,
    EgressPolicyMiddleware,
    EgressToolMiddleware,
    _deny_marker,
)
from ewcp_core.execution_run_store import ExecutionRunRecord, ExecutionRunStore
from ewcp_core.invoke_tools import INVOKE_TASK_MODE
from ewcp_core.model_policy import _identity

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _FakeModel:
    """Duck-typed chat model carrying the endpoint attributes providers use."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


def _model(**attrs: Any) -> _FakeModel:
    return _FakeModel(**attrs)


def _runtime(**context: Any) -> Any:
    return SimpleNamespace(context=dict(context))


def _model_request(model: Any, **context: Any) -> Any:
    return SimpleNamespace(model=model, runtime=_runtime(**context))


def _tool_request(name: str, args: dict | None = None, **context: Any) -> Any:
    return SimpleNamespace(
        tool_call={"name": name, "args": args or {}, "id": "call-1"},
        runtime=_runtime(**context),
    )


def _sandbox(
    *,
    use: str = "deerflow.community.aio_sandbox:AioSandboxProvider",
    mode: str = "open",
    approval: str = "prompt",
    allow_host_bash: bool = False,
) -> Any:
    return SimpleNamespace(
        use=use,
        allow_host_bash=allow_host_bash,
        network=SimpleNamespace(mode=mode, approval=approval, allow_domains=["pypi.org"]),
    )


def _app_config(**sandbox_kw: Any) -> Any:
    return SimpleNamespace(sandbox=_sandbox(**sandbox_kw))


def _policy(tenant_id_getter: Any = None, store_getter: Any = None, **cfg: Any) -> EgressPolicy:
    return EgressPolicy(
        EgressPolicy.resolve_config(cfg),
        tenant_id_getter=tenant_id_getter,
        store_getter=store_getter,
    )


def _sensitive_ctx(**extra: Any) -> dict[str, Any]:
    ctx = {"user_id": "u-sensitive", "app_config": _app_config()}
    ctx.update(extra)
    return ctx


# --------------------------------------------------------------------------
# config resolution
# --------------------------------------------------------------------------


class TestConfigResolution:
    def test_defaults_fail_closed(self) -> None:
        cfg = EgressPolicy.resolve_config({})
        assert cfg.default_mode == LOCAL_ONLY
        assert cfg.tenant_modes == {}
        # kernel parity: demo/default tenants are built-in non-sensitive
        assert cfg.tenant_classes.get("demo") == "non_sensitive"
        assert cfg.tenant_classes.get("default") == "non_sensitive"

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            EgressPolicy.resolve_config({"egress": {"default_mode": "yolo"}})

    def test_tenant_mode_map(self) -> None:
        cfg = EgressPolicy.resolve_config({"egress": {"tenant_modes": {"acme": "restricted"}}})
        assert cfg.tenant_modes["acme"] == RESTRICTED

    def test_bad_tenant_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            EgressPolicy.resolve_config({"egress": {"tenant_modes": {"acme": "nope"}}})

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EWCP_EGRESS_TENANT_MODES", "acme=approved_cloud,ops=restricted")
        monkeypatch.setenv("EWCP_EGRESS_ALLOWED_DOMAINS", "pypi.org,*.pythonhosted.org")
        cfg = EgressPolicy.resolve_config({})
        assert cfg.tenant_modes == {"acme": APPROVED_CLOUD, "ops": RESTRICTED}
        assert cfg.allowed_domains == ("pypi.org", "*.pythonhosted.org")

    def test_env_tenant_classes_kernel_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # same 'tenant=class,tenant2=class' shape as kernel EWCP_TENANT_DATA_CLASS
        monkeypatch.setenv("EWCP_TENANT_DATA_CLASS", "acme=non_sensitive,bad=sensitive")
        cfg = EgressPolicy.resolve_config({})
        assert cfg.tenant_classes["acme"] == "non_sensitive"
        assert cfg.tenant_classes["bad"] == "sensitive"


# --------------------------------------------------------------------------
# policy resolution (identity + classification + mode)
# --------------------------------------------------------------------------


class TestPolicyResolution:
    def test_user_id_tenant_and_default_mode(self) -> None:
        pol = _policy(egress={"tenant_modes": {"u-sensitive": "restricted"}})
        rp = pol.resolve({"user_id": "u-sensitive"})
        assert rp.tenant_id == "u-sensitive"
        assert rp.mode == RESTRICTED
        assert rp.sensitive is True

    def test_unknown_tenant_is_sensitive_local_only(self) -> None:
        pol = _policy()
        rp = pol.resolve({"user_id": "mystery"})
        assert rp.sensitive is True
        assert rp.mode == LOCAL_ONLY

    def test_ewcp_tenant_id_context_beats_user_id(self) -> None:
        pol = _policy(egress={"tenant_modes": {"t-gov": "approved_cloud"}})
        rp = pol.resolve({"user_id": "u1", "ewcp_tenant_id": "t-gov"})
        assert rp.tenant_id == "t-gov"
        assert rp.mode == APPROVED_CLOUD

    def test_per_run_mode_override(self) -> None:
        pol = _policy(egress={"tenant_modes": {"u1": "approved_cloud"}})
        rp = pol.resolve({"user_id": "u1", "ewcp_egress_mode": "local_only"})
        assert rp.mode == LOCAL_ONLY

    def test_per_run_mode_override_rejects_unknown(self) -> None:
        pol = _policy()
        rp = pol.resolve({"user_id": "u1", "ewcp_egress_mode": "bogus"})
        # spoofed/typo context must not soften policy — falls back to tenant map
        assert rp.mode == LOCAL_ONLY

    def test_non_sensitive_classification(self) -> None:
        pol = _policy()
        rp = pol.resolve({"user_id": "demo"})
        assert rp.sensitive is False

    def test_declared_non_sensitive(self) -> None:
        pol = _policy(egress={"tenant_classes": {"t-pub": "non_sensitive"}})
        rp = pol.resolve({"user_id": "t-pub"})
        assert rp.sensitive is False


# --------------------------------------------------------------------------
# model channel decisions
# --------------------------------------------------------------------------


class TestModelChannel:
    def test_local_only_allows_loopback(self) -> None:
        pol = _policy()
        for host in ("http://127.0.0.1:11434", "http://localhost:8000", "http://[::1]:8000"):
            d = pol.check_model_call(_model(base_url=host), _sensitive_ctx())
            assert d.allowed, (host, d.reason)

    def test_local_only_allows_private_ranges(self) -> None:
        pol = _policy()
        for host in ("http://10.0.0.5:8000", "http://192.168.1.10:8000", "http://172.16.0.9:8000"):
            d = pol.check_model_call(_model(base_url=host), _sensitive_ctx())
            assert d.allowed, (host, d.reason)

    def test_local_only_denies_public_endpoint(self) -> None:
        pol = _policy()
        d = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), _sensitive_ctx())
        assert not d.allowed and d.channel == CHANNEL_MODEL

    def test_local_only_denies_unprovable_endpoint(self) -> None:
        pol = _policy()
        d = pol.check_model_call(_model(), _sensitive_ctx())
        assert not d.allowed

    def test_local_only_allows_declared_local_host(self) -> None:
        pol = _policy(egress={"local_model_endpoints": ["vllm.internal"]})
        d = pol.check_model_call(_model(base_url="http://vllm.internal:8000"), _sensitive_ctx())
        assert d.allowed

    def test_restricted_allows_listed_endpoint(self) -> None:
        pol = _policy(egress={"allowed_model_endpoints": ["api.openai.com"]})
        d = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), _sensitive_ctx(ewcp_egress_mode="restricted"))
        assert d.allowed

    def test_restricted_denies_unlisted_endpoint(self) -> None:
        pol = _policy(egress={"allowed_model_endpoints": ["api.openai.com"]})
        d = pol.check_model_call(_model(base_url="https://api.anthropic.com"), _sensitive_ctx(ewcp_egress_mode="restricted"))
        assert not d.allowed

    def test_restricted_denies_unprovable_endpoint(self) -> None:
        pol = _policy(egress={"allowed_model_endpoints": ["api.openai.com"]})
        d = pol.check_model_call(_model(), _sensitive_ctx(ewcp_egress_mode="restricted"))
        assert not d.allowed

    def test_approved_cloud_allows_approved_endpoint(self) -> None:
        pol = _policy(egress={"approved_model_endpoints": ["generativelanguage.googleapis.com"]})
        d = pol.check_model_call(
            _model(base_url="https://generativelanguage.googleapis.com/v1beta"),
            _sensitive_ctx(ewcp_egress_mode="approved_cloud"),
        )
        assert d.allowed

    def test_approved_cloud_denies_unapproved_endpoint(self) -> None:
        pol = _policy(egress={"approved_model_endpoints": ["generativelanguage.googleapis.com"]})
        d = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), _sensitive_ctx(ewcp_egress_mode="approved_cloud"))
        assert not d.allowed

    def test_approved_cloud_empty_set_denies(self) -> None:
        pol = _policy()
        d = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), _sensitive_ctx(ewcp_egress_mode="approved_cloud"))
        assert not d.allowed

    def test_non_sensitive_allows_any_endpoint(self) -> None:
        pol = _policy()
        d = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), {"user_id": "demo", "app_config": _app_config()})
        assert d.allowed

    def test_endpoint_from_nested_client(self) -> None:
        pol = _policy()
        client = SimpleNamespace(base_url="http://127.0.0.1:9000")
        d = pol.check_model_call(_model(client=client), _sensitive_ctx())
        assert d.allowed


# --------------------------------------------------------------------------
# tool channel decisions
# --------------------------------------------------------------------------


class TestToolChannel:
    def test_local_only_denies_web_tools(self) -> None:
        pol = _policy()
        for name in ("web_search", "web_fetch", "image_search", "web_capture", "browser_navigate"):
            d = pol.check_tool_call(name, {"url": "https://x.com", "query": "q"}, _sensitive_ctx(app_config=_app_config(mode="isolated")))
            assert not d.allowed and d.channel == CHANNEL_TOOL, name

    def test_local_only_denies_unknown_tools(self) -> None:
        pol = _policy()
        d = pol.check_tool_call("acme_mcp_fetch_url", {"url": "https://x.com"}, _sensitive_ctx(app_config=_app_config(mode="isolated")))
        assert not d.allowed

    def test_local_only_allows_filesystem_tools(self) -> None:
        pol = _policy()
        for name in ("read_file", "write_file", "ls", "glob", "grep", "str_replace", "present_files", "task"):
            d = pol.check_tool_call(name, {}, _sensitive_ctx(app_config=_app_config(mode="isolated")))
            assert d.allowed, name

    def test_local_only_allows_bash_when_isolated(self) -> None:
        pol = _policy()
        d = pol.check_tool_call("bash", {"command": "curl https://x.com"}, _sensitive_ctx(app_config=_app_config(mode="isolated")))
        assert d.allowed

    def test_local_only_denies_bash_when_open(self) -> None:
        pol = _policy()
        d = pol.check_tool_call("bash", {"command": "echo hi"}, _sensitive_ctx(app_config=_app_config(mode="open")))
        assert not d.allowed

    def test_restricted_allows_allowed_domain(self) -> None:
        pol = _policy(egress={"allowed_domains": ["pypi.org", "*.pythonhosted.org"]})
        d = pol.check_tool_call("web_fetch", {"url": "https://pypi.org/simple/"}, _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")))
        assert d.allowed

    def test_restricted_allows_wildcard_domain(self) -> None:
        pol = _policy(egress={"allowed_domains": ["*.pythonhosted.org"]})
        d = pol.check_tool_call("web_fetch", {"url": "https://files.pythonhosted.org/x.whl"}, _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")))
        assert d.allowed

    def test_restricted_denies_other_domain(self) -> None:
        pol = _policy(egress={"allowed_domains": ["pypi.org"]})
        d = pol.check_tool_call("web_fetch", {"url": "https://evil.example.com/"}, _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")))
        assert not d.allowed

    def test_restricted_denies_provider_bound_tool(self) -> None:
        pol = _policy(egress={"allowed_domains": ["pypi.org"]})
        d = pol.check_tool_call("web_search", {"query": "q"}, _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")))
        assert not d.allowed

    def test_restricted_allows_declared_tool(self) -> None:
        pol = _policy(egress={"allowed_tools": ["web_search"]})
        d = pol.check_tool_call("web_search", {"query": "q"}, _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")))
        assert d.allowed

    # -- WP-02 audit: unauthorized MCP/tool destinations ----------------------
    # Authorization under restricted is per-destination, not per tool name:
    # an allowed tool carrying url/uri/endpoint args must still land inside
    # allowed_domains, or an operator allowlist silently authorizes every
    # destination the model chooses.

    def test_restricted_allowed_tool_with_unauthorized_destination_denied(self) -> None:
        pol = _policy(egress={"allowed_tools": ["acme_mcp_fetch_url"], "allowed_domains": ["mcp.internal"]})
        d = pol.check_tool_call(
            "acme_mcp_fetch_url",
            {"url": "https://evil.example.com/x"},
            _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")),
        )
        assert not d.allowed and d.channel == CHANNEL_TOOL

    def test_restricted_allowed_tool_with_unauthorized_endpoint_arg_denied(self) -> None:
        pol = _policy(egress={"allowed_tools": ["acme_mcp_call"], "allowed_domains": ["mcp.internal"]})
        d = pol.check_tool_call(
            "acme_mcp_call",
            {"endpoint": "https://evil.example.com/mcp"},
            _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")),
        )
        assert not d.allowed

    def test_restricted_allowed_tool_with_authorized_destination_allowed(self) -> None:
        pol = _policy(egress={"allowed_tools": ["acme_mcp_fetch_url"], "allowed_domains": ["mcp.internal", "*.internal.example"]})
        d = pol.check_tool_call(
            "acme_mcp_fetch_url",
            {"url": "https://mcp.internal/fetch"},
            _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")),
        )
        assert d.allowed
        d = pol.check_tool_call(
            "acme_mcp_fetch_url",
            {"url": "https://erp.internal.example/api"},
            _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")),
        )
        assert d.allowed

    def test_restricted_mcp_tool_without_allowlist_entry_denied(self) -> None:
        # an MCP-named tool not declared in allowed_tools is denied even
        # when its destination happens to be in allowed_domains
        pol = _policy(egress={"allowed_domains": ["mcp.internal"]})
        d = pol.check_tool_call(
            "acme_mcp_fetch_url",
            {"url": "https://mcp.internal/fetch"},
            _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="allowlist", approval="deny")),
        )
        assert not d.allowed

    def test_local_only_denies_mcp_tool_even_for_lan_destination(self) -> None:
        # local_only keeps a strict builtin-only tool surface — an in-boundary
        # LAN MCP server needs `restricted` + allowed_domains, not local_only
        pol = _policy()
        d = pol.check_tool_call(
            "acme_mcp_fetch_url",
            {"url": "http://192.168.1.50:8080/mcp"},
            _sensitive_ctx(app_config=_app_config(mode="isolated")),
        )
        assert not d.allowed

    def test_restricted_denies_bash_when_sandbox_open(self) -> None:
        pol = _policy()
        d = pol.check_tool_call("bash", {"command": "ls"}, _sensitive_ctx(ewcp_egress_mode="restricted", app_config=_app_config(mode="open")))
        assert not d.allowed

    def test_approved_cloud_allows_web_tools(self) -> None:
        pol = _policy()
        d = pol.check_tool_call("web_fetch", {"url": "https://x.com"}, _sensitive_ctx(ewcp_egress_mode="approved_cloud", app_config=_app_config(mode="open")))
        assert d.allowed

    def test_approved_cloud_allows_unknown_tools(self) -> None:
        pol = _policy()
        d = pol.check_tool_call("acme_mcp_fetch_url", {"url": "https://x.com"}, _sensitive_ctx(ewcp_egress_mode="approved_cloud", app_config=_app_config(mode="open")))
        assert d.allowed

    def test_non_sensitive_allows_web_tools(self) -> None:
        pol = _policy()
        d = pol.check_tool_call("web_search", {"query": "q"}, {"user_id": "demo", "app_config": _app_config(mode="open")})
        assert d.allowed


# --------------------------------------------------------------------------
# sandbox-net capability gate (fail-closed)
# --------------------------------------------------------------------------


class TestSandboxNetGate:
    def _check(self, pol: EgressPolicy, **ctx: Any):
        return pol.check_sandbox_net(ctx)

    def test_local_only_accepts_isolated(self) -> None:
        d = self._check(_policy(), **_sensitive_ctx(app_config=_app_config(mode="isolated")))
        assert d.allowed and d.channel == CHANNEL_SANDBOX_NET

    def test_local_only_rejects_open(self) -> None:
        d = self._check(_policy(), **_sensitive_ctx(app_config=_app_config(mode="open")))
        assert not d.allowed

    def test_local_only_rejects_allowlist(self) -> None:
        d = self._check(_policy(), **_sensitive_ctx(app_config=_app_config(mode="allowlist", approval="deny")))
        assert not d.allowed

    def test_local_only_accepts_local_provider_no_bash(self) -> None:
        cfg = _app_config(use="deerflow.sandbox.local:LocalSandboxProvider", allow_host_bash=False)
        d = self._check(_policy(), **_sensitive_ctx(app_config=cfg))
        assert d.allowed

    def test_local_only_rejects_local_provider_host_bash(self) -> None:
        cfg = _app_config(use="deerflow.sandbox.local:LocalSandboxProvider", allow_host_bash=True)
        d = self._check(_policy(), **_sensitive_ctx(app_config=cfg))
        assert not d.allowed

    def test_local_only_rejects_unmanaged_provider(self) -> None:
        cfg = _app_config(use="deerflow.community.e2b_sandbox:E2BSandboxProvider")
        d = self._check(_policy(), **_sensitive_ctx(app_config=cfg))
        assert not d.allowed

    def test_missing_app_config_fails_closed(self) -> None:
        d = self._check(_policy(), user_id="u1")
        assert not d.allowed

    def test_restricted_accepts_allowlist_with_deny_approval(self) -> None:
        pol = _policy(egress={"tenant_modes": {"u-sensitive": "restricted"}})
        d = self._check(pol, **_sensitive_ctx(app_config=_app_config(mode="allowlist", approval="deny")))
        assert d.allowed

    def test_restricted_rejects_prompt_approval(self) -> None:
        pol = _policy(egress={"tenant_modes": {"u-sensitive": "restricted"}})
        d = self._check(pol, **_sensitive_ctx(app_config=_app_config(mode="allowlist", approval="prompt")))
        assert not d.allowed

    def test_restricted_rejects_open(self) -> None:
        pol = _policy(egress={"tenant_modes": {"u-sensitive": "restricted"}})
        d = self._check(pol, **_sensitive_ctx(app_config=_app_config(mode="open")))
        assert not d.allowed

    def test_approved_cloud_accepts_open(self) -> None:
        pol = _policy(egress={"tenant_modes": {"u-sensitive": "approved_cloud"}})
        d = self._check(pol, **_sensitive_ctx(app_config=_app_config(mode="open")))
        assert d.allowed

    def test_non_sensitive_ignores_sandbox(self) -> None:
        d = self._check(_policy(), user_id="demo", app_config=_app_config(mode="open"))
        assert d.allowed


# --------------------------------------------------------------------------
# multi-tenant isolation
# --------------------------------------------------------------------------


class TestMultiTenant:
    def test_tenants_isolated(self) -> None:
        pol = _policy(egress={"tenant_modes": {"ta": "restricted", "tb": "approved_cloud"}, "approved_model_endpoints": ["api.openai.com"], "allowed_model_endpoints": ["vllm.internal"]})
        # tenant A restricted: unlisted model endpoint denied
        d_a = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), {"user_id": "ta", "app_config": _app_config(mode="isolated")})
        assert not d_a.allowed
        # tenant B approved_cloud: same endpoint allowed
        d_b = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), {"user_id": "tb", "app_config": _app_config(mode="open")})
        assert d_b.allowed

    def test_tenant_modes_do_not_bleed(self) -> None:
        pol = _policy(egress={"tenant_modes": {"ta": "approved_cloud"}, "approved_model_endpoints": ["api.openai.com"]})
        d = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), {"user_id": "t-other", "app_config": _app_config(mode="open")})
        # t-other has no declared mode -> local_only default -> deny
        assert not d.allowed


# --------------------------------------------------------------------------
# middleware wiring
# --------------------------------------------------------------------------


class TestContributor:
    def test_two_placements(self) -> None:
        pol = _policy()
        placements = EgressMiddlewareContributor(pol).contribute_middlewares(None, None)
        placements = list(placements)
        assert len(placements) == 2
        spots = {p.placement for p in placements}
        assert {"model_physical", "tool_raw"} == {str(s) for s in spots}
        for p in placements:
            assert str(p.scope) != ""

    def test_both_scopes(self) -> None:
        pol = _policy()
        placements = EgressMiddlewareContributor(pol).contribute_middlewares(None, None)
        for p in placements:
            # lead + subagent must both be covered
            assert p.scope.value == 3

    def test_placements_are_intercepting(self) -> None:
        """Deny decisions must not be wrapped by the host's fail-open
        IsolatedMiddleware: a deny short-circuits without calling the
        downstream handler, which a wrapped middleware reports as a
        failure and skips — silently passing the denied call through."""
        pol = _policy()
        placements = EgressMiddlewareContributor(pol).contribute_middlewares(None, None)
        for p in placements:
            assert p.intercepting is True


class TestModelMiddleware:
    @pytest.mark.asyncio
    async def test_deny_short_circuits_handler(self) -> None:
        pol = _policy()
        mw = EgressPolicyMiddleware(pol)
        handler = AsyncMock()
        req = _model_request(_model(base_url="https://api.openai.com/v1"), **_sensitive_ctx())
        result = await mw.awrap_model_call(req, handler)
        handler.assert_not_called()
        # denial surfaces as an AI message with the ewcp marker
        message = result.result[0] if hasattr(result, "result") else result
        marker = _deny_marker(message)
        assert marker and marker["channel"] == CHANNEL_MODEL

    @pytest.mark.asyncio
    async def test_allow_calls_handler(self) -> None:
        pol = _policy()
        mw = EgressPolicyMiddleware(pol)
        sentinel = object()
        handler = AsyncMock(return_value=sentinel)
        req = _model_request(_model(base_url="http://127.0.0.1:8000"), **_sensitive_ctx())
        result = await mw.awrap_model_call(req, handler)
        handler.assert_awaited_once_with(req)
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_before_agent_fails_closed_when_sandbox_unenforceable(self) -> None:
        pol = _policy()
        mw = EgressPolicyMiddleware(pol)
        with pytest.raises(EgressDeniedError):
            await mw.abefore_agent({}, _runtime(**_sensitive_ctx(app_config=_app_config(mode="open"))))

    @pytest.mark.asyncio
    async def test_before_agent_passes_when_isolated(self) -> None:
        pol = _policy()
        mw = EgressPolicyMiddleware(pol)
        result = await mw.abefore_agent({}, _runtime(**_sensitive_ctx(app_config=_app_config(mode="isolated"))))
        assert result is None

    @pytest.mark.asyncio
    async def test_before_agent_skips_non_sensitive(self) -> None:
        pol = _policy()
        mw = EgressPolicyMiddleware(pol)
        result = await mw.abefore_agent({}, _runtime(user_id="demo", app_config=_app_config(mode="open")))
        assert result is None


class TestToolMiddleware:
    @pytest.mark.asyncio
    async def test_deny_short_circuits_handler(self) -> None:
        pol = _policy()
        mw = EgressToolMiddleware(pol)
        handler = AsyncMock()
        req = _tool_request("web_fetch", {"url": "https://x.com"}, **_sensitive_ctx(app_config=_app_config(mode="isolated")))
        result = await mw.awrap_tool_call(req, handler)
        handler.assert_not_called()
        content = getattr(result, "content", "")
        assert "egress" in content.lower() or "denied" in content.lower()

    @pytest.mark.asyncio
    async def test_allow_calls_handler(self) -> None:
        pol = _policy()
        mw = EgressToolMiddleware(pol)
        sentinel = object()
        handler = AsyncMock(return_value=sentinel)
        req = _tool_request("read_file", {"path": "/mnt/user-data/x"}, **_sensitive_ctx(app_config=_app_config(mode="isolated")))
        result = await mw.awrap_tool_call(req, handler)
        handler.assert_awaited_once_with(req)
        assert result is sentinel


class TestInstallWiring:
    def test_install_registers_egress_contributor(self) -> None:
        from ewcp_core import install
        from ewcp_core.egress_policy import EgressMiddlewareContributor

        seen: dict[str, list[Any]] = {"mw": [], "svc": [], "routers": []}
        registry = SimpleNamespace(
            middlewares=lambda c: seen["mw"].append(c),
            service=lambda s: seen["svc"].append(s),
            routers=lambda rs: seen["routers"].extend(rs),
        )

        install(registry, {})

        egress = [c for c in seen["mw"] if isinstance(c, EgressMiddlewareContributor)]
        assert len(egress) == 1
        # the service's policy is the same object the middleware enforces
        assert egress[0].policy is seen["svc"][0].egress_policy

    def test_status_reports_egress(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from ewcp_core.plugin import EwcpCoreService, build_router

        app = FastAPI()
        app.include_router(build_router(EwcpCoreService(config={"egress": {"default_mode": "restricted"}})))
        resp = TestClient(app).get("/api/ewcp/_status")
        assert resp.status_code == 200
        assert resp.json()["egress"]["default_mode"] == "restricted"


# --------------------------------------------------------------------------
# F1 — genai SDK endpoint resolution + approved_cloud provider admission
# --------------------------------------------------------------------------


def _genai_client(endpoint: str | None = "https://generativelanguage.googleapis.com/") -> Any:
    """Duck-typed google-genai SDK client: the wire base URL lives at
    ``client._api_client._http_options.base_url`` (a pinned ``base_url=`` or
    ``client_args`` override propagates to the same place)."""
    http_options = SimpleNamespace(base_url=endpoint)
    api_client = SimpleNamespace(_http_options=http_options, custom_base_url=None)
    return SimpleNamespace(_api_client=api_client)


def _genai_model(**attrs: Any) -> _FakeModel:
    """ChatGoogleGenerativeAI-shaped model: ``base_url`` on the model itself
    is None unless pinned; the real endpoint only exists inside the SDK
    client chain — the A6 F1 failure."""
    attrs.setdefault("client", _genai_client())
    attrs.setdefault("async_client", _genai_client())
    return _model(**attrs)


class TestGenaiEndpointResolution:
    def test_genai_default_endpoint_resolves_and_admits(self) -> None:
        pol = _policy(egress={"approved_model_endpoints": ["generativelanguage.googleapis.com"]})
        d = pol.check_model_call(_genai_model(), _sensitive_ctx(ewcp_egress_mode="approved_cloud"))
        assert d.allowed, d.reason

    def test_genai_pinned_alt_host_resolves(self) -> None:
        pol = _policy(egress={"approved_model_endpoints": ["genai.corp.internal"]})
        model = _genai_model(client=_genai_client("https://genai.corp.internal/v1"))
        d = pol.check_model_call(model, _sensitive_ctx(ewcp_egress_mode="approved_cloud"))
        assert d.allowed, d.reason

    def test_genai_model_level_base_url_still_wins(self) -> None:
        pol = _policy(egress={"approved_model_endpoints": ["genai.corp.internal"]})
        d = pol.check_model_call(
            _genai_model(base_url="https://genai.corp.internal/v1"),
            _sensitive_ctx(ewcp_egress_mode="approved_cloud"),
        )
        assert d.allowed

    def test_genai_host_not_auto_approved(self) -> None:
        # per-destination check preserved: resolving the host must not
        # admit it — an unlisted genai host stays denied
        pol = _policy(egress={"approved_model_endpoints": ["api.openai.com"]})
        d = pol.check_model_call(_genai_model(), _sensitive_ctx(ewcp_egress_mode="approved_cloud"))
        assert not d.allowed and d.channel == CHANNEL_MODEL

    def test_genai_local_only_still_denied(self) -> None:
        pol = _policy()
        d = pol.check_model_call(_genai_model(), _sensitive_ctx())
        assert not d.allowed

    def test_unknown_provider_client_still_unprovable(self) -> None:
        pol = _policy(egress={"approved_model_endpoints": ["generativelanguage.googleapis.com"]})
        d = pol.check_model_call(_model(client=SimpleNamespace()), _sensitive_ctx(ewcp_egress_mode="approved_cloud"))
        assert not d.allowed

    def test_async_client_chain_resolves(self) -> None:
        pol = _policy(egress={"approved_model_endpoints": ["generativelanguage.googleapis.com"]})
        model = _model(async_client=_genai_client())
        d = pol.check_model_call(model, _sensitive_ctx(ewcp_egress_mode="approved_cloud"))
        assert d.allowed, d.reason


class TestConfiguredProviderAdmission:
    """approved_cloud admits the providers configured in ``models:`` — the
    config-driven provider list — unioned with approved_model_endpoints."""

    def _ctx_with_models(self, *models: Any, **extra: Any) -> dict[str, Any]:
        app_config = SimpleNamespace(sandbox=_sandbox(), models=list(models))
        return _sensitive_ctx(app_config=app_config, **extra)

    def test_configured_genai_provider_admitted(self) -> None:
        pol = _policy()
        ctx = self._ctx_with_models(
            SimpleNamespace(use="langchain_google_genai:ChatGoogleGenerativeAI", model="gemini-x"),
            ewcp_egress_mode="approved_cloud",
        )
        d = pol.check_model_call(_genai_model(), ctx)
        assert d.allowed, d.reason

    def test_configured_provider_explicit_endpoint(self) -> None:
        pol = _policy()
        ctx = self._ctx_with_models(
            SimpleNamespace(use="langchain_openai:ChatOpenAI", base_url="https://gateway.corp/v1"),
            ewcp_egress_mode="approved_cloud",
        )
        d = pol.check_model_call(_model(base_url="https://gateway.corp/v1"), ctx)
        assert d.allowed

    def test_unconfigured_provider_denied(self) -> None:
        pol = _policy()
        ctx = self._ctx_with_models(
            SimpleNamespace(use="langchain_ollama:ChatOllama", base_url="http://127.0.0.1:11434"),
            ewcp_egress_mode="approved_cloud",
        )
        d = pol.check_model_call(_genai_model(), ctx)
        assert not d.allowed

    def test_empty_models_fails_closed(self) -> None:
        pol = _policy()
        ctx = self._ctx_with_models(ewcp_egress_mode="approved_cloud")
        d = pol.check_model_call(_genai_model(), ctx)
        assert not d.allowed

    def test_no_app_config_fails_closed(self) -> None:
        pol = _policy()
        d = pol.check_model_call(_genai_model(), {"user_id": "u1", "ewcp_egress_mode": "approved_cloud"})
        assert not d.allowed

    def test_explicit_approved_list_still_works(self) -> None:
        # approved_model_endpoints remains honored alongside derivation
        pol = _policy(egress={"approved_model_endpoints": ["api.openai.com"]})
        ctx = self._ctx_with_models(
            SimpleNamespace(use="langchain_google_genai:ChatGoogleGenerativeAI"),
            ewcp_egress_mode="approved_cloud",
        )
        d = pol.check_model_call(_model(base_url="https://api.openai.com/v1"), ctx)
        assert d.allowed


# --------------------------------------------------------------------------
# F1 — ewcp_tenant_id propagation + forged context keys
# --------------------------------------------------------------------------


class TestTenantPropagation:
    def _policy_with_tenant(self, tenant: str | None, **cfg: Any) -> EgressPolicy:
        return EgressPolicy(EgressPolicy.resolve_config(cfg), tenant_id_getter=lambda: tenant)

    @pytest.mark.asyncio
    async def test_prepare_context_stamps_configured_tenant(self) -> None:
        pol = self._policy_with_tenant("demo-tenant")
        ctx: dict[str, Any] = {"user_id": "u1"}
        out = await pol.prepare_context(ctx)
        assert out["ewcp_tenant_id"] == "demo-tenant"
        # the live runtime dict is stamped — tools see the same view
        assert ctx["ewcp_tenant_id"] == "demo-tenant"

    @pytest.mark.asyncio
    async def test_prepare_context_overwrites_forged_tenant(self) -> None:
        pol = self._policy_with_tenant("demo-tenant")
        out = await pol.prepare_context({"user_id": "u1", "ewcp_tenant_id": "demo"})
        assert out["ewcp_tenant_id"] == "demo-tenant"

    @pytest.mark.asyncio
    async def test_prepare_context_pops_forged_tenant_when_unconfigured(self) -> None:
        pol = self._policy_with_tenant(None)
        out = await pol.prepare_context({"user_id": "u1", "ewcp_tenant_id": "demo"})
        assert "ewcp_tenant_id" not in out
        rp = pol.resolve(out)
        assert rp.tenant_id == "u1"
        assert rp.sensitive is True

    @pytest.mark.asyncio
    async def test_prepare_context_pops_forged_egress_mode(self) -> None:
        pol = self._policy_with_tenant("demo-tenant")
        out = await pol.prepare_context({"ewcp_egress_mode": "approved_cloud"})
        assert "ewcp_egress_mode" not in out

    @pytest.mark.asyncio
    async def test_prepare_context_pops_forged_kernel_identity(self) -> None:
        # a client-forged kernel.workrun_id would redirect governed identity
        pol = self._policy_with_tenant("demo-tenant")
        out = await pol.prepare_context({"kernel": {"workrun_id": "forged"}})
        assert "kernel" not in out

    @pytest.mark.asyncio
    async def test_prepare_context_handles_non_mapping(self) -> None:
        pol = self._policy_with_tenant("demo-tenant")
        out = await pol.prepare_context(None)
        assert out["ewcp_tenant_id"] == "demo-tenant"

    @pytest.mark.asyncio
    async def test_before_agent_stamps_tenant_into_runtime_context(self) -> None:
        pol = self._policy_with_tenant("demo-tenant", egress={"tenant_classes": {"demo-tenant": "non_sensitive"}})
        mw = EgressPolicyMiddleware(pol)
        runtime = _runtime(user_id="u-sensitive", app_config=_app_config(mode="open"))
        await mw.abefore_agent({}, runtime)
        assert runtime.context["ewcp_tenant_id"] == "demo-tenant"

    @pytest.mark.asyncio
    async def test_stamped_tenant_drives_classification(self) -> None:
        # demo-tenant declared non_sensitive: the stamped tenant admits the
        # genai call even though user_id alone would classify sensitive
        pol = self._policy_with_tenant("demo-tenant", egress={"tenant_classes": {"demo-tenant": "non_sensitive"}})
        mw = EgressPolicyMiddleware(pol)
        handler = AsyncMock(return_value=object())
        req = _model_request(
            _genai_model(),
            user_id="u-sensitive",
            ewcp_tenant_id="sensitive-tenant",  # forged — overwritten by the stamp
            app_config=_app_config(mode="open"),
        )
        await mw.awrap_model_call(req, handler)
        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sensitive_tenant_denies_cloud_model(self) -> None:
        pol = self._policy_with_tenant("s-tenant")  # undeclared class → sensitive + local_only
        mw = EgressPolicyMiddleware(pol)
        handler = AsyncMock()
        req = _model_request(_genai_model(), app_config=_app_config(mode="isolated"))
        result = await mw.awrap_model_call(req, handler)
        handler.assert_not_called()
        marker = _deny_marker(result)
        assert marker and marker["channel"] == CHANNEL_MODEL

    @pytest.mark.asyncio
    async def test_forged_egress_mode_does_not_bypass_tool_gate(self) -> None:
        pol = self._policy_with_tenant("s-tenant")
        mw = EgressToolMiddleware(pol)
        handler = AsyncMock()
        req = _tool_request(
            "web_fetch",
            {"url": "https://x.com"},
            ewcp_egress_mode="approved_cloud",  # forged — must be neutralized
            app_config=_app_config(mode="open"),
        )
        await mw.awrap_tool_call(req, handler)
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_forged_context_cannot_skip_run_admission(self) -> None:
        pol = self._policy_with_tenant("s-tenant")
        mw = EgressPolicyMiddleware(pol)
        with pytest.raises(EgressDeniedError):
            await mw.abefore_agent(
                {},
                _runtime(
                    ewcp_tenant_id="demo",
                    ewcp_egress_mode="approved_cloud",
                    user_id="u1",
                    app_config=_app_config(mode="open"),
                ),
            )


# --------------------------------------------------------------------------
# Server-owned vs client-forged context — governed workrun identity.
# ``kernel.workrun_id`` is server-owned: the ExecutionRunMap binding written
# by the governed launch path (api_routes → RunLauncher admission) is the
# only source. ``prepare_context`` strips the client-carried copy, then
# re-stamps the authenticated binding so ``model_policy._identity``
# attributes budget to the workrun. Store is real SQLite in-memory — the
# same session-factory shape as ``ExtensionRuntimeDeps.session_factory``.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def execution_run_store() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = ExecutionRunStore(sf)
    await store.ensure_schema()
    yield store
    await engine.dispose()


def _stored_policy(store: Any, tenant: str | None = "s-tenant", **cfg: Any) -> EgressPolicy:
    return _policy(tenant_id_getter=lambda: tenant, store_getter=lambda: store, **cfg)


async def _bind_governed(
    store: ExecutionRunStore,
    *,
    thread_id: str = "t-gov",
    run_id: str | None = "r-1",
    workrun_id: str = "wr-1",
    task_mode: str = "governed",
) -> ExecutionRunRecord:
    """Insert the map row the governed launch path writes at admission."""
    return await store.insert(
        ExecutionRunRecord.new(
            thread_id=thread_id,
            run_id=run_id,
            workrun_id=workrun_id,
            task_mode=task_mode,
            status="running",
            intent="governed intent",
            idempotency_key=f"idem-{thread_id}-{run_id}-{workrun_id}-{task_mode}",
            created_by="u1",
        )
    )


class TestGovernedIdentityStamp:
    @pytest.mark.asyncio
    async def test_governed_workrun_stamped_from_server_binding(self, execution_run_store: Any) -> None:
        """The api_routes launch injection survives: prepare_context re-stamps
        kernel.workrun_id from the map row after the forged-key strip, and
        _identity resolves the governed workrun for budget attribution."""
        await _bind_governed(execution_run_store)
        pol = _stored_policy(execution_run_store)
        out = await pol.prepare_context({"thread_id": "t-gov", "run_id": "r-1"})
        assert out["kernel"] == {"workrun_id": "wr-1"}
        assert _identity(out) == ("wr-1", True)

    @pytest.mark.asyncio
    async def test_server_binding_overrides_forged_kernel(self, execution_run_store: Any) -> None:
        """A forged kernel.workrun_id is stripped, then the real binding is
        stamped — the forged value never reaches budget attribution."""
        await _bind_governed(execution_run_store)
        pol = _stored_policy(execution_run_store)
        out = await pol.prepare_context({"thread_id": "t-gov", "run_id": "r-1", "kernel": {"workrun_id": "wr-forged"}})
        assert out["kernel"]["workrun_id"] == "wr-1"
        assert _identity(out) == ("wr-1", True)

    @pytest.mark.asyncio
    async def test_forged_kernel_without_binding_stays_general(self, execution_run_store: Any) -> None:
        """No governed binding for this thread → the forged kernel dies with
        the strip and identity falls back to run_id, general lane."""
        pol = _stored_policy(execution_run_store)
        out = await pol.prepare_context({"thread_id": "t-plain", "run_id": "r-x", "kernel": {"workrun_id": "wr-stolen"}})
        assert "kernel" not in out
        assert _identity(out) == ("r-x", False)

    @pytest.mark.asyncio
    async def test_cross_thread_workrun_cannot_be_stolen(self, execution_run_store: Any) -> None:
        """The lookup keys on THIS run's server-owned thread_id — a governed
        binding on another thread never stamps here."""
        await _bind_governed(execution_run_store, thread_id="t-other", run_id="r-o", workrun_id="wr-other")
        pol = _stored_policy(execution_run_store)
        out = await pol.prepare_context({"thread_id": "t-gov", "run_id": "r-1", "kernel": {"workrun_id": "wr-other"}})
        assert "kernel" not in out
        assert _identity(out) == ("r-1", False)

    @pytest.mark.asyncio
    async def test_invoke_row_never_tags_run_governed(self, execution_run_store: Any) -> None:
        """task_mode='invoke' rows belong to a capability call's account, not
        the calling run's identity — the same exclusion
        invoke_tools._execution_run_id applies."""
        await _bind_governed(execution_run_store, task_mode=INVOKE_TASK_MODE, workrun_id="wr-inv")
        pol = _stored_policy(execution_run_store)
        out = await pol.prepare_context({"thread_id": "t-gov", "run_id": "r-1"})
        assert "kernel" not in out
        assert _identity(out) == ("r-1", False)

    @pytest.mark.asyncio
    async def test_followup_run_on_governed_thread_inherits_workrun(self, execution_run_store: Any) -> None:
        """The map row's run_id is the launch run; a follow-up run on the same
        governed thread (new run_id) still attributes to the workrun."""
        await _bind_governed(execution_run_store, run_id="r-1")
        pol = _stored_policy(execution_run_store)
        out = await pol.prepare_context({"thread_id": "t-gov", "run_id": "r-2"})
        assert out["kernel"]["workrun_id"] == "wr-1"
        assert _identity(out) == ("wr-1", True)

    @pytest.mark.asyncio
    async def test_store_absent_leaves_forged_kernel_dropped(self) -> None:
        """Kernel-less deployment (no store): a forged kernel still dies with
        the strip and nothing is stamped."""
        pol = _stored_policy(None)
        out = await pol.prepare_context({"thread_id": "t-1", "run_id": "r-1", "kernel": {"workrun_id": "wr-x"}})
        assert "kernel" not in out
        assert _identity(out) == ("r-1", False)

    @pytest.mark.asyncio
    async def test_before_agent_stamps_governed_identity_into_runtime_context(self, execution_run_store: Any) -> None:
        """End-to-end through the middleware: the forged kernel in the live
        runtime context is replaced by the authenticated workrun, and
        _identity resolves governed on the same dict tools read."""
        await _bind_governed(execution_run_store)
        pol = _stored_policy(
            execution_run_store,
            tenant="demo-tenant",
            egress={"tenant_classes": {"demo-tenant": "non_sensitive"}},
        )
        mw = EgressPolicyMiddleware(pol)
        runtime = _runtime(
            thread_id="t-gov",
            run_id="r-1",
            user_id="u1",
            app_config=_app_config(mode="open"),
            kernel={"workrun_id": "wr-forged"},
        )
        await mw.abefore_agent({}, runtime)
        assert runtime.context["kernel"] == {"workrun_id": "wr-1"}
        assert _identity(runtime.context) == ("wr-1", True)


class TestApprovedCloudForgeBoundary:
    """approved_cloud admits only ``approved_model_endpoints`` unioned with
    the operators' configured ``models:`` providers — forged context keys
    cannot widen it, and the forged mode itself dies in prepare_context."""

    @pytest.mark.asyncio
    async def test_forged_egress_mode_cannot_elevate_model_call(self) -> None:
        """Forged ewcp_egress_mode=approved_cloud is stripped before resolve();
        the configured default_mode (local_only) still denies the cloud host."""
        pol = _policy(tenant_id_getter=lambda: "s-tenant")
        mw = EgressPolicyMiddleware(pol)
        handler = AsyncMock()
        req = _model_request(
            _model(base_url="https://api.openai.com/v1"),
            ewcp_egress_mode="approved_cloud",
            app_config=_app_config(mode="open"),
            user_id="u1",
        )
        result = await mw.awrap_model_call(req, handler)
        handler.assert_not_called()
        marker = _deny_marker(result)
        assert marker and marker["channel"] == CHANNEL_MODEL

    @pytest.mark.asyncio
    async def test_forged_tenant_cannot_unlock_approved_cloud(self) -> None:
        """Forged ewcp_tenant_id=demo (built-in non_sensitive) is popped when
        no tenant is configured — the run classifies sensitive and the cloud
        endpoint stays denied."""
        pol = _policy()  # no tenant_id_getter → nothing server-stamps
        mw = EgressPolicyMiddleware(pol)
        handler = AsyncMock()
        req = _model_request(
            _model(base_url="https://api.openai.com/v1"),
            ewcp_tenant_id="demo",
            ewcp_egress_mode="approved_cloud",
            app_config=_app_config(mode="open"),
            user_id="u1",
        )
        result = await mw.awrap_model_call(req, handler)
        handler.assert_not_called()
        marker = _deny_marker(result)
        assert marker and marker["channel"] == CHANNEL_MODEL

    @pytest.mark.asyncio
    async def test_approved_cloud_denies_endpoint_outside_admit_list(self) -> None:
        """Under a legitimately-approved_cloud tenant, an endpoint that is in
        neither approved_model_endpoints nor the configured models stays
        denied — the configured-provider derivation widens nothing else."""
        pol = _policy(
            tenant_id_getter=lambda: "s-tenant",
            egress={
                "default_mode": "approved_cloud",
                "approved_model_endpoints": ["api.openai.com"],
                "tenant_classes": {"s-tenant": "sensitive"},
            },
        )
        mw = EgressPolicyMiddleware(pol)
        handler = AsyncMock()
        req = _model_request(
            _model(base_url="https://rogue-llm.example.com/v1"),
            app_config=_app_config(mode="open"),
            user_id="u1",
        )
        result = await mw.awrap_model_call(req, handler)
        handler.assert_not_called()
        marker = _deny_marker(result)
        assert marker and marker["channel"] == CHANNEL_MODEL
