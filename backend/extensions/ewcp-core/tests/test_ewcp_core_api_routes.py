"""Task-6 product-facing routes: owner scoping, kernel proxying,
decision-actor gating, launch via bound AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ewcp_core.execution_run_store import ExecutionRunRecord
from ewcp_core.plugin import build_router
from ewcp_core.run_launcher import LaunchOutcome, TaskMode


@dataclass
class _User:
    id: str = "u-1"


def _record(**over) -> ExecutionRunRecord:
    return ExecutionRunRecord.new(
        thread_id=over.get("thread_id", "t-1"),
        task_mode=over.get("task_mode", TaskMode.GOVERNED.value),
        status=over.get("status", "running"),
        idempotency_key=over.get("key", "k-1"),
        created_by=over.get("created_by", "u-1"),
        intent=over.get("intent", "đối soát"),
        workrun_id=over.get("workrun_id", "wr-1"),
    )


class FakeStore:
    def __init__(self, records: list[ExecutionRunRecord] | None = None) -> None:
        self.records = {r.execution_run_id: r for r in records or []}

    async def get(self, execution_run_id: str):
        return self.records.get(execution_run_id)

    async def list_by_owner(self, created_by: str, *, limit: int = 50):
        return [r for r in self.records.values() if r.created_by == created_by]


class FakeClient:
    """KernelClient shape — records calls, returns canned payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_workrun(self, workrun_id):
        self.calls.append(("get_workrun", workrun_id))
        return {"workrun_id": workrun_id, "status": "awaiting_approval"}

    async def get_manifest(self, workrun_id):
        self.calls.append(("get_manifest", workrun_id))
        return {"manifest_hash": "h-1"}

    async def get_outcome(self, workrun_id):
        return {"outcome_type": "invoice_recon", "result": {"ok": True}}

    async def get_evidence(self, workrun_id):
        return {"workrun_id": workrun_id, "deliverables": []}

    async def download_deliverable(self, workrun_id, deliverable_id):
        import httpx

        return httpx.Response(
            200,
            content=b"deliverable-bytes",
            headers={
                "content-type": "application/json",
                "content-disposition": 'attachment; filename="d.json"',
            },
        )

    async def decide(self, workrun_id, *, answer, decision_id=None, decided_by=None, actor=None):
        self.calls.append(("decide", workrun_id, answer, decision_id, decided_by, actor))
        return {"decision": {"answer": answer, "decided_by": f"{actor}@tenant:t-1"}}

    async def list_outcomes(self):
        return [{"outcome_type": "invoice_recon"}]

    async def verify_manifest(self, manifest_hash):
        return {"manifest": {"manifest_hash": manifest_hash}, "seal_ok": True, "workrun_id": "wr-1"}

    async def verify_evidence(self, *, evidence_json, files):
        self.calls.append(("verify_evidence", evidence_json[0], [f[0] for f in files]))
        return {"verdict": "PASS"}


class FakeLauncher:
    """new_launcher() stub — launch records args, refresh is a no-op."""

    def __init__(self) -> None:
        self.launch_calls: list[dict] = []

    async def launch(self, **kwargs) -> LaunchOutcome:
        self.launch_calls.append(kwargs)
        record = _record(thread_id="t-9", intent=kwargs["intent"], task_mode=kwargs["mode"])
        return LaunchOutcome(record=record, run=None, idempotent_replay=False)


