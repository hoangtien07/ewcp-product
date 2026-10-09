# GP-01 — General-Purpose Breadth Evaluation (measured)

**Date:** 2026-10-09 · **Gate source:** EWCP master plan, gate GP-01
**Stack:** ewcp-product `product/vnext@0351f24f` + enterprise-work-control-plane `main@5a79f8f`
**Lane under test:** the general/exploratory chat lane — `POST /api/ewcp/runs` with `task_mode=general` (the exact call the `/ewcp` exploratory pane makes), plus the native `POST /api/threads/{tid}/runs` for follow-up turns (the exact call the chat UI makes).
**Models used:** `gemini-3.8-flash` → exhausted free-tier daily quota (20 req/day); `gemini-3.5-flash-lite` → per-minute input-token cap (250k tok/min). `OCP_SOLVER_API_KEY` probed and rejected by Google (`API key not valid`); `codex-pool` not present on the box.

## Gate text (verbatim)

> "GP-01 — general-purpose breadth: Given heterogeneous Excel/CSV/docs and an unfamiliar request (not a pre-coded outcome pack), the agent uses native tools, diagnoses input issues, creates a correct workbook/report, responds to one correction and makes final files retrievable. Measure completion, intervention, time, spend. Do not force a pack, acceptance contract, approval or seal on a normal exploratory query."

## Method

- Kernel: `EWCP_REQUIRE_AUTH=1`, tenant key `ewcp-demo-7f3a`, `EWCP_STORE_DIR=var/ewcp-gp01`, `EWCP_LLM_MODEL=gemini-3.5-flash-lite`.
- Gateway: `DEER_FLOW_AUTH_DISABLED=1`, `EWCP_KERNEL_URL=http://127.0.0.1:8080`.
- Fixtures generated locally in `/home/ubuntu/gp01_fixtures/` (not committed): `sales_messy.csv` (mixed date formats, impossible date `2025-13-45`, duplicated `ORD-002`, pasted `TOTAL` row mid-data, `$2,345.00`-style amounts, missing rep), `inventory.xlsx` (3 sheets; duplicate `ITM-02` key with divergent quantities, orphan `ITM-04` movement, `not a date`, `2025-02-30`), `memo.docx` + `costs.csv` (memo asks for a report; `hosting_subtotal` states 600.00 while its items sum to 508.50).
- Measurement harness: `run_task.py` posts multipart `intent`+`files` to `/api/ewcp/runs`, polls `GET /api/ewcp/runs/{er}?refresh=1` to terminal, then reads thread messages, workspace files on the host (`…/threads/{tid}/user-data/`), and kernel `budget_admissions`/`ledger` rows keyed by the agent `run_id`.
- Spend source: kernel `ledger`/`budget_admissions` (every general-lane model call is admitted/settled through the kernel budget API by `ewcp_core.model_policy.awrap_model_call`). Ledger USD is priced at the extension's configured `usd_per_1k_tokens: "0.004"` — a list-price estimate. Real billed spend this session was $0 (Google free tier); the ledger is the attribution mechanism, not an invoice.

## Operator interventions applied (recorded as interventions, not code changes)

| # | Intervention | Why | Config change (gitignored `config.yaml`) |
|---|--------------|-----|------------------------------------------|
| I1 | `recursion_limit: 100 → 1000` | Stock default kills runs on tool-friction loops (see F1). The web UI already sends `recursion_limit: 1000` on every run it launches (`frontend/src/core/threads/hooks.ts`); `/api/ewcp/runs` has no such field, so pane-launched runs get the config default. | top-level `recursion_limit` |
| I2 | `sandbox.allow_host_bash: false → true` | `LocalSandboxProvider` ships with host bash disabled (documented secure default). With no execute tool the agent cannot transform files and fabricates instead (see F2). Documented opt-in for "fully trusted, single-user local workflows" — which is what this eval box is. | `sandbox.allow_host_bash` |
| I3 | Model swap `gemini-3.8-flash → gemini-3.5-flash-lite` | 3.8-flash free-tier daily quota exhausted (20 req/day, `Please retry in 10h`). Same `GEMINI_API_KEY`, different model = separate quota bucket. | `models[0].model` |

