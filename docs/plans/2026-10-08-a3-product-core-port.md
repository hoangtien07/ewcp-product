# A3 — Product Core Port Implementation Plan (rev 2 — post council REQUEST CHANGES)

> **For agentic workers:** fresh worker session per task-group; TDD red→green
> per task; verification-before-completion (không completion claim khi chưa
> chạy verify command). Rulings-not-stalls trong tầm spec; dừng chỉ khi:
> irreversible/destructive, security-sensitive, side-effect ngoài worktree,
> hoặc plan vỡ tới mức chỉ còn đoán. Ghi `Ruling:` vào PR body.

> **Base branch:** `product/vnext` — KHÔNG `main` (upstream mirror). Nhánh
> `devin/<ts>-a3-<task>`; một PR = một concern.

**Spec nguồn:** `docs/vnext/MIGRATION_AUDIT.md` + `docs/vnext/PHASE1_BASELINE.md`
+ **repo khác** `hoangtien07/enterprise-work-control-plane`:
`docs/strategy/EXECUTION_STRATEGY_VNEXT.md` §4 và
`docs/architecture/REPOSITORY_MAP.md` (đọc qua path đầy-đủ repo — file KHÔNG
nằm trong repo này; không tạo bản sao mới).

**Council review (2026-10-08): REQUEST CHANGES → rev 2 sửa 8 điểm + thêm
API-contract table + preconditions. Mọi API/method trong plan đều cite path:line
hoặc có kernel PR kèm; không fixture cho API chưa tồn tại.**

## Understanding

A3 port EWCP Product Core lên `product/vnext` theo Option A+: general lane
native trên DeerFlow (AgentRuns control + Gateway SSE observation + uploads
API — **ba surface tách biệt**, không phải một interface), kernel = governed
capability & verification server qua HTTP (không in-process ASGI mount).

Amendments giữ nguyên từ audit (Batch 8): durability gate, `on_disconnect`
(mặc định `cancel` — `backend/app/gateway/run_models.py:53`; bound path đã
override `continue` tại `extension_agent_runs.py:142`), egress foundation kéo
từ A5, pending-interrupt ≠ complete.

## API contract table — verify trước khi worker start

| Cần | Surface thật | Evidence |
|---|---|---|
| Kernel: submit/decide/run/verify | `POST /tasks`, `/decisions`, `GET /workruns/{id}`, `/verify/{hash}` | kernel `src/ewcp/api/app.py` header + routes |
| Kernel: outcome/pack catalog | `GET /outcomes` (+ `POST /outcomes/{type}/run`) | `app.py:1599,1640` — **KHÔNG có `/capabilities` HTTP route** (CapabilityRegistry là in-process `app.py:429-431`). A3 dùng `/outcomes` wire shape thật; capability-discovery API versioned = kernel PR riêng chỉ khi Task 1 chứng minh `/outcomes` thiếu |
| Agent control | `AgentRuns`: `create_thread`/`start`/`resume`/`get`/`get_state`/`wait`/`cancel` | `backend/packages/extension-api/deerflow_extension_api/agent_runs.py:47-82` — **KHÔNG có `stream()`/`join_run()`** |
| Run observation SSE | Gateway HTTP: `POST /api/threads/{tid}/runs/stream` (`:970`), `GET …/runs/{rid}/join` (`:1312`), `POST …/runs/{rid}/stream` (`:1430`) | `backend/app/gateway/routers/thread_runs.py` |
| File upload | `POST /api/threads/{id}/uploads` | `backend/app/gateway/routers/uploads.py:388` — `AgentRuns.start()` chỉ nhận JSON input |
| Egress enforcement hooks | `MODEL_PHYSICAL` middleware placement + tool-call middleware | `backend/packages/extension-api/deerflow_extension_api/placement.py:26` — kiểm tra coverage tool-call + sandbox-net seam trước khi claim |
| Auth | AgentRuns `bind()` chỉ SESSION/AUTH_DISABLED | `extension_agent_runs.py:51-61` — không PAT/service identity |

## File structure

