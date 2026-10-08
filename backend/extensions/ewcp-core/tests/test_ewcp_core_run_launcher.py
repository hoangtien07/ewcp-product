"""run_launcher + execution_run_store contract tests — mocked AgentRuns,
real SQLAlchemy async store on in-memory SQLite (same shared-persistence API
shape as `ExtensionRuntimeDeps.session_factory`).

Contracts pinned:
  - launch(intent, mode) -> create_thread -> (uploads BEFORE start) -> start
    with input={messages:[human, additional_kwargs.files]} + idempotency_key
  - durable dedupe: replay of the same (created_by, idempotency_key, intent)
    returns the stored map row WITHOUT a new thread/run; same key + different
    intent raises LaunchConflict.
  - pending interrupt != complete: a `success` run whose thread still has
    pending interrupts projects to pending_interrupt, not completed.
  - uploads go through the dedicated route POST /api/threads/{id}/uploads
    (HttpThreadUploads) and map into input per the upstream FileInMessage
    contract ({filename, size, path?, status:"uploaded"} — basename only,
    path supplied by the server, UploadsMiddleware derives the sandbox path).
"""

from __future__ import annotations

import re

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ewcp_core.execution_run_store import (
    ExecutionRunRecord,
    ExecutionRunStore,
)
from ewcp_core.run_launcher import (
    EWCP_RUNS_NAMESPACE,
    RUN_JOIN_PATH,
    RUN_STREAM_EXISTING_PATH,
    RUN_STREAM_PATH,
    TABLE_PREFIX,
    THREAD_UPLOADS_PATH,
    ExecutionRunStatus,
    FilePayload,
    FilesWithoutUploader,
    HttpThreadUploads,
    LaunchConflict,
    RunLauncher,
    TaskMode,
    ThreadUploads,
    UploadedFileRef,
    run_join_url,
    thread_uploads_url,
)

# ---------------------------------------------------------------------------
# Fakes — mirror deerflow_extension_api.agent_runs.AgentRuns surface.
# ---------------------------------------------------------------------------


class FakeAgentRun:
    """Mirrors AgentRun{thread_id, run_id, status, assistant_id, stop_reason}."""

    def __init__(self, thread_id: str, run_id: str, status: str = "running") -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.status = status
        self.assistant_id = "lead_agent"
        self.stop_reason = None


class FakeAgentRuns:
    """Bound-handle stand-in: records calls, hands out canned runs."""

    def __init__(self, *, run_status: str = "running") -> None:
        self.namespaces: list[str] = []
        self.created_threads: list[dict] = []
        self.started: list[dict] = []
        self.got: list[tuple[str, str]] = []
        self.state_calls: list[str] = []
        self.run_status = run_status
        self.thread_states: dict[str, dict] = {}
        self._n = 0

    def for_plugin(self, namespace: str) -> FakeAgentRuns:
        self.namespaces.append(namespace)
        return self

    async def create_thread(self, *, assistant_id="lead_agent", thread_id=None, metadata=None) -> str:
        self._n += 1
        self.created_threads.append({"assistant_id": assistant_id, "metadata": metadata})
        return f"thread-{self._n}"

    async def start(self, *, thread_id, input=None, resume=None, context=None, idempotency_key=None) -> FakeAgentRun:
        self.started.append(
            {
                "thread_id": thread_id,
                "input": input,
                "context": context,
                "idempotency_key": idempotency_key,
            }
        )
        return FakeAgentRun(thread_id, f"run-{len(self.started)}", status=self.run_status)

    async def get(self, *, thread_id, run_id) -> FakeAgentRun:
        self.got.append((thread_id, run_id))
        return FakeAgentRun(thread_id, run_id, status=self.run_status)

    async def get_state(self, *, thread_id) -> dict:
        self.state_calls.append(thread_id)
        return self.thread_states.get(thread_id, {})


class FakeUploader(ThreadUploads):
    """Records upload calls; returns fixed refs."""

    def __init__(self, refs: list[UploadedFileRef] | None = None) -> None:
        self.calls: list[tuple[str, tuple[FilePayload, ...]]] = []
        self.refs = refs or []

    async def upload(self, *, thread_id: str, files) -> list[UploadedFileRef]:
        self.calls.append((thread_id, tuple(files)))
        return list(self.refs)


