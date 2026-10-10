# OVERNIGHT HANDOFF — EWCP Continuous Improvement V2 — 2026-10-11

**Window:** ~19:15 → 21:50 UTC (~2.6h of the 7h budget; stopped early — backlog
exhausted, all remaining work is founder-gated, per spec's "genuinely blocked"
stop condition. No paid APIs, no merges, no deploys, no DNS/Access touches.)
**Ledger:** branch `devin/overnight-quality-20261011` → `.devin/overnight/2026-10-11/`
(backlog.json, progress.md, iterations.jsonl).

## Repo SHAs (before → after)

| Repo | Base @ start | Head @ end | Delta |
|---|---|---|---|
| kernel `main` | `f720e023e6fe` | `f720e023e6fe` | 0 merges; 3 open PRs (#145,#146,#147) |
| product `product/vnext` | `e0ac3aea1ba3` | `e0ac3aea1ba3` | 0 merges; 4 open PRs (#71–#74) |
| deploy `main` | `69f91a320a82` | `69f91a320a82` | 0 merges; #6 held, #7 open |
| erp vendor | `ebc414052ca9` | `ebc414052ca9` | pin unchanged |

All 4 product PRs merge cleanly onto vnext in sequence (tested); merged-stack
ext suite: **395 pass / 1 skip** locally.

## Backlog verdicts

| Item | Verdict | Evidence tier | PR |
|---|---|---|---|
| N00 groundwork | **PASS** | measured | ledger branch `b5973eb6`+ |
| N01 false-success projection | **PASS** | unit+DOM | product #71 |
| N02 offline trace evaluator | **PASS** | postcondition grading, 7/7 | product #71 |
| N03 tool failure taxonomy | **PASS** | 11-class table + 17 bounds tests | product #72 |
| N04 SSE recovery honesty | **PASS** — real dishonesty found+fixed | DOM+api pins | product #73 |
| N05 workrun freshness | **PASS** — real STALE-SILENT gap patched fail-closed | 16 pins | kernel #146 |
| N06 deploy #6 audit | **REVISE** | file:line findings | kernel #145 (PR6_REVIEW.md) |
| N07 ERP bypass threat model | **P0-HOLD** — REAL generic-write bypass measured | live on G1′ synthetic | kernel #145 |
| N08 cost observability nulls | **PASS** — 2 dishonest-zeros fixed | unit | product #74 |
| N09 model qualification dossier | **PASS** — verdict NO-GO (model axis) | docs | kernel #147 |
| N10 clean-env repro sweep | **PASS (PARTIAL repro)** — 12 doc gaps, boot reached w/ 3 guesses | live clean-VM boot | deploy #7 |
| N11 integration review | **PASS** — stack merges clean, 395 tests green | local merge+pytest | this doc |

## Truth-vs-completion (the night's thesis)

Measured "complete" surfaces that were not truthful, each now pinned or fixed:
- Pane showed stale status as settled after mid-run SSE drop → now marks
  `streamLost` (#73). Remaining gap: cross-run `{ms}-{seq}` id collision —
  needs run-namespaced event ids upstream (declined, documented).
- Sealed manifest could silently diverge from workrun rows post-seal →
  persistence funnel now rejects manifested-state mutation; same-hash
  re-seal stays idempotent (#146).
- `provider_eval` fabricated `0` tokens for unmeasured providers →
  `None` + `token_usage_coverage` marker (#74).
- `completed` workruns with missing claimed artifacts render UNKNOWN
  ("Không rõ") not "Đã hoàn thành" (#71).
- **Largest truth gap (unfixed, founder-held):** governed-write bridge is
  advisory, not enforced — generic Frappe REST write with agent creds
  succeeds ungoverned (N07, P0).

## P0 / P1 / P2 alerts

- **P0 — N07:** `POST /api/resource/Purchase Order` with EWCP Write creds →
  200 ungoverned on G1′ (PUR-ORD-2026-00001 kept as synthetic evidence).
  Root cause: role DocPerm `create+write` is what generic REST consumes;
  `hooks.py` has no `doc_events` deny guard. Fix direction RFC'd in
  kernel #145 — trust-boundary decision is founder's.
- **P0 — model axis:** NO-GO. Zero qualified candidates; cheapest unblock
  = `CF_WORKERS_AI_API_TOKEN` + `CF_ACCOUNT_ID` (5-min dashboard action).
- **P1 — deploy #6 (REVISE):** `product:boot` depends on ambient
  `GEMINI_API_KEY`; validator doesn't assert its own two knobs
  (`/register`→403, sandbox isolated) — mutation test fails; README stale.
- **P1 — N10 gaps:** alpha recipe misses submodule step, kernel/product
  checkout+venv recipes, kernel↔ERP env wiring; validator is CWD/env
  fragile (fails from `alpha/` dir, under ambient `EWCP_SEAL_KEY`).
- **P2:** trace_eval crashes on truthy non-dict `cost` (flagged for #71);
  `tool_progress.enabled` defaults off; kernel `_spend_view` can't
  distinguish "no calls" vs "unmetered".

## Verdicts on held items

- **deploy #6:** REVISE (see P1). Wiring itself is correct (register-closed,
  isolated-sandbox wiring verified) — the validator evidence is what's weak.
- **ERP bypass:** CONFIRMED REAL, prevention absent, detection partial.
  Deny-side holds (submit/delete/meta/GL 403, no-auth 403, key-only 401).

## GO/NO-GO per Alpha gate

| Gate | Verdict |
|---|---|
| Governed lanes (model-free) | **GO** — unaffected by model axis |
| Pane/invoke agent lanes | **NO-GO** — no qualified model |
| ERP governed writes E2E | **NO-GO** — bypass P0 unresolved; writes stay OFF (C05) |
| Public /verify | **GO** — redacted + owner-scoped (#139 merged) |
| Clean-env reproducibility | **PARTIAL** — bootable, 3 doc-guided gaps (deploy #7) |
| Persistent host | **BLOCKED-FOUNDER** — Devin box ephemeral (measured) |

## NEXT 5 (recommended order)

1. Founder: merge/review overnight PRs (#71–74, #145–147, deploy #7);
   decide N07 fix direction (doc_events deny guard + narrow Accounts User).
2. Founder: `CF_WORKERS_AI_API_TOKEN` + `CF_ACCOUNT_ID` → run workers_ai
   arm on provider_eval bench (arm already shipped in #69).
3. Rework deploy #6 per REVISE items (validator self-asserts its knobs,
   product:boot without ambient key).
4. DNS delegation → hostname + Access → run `edge_probe.sh` (ready).
5. Persistent Alpha host decision (any small Linux VM suffices).

**No credentials in any artifact. All experiments synthetic G1′ only.**
