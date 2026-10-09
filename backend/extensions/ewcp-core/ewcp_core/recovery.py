"""Foreground ExecutionRun recovery (A3 Task 3).

Durability boundary — see docs/vnext/A3_DURABILITY.md:

- FOREGROUND recovery only. The trigger is a NEW authenticated request (the
  user logs back in / reconnects): `resolve_agent_runs(request)` yields a
  request-bound handle, `for_plugin("ewcp.core")` namespaces it, and every
  operation re-runs the host's per-op auth (load_user + token_version +
  permission intersect — app/gateway/extension_agent_runs.py). Between
  requests the extension holds NO credential.
- There is NO background/service recovery in A3: `bind()` accepts only
  SESSION/AUTH_DISABLED auth sources — PATs and internal service identities
  are excluded by the host, so a run whose user never returns has no
  orchestrator. That is an explicit product limitation, not something this
  module papers over.

Reconcile semantics:

- `runs.get(thread_id, run_id)` is the authoritative status for a run id.
- `runs.get_state(thread_id)` is MUTABLE thread state — consulted only to
  flag pending input (interrupts/next) on an ended run; never trusted as a
  terminal snapshot. Ended-run truth = run.status AND no pending state.
- Live event history is observed through the SSE join route
  (`run_join_url`), not via AgentRuns. The map stores ids plus a status
  projection — never event payloads — so a single state read can never be
  mistaken for durable result truth.

Guards:

- dup-start: an open map row on the same thread, or an idempotency key
  already spent on a different intent, raises `OpenRunConflict`. Same key +
  same intent is a replay — `RunLauncher.launch` converges it, no conflict.
- revoke-before-resume: `resume()` reads through the bound handle before
  the mutating call; a revoked/demoted session raises `RecoveryDenied`
  there, before `runs.resume()` is ever issued.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deerflow_extension_api.agent_runs import AgentRunError

from .deliverable_integrity import ClaimedArtifactProbe, assess_deliverable_integrity
from .execution_run_store import ExecutionRunRecord, ExecutionRunStore
from .run_launcher import (
    DEFAULT_RUN_RECURSION_LIMIT,
    ENDED_RUN_STATUSES,
    EWCP_RUNS_NAMESPACE,
    REFRESH_FINAL,
    ExecutionRunStatus,
    detect_pending_interrupt,
    project_run_status,
    run_join_url,
)

if TYPE_CHECKING:
    from deerflow_extension_api.agent_runs import AgentRuns

    from .run_launcher import RunStarter

logger = logging.getLogger(__name__)

__all__ = [
    "OPEN_RUN_STATUSES",
    "ExecutionRunStatus",
    "OpenRunConflict",
    "ReconciledRun",
    "RecoveryDenied",
    "RecoveryReport",
    "ResumeNotPending",
    "RunNotOwned",
    "RunRecovery",
]

#: Map statuses that still admit forward progress — a second start on the
#: same thread while one of these rows exists would be a duplicate.
OPEN_RUN_STATUSES = frozenset(
    {
        ExecutionRunStatus.LAUNCHING,
        ExecutionRunStatus.RUNNING,
        ExecutionRunStatus.PENDING_INTERRUPT,
    }
)


class RecoveryDenied(Exception):
    """The bound handle's per-op auth rejected the caller (session revoked,
    permission lost). Always raised BEFORE the mutating call on resume."""


class RunNotOwned(Exception):
    """The execution_run_id does not exist for this owner — same response for
    unknown ids and other owners' rows, so no existence oracle leaks."""


class ResumeNotPending(Exception):
    """Resume was requested but the thread is not awaiting input — ended run
    without pending state, still-in-flight run, or never-admitted launch."""


class OpenRunConflict(Exception):
    """Dup-start denied: the idempotency key was spent on a different intent,
    or the target thread already has an open run to rejoin instead.

    `record` is the conflicting map row — callers use it to rejoin via
    `RunRecovery.observation_url` or to replay the launch.
    """

    def __init__(self, record: ExecutionRunRecord, reason: str) -> None:
        super().__init__(reason)
        self.record = record


@dataclass(frozen=True)
class ReconciledRun:
    """One map row after reconcile against live truth."""

    record: ExecutionRunRecord
    run_status: str | None  # upstream run.status seen; None when never admitted / unreachable
    pending_interrupt: bool
    changed: bool  # the map projection was updated by this reconcile
    observation_url: str | None  # SSE join path when the run is observable


