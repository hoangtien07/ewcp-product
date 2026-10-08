# A3 — Product Core Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: fresh worker session per task-group; TDD red→green trong từng task; verification-before-completion (không completion claim khi chưa chạy verify command trong message đó). Rulings-not-stalls: tự quyết conflict/ambiguity trong tầm spec, ghi `Ruling:` vào PR body. Dừng chỉ khi: irreversible/destructive, security-sensitive, side-effect ngoài worktree (merge/push shared), hoặc plan vỡ tới mức chỉ còn đoán.

> **Base branch:** `product/vnext` (KHÔNG phải `main` — upstream mirror). Nhánh `devin/<ts>-a3-<task>`. Một PR = một concern.

**Spec nguồn:** `docs/vnext/MIGRATION_AUDIT.md` (§2 matrix, §3 seam spec) + `docs/vnext/PHASE1_BASELINE.md` + kernel `docs/strategy/EXECUTION_STRATEGY_VNEXT.md` §4 (exit criteria amended) + `docs/architecture/REPOSITORY_MAP.md`.

## Understanding

A3 port EWCP Product Core lên `product/vnext` theo Option A+: general lane chạy native trên DeerFlow agent (AgentRuns public API), kernel đứng sau làm governed capability & verification server qua **HTTP** (không in-process ASGI mount). Surface governed cũ (`/ewcp` pane, legacy api.ts/FoundationPort/governed_model/plugin mount) thay bằng ExecutionRun projection + workspace surfaces.

Amendments từ audit 2026-10-08 (Batch 8):
- **Durability gate:** chứng minh disconnect/reconnect, process restart, run reconciliation, interrupt/resume, duplicate-start, authorization revocation — bằng reproduction scripts + tests, không chỉ đọc code.
- **`on_disconnect`:** upstream default `cancel` (`backend/app/gateway/run_models.py:53`) — long-run lanes phải override `continue` (pattern đã có tại `extension_agent_runs.py:142`).
- **Egress policy foundation kéo từ A5 lên A3:** modes `local-only` / `approved-cloud` / `restricted` enforce trên model calls + tool calls + sandbox network. ERP data vào được authorized context nhưng không rời approved boundary.
- `AgentRun.status=success` với pending interrupt KHÔNG tính business-complete.

## File structure

| Path | Verdict | Vai trò |
|---|---|---|
| `backend/extensions/ewcp-core/` | NEW extension | Skeleton port từ `extensions/ewcp-packs` (audit 2b PORT): kernel HTTP client, capability registry client, ExecutionRun launcher, egress middleware, governed card API surface |
| `backend/extensions/ewcp-core/kernel_client.py` | REPLACE plugin.py | httpx client → kernel `/tasks` `/decisions` `/workruns` `/verify` `/capabilities`; kernel key chỉ ở đây (M2M), không leak xuống frontend |
| `backend/extensions/ewcp-core/run_launcher.py` | REPLACE exploratory.ts backend-half | Adapter trên AgentRuns: create_thread→start(idempotency_key)→stream; persist `ExecutionRunMap` (workrun↔thread/run + mode + status) |
| `backend/extensions/ewcp-core/egress_policy.py` | NEW | Egress modes + enforcement hooks (model-call allowlist, tool-call gate, sandbox net profile) |
| `backend/extensions/ewcp-core/model_policy.py` | REPLACE governed_model.py | Product-layer hook: run-scoped cap đọc từ kernel broker API (kernel giữ ledger; không fake-streaming) |
| `backend/extensions/ewcp-core/recovery.py` | NEW | Reacquire handles post-restart, reconcile thread state, pending-interrupt detection, dup-start guard, revoke check |
| `frontend/src/ewcp/` | PORT 7 components + labels + verify page | manifest-card, decision-card, verify-view, unverified-badge, share-verify-link (→signed-token rework defer), pack-gallery→capability-gallery, studio-card→ExecutionRun detail, labels.ts, `app/verify/[hash]/page.tsx` |
| `frontend/src/ewcp/api.ts` | REPLACE | Product API client trên upstream auth (session/PAT) — kernel key chỉ M2M |
| `frontend/src/ewcp/execution-runs*.tsx` | REPLACE general-card/task-list/task-thread | ExecutionRun list/card/thread-view trên SSE `join_run`, embeds manifest/decision components |
| `frontend/src/app/ewcp/page.tsx`, `docker/nginx/*` hunks, `next.config.js` hunk | DROP | Pane frame + proxy workarounds — chỉ xóa khi replacement paths live |
| `frontend/tests/unit/ewcp/` | PORT 6 + REPLACE 6 test files | Test đi theo component |
| `docs/vnext/A3_DURABILITY.md` | NEW | Reproduction scripts + kết quả durability gate |

## Tasks

### Task 1 — Extension skeleton + kernel HTTP client `[ewcp-core-kernel-client]`

