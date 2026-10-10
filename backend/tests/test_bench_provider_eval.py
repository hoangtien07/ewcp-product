"""Offline tests for the provider_eval benchmark harness.

Scripted fake chat models only — no network, no credentials, no provider.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from benchmark.provider_eval.__main__ import (  # noqa: E402
    CF_API_BASE,
    build_model,
    run_task,
    summarize,
)


class _ScriptedModel(GenericFakeChatModel):
    """Fake model that emits one tool call on turn 1 then a final answer."""

    def bind_tools(self, tools, **kwargs):  # noqa: D102
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: D102
        from langchain_core.outputs import ChatGeneration, ChatResult

        if len(messages) == 1:
            msg = AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "add", "args": {"a": 17, "b": 25}}],
            )
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
        s = summarize([run_task(_ScriptedModel(messages=iter([])), TASK)], wall_ms=100, parallel=1)
        assert s["tasks"] == 1
        assert s["completion_rate"] == 1.0
        assert s["tool_call_success_rate"] == 1.0
        assert s["wall_ms"] == 100
        assert s["parallel_workers"] == 1
        assert s["estimated_cost"] is None


class _UnmeteredModel(GenericFakeChatModel):
    """Fake model whose responses carry NO usage_metadata — the wire shape
    an arm produces when the provider never reports token usage."""

    def bind_tools(self, tools, **kwargs):  # noqa: D102
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: D102
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="42"))])


class _MeteredModel(_UnmeteredModel):
    """Same answer, but the provider reports real usage per call."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: D102
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="42",
                        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                    )
                )
            ]
        )


class TestCostObservability:
    """N08 — unmeasured usage must surface as null/unknown, never a
    fabricated 0 (the actual_spend_usd vs estimated_cost_usd class)."""

    def test_unmetered_task_tokens_null_not_zero(self):
        result = run_task(_UnmeteredModel(messages=iter([])), TASK)
        assert result["completed"] is True
        assert result["input_tokens"] is None
        assert result["output_tokens"] is None

    def test_unmetered_summary_totals_null_not_zero(self):
        s = summarize([run_task(_UnmeteredModel(messages=iter([])), TASK)], wall_ms=50, parallel=1)
        assert s["total_input_tokens"] is None
        assert s["total_output_tokens"] is None
        assert s["token_usage_coverage"] == "unmeasured"

    def test_metered_tokens_stay_measured(self):
        result = run_task(_MeteredModel(messages=iter([])), TASK)
        assert result["input_tokens"] == 11
        assert result["output_tokens"] == 7
        s = summarize([result], wall_ms=50, parallel=1)
        assert s["total_input_tokens"] == 11
        assert s["total_output_tokens"] == 7
        assert s["token_usage_coverage"] == "measured"

    def test_partial_coverage_aggregates_only_measured(self):
        s = summarize(
            [
                run_task(_MeteredModel(messages=iter([])), TASK),
                run_task(_UnmeteredModel(messages=iter([])), TASK),
            ],
            wall_ms=50,
            parallel=1,
        )
        assert s["total_input_tokens"] == 11
        assert s["total_output_tokens"] == 7
        assert s["token_usage_coverage"] == "partial"


class TestArms:
    def test_workers_ai_requires_env(self, monkeypatch):
        monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("CF_API_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="CF_API_TOKEN"):
            build_model("workers_ai", None)

    def test_workers_ai_base_url_shape(self):
        assert "{account_id}" in CF_API_BASE
        assert CF_API_BASE.endswith("/ai/v1")

    def test_unknown_arm_rejected(self):
        with pytest.raises(SystemExit, match="unknown arm"):
            build_model("nope", None)


class TestTaskFile:
    def test_tasks_json_valid(self):
        tasks = json.loads((Path(__file__).parent.parent / "scripts" / "benchmark" / "provider_eval" / "tasks.json").read_text())
        assert len(tasks) >= 5
        for t in tasks:
            assert set(t) >= {"id", "prompt", "expect_regex", "use_tool"}
