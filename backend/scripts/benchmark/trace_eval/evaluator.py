"""Offline acceptance evaluator — grades POSTCONDITION, not ``status=success``.

Reads canned run traces (JSONL) and grades each case against the spec's
acceptance schema. No network, no provider, no live model — every verdict
is derived from facts recorded in the trace itself.

Grading semantics (spec EWCP_DEVIN_OVERNIGHT_V2_2026-10-11 §N02):

- ``task_finished`` — the trace declares the run terminal.
- ``artifact_observed`` — every ``required_artifacts`` entry is matched by a
  produced artifact at the same path with ``size > 0`` and, when the
  requirement declares a ``sha256``, a matching digest. A produced artifact
  without a digest cannot verify against a declared one — unobserved.
- ``acceptance_verified`` — for contract-bound lanes (``contract`` recorded
  in the trace) the kernel verdict must be ``"pass"`` AND ``sealed``; for
  the general lane (no contract exists) the artifact postcondition IS the
  acceptance check — there is no second gate to trust.
- ``human_accepted`` — the recorded acceptance event's verdict, or null.
  Absent an event it is never inferred.
- ``oracle`` — the run's self-reported status, verbatim. It is evidence
  about what the run CLAIMED, never a pass condition.
- ``task_passed`` = task_finished AND artifact_observed AND
  acceptance_verified.
- ``evaluator_assertions_passed`` — the computed verdict matches the case's
  declared ``expect.task_passed`` (the evaluator's own oracle check: a
  wrong trace must grade task_passed=false AND this field true).

Result schema (every field always emitted):
``{case_id, base_sha, candidate_sha, lane, evidence_tier, provider,
task_finished, artifact_observed, acceptance_verified, human_accepted,
oracle, task_passed, evaluator_assertions_passed, llm_calls,
estimated_cost_usd, actual_spend_usd, cost_coverage, limitations}``
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

EVIDENCE_TIER_TRACE_REPLAY = "TRACE_REPLAY"
EVIDENCE_TIERS = ("TRACE_REPLAY", "RUNTIME_E2E", "LIVE_MODEL")

REQUIRED_RESULT_KEYS = frozenset(
    {
        "case_id",
        "base_sha",
        "candidate_sha",
        "lane",
        "evidence_tier",
        "provider",
        "task_finished",
        "artifact_observed",
        "acceptance_verified",
        "human_accepted",
        "oracle",
        "task_passed",
        "evaluator_assertions_passed",
        "llm_calls",
        "estimated_cost_usd",
        "actual_spend_usd",
        "cost_coverage",
        "limitations",
    }
)

REQUIRED_CASE_KEYS = frozenset(
    {
        "case_id",
        "lane",
        "provider",
        "run_status",
        "terminal",
        "required_artifacts",
        "produced_artifacts",
        "expect",
    }
)

EVALUATOR_LIMITATION = "trace replay grades facts recorded in the trace — a trace that omits its own events is undetectable at this tier"


def git_revision() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short=10", "HEAD"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def load_cases(path: Path | str) -> list[dict[str, Any]]:
    """Read a JSONL case file. Blank lines are skipped; each line must be a
    complete case object (no streaming/concatenated JSON)."""
    cases: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            case["_lineno"] = lineno
            cases.append(case)
    return cases


def _malformed_note(field: str, limitations: list[str], detail: str) -> None:
    limitations.append(f"malformed {field}: {detail}")


def _artifact_observed(required: Any, produced: Any, limitations: list[str]) -> bool:
    """Every required artifact must be produced at the same path, non-empty,
    and digest-equal when the requirement declares one. Non-list or non-dict
    records are malformed trace data — they are reported, never invented."""
    if required is None:
        required = []
    if produced is None:
        produced = []
    if not isinstance(required, list):
        _malformed_note("required_artifacts", limitations, "expected a list — requirements unreadable")
        return False
    if not isinstance(produced, list):
        _malformed_note("produced_artifacts", limitations, "expected a list — no valid produced records")
        produced = []
    if any(not isinstance(req, dict) for req in required):
        _malformed_note("required_artifacts", limitations, "non-object requirement(s) — cannot verify")
        return False
    valid_produced = [p for p in produced if isinstance(p, dict)]
    if len(valid_produced) != len(produced):
        _malformed_note("produced_artifacts", limitations, "non-object entries ignored")
    by_path = {p.get("path"): p for p in valid_produced}
    for req in required:
        prod = by_path.get(req.get("path"))
        if prod is None:
            return False
        if not isinstance(prod.get("size"), int) or prod["size"] <= 0:
            return False
        want = req.get("sha256")
        if want is not None and prod.get("sha256") != want:
            return False
    return True


def _acceptance_verified(case: dict[str, Any], artifact_observed: bool, limitations: list[str]) -> bool:
    """Contract-bound lanes trust the recorded kernel verdict + seal; the
    general lane has no contract — its artifact postcondition is the only
    acceptance signal that exists. A contract that is present but not an
    object cannot carry a verdict — acceptance stays unverified."""
    contract = case.get("contract")
    if contract is None:
        return artifact_observed
    if not isinstance(contract, dict):
        _malformed_note("contract", limitations, "expected object — verdict unreadable, acceptance unverified")
        return False
    return contract.get("verdict") == "pass" and contract.get("sealed") is True


def _cost_amount(cost: dict[str, Any], key: str, limitations: list[str]) -> float | int | None:
    """A recorded spend value is honest only as a number; anything else
    (string, bool, nested) reports as unrecorded with the malformation
    noted — the evaluator never fabricates a figure."""
    value = cost.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _malformed_note("cost", limitations, f"{key} is not a number — reported as null")
        return None
    return value


def _cost_fields(cost: Any, limitations: list[str]) -> tuple[Any, Any, str]:
    """Project the trace's cost block into (estimated, actual, coverage).
    A missing or non-object cost block reads as unmeasured, with the
    malformation recorded — never as a fabricated zero."""
    if cost is None:
        return None, None, "none"
    if not isinstance(cost, dict):
        _malformed_note("cost", limitations, f"expected object, got {type(cost).__name__} — reported unmeasured")
        return None, None, "none"
    estimated = _cost_amount(cost, "estimated_usd", limitations)
    actual = _cost_amount(cost, "actual_usd", limitations)
    if actual is not None:
        coverage = "measured"
    elif estimated is not None:
        coverage = "estimated_only"
    else:
        coverage = "none"
    return estimated, actual, coverage


def _case_limitations(case: dict[str, Any]) -> list[str]:
    raw = case.get("limitations")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return list(raw)
    return [f"malformed limitations: expected list of strings, got {type(raw).__name__}"]


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    missing_keys = REQUIRED_CASE_KEYS - set(case)
    limitations = _case_limitations(case)
    if EVALUATOR_LIMITATION not in limitations:
        limitations.append(EVALUATOR_LIMITATION)
    if missing_keys:
        limitations.append(f"malformed case: missing keys {sorted(missing_keys)}")
        estimated, actual, coverage = _cost_fields(case.get("cost"), limitations)
        return {
            "case_id": case.get("case_id", f"line:{case.get('_lineno', '?')}"),
            "base_sha": case.get("base_sha"),
            "candidate_sha": git_revision(),
            "lane": case.get("lane"),
            "evidence_tier": EVIDENCE_TIER_TRACE_REPLAY,
            "provider": case.get("provider"),
            "task_finished": False,
            "artifact_observed": False,
            "acceptance_verified": False,
            "human_accepted": None,
            "oracle": case.get("run_status"),
            "task_passed": False,
            "evaluator_assertions_passed": False,
            "llm_calls": case.get("llm_calls"),
            "estimated_cost_usd": estimated,
            "actual_spend_usd": actual,
            "cost_coverage": coverage,
            "limitations": limitations,
        }

    task_finished = bool(case.get("terminal"))
    artifact_observed = _artifact_observed(
        case.get("required_artifacts"),
        case.get("produced_artifacts"),
        limitations,
    )
    acceptance_verified = _acceptance_verified(case, artifact_observed, limitations)

    event = case.get("human_acceptance")
    if event is None:
        human_accepted = None
    elif isinstance(event, dict):
        human_accepted = event.get("approved")
    else:
        human_accepted = None
        _malformed_note("human_acceptance", limitations, "expected object — treated as unrecorded")

    task_passed = bool(task_finished and artifact_observed and acceptance_verified)
    expect = case.get("expect")
    if expect is not None and not isinstance(expect, dict):
        _malformed_note("expect", limitations, "expected object — evaluator oracle unreadable")
        expect = None
    expected = (expect or {}).get("task_passed")
    evaluator_assertions_passed = expected is not None and task_passed == expected

    estimated, actual, coverage = _cost_fields(case.get("cost"), limitations)
    return {
        "case_id": case["case_id"],
        "base_sha": case.get("base_sha"),
        "candidate_sha": git_revision(),
        "lane": case["lane"],
        "evidence_tier": EVIDENCE_TIER_TRACE_REPLAY,
        "provider": case["provider"],
        "task_finished": task_finished,
        "artifact_observed": artifact_observed,
        "acceptance_verified": acceptance_verified,
        "human_accepted": human_accepted,
        "oracle": case.get("run_status"),
        "task_passed": task_passed,
        "evaluator_assertions_passed": evaluator_assertions_passed,
        "llm_calls": case.get("llm_calls"),
        "estimated_cost_usd": estimated,
        "actual_spend_usd": actual,
        "cost_coverage": coverage,
        "limitations": limitations,
    }


def evaluate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [evaluate_case(c) for c in cases]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate metrics — the benchmark's own honesty check
    (evaluator_assertions) plus the graded outcomes, keeping both visible:
    a green assertion summary says the evaluator judged correctly, which is
    NOT the same as saying the traced runs passed."""
    return {
        "cases": len(results),
        "task_passed": sum(1 for r in results if r["task_passed"]),
        "task_finished": sum(1 for r in results if r["task_finished"]),
        "evaluator_assertions_passed": all(r["evaluator_assertions_passed"] for r in results),
        "candidate_sha": git_revision(),
        "evidence_tier": EVIDENCE_TIER_TRACE_REPLAY,
    }
