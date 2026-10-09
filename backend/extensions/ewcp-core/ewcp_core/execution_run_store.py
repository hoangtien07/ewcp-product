"""ExecutionRunMap — durable projection of EWCP execution runs on the shared
product database.

Ruling (Task 2): the plan asked to "check `backend/app/gateway/persistence`
conventions". That directory does not exist on `product/vnext`; shared product
persistence is `deerflow.persistence.*` (`packages/harness/deerflow/
persistence/`) and the documented extension convention (migrations AGENTS.md,
"Extension-owned tables") is a private `MetaData` + a declared `table_prefix`
on the `plugins:` record + lifecycle inside `ExtensionService.start()` against
`ExtensionRuntimeDeps.session_factory`. This module follows that convention:
the table lives in the SAME database the product uses (not a private SQLite
file like `examples/deerflow-extension-agent-teams`), owned entirely by this
extension under the `ewcp_` prefix.

The `table_prefix: ewcp_` declaration lives in the operator's `config.yaml`
`plugins:` entry for `ewcp_core` (gitignored, operator-controlled — see
README). Without it, host autogenerate could reflect this table and propose a
drop; the prefix registration fails closed if any host table collides with
`ewcp_*`.

Schema versioning: v0.1 uses `create_all` (idempotent create-if-absent) under
the same Postgres advisory-lock pattern the host bootstrap uses. When the
schema first needs to *change*, an extension-owned alembic chain
(`version_table="ewcp_alembic_version"`) per the migrations AGENTS.md
convention replaces this; starting the chain now would buy revision machinery
for a table that has never existed.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

logger = logging.getLogger(__name__)

TABLE_PREFIX = "ewcp_"

_METADATA = sa.MetaData()

_EXECUTION_RUNS = sa.Table(
    f"{TABLE_PREFIX}execution_runs",
    _METADATA,
    sa.Column("execution_run_id", sa.String(64), primary_key=True),
    sa.Column("thread_id", sa.String(255), nullable=False),
    # run_id is NULL until AgentRuns.start() acknowledges admission.
    sa.Column("run_id", sa.String(255), nullable=True),
    # Governed-mode kernel link; NULL for general-lane runs.
    sa.Column("workrun_id", sa.String(255), nullable=True),
    sa.Column("task_mode", sa.String(32), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("intent", sa.Text, nullable=False),
    # Always populated (auto-generated when the caller supplies none) so the
    # owner-scoped unique index is unconditional on both backends.
    sa.Column("idempotency_key", sa.String(255), nullable=False),
    sa.Column("created_by", sa.String(255), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    # F2 fabrication guardrail — deliverable-existence verdict for
    # general-lane completions (see deliverable_integrity.py). NULL until
    # assessed; 'verified' | 'no_claims' | 'claimed_artifacts_missing:[...]'.
    sa.Column("integrity_flag", sa.Text, nullable=True),
    sa.UniqueConstraint("created_by", "idempotency_key", name=f"uq_{TABLE_PREFIX}execution_runs_owner_key"),
    sa.Index(f"ix_{TABLE_PREFIX}execution_runs_owner", "created_by"),
    sa.Index(f"ix_{TABLE_PREFIX}execution_runs_thread", "thread_id"),
)

# pg advisory lock key for schema creation (same pattern as the host's
# bootstrap_schema; arbitrary stable int namespaced to this extension).
_SCHEMA_LOCK_KEY = 0x65_77_63_70  # "ewcp"


def _bootstrap(sync_session: Any) -> None:
    """create_all + additive column brings existing databases up to the
    current shape — the cheap `create_all`-plus-ALTER path documented on the
    table (a real alembic chain is only warranted once the schema *changes*
    incompatibly)."""
    connection = sync_session.connection()
    _METADATA.create_all(connection)
    existing = {c["name"] for c in sa.inspect(connection).get_columns(_EXECUTION_RUNS.name)}
    for column in _EXECUTION_RUNS.columns:
        if column.name not in existing:
            connection.execute(sa.DDL(f"ALTER TABLE {_EXECUTION_RUNS.name} ADD COLUMN {column.compile(dialect=connection.dialect)}"))


@dataclass
class ExecutionRunRecord:
    """The ExecutionRunMap projection row.

    `workrun_id` is the nullable kernel-workrun link for governed tasks;
    `run_id` is the runtime run reference, NULL while admission is in flight
    (a crashed launch leaves a LAUNCHING row that owns its pending outcome —
    retry replays `start` on the same thread+key).
    """

    execution_run_id: str
    thread_id: str
    run_id: str | None
    workrun_id: str | None
    task_mode: str
    status: str
    intent: str
    idempotency_key: str
    created_by: str
    created_at: float
    updated_at: float
    # None until deliverable integrity is assessed (F2); defaults so rows
    # read from a pre-column database still construct.
    integrity_flag: str | None = None

    @classmethod
    def new(
        cls,
        *,
        thread_id: str,
        task_mode: str,
        status: str,
        idempotency_key: str,
        created_by: str,
        intent: str,
        run_id: str | None = None,
        workrun_id: str | None = None,
        execution_run_id: str | None = None,
    ) -> ExecutionRunRecord:
        now = time.time()
        return cls(
            execution_run_id=execution_run_id or f"er_{uuid.uuid4().hex}",
            thread_id=thread_id,
            run_id=run_id,
            workrun_id=workrun_id,
            task_mode=task_mode,
            status=status,
            intent=intent,
            idempotency_key=idempotency_key,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )


def _row_to_record(row: sa.engine.Row) -> ExecutionRunRecord:
    return ExecutionRunRecord(**dict(row._mapping))


class ExecutionRunStore:
    """CRUD + schema bootstrap for `ewcp_execution_runs`.

    `session_factory` is the shared `async_sessionmaker` handed to the
    extension via `ExtensionRuntimeDeps.session_factory` — the same engine and
    database the host uses.
    """

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    # -- schema --------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Create the extension's tables if absent.

        Postgres gets an advisory lock so concurrent Gateway instances don't
        race `create_all`; SQLite serializes writers itself.
        """
        async with self._sf() as session:
            dialect = (await session.connection()).dialect.name
            if dialect == "postgresql":
                await self._with_pg_advisory_lock(session)
            else:
                await session.run_sync(_bootstrap)
            await session.commit()

    async def _with_pg_advisory_lock(self, session: Any) -> None:
        # Poll try-lock, never a blocking pg_advisory_lock: a blocked acquire
        # would hold a pooled connection past statement/command timeouts.
        for _ in range(50):
            got = (await session.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": _SCHEMA_LOCK_KEY})).scalar()
            if got:
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("timed out acquiring ewcp_ schema advisory lock")
        try:
            await session.run_sync(_bootstrap)
        finally:
            await session.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": _SCHEMA_LOCK_KEY})

    # -- writes --------------------------------------------------------------

    async def insert(self, record: ExecutionRunRecord) -> ExecutionRunRecord:
        async with self._sf() as session:
            await session.execute(
                _EXECUTION_RUNS.insert().values(
                    execution_run_id=record.execution_run_id,
                    thread_id=record.thread_id,
                    run_id=record.run_id,
                    workrun_id=record.workrun_id,
                    task_mode=record.task_mode,
                    status=record.status,
                    intent=record.intent,
                    idempotency_key=record.idempotency_key,
                    created_by=record.created_by,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return record

    async def update_admission(self, execution_run_id: str, *, run_id: str, status: str) -> None:
        async with self._sf() as session:
            await session.execute(sa.update(_EXECUTION_RUNS).where(_EXECUTION_RUNS.c.execution_run_id == execution_run_id).values(run_id=run_id, status=status, updated_at=time.time()))
            await session.commit()

    async def update_status(self, execution_run_id: str, status: str) -> None:
        async with self._sf() as session:
            await session.execute(sa.update(_EXECUTION_RUNS).where(_EXECUTION_RUNS.c.execution_run_id == execution_run_id).values(status=status, updated_at=time.time()))
            await session.commit()

    async def update_integrity_flag(self, execution_run_id: str, flag: str) -> None:
        """Persist the deliverable-integrity verdict once — the WHERE NULL
        guard makes concurrent assessments idempotent: the first persisted
        flag wins, later writes are no-ops."""
        async with self._sf() as session:
            await session.execute(
                sa.update(_EXECUTION_RUNS)
                .where(
                    _EXECUTION_RUNS.c.execution_run_id == execution_run_id,
                    _EXECUTION_RUNS.c.integrity_flag.is_(None),
                )
                .values(integrity_flag=flag, updated_at=time.time())
            )
            await session.commit()

    async def bind_workrun(self, execution_run_id: str, workrun_id: str) -> None:
        """Attach the kernel workrun once governed admission returns it."""
        async with self._sf() as session:
            await session.execute(sa.update(_EXECUTION_RUNS).where(_EXECUTION_RUNS.c.execution_run_id == execution_run_id).values(workrun_id=workrun_id, updated_at=time.time()))
            await session.commit()

    # -- reads ---------------------------------------------------------------

    async def get(self, execution_run_id: str) -> ExecutionRunRecord | None:
        async with self._sf() as session:
            row = (await session.execute(sa.select(_EXECUTION_RUNS).where(_EXECUTION_RUNS.c.execution_run_id == execution_run_id))).first()
        return _row_to_record(row) if row else None

    async def get_by_idempotency_key(self, created_by: str, key: str) -> ExecutionRunRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    sa.select(_EXECUTION_RUNS).where(
                        _EXECUTION_RUNS.c.created_by == created_by,
                        _EXECUTION_RUNS.c.idempotency_key == key,
                    )
                )
            ).first()
        return _row_to_record(row) if row else None

    async def list_by_owner(self, created_by: str, *, limit: int = 50) -> list[ExecutionRunRecord]:
        async with self._sf() as session:
            rows = (await session.execute(sa.select(_EXECUTION_RUNS).where(_EXECUTION_RUNS.c.created_by == created_by).order_by(_EXECUTION_RUNS.c.created_at.desc()).limit(limit))).all()
        return [_row_to_record(r) for r in rows]

    async def list_for_thread(self, thread_id: str, *, limit: int = 50) -> list[ExecutionRunRecord]:
        """All map rows on one thread, newest first — the dup-start guard's
        open-run check filters these by status at the call site so the open
        set stays owned by the lifecycle code, not duplicated in SQL."""
        async with self._sf() as session:
            rows = (await session.execute(sa.select(_EXECUTION_RUNS).where(_EXECUTION_RUNS.c.thread_id == thread_id).order_by(_EXECUTION_RUNS.c.created_at.desc()).limit(limit))).all()
        return [_row_to_record(r) for r in rows]


__all__ = [
    "TABLE_PREFIX",
    "ExecutionRunRecord",
    "ExecutionRunStore",
]
