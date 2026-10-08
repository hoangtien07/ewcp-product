"""Product-facing ExecutionRun + kernel-proxy routes (A3 Task 6).

The browser NEVER sees the kernel key: every governed read/decision goes
through these session-authenticated routes, which owner-scope the
ExecutionRunMap row and forward to the kernel via the service's M2M
`KernelClient`. Observation itself rides the Gateway SSE routes directly
(`run_join_url` on the record view) — no proxy needed there.

Exception: the two verify routes are PUBLIC by kernel contract — the
manifest hash is the capability (`GET /verify/{hash}` permalink,
`POST /verify` verify-by-them). They serve anonymous third parties, so
`/api/ewcp/verify` sits in the gateway's public path prefixes and the
handlers never consult request.state.user.

Decision gating (plan P1): `POST /runs/{id}/decisions` only serves when
`service.user_actor_binding` is on — i.e. the deployment's kernel honors
`X-Ewcp-Actor` (kernel PR #108), so the audit principal binds
`user:<id>@tenant:<tenant>` instead of the bare tenant. With binding off
the frontend renders decisions read-only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from .run_launcher import (
    FilePayload,
    FilesWithoutUploader,
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


def _request_scoped_uploader(request: Request) -> httpx.AsyncClient:
    """An httpx client carrying the request's own credentials for the
    thread-uploads hop — the caller owns it and must aclose() it."""
    headers = {name: request.headers[name] for name in _FORWARDED_AUTH_HEADERS if name in request.headers}
    return httpx.AsyncClient(
        base_url=str(request.base_url).rstrip("/"),
        headers=headers,
        timeout=60.0,
    )


def build_api_router(service: EwcpCoreService) -> APIRouter:
    # No prefix — mounted under the extension router's /api/ewcp.
    router = APIRouter(tags=["ewcp"])

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
        `workrun_id`, `idempotency_key`, `files` (repeatable)."""
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
        files = [
            FilePayload(
                filename=f.filename or "upload",
                body=await f.read(),
                content_type=f.content_type or "application/octet-stream",
            )
            for f in form.getlist("files")
            if isinstance(f, UploadFile)
        ]
        for f in files:
            if len(f.body) > _MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"file {f.filename!r} exceeds {_MAX_UPLOAD_BYTES} bytes")
        uploader_client: httpx.AsyncClient | None = None
        if files:
            uploader_client = _request_scoped_uploader(request)
            launcher = service.new_launcher(HttpThreadUploads(uploader_client))
        assert launcher is not None
        try:
            outcome = await launcher.launch(
                agent_runs=agent_runs,
                intent=intent,
                mode=_field("task_mode") or "general",
                created_by=uid,
                files=files,
                idempotency_key=_field("idempotency_key"),
                workrun_id=_field("workrun_id"),
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
            if uploader_client is not None:
                await uploader_client.aclose()
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

    @router.get("/verify/{manifest_hash}")
    async def verify_permalink(manifest_hash: str) -> dict[str, Any]:
        """PUBLIC seal permalink — the kernel's `GET /verify/{hash}`
        contract (the hash is the capability; anonymous third parties
        resolve seal state + manifest contents through it)."""
        try:
            return await _require_client(service).verify_manifest(manifest_hash)
        except Exception as exc:
            raise _kernel_error(exc) from exc

    @router.post("/verify")
    async def verify_evidence(request: Request) -> dict[str, Any]:
        """PUBLIC verify-by-them upload — byte-integrity check: forward
        the verifier's evidence.json + declared artifacts to the
        kernel's `POST /verify` (same anonymous contract kernel-side)."""
        form = await request.form()
        evidence = form.get("evidence_json")
        if not isinstance(evidence, UploadFile):
            raise HTTPException(422, "evidence_json file is required")
        files = [(f.filename or "file", await f.read()) for f in form.getlist("files") if isinstance(f, UploadFile)]
        try:
            return await _require_client(service).verify_evidence(
                evidence_json=(evidence.filename or "evidence.json", await evidence.read()),
                files=files,
            )
        except Exception as exc:
            raise _kernel_error(exc) from exc

    return router
