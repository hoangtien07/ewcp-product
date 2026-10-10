"""Cross-PR regression — a client-forged ``ewcp_tenant_id`` (run body
``config['context']``) must never override authenticated budget attribution.

Composition under test — post-#55 rework + merged #59, all REAL seams:

- ``worker._build_runtime_context`` merges ``body.config['context']`` into the
  run's live ``runtime.context`` verbatim (EWCP keys are extension-owned, not
  upstream server-owned) — so a forged ``ewcp_tenant_id`` / ``ewcp_egress_mode``
  / ``kernel.workrun_id`` lands in the shared dict untouched, while
  ``thread_id``/``run_id`` stay server-owned (``setdefault``).
- ``install()`` registers the budget contributor (``EwcpCoreService``) BEFORE
  ``EgressMiddlewareContributor``; at ``Placement.MODEL_PHYSICAL`` both are
  ``order=0`` + ``intercepting=True`` so the first-registered sorts outermost —
  ``BudgetAdmissionMiddleware`` runs OUTER of ``EgressPolicyMiddleware`` on
  ``awrap_model_call`` and admits BEFORE egress's per-call ``prepare_context``
  would run.
- The defense is therefore ``EgressPolicyMiddleware.abefore_agent``: it runs
  ``prepare_context`` on the live context dict at agent start — stripping
  forged keys, re-stamping ``ewcp_tenant_id`` from ``tenant_id_getter`` and
  ``kernel.workrun_id`` from the ExecutionRunMap binding — so every later
  model call (and every budget admission) reads authenticated values.
- The admission itself rides the real ``KernelBudgetClient`` → ``KernelClient``
  serialization; only the httpx transport is stubbed, so the recorded
  ``POST /budget/admissions`` body is byte-for-byte what the kernel receives.

Pinned invariants:

- ``tenant_id`` on the admission wire = the server-stamped tenant
  (``tenant_id`` → ``invoke.tenant_id`` → ``budget.tenant_id``), never the
  client-supplied value;
- ``execution_run_id`` = the ExecutionRunMap workrun (governed) or the run id
  (general), never a forged ``kernel.workrun_id``;
- the live ``runtime.context`` the model path sees carries no forged keys.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

# The real merge seam: caller config['context'] -> runtime.context.
from deerflow.runtime.runs.worker import _build_runtime_context
from deerflow_extension_api.placement import Placement
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ewcp_core import install
from ewcp_core.egress_policy import EgressPolicyMiddleware
from ewcp_core.execution_run_store import ExecutionRunRecord
from ewcp_core.kernel_client import KernelClient
from ewcp_core.model_policy import BudgetAdmissionMiddleware


@pytest.fixture(autouse=True)
def _clean_ewcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kernel/egress config resolves env-first; ambient EWCP_* must not leak in."""
    for name in (
        "EWCP_KERNEL_URL",
        "EWCP_KERNEL_API_KEY",
        "EWCP_EGRESS_DEFAULT_MODE",
        "EWCP_EGRESS_TENANT_MODES",
        "EWCP_EGRESS_TENANT_CLASSES",
        "EWCP_EGRESS_LOCAL_MODEL_ENDPOINTS",
        "EWCP_EGRESS_ALLOWED_MODEL_ENDPOINTS",
        "EWCP_EGRESS_APPROVED_MODEL_ENDPOINTS",
        "EWCP_EGRESS_ALLOWED_DOMAINS",
        "EWCP_EGRESS_ALLOWED_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# fakes — kernel wire + provider boundary; everything else is production code
# --------------------------------------------------------------------------


class _KernelWire:
    """``httpx.MockTransport`` handler recording the kernel's budget wire.

    Replays the ``/budget/admissions`` (+ settle/release) contract the real
    kernel speaks, so assertions run against the exact JSON the middleware
    serialized — no live kernel needed.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if request.method == "POST" and path == "/budget/admissions":
            return httpx.Response(200, json={"admission_id": "adm-test-1", "status": "reserved"})
        if request.method == "POST" and (path.endswith("/settle") or path.endswith("/release")):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "unhandled"})

    def bodies(self, path_suffix: str) -> list[dict[str, Any]]:
        return [body for _, path, body in self.requests if path.endswith(path_suffix)]


class _ProviderSpy:
    """Innermost model-call handler: records the request + the context dict
    the provider boundary actually observed."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.contexts: list[dict[str, Any]] = []

    async def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        context = getattr(getattr(request, "runtime", None), "context", None)
        self.contexts.append(dict(context or {}))
        return SimpleNamespace(result=[AIMessage(content="ok", usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})])


