"""N03 patch — model-visible feedback for non-schema tool-call args.

Measured gap (docs/vnext/GP01_TASKB_COST.md run 3): the model emitted
``read_file(path=…, toolbench_rapidapi_key=null)``. The name is not in
the alias table, and pydantic's ``extra='ignore'`` drops it silently —
the model never learns the argument it believed it passed never reached
the tool. These tests pin the bounded recovery check: a note on the
result naming the dropped args, without retrying or blocking the call.
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from deerflow.sandbox.tools import read_file_tool
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from ewcp_core.tool_arg_feedback import ToolArgFeedbackMiddleware


def _request(args: dict[str, Any], tool: Any = read_file_tool) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"id": "t1", "name": "read_file", "args": args},
        tool=tool,
        state={},
        runtime=types.SimpleNamespace(context={}),
    )


def _handler(message: str = "ok") -> Any:
    def _call(req: Any) -> ToolMessage:
        return ToolMessage(message, tool_call_id="t1", name="read_file")

    return _call


def test_non_schema_args_are_named_on_the_result() -> None:
    """The phantom arg from the measured run must surface: the model sees
    which of its arguments the schema dropped."""
    mw = ToolArgFeedbackMiddleware()
    result = mw.wrap_tool_call(_request({"path": "/x", "toolbench_rapidapi_key": None}), _handler())
    assert isinstance(result, ToolMessage)
    assert "toolbench_rapidapi_key" in result.content
    assert "path" in result.content  # accepted args listed for self-correction


def test_clean_call_is_annotated_nothing() -> None:
    mw = ToolArgFeedbackMiddleware()
    result = mw.wrap_tool_call(_request({"path": "/x"}), _handler("file contents"))
    assert result.content == "file contents"


def test_alias_covered_names_are_not_flagged() -> None:
    """`filepath` renames onto `path` via ToolArgAliasMiddleware — the arg
    is consumed, not dropped, so no note fires."""
    mw = ToolArgFeedbackMiddleware()
    result = mw.wrap_tool_call(_request({"filepath": "/x"}), _handler())
    assert "ignored argument" not in result.content


def test_transform_trail_marks_the_annotation() -> None:
    mw = ToolArgFeedbackMiddleware()
    result = mw.wrap_tool_call(_request({"path": "/x", "bogus": 1}), _handler())
    transforms = result.additional_kwargs.get("deerflow_tool_transforms")
    assert transforms and transforms[-1]["kind"] == "arg_drift_note"


def test_command_results_are_annotated_too() -> None:
    """present_files-style results carry ToolMessages inside
    Command.update — the note must reach those as well."""
    mw = ToolArgFeedbackMiddleware()

    def _command_handler(req: Any) -> Command:
        return Command(update={"messages": [ToolMessage("presented", tool_call_id="t1")]})

    result = mw.wrap_tool_call(_request({"path": "/x", "extra": 1}), _command_handler)
    messages = result.update["messages"]
    assert "extra" in messages[0].content


@pytest.mark.asyncio
async def test_async_path_annotates_identically() -> None:
    mw = ToolArgFeedbackMiddleware()

    async def _ahandler(req: Any) -> ToolMessage:
        return ToolMessage("ok", tool_call_id="t1")

    result = await mw.awrap_tool_call(_request({"path": "/x", "bogus": 2}), _ahandler)
    assert "bogus" in result.content


def test_annotation_is_idempotent_across_wrappers() -> None:
    """A message already carrying the note must not accumulate a second
    copy if it passes the wrapper again (e.g. composed stacks)."""
    mw = ToolArgFeedbackMiddleware()
    result = mw.wrap_tool_call(_request({"path": "/x", "bogus": 1}), _handler())
    once = result.content
    again = mw._annotate_message(result, ["bogus"], "read_file", ["path"])
    assert again.content == once