## Task matrix (measured)

| Task | Final outcome | Run status | Wall time | LLM calls | Tokens | Spend (ledger) | Interventions |
|------|---------------|------------|-----------|-----------|--------|----------------|---------------|
| **A** messy CSV → cleaned workbook + summary | Deliverables produced and **correct** | `failed` (RPM 429 at end) | 56 s | 22 | 370,585 | $1.4823 | I1+I2 applied; prior attempts see below |
| **D** correction round on A (same thread) | **Complete + correct** | `success` | ~32 s | 6 | 172,488 | $0.6900 | one follow-up message |
| **B** multi-sheet Excel diagnosis | **Complete + correct** (after rate-limit cut) | `failed`→continue `success` | 24 s + 16 s | 6+4 | 136,597 | $0.5464 | one "continue" message after 75 s wait |
| **C** memo + costs → report | **Complete + correct** | `completed` | 16 s | 7 | 89,757 | $0.3590 | none |

Failed/aborted attempts (counted honestly, not hidden):
- `task_a#1` (3.8-flash, stock config): `GraphRecursionError` at 344 s — skill-path probe loop during 503 storms. 7 calls, 105,112 tok, $0.4204.
- `task_a#2` (3.8-flash retry): 429 `RESOURCE_EXHAUSTED` — daily quota gone. 2 calls, $0.0974.
- `task_a2` (3.5-flash-lite, still recursion_limit=100): `GraphRecursionError` at 24 s — `write_file`→exists-block→`read_file`→`write_file` loop. 7 calls, $0.4322.
- `task_a3` (3.5-flash-lite, recursion_limit=1000, bash still disabled): run `completed` in 32 s but **fabricated** — wrote 10 `.py` scripts into workspace, never executed, wrote a summary claiming `september_orders_cleaned.xlsx` exists; it did not. 13 calls, $0.7885.
- `task_d` (correction on task_a3, bash still disabled): `success` but fabricated again — updated the summary, still no xlsx. 9 calls, $0.7874.

## Deliverable verification (files on disk + retrievability)

| File | Exists | Opens | Content correct | Retrievable via API |
|------|--------|-------|-----------------|---------------------|
| `outputs/September_Orders_Cleaned.xlsx` (task A thread `ef22ce45…`, regenerated by D) | yes (6,063 B) | yes (openpyxl) | All 9 original rows kept; real `datetime` dates; ORD-004 fixed to 2025-09-04 with `review_flag`; both ORD-002 rows present, dup flagged; pasted TOTAL kept and flagged; missing rep flagged; `=SUM()` totals row | `GET /api/threads/{tid}/artifacts/mnt/user-data/outputs/September_Orders_Cleaned.xlsx` → 200 |
| `outputs/cleaning_summary.md` | yes (4,091 B) | yes | Accurate per-issue description matching actual fixes | 200 |
| `outputs/inventory_diagnosis.md` (task B thread `7f643d01…`) | yes (5,550 B) | yes | Found all 4 seeded quirks: dup `ITM-02` (45 vs 47), orphan `ITM-04`, `not a date`, `2025-02-30`, with sheet/row refs | 200 |
| `outputs/q3_operations_summary_report.md` (task C thread `eb8aa4aa…`) | yes (2,307 B) | yes | Flags `hosting_subtotal` $600.00 vs computed $508.50 (–$91.50); software subtotal correct; corrected total $928.50 | 200 |

## Anti-goal verification — general lane stays pack-free

Measured after 8 extension-launched general runs + 3 native follow-up runs:

- Kernel store `var/ewcp-gp01/runs.db`: `workruns=0, bundles=0, attempts=0, manifests=0, decisions=0, idempotency_keys=0, metric_events=0, demand_intents=0, acceptance_contracts=0, schedules=0`. **No governed machinery engaged.**
- `ewcp_execution_runs` (product DB): 8 rows, all `task_mode='general'`, all `workrun_id=NULL`. The map row exists for status projection (`/ewcp-runs` surface) — it does not create a kernel workrun.
- What DOES engage on a general run: product thread + `lead_agent` run via the AgentRuns grant, budget admission/settlement through the kernel budget API (`budget_admissions`/`ledger`, 85 ledger rows total, $5.69 list-priced), egress policy check (local_only), the `ewcp_capabilities`/`ewcp_invoke` consumer tools.
- Follow-up turns via `POST /api/threads/{tid}/runs` create no `ewcp_execution_runs` row — expected: they were not launched through the extension; they still wrote kernel ledger rows via the budget middleware.