# ---------------------------------------------------------------------------
# Fixtures
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
def launcher(store) -> RunLauncher:
    return RunLauncher(store)


def _file_refs() -> list[UploadedFileRef]:
    return [
        UploadedFileRef(filename="invoices.pdf", size=1234, path="/mnt/user-data/uploads/invoices.pdf"),
        UploadedFileRef(filename="books.csv", size=56),
    ]


# ---------------------------------------------------------------------------
# launch — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_creates_thread_then_start_with_intent(launcher, store) -> None:
    runs = FakeAgentRuns()
    out = await launcher.launch(agent_runs=runs, intent="đối soát hóa đơn", mode=TaskMode.GENERAL, created_by="u-1")

    assert runs.namespaces == [EWCP_RUNS_NAMESPACE]
    assert len(runs.created_threads) == 1
    assert len(runs.started) == 1
    start = runs.started[0]
    assert start["thread_id"] == out.record.thread_id
    assert out.record.run_id == "run-1"
    assert out.record.status == ExecutionRunStatus.RUNNING
    assert out.idempotent_replay is False

    # input follows the upstream wire contract: {messages: [human]}
    msgs = start["input"]["messages"]
    assert msgs[0]["type"] == "human"
    assert msgs[0]["content"][0]["type"] == "text"
    assert msgs[0]["content"][0]["text"] == "đối soát hóa đơn"


@pytest.mark.asyncio
async def test_launch_records_map_projection(launcher, store) -> None:
    runs = FakeAgentRuns()
    out = await launcher.launch(
        agent_runs=runs,
        intent="tóm tắt",
        mode=TaskMode.GOVERNED,
        created_by="u-7",
        idempotency_key="req-abc",
        workrun_id="wr-9",
    )

    rec = out.record
    assert rec.task_mode == "governed"
    assert rec.workrun_id == "wr-9"
    assert rec.idempotency_key == "req-abc"
    assert rec.created_by == "u-7"
    assert rec.thread_id and rec.run_id
    assert rec.intent == "tóm tắt"

    fetched = await store.get(rec.execution_run_id)
    assert fetched == rec


@pytest.mark.asyncio
async def test_launch_generates_key_when_caller_omits(launcher) -> None:
    runs = FakeAgentRuns()
    out = await launcher.launch(agent_runs=runs, intent="x", mode=TaskMode.GENERAL, created_by="u-1")
    assert out.record.idempotency_key
    assert runs.started[0]["idempotency_key"] == out.record.idempotency_key


@pytest.mark.asyncio
async def test_launch_rejects_empty_intent_and_mode(launcher) -> None:
    runs = FakeAgentRuns()
    with pytest.raises(ValueError):
        await launcher.launch(agent_runs=runs, intent="", mode=TaskMode.GENERAL, created_by="u-1")
    with pytest.raises(ValueError):
        await launcher.launch(agent_runs=runs, intent="  ", mode=TaskMode.GENERAL, created_by="u-1")
    with pytest.raises(ValueError):
        await launcher.launch(agent_runs=runs, intent="x", mode="bogus", created_by="u-1")
    with pytest.raises(ValueError):
        await launcher.launch(agent_runs=runs, intent="x", mode=TaskMode.GENERAL, created_by="")
    assert runs.started == []


# ---------------------------------------------------------------------------
# idempotent replay — DoD: replay does NOT create a new run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_returns_stored_record_without_new_run(launcher) -> None:
    runs = FakeAgentRuns()
    first = await launcher.launch(agent_runs=runs, intent="đối soát", mode=TaskMode.GENERAL, created_by="u-1", idempotency_key="k-1")
    second = await launcher.launch(agent_runs=runs, intent="đối soát", mode=TaskMode.GENERAL, created_by="u-1", idempotency_key="k-1")

    assert second.idempotent_replay is True
    assert second.record.execution_run_id == first.record.execution_run_id
    assert len(runs.created_threads) == 1
    assert len(runs.started) == 1  # no new run admitted


@pytest.mark.asyncio
async def test_same_key_different_owner_is_separate_run(launcher) -> None:
    runs = FakeAgentRuns()
    await launcher.launch(agent_runs=runs, intent="đối soát", mode=TaskMode.GENERAL, created_by="u-1", idempotency_key="k-2")
    other = await launcher.launch(agent_runs=runs, intent="đối soát", mode=TaskMode.GENERAL, created_by="u-2", idempotency_key="k-2")
    assert other.idempotent_replay is False
    assert len(runs.created_threads) == 2


