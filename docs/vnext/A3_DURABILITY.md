# A3 Durability — scope, semantics, repro

**Ngày:** 2026-10-08. **Phạm vi:** Task 3 của
`docs/plans/2026-10-08-a3-product-core-port.md` — `ewcp_core/recovery.py`
+ `GET /api/ewcp/runs` + `POST /api/ewcp/runs/{id}/resume`.

## Scope — stated plainly

**Foreground recovery ONLY.** The trigger is a new authenticated request:
the user logs back in / reconnects → `resolve_agent_runs(request)` binds a
fresh `AgentRuns` handle → `for_plugin("ewcp.core")` namespaces it →
`recover()` reconciles `ExecutionRunMap` rows against live thread truth.

**There is no durable autonomous background execution in A3.** The host
`bind()` accepts only SESSION and AUTH_DISABLED auth sources —
`backend/app/gateway/extension_agent_runs.py:51-61` excludes PATs and
internal/service identities, so the extension never holds a credential
outside a user request. A run whose user never returns has no orchestrator:
the `ExecutionRunMap` row persists (it owns the outcome), but nothing
drives reconcile/resume until a request arrives. Scheduler/repair-loop
durability needs an upstream system-identity contract — that gap is a
separate follow-up issue, not claimed here.

Runs themselves DO survive a client disconnect: the bound impl admits
every start/resume with `on_disconnect="continue"`
(`extension_agent_runs.py:142`), and thread state lives in the LangGraph
checkpointer — foreground recovery is what reconnects the user to that
surviving work.

## Reconcile semantics (what is trusted)

| Fact | Source | Trusted as |
|---|---|---|
| Run status | `runs.get(thread_id, run_id)` — run-id lookup | authoritative per run id |
| Pending input | `runs.get_state(thread_id)` — mutable thread state | ONLY to flag `interrupts`/`next` on an ENDED run (`ENDED_RUN_STATUSES`); never a terminal snapshot |
| Event history | SSE `GET /api/threads/{t}/runs/{r}/join` (`observation_url`) | live replay for observers — not read by the map |

Terminal projection = run.status ended AND no pending thread state — a
single `get_state` snapshot is never treated as result truth, and the map
stores ids + a status projection, never event payloads.

Owner scoping: `created_by` = `resolve_principal(request).user_id`;
cross-owner rows answer 404 (no existence oracle).

## Guards

- **dup-start deny** (`assert_can_start`): an OPEN map row
  (`launching`/`running`/`pending_interrupt`) on the same thread, or an
  idempotency key already spent on a different intent, raises
  `OpenRunConflict` carrying the conflicting row (rejoin via
  `observation_url`). Same key + same intent is a replay — `launch()`
  converges it, no conflict.
- **revoke-before-resume**: `resume()` reads `get`/`get_state` through the
  bound handle BEFORE `runs.resume()` — the host re-checks session auth
  (`load_user` + `token_version` + permission intersect) on every op, so a
  revoked/demoted session raises `RecoveryDenied` (HTTP 403) before any
  mutation.
- **pending-interrupt flag**: ended run + pending thread state projects
  `pending_interrupt`, never `completed` (same rule as the launcher's
  `refresh`).

## Repro scenarios (all green)

Run: `cd backend && .venv/bin/python -m pytest extensions/ewcp-core/tests -q`

| # | Scenario | Test | Expected → measured |
|---|---|---|---|
| 1 | disconnect → rejoin SSE | `test_rejoin_returns_sse_join_url_for_open_run` | open run → `observation_url` = join route on run id; status stays `running` → ✓ |
| 2 | restart → foreground recovery | `test_restart_reconcile_projects_thread_truth` | fresh handle over persisted map: finished→`completed`, success+pending→`pending_interrupt`, `run_id NULL`→`launching` (owns pending launch), dead-final not re-polled → ✓ |
| 3 | dup-start deny | `test_dup_start_denied_when_thread_has_open_run`, `…_for_pending_interrupt`, `…_when_key_spent_on_different_intent`, `test_dup_start_allows_replay_and_closed_thread` | `OpenRunConflict` with the open row / spent key; replay + closed thread allowed → ✓ |
| 4 | revoked-auth deny | `test_revoked_auth_denies_recover_without_mutation`, `test_revoked_auth_denies_resume_before_mutating` | `RecoveryDenied`; map unchanged; `runs.resume` never issued → ✓ |
| 5 | interrupt → resume | `test_resume_rebinds_run_and_marks_running` (+ denial variants) | explicit `resume` payload + idempotency key forwarded; new upstream run id rebinds the map row → `running` → ✓ |

Route-level coverage (TestClient + stubbed resolvers):
`GET /api/ewcp/runs` → 401 without principal, 503 without bound handle /
store, 200 reconcile+list; `POST …/resume` → 404 other-owner, 409
not-pending (map repaired on the way out), 200 resume.

## Live E2E results (Task 8)

Các kịch bản trên được đo lại end-to-end trên stack thật (kernel HTTP
@ 9d26e54 + gateway + frontend + Gemini live): bảng kết quả, repro
commands, boot recipe clean-checkout, bugs đã fix trong quá trình proof
và limitations đầy đủ — xem `docs/vnext/A3_SMOKE.md`.

- restart: `SIGKILL` gateway giữa run → map row reconcile `failed` sau
  relaunch; các row khác giữ nguyên projection.
- interrupt/resume: `cancel?action=interrupt` → `pending_interrupt` →
  `POST /runs/{id}/resume` → run mới cùng thread → `completed`.
- egress `local_only` deny: tenant `default` sensitive → model call deny
  fail-closed, run completed với denial message (không provider call).
- budget: `cap_usd` nhỏ → kernel `POST /budget/admissions` 402 → deny
  trước provider call, không account/reservation nào đổ bộ.

## Limitations — explicit

1. **No background/service recovery.** No PAT, no internal creds, no
   scheduler-driven reconcile — `bind()` excludes them by design. If
   durable autonomous execution is required (repair loops, scheduled
   continuation), it needs a NEW upstream contract for system-identity
   binding on `AgentRuns`; track as a separate issue, do not work around.
2. **Reconcile is pull-on-request.** Rows only refresh when their owner
   makes a request; a run can sit `running`/`pending_interrupt` in the map
   indefinitely in between.
3. **Crash mid-launch** leaves a `launching` row with `run_id NULL` —
   reconcile reports it; forward progress needs the caller to replay
   `launch()` with the same idempotency key (host dedupe converges).
4. **Deleted threads** reconcile to `failed` — a gone thread can never
   resume or produce evidence.
5. No SSE observer is bundled — `observation_url` is emitted for callers
   to join; the frontend surface is Task 6.
