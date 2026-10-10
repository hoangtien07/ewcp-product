"""Offline tests for the trace_eval acceptance evaluator (N02).

Deterministic canned traces only — no network, no credentials, no provider,
no live model. The evaluator grades the POSTCONDITION recorded in the trace
(artifact exists + valid / acceptance verified), never ``status=success``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from benchmark.trace_eval.evaluator import (  # noqa: E402
    EVIDENCE_TIER_TRACE_REPLAY,
    REQUIRED_RESULT_KEYS,
    evaluate_case,
    evaluate_cases,
    load_cases,
)

FIXTURES = (
    Path(__file__).parent.parent / "scripts" / "benchmark" / "trace_eval" / "fixtures"
)


@pytest.fixture(scope="module")
def fixture_results():
    cases = load_cases(FIXTURES / "cases.jsonl")
    return {r["case_id"]: r for r in evaluate_cases(cases)}


def test_committed_fixtures_cover_required_shapes(fixture_results):
    """The committed fixture set must include ≥1 holdout success and ≥1
    holdout failure control (spec N02)."""
    ids = set(fixture_results)
    assert "general_text_only_answer" in ids  # holdout success (general)
    assert "governed_sealed_workrun" in ids  # holdout success (governed)
    assert "general_produced_empty_artifact" in ids  # failure control
    assert "general_required_artifact_missing" in ids
    assert "general_forged_completion_claim" in ids
    assert "general_sha_mismatch" in ids
    assert "governed_pending_approval" in ids


def test_result_schema_complete(fixture_results):
    for case_id, row in fixture_results.items():
        assert REQUIRED_RESULT_KEYS <= set(row), f"{case_id} missing keys"
        assert row["evidence_tier"] == EVIDENCE_TIER_TRACE_REPLAY


def test_oracle_status_does_not_grant_pass(fixture_results):
    """The core N02 property: oracle run_status 'success' with a missing or
    invalid artifact must grade task_passed=false."""
    for case_id in (
        "general_required_artifact_missing",
        "general_forged_completion_claim",
        "general_produced_empty_artifact",
        "general_sha_mismatch",
    ):
        row = fixture_results[case_id]
        assert row["oracle"] == "success", case_id
        assert row["task_passed"] is False, case_id


def test_holdout_success_controls_pass(fixture_results):
    assert fixture_results["general_text_only_answer"]["task_passed"] is True
    assert fixture_results["governed_sealed_workrun"]["task_passed"] is True


def test_pending_approval_is_not_complete(fixture_results):
    row = fixture_results["governed_pending_approval"]
    assert row["task_finished"] is False
    assert row["task_passed"] is False
    assert row["human_accepted"] is None  # no event — never inferred


def test_evaluator_assertions_all_pass_on_committed_fixtures(fixture_results):
    """Every committed fixture's expected verdict matches — a wrong trace
    grades task_passed=false AND evaluator_assertions_passed=true; the
    golden controls grade task_passed=true."""
    bad = [
        cid for cid, r in fixture_results.items()
        if r["evaluator_assertions_passed"] is not True
    ]
    assert bad == []


def test_wrong_trace_fails_task_but_passes_evaluation(tmp_path):
    """Spec invariant, directly: a wrong trace -> task_passed=false AND
    evaluator_assertions_passed=true; a golden trace -> task_passed=true."""
    wrong = {
        "case_id": "wrong",
        "lane": "general",
        "provider": "p",
        "run_status": "success",
        "terminal": True,
        "required_artifacts": [{"path": "outputs/x.md", "sha256": "a" * 64}],
        "produced_artifacts": [],
        "contract": None,
        "human_acceptance": None,
        "expect": {"task_passed": False},
    }
    golden = {
        "case_id": "golden",
        "lane": "general",
        "provider": "p",
        "run_status": "success",
        "terminal": True,
        "required_artifacts": [{"path": "outputs/x.md", "sha256": "a" * 64}],
        "produced_artifacts": [
            {"path": "outputs/x.md", "sha256": "a" * 64, "size": 10}
        ],
        "contract": None,
        "human_acceptance": None,
        "expect": {"task_passed": True},
    }
    rw, rg = evaluate_case(wrong), evaluate_case(golden)
    assert rw["task_passed"] is False and rw["evaluator_assertions_passed"] is True
    assert rg["task_passed"] is True and rg["evaluator_assertions_passed"] is True
    # artifact_observed is the discriminating postcondition field
    assert rw["artifact_observed"] is False and rg["artifact_observed"] is True


def test_sha_mismatch_and_empty_are_invalid():
    for produced in (
        [{"path": "o/x", "sha256": "b" * 64, "size": 5}],  # wrong digest
        [{"path": "o/x", "sha256": "a" * 64, "size": 0}],  # empty file
        [{"path": "o/x", "size": 5}],  # undigestable — cannot verify
    ):
        row = evaluate_case(
            {
                "case_id": "c",
                "lane": "general",
                "provider": "p",
                "run_status": "success",
                "terminal": True,
                "required_artifacts": [{"path": "o/x", "sha256": "a" * 64}],
                "produced_artifacts": produced,
                "contract": None,
                "human_acceptance": None,
                "expect": {"task_passed": False},
            }
        )
        assert row["artifact_observed"] is False
        assert row["task_passed"] is False


def test_unsealed_contract_fails_acceptance():
    row = evaluate_case(
        {
            "case_id": "c",
            "lane": "governed",
            "provider": "p",
            "run_status": "verified",
            "terminal": True,
            "required_artifacts": [{"path": "d/x", "sha256": "a" * 64}],
            "produced_artifacts": [{"path": "d/x", "sha256": "a" * 64, "size": 3}],
            "contract": {"verdict": "pass", "sealed": False},
            "human_acceptance": None,
            "expect": {"task_passed": False},
        }
    )
    assert row["artifact_observed"] is True
    assert row["acceptance_verified"] is False
    assert row["task_passed"] is False


def test_human_accepted_never_inferred():
    row = evaluate_case(
        {
            "case_id": "c",
            "lane": "general",
            "provider": "p",
            "run_status": "success",
            "terminal": True,
            "required_artifacts": [],
            "produced_artifacts": [],
            "contract": None,
            "human_acceptance": None,
            "expect": {"task_passed": True},
        }
    )
    assert row["human_accepted"] is None
    row2 = evaluate_case(
        {
            "case_id": "c2",
            "lane": "general",
            "provider": "p",
            "run_status": "success",
            "terminal": True,
            "required_artifacts": [],
            "produced_artifacts": [],
            "contract": None,
            "human_acceptance": {"approved": True},
            "expect": {"task_passed": True},
        }
    )
    assert row2["human_accepted"] is True


def test_load_cases_skips_blank_and_comment_lines(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text(
        '{"case_id": "a", "lane": "general", "provider": "p", "run_status": "success",'
        ' "terminal": true, "required_artifacts": [], "produced_artifacts": [],'
        ' "contract": null, "human_acceptance": null, "expect": {"task_passed": true}}\n\n'
        '{"case_id": "b", "lane": "general", "provider": "p", "run_status": "failed",'
        ' "terminal": true, "required_artifacts": [], "produced_artifacts": [],'
        ' "contract": null, "human_acceptance": null, "expect": {"task_passed": false}}\n'
    )
    assert [c["case_id"] for c in load_cases(p)] == ["a", "b"]


def test_cli_run_writes_results(tmp_path):
    from benchmark.trace_eval.__main__ import main

    out = tmp_path / "results.json"
    rc = main(
        [
            "run",
            "--traces",
            str(FIXTURES / "cases.jsonl"),
            "--output",
            str(out),
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["summary"]["evaluator_assertions_passed"] is True
    assert len(payload["cases"]) == len(fixture_ids())


def fixture_ids():
    return {c["case_id"] for c in load_cases(FIXTURES / "cases.jsonl")}
