"""Tests for the EWCP general-lane FoundationPort adapter (M-EA1 fork side).

These pin the spec-005 §2.5 contract the kernel executor will call once
the kernel side merges: 1 WorkRun = 1 deer-flow thread
(``thread_id="ewcp-"+workrun_id``, ``user_id="ewcp-"+tenant_id``),
each attempt = one ``client.stream()`` turn consumed to exhaustion.
"""

from __future__ import annotations

import os
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from ewcp_packs.foundation_port import (
    EWCP_AGENT_NAME,
    EWCP_TOOL_GROUPS,
    DeerFlowPortImpl,
    TurnResult,
    ensure_agent_profile,
    load_foundation_cfg,
)


def _ev(type_: str, data: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(type=type_, data=data or {})


def _ai(msg_id: str, delta: str) -> types.SimpleNamespace:
    return _ev("messages-tuple", {"type": "ai", "id": msg_id, "content": delta})


class _StubClient:
    """Duck-typed DeerFlowClient stand-in — stream() yields canned events."""

    def __init__(self, events: list | None = None, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self._events = events or []
        self._error = error
        self.closed = threading.Event()

    def stream(self, message: str, *, thread_id: str | None = None, **kwargs):
        self.calls.append({"message": message, "thread_id": thread_id, **kwargs})

        def gen():
            try:
                for ev in self._events:
                    yield ev
                if self._error is not None:
                    raise self._error
            except GeneratorExit:
                self.closed.set()
                raise

        return gen()

    def upload_files(self, thread_id: str, files: list) -> dict:
        # record the user identity the upload helpers resolve at call time
        from deerflow.runtime.user_context import get_effective_user_id

        self.upload_calls.append({"thread_id": thread_id, "files": list(files), "user_id": get_effective_user_id()})
        return {"success": True, "files": [], "message": "ok"}


def _port(client: _StubClient | None = None) -> tuple[DeerFlowPortImpl, _StubClient]:
    client = client or _StubClient()
    return DeerFlowPortImpl(client=client), client


class TestTurn:
    def test_collects_last_ai_message_and_usage(self) -> None:
        port, client = _port(
            _StubClient(
                [
                    _ai("m1", "phần đầu "),
                    _ai("m2", "draft giữa"),
                    _ai("m1", "và phần cuối"),
                    _ev("values", {"ignored": True}),
                    _ev("end", {"usage": {"input_tokens": 11, "output_tokens": 7}}),
                ]
            )
        )
        result = port.turn(
            thread_id="ewcp-w1",
            user_id="ewcp-t1",
            message="plan",
            workspace=Path("/tmp/ws"),
            model=None,
            timeout_s=60,
        )
        # mirrors DeerFlowClient.chat(): only the LAST message id's text
        assert result.final_text == "phần đầu và phần cuối"
        assert result.usage == {"input_tokens": 11, "output_tokens": 7}
        assert result.stop_reason == "end"
        call = client.calls[0]
        assert call["thread_id"] == "ewcp-w1"
        assert call["user_id"] == "ewcp-t1"
        assert call["is_internal"] is True
        assert "model_name" not in call

    def test_model_override_forwarded(self) -> None:
        port, client = _port(_StubClient([_ev("end", {"usage": {}})]))
        port.turn(
            thread_id="ewcp-w2",
            user_id="ewcp-t1",
            message="go",
            workspace=Path("/tmp/ws"),
            model="strong-model",
            timeout_s=10,
        )
        assert client.calls[0]["model_name"] == "strong-model"

    def test_timeout_closes_generator(self) -> None:
        def endless():
            try:
                while True:
                    yield _ev("values", {})
            except GeneratorExit:
                raise

        client = _StubClient()
        gen_holder: list[Any] = []

        def stream(message, *, thread_id=None, **kwargs):  # noqa: ANN001, ANN202
            gen = endless()
            gen_holder.append(gen)
            return gen

        client.stream = stream  # type: ignore[method-assign]
        port = DeerFlowPortImpl(client=client)
        result = port.turn(
            thread_id="ewcp-w3",
            user_id="ewcp-t1",
            message="x",
            workspace=Path("/tmp/ws"),
            model=None,
            timeout_s=0.0,
        )
        assert result.stop_reason == "timeout"
        # closed generators stay closed — a second next() never resumes
        assert next(gen_holder[0], None) is None

    def test_error_becomes_stop_reason_with_partial_text(self) -> None:
        port, _client = _port(_StubClient([_ai("m1", "lỗi giữa chừng")], error=RuntimeError("boom")))
        result = port.turn(
            thread_id="ewcp-w4",
            user_id="ewcp-t1",
            message="x",
            workspace=Path("/tmp/ws"),
            model=None,
            timeout_s=60,
        )
        assert result.stop_reason == "error:RuntimeError"
        assert result.final_text == "lỗi giữa chừng"

    def test_thread_registry_cleared_after_turn(self) -> None:
        port, _client = _port(_StubClient([_ev("end", {"usage": {}})]))
        port.turn(
            thread_id="ewcp-w5",
            user_id="ewcp-t1",
            message="x",
            workspace=Path("/tmp/ws"),
            model=None,
            timeout_s=60,
        )
        assert "ewcp-w5" not in port._turns


class TestUploadFiles:
    def test_runs_under_scoped_user(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("data")
        port, client = _port()
        port.upload_files(thread_id="ewcp-w6", user_id="ewcp-tenant9", files=[f])
        assert client.upload_calls[0]["user_id"] == "ewcp-tenant9"
        assert client.upload_calls[0]["files"] == [str(f)]

        # contextvar restored — later unscoped calls see the default bucket
        from deerflow.runtime.user_context import get_effective_user_id

        assert get_effective_user_id() == "default"


class TestAbort:
    def test_abort_closes_live_generator(self) -> None:
        port, _client = _port()
        closed = threading.Event()

        def gen():
            try:
                while True:
                    yield _ev("values", {})
            except GeneratorExit:
                closed.set()
                raise

        g = gen()
        next(g)  # prime it — close() on a never-started gen is a no-op
        port._turns["ewcp-w7"] = g
        port.abort(thread_id="ewcp-w7")
        assert closed.is_set()

    def test_abort_unknown_thread_is_noop(self) -> None:
        port, _client = _port()
        port.abort(thread_id="ewcp-nobody")  # must not raise

    def test_abort_racing_turn_does_not_raise(self) -> None:
        # close() on a generator currently executing on another thread
        # raises ValueError — abort must swallow it (best-effort per spec)
        started = threading.Event()
        done = threading.Event()

        def gen():
            try:
                while True:
                    yield _ev("values", {})
            finally:
                done.set()

        client = _StubClient()
        client.stream = lambda message, thread_id=None, **kwargs: gen()  # type: ignore[method-assign]
        port = DeerFlowPortImpl(client=client)
        started.set()

        # drive one next() so the generator is running when abort lands
        g = port._turns  # noqa: SLF001 — introspection is the point
        t = threading.Thread(
            target=lambda: port.turn(
                thread_id="ewcp-w8",
                user_id="ewcp-t1",
                message="x",
                workspace=Path("/tmp/ws"),
                model=None,
                timeout_s=5,
            )
        )
        t.start()
        time.sleep(0.05)
        port.abort(thread_id="ewcp-w8")  # must not raise
        t.join(timeout=5)
        assert done.is_set()
        assert "ewcp-w8" not in g


class TestFlags:
    def test_middleware_stamps_noninteractive_flags(self) -> None:
        from ewcp_packs.foundation_port import _EwcpRunFlagsMiddleware

        runtime = types.SimpleNamespace(context={"user_id": "ewcp-t1"})
        _EwcpRunFlagsMiddleware().before_agent({}, runtime)
        assert runtime.context["disable_clarification"] is True
        assert runtime.context["non_interactive"] is True

    def test_middleware_tolerates_missing_context(self) -> None:
        from ewcp_packs.foundation_port import _EwcpRunFlagsMiddleware

        runtime = types.SimpleNamespace(context=None)
        assert _EwcpRunFlagsMiddleware().before_agent({}, runtime) is None

    def test_client_tool_profile_restricts_to_file_and_bash_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ewcp_packs.foundation_port import EwcpDeerFlowClient

        captured: dict[str, Any] = {}

        def fake_get_available_tools(**kwargs):
            captured.update(kwargs)
            return [
                types.SimpleNamespace(name="bash"),
                types.SimpleNamespace(name="read_file"),
                types.SimpleNamespace(name="ask_clarification"),
                types.SimpleNamespace(name="view_image"),
                types.SimpleNamespace(name="present_files"),
            ]

        import deerflow.tools

        monkeypatch.setattr(deerflow.tools, "get_available_tools", fake_get_available_tools)
        tools = EwcpDeerFlowClient._get_tools(model_name=None, subagent_enabled=True)
        assert captured["groups"] == list(EWCP_TOOL_GROUPS)
        assert captured["include_mcp"] is False
        # spec §9: subagents off, MCP off, clarification+view_image off
        assert captured["subagent_enabled"] is False
        assert captured["include_conversation_reader"] is False
        names = {t.name for t in tools}
        assert "bash" in names and "read_file" in names
        assert "ask_clarification" not in names and "view_image" not in names

    def test_client_runnable_config_pins_noninteractive(self) -> None:
        from ewcp_packs.foundation_port import EwcpDeerFlowClient

        # __new__ skips __init__ (config load); _get_runnable_config only
        # reads these four ctor attrs
        client = EwcpDeerFlowClient.__new__(EwcpDeerFlowClient)
        client._model_name = None
        client._thinking_enabled = True
        client._subagent_enabled = False
        client._plan_mode = False
        cfg = client._get_runnable_config("ewcp-w9")
        assert cfg["configurable"]["non_interactive"] is True
        assert cfg["configurable"]["max_total_subagents"] == 0


class TestAgentProfile:
    def test_materializes_shared_profile(self, tmp_path: Path) -> None:
        agent_dir = ensure_agent_profile(base_dir=tmp_path)
        assert agent_dir == tmp_path / "agents" / EWCP_AGENT_NAME
        cfg = agent_dir / "config.yaml"
        assert cfg.is_file()
        import yaml

        parsed = yaml.safe_load(cfg.read_text())
        assert parsed["name"] == EWCP_AGENT_NAME
        assert parsed["allowed_subagents"] == []
        assert parsed["memory_enabled"] is False
        assert sorted(parsed["tool_groups"]) == sorted(EWCP_TOOL_GROUPS)
        soul = agent_dir / "SOUL.md"
        assert soul.is_file()
        assert "outbox" in soul.read_text()

    def test_does_not_overwrite_operator_edits(self, tmp_path: Path) -> None:
        agent_dir = ensure_agent_profile(base_dir=tmp_path)
        cfg = agent_dir / "config.yaml"
        cfg.write_text("name: ewcp-general\nmemory_enabled: true\n")
        ensure_agent_profile(base_dir=tmp_path)
        assert "memory_enabled: true" in cfg.read_text()


class TestKernelTypes:
    def test_turnresult_shape(self) -> None:
        r = TurnResult(final_text="x", usage={"input_tokens": 1}, stop_reason="end")
        assert r.final_text == "x"
        assert r.usage["input_tokens"] == 1
        assert r.stop_reason == "end"

    def test_foundation_cfg_loader(self) -> None:
        # kernel foundation module not merged yet -> None (kernel default
        # applies); when it merges this returns FoundationConfig.from_env()
        cfg = load_foundation_cfg()
        try:
            import ewcp.foundation.port  # noqa: F401
        except ImportError:
            assert cfg is None
        else:
            assert cfg is not None

    def test_workspace_base_defaults_to_harness_users_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The kernel resolves workspaces as {base}/users/<u>/threads/<t>/
        user-data — must land on the same dir the sandbox tools map to
        /mnt/user-data/, or outbox envelopes vanish."""
        try:
            import ewcp.foundation.port  # noqa: F401
        except ImportError:
            pytest.skip("kernel foundation module not merged")
        monkeypatch.delenv("EWCP_FOUNDATION_WORKSPACE_BASE", raising=False)
        from deerflow.config.paths import get_paths

        cfg = load_foundation_cfg()
        assert cfg is not None
        expected = str(get_paths().base_dir)
        assert os.environ["EWCP_FOUNDATION_WORKSPACE_BASE"] == expected
        assert getattr(cfg, "workspace_base", None) in (expected, None)

    def test_workspace_base_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EWCP_FOUNDATION_WORKSPACE_BASE", "/custom/ws")
        load_foundation_cfg()
        assert os.environ["EWCP_FOUNDATION_WORKSPACE_BASE"] == "/custom/ws"


class TestSoulContract:
    """The kernel prompt names workspace-relative paths, but sandbox file
    tools only accept absolute /mnt/user-data/ paths, and envelopes must
    be {id,type,payload} — the SOUL must pin both or the agent loops."""

    def test_soul_maps_virtual_paths(self) -> None:
        from ewcp_packs.foundation_port import _EWCP_SOUL_MD

        assert "/mnt/user-data/workspace/" in _EWCP_SOUL_MD
        assert "/mnt/user-data/uploads/" in _EWCP_SOUL_MD
        assert "/mnt/user-data/outputs/" in _EWCP_SOUL_MD

    def test_soul_pins_envelope_shape(self) -> None:
        from ewcp_packs.foundation_port import _EWCP_SOUL_MD

        assert '"type": "propose_contract"' in _EWCP_SOUL_MD
        assert '"payload"' in _EWCP_SOUL_MD
        assert '"base": "uploads"' in _EWCP_SOUL_MD