@pytest.mark.asyncio
async def test_same_key_different_intent_conflicts(launcher) -> None:
    runs = FakeAgentRuns()
    await launcher.launch(agent_runs=runs, intent="đối soát", mode=TaskMode.GENERAL, created_by="u-1", idempotency_key="k-3")
    with pytest.raises(LaunchConflict):
        await launcher.launch(agent_runs=runs, intent="khác", mode=TaskMode.GENERAL, created_by="u-1", idempotency_key="k-3")
    assert len(runs.started) == 1


@pytest.mark.asyncio
async def test_crash_resume_replays_start_on_same_thread(launcher, store) -> None:
    """Crash between map insert and start-ack: the row exists with run_id NULL.
    A retry must re-issue start() on the SAME thread with the SAME key — the
    host's `extension:{ns}:{key}` dedupe then returns the same run instead of
    erroring on 'different thread' (manager.py:1664)."""
    runs = FakeAgentRuns()
    # Simulate the crashed launch: insert row directly as the launcher would.
    record = await store.insert(
        ExecutionRunRecord.new(
            thread_id="thread-crash",
            task_mode="general",
            status=ExecutionRunStatus.LAUNCHING,
            idempotency_key="k-crash",
            created_by="u-1",
            intent="đối soát",
        )
    )
    assert record.run_id is None

    out = await launcher.launch(agent_runs=runs, intent="đối soát", mode=TaskMode.GENERAL, created_by="u-1", idempotency_key="k-crash")
    assert out.idempotent_replay is False  # resumed admission, not a fresh one
    assert len(runs.created_threads) == 0  # reuses the stored thread
    assert runs.started[0]["thread_id"] == "thread-crash"
    assert runs.started[0]["idempotency_key"] == "k-crash"
    assert out.record.run_id == "run-1"


# ---------------------------------------------------------------------------
# pending interrupt != complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_with_pending_interrupt_is_not_complete(launcher, store) -> None:
    runs = FakeAgentRuns(run_status="success")
    out = await launcher.launch(agent_runs=runs, intent="làm việc", mode=TaskMode.GENERAL, created_by="u-1")
    runs.thread_states[out.record.thread_id] = {"values": {}, "interrupts": [{"id": "i-1", "value": "approve?"}]}

    rec = await launcher.refresh(agent_runs=runs, record=out.record)
    assert rec.status == ExecutionRunStatus.PENDING_INTERRUPT
    assert rec.status != ExecutionRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_success_without_interrupt_completes(launcher, store) -> None:
    runs = FakeAgentRuns(run_status="success")
    out = await launcher.launch(agent_runs=runs, intent="làm việc", mode=TaskMode.GENERAL, created_by="u-1")
    runs.thread_states[out.record.thread_id] = {"values": {}, "next": []}

    rec = await launcher.refresh(agent_runs=runs, record=out.record)
    assert rec.status == ExecutionRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_interrupted_with_pending_input_is_pending_interrupt(launcher, store) -> None:
    runs = FakeAgentRuns(run_status="interrupted")
    out = await launcher.launch(agent_runs=runs, intent="làm việc", mode=TaskMode.GENERAL, created_by="u-1")
    runs.thread_states[out.record.thread_id] = {"next": ["node_a"]}

    rec = await launcher.refresh(agent_runs=runs, record=out.record)
    assert rec.status == ExecutionRunStatus.PENDING_INTERRUPT


@pytest.mark.asyncio
async def test_error_projects_failed(launcher, store) -> None:
    runs = FakeAgentRuns(run_status="error")
    out = await launcher.launch(agent_runs=runs, intent="x", mode=TaskMode.GENERAL, created_by="u-1")
    rec = await launcher.refresh(agent_runs=runs, record=out.record)
    assert rec.status == ExecutionRunStatus.FAILED