- [ ] Tạo `backend/extensions/ewcp-core/` skeleton theo `examples/deerflow-extension-*` conventions + `config.yaml plugins:` entry doc. Test: `deerflow extensions install` loads, extension registers, unit test import.
- [ ] `kernel_client.py`: httpx sync client, config `ewcp.kernel_url` + `ewcp.kernel_api_key` (env-first, never logged); methods submit_task/get_run/verify/list_capabilities/decide; timeout + bounded retry (pattern `GeminiClient` PR #92). Tests: respx/mock httpx — happy path, 401, timeout, retry exhaustion.
- [ ] Registry endpoint client: `GET /capabilities` → typed `Capability` model (id/kind/input-schema). Tests: parse fixture from kernel `/capabilities` response shape.
- [ ] **DoD:** `cd backend && python -m pytest tests/ -k ewcp_core -q` green; kernel key không xuất hiện trong log/response (negative test).

### Task 2 — ExecutionRun launcher trên AgentRuns `[ewcp-core-runs]`

- [ ] `run_launcher.py`: `launch(intent, files, mode)` → `agent_runs.create_thread` → `agent_runs.start(idempotency_key=…)` → trả `ExecutionRun` projection. Mode→on_disconnect: governed lanes `continue`.
- [ ] `ExecutionRunMap` persistence model + migration (thread_id, run_id, workrun_id nullable, task_mode, status, idempotency_key, created_by) — SQLite phía extension.
- [ ] Tests: mock AgentRuns protocol (`tests/test_extension_agent_runs.py` patterns) — launch happy, idempotent replay (cùng key → same run), mode mapping, pending-interrupt ≠ complete.
- [ ] **DoD:** launcher + map migration pass tests; replay không tạo run mới.

### Task 3 — Durability + recovery `[ewcp-core-durability]`

- [ ] `recovery.py`: `recover(run_map)` — reacquire handle qua AgentRuns `get`/`get_state` sau restart; reconcile map status vs thread truth; `pending_interrupt` flag; dup-start guard (idempotency_key + open-run check); revoke check trước resume.
- [ ] Reproduction tests (`tests/` + `docs/vnext/A3_DURABILITY.md`): (a) client disconnect → run sống tiếp (`on_disconnect=continue`), rejoin qua `join_run`; (b) process restart → handle reacquired, map reconciled; (c) duplicate start → guard; (d) auth revoked → operation denied; (e) interrupt → resume roundtrip.
- [ ] **DoD:** 5 scenarios có test + script repro; `success` + pending interrupt không đóng ExecutionRun.

### Task 4 — Egress policy foundation `[ewcp-core-egress]`

- [ ] `egress_policy.py`: `EgressMode` enum (`local_only`/`approved_cloud`/`restricted`) + `EgressPolicy` per-tenant/run config surface (extension config); enforcement hooks: (a) model-call hook — chỉ allowlisted providers/models, `local_only` → chỉ local endpoint; (b) tool-call gate — capability+network-class check fail-closed; (c) sandbox net profile binding (upstream sandbox config seam; doc nếu upstream thiếu hook — ghi gap, không patch core).
- [ ] Tests: allow/deny matrix per mode × per channel (model/tool/net); fail-closed khi policy thiếu; tenant isolation của policy.
- [ ] **DoD:** 3 channels có enforcement + matrix tests green; gap upstream được ghi rõ trong doc.

### Task 5 — Model policy hook `[ewcp-core-model-policy]`

- [ ] `model_policy.py`: hook trên upstream model factory/profile surface — run-scoped $ cap đọc từ kernel broker (`/workruns/{id}` spend view hoặc endpoint cap mới — Ruling nếu kernel thiếu endpoint: product-side counter + kernel reconcile sau). Không fake-streaming.
- [ ] Tests: cap-exceeded → deny trước call (spy factory không được gọi); mode→cap mapping; ledger reconcile shape.
- [ ] **DoD:** deny-before-call test green; kernel giữ authority cho ledger.

### Task 6 — Frontend ExecutionRun surface `[ewcp-frontend-runs]`

- [ ] `api.ts` REPLACE: client trên upstream auth; chỉ các route product; test contract vs kernel shapes (fixtures).
- [ ] ExecutionRun list + detail view: run từ SSE `join_run` (không `setInterval` polling), manifest-card/decision-card embed trong run view.
- [ ] REPLACE: `exploratory.ts`→launcher client; `registry.ts`→capability registry client; `general-card`/`task-list`/`task-thread` → ExecutionRun components; nav hunk → workspace entry point.
- [ ] Tests PORT+REPLACE (12 files): dom tests cho components mới + ported (re-point imports).
- [ ] **DoD:** `cd frontend && pnpm rstest run ewcp` green; `pnpm check` clean.

### Task 7 — Surface rehome + cleanup `[ewcp-surface-rehome]`

- [ ] PORT components 1:1 vào workspace views (manifest/decision/verify/studio→ExecutionRun detail/capability-gallery/badge/labels); `app/verify/[hash]` public route ngoài pane.
- [ ] DROP sau khi T6 live: `app/ewcp/page.tsx`, `docker/nginx` 2 hunks, `next.config.js` hunk, `frontend/src/ewcp/README.md` (viết lại README mới).
- [ ] **DoD:** verify route render từ kernel response mock; `/ewcp` pane gone; không còn import tới paths đã drop (grep CI step hoặc test).

### Task 8 — Integration proof + doc `[ewcp-a3-proof]`

- [ ] Cross-repo smoke: product → kernel HTTP `submit_task` → governed run → manifest → `/verify` — 1 script E2E trong `docs/vnext/` (kernel service boot local hoặc docker).
- [ ] `A3_DURABILITY.md` tổng hợp: scenario → expected → measured → command repro.
- [ ] **DoD:** smoke E2E chạy được trên checkout sạch; doc ghi limitations còn lại.

## Notes cho workers

- Legacy kernel `foundation/*` DROP chỉ làm trong PR riêng trên kernel repo (audit §2a) — task này KHÔNG đụng kernel code trừ khi phải mở endpoint (ghi Ruling + kernel PR riêng).
- `bind()` không nhận PAT (`extension_agent_runs.py:61`) — flow không-do-request (scheduler) cần system-identity path chưa có upstream: ghi gap trong doc, không patch core.
- share-verify-link → signed-token rework DEFER (ghi issue); giữ `/verify` public behavior hiện tại.
- Kernel repo contract: HTTP surface hiện có đủ dùng; nếu thiếu endpoint thật (vd run-cap read), mở kernel PR riêng, không embed kernel code vào extension.

## Open questions

- None cho founder trong stage này (system-identity path + signed-token = follow-ups trong doc).
