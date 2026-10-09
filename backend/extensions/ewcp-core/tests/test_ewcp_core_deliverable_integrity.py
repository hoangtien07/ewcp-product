"""F2 fabrication guardrail — deliverable-existence integrity for the
general (pane) lane.

Contracts pinned (see docs/vnext/F2_FABRICATION_GUARD.md + GP01_REEVAL F2):

  - The failure mode is the model's UNVERIFIABLE SELF-REPORT: a general-lane
    run lands `completed` while the deliverables it claims were never written
    (or were emptied) in the sandbox outputs tree. `present_files` only
    normalizes the claim into `values.artifacts`; it never stats the file.
  - The guardrail is POST-RUN and ADVISORY: on reconcile/refresh of a
    completed general-lane run, the extension resolves every claimed
    `/mnt/user-data/**` path against the host sandbox dirs and persists an
    `integrity_flag` on the ExecutionRunMap row. Missing/empty claims surface
    as `claimed_artifacts_missing:[...]` — a UI honesty marker, never a hard
    failure (the pane lane is not governed).
  - Once assessed the flag is durable: claims are completion-time truth, so
    later reconciles never recompute or clear it.
  - Governed and invoke rows are out of scope: governed runs get the kernel
    manifest/seal verify path; invoke rows are capability calls, not
    deliverable runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ewcp_core.deliverable_integrity import (
    FLAG_CLAIMED_MISSING_PREFIX,
    INTEGRITY_NO_CLAIMS,
    INTEGRITY_VERIFIED,
    HostOutputsProbe,
    assess_deliverable_integrity,
    extract_claimed_artifacts,
)
from ewcp_core.execution_run_store import ExecutionRunRecord, ExecutionRunStore
from ewcp_core.recovery import ExecutionRunStatus, RunRecovery
from ewcp_core.run_launcher import RunLauncher

# ---------------------------------------------------------------------------
# extract_claimed_artifacts — the claim channel
# ---------------------------------------------------------------------------


def test_extract_claims_from_artifacts_channel() -> None:
    state = {
        "values": {
            "artifacts": [
                "/mnt/user-data/outputs/report.csv",
                "/mnt/user-data/outputs/world.geojson",
            ],
            "messages": [],
        }
    }
    assert extract_claimed_artifacts(state) == [
        "/mnt/user-data/outputs/report.csv",
        "/mnt/user-data/outputs/world.geojson",
    ]


def test_extract_claims_from_final_message_mentions() -> None:
    state = {
        "values": {
            "messages": [
                {"type": "human", "content": "fetch me a csv"},
                {
                    "type": "ai",
                    "content": "Done — wrote `/mnt/user-data/outputs/output.csv` (12 rows).",
                },
            ],
        }
    }
    assert extract_claimed_artifacts(state) == ["/mnt/user-data/outputs/output.csv"]


def test_extract_claims_ignores_non_sandbox_paths_and_tools() -> None:
    state = {
        "values": {
            "artifacts": ["report.csv", "https://example.com/x.csv"],  # not virtual paths
            "messages": [
                {
                    "type": "tool",
                    "content": "created /mnt/user-data/outputs/from_tool.csv",
                },
                {
                    "type": "ai",
                    "content": "kept scratch in /tmp/x.bin; deliverable at /mnt/user-data/outputs/real.csv.",
                },
            ],
        }
    }
    assert extract_claimed_artifacts(state) == ["/mnt/user-data/outputs/real.csv"]


def test_extract_claims_dedupes_across_channels() -> None:
    state = {
        "values": {
            "artifacts": ["/mnt/user-data/outputs/a.csv"],
            "messages": [
                {
                    "type": "ai",
                    "content": "Here is /mnt/user-data/outputs/a.csv and /mnt/user-data/outputs/b.md",
                }
            ],
        }
    }
    assert extract_claimed_artifacts(state) == [
        "/mnt/user-data/outputs/a.csv",
        "/mnt/user-data/outputs/b.md",
    ]


def test_extract_claims_handles_object_messages_and_empty_state() -> None:
    class _Msg:
        type = "ai"
        content = [{"type": "text", "text": "saved /mnt/user-data/outputs/obj.csv"}]

    assert extract_claimed_artifacts({"values": {"messages": [_Msg()]}}) == ["/mnt/user-data/outputs/obj.csv"]
    assert extract_claimed_artifacts(None) == []
    assert extract_claimed_artifacts({}) == []


# ---------------------------------------------------------------------------
# HostOutputsProbe — existence on the host sandbox dirs
# ---------------------------------------------------------------------------


def _write_outputs(paths, thread_id: str, user_id: str | None, name: str, body: bytes) -> None:
    out = paths.sandbox_outputs_dir(thread_id, user_id=user_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_bytes(body)


def test_probe_flags_missing_and_empty(tmp_path: Path) -> None:
    class _Paths:
        base_dir = tmp_path

        def thread_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
            if user_id is not None:
                return tmp_path / "users" / user_id / "threads" / thread_id
            return tmp_path / "threads" / thread_id

        def sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
            return self.thread_dir(thread_id, user_id=user_id) / "user-data"

        def sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
            return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "outputs"

        def resolve_virtual_path(self, thread_id: str, virtual_path: str, *, user_id: str | None = None) -> Path:
            stripped = virtual_path.lstrip("/")
            prefix = "mnt/user-data"
            if stripped != prefix and not stripped.startswith(prefix + "/"):
                raise ValueError("bad prefix")
            relative = stripped[len(prefix) :].lstrip("/")
            base = self.sandbox_user_data_dir(thread_id, user_id=user_id).resolve()
            actual = (base / relative).resolve()
            try:
                actual.relative_to(base)
            except ValueError:
                raise ValueError("traversal") from None
            return actual

    paths = _Paths()
    _write_outputs(paths, "t1", "u-1", "real.csv", b"x" * 10)
    _write_outputs(paths, "t1", "u-1", "empty.csv", b"")

    probe = HostOutputsProbe(paths_getter=lambda: paths)
    import asyncio

    missing = asyncio.run(
        probe.missing(
            thread_id="t1",
            user_id="u-1",
            claims=[
                "/mnt/user-data/outputs/real.csv",
                "/mnt/user-data/outputs/empty.csv",
                "/mnt/user-data/outputs/never_written.csv",
                "/mnt/user-data/outputs/../workspace/escape.txt",
            ],
        )
    )
    assert missing == [
        "outputs/empty.csv",
        "outputs/never_written.csv",
        "outputs/../workspace/escape.txt",
    ]


# ---------------------------------------------------------------------------
# assess_deliverable_integrity — persisted flag semantics
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store():
    engine = create_async_engine("sqlite+aiosqlite://")
    sf = async_sessionmaker(engine, expire_on_commit=False)
    s = ExecutionRunStore(sf)
    await s.ensure_schema()
    yield s
    await engine.dispose()


async def _seed(store, **overrides) -> ExecutionRunRecord:
    params: dict[str, Any] = {
        "thread_id": "thread-1",
        "task_mode": "general",
        "status": ExecutionRunStatus.COMPLETED,
        "idempotency_key": "k-1",
        "created_by": "u-1",
        "intent": "đối soát",
        "run_id": "run-1",
    }
    params.update(overrides)
    return await store.insert(ExecutionRunRecord.new(**params))


class _FixedProbe:
    def __init__(self, missing: list[str]) -> None:
        self._missing = missing
        self.calls = 0

    async def missing(self, *, thread_id: str, user_id: str, claims) -> list[str]:
        self.calls += 1
        return list(self._missing)


@pytest.mark.asyncio
async def test_assess_marks_missing_claims(store) -> None:
    record = await _seed(store)
    state = {"values": {"artifacts": ["/mnt/user-data/outputs/output.csv"]}}
    flag = await assess_deliverable_integrity(store=store, record=record, state=state, probe=_FixedProbe(["outputs/output.csv"]))
    assert flag is not None
    assert flag.startswith(FLAG_CLAIMED_MISSING_PREFIX + ":")
    assert json.loads(flag.split(":", 1)[1]) == ["outputs/output.csv"]
    refreshed = await store.get(record.execution_run_id)
    assert refreshed is not None
    assert refreshed.integrity_flag == flag


@pytest.mark.asyncio
async def test_assess_verified_when_all_claims_exist(store) -> None:
    record = await _seed(store)
    state = {"values": {"artifacts": ["/mnt/user-data/outputs/real.csv"]}}
    flag = await assess_deliverable_integrity(store=store, record=record, state=state, probe=_FixedProbe([]))
    assert flag == INTEGRITY_VERIFIED


@pytest.mark.asyncio
async def test_assess_no_claims(store) -> None:
    record = await _seed(store)
    flag = await assess_deliverable_integrity(store=store, record=record, state={"values": {}}, probe=_FixedProbe([]))
    assert flag == INTEGRITY_NO_CLAIMS


@pytest.mark.asyncio
async def test_assess_is_sticky_and_scoped(store) -> None:
    probe = _FixedProbe(["outputs/output.csv"])
    flagged = await _seed(store, idempotency_key="k-flag")
    state = {"values": {"artifacts": ["/mnt/user-data/outputs/output.csv"]}}
    await assess_deliverable_integrity(store=store, record=flagged, state=state, probe=probe)
    assert probe.calls == 1
    # Second assessment must not recompute — completion-time truth is durable.
    second = await assess_deliverable_integrity(store=store, record=await store.get(flagged.execution_run_id), state=state, probe=probe)
    stored = await store.get(flagged.execution_run_id)
    assert stored is not None and second == stored.integrity_flag
    assert probe.calls == 1

    # Governed rows are out of scope — kernel verify owns them.
    governed = await _seed(store, idempotency_key="k-gov", task_mode="governed")
    assert await assess_deliverable_integrity(store=store, record=governed, state=state, probe=probe) is None
    # Still-running rows are out of scope — claims may still be landing.
    running = await _seed(store, idempotency_key="k-run", status=ExecutionRunStatus.RUNNING)
    assert await assess_deliverable_integrity(store=store, record=running, state=state, probe=probe) is None


# ---------------------------------------------------------------------------
# Reconcile/refresh integration — the completion seam
# ---------------------------------------------------------------------------


class _FakeRun:
    def __init__(self, thread_id: str, run_id: str, status: str) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.status = status


class _FakeRuns:
    def __init__(self, *, status: str = "success", state: dict | None = None) -> None:
        self.status = status
        self.state = state or {}

    def for_plugin(self, namespace: str) -> _FakeRuns:
        return self

    async def get(self, *, thread_id, run_id):
        return _FakeRun(thread_id, run_id, self.status)

    async def get_state(self, *, thread_id):
        return self.state


@pytest_asyncio.fixture
async def seeded_running(store):
    return await _seed(
        store,
        status=ExecutionRunStatus.RUNNING,
        thread_id="thread-rec",
        run_id="run-rec",
    )


@pytest.mark.asyncio
async def test_reconcile_flags_fabricated_deliverable(store, seeded_running) -> None:
    """A completed general-lane run whose claimed output.csv was never
    written lands with `claimed_artifacts_missing`, not a clean completion."""
    recovery = RunRecovery(store, probe=_FixedProbe(["outputs/output.csv"]))
    runs = _FakeRuns(state={"values": {"artifacts": ["/mnt/user-data/outputs/output.csv"]}})
    item = await recovery.reconcile_one(
        agent_runs=runs,
        created_by="u-1",
        execution_run_id=seeded_running.execution_run_id,
    )
    record = item.record
    assert record.status == ExecutionRunStatus.COMPLETED
    assert record.integrity_flag is not None
    assert record.integrity_flag.startswith(FLAG_CLAIMED_MISSING_PREFIX)


@pytest.mark.asyncio
async def test_reconcile_verified_run_has_clean_flag(store, seeded_running) -> None:
    recovery = RunRecovery(store, probe=_FixedProbe([]))
    runs = _FakeRuns(state={"values": {"artifacts": ["/mnt/user-data/outputs/real.csv"]}})
    item = await recovery.reconcile_one(
        agent_runs=runs,
        created_by="u-1",
        execution_run_id=seeded_running.execution_run_id,
    )
    assert item.record.integrity_flag == INTEGRITY_VERIFIED


@pytest.mark.asyncio
async def test_reconcile_no_claims(store, seeded_running) -> None:
    recovery = RunRecovery(store, probe=_FixedProbe([]))
    runs = _FakeRuns(state={"values": {}})
    item = await recovery.reconcile_one(
        agent_runs=runs,
        created_by="u-1",
        execution_run_id=seeded_running.execution_run_id,
    )
    assert item.record.integrity_flag == INTEGRITY_NO_CLAIMS


@pytest.mark.asyncio
async def test_launcher_refresh_assesses_newly_completed_run(store, seeded_running) -> None:
    launcher = RunLauncher(store, probe=_FixedProbe(["outputs/never.csv"]))
    runs = _FakeRuns(state={"values": {"artifacts": ["/mnt/user-data/outputs/never.csv"]}})
    refreshed = await launcher.refresh(agent_runs=runs, record=seeded_running)
    assert refreshed.status == ExecutionRunStatus.COMPLETED
    assert refreshed.integrity_flag.startswith(FLAG_CLAIMED_MISSING_PREFIX)
