"""Task-6 product-facing routes: owner scoping, kernel proxying,
decision-actor gating, launch via bound AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ewcp_core.execution_run_store import ExecutionRunRecord
from ewcp_core.plugin import EwcpCoreService, build_router
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

    async def get_by_idempotency_key(self, created_by: str, key: str):
        return next(
            (r for r in self.records.values() if r.created_by == created_by and r.idempotency_key == key),
            None,
        )

    async def list_for_thread(self, thread_id: str, *, limit: int = 50):
        return [r for r in self.records.values() if r.thread_id == thread_id]


class FakeClient:
    """KernelClient shape — records calls, returns canned payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.create_task_response: dict = {"workrun_id": "wr-new"}
        self.create_task_replay = False
        self.run_outcome_response: dict = {"workrun_id": "wr-new"}
        self.list_workrun_statuses_response: list = []
        self.list_workrun_statuses_error: Exception | None = None

    async def get_workrun(self, workrun_id):
        self.calls.append(("get_workrun", workrun_id))
        return {"workrun_id": workrun_id, "status": "awaiting_approval"}

    async def list_workrun_statuses(self, workrun_ids, *, tenant_id=None):
        self.calls.append(("list_workrun_statuses", list(workrun_ids), tenant_id))
        if self.list_workrun_statuses_error is not None:
            raise self.list_workrun_statuses_error
        return list(self.list_workrun_statuses_response)

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

    async def create_task(self, *, intent, tenant_id=None, fields=None, files=None, idempotency_key=None, timeout=None, actor=None):
        self.calls.append(
            (
                "create_task",
                intent,
                tenant_id,
                dict(fields or {}),
                {k: [m[0] for m in v] for k, v in (files or {}).items()},
                idempotency_key,
                actor,
            )
        )
        run = dict(self.create_task_response)
        return SimpleNamespace(
            run=run,
            idempotent_replay=self.create_task_replay,
        )

    async def run_outcome(self, outcome_type, *, tenant_id=None, fields=None, files=None, timeout=None, actor=None):
        self.calls.append(
            (
                "run_outcome",
                outcome_type,
                tenant_id,
                dict(fields or {}),
                {k: [m[0] for m in v] for k, v in (files or {}).items()},
                actor,
            )
        )
        return dict(self.run_outcome_response)

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
        record = _record(
            thread_id="t-9",
            intent=kwargs["intent"],
            task_mode=kwargs["mode"],
            workrun_id=kwargs.get("workrun_id") or "wr-1",
        )
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
    def __init__(self, *, records=None, client=None, binding=True, launcher=None, config=None, tenant_id=None) -> None:
        self._store = FakeStore(records)
        self.client = client
        self._binding = binding
        self._launcher = launcher
        self._recovery = FakeRecovery(self._store)
        self.config: dict = dict(config or {})
        self._tenant_id = tenant_id

    @property
    def invoke_tenant_id(self):
        return self._tenant_id

    @property
    def store(self):
        return self._store

    @property
    def recovery(self):
        return self._recovery

    @property
    def user_actor_binding(self) -> bool:
        return self._binding

    def new_launcher(self, uploader=None, starter=None):
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


# -- A6-07 workrun_status list projection -------------------------------------