def _app_config(*, mode: str = "isolated") -> Any:
    """Minimal sandbox config: AIO provider with an enforceable network mode."""
    return SimpleNamespace(
        sandbox=SimpleNamespace(
            use="deerflow.community.aio_sandbox:AioSandboxProvider",
            allow_host_bash=False,
            network=SimpleNamespace(mode=mode, approval="deny", allow_domains=[]),
        )
    )


def _model(**attrs: Any) -> Any:
    model = SimpleNamespace()
    model.__dict__.update(attrs)
    return model


# --------------------------------------------------------------------------
# composition — the same steps production runs, nothing re-implemented
# --------------------------------------------------------------------------


async def _install_service(config: dict[str, Any], session_factory: Any, wire: _KernelWire) -> tuple[Any, list[Any]]:
    """Run the real ``install()`` + ``start()`` and return (service, contributors).

    ``service._client`` is then pointed at the wire-recording transport — the
    ``KernelBudgetClient(lambda: self._client)`` inside the contributed
    middleware resolves it per call, exactly like a live deployment.
    """
    captured: dict[str, list[Any]] = {"services": [], "contributors": [], "routers": []}
    registry = SimpleNamespace(
        service=captured["services"].append,
        middlewares=captured["contributors"].append,
        routers=captured["routers"].extend,
    )
    install(registry, config)
    service = captured["services"][0]
    await service.start(SimpleNamespace(session_factory=session_factory))
    if service.client is not None:
        await service.client.aclose()
    service._client = KernelClient(service._resolved, transport=httpx.MockTransport(wire))
    return service, captured["contributors"]


def _model_physical_chain(contributors: list[Any]) -> list[Any]:
    """Collect MODEL_PHYSICAL middlewares in install() registration order.

    All EWCP placements are ``order=0`` at the same anchor, so the harness's
    ``(order, registration_index)`` sort leaves them in contribution order and
    the first-registered lands outermost — identical to ``inject_middlewares``.
    """
    chain: list[Any] = []
    for contributor in contributors:
        for placement in contributor.contribute_middlewares(None, SimpleNamespace()):
            if placement.placement is Placement.MODEL_PHYSICAL:
                chain.append(placement.middleware)
    return chain


def _compose(chain: list[Any], terminal: Any) -> Any:
    """Outer-first wrap, as ``inject_middlewares`` would emit it."""
    handler = terminal
    for mw in reversed(chain):
        inner = handler

        async def handler(request: Any, mw: Any = mw, inner: Any = inner) -> Any:
            return await mw.awrap_model_call(request, inner)

    return handler


async def _agent_start(chain: list[Any], runtime: Any) -> None:
    """The before_agent pass the agent runtime performs before any model call.

    ``AgentMiddleware.abefore_agent`` is a no-op on middleware that doesn't
    implement it, so calling it unconditionally mirrors production dispatch.
    """
    for mw in chain:
        await mw.abefore_agent({}, runtime)


async def _bind_governed(store: Any, *, thread_id: str = "t-gov", run_id: str = "r-1", workrun_id: str = "wr-real") -> Any:
    """Insert the ExecutionRunMap row the governed launch path writes."""
    return await store.insert(
        ExecutionRunRecord.new(
            thread_id=thread_id,
            run_id=run_id,
            workrun_id=workrun_id,
            task_mode="governed",
            status="running",
            intent="governed intent",
            idempotency_key=f"idem-{thread_id}-{run_id}-{workrun_id}",
            created_by="u1",
        )
    )


@pytest_asyncio.fixture
async def session_factory() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# body.config['context'] bytes a hostile client can send — verbatim forge set.
_FORGED_CALLER_CONTEXT = {
    "ewcp_tenant_id": "t-victim",
    "ewcp_egress_mode": "approved_cloud",
    "kernel": {"workrun_id": "wr-forged"},
    "thread_id": "t-forged",
    "run_id": "r-forged",
    "user_id": "u1",
}

_TENANTED_CONFIG = {
    "kernel_url": "http://kernel.test",
    "kernel_api_key": "test-key",
    "tenant_id": "t-server",
    "invoke": {"tenant_id": "t-invoke"},
    "budget": {"cap_usd": "5.00"},
    "egress": {"tenant_classes": {"t-server": "non_sensitive"}},
}