| Path | Verdict | Vai trò |
|---|---|---|
| `backend/extensions/ewcp-core/` | NEW extension | Kernel HTTP client + run launcher + egress + model-policy + recovery |
| `…/kernel_client.py` | REPLACE plugin.py | **`httpx.AsyncClient`** (Gateway handlers async — không sync httpx block event loop); retry CHỈ cho safe reads (`GET`); mutations (`POST /tasks`, `/decisions`) retry chỉ khi idempotency-key contract thật của kernel |
| `…/run_launcher.py` | REPLACE exploratory backend-half | AgentRuns control (thread/start/resume/cancel) + Gateway SSE route cho observation + uploads route cho files — tách rõ 3 surface |
| `…/execution_run_store.py` | NEW | `ExecutionRunMap` — **projection chỉ**: identifiers + product-owned metadata; governed truth đọc từ kernel, không sao chép. Dùng shared durable persistence của Product (không SQLite file riêng lẻ nếu product DB đã có) |
| `…/egress_policy.py` | NEW | `local_only`/`approved_cloud`/`restricted` × model+tool+sandbox-net; fail-closed |
| `…/model_policy.py` | REPLACE governed_model.py | Budget admission qua contract kernel (Task 5) |
| `…/recovery.py` | NEW | Foreground recovery chỉ (xem Task 3) |
| `frontend/src/ewcp/` | PORT 7 components + labels + `app/verify/[hash]` | manifest-card, decision-card, verify-view, unverified-badge, pack-gallery→capability-gallery, studio-card→ExecutionRun detail, labels.ts |
| `frontend/src/ewcp/api.ts` + `exploratory.ts` + `registry.ts` + `general-card`/`task-list`/`task-thread` | REPLACE | Product API client trên upstream auth; SSE `join_run` thay polling; launcher + registry trên contract thật |
| `frontend/src/app/ewcp/page.tsx`, nginx hunks, `next.config.js` hunk | DROP | Sau khi replacement live |
| `frontend/tests/unit/ewcp/` | PORT 6 + REPLACE 6 | Đi theo component |
| `docs/vnext/A3_DURABILITY.md` | NEW | Repro scripts + measured results |

## Tasks

### Task 1 — Extension skeleton + kernel HTTP client `[ewcp-core-kernel-client]`

