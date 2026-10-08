# vNext Migration Audit — evidence-based

**Ngày:** 2026-10-08. **Người viết:** Devin session `990338f1bed94e4cb7feb3d1176fa5bf`.
**Đối tượng:** audit migration `ewcp/main` → `product/vnext` theo quyết định Option A+
"kernel-as-capability-server" của vNext Council 2026-10.

**Method:** mọi con số đo lại trực tiếp trên clone thật (`~/repos/ewcp-product`,
`~/repos/enterprise-work-control-plane`); lệnh reproduce kèm theo. Claim không có
code cite gắn nhãn `[suy luận]` / `[giả định]` / `[không đo được]`.

**Revisions đã audit:**

| Repo | Ref | SHA | Vai trò |
|---|---|---|---|
| ewcp-product | `product/vnext` | `53df22bd` | dev-line pin = upstream `main` tip, DeerFlow vanilla |
| ewcp-product | `ewcp/main` | `aade2ada` | legacy EWCP implementation — **read-only reference** |
| enterprise-work-control-plane | `main` | `b99ebf0` | kernel — governed capability server |

> Quy ước cite: `vnext:` = ewcp-product@`53df22bd`; `fork:` = ewcp-product@`ewcp/main`;
> `kernel:` = enterprise-work-control-plane@`main` (repo-relative `src/ewcp/`).
> Council artifacts đối chiếu: `kernel:docs/reviews/vnext-council-2026-10/{README,W1-benchmark,W2-drift-audit,W3-arch-review,W4-redteam}.md`.

---

## 0. Founder decisions đã ghi nhận

