"""recovery.py — foreground-only ExecutionRun recovery (A3 Task 3).

Contracts pinned (see docs/vnext/A3_DURABILITY.md):

  - FOREGROUND ONLY: every op goes through the request-fresh bound
    `AgentRuns` handle (`resolve_agent_runs(request)` -> `for_plugin`);
    between requests the extension holds no credential. A revoked or
    demoted session denies (AgentRunError 403 -> RecoveryDenied) BEFORE
    any mutating resume call is issued.
  - Restart recovery: rows persisted in the shared ExecutionRunMap
    reconcile against live thread truth on the next authenticated
    request — running/success/interrupt land on the right projection;
    admission-in-flight rows (run_id NULL) stay LAUNCHING and keep
    ownership of the pending launch.
  - `get_state` is mutable thread state, not a run result: it is only
    consulted to flag pending input on an ENDED run. Observers rejoin
    live event history through the SSE join route (`run_join_url`),
    never through a state snapshot.
  - dup-start deny: an open run on the target thread, or an
    idempotency key already spent on a different intent, raises
    OpenRunConflict; same key + same intent is a replay (allowed).
  - interrupt->resume: resume is gated on real pending state (ended
    run + pending thread state); it rebinds run_id when upstream
    admits a new run on the same thread.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from deerflow_extension_api.agent_runs import AgentRunError
from deerflow_extension_api.auth import EXTENSION_PRINCIPAL_RESOLVER_KEY, ExtensionPrincipal
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ewcp_core.execution_run_store import ExecutionRunRecord, ExecutionRunStore
from ewcp_core.plugin import EwcpCoreService, build_router
from ewcp_core.recovery import (
    OPEN_RUN_STATUSES,
    ExecutionRunStatus,
    OpenRunConflict,
    RecoveryDenied,
    ResumeNotPending,
    RunNotOwned,
    RunRecovery,
)
from ewcp_core.run_launcher import EWCP_RUNS_NAMESPACE, run_join_url

# ---------------------------------------------------------------------------
# Fakes — mirror deerflow_extension_api.agent_runs.AgentRuns surface.
# ---------------------------------------------------------------------------


class FakeAgentRun:
    def __init__(self, thread_id: str, run_id: str, status: str = "running") -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.status = status
        self.assistant_id = "lead_agent"
        self.stop_reason = None


class FakeAgentRuns:
    """Bound-handle stand-in with per-run truth + injectable failures."""

    def __init__(self) -> None:
        self.namespaces: list[str] = []
        self.run_status: dict[str, str] = {}  # run_id -> upstream status
        self.thread_state: dict[str, dict] = {}  # thread_id -> get_state result
        self.resumed: list[dict] = []
        self.got: list[tuple[str, str]] = []
        self.errors: dict[str, Exception] = {}  # method name -> exception to raise
        self._resume_n = 0

    def for_plugin(self, namespace: str) -> FakeAgentRuns:
        self.namespaces.append(namespace)
        return self

    def _maybe_raise(self, method: str) -> None:
        exc = self.errors.get(method)
        if exc is not None:
            raise exc

    async def create_thread(self, *, assistant_id="lead_agent", thread_id=None, metadata=None) -> str:
        return thread_id or "thread-new"

    async def start(self, *, thread_id, input=None, context=None, idempotency_key=None) -> FakeAgentRun:
        raise AssertionError("recovery tests never start runs")

    async def get(self, *, thread_id, run_id) -> FakeAgentRun:
        self._maybe_raise("get")
        self.got.append((thread_id, run_id))
        return FakeAgentRun(thread_id, run_id, status=self.run_status.get(run_id, "running"))

    async def get_state(self, *, thread_id) -> dict:
        self._maybe_raise("get_state")
        return self.thread_state.get(thread_id, {})

    async def resume(self, *, thread_id, resume, idempotency_key=None) -> FakeAgentRun:
        self._maybe_raise("resume")
        self._resume_n += 1
        self.resumed.append(
            {
                "thread_id": thread_id,
                "resume": resume,
                "idempotency_key": idempotency_key,
            }
        )
        # Upstream admits a NEW run on the same thread on resume.
        return FakeAgentRun(thread_id, f"run-resumed-{self._resume_n}", status="running")

    async def cancel(self, *, thread_id, run_id) -> None:
        raise AssertionError("recovery tests never cancel runs")

    async def wait(self, *, thread_id, run_id, timeout=60) -> FakeAgentRun:
        raise AssertionError("recovery tests never wait on runs")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store():
    engine = create_async_engine("sqlite+aiosqlite://")
    sf = async_sessionmaker(engine, expire_on_commit=False)
    s = ExecutionRunStore(sf)
    await s.ensure_schema()
    yield s
    await engine.dispose()


@pytest.fixture
def recovery(store) -> RunRecovery:
    return RunRecovery(store)


async def _seed(store, **overrides) -> ExecutionRunRecord:
    params: dict[str, Any] = {
        "thread_id": "thread-1",
        "task_mode": "general",
        "status": ExecutionRunStatus.RUNNING,
        "idempotency_key": "k-1",
        "created_by": "u-1",
        "intent": "đối soát",
    }
    params.update(overrides)
    record = ExecutionRunRecord.new(**params)
    return await store.insert(record)


# ---------------------------------------------------------------------------
# Scenario 1 — disconnect -> rejoin SSE (event history, not a snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejoin_returns_sse_join_url_for_open_run(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-open", run_id="r-9")
    runs = FakeAgentRuns()  # fresh handle = new authenticated request

    report = await recovery.recover(agent_runs=runs, created_by="u-1")

    assert runs.namespaces == [EWCP_RUNS_NAMESPACE]
    assert len(report.runs) == 1
    item = report.runs[0]
    assert item.observation_url == "/api/threads/t-open/runs/r-9/join"
    assert item.observation_url == run_join_url(rec.thread_id, rec.run_id)
    assert item.run_status == "running"
    assert item.changed is False
    assert item.record.status == ExecutionRunStatus.RUNNING


# ---------------------------------------------------------------------------
# Scenario 2 — restart -> foreground recovery reconciles the durable map
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_reconcile_projects_thread_truth(store) -> None:
    # Rows persisted BEFORE the "restart" (a fresh RunRecovery + fresh bound
    # handle act on the same shared store afterwards).
    finished = await _seed(store, thread_id="t-done", run_id="r-done", status=ExecutionRunStatus.RUNNING)
    pending = await _seed(store, thread_id="t-pend", run_id="r-pend", status=ExecutionRunStatus.RUNNING, idempotency_key="k-2")
    live = await _seed(store, thread_id="t-live", run_id="r-live", status=ExecutionRunStatus.RUNNING, idempotency_key="k-3")
    crashed_launch = await _seed(store, thread_id="t-crash", run_id=None, status=ExecutionRunStatus.LAUNCHING, idempotency_key="k-4")
    dead = await _seed(store, thread_id="t-dead", run_id="r-dead", status=ExecutionRunStatus.FAILED, idempotency_key="k-5")

    runs = FakeAgentRuns()
    runs.run_status.update({"r-done": "success", "r-pend": "success", "r-live": "running"})
    runs.thread_state["t-pend"] = {"values": {}, "interrupts": [{"id": "i-1"}]}

    recovery = RunRecovery(store)  # post-restart instance
    report = await recovery.recover(agent_runs=runs, created_by="u-1")
    by_id = {r.record.execution_run_id: r for r in report.runs}

    assert by_id[finished.execution_run_id].record.status == ExecutionRunStatus.COMPLETED
    assert by_id[finished.execution_run_id].changed is True

    # pending interrupt != complete: ended run + pending thread state
    assert by_id[pending.execution_run_id].record.status == ExecutionRunStatus.PENDING_INTERRUPT
    assert by_id[pending.execution_run_id].pending_interrupt is True

    assert by_id[live.execution_run_id].record.status == ExecutionRunStatus.RUNNING
    assert by_id[live.execution_run_id].changed is False

    # Admission never acknowledged: the row keeps ownership, nothing polled.
    assert by_id[crashed_launch.execution_run_id].record.status == ExecutionRunStatus.LAUNCHING
    assert by_id[crashed_launch.execution_run_id].run_status is None
    assert by_id[crashed_launch.execution_run_id].observation_url is None

    # Dead-final rows are not re-polled.
    assert by_id[dead.execution_run_id].record.status == ExecutionRunStatus.FAILED
    assert ("t-dead", "r-dead") not in runs.got

    # Persisted truth, not just the return value.
    assert (await store.get(finished.execution_run_id)).status == ExecutionRunStatus.COMPLETED
    assert (await store.get(pending.execution_run_id)).status == ExecutionRunStatus.PENDING_INTERRUPT


@pytest.mark.asyncio
async def test_thread_gone_marks_run_failed(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-gone", run_id="r-gone")
    runs = FakeAgentRuns()
    runs.errors["get"] = AgentRunError(404, "Thread not found")

    report = await recovery.recover(agent_runs=runs, created_by="u-1")

    item = report.runs[0]
    assert item.record.status == ExecutionRunStatus.FAILED
    assert item.changed is True
    assert (await store.get(rec.execution_run_id)).status == ExecutionRunStatus.FAILED


# ---------------------------------------------------------------------------
# Scenario 3 — dup-start deny (idempotency key + open-run check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dup_start_denied_when_thread_has_open_run(recovery, store) -> None:
    open_row = await _seed(store, thread_id="t-busy", run_id="r-1", status=ExecutionRunStatus.RUNNING)

    with pytest.raises(OpenRunConflict) as exc_info:
        await recovery.assert_can_start(created_by="u-1", thread_id="t-busy")
    assert exc_info.value.record.execution_run_id == open_row.execution_run_id


@pytest.mark.asyncio
async def test_dup_start_denied_for_pending_interrupt(recovery, store) -> None:
    await _seed(store, thread_id="t-pi", run_id="r-1", status=ExecutionRunStatus.PENDING_INTERRUPT)
    assert ExecutionRunStatus.PENDING_INTERRUPT in OPEN_RUN_STATUSES
    with pytest.raises(OpenRunConflict):
        await recovery.assert_can_start(created_by="u-1", thread_id="t-pi")


@pytest.mark.asyncio
async def test_dup_start_denied_when_key_spent_on_different_intent(recovery, store) -> None:
    await _seed(store, idempotency_key="k-spent", intent="đối soát", status=ExecutionRunStatus.COMPLETED)

    with pytest.raises(OpenRunConflict):
        await recovery.assert_can_start(created_by="u-1", idempotency_key="k-spent", intent="khác")


@pytest.mark.asyncio
async def test_dup_start_allows_replay_and_closed_thread(recovery, store) -> None:
    await _seed(store, thread_id="t-closed", run_id="r-x", status=ExecutionRunStatus.COMPLETED, idempotency_key="k-done", intent="đối soát")

    # same key + same intent -> replay path (launcher converges), allowed
    await recovery.assert_can_start(created_by="u-1", idempotency_key="k-done", intent="đối soát")
    # thread has no OPEN row -> allowed
    await recovery.assert_can_start(created_by="u-1", thread_id="t-closed")
    # nothing requested -> allowed
    await recovery.assert_can_start(created_by="u-1")


# ---------------------------------------------------------------------------
# Scenario 4 — revoked-auth deny (before any mutation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_auth_denies_recover_without_mutation(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-r", run_id="r-1")
    runs = FakeAgentRuns()
    runs.errors["get"] = AgentRunError(403, "Agent run authorization revoked")

    with pytest.raises(RecoveryDenied):
        await recovery.recover(agent_runs=runs, created_by="u-1")

    assert (await store.get(rec.execution_run_id)).status == ExecutionRunStatus.RUNNING


@pytest.mark.asyncio
async def test_revoked_auth_denies_resume_before_mutating(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-r2", run_id="r-2", status=ExecutionRunStatus.PENDING_INTERRUPT)
    runs = FakeAgentRuns()
    # A revoked session fails EVERY bound-handle op (the host re-auths per
    # op), so the first pre-resume read already denies — before resume().
    runs.errors["get"] = AgentRunError(403, "Agent run authorization revoked")
    runs.errors["get_state"] = AgentRunError(403, "Agent run authorization revoked")
    runs.errors["resume"] = AgentRunError(403, "Agent run authorization revoked")

    with pytest.raises(RecoveryDenied):
        await recovery.resume(
            agent_runs=runs,
            created_by="u-1",
            execution_run_id=rec.execution_run_id,
            resume={"answer": "approve"},
        )

    assert runs.resumed == []  # the mutating call was never issued
    assert (await store.get(rec.execution_run_id)).status == ExecutionRunStatus.PENDING_INTERRUPT


# ---------------------------------------------------------------------------
# Scenario 5 — interrupt -> resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_rebinds_run_and_marks_running(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-int", run_id="r-old", status=ExecutionRunStatus.PENDING_INTERRUPT)
    runs = FakeAgentRuns()
    runs.run_status["r-old"] = "interrupted"
    runs.thread_state["t-int"] = {"interrupts": [{"id": "i-1", "value": "approve?"}]}

    out = await recovery.resume(
        agent_runs=runs,
        created_by="u-1",
        execution_run_id=rec.execution_run_id,
        resume={"answer": "approve"},
        idempotency_key="resume-1",
    )

    assert runs.resumed == [{"thread_id": "t-int", "resume": {"answer": "approve"}, "idempotency_key": "resume-1"}]
    assert out.run_id == "run-resumed-1"  # rebound to the new upstream run
    assert out.status == ExecutionRunStatus.RUNNING
    persisted = await store.get(rec.execution_run_id)
    assert persisted.run_id == "run-resumed-1"
    assert persisted.status == ExecutionRunStatus.RUNNING


@pytest.mark.asyncio
async def test_resume_denied_when_not_pending_repairs_map(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-fin", run_id="r-fin", status=ExecutionRunStatus.PENDING_INTERRUPT)
    runs = FakeAgentRuns()
    runs.run_status["r-fin"] = "success"
    runs.thread_state["t-fin"] = {"values": {}, "next": []}  # nothing pending

    with pytest.raises(ResumeNotPending):
        await recovery.resume(
            agent_runs=runs,
            created_by="u-1",
            execution_run_id=rec.execution_run_id,
            resume={"answer": "approve"},
        )

    assert runs.resumed == []
    # Stale map row repaired to the real terminal state on the way out.
    assert (await store.get(rec.execution_run_id)).status == ExecutionRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_denied_while_run_in_flight(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-fly", run_id="r-fly", status=ExecutionRunStatus.RUNNING)
    runs = FakeAgentRuns()
    runs.run_status["r-fly"] = "running"
    # A live run always shows `next` nodes — that is in-flight, not pending.
    runs.thread_state["t-fly"] = {"next": ["tools"]}

    with pytest.raises(ResumeNotPending):
        await recovery.resume(
            agent_runs=runs,
            created_by="u-1",
            execution_run_id=rec.execution_run_id,
            resume={"answer": "approve"},
        )
    assert runs.resumed == []


@pytest.mark.asyncio
async def test_resume_denied_for_unadmitted_run(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-l", run_id=None, status=ExecutionRunStatus.LAUNCHING)
    runs = FakeAgentRuns()

    with pytest.raises(ResumeNotPending):
        await recovery.resume(
            agent_runs=runs,
            created_by="u-1",
            execution_run_id=rec.execution_run_id,
            resume={"answer": "approve"},
        )
    assert runs.resumed == []


@pytest.mark.asyncio
async def test_resume_rejects_other_owners_record(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-own", run_id="r-1", status=ExecutionRunStatus.PENDING_INTERRUPT, created_by="u-2")

    with pytest.raises(RunNotOwned):
        await recovery.resume(
            agent_runs=FakeAgentRuns(),
            created_by="u-1",
            execution_run_id=rec.execution_run_id,
            resume={"answer": "approve"},
        )


@pytest.mark.asyncio
async def test_recover_only_lists_callers_rows(recovery, store) -> None:
    await _seed(store, thread_id="t-mine", run_id="r-1", created_by="u-1")
    await _seed(store, thread_id="t-other", run_id="r-2", created_by="u-2", idempotency_key="k-2")

    report = await recovery.recover(agent_runs=FakeAgentRuns(), created_by="u-1")

    assert [r.record.thread_id for r in report.runs] == ["t-mine"]


# ---------------------------------------------------------------------------
# Foreground entry points — contributed routes resolve request-fresh handles
# ---------------------------------------------------------------------------


async def _started_service(store) -> EwcpCoreService:
    service = EwcpCoreService(config={})
    engine_sf = store._sf  # same shared session_factory shape as deps
    await service.start(SimpleNamespace(session_factory=engine_sf))
    return service


def _route_app(service: EwcpCoreService, *, principal: ExtensionPrincipal | None, runs: FakeAgentRuns | None) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(service))
    if principal is not None:
        setattr(app.state, EXTENSION_PRINCIPAL_RESOLVER_KEY, lambda request: principal)
    if runs is not None:
        from deerflow_extension_api.agent_runs import AGENT_RUNS_RESOLVER_KEY

        setattr(app.state, AGENT_RUNS_RESOLVER_KEY, lambda request: runs)

        # F1 seam: resume admits through HttpRunStarter -> POST
        # /api/threads/{id}/runs on the request's own origin — the fake
        # upstream delegates to FakeAgentRuns.resume so route tests keep
        # asserting against the same recorded mutations.
        @app.post("/api/threads/{thread_id}/runs")
        async def _upstream_run_create(thread_id: str, request: Request):
            body = await request.json()
            command = body.get("command") or {}
            run = await runs.resume(
                thread_id=thread_id,
                resume=command.get("resume"),
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
            return {"run_id": run.run_id, "thread_id": thread_id, "assistant_id": None, "status": run.status}

    # The route builds a real httpx client on request.base_url — swap it
    # for an in-process ASGI transport so the TestClient serves its own
    # upstream hop (production: nginx proxies /api/* same-origin).
    import ewcp_core.plugin as plugin_module

    def _asgi_client(request: Request) -> httpx.AsyncClient:
        headers = {name: request.headers[name] for name in ("cookie", "authorization", "x-csrf-token") if name in request.headers}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
            timeout=60.0,
        )

    plugin_module.request_scoped_client = _asgi_client
    return app


@pytest.mark.asyncio
async def test_runs_route_recovers_and_lists(store) -> None:
    service = await _started_service(store)
    try:
        await _seed(store, thread_id="t-http", run_id="r-http", status=ExecutionRunStatus.RUNNING)
        runs = FakeAgentRuns()
        app = _route_app(service, principal=ExtensionPrincipal(user_id="u-1"), runs=runs)
        client = TestClient(app)

        resp = client.get("/api/ewcp/runs")

        assert resp.status_code == 200
        body = resp.json()
        assert body["owner"] == "u-1"
        assert len(body["runs"]) == 1
        row = body["runs"][0]
        assert row["thread_id"] == "t-http"
        assert row["run_id"] == "r-http"
        assert row["run_status"] == "running"
        assert row["observation_url"] == "/api/threads/t-http/runs/r-http/join"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_runs_route_401_without_principal(store) -> None:
    service = await _started_service(store)
    try:
        app = _route_app(service, principal=None, runs=FakeAgentRuns())
        resp = TestClient(app).get("/api/ewcp/runs")
        assert resp.status_code == 401
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_runs_route_503_when_handle_unavailable(store) -> None:
    service = await _started_service(store)
    try:
        app = _route_app(service, principal=ExtensionPrincipal(user_id="u-1"), runs=None)
        resp = TestClient(app).get("/api/ewcp/runs")
        assert resp.status_code == 503
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_resume_route_404_for_other_owner(store) -> None:
    service = await _started_service(store)
    try:
        rec = await _seed(store, thread_id="t-x", run_id="r-x", status=ExecutionRunStatus.PENDING_INTERRUPT, created_by="u-2")
        app = _route_app(service, principal=ExtensionPrincipal(user_id="u-1"), runs=FakeAgentRuns())
        resp = TestClient(app).post(f"/api/ewcp/runs/{rec.execution_run_id}/resume", json={"resume": {"answer": "approve"}})
        assert resp.status_code == 404
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_resume_route_409_when_not_pending(store) -> None:
    service = await _started_service(store)
    try:
        rec = await _seed(store, thread_id="t-y", run_id="r-y", status=ExecutionRunStatus.PENDING_INTERRUPT)
        runs = FakeAgentRuns()
        runs.run_status["r-y"] = "success"
        app = _route_app(service, principal=ExtensionPrincipal(user_id="u-1"), runs=runs)
        resp = TestClient(app).post(f"/api/ewcp/runs/{rec.execution_run_id}/resume", json={"resume": {"answer": "approve"}})
        assert resp.status_code == 409
        assert runs.resumed == []
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_resume_route_resumes_pending_run(store) -> None:
    service = await _started_service(store)
    try:
        rec = await _seed(store, thread_id="t-z", run_id="r-z", status=ExecutionRunStatus.PENDING_INTERRUPT)
        runs = FakeAgentRuns()
        runs.run_status["r-z"] = "interrupted"
        runs.thread_state["t-z"] = {"interrupts": [{"id": "i-1"}]}
        app = _route_app(service, principal=ExtensionPrincipal(user_id="u-1"), runs=runs)

        resp = TestClient(app).post(
            f"/api/ewcp/runs/{rec.execution_run_id}/resume",
            json={"resume": {"answer": "approve"}, "idempotency_key": "rs-1"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == "run-resumed-1"
        assert body["status"] == "running"
        assert runs.resumed[0]["idempotency_key"] == "rs-1"
    finally:
        await service.stop()


# ---------------------------------------------------------------------------
# F1 — resume carries the same recursion budget (GP01_EVAL F1; upstream
# resume admits a NEW run on the same thread, so a resume without config
# would fall back to the server default 100 and die on long tool loops).
# ---------------------------------------------------------------------------


class FakeRunStarter:
    """RunStarter stand-in for the resume path."""

    def __init__(self) -> None:
        self.resumed: list[dict] = []

    async def resume(self, *, thread_id, resume, config=None, idempotency_key=None) -> FakeAgentRun:
        self.resumed.append(
            {
                "thread_id": thread_id,
                "resume": resume,
                "config": config,
                "idempotency_key": idempotency_key,
            }
        )
        return FakeAgentRun(thread_id, "run-resumed-http", status="running")


@pytest.mark.asyncio
async def test_resume_via_starter_forwards_recursion_limit(recovery, store) -> None:
    rec = await _seed(store, thread_id="t-int", run_id="r-old", status=ExecutionRunStatus.PENDING_INTERRUPT)
    runs = FakeAgentRuns()
    runs.run_status["r-old"] = "interrupted"
    runs.thread_state["t-int"] = {"interrupts": [{"id": "i-1", "value": "approve?"}]}
    starter = FakeRunStarter()

    out = await recovery.resume(
        agent_runs=runs,
        created_by="u-1",
        execution_run_id=rec.execution_run_id,
        resume={"answer": "approve"},
        idempotency_key="resume-1",
        starter=starter,
    )

    assert starter.resumed == [
        {
            "thread_id": "t-int",
            "resume": {"answer": "approve"},
            "config": {"recursion_limit": 1000},
            "idempotency_key": "resume-1",
        }
    ]
    assert runs.resumed == []  # bound contract resume not used when a starter is bound
    assert out.run_id == "run-resumed-http"
    assert out.status == ExecutionRunStatus.RUNNING
