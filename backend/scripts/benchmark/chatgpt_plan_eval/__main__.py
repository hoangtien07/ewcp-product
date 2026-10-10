"""Benchmark harness: DeerFlow + ChatGPT-plan OAuth vs DeerFlow + Ollama.

See README.md. Import production functions only; credentials come from the
OAuth credential store / env vars — never CLI args.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool as lc_tool


# Local deterministic tools — identical for both arms.
@lc_tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@lc_tool
def now() -> str:
    """Return the current UTC timestamp in ISO 8601."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


TOOLS = {"add": add, "now": now}

MAX_TOOL_TURNS = 4


def git_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parents[4], text=True).strip()
    except Exception:
        return "unknown"


def build_model(arm: str, model_id: str | None, credentials_dir: str | None):
    """Construct via the production class paths the same way config does."""
    if arm == "chatgpt_plan":
        from deerflow.models.chatgpt_plan_provider import ChatGPTPlanChatModel

        model_slug = model_id or os.environ.get("CHATGPT_PLAN_MODEL")
        if not model_slug:
            raise SystemExit("chatgpt_plan arm needs --model or CHATGPT_PLAN_MODEL (a slug from `...chatgpt_plan_oauth models`)")
        return ChatGPTPlanChatModel(model=model_slug, credentials_dir=credentials_dir or os.environ.get("CHATGPT_PLAN_CREDENTIALS_DIR"))
    if arm == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=model_id or os.environ.get("OLLAMA_MODEL", "qwen3:8b"), base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    raise SystemExit(f"unknown arm {arm!r}")


def run_task(model, task: dict[str, Any], max_turns: int = MAX_TOOL_TURNS) -> dict[str, Any]:
    """One mini agent loop: invoke → execute tool calls → feed back. Bounded."""
    messages: list = [HumanMessage(content=task["prompt"])]
    bound = model.bind_tools(list(TOOLS.values())) if task.get("use_tool") else model

    inference_calls = 0
    tool_calls_total = 0
    tool_calls_ok = 0
    in_tokens = out_tokens = 0
    latencies: list[int] = []
    failure_cause: str | None = None
    first_pass_correct = False
    completed = False
    final_text = ""

    for turn in range(max_turns):
        started = time.monotonic()
        try:
            response: AIMessage = bound.invoke(messages)
        except Exception as exc:  # provider errors carry .code when mapped
            failure_cause = f"{exc.__class__.__name__}:{getattr(exc, 'code', '') or getattr(exc, 'status_code', '') or str(exc)[:120]}"
            break
        latency_ms = int((time.monotonic() - started) * 1000)
        latencies.append(latency_ms)
        inference_calls += 1
        usage = response.usage_metadata or {}
        in_tokens += int(usage.get("input_tokens") or 0)
        out_tokens += int(usage.get("output_tokens") or 0)
        messages.append(response)

        final_text = str(response.content or "")
        if not response.tool_calls:
            completed = bool(final_text.strip())
            if turn == 0 and re.search(task["expect_regex"], final_text):
                first_pass_correct = True
            break

        for call in response.tool_calls:
            tool_calls_total += 1
            fn = TOOLS.get(call["name"])
            output: str
            if fn is None:
                output = f"error: unknown tool {call['name']}"
            else:
                try:
                    output = str(fn.invoke(call["args"]))
                    tool_calls_ok += 1
                except Exception as exc:
                    output = f"error: {exc}"
            messages.append(ToolMessage(content=output, tool_call_id=call["id"]))
    else:
        failure_cause = failure_cause or "max_tool_turns"

    # Correctness on the LAST text produced (any turn) for completion credit.
    correct = bool(re.search(task["expect_regex"], final_text)) if final_text else False
    return {
        "id": task["id"],
        "completed": completed,
        "correct": correct,
        "first_pass_correct": first_pass_correct or (correct and turn == 0),
        "tool_calls": tool_calls_total,
        "tool_call_success": tool_calls_ok,
        "inference_calls": inference_calls,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "latency_ms": sum(latencies),
        "latency_per_call_ms": latencies,
        "failure_cause": failure_cause,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results) or 1
    return {
        "tasks": len(results),
        "completion_rate": sum(r["completed"] for r in results) / n,
        "correct_rate": sum(r["correct"] for r in results) / n,
        "first_pass_correct_rate": sum(r["first_pass_correct"] for r in results) / n,
        "tool_call_success_rate": (sum(r["tool_call_success"] for r in results) / max(1, sum(r["tool_calls"] for r in results))),
        "total_inference_calls": sum(r["inference_calls"] for r in results),
        "total_input_tokens": sum(r["input_tokens"] for r in results),
        "total_output_tokens": sum(r["output_tokens"] for r in results),
        "total_latency_ms": sum(r["latency_ms"] for r in results),
        "failures": {r["id"]: r["failure_cause"] for r in results if r["failure_cause"]},
        # API-equivalent estimate only — subscription draw is plan-gated, not billed.
        "estimated_cost": None,
    }


def cmd_smoke(args) -> int:
    """Single live inference request — the acceptance 'authorized inference' proof."""
    started = time.monotonic()
    try:
        model = build_model(args.arm, args.model, args.credentials_dir)
        resp = model.invoke([HumanMessage(content="Reply with exactly: ok")])
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}", "code": getattr(exc, "code", None)}))
        return 1
    out = {
        "ok": True,
        "content": str(resp.content)[:200],
        "latency_ms": int((time.monotonic() - started) * 1000),
        "usage_metadata": resp.usage_metadata,
        "billing_source": resp.response_metadata.get("billing_source"),
        "provider": resp.response_metadata.get("provider"),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_run(args) -> int:
    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    arms = [args.arm] if args.arm else ["chatgpt_plan", "ollama"]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_doc: dict[str, Any] = {"git": git_revision(), "tasks_file": str(args.tasks), "arms": {}}
    for arm in arms:
        model = build_model(arm, args.model, args.credentials_dir)
        results = [run_task(model, t) for t in tasks]
        arm_doc = {"model": getattr(model, "model", None), "provider": arm, "results": results, "summary": summarize(results)}
        run_doc["arms"][arm] = arm_doc
        print(f"== {arm} ==", json.dumps(arm_doc["summary"], indent=2))
    (out_dir / "results.json").write_text(json.dumps(run_doc, indent=2), encoding="utf-8")
    print(f"wrote {out_dir / 'results.json'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="chatgpt_plan_eval")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("smoke", "run"):
        p = sub.add_parser(name)
        p.add_argument("--arm", choices=["chatgpt_plan", "ollama"], default=None if name == "run" else "chatgpt_plan")
        p.add_argument("--model", default=None, help="Model slug/id for the arm")
        p.add_argument("--credentials-dir", default=None)
        if name == "run":
            p.add_argument("--tasks", required=True)
            p.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    return cmd_smoke(args) if args.cmd == "smoke" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
