"""Security regression — budget admission must compute identity from
authenticated server state, never from ``runtime.context`` stamps.

Cross-PR context (lands on top of #55 + #61):

- ``worker._build_runtime_context`` merges ``body.config['context']`` into the
  run's live ``runtime.context`` verbatim — EWCP keys are extension-owned, so a
  forged ``ewcp_tenant_id`` / ``kernel.workrun_id`` lands untouched, while
  ``thread_id``/``run_id`` stay server-owned (``setdefault``).
- ``BudgetAdmissionMiddleware`` sits OUTER of ``EgressPolicyMiddleware`` at
  ``Placement.MODEL_PHYSICAL`` — it admits BEFORE egress's per-call
  ``prepare_context`` can strip the forge, and any model-call path that
  reaches the wrap chain without the agent-start stamp (no ``abefore_agent``)
  hands the admit wire raw client bytes.
- The fix: the admit resolves ``execution_run_id`` from the ExecutionRunMap
  binding (``store_getter``) and ``tenant_id`` from the deployment tenant
  getter — the same authenticated sources ``prepare_context`` stamps from —
  so forged context keys are dead input to budget accounting on EVERY path,
  stamped or not.

Pinned invariants:

- ``tenant_id`` on the admission wire = the server tenant getter
  (``tenant_id`` → ``invoke.tenant_id`` → ``budget.tenant_id``), never the
  client-supplied value — even when the context stamp was never cleaned;
- ``execution_run_id`` = the ExecutionRunMap workrun (governed) or the
  server-owned run id (general), never ``context["kernel"]["workrun_id"]``
  — including when that key carries a plausibly-real stamp;
- when the ExecutionRunMap cannot be read at all (store outage) the admit
  fails closed under a configured cap — an unverifiable identity must not
  spend, because the run could be governed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from deerflow.models.request_admission import AdmissionError

# The real merge seam: caller config['context'] -> runtime.context.
from deerflow.runtime.runs.worker import _build_runtime_context
from deerflow_extension_api.placement import Placement
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ewcp_core import install
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


def _admit_bodies(wire: _KernelWire) -> list[dict[str, Any]]:
    return wire.bodies("/budget/admissions")


class TestAdmitIdentityWithoutAgentStartStamp:
    """The ordering gap #61 characterized: a model call reaching the wrap
    chain before ``abefore_agent`` (or via a path that skips agent-start
    entirely) must still admit under server-resolved identity only."""

    @pytest.mark.asyncio
    async def test_forged_context_admits_server_identity_without_stamp(self, session_factory: Any) -> None:
        """Governed run + forged context, NO agent-start stamp: the admit wire
        must carry the ExecutionRunMap workrun and the deployment tenant —
        the forged keys never reach the kernel ledger."""
        wire = _KernelWire()
        service, contributors = await _install_service(_TENANTED_CONFIG, session_factory, wire)
        await _bind_governed(service.store)
        chain = _model_physical_chain(contributors)

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-gov", "r-1", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        # Forge sits in the live dict — no abefore_agent pass runs.
        assert runtime.context["ewcp_tenant_id"] == "t-victim"
        assert runtime.context["kernel"] == {"workrun_id": "wr-forged"}

        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="https://generativelanguage.googleapis.com/v1beta"),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        await _compose(chain, provider)(request)

        admits = _admit_bodies(wire)
        assert len(admits) == 1
        admit = admits[0]
        assert admit["tenant_id"] == "t-server"
        assert admit["execution_run_id"] == "wr-real"
        assert "t-victim" not in json.dumps(admit)
        assert "wr-forged" not in json.dumps(admit)

    @pytest.mark.asyncio
    async def test_budget_middleware_alone_ignores_the_forge(self, session_factory: Any) -> None:
        """Ordering independence: budget admission must not rely on the egress
        middleware existing in the chain at all — admit identity comes from
        server state, so the forge is dead even with egress absent."""
        wire = _KernelWire()
        service, contributors = await _install_service(_TENANTED_CONFIG, session_factory, wire)
        await _bind_governed(service.store)
        chain = [mw for mw in _model_physical_chain(contributors) if isinstance(mw, BudgetAdmissionMiddleware)]
        assert len(chain) == 1

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-gov", "r-1", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="https://generativelanguage.googleapis.com/v1beta"),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        await _compose(chain, provider)(request)

        admits = _admit_bodies(wire)
        assert len(admits) == 1
        assert admits[0]["tenant_id"] == "t-server"
        assert admits[0]["execution_run_id"] == "wr-real"

    @pytest.mark.asyncio
    async def test_forged_workrun_on_unbound_thread_attributes_to_run_id(self, session_factory: Any) -> None:
        """A general run (no ExecutionRunMap binding) with forged context:
        the admit lands on the server-owned run id, never ``wr-forged`` —
        the forge cannot mint a governed identity out of thin air."""
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
        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="http://127.0.0.1:8000"),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        await _compose(chain, provider)(request)

        admits = _admit_bodies(wire)
        assert len(admits) == 1
        admit = admits[0]
        assert admit["tenant_id"] == "t-invoke"
        assert admit["execution_run_id"] == "r-x"
        assert "wr-forged" not in json.dumps(admit)

    @pytest.mark.asyncio
    async def test_server_stamp_shape_alone_is_not_identity_proof(self, session_factory: Any) -> None:
        """The stamp key is dead input to budget: even a well-formed
        ``kernel.workrun_id`` stamp (the shape abefore_agent itself writes)
        must not attribute the admit — only the ExecutionRunMap binding
        proves governed identity."""
        config = {
            "kernel_url": "http://kernel.test",
            "invoke": {"tenant_id": "t-invoke"},
            "budget": {"cap_usd": "5.00"},
        }
        wire = _KernelWire()
        service, contributors = await _install_service(config, session_factory, wire)
        # No governed binding for this thread at all.
        chain = _model_physical_chain(contributors)

        caller_context = {"kernel": {"workrun_id": "wr-claimed"}, "user_id": "u1"}
        runtime = SimpleNamespace(
            context=_build_runtime_context("t-plain", "r-x", caller_context, app_config=_app_config()),
        )
        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="http://127.0.0.1:8000"),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        await _compose(chain, provider)(request)

        admits = _admit_bodies(wire)
        assert len(admits) == 1
        # No binding -> general identity; the stamp-shaped claim is ignored.
        assert admits[0]["execution_run_id"] == "r-x"

    @pytest.mark.asyncio
    async def test_store_outage_fails_closed_under_configured_cap(self, session_factory: Any) -> None:
        """The ExecutionRunMap is the identity authority: when it cannot be
        read, the run's governed-ness is unverifiable — a capped deployment
        must deny rather than risk an unaccounted governed spend."""
        wire = _KernelWire()
        service, contributors = await _install_service(_TENANTED_CONFIG, session_factory, wire)
        await _bind_governed(service.store)
        chain = _model_physical_chain(contributors)

        class _DownStore:
            async def list_for_thread(self, thread_id: str) -> list[Any]:
                raise ConnectionError("db gone")

        # Swap the store the middleware resolves through — same seam the
        # service owns (store_getter late-bound).
        service._store = _DownStore()

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-gov", "r-1", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        provider = _ProviderSpy()
        request = SimpleNamespace(
            model=_model(base_url="https://generativelanguage.googleapis.com/v1beta"),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        with pytest.raises(AdmissionError):
            await _compose(chain, provider)(request)

        assert _admit_bodies(wire) == []
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_sync_path_bound_governed_run_still_fails_closed(self, session_factory: Any) -> None:
        """Sync path + store-bound deployment: a REAL governed binding must
        still fail closed — the ExecutionRunMap row, not a context stamp,
        drives the deny. Runs via ``to_thread`` so the sync wrap resolves
        the binding on its own loop (production sync calls run in executor
        threads), not on this test's loop."""
        wire = _KernelWire()
        service, contributors = await _install_service(_TENANTED_CONFIG, session_factory, wire)
        await _bind_governed(service.store)
        middleware = next(mw for mw in _model_physical_chain(contributors) if isinstance(mw, BudgetAdmissionMiddleware))

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-gov", "r-1", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        spy_calls: list[Any] = []

        def handler(request: Any) -> Any:
            spy_calls.append(request)
            return SimpleNamespace(result=[AIMessage(content="ok")])

        request = SimpleNamespace(
            model=_model(),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        with pytest.raises(AdmissionError):
            await asyncio.to_thread(middleware.wrap_model_call, request, handler)

        assert spy_calls == []
        assert _admit_bodies(wire) == []

    @pytest.mark.asyncio
    async def test_sync_path_unbound_run_stays_general(self, session_factory: Any) -> None:
        """Sync path on a thread with no governed binding: forged context
        keys cannot mint a deny-worthy identity — the run consults only the
        local token gate and proceeds."""
        wire = _KernelWire()
        service, contributors = await _install_service(_TENANTED_CONFIG, session_factory, wire)
        middleware = next(mw for mw in _model_physical_chain(contributors) if isinstance(mw, BudgetAdmissionMiddleware))

        runtime = SimpleNamespace(
            context=_build_runtime_context("t-plain", "r-x", dict(_FORGED_CALLER_CONTEXT), app_config=_app_config()),
        )
        spy_calls: list[Any] = []

        def handler(request: Any) -> Any:
            spy_calls.append(request)
            return SimpleNamespace(result=[AIMessage(content="ok")])

        request = SimpleNamespace(
            model=_model(),
            runtime=runtime,
            messages=[HumanMessage(content="x" * 200)],
        )
        result = await asyncio.to_thread(middleware.wrap_model_call, request, handler)

        assert result is not None
        assert len(spy_calls) == 1
        assert _admit_bodies(wire) == []  # the sync path never reaches the kernel