class TestForgedTenantCannotOverrideBudgetAttribution:
    @pytest.mark.asyncio
    async def test_forged_context_never_reaches_the_admission_wire(self, session_factory: Any) -> None:
        """Governed run + authenticated tenant: forged context keys in the run
        body cannot move budget attribution or survive into the live context."""
        wire = _KernelWire()
        service, contributors = await _install_service(_TENANTED_CONFIG, session_factory, wire)
        await _bind_governed(service.store)

        chain = _model_physical_chain(contributors)
        # The production composition this regression pins: budget admits
        # OUTER of the egress endpoint gate at MODEL_PHYSICAL.
        assert [type(mw) for mw in chain] == [BudgetAdmissionMiddleware, EgressPolicyMiddleware]

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-gov", "r-1", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        # Forge reaches the live dict verbatim; server-owned ids do not.
        assert runtime.context["ewcp_tenant_id"] == "t-victim"
        assert runtime.context["kernel"] == {"workrun_id": "wr-forged"}
        assert runtime.context["thread_id"] == "t-gov"
        assert runtime.context["run_id"] == "r-1"

        await _agent_start(chain, runtime)

        # The forged keys are stripped/re-stamped on the dict the model path reads.
        assert runtime.context["ewcp_tenant_id"] == "t-server"
        assert runtime.context["kernel"] == {"workrun_id": "wr-real"}
        assert "ewcp_egress_mode" not in runtime.context

        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="https://generativelanguage.googleapis.com/v1beta"),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        result = await _compose(chain, provider)(request)

        assert result is not None
        assert len(provider.calls) == 1, "the composed chain must reach the provider boundary"

        admits = wire.bodies("/budget/admissions")
        assert len(admits) == 1
        admit = admits[0]
        # Authenticated attribution wins — never the client-supplied tenant.
        assert admit["tenant_id"] == "t-server"
        # Governed identity comes from the ExecutionRunMap binding, never the forge.
        assert admit["execution_run_id"] == "wr-real"
        assert "t-victim" not in json.dumps(admit)

        # The provider boundary saw the cleaned live dict — no forged keys.
        seen = provider.contexts[0]
        assert seen["ewcp_tenant_id"] == "t-server"
        assert seen["kernel"] == {"workrun_id": "wr-real"}
        assert "ewcp_egress_mode" not in seen

        # Settle closed the same admission — the ledger stayed coherent.
        assert wire.bodies("/settle"), "admitted call must settle"

    @pytest.mark.asyncio
    async def test_forge_on_general_run_falls_back_to_invoke_tenant(self, session_factory: Any) -> None:
        """No governed binding + ``invoke.tenant_id`` fallback: the forged
        tenant and forged workrun both die; attribution lands on the run id."""
        config = {
            "kernel_url": "http://kernel.test",
            "invoke": {"tenant_id": "t-invoke"},
            "budget": {"cap_usd": "5.00"},
        }
        wire = _KernelWire()
        service, contributors = await _install_service(config, session_factory, wire)
        chain = _model_physical_chain(contributors)

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-plain", "r-x", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        await _agent_start(chain, runtime)

        # Nothing re-stamps a tenant here — the invoke fallback is stamped,
        # the forged kernel dies with no binding to restore.
        assert runtime.context["ewcp_tenant_id"] == "t-invoke"
        assert "kernel" not in runtime.context

        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="http://127.0.0.1:8000"),  # sensitive tenant → local_only endpoint only
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        result = await _compose(chain, provider)(request)

        assert result is not None
        assert len(provider.calls) == 1

        admits = wire.bodies("/budget/admissions")
        assert len(admits) == 1
        admit = admits[0]
        assert admit["tenant_id"] == "t-invoke"
        # General lane: attributed to the server-owned run id, never wr-forged.
        assert admit["execution_run_id"] == "r-x"
        assert "wr-forged" not in json.dumps(admit)

    @pytest.mark.asyncio
    async def test_without_agent_start_stamp_the_forge_still_never_reaches_the_wire(self, session_factory: Any) -> None:
        """SECURITY REGRESSION — the admit must not depend on stamp ordering.

        ``BudgetAdmissionMiddleware`` sits OUTER of the egress gate, so a
        model call that ever bypasses ``abefore_agent`` would reach the wrap
        chain unstamped. Post-#63 the admit resolves tenant/workrun identity
        from server-owned sources (``tenant_id_getter`` / ``ExecutionRunMap``)
        and never reads client-carried context keys, so even an unstamped
        call can only admit under server-resolved identity — forged values
        are dead input. If this ever breaks, the ordering exposure from the
        original characterization finding is back: revisit the invariant,
        don't just update the expectations.
        """
        wire = _KernelWire()
        service, contributors = await _install_service(_TENANTED_CONFIG, session_factory, wire)
        await _bind_governed(service.store)
        chain = _model_physical_chain(contributors)

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-gov", "r-1", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        # No before_agent pass — a call reaching the wrap chain unstamped.
        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="https://generativelanguage.googleapis.com/v1beta"),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        await _compose(chain, provider)(request)

        admits = wire.bodies("/budget/admissions")
        assert len(admits) == 1
        admit = admits[0]
        # Server-resolved identity survives the missing stamp.
        assert admit["tenant_id"] == "t-server"
        assert admit["execution_run_id"] == "wr-real"
        # No forged value reaches the kernel ledger.
        body = json.dumps(admit)
        assert "t-victim" not in body
        assert "wr-forged" not in body