| # | Quyết định | Ghi vào doc này |
|---|---|---|
| Q42 — pin | Tạo `product/vnext` từ upstream `main` (`53df22bd`) là **dev-line pin có chủ đích**: `AgentRuns` extension API (upstream PR #6190, commit `beaae220`) chưa nằm trong release nào — v2.1.0 (`345f08be`) không có `backend/docs/extension-agent-runs.md` | §7.1 ghi amendment tường minh vào policy "track tags, not main" (`fork:docs/ewcp/UPSTREAM_SYNC.md:10-11`) + exit criteria |
| Q43 — moat | "Governed outcome" giữ làm moat, mở rộng thành **third-party-verifiable outcomes** (seal + `/verify`) mà Claudeforce/Agentforce không có | §2 ma trận giữ packs/seal/verify là KEEP |
| Direction | DeerFlow-native general AI Workspace + EWCP Product Core (WorkRun/verification/review/approval) + reusable Enterprise Capabilities (packs→business capabilities) + **Odoo Integration (scope MỚI — council chưa từng đánh giá)** + Verified Execution + Cost Governance | §5 (Odoo, đánh dấu new-scope); §4 phases |

---

## 1. Verified delta inventory — đo lại trên repo thật

Lệnh reproduce (chạy trong `ewcp-product`):

```bash
git diff 345f08be origin/ewcp/main --name-status   # v2.1.0 tag → ewcp/main tip
git diff 345f08be origin/ewcp/main --numstat
git diff 345f08be origin/ewcp/main --shortstat
```

**Kết quả đo (2026-10-08):** 45 files changed, **7.865 insertions(+), 0 deletions** —
41 Added + 4 Modified. Khớp 100% với W2 §1.

> ⚠️ Không dùng `main...ewcp/main` (3-chấm): merge-base là `769589e8` (v2.1.0-**rc0**)
> nên đếm cả 90 file của 10 commit upstream rc0→v2.1.0 → trả 135 file.
> Cách đúng: diff từ tag `345f08be` (v2.1.0 — ancestor của `ewcp/main`, không nằm
> trong history `origin/main`; verify: `git merge-base --is-ancestor 345f08be
> origin/main` = false).

### 45 files theo module

| Module | Files | LOC (+) | Chi tiết |
|---|---|---|---|
| `extensions/ewcp-packs/` (backend extension) | **9** | **1.975** | prod: `foundation_port.py` 394, `governed_model.py` 357, `plugin.py` 196, `__init__.py` 24 (=971); tests: `test_foundation_port.py` 422 + `test_governed_model.py` 340 + `test_plugin.py` 127 (=889); `pyproject.toml` 37; `README.md` 78 |
| `frontend/src/ewcp/` | **15** | **3.224** | components 10 files 2.297 (`task-thread` 623, `studio-card` 414, `verify-view` 356, `general-card` 254, `task-list` 167, `pack-gallery` 140, `decision-card` 121, `manifest-card` 112, `share-verify-link` 78, `unverified-badge` 32); `api.ts` 412; `registry.ts` 209; `labels.ts` 153; `exploratory.ts` 126; `README.md` 27 |
| `frontend/src/app/ewcp/` | **2** | **293** | `page.tsx` 260; `verify/page.tsx` 33 |
| `frontend/tests/unit/ewcp/` | **12** | **2.134** | 8 `.dom.test.tsx` + 4 `.test.ts` |
| `docs/ewcp/` | **2** | **149** | `UPSTREAM_SYNC.md` 81; `PA_A_LAYOUT.md` 68 |
| `UPSTREAM_TOUCH.md` | **1** | **27** | ledger 4/8 slots |
| **Upstream touches (M)** | **4** | **63** | `docker/nginx/nginx.conf` +22; `docker/nginx/nginx.local.conf` +22 (cả hai: `location /api/ewcp` + 660s timeouts); `frontend/next.config.js` +8 (`proxyTimeout: 660_000`); `frontend/src/components/workspace/workspace-nav-chat-list.tsx` +11 (nav link `/ewcp`) |
| **Tổng** | **45** | **7.865** | |

### Kernel inventory (repo riêng, `src/ewcp/` @ `b99ebf0`)

Đo bằng `find src/ewcp -name '*.py' | xargs wc -l`: **21.356 LOC, 96 file**.

| Module | LOC | Nội dung |
|---|---|---|
| `outcomes/` | 8.232 | 4 packs: `invoice_recon` 2.378, `three_way_match` 2.491, `congno_doi_chieu` 1.854, `dossier_check` 1.509 |
| `kernel/` | 3.831 | `persistence.py` 1.382 (SqliteStore, workruns/attempts/manifests/ledger/decisions/idempotency/metric_events), `contract.py` 343 (7 criterion kinds), `models.py` (WorkRun 9-state machine, `HumanDecision` `:317-330`), `evidence.py` (seal/manifest), `gateway.py` (CapabilityGateway grant mint), `run.py`, `outcome.py`, `schedule.py` 253, `email_ingest.py` 224, `strategy.py`, `taskstate.py` 126, `errors.py` |
| `api/` | 3.454 | `app.py` monolith: intake, routing, decisions, seal, `/verify`, schedule tick, metrics, demo fixtures |
| `foundation/` | 1.677 | `invoke.py` 619 (`invoke_capability` sub-runs), `executor.py` 585 (`FoundationAgentExecutor` — general-lane turn driver), `port.py` 163 (`FoundationPort` Protocol), `spec.py`, `prompts.py` (SOUL/envelope protocol) |
| `sandbox/` | 1.263 | netns deny-all, userns+chroot jail, rlimits, EgressProxy allowlist |
| `runtime/` | 920 | `model_broker.py` 179 (virtual-key `vk-<wid>` + budget), `llm_gateway.py` 164, `gemini_client.py`, `deterministic.py`, `router.py` (~180, intent router) |
| `mcp/` | 598 | `server.py` — EWCP-as-MCP: `ewcp_submit_task`/`ewcp_get_run`/`ewcp_verify` (`:263,:320,:346`) |
| `checks/` | 472 | `engine.py` — 7 validators (`_v_file_exists` `:105`, `_v_human_approval` `:300-311`, …) |
| `capability/` | 324 | `registry.py` — CapabilityEntry (`kind` gồm `"mcp"` — `:21`) |
| `workspace/` | 295 | `primitive.py` — workspace layout `ewcp/{inbox,outbox,contract.json,state.json,checkpoints}` |
| `policy/` | 143 | `egress.py` (`BUILTIN_ALLOW={"demo","default"}` `:35`), `export.py` |
| root (`__init__`, `cli.py`, …) | 139 | |

> **Correction vs W4 §0:** W4 ghi "Kernel `src/ewcp` = **18.596 LOC**". Đo lại trên
> `main@b99ebf0`: **21.356 LOC**. Tổng của W4 chỉ cộng 7 module (kernel+outcomes+
> api+foundation+mcp+checks+capability = đúng 18.596) nhưng dòng chú thích ghi
> "runtime + workspace + sandbox còn lại" — thực tế đã bỏ sót sandbox 1.263 +
> runtime 920 + workspace 295 + policy 143 + root 139 = **2.760 LOC**. Kernel lớn
> hơn con số council dùng ~15%. Một phần chênh lệch có thể do `0ec7c8f→b99ebf0`
> drift [giả định — phần lớn là lỗi cộng: parenthetical của W4 không khớp tổng].

---

## 2. Migration matrix — KEEP / PORT / REPLACE / DROP

Quy ước verdict:

- **KEEP** — giữ nguyên function + implementation (kernel sống tiếp làm
  capability/verification server theo Option A+).
- **PORT** — function giữ, code chuyển chỗ/re-home với chỉnh sửa nhỏ.
- **REPLACE** — function giữ hoặc đổi, implementation viết lại trên seam mới.
- **DROP** — chết, không cần thay thế.

### 2a. Kernel modules (verdict ở module granularity — kernel KHÔNG nằm trong delta 45 file)

| Module/file | Verdict | Evidence một dòng |
|---|---|---|
| `checks/engine.py` (472) | **KEEP** | Check engine 7 criterion kinds là moat; `run.py:218-265` chỉ VERIFIED qua sealed manifest |
| `kernel/contract.py` (343) | **KEEP** | AcceptanceContract + criteria; contract-first intake giữ theo Q43 |
| `kernel/evidence.py` | **KEEP** | Evidence tiers + HMAC seal — lõi third-party-verifiable |
| `kernel/run.py` + `models.py` | **KEEP** | WorkRun authority + `HumanDecision` (`models.py:317`) cho governed runs |
| `kernel/persistence.py` (1.382) | **KEEP** | State authority; `idempotency_keys` tenant+key (`:105-111`) |
| `kernel/gateway.py` | **KEEP** | CapabilityGateway: grant bound workrun+attempt+tenant, wildcard `*` chặn non-read (`:44-99`) |
| `kernel/outcome.py` + `outcomes/*` (8.232) | **KEEP** | 4 pack deterministic = "governed outcome fast-path" — moat theo Q43; KHÔNG retire trước Solver Lab parity (§6) |
| `api/app.py` (3.454) | **KEEP** | HTTP surface của capability server: `/tasks`, `/decisions`, `/workruns`, `/verify`, `/capabilities`. Các path general-lane (`_drive_general` `:2542`, `_sync_resume_general` `:1307,2003,2089,2109`) sẽ co lại khi general lane chuyển sang vNext — monolith debt là kernel-internal, tách sau |
| `api/` `/verify/{manifest_hash}` | **KEEP (+PORT)** | `app.py:3166-3184` "the hash is the capability" — public by design; nâng cấp authz bằng signed-share-token + legacy redirect ở Phase 3 (W4 §1.5.2: link đã phát hành không được break) |
| `kernel/schedule.py` (253) | **KEEP** | Scheduler governed runs ở kernel; upstream scheduler (`scheduler.enabled`) là surface khác cho product lane |
| `kernel/email_ingest.py` (224) | **KEEP** | Intake channel → governed runs; vẫn hit kernel API |
| `policy/egress.py` + `export.py` (143) | **KEEP** | Tenant classify fail-closed (`sensitive` default `:9-12`) = prevention>proof |
| `capability/registry.py` (324) | **KEEP** | Capability catalog; `kind="mcp"` đã chờ sẵn cho MCP-scope grants |
| `runtime/model_broker.py` + `llm_gateway.py` (343) | **KEEP** | `vk-<wid>` idempotent + BudgetGuard reserve/charge (`model_broker.py:96-179`) — cost governance ở governed boundary |
| `runtime/gemini_client.py` | **KEEP + P0 fix** | `params={"key":…}` `:256-259` → key nằm trong URL → httpx error embeds `?key=` vào traceback/log **[security]** |
| `runtime/deterministic.py` + `router.py` (~180) | **KEEP** | Intent router → pack dispatch; LLM router `gemini-3.5-flash` ~1.4k tok/run (W1 §1) |
| `sandbox/` (1.263) | **KEEP** | Isolation thật (netns/jail/rlimits) cho governed sub-runs — upstream `LocalSandboxProvider` tự khai "not a security boundary" |
| `mcp/server.py` (598) | **KEEP** | Capability surface #2: `ewcp_submit_task`/`ewcp_get_run`/`ewcp_verify` — product lane gọi kernel qua đây hoặc invoke |
| `foundation/invoke.py` (619) | **KEEP** | Capability surface #1: `invoke_capability` → governed sub-run in-process (M-EA2) |
| `kernel/strategy.py` | **KEEP** | ExecutionStrategy dispatch (deterministic/LLM) của pack lane |
| `cli.py` + root files | **KEEP** | Ops tooling |
| `foundation/executor.py` (585) | **DROP** | General-lane turn driver + envelope drain; chỉ `app.py:414` gọi — packs không dùng (packs đi qua `kernel/run.py` + outcome pipeline). Chết cùng filesystem protocol |
| `foundation/port.py` (163) | **DROP** | `FoundationPort` Protocol{turn, upload_files, abort} — tồn tại vì v2.1.0 thiếu public run API; AgentRuns thay thế |
| `foundation/spec.py` | **DROP** | General-lane spec builder (gắn `human_approval` required `:37`); contract machinery sống qua `kernel/contract.py` |
| `foundation/prompts.py` | **DROP** | SOUL/envelope prompt protocol (~10.7k input/call — nguồn token overhead, W1 §2) |
| `workspace/primitive.py` (295) | **DROP** | Filesystem inbox/outbox/state.json cho general lane — thay bằng AgentRuns + interrupts + uploads API. **Gate:** verify pack pipeline không share path này trước khi xóa [cần verify] |
| `kernel/taskstate.py` (126) | **DROP** | Turn-state projection của envelope machinery (W3 R8 gộp vào "second agent framework") |

**Đếm kernel:** KEEP **21** · DROP **6** · REPLACE **0** (kernel không đổi boundary
theo Option A+; `/verify` tính KEEP kèm PORT authz, `gemini_client` tính KEEP kèm
P0 patch).

### 2b. Fork delta — 45 files

| Module / file(s) | Verdict | Evidence một dòng |
|---|---|---|
| `UPSTREAM_TOUCH.md` | **KEEP** | Ledger discipline sống tiếp trên vNext; counter re-baseline về 0/8 cho upstream mới |
| `docs/ewcp/UPSTREAM_SYNC.md` | **KEEP (amended)** | Policy "track tags, not main" (`:10-11`) giữ — + amendment Q42 ghi ngoại lệ dev-line (§7.1) |
| `docs/ewcp/PA_A_LAYOUT.md` | **REPLACE** | Mô tả layout ewcp/main (boxed extension) — viết lại cho layout vNext (kernel service ngoài repo) |
| `extensions/ewcp-packs/…/foundation_port.py` (394) | **REPLACE** | Private-seam wrapper (`_get_tools`/`_get_runnable_config`/context-stamp `:60-63,124-160`) → thin adapter trên `AgentRuns` public API (§3) |
| `…/governed_model.py` (357) | **REPLACE** | Fake-streaming `_stream` block-then-yield (`:335-345`) + profile wrap → product-layer model policy hook gọi kernel broker (W3 C4) |
| `…/plugin.py` (196) | **REPLACE** | In-process ASGI mount (`ASGITransport` `:100-103`) + header allowlist (`:29-36`) → extension shim: HTTP client tới kernel service + capability tool registration; W3 điều kiện 1 cấm in-process mount |
| `…/__init__.py` + `pyproject.toml` + `README.md` | **PORT** | Extension skeleton mang qua; README viết lại nội dung |
| `…/tests/test_{foundation_port,governed_model,plugin}.py` (889) | **REPLACE** | Test theo seam mới (AgentRuns mock + HTTP kernel mock) |
| `frontend/src/ewcp/components/manifest-card.tsx` (112) | **PORT** | Manifest review surface → workspace view |
| `…/decision-card.tsx` (121) | **PORT** | HumanDecision card → workspace/interrupt card (W4 §2.1: form native thay mailbox) |
| `…/verify-view.tsx` (356) | **PORT** | Verify surface (đọc `seal_ok`, checks) → `/verify` public page |
| `…/unverified-badge.tsx` (32) | **PORT** | Badge trên deliverable cards |
| `…/share-verify-link.tsx` (78) | **PORT** | + rework sang signed-share-token (D13) |
| `…/pack-gallery.tsx` (140) | **PORT** | Pack gallery → capability gallery trong workspace |
| `…/studio-card.tsx` (414) | **PORT** | Outcome card surface [suy luận: re-home thành ExecutionRun detail view] |
| `…/labels.ts` (153) | **PORT** | Vietnamese label maps tái dùng nguyên |
| `frontend/src/app/ewcp/verify/page.tsx` (33) | **PORT** | Public verify route — path mới `/verify/[hash]` ngoài pane |
| `frontend/src/ewcp/api.ts` (412) | **REPLACE** | `x-ewcp-api-key` client (`:89`) → product API client trên upstream auth (session/PAT); kernel key chỉ còn M2M |
| `…/exploratory.ts` (126) | **REPLACE** | Thread bridge → AgentRuns-based run launcher |
| `…/registry.ts` (209) | **REPLACE** | Outcome registry client → capability registry client (kernel `/capabilities`) |
| `…/components/general-card.tsx` (254) | **REPLACE** | Governed-run card → ExecutionRun card (ontology mới) |
| `…/task-list.tsx` (167) | **REPLACE** | Task list → ExecutionRun list view |
| `…/task-thread.tsx` (623) | **REPLACE** | `setInterval` polling (`:117`) → SSE run events (upstream `join_run`); decision/manifest embeds giữ qua components PORT |
| `frontend/src/ewcp/README.md` | **DROP** | Doc của pane; viết lại trong tài liệu vNext |
| `frontend/src/app/ewcp/page.tsx` (260) | **DROP** | Pane frame — parallel workbench chết (W3 D11/Q4); surfaces đã PORT |
| `frontend/tests/unit/ewcp/` — 6 file test cho components PORT | **PORT** | `decision-card.dom`, `labels.test`, `pack-gallery.dom`, `share-verify-link.dom`, `studio-card.dom`, `verify-view.dom` (929 LOC) — test đi theo component |
| `frontend/tests/unit/ewcp/` — 6 file cho phần REPLACE | **REPLACE** | `api.test`, `exploratory.test`, `registry.test`, `general-card.dom`, `task-list.dom`, `task-thread.dom` (1.205 LOC) |
| `docker/nginx/nginx.conf` + `nginx.local.conf` (2 hunks) | **DROP** | `/api/ewcp` location + 660s — vestigial khi kernel thành service riêng (route không còn đi qua product nginx); intake đã async (`kernel:app.py:2536-2548`) |
| `frontend/next.config.js` hunk | **DROP** | `proxyTimeout: 660_000` workaround cho sync intake/resume — vNext intake = AgentRuns + SSE, không sync POST dài |
| `workspace-nav-chat-list.tsx` hunk | **REPLACE** | Nav link `/ewcp` → entry point workspace mới (capability/verify sections) |

**Đếm fork (45 files):** KEEP **2** · PORT **18** · REPLACE **20** · DROP **5**.

### 2c. Tổng hợp

| Verdict | Kernel modules | Fork files | Tổng |
|---|---|---|---|
| KEEP | 21 | 2 | **23** |
| PORT | 0 | 18 | **18** |
| REPLACE | 0 | 20 | **20** |
| DROP | 6 | 5 | **11** |

Theo LOC (fork delta 7.865): KEEP ~108 · PORT ~2.475 · REPLACE ~4.943 ·
DROP ~339 — cộng lại đúng 7.865. [đo theo bảng §1; LOC của REPLACE là upper
bound vì implementation mới nhỏ hơn nhiều — seam adapter ~250–400 LOC vs bridge
cũ ~3.4k, W3 C2]

**Lưu ý chi phí thật** (W2 §5 + W4 §1.2): `git apply --check` toàn bộ delta lên
`main` CLEAN — nhưng port không phải chuyển file; phần đắt là viết lại semantics
(WorkRun≠thread, filesystem protocol, lane profile). "Adapter nhỏ hơn ~10×" của
reviewer chỉ đúng cho **bridge seam** (~3.4k → ~250-400 LOC [suy luận, W3 C2]) —
verification/grants/budget dời chỗ chứ không co lại.

---

## 3. Adapter seam spec — verify trên `53df22bd`

Mỗi seam được grep trực tiếp trong checkout `product/vnext`:

| # | Seam | Tồn tại? | Evidence trên `53df22bd` |
|---|---|---|---|
| S1 | **AgentRuns** (run lifecycle) | ✓ | `vnext:backend/packages/extension-api/deerflow_extension_api/agent_runs.py:30-84` — Protocol `for_plugin` + `create_thread`/`start`/`resume`/`get`/`get_state`/`wait`/`cancel`; `start` có `idempotency_key` native (`:51`). Host impl: `backend/app/gateway/extension_agent_runs.py` — `bind()` chỉ nhận SESSION/AUTH_DISABLED (`:51-61`), per-op permission recheck qua `_request` (`:85-88`). Doc: `backend/docs/extension-agent-runs.md` |
| S2 | **stream_events** (run event stream) | ✓ | `vnext:backend/app/gateway/routers/thread_runs.py` — `POST /{tid}/runs/stream` SSE (`:970`), `GET /{tid}/runs/{rid}/join` SSE (`:1312`), `POST /{tid}/runs/{rid}/stream` (`:1430`); stateless `POST /api/runs/stream` (`routers/runs.py:39`). Event categories: `backend/docs/RUN_EVENT_STREAM.md:55-60` (`message/trace/outputs/error/middleware/context/subagent`). Kèm `packages/harness/deerflow/workspace_changes/` (diff/evidence surface) |
| S3 | **upload_files** | ✓ | `vnext:backend/app/gateway/routers/uploads.py` — `POST /api/threads/{id}/uploads` (`:388`), `GET …/list` (`:484`), `DELETE …/{filename}` (`:496`); embedded client `packages/harness/deerflow/client.py:1712` `upload_files()`. Doc: `backend/docs/FILE_UPLOAD.md` |
| S4 | **config/skills surfaces** | ✓ | `vnext:config.example.yaml` `plugins:` block (`:3410` — operator-controlled, extension entry); `routers/skills.py` `/api/skills` (list/inspect/toggle/install); `extensions_config.json` MCP servers (`/api/mcp/config`). Interaction policy: `backend/docs/RUN_INTERACTION_POLICY.md` — `non_interactive`/`disable_clarification` là context keys internal-caller-only (thay `_EwcpRunFlagsMiddleware` tự chế) |

**Những gì KHÔNG nằm trong adapter (theo W3 Q5 — product-layer, không phải seam):**

- **Budget cap mid-run** → Model Policy Layer riêng (hook đọc run-scoped cap +
  egress allowlist; kernel `model_broker` giữ ledger). Upstream `models/factory.py`
  + profiles tồn tại nhưng **không** có per-run $ budget — đây là work mới thật.
- **invoke_capability (governed sub-run)** → đi qua 2 surface kernel đã tồn tại
  (`invoke.py` envelope hoặc `mcp/server.py` tools), không phải AgentRuns.

**Constraints của AgentRuns cần thiết kế quanh** (đọc từ source):

- Handle không serializable qua restart — persist `thread_id`/`run_id`, reacquire
  từ request authenticated (`agent_runs.py:33-35,87-95`).
- `bind()` loại PAT (`extension_agent_runs.py:61`) → các flow không do request
  khởi tạo (scheduler, repair loop) cần system-identity path riêng — **chưa có
  trên upstream** [gap — mở câu hỏi §7].

---

## 4. Phased implementation plan

Quy ước: 1 Devin session ≈ 1–2 tuần người. Estimate cộng từ W4 (A+ = 4–6 sess
cho phần hybrid) + reviewer ordering của founder.

### Track P0 (parallel, kernel repo) — protocol-edge fixes ~1 session

Vẫn cần **dù chọn hướng nào** vì kernel tiếp tục là capability server:

| # | Fix | Evidence |
|---|---|---|
| P0a | `GeminiClient` — không để `?key=` vào traceback/log | `kernel:src/ewcp/runtime/gemini_client.py:256-263` — `params={"key":…}` lộ API key qua httpx error message **[security — làm trước hết]** |
| P0b | Turn degrade về `declare_done` khi agent trả text thay vì raise `KernelError` | `kernel:src/ewcp/foundation/executor.py:332-333`; W1 t2 fail 2/2 dù deliverable đúng |
| P0c | `file_exists(uploads/x)` resolve cả prefixed intake `uploads/NNNN-x` | `kernel:src/ewcp/checks/engine.py:54-65` — `_resolve` chỉ tolerate `outputs/` prefix; W1 t5 kẹt approve |
| P0d | `state_warnings` echo ngược cho agent thay vì drop lặng | `kernel:src/ewcp/foundation/executor.py:216-217` |

### Phase 1 — Vanilla baseline + CI + Solver Lab (~1–1.5 sess)

- **Entry:** `product/vnext` tồn tại (✓ `53df22bd`); blueprint cho branch.
- **Work:** `make config && make install && make dev` boot sạch; CI green trên
  nhánh; Solver Lab harness + corpus + baseline report (§6); amend block vào
  `UPSTREAM_SYNC.md`-tương-đương trên vNext ghi dev-line pin.
- **Exit criteria:** vanilla lane trả lời/ship được task end-to-end trên CI-môi
  trường; baseline solver report land; pin-policy amendment doc merged.
- **Rollback:** không có product change — xóa nhánh là xong.

### Phase 2 — General AI Workspace (~1–2 sess)

- **Entry:** Phase 1 xong.
- **Work:** workspace UX native hoàn chỉnh (threads/uploads/artifacts/skills/
  MCP settings); các surface EWCP-specific để chỗ trống (stub sections).
- **Exit:** người dùng chạy được full-capability agent trên vNext; uploads→
  outputs e2e; không EWCP code nào trong path này.
- **Rollback:** workspace chỉ là upstream — revert = không ship.

### Phase 3 — EWCP Product Core port (~3–4 sess; lớn nhất)

- **Entry:** Solver Lab baseline có số; seam spec §3 verified (đã xong trong doc này).
- **Work:** (i) thin adapter ~250–400 LOC trên AgentRuns + run-events + uploads;
  (ii) ExecutionRun/Artifact/VerificationRun model phía product (runtime refs là
  fields, không phải naming convention); (iii) kernel = service HTTP riêng —
  gỡ in-process ASGI mount (W3 điều kiện 1); (iv) rehome surfaces PORT
  (manifest/decision/verify/badge/gallery) vào workspace; (v) `/verify` bọc
  signed-share-token (scope+expiry+revocation) + legacy `/verify/{hash}` redirect;
  (vi) `x-ewcp-api-key` → chỉ còn M2M giữa product↔kernel.
- **Exit:** 1 governed pack run e2e: intake qua product → kernel capability →
  sealed manifest → verify permalink từ UI workspace; pane cũ không còn là đường
  duy nhất.
- **Rollback:** `ewcp/main` + kernel hiện tại vẫn nguyên và chạy được — vNext
  chưa chiếm production path nào.
- **Risk riêng:** system-identity path cho non-request-initiated runs
  (scheduler/repair) — AgentRuns bind cần authenticated request; giải pháp
  `for_plugin` namespace + service account [giả định — cần spike].

### Phase 4 — Odoo integration (**scope MỚI**, ~1–3 sess + discovery)

Xem §5 — chưa estimate được trước discovery.
- **Entry:** founder trả lời open questions §5; Odoo instance + credentials sẵn.
- **Exit:** ít nhất 1 business capability gọi Odoo qua contract+verify.
- **Rollback:** capability off; không ảnh hưởng lane khác.
- **Dependency note:** nếu Odoo capability muốn governed (contract-first + seal),
  nó cần machinery của Phase 5 — thứ tự 4-trước-5 chỉ khả thi nếu Odoo phase 1
  là pure upstream extension (tools/skills), chưa governed [suy luận].

### Phase 5 — Hybrid execution (~2–3 sess)

- **Entry:** Phase 3 xong + Solver Lab có số liệu parity (§6).
- **Work:** `invoke_capability`/MCP-shim như tool trong general lane; governed
  fast-path (task quen → pack deterministic trước, agent fallback); model
  policy layer (per-run $ cap + egress) nối kernel broker; 2-gate policy theo
  TaskMode (GOVERNED_OUTCOME giữ contract+seal gates; mode thấp hơn giảm —
  enforcement path cho invariant-4 autonomy dial, W4 §2.2).
- **Exit:** agent gọi capability → sealed deliverable → verify link, trong 1
  workspace run; budget cap chặn được overspend (test bằng giới hạn nhỏ).
- **Rollback:** capability tool flag off; general lane vẫn vanilla-complete.

### Phase 6 — Product experience (~2 sess)

- **Entry:** Phase 3+5 surfaces ổn định.
- **Work:** gallery/cards polish, verify UX, onboarding flow, docs;
  quyết định retire `ewcp/main` pane sau parity sign-off.
- **Exit:** không còn đường UX nào cần `/ewcp` pane; UPSTREAM_TOUCH counter
  vNext ≤ budget.
- **Rollback:** cosmetic — tắt surfaces riêng lẻ.

**Tổng:** ~9–13.5 sessions — nằm giữa estimate W4 cho A+ (4–6, chỉ phần hybrid
core) và brief nguyên bản (8–13) vì plan này gồm cả workspace baseline + Odoo
discovery + product experience mà các estimate kia không tính [giả định].

---

## 5. Odoo — scope MỚI (council chưa đánh giá)

**Đánh dấu rõ: phần này không có trong bất kỳ council artifact nào** — toàn bộ
dưới đây là phân tích mới, claim yếu gắn nhãn.

### 5.1 Nó ngầm định gì

- **Odoo là system riêng**, không phải module trong EWCP — product connect qua
  API (XML-RPC/JSON-RPC/REST tùy edition) hoặc extension. [giả định từ
  "Odoo Integration" trong direction doc]
- **Business capabilities gọi Odoo**: ví dụ pack `invoice_recon` đọc hóa đơn từ
  Odoo `account.move`, `congno_doi_chieu` đối chiếu công nợ với ledger Odoo,
  `three_way_match` match PO/receipt/invoice trong Odoo — 3/4 pack hiện có map
  trực tiếp sang Odoo objects [suy luận từ tên pack — chưa có API survey].
- Mô hình khả dĩ nhất theo kiến trúc Option A+: **Odoo connector là một
  enterprise capability** (upstream extension hoặc kernel capability), agent
  lane gọi qua `invoke_capability`/MCP như mọi capability khác — Odoo không
  thành execution substrate.

### 5.2 Evidence đang thiếu [không đo được]

- Không có Odoo deployment nào trong org để survey (không instance, không
  credentials, không version).
- Không có API contract survey: Odoo Online/Odoo.sh/self-hosted khác nhau về API
  access (XML-RPC external API chỉ certain plans; custom module cần on-prem/
  Odoo.sh); edition nào quyết định seam nào.
- Không rõ data-residency interplay: founder constraint "data lưu tại doanh
  nghiệp" (kernel `AGENTS.md`) — Odoo Online là SaaS → nếu dùng Online, dữ liệu
  kế toán rời khỏi môi trường khách, **mâu thuẫn trực tiếp với constraint**
  [suy luận — cần founder xác nhận deployment model trước khi thiết kế].
- Chưa biết write-model: capability chỉ đọc Odoo (report/recon) hay ghi
  (tạo invoice/bill)? Ghi vào ERP là privileged+irreversible boundary —
  theo invariant R5 phải qua grant+contract gate.

### 5.3 Open questions cho founder

1. Odoo edition/deployment nào (Community self-host, Enterprise on-prem,
   Odoo.sh, Online)? Quyết định cả seam lẫn residency story.
2. Read-only hay read-write? (read-only → capability rẻ; write → cần
   CapabilityGrant + contract gate + audit trail từ ngày đầu)
3. Business capability đầu tiên gọi Odoo là gì — recon/3-way-match/AR-AP?
4. Odoo có phải *the* ERP target hay là instance đầu của "ERP-agnostic
   connector" (MISA/SAP/NetSuite sau)? — quyết định có build abstraction
   `ErpPort` hay thẳng Odoo client.
5. Licensing: Odoo Community (LGPL) vs Enterprise (proprietary, cần license
   khách) — ảnh hưởng distribution story của product.

---

## 6. Solver Lab — design note (parity benchmark gating pack-lane retirement)

Điều kiện W3 #2 + verdict W4 #3: **packs không retire cho tới khi agent lane
chứng minh parity trên domain corpus** — nếu không packs ở lại kernel mãi và đó
là feature, không phải debt.

### 6.1 Thiết kế

| Biến | Spec |
|---|---|
| Corpus | ≥20 task: ≥8 task domain của 4 packs (recon/công nợ/3-way/dossier — fixture ẩn danh sẵn có trong `kernel:fixtures/`), ≥4 task general W1 corpus, ≥4 governance questions (run nào cần contract? verify rate? — W4 §1.4: corpus phải đo governance chứ không chỉ task-success) |
| Arms | (a) vanilla deer-flow trên `product/vnext`; (b) governed lane = kernel capability path (pack fast-path + agent fallback); optional (c) governed-full-agent (agent làm lại việc pack) để đo "packs có còn đáng giữ" |
| Model pin | Cùng wire model cả 2 arm (W1 dùng `gemini-3.1-flash-lite`; nên đo lại với model mạnh hơn — pin yếu là flake source đã biết) |
| Metrics | outcome correctness (per-task verifier deterministic — cùng check engine), **seal rate**, tokens/verified-outcome, **human interventions**, wall-time (ghi confound quota), $/verified-outcome |
| Driver | auto-approve decisions (như `/tmp/bench/bench.py` của W1) nhưng interventions vẫn đếm; không sửa tay giữa run |

### 6.2 Tiêu chuẩn quyết định (đề xuất, founder duyệt)

- **Retire pack lane** chỉ khi arm (c) match arm (b) trên outcome correctness
  trong corpus domain **và** seal rate tương đương.
- **Giữ packs mãi** nếu deterministic pipeline thắng trên correctness/$ —
  theo W4: "cùng input → cùng output → cùng seal" là thứ kế toán trưởng ký
  được mà không cần tin model.
- Chạy **sớm** (P0.5/Phase 1) không để cuối roadmap — bake-off sau khi product
  model đã dựng là sunk-cost trap (W4 §1.4).

### 6.3 Baseline đã có (W1 — tái sử dụng làm seed corpus)

5 task, cùng `gemini-3.1-flash-lite`: governed đúng 5/5 deliverable vs vanilla
4/5 (t2 sai số); governed 3/5 verified trực tiếp; tokens governed 4.2–6.8×;
interventions 0 vs ≥2. Confounds: n=5, quota storm 15 RPM, 2 flake đối xứng.
→ Solver Lab phải n≥20 + model pin mạnh hơn + paid-tier quota mới kết luận được
[W1 §5 confounds].

---

## 7. Risks & open questions

### 7.1 Dev-line pin — amendment tường minh (ghi nhận Q42)

Policy hiện hành `fork:docs/ewcp/UPSTREAM_SYNC.md:10-11`: *"Track tags, not
`main`… never a moving branch."* — `product/vnext` = upstream `main` tip
**phá policy này có chủ đích**. Amendment được ghi nhận:

- **Lý do:** `AgentRuns` extension API (PR #6190, `beaae220`) là seam bắt buộc
  của thin adapter và **chưa nằm trong release nào** — absent tại `v2.1.0`
  (`git show 345f08be:backend/docs/extension-agent-runs.md` → không tồn tại).
- **Scope ngoại lệ:** chỉ `product/vnext`. `ewcp/main` tiếp tục pin tag v2.1.0
  tới khi retire.
- **Pin thật:** `53df22bd` — branch theo tip nhưng mọi deploy/build ghi SHA;
  sync cadence mới = merge tip định kỳ + re-run seam checks §3 (mỗi sync re-verify
  `agent_runs.py` contract không drift).
- **Exit criteria:** quay lại track tags ngay khi upstream ship release đầu
  tiên chứa `agent_runs.py` (dự kiến tag > v2.1.0) — lúc đó `product/vnext`
  re-pin sang tag và resume `UPSTREAM_SYNC.md` cadence chuẩn.
- **Revert path:** nếu `AgentRuns` bị upstream revert/breaking-change trước
  release → fallback Option B (fix-in-place trên ewcp/main, 3–5 sess, W4 §2.3).

### 7.2 Risk register

| Risk | Evidence | Mitigation |
|---|---|---|
| `AgentRuns` community maturity = 0 | API chỉ có post-v2.1.0, chưa release, chưa có external extension nào dùng [đo: chỉ `test_extension_agent_runs.py` trong repo] | Contract pinned bởi upstream test suite + `api="0.2.0"` versioning; spike Phase 3 trước khi commit; fallback Option B |
| Handles không serializable + bind cần authenticated request | `agent_runs.py:33-35,87-95`; `extension_agent_runs.py:51-61` (PAT bị loại) | System-identity path cho scheduler/repair = gap cần upstream-hoặc-patch; **đây là UPSTREAM_TOUCH đầu tiên có thể phải xin của vNext** [suy luận] |
| 2-gate UX vs frontier autonomy | W1: ≥2 interventions/run (contract + candidate approve); `spec.py:37` hard-code `human_approval` required; `engine.py:300-311` seal FAIL không có approve | TaskMode-gated gates (Phase 5): GOVERNED_OUTCOME giữ 2 gate; mode thấp hơn = seal-only/audit-only — enforcement path cho invariant-4 |
| Token overhead 4–7× | W1 §2: governed 122–221k vs vanilla 21–83k; SOUL prompt ~10.7k/call + 2-phase | Filesystem protocol chết theo lane mới → overhead protocol phần lớn tự mất; budget cap per-run là cost control, không phải cost cut |
| Kernel-as-service ops cost | Kernel hiện mount in-process (`fork:plugin.py:100-103`); thành HTTP service riêng = +1 deployable (health, secrets, version skew) [suy luận] | Sidecar compose service trong cùng chart; version lockstep product↔kernel contract test |
| 519-commit drift → sync cadence mới undefined | W4 §1.5.1: tip-merge per tag? cherry-pick? | Định nghĩa trong Phase 1 amendment (§7.1); CI trên `product/vnext` chạy seam contract tests mỗi sync |
| Dual capability surface (invoke vs MCP shim) | `invoke.py` 619 LOC in-process vs `mcp/server.py` 598 LOC network — 2 đường vào kernel | Chọn 1 surface canonical trong Phase 3 (đề xuất: MCP shim cho cross-process, invoke chỉ khi in-process) [suy luận] |
| Verify permalink migration | `app.py:3166-3184` hash-as-capability public; link đã phát hành | Signed-share-token bọc + legacy redirect; hash giữ vai trò integrity anchor (D13/C7) |
| Odoo scope chưa validated | §5.2 — không deployment/API survey/residency story | Founder trả lời §5.3 trước Phase 4; không commit build trước discovery |

### 7.3 Open questions (ngoài Q42–44 đã trả lời)

1. System-identity path cho non-request runs trên AgentRuns — upstream có
   intended seam không, hay đây là generic hook cần xin? (ảnh hưởng
   UPSTREAM_TOUCH budget của vNext)
2. Pack-lane parity threshold — con số cụ thể cho "match pack accuracy"
   (correctness delta ≤ bao nhiêu, trên corpus nào) cần founder định trước
   Solver Lab chạy.
3. `ewcp/main` retirement criteria — sau khi vNext ổn định, pane/branch cũ
   archive hay xóa? `runs.db` existing tenants migrate hay freeze?
4. Odoo §5.3 (5 câu).
5. Multi-instance deploy: kernel service scale riêng hay co-locate với Gateway
   pod? [giả định: co-locate đơn giản hơn ở size hiện tại]

---

## 8. Corrections found on re-verification (vs council docs)

Các claim council **sai hoặc lệch** khi đo lại trên repo thật:

| # | Council claim | Đo lại | Verdict |
|---|---|---|---|
| 1 | W4 §0: kernel `src/ewcp` = **18.596 LOC** | `find src/ewcp -name '*.py' \| xargs wc -l` = **21.356 LOC** (96 file) | **SAI** — W4 cộng thiếu sandbox/runtime/workspace/policy/root (2.760 LOC) dù parenthetical ghi "còn lại" |
| 2 | W3 D11: "`page.tsx` ~2900 LOC + `src/ewcp/` ~900 LOC" | `page.tsx` = **260** (`app/ewcp` tổng 293); `src/ewcp/` = **3.224** | **SAI split** — tổng pane ~3.5k đúng nhưng phân bổ ngược: khung pane mỏng, components dày. Hệ quả: rehome rẻ hơn W3 tưởng (frame 260 LOC không phải 2900) nhưng port components đắt hơn |
| 3 | W4 §2.1: `task-thread.tsx:147` `setInterval` poll | Actual: `:117` | Minor — cite lệch 30 dòng, claim đúng |
| 4 | W4 §0: `mcp/server.py` 606 LOC | Actual trên `b99ebf0`: **598** | Drift giữa `0ec7c8f`→`b99ebf0` — không phải lỗi, ghi nhận revision |

Mọi con số khác verify **đúng**: 45 files / 7.865 LOC / 41A+4M+0D (§1),
42 ahead / 519 behind, 10 commit rc0→v2.1.0, FoundationPort 394 LOC,
`foundation_port.py` line-cites (`:60,63,124-129,143-160`), async-intake
`app.py:2536-2548`, `/verify` permalink `:3166`, 2 mandatory gates
(`spec.py:37` + `engine.py:300-311` + `executor.py:506-523`), `?key=` leak
surface `gemini_client.py:256-263`, AgentRuns 7-method protocol
`agent_runs.py:30-84` (W1 nói "post-v2.1.0" — verified: file absent tại tag
`345f08be`).

---

*Audit này là docs-only: không dòng code nào ngoài `docs/vnext/` bị động vào.*
