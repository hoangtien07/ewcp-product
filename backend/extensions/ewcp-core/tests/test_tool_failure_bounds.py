"""N03 — bounded-recovery checks for the OBSERVED tool-failure classes.

docs/vnext/TOOL_FAILURE_TAXONOMY.md classifies every tool-invocation
failure measured in docs/vnext/GP01_TASKB_COST.md. These tests pin, at
the product surface, what the current machinery does with each class —
the smallest deterministic proof of the taxonomy verdicts:

  - enforced (detected-by-middleware): a bound exists and fires;
  - surfaced (surfaced-to-model): the model receives an actionable error;
  - silent (silent-fail): no signal exists today (the gap the
    arg-drift patch closes — see test_arg_drift_feedback.py);
  - MODEL_LIMIT: honest surfacing is the only product-side bound.

Everything below is offline and deterministic — no sandbox, model, or
kernel is contacted. Upstream suites cover the same mechanisms more
deeply (`backend/tests/test_loop_detection_*`, `test_aio_sandbox.py`,
`test_read_file_tool_binary.py`); this file pins the contracts at the
lane the extension operates.
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
from deerflow.agents.middlewares.terminal_response_middleware import TerminalResponseMiddleware
from deerflow.agents.middlewares.tool_arg_alias_middleware import normalize_tool_call_args
from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from deerflow.community.aio_sandbox.aio_sandbox import _aio_file_error_from_body
from deerflow.sandbox import tools as sandbox_tools
from deerflow.sandbox.tools import read_file_tool
from deerflow.tools.builtins.present_file_tool import present_file_tool


def _runtime(**context: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(context=dict(context), state={}, execution_info=None, control=None)


def _ai_with_calls(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"tc-{i}", "name": c["name"], "args": c.get("args", {})} for i, c in enumerate(calls)],
    )


# ---------------------------------------------------------------------------
# Class: present_files on a non-outputs path → surfaced-to-model
# ---------------------------------------------------------------------------


def test_present_files_rejects_non_outputs_path_with_message() -> None:
    """GP01 run 1+2: present_files(/mnt/user-data/uploads/inventory.xlsx).
    The tool's rejection is a real ToolMessage — the model reads
    "Only files in /mnt/user-data/outputs can be presented"."""
    runtime = _runtime(thread_id="thr-1")
    runtime.state = {"thread_data": {"outputs_path": "/tmp/ewcp-n03/outputs"}}
    result = present_file_tool.func(
        runtime,
        filepaths=["/mnt/user-data/uploads/inventory.xlsx"],
        tool_call_id="tc-0",
    )
    update = getattr(result, "update", {})
    messages = update.get("messages")
    assert isinstance(messages, list) and isinstance(messages[0], ToolMessage)
    assert "Only files in /mnt/user-data/outputs can be presented" in messages[0].content


# ---------------------------------------------------------------------------
# Class: read_file on a binary file → designed hint reaches the model
# (the AIO envelope side is fixed by #59; both halves pinned here)
# ---------------------------------------------------------------------------


def test_aio_error_envelope_maps_decode_error_to_unicode_decode_error() -> None:
    """The `success:false` envelope that used to crash the SDK's typed
    parse now maps onto the builtin exception the tool layer renders."""
    body = {
        "success": False,
        "data": {
            "error_type": "decode_error",
            "exception_type": "UnicodeDecodeError",
            "message": "'utf-8' codec can't decode byte 0xd0",
        },
    }
    assert isinstance(_aio_file_error_from_body(body), UnicodeDecodeError)


def test_read_file_binary_returns_designed_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox/tools.py:2849 — a binary read resolves to the 'use bash
    with pandas/openpyxl' hint, not a bare crash string."""

    def _raise_ude(runtime: Any, path: str, **kwargs: Any) -> str:
        raise UnicodeDecodeError("utf-8", b"\xd0", 0, 1, "invalid start byte")

    monkeypatch.setattr(sandbox_tools, "read_current_file_content", _raise_ude)
    result = read_file_tool.func(
        types.SimpleNamespace(context={"user_id": "u"}, state={}),
        "/mnt/user-data/uploads/inventory.xlsx",
        "",
        None,
        None,
    )
    assert "binary file" in result
    assert "pandas/openpyxl" in result


# ---------------------------------------------------------------------------
# Class: non-schema arguments → silent drop (the gap the patch closes)
# ---------------------------------------------------------------------------


def test_non_schema_args_are_silently_dropped_by_validation() -> None:
    """GP01 run 3 emitted read_file(toolbench_rapidapi_key=null): the
    model-visible schema accepts it without error and drops it — no
    signal reaches the model. This pins the silent class; the companion
    test file pins the new feedback note."""
    validated = read_file_tool.tool_call_schema.model_validate(
        {"path": "/mnt/user-data/uploads/inventory.xlsx", "toolbench_rapidapi_key": None}
    )
    assert "toolbench_rapidapi_key" not in validated.model_dump()


