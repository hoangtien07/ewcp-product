"""Product-facing ExecutionRun + kernel-proxy routes (A3 Task 6).

The browser NEVER sees the kernel key: every governed read/decision goes
through these session-authenticated routes, which owner-scope the
ExecutionRunMap row and forward to the kernel via the service's M2M
`KernelClient`. Observation itself rides the Gateway SSE routes directly
(`run_join_url` on the record view) — no proxy needed there.

The two verify routes are NOT here: `GET /verify/{hash}` and
`POST /verify` are PUBLIC by kernel contract (the manifest hash is the
capability), and the extension gateway fences contributed routes out of
the host public namespace — so they are mounted host-side at
`app.gateway.ewcp_verify` and share their logic through
`ewcp_core.verify_surface`. Everything below stays session-gated.

Decision gating (plan P1): `POST /runs/{id}/decisions` only serves when
`service.user_actor_binding` is on — i.e. the deployment's kernel honors
`X-Ewcp-Actor` (kernel PR #108), so the audit principal binds
`user:<id>@tenant:<tenant>` instead of the bare tenant. With binding off
the frontend renders decisions read-only.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from .run_launcher import (
    EWCP_RUNS_NAMESPACE,
    FilePayload,
    FilesWithoutUploader,
    HttpRunStarter,
    HttpThreadUploads,
    LaunchConflict,
    UploadsError,
    run_join_url,
)

if TYPE_CHECKING:
    from .plugin import EwcpCoreService

logger = logging.getLogger(__name__)

# Headers the uploads call reuses from the incoming browser request —
# the session cookie (or Bearer PAT) authenticates it and the CSRF
# double-submit pair satisfies the mutating-route check.
_FORWARDED_AUTH_HEADERS = ("cookie", "authorization", "x-csrf-token")

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Form fields the launch route consumes itself — every other PLAIN form
# field on a governed intake is a spec context key (mst, ky…) forwarded
# to the kernel verbatim; every other FILE field is an input slot
# (invoices_zip, books…).
_LAUNCH_FORM_FIELDS = frozenset(
    {
        "intent",
        "task_mode",
        "workrun_id",
        "idempotency_key",
        "outcome_type",
        "tenant_id",
        "files",
    }
)


def _user_id(request: Request) -> str:
    """Authenticated product user id — set by the gateway auth layer for
    session and auth-disabled callers alike; PAT callers carry it too
    (their durable-grant limits surface through `resolve_agent_runs`)."""
    user = getattr(request.state, "user", None)
    uid = getattr(user, "id", None) if user is not None else None
    if uid is None:
        raise HTTPException(401, "authentication required")
    return str(uid)


def _actor(user_id: str) -> str:
    return f"user:{user_id}"


def _kernel_error(exc: Exception) -> HTTPException:
    from .kernel_client import KernelNotConfigured

    if isinstance(exc, KernelNotConfigured):
        return HTTPException(503, "ewcp kernel is not configured")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return HTTPException(status if 400 <= status < 600 else 502, "kernel rejected the request")
    if isinstance(exc, httpx.TransportError):
        return HTTPException(502, "kernel unreachable")
    return HTTPException(502, "kernel request failed")


def _record_view(record) -> dict[str, Any]:
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
        "created_at": str(record.created_at),
        "updated_at": str(record.updated_at),
        "join_url": run_join_url(record.thread_id, record.run_id) if record.run_id else None,
        "integrity_flag": getattr(record, "integrity_flag", None),
    }


async def _owned_record(service: EwcpCoreService, execution_run_id: str, user_id: str):
    store = service.store
    if store is None:
        raise HTTPException(503, "execution run store is not started")
    record = await store.get(execution_run_id)
    if record is None or record.created_by != user_id:
        raise HTTPException(404, "unknown execution run")
    return record


def _require_client(service: EwcpCoreService):
    if service.client is None:
        raise HTTPException(503, "ewcp kernel is not configured")
    return service.client


async def _workrun_record(service: EwcpCoreService, execution_run_id: str, user_id: str):
    record = await _owned_record(service, execution_run_id, user_id)
    if not record.workrun_id:
        raise HTTPException(404, "run has no governed workrun binding")
    return record


class DecisionBody(BaseModel):
    answer: str = Field(min_length=1, max_length=200)
    decision_id: str | None = Field(default=None, max_length=200)


def request_scoped_client(request: Request) -> httpx.AsyncClient:
    """An httpx client carrying the request's own credentials for the
    thread-uploads and run-admission hops — the caller owns it and must
    aclose() it."""
    headers = {name: request.headers[name] for name in _FORWARDED_AUTH_HEADERS if name in request.headers}
    return httpx.AsyncClient(
        base_url=str(request.base_url).rstrip("/"),
        headers=headers,
        timeout=60.0,
    )


def build_api_router(service: EwcpCoreService) -> APIRouter:
    # Prefix baked per-route: plugin.build_router appends these routes onto
    # the /api/ewcp extension router verbatim (it cannot include_router —
    # FastAPI 0.136 include merges a non-default lifespan_context that the
    # host extension gateway rejects), so each route must carry the full path.
    router = APIRouter(prefix="/api/ewcp", tags=["ewcp"])

    @router.get("/identity")
    async def read_identity(request: Request) -> dict[str, Any]:
        """Who the product user is for decision binding — the frontend
        gates Approve/Reject buttons on `user_actor_binding`."""
        uid = _user_id(request)
        return {
            "user_id": uid,
            "actor": _actor(uid),
            "user_actor_binding": service.user_actor_binding,
        }

    # NOTE: GET /runs intentionally lives in plugin.build_router — the
    # foreground-recovery handler is the canonical list route (its
    # response is a superset: owner + reconciled run projection).
    @router.post("/runs")
    async def launch_run(request: Request) -> dict[str, Any]:
        """Launch an ExecutionRun: product thread + run via the request's
        bound AgentRuns grant, projected into the ExecutionRunMap.
        Multipart: `intent` (required), `task_mode` (general|governed),
        `workrun_id`, `idempotency_key`, `outcome_type`, `tenant_id`,
        `files` (repeatable thread attachments for `general`) plus, for
        `governed`, any file field whose name is a spec input slot
        (`invoices_zip`, `books`, …) and any plain field treated as a
        spec context key.

        Governed ordering is kernel-first: dispatch the pack pipeline via
        `POST /tasks` (router intake, kernel-deduped on Idempotency-Key)
        or `POST /outcomes/{type}/run` (explicit pack — no kernel replay
        contract, so the ExecutionRunMap row dedupes retries) BEFORE the
        product thread exists — an intake 422 or clarify never leaves an
        orphan thread behind. The returned `workrun_id` then rides the
        normal launch path, which writes the map row already bound and
        stamps `context.kernel.workrun_id` for budget admission
        identity (A3 §Identity)."""
        from deerflow_extension_api.agent_runs import AgentRunError, resolve_agent_runs

        uid = _user_id(request)
        agent_runs = resolve_agent_runs(request)
        if agent_runs is None:
            raise HTTPException(403, "execution runs require an authenticated session grant")
        launcher = service.new_launcher()
        if launcher is None:
            raise HTTPException(503, "execution run store is not started")

        def _field(name: str) -> str | None:
            value = form.get(name)
            return value if isinstance(value, str) else None

        form = await request.form()
        intent = _field("intent")
        if intent is None or not intent.strip():
            raise HTTPException(422, "intent is required")
        task_mode = _field("task_mode") or "general"
        idempotency_key = _field("idempotency_key")
        workrun_id = _field("workrun_id")

        uploads_by_field: dict[str, list[FilePayload]] = {}
        for key, item in form.multi_items():
            if not isinstance(item, UploadFile):
                continue
            payload = FilePayload(
                filename=item.filename or "upload",
                body=await item.read(),
                content_type=item.content_type or "application/octet-stream",
            )
            uploads_by_field.setdefault(key, []).append(payload)
        for payloads in uploads_by_field.values():
            for f in payloads:
                if len(f.body) > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"file {f.filename!r} exceeds {_MAX_UPLOAD_BYTES} bytes")

        context: dict[str, Any] | None = None
        if task_mode == "governed" and workrun_id is None:
            # Kernel-first governed intake: the workrun is created (and
            # the pack pipeline dispatched, synchronously) before any
            # product thread/run exists.
            client = _require_client(service)
            key = idempotency_key or f"{EWCP_RUNS_NAMESPACE}:{uuid.uuid4().hex}"
            idempotency_key = key
            store = service.store
            existing = await store.get_by_idempotency_key(uid, key) if store is not None else None
            if existing is not None:
                if existing.intent != intent or existing.task_mode != task_mode:
                    raise HTTPException(409, "idempotency key already used for a different launch")
                if existing.workrun_id:
                    # Product-level replay: never reach the kernel twice
                    # for the same owner+key — this is what makes the
                    # (idempotency-free) /outcomes run contract safe to
                    # retry from the UI.
                    return {"run": _record_view(existing), "idempotent_replay": True}
            tenant_id = _field("tenant_id") or service.config.get("tenant_id")
            context_fields = {name: value for name, value in form.multi_items() if isinstance(value, str) and name not in _LAUNCH_FORM_FIELDS}
            kernel_files = {slot: [(f.filename, f.body, f.content_type) for f in payloads] for slot, payloads in uploads_by_field.items()}
            timeout = float(service.config.get("intake_timeout_seconds") or 180.0)
            # C10: the kernel binds ProposedAction.requester / audit
            # principals from X-Ewcp-Actor — minted HERE from the session,
            # never from the inbound request (a forged client header is
            # dropped by this boundary).
            actor = _actor(uid)
            try:
                outcome_type = _field("outcome_type")
                if outcome_type:
                    run_view = await client.run_outcome(
                        outcome_type,
                        tenant_id=tenant_id,
                        fields=context_fields,
                        files=kernel_files,
                        timeout=timeout,
                        actor=actor,
                    )
                else:
                    submitted = await client.create_task(
                        intent=intent,
                        tenant_id=tenant_id,
                        fields=context_fields,
                        files=kernel_files,
                        idempotency_key=key,
                        timeout=timeout,
                        actor=actor,
                    )
                    run_view = submitted.run
            except Exception as exc:
                raise _kernel_error(exc) from exc
            workrun_id = run_view.get("workrun_id")
            if not workrun_id:
                # Router clarify: no run was created — surface the
                # kernel's question so the user can refine the intent.
                detail = run_view.get("clarify_question") or "kernel could not route the intent to an outcome"
                raise HTTPException(422, detail)
            context = {"kernel": {"workrun_id": workrun_id}}

        # Governed uploads were already delivered to the kernel under
        # their slot names — they do not double as thread attachments.
        files = [] if task_mode == "governed" else uploads_by_field.get("files", [])
        # F1: the bound AgentRuns.start() cannot carry run config, so the
        # launch's run admission goes through the request-scoped HTTP seam
        # (same route the chat UI uses) with config.recursion_limit. The
        # bound handle still serves thread creation + status reads.
        request_client = request_scoped_client(request)
        launcher = service.new_launcher(
            uploader=HttpThreadUploads(request_client) if files else None,
            starter=HttpRunStarter(request_client),
        )
        assert launcher is not None
        try:
            outcome = await launcher.launch(
                agent_runs=agent_runs,
                intent=intent,
                mode=task_mode,
                created_by=uid,
                files=files,
                idempotency_key=idempotency_key,
                workrun_id=workrun_id,
                context=context,
            )
        except LaunchConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (FilesWithoutUploader, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except UploadsError as exc:
            raise HTTPException(502, str(exc)) from exc
        except AgentRunError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        finally:
            await request_client.aclose()
        return {
            "run": _record_view(outcome.record),
            "idempotent_replay": outcome.idempotent_replay,
        }

    @router.get("/runs/{execution_run_id}")
    async def read_run(execution_run_id: str, request: Request, refresh: bool = False) -> dict[str, Any]:
        record = await _owned_record(service, execution_run_id, _user_id(request))
        if refresh:
            from deerflow_extension_api.agent_runs import resolve_agent_runs

            agent_runs = resolve_agent_runs(request)
            launcher = service.new_launcher()
            if agent_runs is not None and launcher is not None:
                try:
                    record = await launcher.refresh(agent_runs=agent_runs, record=record)
                except Exception:
                    logger.warning("execution run refresh failed", exc_info=True)
        return {"run": _record_view(record)}

    @router.get("/runs/{execution_run_id}/workrun")
    async def read_workrun(execution_run_id: str, request: Request) -> dict[str, Any]:
        record = await _workrun_record(service, execution_run_id, _user_id(request))
        try:
            return {"workrun": await _require_client(service).get_workrun(record.workrun_id)}
        except Exception as exc:
            raise _kernel_error(exc) from exc

    @router.get("/runs/{execution_run_id}/manifest")
    async def read_manifest(execution_run_id: str, request: Request) -> dict[str, Any]:
        record = await _workrun_record(service, execution_run_id, _user_id(request))
        try:
            return await _require_client(service).get_manifest(record.workrun_id)
        except Exception as exc:
            raise _kernel_error(exc) from exc

    @router.get("/runs/{execution_run_id}/outcome")
    async def read_outcome(execution_run_id: str, request: Request) -> dict[str, Any]:
        record = await _workrun_record(service, execution_run_id, _user_id(request))
        try:
            return await _require_client(service).get_outcome(record.workrun_id)
        except Exception as exc:
            raise _kernel_error(exc) from exc

    @router.get("/runs/{execution_run_id}/evidence")
    async def read_evidence(execution_run_id: str, request: Request) -> dict[str, Any]:
        record = await _workrun_record(service, execution_run_id, _user_id(request))
        try:
            return await _require_client(service).get_evidence(record.workrun_id)
        except Exception as exc:
            raise _kernel_error(exc) from exc

    @router.get("/runs/{execution_run_id}/deliverables/{deliverable_id}")
    async def download_deliverable(execution_run_id: str, deliverable_id: str, request: Request) -> Response:
        record = await _workrun_record(service, execution_run_id, _user_id(request))
        try:
            upstream = await _require_client(service).download_deliverable(record.workrun_id, deliverable_id)
        except Exception as exc:
            raise _kernel_error(exc) from exc
        headers = {}
        disposition = upstream.headers.get("content-disposition")
        if disposition:
            headers["Content-Disposition"] = disposition
        return Response(
            content=upstream.content,
            media_type=upstream.headers.get("content-type", "application/octet-stream"),
            headers=headers,
        )

    @router.post("/runs/{execution_run_id}/decisions")
    async def decide(execution_run_id: str, body: DecisionBody, request: Request) -> dict[str, Any]:
        """Decide a governed question — gated on the user-actor binding
        contract: without it the kernel would only record the tenant
        principal, so the route refuses instead of silently downgrading
        the audit identity."""
        uid = _user_id(request)
        if not service.user_actor_binding:
            raise HTTPException(409, "decision actions are disabled: user-actor binding is not enabled on this deployment")
        record = await _workrun_record(service, execution_run_id, uid)
        actor = _actor(uid)
        try:
            return await _require_client(service).decide(
                record.workrun_id,
                answer=body.answer,
                decision_id=body.decision_id,
                decided_by=actor,
                actor=actor,
            )
        except Exception as exc:
            raise _kernel_error(exc) from exc

    @router.get("/outcomes")
    async def list_outcomes(request: Request) -> dict[str, Any]:
        _user_id(request)
        try:
            return {"outcomes": await _require_client(service).list_outcomes()}
        except Exception as exc:
            raise _kernel_error(exc) from exc

    return router