# ---------------------------------------------------------------------------
# uploads BEFORE start + FileInMessage mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_upload_before_start_and_map_into_input(store) -> None:
    order: list[str] = []

    class OrderRuns(FakeAgentRuns):
        async def start(self, **kwargs):
            order.append("start")
            return await super().start(**kwargs)

    class OrderUploader(FakeUploader):
        async def upload(self, *, thread_id, files):
            order.append("upload")
            return await super().upload(thread_id=thread_id, files=files)

    runs = OrderRuns()
    uploader = OrderUploader(_file_refs())
    launcher = RunLauncher(store, uploader=uploader)

    await launcher.launch(
        agent_runs=runs,
        intent="đối soát",
        mode=TaskMode.GENERAL,
        created_by="u-1",
        files=[FilePayload(filename="invoices.pdf", body=b"%PDF-fake", content_type="application/pdf")],
    )

    assert order == ["upload", "start"]  # uploads strictly BEFORE start

    files_kwarg = runs.started[0]["input"]["messages"][0]["additional_kwargs"]["files"]
    assert files_kwarg == [
        {
            "filename": "invoices.pdf",
            "size": 1234,
            "path": "/mnt/user-data/uploads/invoices.pdf",
            "status": "uploaded",
        },
        {"filename": "books.csv", "size": 56, "status": "uploaded"},
    ]


@pytest.mark.asyncio
async def test_files_without_uploader_fails(store) -> None:
    runs = FakeAgentRuns()
    launcher = RunLauncher(store)  # no uploader
    with pytest.raises(FilesWithoutUploader):
        await launcher.launch(
            agent_runs=runs,
            intent="x",
            mode=TaskMode.GENERAL,
            created_by="u-1",
            files=[FilePayload(filename="a.txt", body=b"a", content_type="text/plain")],
        )
    assert runs.started == []


@pytest.mark.asyncio
async def test_no_files_omits_files_key(launcher) -> None:
    runs = FakeAgentRuns()
    await launcher.launch(agent_runs=runs, intent="x", mode=TaskMode.GENERAL, created_by="u-1")
    kwargs = runs.started[0]["input"]["messages"][0]["additional_kwargs"]
    assert "files" not in kwargs


# ---------------------------------------------------------------------------
# HttpThreadUploads — real multipart POST to the upstream route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_uploads_posts_multipart_and_maps_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/api/threads/t-42/uploads"
        assert request.method == "POST"
        body = request.content
        assert b'name="files"; filename="a.txt"' in body
        return httpx.Response(
            200,
            json={
                "success": True,
                "files": [
                    {
                        "filename": "a.txt",
                        "size": 10,
                        "path": "/mnt/user-data/uploads/a.txt",
                        "virtual_path": "/mnt/user-data/uploads/a.txt",
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gateway.test")
    uploader = HttpThreadUploads(client)
    refs = await uploader.upload(thread_id="t-42", files=[FilePayload(filename="a.txt", body=b"0123456789", content_type="text/plain")])
    await client.aclose()

    assert len(seen) == 1
    assert refs == [UploadedFileRef(filename="a.txt", size=10, path="/mnt/user-data/uploads/a.txt")]


@pytest.mark.asyncio
async def test_http_uploads_error_never_leaks_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://gateway.test",
        headers={"Authorization": "Bearer sekret-token-123"},
    )
    uploader = HttpThreadUploads(client)
    with pytest.raises(Exception) as exc_info:
        await uploader.upload(thread_id="t", files=[FilePayload(filename="a", body=b"a", content_type="text/plain")])
    await client.aclose()
    assert "sekret-token-123" not in str(exc_info.value)
    assert "sekret-token-123" not in repr(exc_info.value)
    assert "sekret-token-123" not in repr(uploader)


# ---------------------------------------------------------------------------
# SSE route constants (plan §API table; thread_runs.py)
# ---------------------------------------------------------------------------


def test_sse_route_constants_match_upstream() -> None:
    assert RUN_STREAM_PATH == "/api/threads/{thread_id}/runs/stream"  # POST create+SSE :970
    assert RUN_JOIN_PATH == "/api/threads/{thread_id}/runs/{run_id}/join"  # GET join :1312
    assert RUN_STREAM_EXISTING_PATH == "/api/threads/{thread_id}/runs/{run_id}/stream"  # :1430/:1443
    assert THREAD_UPLOADS_PATH == "/api/threads/{thread_id}/uploads"  # POST :388
    assert run_join_url("t1", "r2") == "/api/threads/t1/runs/r2/join"
    assert thread_uploads_url("t1") == "/api/threads/t1/uploads"


def test_table_prefix() -> None:
    assert TABLE_PREFIX == "ewcp_"
    assert re.fullmatch(r"[a-z][a-z0-9_]*_", TABLE_PREFIX)