def test_alias_middleware_renames_drifted_args() -> None:
    """The same drift, when the drifted name is in the alias table, is
    corrected by middleware before validation — the canonical name wins."""
    normalized, applied = normalize_tool_call_args("write_file", {"path": "/a", "contents": "x"})
    assert applied == {"contents": "content"}
    assert normalized == {"path": "/a", "content": "x"}


# ---------------------------------------------------------------------------
# Class: repeated identical tool calls → enforced bound (warn 3 / hard 5)
# ---------------------------------------------------------------------------


def test_identical_calls_below_warn_threshold_produce_no_signal() -> None:
    """The measured failure retried once — a byte-identical second call
    (GP01 run 2). Two identical call sets stay below the warn threshold
    by design: one retry is legitimate recovery."""
    mw = LoopDetectionMiddleware()
    rt = _runtime(thread_id="t", run_id="r")
    call = {"name": "present_files", "args": {"filepaths": ["/mnt/user-data/uploads/inventory.xlsx"]}}
    for _ in range(2):
        assert mw.after_model({"messages": [_ai_with_calls(call)]}, rt) is None


def test_identical_calls_warn_at_threshold_and_hard_stop_at_limit() -> None:
    """The bound DOES exist: the 3rd identical call set queues a warning
    for the next model call, the 5th strips tool_calls (loop_capped)."""
    mw = LoopDetectionMiddleware()
    rt = _runtime(thread_id="t", run_id="r")
    call = {"name": "read_file", "args": {"path": "/x"}}

    for _ in range(2):
        assert mw.after_model({"messages": [_ai_with_calls(call)]}, rt) is None
    assert mw.after_model({"messages": [_ai_with_calls(call)]}, rt) is None  # warn is deferred

    captured: dict[str, Any] = {}

    def _handler(request: Any) -> Any:
        captured["messages"] = request.messages
        return "response"

    request = types.SimpleNamespace(messages=[HumanMessage(content="go")], runtime=rt, override=lambda **kw: types.SimpleNamespace(**{**vars(request), **kw}))
    mw.wrap_model_call(request, _handler)
    warnings = [m for m in captured["messages"] if isinstance(m, HumanMessage) and "LOOP DETECTED" in str(m.content)]
    assert warnings, "the warn-threshold hint must reach the next model call"

    update = None
    for _ in range(2):
        update = mw.after_model({"messages": [_ai_with_calls(call)]}, rt)
    assert update is not None
    stripped = update["messages"][0]
    assert stripped.tool_calls == []
    assert "FORCED STOP" in str(stripped.content)
    assert mw.consume_stop_reason("r") == "loop_capped"


# ---------------------------------------------------------------------------
# Class: surrender mid-run → MODEL_LIMIT + advisory surface only
# ---------------------------------------------------------------------------


def test_empty_terminal_response_gets_fallback_not_silent_success() -> None:
    """The one surrender subclass the harness bounds: an EMPTY post-tool
    terminal response is replaced with honest fallback text."""
    mw = TerminalResponseMiddleware()
    state = {
        "messages": [
            HumanMessage(content="do the thing"),
            AIMessage(content="", tool_calls=[{"id": "t1", "name": "read_file", "args": {"path": "/x"}}]),
            ToolMessage(content="Error: File not found: /x", tool_call_id="t1"),
            AIMessage(content=""),  # the surrender: no text, no tool calls
        ]
    }
    update = mw.after_model(state, _runtime())
    assert update is not None
    fallback = update["messages"][0]
    assert "no final response" in str(fallback.content)
    assert fallback.additional_kwargs["error_reason"] == "Model returned an empty terminal response"


def test_nonempty_surrender_is_not_caught_by_terminal_middleware() -> None:
    """The GP01 surrender carried prose ('move the file and retry') —
    no middleware bounds a model that simply stops. The product's honest
    surface is the post-run integrity flag, pinned in
    test_ewcp_core_deliverable_integrity.py."""
    mw = TerminalResponseMiddleware()
    state = {
        "messages": [
            HumanMessage(content="do the thing"),
            ToolMessage(content="Error: File not found: /x", tool_call_id="t1"),
            AIMessage(content="I could not read it; please move the file."),
        ]
    }
    assert mw.after_model(state, _runtime()) is None


# ---------------------------------------------------------------------------
# Class: tool exception → wrapped error ToolMessage (surfaced)
# ---------------------------------------------------------------------------


def test_tool_exception_becomes_error_toolmessage() -> None:
    """Any tool exception lands as 'Error: Tool <name> failed with <Exc>:
    <detail>. Continue with available context…' — never a run-killing
    crash."""
    mw = ToolErrorHandlingMiddleware()
    request = ToolCallRequest(
        tool_call={"id": "t1", "name": "read_file", "args": {"path": "/x"}},
        tool=None,
        state={},
        runtime=None,
    )

    def _handler(req: Any) -> ToolMessage:
        raise ValueError("boom")

    result = mw.wrap_tool_call(request, _handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "read_file" in result.content
    assert "ValueError" in result.content
    assert "Continue with available context" in result.content
