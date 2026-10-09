# A3 Re-smoke — P1 evidence trên heads hiện tại

**Ngày:** 2026-10-09. **Phạm vi:** re-prove governed lane E2E của A3 trên
bundle hiện tại — fill hàng P1 trong `docs/program/RELEASE_EVIDENCE.md`
(kernel repo). Minimum set theo task: governed run → seal →
verified+accepted events → `/verify/{hash}` public render; CpAO đếm run;
mới chu kỳ này: `delete_run` retention (WP-01); optional: restart giữa
pending auto-accept (#110/#111).

**Bundle đo được (heads thật, không stub):**

| Repo | Branch/SHA |
|---|---|
| `hoangtien07/enterprise-work-control-plane` | `main@154eb0e` (sau #112/#113) |
| `hoangtien07/ewcp-product` | `product/vnext@e8f12bab` (sau #37) |

Mọi kết quả đo trên stack thật: kernel uvicorn :8080 (auth + seal key),
gateway :8001 (extension `ewcp_core` editable), frontend `pnpm dev` :3000.

## Boot (lệnh thật đã chạy)

```bash
# kernel (repo enterprise-work-control-plane @154eb0e)
cd enterprise-work-control-plane
mkdir -p var/ewcp-resmoke var/work-resmoke
EWCP_REQUIRE_AUTH=1 EWCP_TENANT_KEYS='ewcp-demo-7f3a:demo' \
EWCP_SEAL_KEY=<random-hex> \
EWCP_STORE_DIR=var/ewcp-resmoke EWCP_WORK_DIR=var/work-resmoke \
EWCP_LLM_API_KEY=$GEMINI_API_KEY EWCP_LLM_MODEL=gemini-3.5-flash-lite \
  .venv/bin/uvicorn app.main:app --port 8080

# gateway (repo ewcp-product @e8f12bab)
# config.yaml (gitignored): models[0] = gemini-3.8-flash qua
# langchain_google_genai:ChatGoogleGenerativeAI; plugins: block =
# ewcp_core với kernel_url=http://127.0.0.1:8080 + egress/budget giống
# A3_SMOKE.md.
cd backend && uv sync --locked
uv pip install --python .venv/bin/python -e ./extensions/ewcp-core
DEER_FLOW_AUTH_DISABLED=1 EWCP_KERNEL_URL=http://127.0.0.1:8080 \
EWCP_KERNEL_API_KEY=ewcp-demo-7f3a \
  .venv/bin/uvicorn app.gateway.app:app --port 8001

# frontend
cd frontend && pnpm dev   # :3000

# fixture
cd <kernel>/fixtures/recon && zip -j /tmp/inv.zip invoices/*.xml
# books: fixtures/recon/books/misa_export.csv
```

Sanity đo được:
`GET localhost:8001/api/ewcp/_status` →
`{"extension":"ewcp_core","kernel_url":"http://127.0.0.1:8080","kernel_configured":true,"api_key_configured":true,"client_started":true,"store_started":true,...,"recovery_started":true,"budget_cap_usd":"5.00","budget_admission_enabled":true}`.
Boot log in `Extension routers mounted: ewcp_core:install -> /api/ewcp/...`
đầy đủ (12 routes, không `could not be mounted`).

## Kết quả đo được

| # | Scenario | Measured | Verdict |
|---|---|---|---|
| 1 | Governed run → seal → verified+accepted → `/verify/{hash}` | `wr-cf2ee4557310` (và `wr-f429fdbaa7e6` trước đó) → `verified`, manifest `2a88a865c835…`; accept qua `POST /workruns/{id}/accept` → `accepted_at` + event `{actor:"tenant:demo",via:"api"}`; `GET :8080/verify/{hash}` anonymous → `seal_ok:true`, 5 checks pass, 4 artifacts; `GET :8001/api/ewcp/verify/{hash}` anonymous 200 cùng payload; `localhost:3000/verify/{hash}` render "PASS — niêm phong hợp lệ", 5 checks xanh, 4 artifacts | PASS |
| 2 | CpAO đếm run | `GET /metrics/cost-per-accepted-outcome` → `verified_outcomes:1, accepted_outcomes:1, by_outcome_type.invoice_recon{verified:1,accepted:1}` sau run đầu | PASS |
| 3 | `delete_run` retention (WP-01) — MỚI | spend bind vào run qua budget-admission settle (`adm-bd54c6e3c1f64960` → ledger row `workrun_id=wr-f429fdbaa7e6, tenant_id='demo', outcome_type='invoice_recon', usd='0.008432'`); CpAO trước delete: `total_usd=0.008432, accepted=1`; `DELETE /workruns/wr-f429fdbaa7e6` → 204 → run 404, `/verify/{hash}` → 404; CpAO sau delete: **không đổi** — `total_usd=0.008432`, `verified=1`, `accepted=1`. Ledger row + metric_events sống sót (sqlite dump dưới) | PASS |
| 4 | Restart giữa pending auto-accept (#110/#111) — OPTIONAL | kernel với `EWCP_AUTO_ACCEPT_VERIFIED=1`; run `wr-644d3f40cbdc` seal → event verified `{auto_accept_pending:true}` + accepted `{actor:policy:auto_accept_verified, via:"policy"}`. Mô phỏng crash-window bằng cách xoá row accepted trong sqlite (state y hệt kill giữa seal-tx và policy emit) → restart kernel → `reconcile_unaccepted_policy()` re-emit: event mới `{actor:"policy:auto_accept_verified","via":"policy-reconcile"}`; CpAO `accepted=3` — replay idempotent, không double-count | PASS (mô phỏng crash-window, code path reconcile thật) |

## Evidence chi tiết

### 1+2. Governed lane → seal → accept → verify → CpAO

```bash
# governed run (endpoint direct-run của pack — không cần LLM intake)
curl -s -X POST localhost:8080/outcomes/invoice_recon/run \
  -H "X-API-Key: ewcp-demo-7f3a" \
  -F "invoices_zip=@/tmp/inv.zip" -F "books=@misa_export.csv" \
  -F "ky=2025-09" -F "mst_doanh_nghiep=0101234567"
# → {"workrun_id":"wr-f429fdbaa7e6","status":"candidate_complete",
#    "pending_questions":[hd-ee6c…(option_choice), hd-41a7…(anomaly_confirm)]}

# trả lời 2 pending: keep_and_submit + confirm_correct
curl -s -X POST localhost:8080/workruns/wr-f429fdbaa7e6/decisions \
  -H "X-API-Key: ewcp-demo-7f3a" -H 'Content-Type: application/json' \
  -d '{"decision_id":"hd-ee6c775bc559","answer":"keep_and_submit"}'
curl -s -X POST .../decisions -d '{"decision_id":"hd-41a76af348ab","answer":"confirm_correct"}'

# approve → seal
curl -s -X POST .../decisions -d '{"answer":"approve","decided_by":"smoke-rerun"}'
# → {"status":"verified",
#    "manifest_hash":"f5f743c0afe6bb8f4133b3b6fd8909ead54af2b2c9623d81d890377213b0e779",
#    "decision":{"answer":"approve","decided_by":"tenant:demo","decider_label":"smoke-rerun"}}

# accept (A2′ — event 'accepted' tách khỏi seal)
curl -s -X POST localhost:8080/workruns/wr-f429fdbaa7e6/accept \
  -H "X-API-Key: ewcp-demo-7f3a" -d '{}'
# → {"accepted":true,"accepted_at":1791539966.89,"idempotent_replay":false}

# verify public — kernel trực tiếp + gateway proxy, cả hai anonymous
curl -s localhost:8080/verify/f5f743c0…        # no auth header
curl -s localhost:8001/api/ewcp/verify/2a88a865…   # run thứ 2, anonymous
# → seal_ok:true; checks: input_partition, totals_match, traceability,
#   deliverable_integrity, human_approval — tất cả pass; 4 deliverables
#   (reconciliation.xlsx/.json, exceptions_report.md, ingest_report.json)

# browser: http://localhost:3000/verify/2a88a865…
# render "PASS — niêm phong hợp lệ", workrun wr-cf2ee4557310, 5 checks, 4 artifacts
```

`GET /metrics/cost-per-accepted-outcome` sau run 1:

```json
{"verified_outcomes":1,"accepted_outcomes":1,"total_usd":0.0,
 "cost_per_accepted_outcome":0.0,
 "by_outcome_type":{"invoice_recon":{"verified":1,"accepted":1,...}}}
```

(Pack invoice_recon deterministic — không gọi LLM → spend $0 là đúng.)

### 3. `delete_run` retention (WP-01)

Governed pack không phát spend LLM nên spend bind vào workrun qua chính
path metering product-lane của kernel (budget admission → settle — API
thật, không stub):

```bash
curl -s -X POST localhost:8080/budget/admissions -H "X-API-Key: …" \
  -H "Idempotency-Key: resmoke-adm-1" \
  -d '{"execution_run_id":"wr-f429fdbaa7e6","cap_usd":"5.00","reserve_usd":"0.01"}'
# → 201 {"admission_id":"adm-bd54c6e3c1f64960"}
curl -s -X POST localhost:8080/budget/admissions/adm-bd54c6e3c1f64960/settle \
  -d '{"usd":"0.008432","tokens":2107,"model":"gemini-3.8-flash"}'
# → {"settled":true,"charged_usd":"0.008432"}
```

CpAO trước delete: `total_usd=0.008432, productive_usd=0.008432,
verified=1, accepted=1, cost_per_accepted=0.008432`.

```bash
curl -X DELETE localhost:8080/workruns/wr-f429fdbaa7e6 -H "X-API-Key: …"  # → 204
curl localhost:8080/workruns/wr-f429fdbaa7e6    # → 404
curl localhost:8080/verify/f5f743c0…            # → 404 (manifest xoá theo run — by design)
```

CpAO sau delete — **numerator + denominator đều giữ nguyên**:

```json
{"verified_outcomes":1,"accepted_outcomes":1,"total_usd":0.008432,
 "cost_per_accepted_outcome":0.008432}
```

sqlite dump sau delete (ledger + metric_events sống sót, workruns row mất):

```
ledger: (2, 'wr-f429fdbaa7e6', 'gemini-3.8-flash', 2107, '0.008432', 'demo', 'invoice_recon')
metric_events outcome:
  ('demo','verified','wr-f429fdbaa7e6')  {"auto_accept_pending":false,...}
  ('demo','accepted','wr-f429fdbaa7e6')  {"actor":"tenant:demo","via":"api"}
workruns: chỉ còn wr-cf2ee4557310, wr-644d3f40cbdc
```

### 4. Auto-accept reconcile (#110/#111)

Với `EWCP_AUTO_ACCEPT_VERIFIED=1` trên keyed deploy: run
`wr-644d3f40cbdc` seal → hai event: `verified{"auto_accept_pending":true}`
rồi `accepted{"actor":"policy:auto_accept_verified","via":"policy"}`.
Xoá row `accepted` trong sqlite (giả lập process chết giữa seal-tx và
policy emit — persisted state y hệt), restart kernel → boot lifespan gọi
`reconcile_unaccepted_policy()` → event mới:

```
(11,'accepted','wr-644d3f40cbdc') {"actor":"policy:auto_accept_verified","via":"policy-reconcile"}
```

CpAO sau restart: `verified_outcomes=3, accepted_outcomes=3` — replay
idempotent (uq_metric_events_outcome), không double-count. Actor giữ
`policy:…` (không forged human), `via='policy-reconcile'` đánh dấu replay.

## Limitations / labels

1. **Nguồn spend**: pack invoice_recon deterministic → run spend $0.
   Numerator test dùng ledger row ghi bởi budget-admission settle bind
   vào workrun_id (path product-lane metering thật của kernel). Hành vi
   retention của `delete_run` (không đụng bảng ledger) giống hệt với
   spend phát sinh từ LLM call.
2. **Intent-route spend ngoài CpAO (by design)**: `POST /tasks` gọi
   `gemini-3.5-flash-lite` 1 lần (492 tok, $0.0001968) ghi ledger row
   `workrun_id='intent:demo:…'` với `tenant_id=NULL` → `spend_since`
   không đếm (intake metering là overhead pre-run, không gắn workrun).
3. **Crash-window scenario** mô phỏng bằng xoá row `accepted` trực tiếp —
   persisted state không phân biệt được với kill -9 giữa seal-tx và emit;
   reconcile path (lifespan → `reconcile_unaccepted_policy`) là code thật.
4. **Quota**: `gemini-3.5-flash-lite` trả ~1s, không gặp 429/503 lần nào;
   `gemini-3.8-flash` chỉ cần cho `models[0]` của gateway config (boot),
   không gọi trong smoke. Không có item BLOCKED_QUOTA.
5. Delete của sealed run xoá cả manifest → `/verify/{hash}` 404 sau
   delete — by design (tenant action, không phải audit escape hatch);
   ledger + metric_events (telemetry anonymized) giữ lại theo WP-01.

## Khác với smoke trước

- Heads mới: kernel `9d26e54` → `154eb0e` (thêm #109 A2′ metrics split,
  #112 program ledger, #113 retention/reconcile); product `50b0a7fc` →
  `e8f12bab` (thêm #37 WP-02 egress fix). Không phát hiện regression nào
  trên các scenario chạy lại.
- Endpoint governed: smoke trước qua `POST /tasks` (LLM router); lần này
  dùng thêm `POST /outcomes/invoice_recon/run` (direct-run, context qua
  form fields — không cần LLM intake, quota-free).
- Accept path mới đo trực tiếp: `POST /workruns/{id}/accept` →
  `{"actor":"tenant:demo","via":"api"}` (A2′ verified≠accepted split).
- Scenario mới: retention delete (3) + auto-accept reconcile (4).
- Native lane / restart gateway / interrupt-resume / egress deny /
  budget-402 (A–F của A3_SMOKE.md) không chạy lại — task yêu cầu minimum
  governed-lane set; các surface đó không đổi giữa hai heads này.
