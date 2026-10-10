# N08 — Cost/quality observability audit: nulls must never render as zeros

**EWCP Overnight V2 worker W6** — per-surface audit of every place a
cost/token/quality number can be absent, and whether the surface emits
`null`/unknown/unmeasured or silently collapses to `0`/a fabricated
default. Base: `product/vnext` @ `e0ac3aea` (fetched 2026-10-10).
Verdict vocabulary: **honest-null** (absent → `null`/explicit marker),
**honest-zero** (0 emitted only when genuinely measured),
**dishonest-zero** (absent → 0 presented as measured), **unrendered**
(field exists on the wire but no surface displays it).

> ⚠ **Source note.** The task brief assumes `trace_eval` "landed via #71
> on vnext". It has not: PR [#71](https://github.com/hoangtien07/ewcp-product/pull/71)
> is **open, unmerged** (mergeable, base `e0ac3aea`). The evaluator was
> audited read-only at the PR head `fc6f20a9` — no trace_eval tests are
> committed here because the module does not exist on vnext yet; findings
> about it belong to #71.

## Verdicts — product-owned surfaces (on vnext)

| # | Surface | Location | Verdict |
|---|---------|----------|---------|
| 1 | `provider_eval` per-task `input_tokens`/`output_tokens` | `backend/scripts/benchmark/provider_eval/__main__.py` `run_task` result row | **dishonest-zero → FIXED** — a provider that never reports `usage_metadata` emitted `"input_tokens": 0` (accumulators seeded at 0, `usage.get(...) or 0`). Now emits `None` per field when that field was never observed (`:122-128`, `:164-165`). |
| 2 | `provider_eval` summary `total_input_tokens`/`total_output_tokens` | same file, `summarize` (`:188-215`) | **dishonest-zero → FIXED** — totals summed the silent zeros, looking measured. Now aggregate measured rows only, `null` when none, plus `token_usage_coverage` ∈ `measured`/`partial`/`unmeasured` (same vocabulary as trace_eval's `cost_coverage`). **Council rework (W9):** coverage is now measured **per inference call** — `run_task` counts `usage_calls` (calls whose `usage_metadata` reported a token field) and emits `usage_call_coverage` ∈ `full`/`partial`/`none`; `summarize` reads `measured` only when **every** call reported and both fields are non-null on every task (a 2-of-3-calls task reads `partial`, previously `measured` too coarsely). `calls_with_usage` is emitted alongside `total_inference_calls`. |
| 3 | `provider_eval` `estimated_cost` | `__main__.py:195` | **honest-null** — literal `None`, always emitted (correct: no pricing data is ever collected by this harness). |
| 4 | `provider_eval` `inference_calls`, `tool_calls*`, `latency_ms`, `completed`, `correct`, `failure_cause` | `__main__.py:159-168` | **honest** — locally counted/measured by the harness itself; `failure_cause: None` when no failure. |
| 5 | Pane count formatter | `frontend/src/ewcp/labels.ts:246-253` `formatCounts` | **honest-null** — non-number entries (`undefined`/`null`) are dropped, never rendered as `0 <unit>`; a real `0` still renders. Contract pinned by `frontend/tests/unit/ewcp/labels.test.ts` (N08 case). |
| 6 | Pane spend view | `frontend/src/ewcp/api.ts:118-123` `RunView.spend` | **unrendered** — the type declares `tokens`/`usd`/`calls`/`cap_usd` optional but no component consumes the field on vnext; absence, not fabrication. When a consumer is added it must carry the same null discipline (omit unset keys, never `?? 0`). |
| 7 | Pane lifecycle projection | `frontend/src/ewcp/labels.ts:162-204` `lifecycleStatus` | **honest** — unrecognized/terminal-failure states project `UNKNOWN` ("Không rõ"), never a guessed success. |
| 8 | Budget settle path (extension) | `backend/extensions/ewcp-core/ewcp_core/model_policy.py:657-660` | **honest** — missing provider usage settles at the **reservation** (conservative charge, fail-closed accounting), never a 0-token claim. |

## Verdict — unmerged sibling surface (PR #71, read-only)

| # | Surface | Location | Verdict |
|---|---------|----------|---------|
| 9 | `trace_eval` evaluator result keys `llm_calls`, `estimated_cost_usd`, `actual_spend_usd`, `cost_coverage`, `human_accepted` | `backend/scripts/benchmark/trace_eval/evaluator.py` @ `fc6f20a9`: `:166-173` (malformed branch), `:185`, `:202-209` | **honest-null** — every missing field serializes as JSON `null` (`dict.get`, never `or 0`); `cost_coverage` is the explicit enum `none`/`measured`/`estimated_only`; `human_accepted` is `null` unless an event recorded it. Fixtures exercise the `cost: {estimated_usd: null, actual_usd: null}` path. **Robustness note (not a dishonest zero):** `cost` present as a truthy non-dict (e.g. `"n/a"`) crashes `evaluate_case` on `.get` (`:171-172`, `:207-208`) — worth a guard on #71, not patched here. |

## Verdict — kernel-owned (sibling repo, audited read-only — no kernel changes per task rules)

| # | Surface | Location | Verdict |
|---|---------|----------|---------|
| 10 | Kernel run-view `spend` | `src/ewcp/api/app.py:1126-1135` `_spend_view` | **honest-by-ledger, ambiguous on empty** — emits ledger sums `{tokens: 0, usd: 0.0, calls: 0}` when the ledger is empty: literally true for "zero settled calls", but cannot distinguish "no calls" from "calls that bypassed metering". The pane marks every `spend` field optional (`api.ts:118-123`), so a future renderer must treat an all-zero spend block as *possibly unmetered*, not free. Kernel-owned — flagged, not patched. |

## Verdict — upstream deer-flow (audited, out of patch scope)

| # | Surface | Location | Verdict |
|---|---------|----------|---------|
| 11 | Console API cost fields (`total_cost`, per-run `cost`, per-day `cost`) | `backend/app/gateway/routers/console.py:55`, `:73`, `:92`, `:100`, `:112`; `_run_cost` `:245-276` | **honest-null** — `cost: null` when unpriced or when any call carries the `missing_usage_calls` journal marker (`backend/packages/harness/deerflow/runtime/journal.py:1202`; detected at `console.py:230-236`, applied at `:255`, `:328`, `:498`). |
| 12 | Console token totals (`total_*_tokens`) | `console.py:277-278` (`int(... or 0)`), `persistence/run/sql.py:596-600`, `persistence/run/model.py:40` | **schema-zero (upstream)** — `int NOT NULL DEFAULT 0` end-to-end; cannot express unmeasured. Mitigated upstream-side by the `missing_usage_calls` marker inside `token_usage_by_model` — consumers can detect unmetered usage even though headline totals read 0. |
| 13 | Workspace token-usage adapter | `frontend/src/core/threads/token-usage.ts:22-26` (`?? 0`) | **upstream** — response schema declares the totals required `number` (`types.ts:85-91`), so `?? 0` only guards a malformed payload; the UI shows backend totals verbatim and drops the `missing_usage_calls` distinction. Upstream-owned — not patched. |
| 14 | `context_snapshot` token report | `backend/scripts/benchmark/context_snapshot/report.py:7-34` | **honest-null** — per-row `complete` flag requires all three token fields as ints; `mean_total_tokens` is `None` when usage is incomplete and `observed_total_tokens` sums measured rows only. |

## Verdict — eval docs (metrics tables)

| # | Surface | Location | Verdict |
|---|---------|----------|---------|
| 15 | GP-01 task-B cost decomposition | `docs/vnext/GP01_TASKB_COST.md` | **honest** — unmeasured cells marked `unmeasurable`, `(not isolated)`, `—`; inferences tagged `[suy luận]` (`:136-146`). |
| 16 | GP-01 spend caveat | `docs/vnext/GP01_EVAL.md:20` | **honest** — explicitly separates ledger list-price estimate (`usd_per_1k_tokens: "0.004"`) from actual billed spend ($0 free tier) — the exact `estimated_cost_usd` vs `actual_spend_usd` class this item guards. |
| 17 | GP-01 re-eval run table | `docs/vnext/GP01_REEVAL.md:52` | **honest** — `—` marks the absent map row rather than a zero. |

## What changed (IMPLEMENTATION)

- `backend/scripts/benchmark/provider_eval/__main__.py` — rows 1–2 above:
  per-field usage observation; totals over measured rows only;
  `token_usage_coverage` enum added to `summarize`.
- No other code touched — every other product surface audited was already
  honest (verdicts 3–8).

## Tests (RED first, then patch)

- `backend/tests/test_bench_provider_eval.py::TestCostObservability` — 4
  cases: unmetered task → `input_tokens`/`output_tokens` `is None` (RED:
  was `0`); unmetered summary → totals `is None` + `coverage ==
  "unmeasured"` (RED: was `0`); metered task/summary stay measured +
  `coverage == "measured"` (RED: key missing); mixed → totals aggregate
  measured only + `coverage == "partial"` (RED).
- `frontend/tests/unit/ewcp/labels.test.ts` — N08 case: `formatCounts`
  drops `undefined` counts instead of fabricating `0` (GREEN on base —
  pins the already-honest pane contract).

Run: `cd backend && .venv/bin/python -m pytest tests/test_bench_provider_eval.py -q`
(11 passed) and `cd frontend && pnpm rstest run tests/unit/ewcp/labels.test.ts`
(16 passed).
