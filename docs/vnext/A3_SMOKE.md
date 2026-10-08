# A3 Smoke — live E2E proof (Task 8)

**Ngày:** 2026-10-08. **Phạm vi:** Task 8 của
`docs/plans/2026-10-08-a3-product-core-port.md` — E2E proof cả hai lanes +
durability scenarios trên stack thật (kernel HTTP + gateway + frontend).

Mọi kết quả dưới đây đo trên VM thật, không stub. Repro trên clean
checkout: `product/vnext` + nhánh này; kernel `enterprise-work-control-plane`
@ `9d26e54` (main).

## Boot (clean checkout)

```bash
# 0. repo deps
cd backend && uv sync --locked && cd ..

# 1. config.yaml (gitignored) — plugins: block đầy đủ tối thiểu
#    models[0] phải là một model còn phục vụ (xem Limitations #1).
cat >> config.yaml <<'YAML'
plugins:
  - name: ewcp_core
    package: ewcp-core
    use: ewcp_core:install
    enabled: true
    required: true
    table_prefix: ewcp_
    config:
      kernel_url: http://127.0.0.1:8080
      kernel_api_key: null            # hoặc EWCP_KERNEL_API_KEY env
      budget:
        cap_usd: "5.00"
        usd_per_1k_tokens: "0.004"
        max_output_tokens_per_call: 4096
        general_on_policy_unavailable: local
      egress:
        default_mode: local_only
        tenant_modes: {}
        tenant_classes: {}            # "default: sensitive" cho scenario egress
        local_model_endpoints: []
        allowed_model_endpoints: []
        approved_model_endpoints: []
        allowed_domains: []
        allowed_tools: []
YAML

# 2. extension editable install + gateway
cd backend
uv pip install --python .venv/bin/python -e ./extensions/ewcp-core
DEER_FLOW_AUTH_DISABLED=1 \
EWCP_KERNEL_URL=http://127.0.0.1:8080 \
EWCP_KERNEL_API_KEY=ewcp-demo-7f3a \
  .venv/bin/uvicorn app.gateway.app:app --port 8001 &

# 3. kernel (repo hoangtien07/enterprise-work-control-plane)
cd <kernel-repo>
EWCP_REQUIRE_AUTH=1 EWCP_TENANT_KEYS='ewcp-demo-7f3a:demo' \
EWCP_SEAL_KEY=<random-hex> EWCP_STORE_DIR=<dir> EWCP_WORK_DIR=<dir> \
  .venv/bin/uvicorn app.main:app --port 8080 &

# 4. frontend (cho permalink render)
cd frontend && pnpm dev   # :3000, rewrites /api/* → 127.0.0.1:8001
```

Sanity: `curl -s localhost:8001/api/ewcp/_status` →
`client_started:true, kernel_configured:true, recovery_started:true`.
Startup log phải in `Extension routers mounted: ... /api/ewcp/*` — nếu
thấy `router could not be mounted` thì router fix ở task này chưa có.

## Kết quả đo được

| # | Scenario | Expected | Measured | Verdict |
|---|---|---|---|---|
| A | General task, native lane | DeerFlow run completes; kernel không bị đụng | `er_2b523fc2` → `completed` (answer "391"); kernel `GET /workruns` = `[]` | PASS |
| B | Governed → kernel → seal → `/verify` | workrun verified, manifest sealed, permalink anonymous render | `wr-912993918ef7` → `verified`, manifest `986e46ec…`; `GET /api/ewcp/verify/{hash}` (no auth) → manifest+seal; `/verify/<hash>` render "PASS — niêm phong hợp lệ", 5 checks xanh, 4 artifacts; product-side bound `er_52b77eb4` → `GET /runs/{id}/workrun` proxy → `verified` | PASS |
| C | Restart gateway giữa run | run mất → reconcile `failed`/interrupted; map persist | `SIGKILL` uvicorn lúc `er_2a5b5d56` running → relaunch → `GET /api/ewcp/runs` trả đủ 10 row; killed row → `failed` | PASS |
| D | Interrupt → resume | cancel giữ checkpoint → `pending_interrupt` → resume cùng thread → complete | `POST /api/threads/{t}/runs/{r}/cancel?action=interrupt` trên `er_9ee53137` → `pending_interrupt` → `POST /api/ewcp/runs/{id}/resume {"resume":"continue"}` → run_id mới `62b8a980` trên cùng thread → `completed` | PASS |
| E | Egress deny `local_only` | model call bị deny fail-closed trước provider | `tenant_classes: {default: sensitive}` + restart → run `er_3e9d629f` → `completed` với AI message `EWCP egress policy denied this model call. channel=model … local_only — denied (fail-closed)`; log không có `did not call the downstream handler` | PASS |
| F | Budget rejection | reserve > cap → kernel 402 → deny trước provider call | `cap_usd: "0.0001"` + restart → run `er_91d8c777` → `failed`, AI msg `ewcp budget denied … reserve $0.016576 would breach cap $0.0001 — denied before the call`; kernel log `POST /budget/admissions → 402`; `GET /budget/accounts/{id}` → `unknown budget account` (không reservation nào đổ bộ) | PASS |

Repro nhanh từng scenario — lệnh đúng y chang đo được:

```bash
# A — general lane
curl -s -X POST localhost:8001/api/ewcp/runs \
  -F "intent=<task>" -F task_mode=general -F "idempotency_key=<key>"
curl -s "localhost:8001/api/ewcp/runs/{execution_run_id}?refresh=true"
curl -s -H "X-API-Key: ewcp-demo-7f3a" localhost:8080/workruns   # vẫn []

# B — governed (kernel intake → decisions → seal)
curl -s -X POST localhost:8080/tasks -H "X-API-Key: ewcp-demo-7f3a" \
  -F "intent=Đối soát hóa đơn đầu vào kỳ 2025-09" \
  -F "invoices_zip=@invoices.zip" -F "books=@misa_export.csv"
# trả lời pending_questions qua POST /workruns/{id}/decisions
#   {"decision_id": ..., "answer": <option_id>} — cuối cùng {"answer":"approve"}
curl -s localhost:8001/api/ewcp/verify/<manifest_hash>          # anonymous
# browser: http://localhost:3000/verify/<manifest_hash>         # PASS card

# C — restart: kill -9 <uvicorn pid> giữa run → restart → GET /api/ewcp/runs

# D — interrupt/resume:
curl -s -X POST "localhost:8001/api/threads/{tid}/runs/{rid}/cancel?action=interrupt"
curl -s "localhost:8001/api/ewcp/runs/{er}?refresh=true"        # pending_interrupt
curl -s -X POST "localhost:8001/api/ewcp/runs/{er}/resume" \
  -H 'Content-Type: application/json' -d '{"resume":"continue where you left off"}'

# E — egress: config egress.tenant_classes.default=sensitive → restart → launch
#     → AI message chứa "EWCP egress policy denied this model call"

# F — budget: config budget.cap_usd="0.0001" → restart → launch
#     → kernel POST /budget/admissions 402 → run failed, AI msg "denied before the call"
```

## Bugs found & fixed in this task (merged T-code was broken on the live stack)

1. **`/api/ewcp/*` contributed router never mounted** — `build_router()`
   dùng `router.include_router(api_router)`; FastAPI 0.136 merge một
   `lifespan_context` function vào router, host gateway
   (`extensions/gateway.py` `_router_routes`) reject vì `lifespan_context
   is not _DefaultLifespan` → "router could not be mounted; continuing
   without it" → toàn bộ surface T6/T7 trả 404. Test suite pass vì mount
   trực tiếp, không qua host preflight. Fix: inner router bake prefix
   `/api/ewcp` vào từng route + `router.routes.extend(...)` (không
   include_router). Startup log giờ in `Extension routers mounted: …`
   đầy đủ.
2. **Egress deny bị nuốt bởi `IsolatedMiddleware` (fail-open)** —
   deny path trả AIMessage không gọi `handler` → wrapper báo
   `did not call the downstream handler` → skip → call xuyên qua →
   model thật trả lời (đo được: answer "10" thay vì deny). Fix:
   `intercepting=True` trên cả hai `MiddlewarePlacement` (MODEL_PHYSICAL,
   TOOL_RAW) — cùng quy ước budget middleware (`plugin.py`). Test mới
   `test_placements_are_intercepting` chốt regression.

Không có host-file edit (`app/`, `packages/`) — `UPSTREAM_TOUCH.md` không
cần mục mới. Không có kernel repo change — kernel contract đủ dùng.

## Limitations — ghi đầy đủ

1. **Model pin drift**: `gemini-2.5-flash` bị Google retire ngay trong
   phiên proof (404 NOT_FOUND) → chuyển `gemini-3.8-flash`. Smoke doc chỉ
   yêu cầu `models[0]` là một model còn phục vụ — không pin cứng.
2. **Free-tier quota cạn**: `gemini-3.8-flash` free tier = 20 req/day,
   hết giữa chừng (429 RESOURCE_EXHAUSTED, retry ~5h). Các scenario
   deny-path (E, F) không cần provider trả lời — deny xảy ra trước call.
   Một số run phụ thuộc model trả `failed` vì quota là expected-noise,
   không phải lỗi product.
3. **`ask_clarification` không tạo LangGraph interrupt**: middleware
   clarification auto-resolve thành tool output → run `completed`,
   không `pending_interrupt`. Pending state thật chỉ đến từ
   `cancel?action=interrupt` (giữ checkpoint) — đó là đường interrupt
   đo được ở scenario D.
4. **Egress endpoint detection fail-closed về phía deny**: model
   `ChatGoogleGenerativeAI` không expose endpoint host quen thuộc →
   `model endpoint unprovable` → deny (đúng semantics local_only). Để
   allow một local model (vLLM/Ollama) cần endpoint tự khai báo trong
   `local_model_endpoints` — chưa verify positive-allow path của model
   local.
5. **Budget denial surface**: 402 trả về dạng AI message "LLM request
   failed: ewcp budget denied …" trong thread (run `failed`), chưa có
   banner/lỗi định danh riêng ở UI pane — follow-up UX.
6. **Governed ExecutionRun binding**: `task_mode=governed` vẫn spawn một
   DeerFlow thread run (presence/bookkeeping + proxies `/workrun`,
   `/manifest`, `/evidence`); compute governed thật nằm ở kernel. Thread
   đó tiêu tốn một model call nhỏ — documented behavior, không phải
   governed-path computation.
7. **Foreground-only recovery** giữ nguyên như A3_DURABILITY.md — không
   có service identity → không có background reconcile loop.