@dataclass(frozen=True)
class RecoveryReport:
    """Result of a foreground reconcile for one owner."""

    created_by: str
    runs: tuple[ReconciledRun, ...]


class RunRecovery:
    """Foreground reconcile + guarded resume over the ExecutionRunMap.

    Constructed on the shared store; every method takes the request-fresh
    `AgentRuns` handle — the extension never retains a credential between
    requests.
    """

    def __init__(self, store: ExecutionRunStore, *, plugin_namespace: str = EWCP_RUNS_NAMESPACE, recursion_limit: int = DEFAULT_RUN_RECURSION_LIMIT, probe: ClaimedArtifactProbe | None = None) -> None:
        self._store = store
        self._namespace = plugin_namespace
        self._recursion_limit = recursion_limit
        self._probe = probe

    # -- reads ---------------------------------------------------------------

    async def recover(self, *, agent_runs: AgentRuns, created_by: str, limit: int = 100) -> RecoveryReport:
        """Reconcile every map row owned by `created_by` against live truth.

        Call this from an authenticated request handler with the request's
        freshly bound handle — it IS the foreground recovery entry point.
        """
        if not created_by:
            raise ValueError("created_by is required — map rows are owner-scoped")
        runs = agent_runs.for_plugin(self._namespace)
        reconciled = [await self._reconcile(runs, record) for record in await self._store.list_by_owner(created_by, limit=limit)]
        return RecoveryReport(created_by=created_by, runs=tuple(reconciled))

    async def reconcile_one(self, *, agent_runs: AgentRuns, created_by: str, execution_run_id: str) -> ReconciledRun:
        record = await self._owned(created_by, execution_run_id)
        return await self._reconcile(agent_runs.for_plugin(self._namespace), record)

    @staticmethod
    def observation_url(record: ExecutionRunRecord) -> str | None:
        """SSE rejoin path for the run — live event history, never a snapshot.
        None while admission is still in flight (run_id NULL)."""
        if record.run_id is None:
            return None
        return run_join_url(record.thread_id, record.run_id)

    # -- guards --------------------------------------------------------------

    async def assert_can_start(
        self,
        *,
        created_by: str,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        intent: str | None = None,
    ) -> None:
        """Dup-start guard — run before re-admitting work on an existing
        thread or replaying a caller-supplied idempotency key.

        Denies:
          * `idempotency_key` already consumed by this owner for a DIFFERENT
            intent (same key + same intent is a replay — the launcher
            converges it, no conflict);
          * an OPEN map row already on `thread_id` — a second start would be
            a duplicate; the conflicting row is rejoinable via
            `observation_url`.
        """
        if idempotency_key is not None:
            existing = await self._store.get_by_idempotency_key(created_by, idempotency_key)
            if existing is not None and intent is not None and existing.intent != intent:
                raise OpenRunConflict(existing, "idempotency_key already used for a different launch")
        if thread_id is not None:
            for row in await self._store.list_for_thread(thread_id):
                if row.status in OPEN_RUN_STATUSES:
                    raise OpenRunConflict(row, "thread already has an open ExecutionRun — rejoin instead of starting")

    async def resume(
        self,
        *,
        agent_runs: AgentRuns,
        created_by: str,
        execution_run_id: str,
        resume: Any,
        idempotency_key: str | None = None,
        starter: RunStarter | None = None,
    ) -> ExecutionRunRecord:
        """Submit an interrupt response, gated on real pending state.

        Order matters: `get_state`/`get` run through the bound handle FIRST —
        the host re-checks session auth on every op, so a revoked session is
        denied (`RecoveryDenied`) before `runs.resume()` mutates anything.
        A resume is only issued when the run ended AND the thread still
        awaits input; otherwise `ResumeNotPending` (and the map row is
        repaired to the real status on the way out).
        """
        record = await self._owned(created_by, execution_run_id)
        if record.run_id is None:
            raise ResumeNotPending("run was never admitted — replay the launch instead")
        runs = agent_runs.for_plugin(self._namespace)
        try:
            run = await runs.get(thread_id=record.thread_id, run_id=record.run_id)
            # Pending input is only meaningful on an ended run — `next` is
            # non-empty for every in-flight run, so gate the state read the
            # same way the launcher does.
            state = await runs.get_state(thread_id=record.thread_id) if run.status in ENDED_RUN_STATUSES else None
        except AgentRunError as exc:
            raise self._translate(exc) from exc
        pending = detect_pending_interrupt(state)
        if not pending:
            status = project_run_status(run.status, pending_interrupt=False)
            if status.value != record.status:
                await self._store.update_status(record.execution_run_id, status.value)
            raise ResumeNotPending(f"thread is not awaiting input (run status {run.status!r})")
        try:
            if starter is not None:
                # F1: resume admits a NEW run on the same thread — carry the
                # same recursion budget (the bound resume has no config
                # channel either).
                agent_run = await starter.resume(
                    thread_id=record.thread_id,
                    resume=resume,
                    config={"recursion_limit": self._recursion_limit},
                    idempotency_key=idempotency_key,
                )
            else:
                agent_run = await runs.resume(thread_id=record.thread_id, resume=resume, idempotency_key=idempotency_key)
        except AgentRunError as exc:
            raise self._translate(exc) from exc
        # Upstream resume admits a NEW run on the same thread — rebind the
        # projection's run_id when it changes.
        status = project_run_status(agent_run.status)
        if agent_run.run_id != record.run_id:
            await self._store.update_admission(record.execution_run_id, run_id=agent_run.run_id, status=status.value)
        else:
            await self._store.update_status(record.execution_run_id, status.value)
        refreshed = await self._store.get(record.execution_run_id)
        return refreshed if refreshed is not None else record

    # -- internals -----------------------------------------------------------

    async def _owned(self, created_by: str, execution_run_id: str) -> ExecutionRunRecord:
        record = await self._store.get(execution_run_id)
        if record is None or record.created_by != created_by:
            raise RunNotOwned(execution_run_id)
        return record

    async def _reconcile(self, runs: AgentRuns, record: ExecutionRunRecord) -> ReconciledRun:
        if record.run_id is None:
            # Admission never acknowledged — the row still owns a pending
            # launch; replay converges through RunLauncher.launch (same key).
            return ReconciledRun(record=record, run_status=None, pending_interrupt=False, changed=False, observation_url=None)
        if record.status in REFRESH_FINAL:
            # Dead and cannot leave pending input behind — nothing to poll.
            return ReconciledRun(
                record=record,
                run_status=None,
                pending_interrupt=False,
                changed=False,
                observation_url=self.observation_url(record),
            )
        try:
            run = await runs.get(thread_id=record.thread_id, run_id=record.run_id)
            state = await runs.get_state(thread_id=record.thread_id) if run.status in ENDED_RUN_STATUSES else None
        except AgentRunError as exc:
            if exc.status_code == 404:
                # Thread truth: the thread is gone — this run can never
                # resume or produce evidence, so the projection is dead.
                return await self._mark_failed(record)
            raise self._translate(exc) from exc
        pending = detect_pending_interrupt(state)
        status = project_run_status(run.status, pending_interrupt=pending)
        changed = status.value != record.status
        if changed:
            await self._store.update_status(record.execution_run_id, status.value)
            refreshed = await self._store.get(record.execution_run_id)
            if refreshed is not None:
                record = refreshed
        # F2: a completed general-lane run is a self-report — verify the
        # deliverables it claims actually exist before the row renders as
        # clean. Advisory only; the status stays COMPLETED either way.
        if status == ExecutionRunStatus.COMPLETED and record.integrity_flag is None:
            if await assess_deliverable_integrity(store=self._store, record=record, state=state, probe=self._probe) is not None:
                refreshed = await self._store.get(record.execution_run_id)
                if refreshed is not None:
                    record = refreshed
        return ReconciledRun(
            record=record,
            run_status=run.status,
            pending_interrupt=pending,
            changed=changed,
            observation_url=self.observation_url(record),
        )

    async def _mark_failed(self, record: ExecutionRunRecord) -> ReconciledRun:
        if record.status != ExecutionRunStatus.FAILED:
            await self._store.update_status(record.execution_run_id, ExecutionRunStatus.FAILED.value)
            refreshed = await self._store.get(record.execution_run_id)
            if refreshed is not None:
                record = refreshed
        return ReconciledRun(
            record=record,
            run_status=None,
            pending_interrupt=False,
            changed=True,
            observation_url=self.observation_url(record),
        )

    @staticmethod
    def _translate(exc: AgentRunError) -> Exception:
        if exc.status_code == 403:
            return RecoveryDenied(str(exc))
        return exc