def test_list_runs_hydrates_workrun_status_batched():
    """Bound rows get the kernel projection via ONE batched call — the
    approval exists to kill per-row fetches, so exactly one
    list_workrun_statuses call may cover every bound id."""
    client = FakeClient()
    client.list_workrun_statuses_response = [
        {
            "workrun_id": "wr-1",
            "status": "awaiting_input",
            "pending_decision": True,
            "last_event_at": 1700000001.5,
        }
    ]
    service = FakeService(
        records=[
            _record(workrun_id="wr-1"),
            _record(workrun_id="wr-2"),
            _record(workrun_id=None),
        ],
        client=client,
        tenant_id="t-1",
    )
    c = TestClient(_app(service, agent_runs=object()))

    runs = c.get("/api/ewcp/runs").json()["runs"]
    assert len(runs) == 3
    by_wr = {r.get("workrun_id"): r for r in runs}
    assert by_wr["wr-1"]["workrun_status"] == {
        "status": "awaiting_input",
        "pending_decision": True,
        "last_event_at": 1700000001.5,
    }
    # kernel-truth-at-query-time: unknown to kernel -> no projection
    assert "workrun_status" not in by_wr["wr-2"]
    # unbound map row -> never projected
    assert "workrun_status" not in by_wr[None]

    calls = [c_ for c_ in client.calls if c_[0] == "list_workrun_statuses"]
    assert len(calls) == 1
    _, ids, tenant_id = calls[0]
    assert ids == ["wr-1", "wr-2"]
    assert tenant_id == "t-1"


def test_list_runs_projection_degrades_on_kernel_outage():
    """A kernel error degrades the projection, never the list itself —
    the map rows are still renderable off launcher state."""
    import httpx

    client = FakeClient()
    client.list_workrun_statuses_error = httpx.ConnectError("kernel down")
    service = FakeService(records=[_record()], client=client)
    c = TestClient(_app(service, agent_runs=object()))

    resp = c.get("/api/ewcp/runs")
    assert resp.status_code == 200
    (run,) = resp.json()["runs"]
    assert "workrun_status" not in run


def test_list_runs_skips_projection_without_client():
    service = FakeService(records=[_record()], client=None)
    c = TestClient(_app(service, agent_runs=object()))
    resp = c.get("/api/ewcp/runs")
    assert resp.status_code == 200
    assert "workrun_status" not in resp.json()["runs"][0]


def test_list_runs_thread_branch_stays_map_only():
    """?thread_id= is the chat-page reverse lookup — documented as a
    map-store read only; it must NOT fan out to the kernel."""
    client = FakeClient()
    service = FakeService(records=[_record(thread_id="t-1")], client=client)
    c = TestClient(_app(service))
    runs = c.get("/api/ewcp/runs", params={"thread_id": "t-1"}).json()["runs"]
    assert len(runs) == 1
    assert "workrun_status" not in runs[0]
    assert not [c_ for c_ in client.calls if c_[0] == "list_workrun_statuses"]


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


def test_decide_replaces_client_supplied_actor_header(service):
    """C10 anti-impersonation: a caller-asserted X-Ewcp-Actor never
    reaches the kernel — the route mints the actor from the session
    principal, replacing the forged value."""
    c = TestClient(_app(service))
    er_id = next(iter(service._store.records))
    r = c.post(
        f"/api/ewcp/runs/{er_id}/decisions",
        json={"answer": "approve", "decision_id": "d-1"},
        headers={"X-Ewcp-Actor": "user:admin"},
    )
    assert r.status_code == 200
    assert service.client.calls[-1] == ("decide", "wr-1", "approve", "d-1", "user:u-1", "user:u-1")


def test_outcomes_proxy(service):
    c = TestClient(_app(service))
    assert c.get("/api/ewcp/outcomes").json()["outcomes"] == [{"outcome_type": "invoice_recon"}]


def _public_verify_app(service: EwcpCoreService) -> FastAPI:
    """Host-mounted verify surface (A3 Task 7): the public routes are NOT
    extension-contributed — the gateway registers
    app.gateway.routers.ewcp_verify, whose handlers resolve the running
    service on app.state.extensions."""
    from app.gateway.routers.ewcp_verify import router as verify_router

    app = FastAPI()
    app.include_router(verify_router)
    app.state.extensions = SimpleNamespace(services=[("ewcp_core:install", service)])
    return app


def _real_service(client: FakeClient) -> EwcpCoreService:
    service = EwcpCoreService(config={})
    service._client = client  # start() normally wires this from kernel_url
    return service


