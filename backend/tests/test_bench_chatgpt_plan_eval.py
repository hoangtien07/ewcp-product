"""Offline tests for the chatgpt_plan_eval benchmark harness.

Uses a scripted fake chat model — no network, no credentials.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from benchmark.chatgpt_plan_eval.__main__ import run_task, summarize  # noqa: E402


class _ScriptedModel(GenericFakeChatModel):
    """Fake model that emits one tool call on turn 1 then a final answer."""

    def bind_tools(self, tools, **kwargs):  # noqa: D102
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: D102
        from langchain_core.outputs import ChatGeneration, ChatResult

        if len(messages) == 1:
            msg = AIMessage(content="", tool_calls=[{"id": "c1", "name": "add", "args": {"a": 17, "b": 25}}])
        else:
            msg = AIMessage(content="42")
        return ChatResult(generations=[ChatGeneration(message=msg)])


TASK = {"id": "t2", "prompt": "add", "expect_regex": "42", "use_tool": True}


class TestRunTask:
    def test_tool_round_trip_metrics(self):
        result = run_task(_ScriptedModel(messages=iter([])), TASK)
        assert result["completed"] is True
        assert result["correct"] is True
        assert result["tool_calls"] == 1
        assert result["tool_call_success"] == 1
        assert result["inference_calls"] == 2
        assert result["failure_cause"] is None

    def test_provider_failure_recorded(self):
        class _Failing(GenericFakeChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                raise RuntimeError("boom")

        result = run_task(_Failing(messages=iter([])), TASK)
        assert result["completed"] is False
        assert "RuntimeError" in result["failure_cause"]

    def test_summarize_shape(self):
        s = summarize([run_task(_ScriptedModel(messages=iter([])), TASK)])
        assert s["tasks"] == 1
        assert s["completion_rate"] == 1.0
        assert s["tool_call_success_rate"] == 1.0
        assert s["estimated_cost"] is None


class TestTaskFile:
    def test_tasks_json_valid(self):
        tasks = json.loads((Path(__file__).parent.parent / "scripts" / "benchmark" / "chatgpt_plan_eval" / "tasks.json").read_text())
        assert len(tasks) >= 5
        for t in tasks:
            assert set(t) >= {"id", "prompt", "expect_regex", "use_tool"}
