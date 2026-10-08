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


def _policy(**cfg: Any) -> EgressPolicy:
    return EgressPolicy(EgressPolicy.resolve_config(cfg))


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