## Findings

- **F1 — recursion ceiling gap between UI and extension lane.** Chat UI sends `recursion_limit: 1000` per run; `/api/ewcp/runs` (exploratory pane) does not forward any override, so pane runs get `recursion_limit: 100` and die on modest tool loops (observed twice: 344 s and 24 s). *Suggested fix:* extension forwards a configurable default (e.g. 1000) matching the UI. **Fixed** — run admission now goes through a request-scoped `HttpRunStarter` seam on `POST /api/threads/{id}/runs` carrying `config.recursion_limit` (`general_recursion_limit`, default 1000) on launch AND resume; the bound `AgentRuns` contract cannot carry run config, so this stays extension-side on the same route the UI uses.
- **F2 — no-execute configs invite fabrication on lite models.** With `allow_host_bash: false`, gemini-3.5-flash-lite twice produced a run that reported success while the deliverable file did not exist (wrote scripts + claimed execution). This is a model behavior, not a product bug — but it means the GP-01 "correct workbook" criterion silently fails in secure-default local deployments unless an operator enables host bash or provisions a real sandbox. Worth a gate note: file-producing tasks need an execution-capable sandbox config.
- **F3 — transient provider rate limits terminate runs.** Two runs died on 429 input-tokens/min with `retryDelay` of 6–15 s; the run went straight to `error` instead of backing off (model `max_retries: 2` did not cover quota errors). User-side "continue" message recovered cleanly, so recovery is one message — but the run record still shows `failed`. **Fixed** — `TransientRetryPolicy` inside `BudgetAdmissionMiddleware.awrap_model_call` retries 429s in-band honoring the provider delay hint (bounded `max_attempts`, jitter, hard-quota/non-429 errors never retried, waits via `asyncio.sleep` off the event loop).
- **F4 — free-tier quota shape.** gemini-3.8-flash: 20 requests/day/model. gemini-3.5-flash-lite: 250k input tokens/min. Both were hit during a 4-task eval; production use needs a paid project or a slower pace. `OCP_SOLVER_API_KEY` is not a Gemini key (400 INVALID_ARGUMENT) — documented as a non-fallback.
- **F5 — spend attribution works.** Every LLM call in the general lane passes through kernel `budget_admissions` (reserve→settle) keyed by agent `run_id`, even though `workrun_id` is null. Ledger `usd` uses the configured list price ($0.004/1k), so it measures attributable usage, not actual billed cost ($0 free tier here). Cap `5.00` is per-run scope — total eval usage $5.69 across runs did not trip it.

## Verdict: **GAP**

Against the gate criteria:

- Uses native tools: **yes** (read/write/bash/present_files), once `allow_host_bash` is on.
- Diagnoses input issues: **yes** — B caught all four seeded defects with correct row refs; A's summary enumerates every real quirk.
- Creates correct workbook/report: **yes after interventions** — A's xlsx verified cell-by-cell; C flags the $91.50 subtotal discrepancy precisely. **Not achievable as-shipped** (no exec tool → fabrication; recursion_limit=100 → loops kill runs).
- Responds to one correction: **yes** — task D follow-up applied keep-all-rows + `review_flag`/`review_reason` correctly in one turn.
- Final files retrievable: **yes** — artifacts API returns 200 with real bytes for all deliverables.
- No forced pack/acceptance/seal: **yes — verified empirically**, all governed kernel tables empty across 11 runs.
- Measures completion/interventions/time/spend: **yes** (this document + kernel ledger).

Gaps keeping it from a clean PASS: F1 (pane doesn't inherit the UI's recursion budget — a real defect candidate), F2 (secure default without an execution path makes file-deliverable tasks report false success), F3 (rate-limit = terminal run error, not retry).
