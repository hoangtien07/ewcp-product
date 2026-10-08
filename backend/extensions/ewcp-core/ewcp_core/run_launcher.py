"""ExecutionRun launcher — product-side run admission via the bound
`AgentRuns` extension contract.

Flow (plan Task 2):
    launch(intent, mode)
      -> agent_runs.for_plugin("ewcp.core")          (namespaces idempotency keys)
      -> create_thread()
      -> uploads POST /api/threads/{id}/uploads      (BEFORE start, via an
         injected ThreadUploads; the bound AgentRuns surface has no upload
         capability, so the caller binds a request-authenticated uploader)
      -> start(input={messages:[human, additional_kwargs.files]},
               idempotency_key=...)
      -> ExecutionRunMap row (persisted BEFORE start so a crash mid-launch
         retains ownership — host dedupe keys `extension:{ns}:{key}` globally
         and rejects the same key on a different thread).

Observation is intentionally NOT via AgentRuns: callers stream the Gateway SSE
routes below (`RUN_STREAM_PATH`, `RUN_JOIN_PATH`, `RUN_STREAM_EXISTING_PATH`);
`refresh()` reconciles the map from `get()`/`get_state()` for non-SSE
consumers, with pending-interrupt != complete (a `success` run on a thread
with pending interrupts projects to `pending_interrupt`).

`on_disconnect="continue"` is asserted by the host bound impl
(`extension_agent_runs.py:142`) on every admission path (`start` and
`resume`); there is no parameter to pass — verified, not configurable.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from .execution_run_store import TABLE_PREFIX, ExecutionRunRecord, ExecutionRunStore

if TYPE_CHECKING:
    from deerflow_extension_api.agent_runs import AgentRun, AgentRuns

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE observation route constants (verified on product/vnext,
# backend/app/gateway/routers/thread_runs.py and routers/uploads.py).
# ---------------------------------------------------------------------------

#: POST create-run-and-stream (thread_runs.py:970; Content-Location carries
#: the run URL). Body is the RunCreateRequest; used for "launch + observe".
RUN_STREAM_PATH = "/api/threads/{thread_id}/runs/stream"

#: GET read-only SSE join on an existing run (thread_runs.py:1312,
#: apply_on_disconnect=False — observation never cancels work).
RUN_JOIN_PATH = "/api/threads/{thread_id}/runs/{run_id}/join"

#: POST (join + optional action=interrupt|rollback, :1430) and GET
#: (observation-only join, :1443) on an existing run.
RUN_STREAM_EXISTING_PATH = "/api/threads/{thread_id}/runs/{run_id}/stream"

#: POST multipart upload onto a thread (routers/uploads.py:388).
THREAD_UPLOADS_PATH = "/api/threads/{thread_id}/uploads"

#: AgentRuns plugin namespace: idempotency keys are scoped
#: `extension:ewcp.core:<key>` by the host bound impl (:143).
EWCP_RUNS_NAMESPACE = "ewcp.core"

__all__ = [
    "EWCP_RUNS_NAMESPACE",
    "RUN_STREAM_PATH",
    "RUN_JOIN_PATH",
    "RUN_STREAM_EXISTING_PATH",
    "THREAD_UPLOADS_PATH",
    "TABLE_PREFIX",
    "ExecutionRunStatus",
    "FilePayload",
    "FilesWithoutUploader",
    "HttpThreadUploads",
    "LaunchConflict",
    "LaunchOutcome",
    "RunLauncher",
    "TaskMode",
    "ThreadUploads",
    "UploadedFileRef",
    "UploadsError",
    "build_run_input",
    "detect_pending_interrupt",
    "project_run_status",
    "run_join_url",
    "run_stream_url",
    "thread_uploads_url",
]


def run_stream_url(thread_id: str) -> str:
    return RUN_STREAM_PATH.format(thread_id=thread_id)


def run_join_url(thread_id: str, run_id: str) -> str:
    return RUN_JOIN_PATH.format(thread_id=thread_id, run_id=run_id)


def run_stream_existing_url(thread_id: str, run_id: str) -> str:
    return RUN_STREAM_EXISTING_PATH.format(thread_id=thread_id, run_id=run_id)


def thread_uploads_url(thread_id: str) -> str:
    return THREAD_UPLOADS_PATH.format(thread_id=thread_id)


class TaskMode(StrEnum):
    """EWCP task modes (kernel ontology: general lane vs governed outcome)."""

    GENERAL = "general"
    GOVERNED = "governed"


class ExecutionRunStatus(StrEnum):
    """Launcher-level lifecycle projection in the ExecutionRunMap.

    `PENDING_INTERRUPT` is the load-bearing durability semantics: a run that
    returned `success` (or `interrupted`) while its thread still awaits human
    input is NOT complete — it must not be reported, sealed, or verified as
    done.
    """

    LAUNCHING = "launching"
    RUNNING = "running"
    PENDING_INTERRUPT = "pending_interrupt"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


# Map statuses that refresh() no longer re-polls: the run is dead AND cannot
# leave pending input behind. COMPLETED / INTERRUPTED are re-verified against
# thread state on refresh (a `success` row written before an interrupt lands
# must still flip to pending_interrupt).
_REFRESH_FINAL = frozenset(
    {
        ExecutionRunStatus.FAILED,
        ExecutionRunStatus.TIMEOUT,
    }
)

# Run statuses for which a thread-state check decides pending vs terminal:
# only a *ended* run can leave pending input behind.
_ENDED_RUN_STATUSES = frozenset({"success", "interrupted"})


# Upstream RunStatus (deerflow/runtime/runs/schemas.py) -> projection.
def project_run_status(run_status: str, *, pending_interrupt: bool = False) -> ExecutionRunStatus:
    """Map an upstream AgentRun status to the launcher lifecycle.

    `pending_interrupt` is the thread-state fact (interrupts/next present);
    a successful run with pending input projects to PENDING_INTERRUPT, never
    COMPLETED.
    """
    match run_status:
        case "pending" | "running":
            return ExecutionRunStatus.RUNNING
        case "success":
            return ExecutionRunStatus.PENDING_INTERRUPT if pending_interrupt else ExecutionRunStatus.COMPLETED
        case "interrupted":
            return ExecutionRunStatus.PENDING_INTERRUPT if pending_interrupt else ExecutionRunStatus.INTERRUPTED
        case "timeout":
            return ExecutionRunStatus.TIMEOUT
        case _:
            # error and any unknown upstream status are not completions.
            return ExecutionRunStatus.FAILED


def detect_pending_interrupt(thread_state: dict | None) -> bool:
    """A thread awaits input when its state carries interrupts or pending
    `next` nodes after the run ended."""
    if not isinstance(thread_state, dict):
        return False
    return bool(thread_state.get("interrupts")) or bool(thread_state.get("next"))


class LaunchConflict(Exception):
    """Same (created_by, idempotency_key) replayed with a different intent —
    the caller must choose a fresh key."""


class FilesWithoutUploader(Exception):
    """files= were passed but no ThreadUploads was bound to the launcher."""


class UploadsError(Exception):
    """The uploads route reported failure or a non-2xx response."""


# ---------------------------------------------------------------------------
# Uploads — the file contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilePayload:
    """A file body to push through POST /api/threads/{id}/uploads."""

    filename: str
    body: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class UploadedFileRef:
    """Server-side file identity, mapped into `additional_kwargs.files`.

    `path` is the server-assigned location returned by the uploads route; it
    is informational in the message contract (UploadsMiddleware re-derives the
    sandbox path from the basename and validates it) — never trusted as
    authority.
    """

    filename: str
    size: int
    path: str | None = None
    status: str = "uploaded"

    def as_input_file(self) -> dict[str, Any]:
        """The upstream FileInMessage shape (frontend FileInMessage +
        UploadsMiddleware: basename-only filename, size, optional path,
        status "uploaded")."""
        ref: dict[str, Any] = {"filename": self.filename, "size": self.size, "status": self.status}
        if self.path is not None:
            ref["path"] = self.path
        return ref


@runtime_checkable
class ThreadUploads(Protocol):
    """Uploads seam: push file bodies onto a thread, get message refs back.

    The bound AgentRuns surface deliberately has no upload capability; uploads
    are an HTTP concern on the caller's auth. `HttpThreadUploads` is the
    production impl (caller binds an httpx client carrying the request's own
    credentials); tests substitute fakes.
    """

    async def upload(self, *, thread_id: str, files: Sequence[FilePayload]) -> list[UploadedFileRef]: ...


class HttpThreadUploads:
    """POST /api/threads/{id}/uploads — multipart `files`, UploadResponse in.

    The httpx client (and therefore its auth material) is owned by the caller;
    this class never inspects, stores, or renders headers.
    """

    def __init__(self, client: httpx.AsyncClient, *, route: str = THREAD_UPLOADS_PATH) -> None:
        self._client = client
        self._route = route

    def __repr__(self) -> str:  # no client/auth material in reprs
        return f"HttpThreadUploads(route={self._route!r})"

    async def upload(self, *, thread_id: str, files: Sequence[FilePayload]) -> list[UploadedFileRef]:
        if not files:
            return []
        multipart = [(f.filename, f.body, f.content_type) for f in files]
        # httpx errors (status/transport) carry the request line, never headers
        # — safe to propagate.
        response = await self._client.post(
            self._route.format(thread_id=thread_id),
            files=[("files", item) for item in multipart],
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", False):
            raise UploadsError(f"uploads route reported failure: {payload.get('message') or 'unknown'}")
        skipped = payload.get("skipped_files") or []
        if skipped:
            raise UploadsError(f"uploads route skipped files: {skipped}")
        return [
            UploadedFileRef(
                filename=item["filename"],
                size=int(item.get("size") or 0),
                path=item.get("path"),
            )
            for item in payload.get("files", [])
        ]


def build_run_input(intent: str, uploaded: Sequence[UploadedFileRef]) -> dict[str, Any]:
    """RunCreateRequest-compatible graph input: the upstream file contract is
    `additional_kwargs.files` on the human message (UploadsMiddleware validates
    basenames and derives `/mnt/user-data/uploads/{filename}` itself)."""
    additional_kwargs: dict[str, Any] = {}
    if uploaded:
        additional_kwargs["files"] = [ref.as_input_file() for ref in uploaded]
    return {
        "messages": [
            {
                "type": "human",
                "content": [{"type": "text", "text": intent}],
                "additional_kwargs": additional_kwargs,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchOutcome:
    record: ExecutionRunRecord
    run: Any  # AgentRun | None — None on a replayed map hit without re-admission
    idempotent_replay: bool


class RunLauncher:
    """Admission-side orchestrator for ExecutionRuns.

    `store` is the durable ExecutionRunMap on the shared product DB.
    `uploader` (optional) is invoked strictly before `start()` when files are
    attached; construct it per request with the caller's auth (e.g.
    HttpThreadUploads over an httpx client forwarding the request headers).
    """

    def __init__(
        self,
        store: ExecutionRunStore,
        *,
        uploader: ThreadUploads | None = None,
        plugin_namespace: str = EWCP_RUNS_NAMESPACE,
    ) -> None:
        self._store = store
        self._uploader = uploader
        self._namespace = plugin_namespace

    async def launch(
        self,
        *,
        agent_runs: AgentRuns,
        intent: str,
        mode: TaskMode | str,
        created_by: str,
        files: Sequence[FilePayload] = (),
        idempotency_key: str | None = None,
        workrun_id: str | None = None,
        context: dict[str, Any] | None = None,
        assistant_id: str = "lead_agent",
    ) -> LaunchOutcome:
        """Create a product thread + run and project it into the map.

        Replay semantics: an existing map row for (created_by,
        idempotency_key) with a run_id is returned untouched — no new thread,
        no new run. A row still at `launching` (run_id NULL — crash between
        insert and admission ack) resumes: `start` is re-issued on the SAME
        thread with the SAME key so the host's global key dedupe replays into
        the same run rather than erroring on a thread mismatch.
        """
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("intent must be a non-empty string")
        try:
            task_mode = TaskMode(mode)
        except ValueError as exc:
            raise ValueError(f"mode must be one of {[m.value for m in TaskMode]!r}") from exc
        if not created_by:
            raise ValueError("created_by is required — map rows are owner-scoped")
        if files and self._uploader is None:
            raise FilesWithoutUploader("files passed but no ThreadUploads is bound")

        key = idempotency_key or f"{EWCP_RUNS_NAMESPACE}:{uuid.uuid4().hex}"
        runs = agent_runs.for_plugin(self._namespace)

        existing = await self._store.get_by_idempotency_key(created_by, key)
        if existing is not None:
            if existing.intent != intent or existing.task_mode != task_mode.value:
                raise LaunchConflict(f"idempotency_key {key!r} already used for a different launch")
            if existing.run_id is not None:
                # Already admitted — replay the stored row, create nothing.
                return LaunchOutcome(record=existing, run=None, idempotent_replay=True)
            # run_id NULL: admission outcome unknown — resume on the stored
            # thread with the same key (host dedupe yields the same run).
            thread_id = existing.thread_id
            uploaded: list[UploadedFileRef] = await self._upload(thread_id, files)
            run = await self._admit(runs, existing.execution_run_id, thread_id, intent, uploaded, context, key)
            status = await self._resolve_status(runs, thread_id, run)
            await self._store.update_admission(existing.execution_run_id, run_id=run.run_id, status=status.value)
            record = await self._store.get(existing.execution_run_id)
            return LaunchOutcome(record=record, run=run, idempotent_replay=False)

        thread_id = await runs.create_thread(
            assistant_id=assistant_id,
            metadata={"title": intent[:60]},
        )
        uploaded = await self._upload(thread_id, files)

        # Persist BEFORE admission: this row owns the pending outcome; the key
        # + thread_id pair is what makes a later retry converge instead of
        # double-launching.
        record = await self._store.insert(
            ExecutionRunRecord.new(
                thread_id=thread_id,
                task_mode=task_mode.value,
                status=ExecutionRunStatus.LAUNCHING.value,
                idempotency_key=key,
                created_by=created_by,
                intent=intent,
                workrun_id=workrun_id,
            )
        )
        run = await self._admit(runs, record.execution_run_id, thread_id, intent, uploaded, context, key)
        status = await self._resolve_status(runs, thread_id, run)
        await self._store.update_admission(record.execution_run_id, run_id=run.run_id, status=status.value)
        record = await self._store.get(record.execution_run_id)
        return LaunchOutcome(record=record, run=run, idempotent_replay=False)

    async def refresh(self, *, agent_runs: AgentRuns, record: ExecutionRunRecord) -> ExecutionRunRecord:
        """Reconcile a map row from live state for non-SSE consumers.

        Combines `get()` (run status) with `get_state()` (pending interrupts)
        so `success`-but-interrupted threads land on PENDING_INTERRUPT.
        Rows still `launching` (run never admitted) return unchanged.
        """
        if record.run_id is None or record.status in _REFRESH_FINAL:
            return record
        runs = agent_runs.for_plugin(self._namespace)
        run = await runs.get(thread_id=record.thread_id, run_id=record.run_id)
        status = await self._resolve_status(runs, record.thread_id, run)
        if status.value != record.status:
            await self._store.update_status(record.execution_run_id, status.value)
            refreshed = await self._store.get(record.execution_run_id)
            return refreshed if refreshed is not None else record
        return record

    async def bind_workrun(self, execution_run_id: str, workrun_id: str) -> None:
        await self._store.bind_workrun(execution_run_id, workrun_id)

    # -- internals -----------------------------------------------------------

    async def _resolve_status(self, runs: AgentRuns, thread_id: str, run: AgentRun) -> ExecutionRunStatus:
        """Project the launcher status, consulting thread state when the run
        ended (only an ended run can leave pending input behind — the extra
        `get_state` is skipped for in-flight runs)."""
        pending = False
        if run.status in _ENDED_RUN_STATUSES:
            pending = detect_pending_interrupt(await runs.get_state(thread_id=thread_id))
        return project_run_status(run.status, pending_interrupt=pending)

    async def _upload(self, thread_id: str, files: Sequence[FilePayload]) -> list[UploadedFileRef]:
        if not files:
            return []
        assert self._uploader is not None  # guarded in launch()
        return await self._uploader.upload(thread_id=thread_id, files=files)

    async def _admit(
        self,
        runs: AgentRuns,
        execution_run_id: str,
        thread_id: str,
        intent: str,
        uploaded: Sequence[UploadedFileRef],
        context: dict[str, Any] | None,
        key: str,
    ) -> AgentRun:
        input_ = build_run_input(intent, uploaded)
        try:
            return await runs.start(thread_id=thread_id, input=input_, context=context, idempotency_key=key)
        except Exception as exc:
            from deerflow_extension_api.agent_runs import AgentRunError

            if isinstance(exc, AgentRunError):
                # Admission was definitively rejected (4xx/409) — record FAILED
                # so the row doesn't sit at LAUNCHING forever.
                await self._store.update_status(execution_run_id, ExecutionRunStatus.FAILED.value)
            # Other exceptions: admission outcome unknown — leave the row at
            # LAUNCHING retaining ownership of the pending job; a retry resumes.
            raise