def test_verify_routes_serve_anonymous_verifiers():
    """PUBLIC verify contract (A3 Task 7): the host-mounted permalink and
    verify-by-them routes answer without request.state.user — the
    manifest hash is the capability."""
    c = TestClient(_public_verify_app(_real_service(FakeClient())))
    assert c.get("/api/ewcp/verify/h-9").json()["workrun_id"] == "wr-1"
    r = c.post(
        "/api/ewcp/verify",
        files={
            "evidence_json": ("evidence.json", b"{}", "application/json"),
            "files": ("a.json", b"1"),
        },
    )
    assert r.json()["verdict"] == "PASS"


def test_verify_routes_404_when_extension_absent():
    """The permalink surface exists only where ewcp-core runs."""
    app = _public_verify_app(_real_service(FakeClient()))
    app.state.extensions = SimpleNamespace(services=[])
    assert TestClient(app).get("/api/ewcp/verify/h-9").status_code == 404


def test_extension_router_does_not_claim_verify_paths(service):
    """The contributed router must not re-claim /api/ewcp/verify — the
    harness would unmount it for entering the reserved public namespace."""
    c = TestClient(_app(service, user=None))
    assert c.get("/api/ewcp/verify/h-9").status_code == 404
    assert c.post("/api/ewcp/verify").status_code == 404


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


# -- governed launch: kernel-first intake (A6 gap #1) -------------------------


class _Runs:
    def for_plugin(self, ns):
        return self