- [ ] Skeleton `backend/extensions/ewcp-core/` theo `examples/deerflow-extension-*`; `config.yaml plugins:` doc. Test: extension loads + registers.
- [ ] `kernel_client.py` `httpx.AsyncClient`: `/tasks`/`/decisions`/`/workruns`/`/verify`/**`/outcomes`** (contract thật, fixture từ response shape thật — KHÔNG giả `/capabilities`). Config `ewcp.kernel_url`/`ewcp.kernel_api_key` env-first, never logged (negative test).
- [ ] Retry policy: safe reads bounded retry (pattern PR #92); mutations only với kernel idempotency-key thật (`idempotency_keys` table tồn tại — cite `kernel/persistence.py:105-111`).
- [ ] **Nếu `/outcomes` thiếu fields worker cần** (input schema/grants): mở kernel PR riêng cho capability-discovery endpoint versioned + contract tests — không invent.
- [ ] **DoD:** `pytest -k ewcp_core` green; no kernel key in logs; mutation-no-retry test.

### Task 2 — ExecutionRun launcher (3 surfaces tách) `[ewcp-core-runs]`

- [ ] `run_launcher.py`: `launch(intent, mode)` → `agent_runs.create_thread` → `start(input=…, idempotency_key=…)`; files đi qua **uploads route riêng** (`POST /api/threads/{id}/uploads`) trước start, reference trong input theo upstream file contract — spec exact shape khi implement.
- [ ] Observation = Gateway SSE endpoints (table §API), KHÔNG qua AgentRuns.
- [ ] `ExecutionRunMap` projection: thread_id/run_id/workrun_id(nullable)/task_mode/status/idempotency_key/created_by — trên **shared product persistence** (check `backend/app/gateway/persistence` conventions trước khi tạo store riêng; Ruling nếu phải dùng table riêng).
- [ ] `on_disconnect`: verify mọi admission path dùng bound impl có `continue` (`extension_agent_runs.py:142`) — không thêm param vào interface không có.
- [ ] Tests: mock AgentRuns — happy/idempotent-replay/pending-interrupt≠complete; uploads-mapping test.
- [ ] **DoD:** launch + map + SSE-route constants pass; replay không tạo run mới.

### Task 3 — Durability: foreground recovery ONLY `[ewcp-core-durability]`

- [ ] `recovery.py` **foreground**: user đăng nhập lại → extension nhận authenticated request mới → `for_plugin` handle mới → `get`/`get_state` reconcile `ExecutionRunMap` vs thread truth; pending-interrupt flag; dup-start guard (idempotency-key + open-run check); revoke check trước resume.
- [ ] `get_state` = mutable thread state, KHÔNG phải immutable run result — reconcile bằng run ids + event history, không tin một snapshot.
- [ ] **Background/service recovery OUT OF SCOPE A3** — `bind()` không nhận PAT/internal creds (`extension_agent_runs.py:61`); ghi rõ limitation trong `A3_DURABILITY.md`: không claim durable autonomous background execution. Nếu cần: mở issue upstream/system-identity contract riêng.
- [ ] Repro tests: disconnect→rejoin SSE; restart→foreground recovery; dup-start deny; revoked-auth deny; interrupt→resume.
- [ ] **DoD:** 5 scenarios tested; doc ghi đúng phạm vi.

### Task 4 — Egress policy: enforcement hoặc fail-closed `[ewcp-core-egress]`

- [ ] Inventory trước: middleware hooks (`MODEL_PHYSICAL` placement + tool-call middleware) + sandbox network seam — map chính xác hook nào tồn tại, channel nào cover được.
- [ ] `EgressPolicy` per-tenant/run; enforce ở từng channel có hook thật. **Middleware policy ≠ network isolation** — sandbox-net cần seam riêng.
- [ ] **Fail-closed rule:** với `local_only`/`restricted`, nếu bất kỳ đường dữ liệu bắt buộc nào (model/tool/sandbox-net) chưa enforce được → workload nhạy cảm KHÔNG được chạy. Thiếu hook bắt buộc → task FAIL, không nghiệm thu bằng doc.
- [ ] Nếu cần upstream hook mới: PR nhỏ, ghi `UPSTREAM_TOUCH` — có chủ đích.
- [ ] Tests: allow/deny matrix per mode × per channel; fail-closed khi policy/hook thiếu; multi-tenant isolation.
- [ ] **DoD:** enforcement matrix green HOẶC workload nhạy cảm bị deny — không có trạng thái giữa.

### Task 5 — Budget admission contract (không fallback counter) `[ewcp-core-model-policy]`

- [ ] **Contract chốt trước:** budget admission áp cho cả general runs (không `kernel.workrun_id`) lẫn governed runs: fields `tenant_id`, `execution_run_id`, cap, spend attribution, behavior khi không lấy được policy (deny = fail-closed cho governed; configurable cho general).
- [ ] Kernel PR riêng nếu cần endpoint (vd `POST /budget/admission` hoặc extend spend view) — admission/reservation TRƯỚC call, không "product-side counter rồi reconcile sau" làm hard-cap authority. Reconciliation chỉ là audit, không phải enforcement.
- [ ] Enforcement granularity được ghi rõ: token limit, retry bound, sai số tối đa có thể (provider thiếu hard-cap → công bố bounded overshoot).
- [ ] Tests: cap-exceeded deny trước call (spy không được gọi); no-policy behavior per mode; attribution đúng execution_run_id.
- [ ] **DoD:** deny-before-call green; contract doc trong `docs/vnext/`.

### Task 6 — Frontend ExecutionRun surface `[ewcp-frontend-runs]`

- [ ] `api.ts` REPLACE: upstream auth (session); kernel key chỉ M2M — frontend KHÔNG bao giờ thấy kernel key.
- [ ] ExecutionRun list/detail trên SSE `join_run`; manifest-card/decision-card embeds.
- [ ] **Identity binding (P1 bắt buộc trước khi decisions bật nút Approve):** map authenticated product user → decision authorization; kernel `_actor()` hiện trả `tenant:<tenant>` — A3 chỉ hiển thị decisions (read-only) được, nhưng nút quyết định đòi user-identity binding contract (kernel PR nếu cần; hard requirement trước A5 ERP writes).
- [ ] Tests PORT+REPLACE (12 files).
- [ ] **DoD:** `pnpm rstest run ewcp` green; `pnpm check` clean; decision buttons gated đúng.

### Task 7 — Surface rehome + cleanup `[ewcp-surface-rehome]`

- [ ] PORT components vào workspace views; `app/verify/[hash]` public route.
- [ ] DROP sau khi T6 live: `app/ewcp/page.tsx`, nginx 2 hunks, `next.config.js` hunk, pane README (viết lại doc vNext).
- [ ] **DoD:** verify route render từ kernel mock; `/ewcp` gone; grep CI step không còn import drop-paths.

### Task 8 — Integration proof (mở rộng phạm vi) `[ewcp-a3-proof]`

- [ ] E2E đủ 2 loại: (a) **native general task** (không pack) chạy native DeerFlow lane — không ép qua kernel; (b) **governed task** → kernel HTTP → seal → `/verify`.
- [ ] + durability scenarios: restart, interrupt/resume, denied network egress (local_only deny ra ngoài), budget rejection.
- [ ] `A3_DURABILITY.md` + smoke doc: scenario → expected → measured → repro command + limitations.
- [ ] **DoD:** smoke chạy được trên checkout sạch; limitations ghi đầy đủ.

## Preconditions (check trước khi spawn từng task)

- Worker đọc MIGRATION_AUDIT §2–3 + API-contract table ở đây; verify mọi
  endpoint/method trên `product/vnext` + kernel `main` trước khi viết code.
- Kernel-side endpoint mới = PR riêng vào `enterprise-work-control-plane`
  `main`, không embed kernel code vào extension.
- Frontend decisions approval = read-only display cho A3 trừ khi identity
  binding contract đã implement (Task 6 note; A5 gate).
- `foundation/*` kernel legacy DROP = kernel PR riêng sau A3.

## Open questions

- Background recovery (service-identity path) → issue upstream/follow-up doc.
- Signed-share-token cho `/verify` links → follow-up issue (A6).