class FakeRecovery:
    """RunRecovery shape — GET /runs reconcile returns the owner's
    stored records as a projection (no live AgentRuns calls)."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def recover(self, *, agent_runs, created_by: str, limit: int = 100):
        from types import SimpleNamespace

        items = [
            SimpleNamespace(
                record=r,
                run_status=None,
                pending_interrupt=False,
                changed=False,
                observation_url=None,
            )
            for r in await self._store.list_by_owner(created_by)
        ]
        return SimpleNamespace(created_by=created_by, runs=items)


class FakeService:
    def __init__(self, *, records=None, client=None, binding=True, launcher=None) -> None:
        self._store = FakeStore(records)
        self.client = client
        self._binding = binding
        self._launcher = launcher
        self._recovery = FakeRecovery(self._store)

    @property
    def store(self):
        return self._store

    @property
    def recovery(self):
        return self._recovery

    @property
    def user_actor_binding(self) -> bool:
        return self._binding

    def new_launcher(self, uploader=None):
        return self._launcher


def _app(service: FakeService, *, user=_User(), agent_runs=None) -> FastAPI:
    app = FastAPI()
    # The full extension router: GET /runs is the recovery/reconcile
    # handler (canonical list), the api_routes surface is included under
    # the same /api/ewcp prefix — mirrors production plugin.py.
    app.include_router(build_router(service))

    @app.middleware("http")
    async def stamp_user(request: Request, call_next):
        if user is not None:
            request.state.user = user
        return await call_next(request)

    if agent_runs is not None:
        from deerflow_extension_api.agent_runs import AGENT_RUNS_RESOLVER_KEY

        setattr(app.state, AGENT_RUNS_RESOLVER_KEY, lambda request: agent_runs)
    if user is not None:
        # T3 recovery routes resolve the caller via the extension
        # principal resolver, not request.state.user.
        from deerflow_extension_api.auth import (
            EXTENSION_PRINCIPAL_RESOLVER_KEY,
            ExtensionPrincipal,
        )

        principal = ExtensionPrincipal(user_id=user.id)
        setattr(app.state, EXTENSION_PRINCIPAL_RESOLVER_KEY, lambda request: principal)
    return app


@pytest.fixture
def service() -> FakeService:
    return FakeService(records=[_record()], client=FakeClient())


def test_identity_reports_actor_and_binding(service):
    c = TestClient(_app(service))
    assert c.get("/api/ewcp/identity").json() == {
        "user_id": "u-1",
        "actor": "user:u-1",
        "user_actor_binding": True,
    }


def test_routes_require_user():
    c = TestClient(_app(FakeService(client=FakeClient()), user=None))
    assert c.get("/api/ewcp/runs").status_code == 401
    assert c.get("/api/ewcp/identity").status_code == 401


def test_list_runs_owner_scoped(service):
    service._store.records["other"] = _record(created_by="u-2")
    c = TestClient(_app(service, agent_runs=object()))
    runs = c.get("/api/ewcp/runs").json()["runs"]
    assert {r["created_by"] for r in runs} == {"u-1"}


def test_read_run_owner_scoped(service):
    service._store.records["other"] = _record(created_by="u-2")
    c = TestClient(_app(service))
    er_id = next(iter(service._store.records))
    assert c.get(f"/api/ewcp/runs/{er_id}").status_code == 200
    assert c.get("/api/ewcp/runs/other").status_code == 404
    assert c.get("/api/ewcp/runs/missing").status_code == 404


def test_workrun_proxy_uses_bound_workrun(service):
    c = TestClient(_app(service))
    er_id = next(iter(service._store.records))
    assert c.get(f"/api/ewcp/runs/{er_id}/workrun").json()["workrun"]["workrun_id"] == "wr-1"
    assert service.client.calls[-1] == ("get_workrun", "wr-1")


def test_governed_reads(service):
    c = TestClient(_app(service))
    er_id = next(iter(service._store.records))
    assert c.get(f"/api/ewcp/runs/{er_id}/manifest").json()["manifest_hash"] == "h-1"
    assert c.get(f"/api/ewcp/runs/{er_id}/outcome").json()["outcome_type"] == "invoice_recon"
    r = c.get(f"/api/ewcp/runs/{er_id}/deliverables/d-1")
    assert r.content == b"deliverable-bytes"
    assert r.headers["content-disposition"] == 'attachment; filename="d.json"'


def test_unbound_run_has_no_workrun(service):
    service._store.records["plain"] = _record(workrun_id=None)
    c = TestClient(_app(service))
    assert c.get("/api/ewcp/runs/plain/workrun").status_code == 404


def test_decide_binds_user_actor(service):
    c = TestClient(_app(service))
    er_id = next(iter(service._store.records))
    r = c.post(f"/api/ewcp/runs/{er_id}/decisions", json={"answer": "approve", "decision_id": "d-1"})
    assert r.status_code == 200
    assert service.client.calls[-1] == ("decide", "wr-1", "approve", "d-1", "user:u-1", "user:u-1")


def test_decide_refused_without_binding():
    service = FakeService(records=[_record()], client=FakeClient(), binding=False)
    c = TestClient(_app(service))
    er_id = next(iter(service._store.records))
    assert c.post(f"/api/ewcp/runs/{er_id}/decisions", json={"answer": "approve"}).status_code == 409


def test_outcomes_and_verify_proxies(service):
    c = TestClient(_app(service))
    assert c.get("/api/ewcp/outcomes").json()["outcomes"] == [{"outcome_type": "invoice_recon"}]
    assert c.get("/api/ewcp/verify/h-9").json()["workrun_id"] == "wr-1"
    r = c.post(
        "/api/ewcp/verify",
        files={
            "evidence_json": ("evidence.json", b"{}", "application/json"),
            "files": ("a.json", b"1"),
        },
    )
    assert r.json()["verdict"] == "PASS"


def test_launch_run_uses_bound_agent_runs():
    launcher = FakeLauncher()
    service = FakeService(client=FakeClient(), launcher=launcher)

    class _Runs:
        def for_plugin(self, ns):
            return self

    c = TestClient(_app(service, agent_runs=_Runs()))
    r = c.post("/api/ewcp/runs", data={"intent": "đối soát Q4", "task_mode": "general"})
    assert r.status_code == 200
    assert launcher.launch_calls[0]["intent"] == "đối soát Q4"
    assert launcher.launch_calls[0]["created_by"] == "u-1"


def test_launch_run_rejects_unauthenticated_and_missing_grant():
    service = FakeService(client=FakeClient(), launcher=FakeLauncher())
    c = TestClient(_app(service))  # no agent_runs resolver → None
    assert c.post("/api/ewcp/runs", data={"intent": "x"}).status_code == 403
    c2 = TestClient(_app(service, user=None))
    assert c2.post("/api/ewcp/runs", data={"intent": "x"}).status_code == 401


def test_launch_run_validates_intent():
    service = FakeService(client=FakeClient(), launcher=FakeLauncher())

    class _Runs:
        def for_plugin(self, ns):
            return self

    c = TestClient(_app(service, agent_runs=_Runs()))
    assert c.post("/api/ewcp/runs", data={"intent": "  "}).status_code == 422