def test_governed_launch_dispatches_via_create_task_and_binds_workrun():
    client = FakeClient()
    launcher = FakeLauncher()
    service = FakeService(client=client, launcher=launcher)
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post(
        "/api/ewcp/runs",
        data={
            "intent": "đối soát Q4",
            "task_mode": "governed",
            "idempotency_key": "k-1",
            "mst": "0101",
        },
        files=[("invoices_zip", ("inv.zip", b"PKfake", "application/zip"))],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["run"]["workrun_id"] == "wr-new"
    assert body["run"]["task_mode"] == "governed"

    call = client.calls[0]
    assert call[0] == "create_task"
    assert call[1] == "đối soát Q4"
    assert call[3] == {"mst": "0101"}
    assert call[4] == {"invoices_zip": ["inv.zip"]}
    assert call[5] == "k-1"

    launch = launcher.launch_calls[0]
    assert launch["mode"] == "governed"
    assert launch["workrun_id"] == "wr-new"
    assert launch["context"] == {"kernel": {"workrun_id": "wr-new"}}
    assert launch["files"] == []
    assert launch["idempotency_key"] == "k-1"


def test_governed_launch_with_outcome_type_dispatches_run_outcome():
    client = FakeClient()
    launcher = FakeLauncher()
    service = FakeService(client=client, launcher=launcher, config={"tenant_id": "demo"})
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post(
        "/api/ewcp/runs",
        data={
            "intent": "đối soát",
            "task_mode": "governed",
            "outcome_type": "invoice_recon",
        },
        files=[("invoices_zip", ("inv.zip", b"PKfake", "application/zip"))],
    )
    assert r.status_code == 200

    call = client.calls[0]
    assert call[0] == "run_outcome"
    assert call[1] == "invoice_recon"
    assert call[2] == "demo"
    assert call[4] == {"invoices_zip": ["inv.zip"]}
    assert launcher.launch_calls[0]["workrun_id"] == "wr-new"


def test_governed_launch_binds_session_actor_replacing_client_header():
    """C10: governed intake binds ProposedAction.requester — the actor
    minted from the authenticated session travels on every intake
    variant, and a forged inbound X-Ewcp-Actor is dropped, never
    forwarded."""
    client = FakeClient()
    launcher = FakeLauncher()
    service = FakeService(client=client, launcher=launcher)
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post(
        "/api/ewcp/runs",
        data={
            "intent": "đối soát Q4",
            "task_mode": "governed",
            "idempotency_key": "k-1",
        },
        headers={"X-Ewcp-Actor": "user:admin"},
    )
    assert r.status_code == 200
    call = client.calls[0]
    assert call[0] == "create_task"
    # session-bound actor, not the forged header value
    assert call[6] == "user:u-1"

    r2 = c.post(
        "/api/ewcp/runs",
        data={
            "intent": "đối soát",
            "task_mode": "governed",
            "outcome_type": "invoice_recon",
        },
        headers={"X-Ewcp-Actor": "user:admin"},
    )
    assert r2.status_code == 200
    call2 = client.calls[1]
    assert call2[0] == "run_outcome"
    assert call2[5] == "user:u-1"


def test_governed_launch_form_tenant_beats_config():
    client = FakeClient()
    service = FakeService(client=client, launcher=FakeLauncher(), config={"tenant_id": "demo"})
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post(
        "/api/ewcp/runs",
        data={"intent": "x", "task_mode": "governed", "tenant_id": "acme"},
    )
    assert r.status_code == 200
    assert client.calls[0][2] == "acme"


def test_governed_launch_clarify_returns_422_and_spawns_nothing():
    client = FakeClient()
    client.create_task_response = {"status": "clarify", "clarify_question": "Loại hóa đơn nào?"}
    launcher = FakeLauncher()
    service = FakeService(client=client, launcher=launcher)
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post("/api/ewcp/runs", data={"intent": "mơ hồ", "task_mode": "governed"})
    assert r.status_code == 422
    assert "Loại hóa đơn nào?" in r.text
    assert launcher.launch_calls == []


def test_governed_launch_replays_on_same_idempotency_key_without_kernel_call():
    client = FakeClient()
    launcher = FakeLauncher()
    existing = _record(key="k-9", intent="đối soát", task_mode="governed", workrun_id="wr-old")
    service = FakeService(records=[existing], client=client, launcher=launcher)
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post(
        "/api/ewcp/runs",
        data={"intent": "đối soát", "task_mode": "governed", "idempotency_key": "k-9"},
    )
    assert r.status_code == 200
    assert r.json()["idempotent_replay"] is True
    assert r.json()["run"]["workrun_id"] == "wr-old"
    assert client.calls == []
    assert launcher.launch_calls == []


def test_governed_launch_conflicting_idempotency_key_returns_409():
    client = FakeClient()
    service = FakeService(
        records=[_record(key="k-9", intent="khác", task_mode="governed")],
        client=client,
        launcher=FakeLauncher(),
    )
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post(
        "/api/ewcp/runs",
        data={"intent": "đối soát", "task_mode": "governed", "idempotency_key": "k-9"},
    )
    assert r.status_code == 409
    assert client.calls == []


def test_governed_launch_with_bound_workrun_skips_kernel_intake():
    client = FakeClient()
    launcher = FakeLauncher()
    service = FakeService(client=client, launcher=launcher)
    c = TestClient(_app(service, agent_runs=_Runs()))

    r = c.post(
        "/api/ewcp/runs",
        data={"intent": "đối soát", "task_mode": "governed", "workrun_id": "wr-ext"},
    )
    assert r.status_code == 200
    assert client.calls == []
    launch = launcher.launch_calls[0]
    assert launch["workrun_id"] == "wr-ext"
    assert launch["context"] is None


def test_governed_launch_requires_configured_kernel():
    service = FakeService(client=None, launcher=FakeLauncher())
    c = TestClient(_app(service, agent_runs=_Runs()))
    r = c.post("/api/ewcp/runs", data={"intent": "x", "task_mode": "governed"})
    assert r.status_code == 503


def test_list_runs_filters_by_thread_and_owner_scope():
    mine = _record(thread_id="t-1", task_mode="governed")
    other_thread = _record(thread_id="t-2")
    other_owner = _record(thread_id="t-1", created_by="u-2")
    service = FakeService(records=[mine, other_thread, other_owner])
    c = TestClient(_app(service))

    r = c.get("/api/ewcp/runs", params={"thread_id": "t-1"})
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["thread_id"] == "t-1"
    assert runs[0]["created_by"] == "u-1"
