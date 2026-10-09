"""ewcp-core extension contributions: kernel client service + status,
ExecutionRun recovery routers.

Topology (A3, Option A+): the EWCP kernel lives in its own repo and is
reached over HTTP/M2M via `KernelClient` — no in-process ASGI mount.
The service resolves `kernel_url`/`kernel_api_key` env-first
(EWCP_KERNEL_URL / EWCP_KERNEL_API_KEY beat the plugin `config:` block),
starts one shared AsyncClient at Gateway startup, and reports wiring on
`GET /api/ewcp/_status`. The API key is only ever a request header —
the status payload carries `api_key_configured`, never the key.

`GET /api/ewcp/runs` and `POST /api/ewcp/runs/{id}/resume` are the
FOREGROUND recovery surface (Task 3): each request resolves a fresh
bound `AgentRuns` handle + the caller's principal — nothing is retained
between requests, and revoked sessions are denied by the host's per-op
auth before any resume mutates a run. No background recovery exists
(see docs/vnext/A3_DURABILITY.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deerflow_extension_api import AgentBuildContext, AgentScope, ExtensionRuntimeDeps, HostPolicySnapshot, MiddlewarePlacement, Placement
from deerflow_extension_api.agent_runs import AgentRunError, resolve_agent_runs
from deerflow_extension_api.auth import resolve_principal
from fastapi import APIRouter, HTTPException, Request

from .api_routes import build_api_router
from .egress_policy import EgressPolicy
from .execution_run_store import ExecutionRunStore
from .kernel_client import KernelClient, KernelClientConfig
from .model_policy import BudgetAdmissionMiddleware, KernelBudgetClient, ModelPolicyConfig
from .recovery import RecoveryDenied, ResumeNotPending, RunNotOwned, RunRecovery
from .run_launcher import RunLauncher, ThreadUploads


class EwcpCoreService:
    """Owns the shared kernel HTTP client and the ExecutionRunMap launcher
    for the Gateway lifetime.

    The store binds to the shared product DB via
    `ExtensionRuntimeDeps.session_factory` (extension-owned tables under the
    `ewcp_` prefix — declare `table_prefix: ewcp_` on the `plugins:` record;
    see README). The launcher it exposes needs the request-bound `AgentRuns`
    handle (`resolve_agent_runs(request)`) passed per call — never cached.

    Also a MiddlewareContributor: `contribute_middlewares` installs the
    budget-admission gate (A3 Task 5) at Placement.MODEL_PHYSICAL with
    `intercepting=True` — deny decisions must propagate, not be
    swallowed by the host's fail-open isolation wrapper. The middleware
    resolves the live client per call, so kernel-absent deployments get
    the local policy behavior instead of a broken build.
    """

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config: Mapping[str, Any] = config or {}
        self._resolved = KernelClientConfig.resolve(self.config)
        self.egress_policy = EgressPolicy(
            EgressPolicy.resolve_config(self.config),
            kernel_url=self._resolved.kernel_url,
        )
        self._model_policy = ModelPolicyConfig.resolve(self.config)
        self._client: KernelClient | None = None
        self._store: ExecutionRunStore | None = None
        self._launcher: RunLauncher | None = None
        self._recovery: RunRecovery | None = None

    @property
    def client(self) -> KernelClient | None:
        return self._client

    @property
    def store(self) -> ExecutionRunStore | None:
        return self._store

    @property
    def launcher(self) -> RunLauncher | None:
        return self._launcher

    @property
    def recovery(self) -> RunRecovery | None:
        return self._recovery

    @property
    def user_actor_binding(self) -> bool:
        """Whether decision actions may bind the product user as the
        audit principal — requires a kernel honoring `X-Ewcp-Actor`
        (kernel PR #108). Operators running an older kernel set
        `decision_user_binding: false` in the plugin config, which
        renders frontend decision buttons read-only and 409s the route.
        """
        return bool(self.config.get("decision_user_binding", True))

    @property
    def invoke_tenant_id(self) -> str | None:
        """Tenant id stamped on capability invokes (`invoke.tenant_id`,
        falling back to `budget.tenant_id`). Needed when the kernel runs
        dev-mode auth (no API keys → declared tenant is required); under
        tenant-key auth the kernel derives tenant from the key and this
        field is ignored on mismatch (403 tenant_mismatch)."""
        invoke = self.config.get("invoke")
        if isinstance(invoke, Mapping) and invoke.get("tenant_id"):
            return str(invoke["tenant_id"])
        budget = self.config.get("budget")
        if isinstance(budget, Mapping) and budget.get("tenant_id"):
            return str(budget["tenant_id"])
        return None

    def new_launcher(self, uploader: ThreadUploads | None = None) -> RunLauncher | None:
        """Per-request launcher: the shared store plus an uploads seam
        bound to the caller's credentials (HttpThreadUploads)."""
        if self._store is None:
            return None
        return RunLauncher(self._store, uploader=uploader)

    async def start(self, deps: ExtensionRuntimeDeps) -> None:
        if self._resolved.kernel_url:
            self._client = KernelClient(self._resolved)
        session_factory = getattr(deps, "session_factory", None) if deps is not None else None
        if session_factory is not None:
            self._store = ExecutionRunStore(session_factory)
            await self._store.ensure_schema()
            self._launcher = RunLauncher(self._store)
            self._recovery = RunRecovery(self._store)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._launcher = None
        self._store = None
        self._recovery = None

    def contribute_middlewares(self, app_store: Any, ctx: AgentBuildContext) -> tuple[MiddlewarePlacement, ...]:
        policy = ctx.policy if isinstance(getattr(ctx, "policy", None), HostPolicySnapshot) else HostPolicySnapshot()
        middleware = BudgetAdmissionMiddleware(
            KernelBudgetClient(lambda: self._client),
            self._model_policy,
            policy,
        )
        return (MiddlewarePlacement(middleware, Placement.MODEL_PHYSICAL, AgentScope.BOTH, intercepting=True),)

    def status(self) -> dict[str, Any]:
        return {
            "extension": "ewcp_core",
            "kernel_url": self._resolved.kernel_url,
            "kernel_configured": self._resolved.kernel_url is not None,
            "api_key_configured": self._resolved.api_key is not None,
            "client_started": self._client is not None,
            "store_started": self._store is not None,
            "egress": {
                "default_mode": self.egress_policy.config.default_mode,
                "tenant_modes": dict(self.egress_policy.config.tenant_modes),
            },
            "recovery_started": self._recovery is not None,
            "budget_cap_usd": str(self._model_policy.cap_usd) if self._model_policy.cap_usd is not None else None,
            "budget_admission_enabled": self._model_policy.cap_usd is not None,
            "general_on_policy_unavailable": self._model_policy.general_on_policy_unavailable,
        }


def _record_view(record: Any) -> dict[str, Any]:
    return {
        "execution_run_id": record.execution_run_id,
        "thread_id": record.thread_id,
        "run_id": record.run_id,
        "workrun_id": record.workrun_id,
        "task_mode": record.task_mode,
        "status": record.status,
        "intent": record.intent,
        "idempotency_key": record.idempotency_key,
        "created_by": record.created_by,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _recovery_or_503(service: EwcpCoreService) -> RunRecovery:
    if service.recovery is None:
        raise HTTPException(503, "ExecutionRun store is unavailable on this host")
    return service.recovery


def _bound_runs_or_503(request: Request) -> Any:
    runs = resolve_agent_runs(request)
    if runs is None:
        # SESSION/AUTH_DISABLED only — PAT and internal creds never bind, so
        # there is no background/service path to fall back to (by design).
        raise HTTPException(503, "Agent run control is unavailable for this credential")
    return runs


def build_router(service: EwcpCoreService) -> APIRouter:
    router = APIRouter(prefix="/api/ewcp", tags=["ewcp"])

    @router.get("/_status")
    async def read_status() -> dict[str, Any]:
        return service.status()

    @router.get("/runs")
    async def recover_runs(request: Request) -> dict[str, Any]:
        """Foreground recovery: reconcile the caller's ExecutionRunMap rows
        against live thread truth and return the projection."""
        principal = resolve_principal(request)
        if principal is None:
            raise HTTPException(401, "Authentication required")
        runs = _bound_runs_or_503(request)
        try:
            report = await _recovery_or_503(service).recover(agent_runs=runs, created_by=principal.user_id)
        except RecoveryDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except AgentRunError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return {
            "owner": report.created_by,
            "runs": [
                {
                    **_record_view(item.record),
                    "run_status": item.run_status,
                    "pending_interrupt": item.pending_interrupt,
                    "changed": item.changed,
                    "observation_url": item.observation_url,
                }
                for item in report.runs
            ],
        }

    @router.post("/runs/{execution_run_id}/resume")
    async def resume_run(execution_run_id: str, request: Request) -> dict[str, Any]:
        """Submit an interrupt response for a pending ExecutionRun.

        Gated on real pending state — the pre-resume reads through the bound
        handle are the revoke check, so a dead session denies before the
        mutating call. Body: {"resume": <any JSON>, "idempotency_key": str?}.
        """
        principal = resolve_principal(request)
        if principal is None:
            raise HTTPException(401, "Authentication required")
        runs = _bound_runs_or_503(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(422, "resume requires a JSON object body") from exc
        if not isinstance(body, dict) or "resume" not in body:
            raise HTTPException(422, "resume requires a JSON object body with a 'resume' field")
        try:
            record = await _recovery_or_503(service).resume(
                agent_runs=runs,
                created_by=principal.user_id,
                execution_run_id=execution_run_id,
                resume=body["resume"],
                idempotency_key=body.get("idempotency_key"),
            )
        except RecoveryDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except RunNotOwned as exc:
            raise HTTPException(404, "ExecutionRun not found") from exc
        except ResumeNotPending as exc:
            raise HTTPException(409, str(exc)) from exc
        except AgentRunError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return _record_view(record)

    # NOTE: not `router.include_router(build_api_router(service))` —
    # FastAPI 0.136 merges a non-default `lifespan_context` into this router
    # on include, and the host extension gateway rejects contributed routers
    # carrying a lifespan ("register an ExtensionService instead"), which
    # unmounts every /api/ewcp route. The inner router bakes the `/api/ewcp`
    # prefix into each route at decoration time, so appending its routes
    # mounts them at the same final paths the host expects.
    router.routes.extend(build_api_router(service).routes)
    return router
