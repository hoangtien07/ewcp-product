# trace_eval — offline acceptance evaluator (N02)

Grades canned run traces against the **postcondition**, never the run's own
`status=success` claim. Deterministic: no network, no provider, no live
model, no credentials. Sibling of `provider_eval` (that harness measures
live arms; this one replays recorded facts — the `TRACE_REPLAY` evidence
tier of the acceptance schema).

## Commands (run from `backend/`)

```bash
uv run python -m scripts.benchmark.trace_eval run                # committed fixtures → stdout
uv run python -m scripts.benchmark.trace_eval run --traces path/to/cases.jsonl --output results.json
cd backend && uv run pytest tests/test_bench_trace_eval.py -q   # offline suite
```

`run` exits non-zero when any `evaluator_assertions_passed` is false.

## Case format (one JSON object per JSONL line)

| key | meaning |
| --- | --- |
| `case_id` | stable fixture id |
| `lane` | `general` \| `governed` \| `invoke` |
| `provider` | recorded provider name or `synthetic-trace` |
| `base_sha` | repo revision the trace was recorded against |
| `run_status` | the run's self-reported status — the **oracle claim**, graded against, never trusted |
| `terminal` | whether the run is finished |
| `required_artifacts` | `[{path, sha256?}]` — the acceptance deliverables |
| `produced_artifacts` | `[{path, sha256?, size}]` — what the trace recorded on disk |
| `contract` | `null` for the general lane; `{verdict, sealed}` for contract-bound lanes |
| `human_acceptance` | `null` or `{approved: bool}` — an explicit event; never inferred |
| `llm_calls`, `cost` | recorded counts/spend when the trace has them |
| `limitations` | per-case caveats carried into the result row |
| `expect.task_passed` | the declared verdict — the evaluator's own oracle |

## Grading

`artifact_observed` — every required artifact produced at the same path,
`size > 0`, digest-equal when the requirement declares a `sha256`.
`acceptance_verified` — contract-bound lanes: recorded verdict `pass` AND
`sealed`; general lane: equals `artifact_observed` (no contract exists).
`task_passed` = `task_finished` AND `artifact_observed` AND
`acceptance_verified`. `evaluator_assertions_passed` — computed verdict ==
`expect.task_passed`.

Result rows emit the full spec schema including `evidence_tier:
TRACE_REPLAY`, `candidate_sha` (the repo revision being graded), and
`cost_coverage` (`none` \| `estimated_only` \| `measured`).

## Committed fixtures

`fixtures/cases.jsonl` — synthetic, self-identifying (`provider:
synthetic-trace`). Covers the N01 false-success fixture set: required-but-
missing artifact, forged completion claim, legit text-only answer (must
NOT fail), produced-but-invalid (empty + sha mismatch), pending approval
(not complete), sealed WorkRun (genuine). ≥1 holdout success +
≥1 holdout failure control.

## Honest limits

Trace replay grades **recorded facts** — a trace that omits its own events
is undetectable at this tier. Content-level fabrication (a produced file
with plausible-looking wrong content and a matching declared digest) is
equally outside it. Higher tiers (`RUNTIME_E2E`, `LIVE_MODEL`) exist in
the schema for probes that execute real code or real providers.
