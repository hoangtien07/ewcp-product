# A7A — Product Reliability Matrix (auth-on workbench, browser/HTTP E2E)

Date: 2026-10-10. Worker B (Product Reliability), branch `product/vnext`.
Scope: recovery behaviors NOT already proven by
[A7A_SSE_RECONNECT.md](A7A_SSE_RECONNECT.md) (server-side SSE resume/gap, 9/9)
and [A7A_AUTH_ON_ISOLATION.md](A7A_AUTH_ON_ISOLATION.md) (52-probe isolation).
Everything below is a measured request, stream capture, or browser screenshot
on the real auth-on stack — no stubs.

## Bench

- kernel `ewcp.api.app` on :8080, `EWCP_REQUIRE_AUTH=1`, tenant key +
  `EWCP_SEAL_KEY` (Option A+ wired path — `_status` reports
  `kernel_configured/api_key_configured: true`).
- gateway `app.gateway.app:app` on :8001 (auth ON — real AuthMiddleware + JWT),
  frontend `pnpm dev` :3000, nginx :2026.
- Model: Ollama `qwen2.5:0.5b`/`1.5b` local-only (no Gemini). Governed lane is
  deterministic — needs no LLM.
- Browser: real Chrome via CDP (:29229 alice / :29230 bob), session-cookie
  injection (login API is form-encoded `username`+`password` →
  `access_token`+`csrf_token` cookies; Playwright UI login did not complete —
  cookie jar injection used, same-session truth).
- Extra knob for S3: `stream_bridge.queue_maxsize: 4` in `config.yaml`
  (gitignored, local-only — shrinks the retained window so eviction is
  reachable inside short test runs).

## Matrix

| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 1 | Hard reload + close/reopen tab **mid-run** (general) | **PASS** | `er_12914cf9`, `er_c1dc06cc`: mount at srv=`running` → `Đã tiếp nhận · trực tiếp` + event feed; hard reload → fresh `/join` 200, live state kept; close+reopen at srv=`running` → JOIN#2 200, feed resumes from retained buffer. Post-settle: `Agent hoàn tất` + `?refresh=1` reconciliation fired by stream `finally`. Measured: pane never sends `Last-Event-ID` (request header `null` on all joins). |
| 2 | Gateway kill `:8001` mid-run → restart | **PASS with gap (recorded)** | Down-phase: `:8001` dead, nginx `/api` → 502, web shell 200. **General lane** `er_f9a814ce`: record persisted stale `running` in sqlite; `GET /runs/{er}?refresh=1` reconciles to `failed`; pane remount → join gets `{"detail":"Run ... is not active on this worker"}` → finally-refresh → honest `Không rõ` (failed), no phantom retry. The in-process LangGraph run is unrecoverable by design — the failure surfaces honestly, which is the required behavior. **Governed lane** `wr-a3373208c638` (`er_cd61286d`): kernel is a separate process — workrun reached `candidate_complete` *while gateway was down*; post-restart `/runs/{er}/workrun` returns full truth (4 deliverables, decision question) and the pane renders the gate. Durable-state recovery PASS. |
| 3 | Stale SSE cursor / evicted buffer — **browser side** | **PASS + recorded limits** | Server contract re-verified on `er_9d25c1d0` (maxsize 4): well-formed stale cursor `…-1` below watermark mid-run → `event: gap` + `{"code":"stream_replay_gap","recovery":"reload_durable_state"}`. Malformed/non-`{ts}-{seq}` cursor → silently clamped to earliest retained (no gap) — code-verified `memory.py:_resolve_start_offset`. Browser path: pane never sends a cursor, so every remount joins fresh → gets only the retained tail (measured: first retained event seq `-5` while pane joined at seq 1..4 range earlier) — **silent truncation, no gap indicator**. Post-completion the buffer is torn down: fresh join returns only `end`, and a remounted pane shows **zero** activity events (feed is ephemeral, not durable). No defect filed: durable truth (status/workrun/deliverables) never rides the stream; the event feed is a live-activity view by design. |
| 4 | Offline completion → returning user (governed) | **PASS** | `er_cd61286d` reached `candidate_complete` while no browser was attached (and while gateway was down — see #2). Reopening the pane later mounts → `Agent hoàn tất` + `Kernel: Chờ duyệt — Kết quả chờ duyệt` + pending decision question with options + deliverable links + approval gate. Screenshot `s4-returning.png`. |
| 5 | Two-user isolation **during recovery** | **PASS** | bob (fresh Chrome profile, own jar): `GET /api/ewcp/runs/{alice_er}` → 404, `…/workrun` → 404, alice's `/join` → 404; bob's `/workspace/ewcp-runs` list contains none of alice's 6 run ids. alice's reload/reopen cycles (S1) never surfaced bob data — jars are separate sessions, no bleed. Consistent with #65's static matrix, now exercised mid-recovery. |
| 6 | Fix only demonstrated defects | **1 fix → PR #67** | See below. |

## Defect found and fixed (PR #67 — founder review)

**Stale governed workrun view on stream end.**
`execution-run-thread.tsx` fetched `getWorkrun` only in the mount effect
(deps `[executionRunId]`); the SSE effect's `finally` refreshed the record
only, despite the comment promising "the record + governed view once". Kernel
truth transitioning while the stream stayed open (kernel-first dispatch →
concurrent execution) left the pane showing mount-time workrun state —
approval gate / decisions / deliverables invisible until remount or manual
"Làm mới". Demonstrated in code + bounded live: deterministic packs finish
in ~1 s so the mount read usually wins, but any slower governed transition
(multi-step approvals, LLM/OCR stages) opens the window — and external
transitions (another actor/device deciding) were never reflected.
Fix: `.finally` now also re-reads `getWorkrun` when the run is bound.
Regression test: `tests/unit/ewcp/execution-run-thread.dom.test.tsx`
"stream end on a live governed run re-fetches the workrun".

**Not fixed (recorded, inherent or out of scope):**
- Workrun transitions landing *after* the product stream has fully ended are
  still missed until remount/manual refresh — durable truth needs either a
  workrun-level push or periodic refetch; left as a recorded limit.
- Ephemeral activity feed (S3): remount of a finished run shows an empty
  event feed; mid-run remount shows only the retained tail with no
  truncation indicator. Cosmetic by design — durable truth unaffected.
- General-lane runs orphaned by a gateway restart reconcile to `failed` on
  refresh — correct honest failure, not resumable (the run's execution
  context died with the process).
- `Last-Event-ID`/`gap` contract exists server-side but is unreachable from
  the pane (no cursor sent, no auto-reconnect path). Dead code for the
  browser until/unless a resume-with-cursor UX is wanted.
- Malformed cursors are silently clamped (no gap) — conservative but
  invisible; only well-formed below-watermark cursors produce `gap`.

## Incidental measurement

- One general run (`er_2d6efd5d`) reached `failed` after ~79 s with no error
  surfaced in the run record (`integrity_flag: null`) — Ollama-flake class
  consistent with GP01 notes; not a regression of this work.
- Governed intake rejects missing required context with 422
  (`mst_doanh_nghiep`, `ky` for `invoice_recon`) — form fields must use the
  spec slot names (`invoices_zip`, `books`), not `slot_files`.
