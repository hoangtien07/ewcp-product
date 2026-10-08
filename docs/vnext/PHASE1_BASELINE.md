# Phase 1 — Vanilla DeerFlow baseline trên `product/vnext`

Báo cáo kết quả bring-up baseline theo `docs/vnext/MIGRATION_AUDIT.md` §4 Phase 1.
Nhánh kiểm chứng: `product/vnext` @ `9ba92e0f` (= upstream tip `53df22bd` + 2 commit docs).
Ngày chạy: 2026-10-08 (UTC), máy Devin Linux (amd64).

## 1. Kết luận nhanh

| Hạng mục | Kết quả | Bằng chứng |
|---|---|---|
| Install | ✅ sạch sau `make config` + `make install` | §2 |
| Backend tests | ✅ **24.852 passed, 0 failed, 281 skipped** (4 shards) | §3 |
| Backend lint | ✅ `ruff check` + `ruff format --check` sạch | §3 |
| Frontend check | ✅ `pnpm check` sạch (0 errors) | §3 |
| Boot stack | ✅ gateway :8001 healthy + frontend :3000 phục vụ UI | §4 |
| Chat turn thật | ✅ UI → gateway → Gemini → reply `BASELINE_OK` trong 1s | §5 |
| CI trên PR → `product/vnext` | ✅ trigger `pull_request` không lọc branch | §6 |
| CI trên push → `product/vnext` | ⚠️ không có → đã thêm `product/vnext` vào push filter (5 workflow) | §6 |

Không patch code upstream nào. Mọi thay đổi nằm ở config môi trường (gitignored)
và 5 dòng branch-filter trong workflow CI.

## 2. Môi trường và cài đặt

Toolchain trên máy verify:

```
node v24.19.0  ·  pnpm 11.21.0  ·  python 3.12.13  ·  uv 0.12.23
```

Các lệnh (theo README/Makefile):

```bash
git clone <repo> && cd ewcp-product && git checkout product/vnext
make config      # sinh config.yaml + extensions_config.json từ example (gitignored)
make install     # uv sync (backend) + pnpm install (frontend) + pre-commit hooks
```

### Lỗi môi trường duy nhất phải sửa: readabilipy/jsdom

`make test` lần đầu fail 22 test trong `tests/test_web_fetch_relative_links.py`
(mọi shard): `Cannot find module 'jsdom'` khi `readabilipy` gọi
`javascript/ExtractArticle.js`, fallback pure-Python mất `href` của link → assert fail.

Nguyên nhân: thư mục `backend/.venv/lib/python3.12/site-packages/readabilipy/javascript/`
có `ExtractArticle.js` nhưng thiếu `package.json`/`node_modules` (venv đóng gói thiếu
node deps — [suy luận] từ cấu trúc thư mục, không phải lỗi test).

Fix môi trường (không sửa code):

```bash
cd backend/.venv/lib/python3.12/site-packages/readabilipy/javascript && npm install
```

Sau đó 48/48 test file đó pass. Lưu ý: upstream CI **không** chạy bước npm này —
trên CI pure-Python fallback được kỳ vọng là đủ, còn suite test mới đòi JS extractor.
Đây là khác biệt môi trường cần ghi nhận, không phải defect của suite.

## 3. Kết quả verify

```bash
cd backend && make test
# => 24.852 passed, 0 failed, 281 skipped (pytest-xdist 4 shards, mặc định loại live/blocking-IO)

cd backend && make lint
# => ruff check: sạch; ruff format --check: sạch

cd frontend && pnpm check
# => eslint + tsc --noEmit: 0 errors
```

Không có test upstream nào fail thật sau khi bù môi trường jsdom.
Lần chạy đầu còn ~59–62 fail/shard dạng `Environment variable GEMINI_API_KEY not found`
— nguyên nhân là shell nền của runner không inject secret, không phải lỗi repo;
chạy lại `make test` trong process có env secret thì xanh toàn bộ.

## 4. Boot stack (không có nginx trên máy)

`make dev` orchestrates qua nginx, nhưng máy này không có nginx → boot từng service:

```bash
# Gateway :8001 (auth tắt cho baseline local)
cd backend && DEER_FLOW_AUTH_DISABLED=1 PYTHONPATH=. \
  .venv/bin/uvicorn app.gateway.app:app --port 8001 --host 127.0.0.1

# Frontend :3000 — KHÔNG cần sửa .env:
# next.config.js mặc định rewrite /api/langgraph/* -> http://127.0.0.1:8001/api/*
cd frontend && pnpm dev
```

Kiểm tra:

- `GET http://127.0.0.1:8001/health` → `{"status":"healthy","service":"deer-flow-gateway"}`
- `GET http://127.0.0.1:3000/api/langgraph/models` → trả catalog model (proxy hoạt động)
- `http://localhost:3000` → landing render; `/workspace` → yêu cầu tạo admin account
  ở `/setup` (tầng auth của frontend độc lập với `DEER_FLOW_AUTH_DISABLED` của gateway;
  tạo `devin@deerflow.dev` qua form là đủ — bước một lần, không phải code change).

## 5. Chat turn thật (end-to-end)

### Model wiring

- `config.example.yaml` gợi ý `gemini-2.5-flash` — model này **đã deprecated với key
  hiện tại**: Google trả 404 "no longer available to new users… use
  models/gemini-3.8-flash" (đo trực tiếp qua `/v1beta/models` và lỗi runtime).
- `config.yaml` (gitignored) cấu hình 2 model dùng `GEMINI_API_KEY` org secret:

```yaml
models:
  - name: gemini-3.8-flash
    use: langchain_google_genai:ChatGoogleGenerativeAI
    model: gemini-3.8-flash
    google_api_key: $GEMINI_API_KEY   # đúng tên param; gemini_api_key bị warn
    timeout: 600.0
    max_retries: 2
  - name: gemini-3.5-flash-lite     # fallback ít tải hơn
    use: langchain_google_genai:ChatGoogleGenerativeAI
    model: gemini-3.5-flash-lite
    google_api_key: $GEMINI_API_KEY
```

Lưu ý upstream doc lệch chuẩn: `config.example.yaml` ghi `gemini_api_key` nhưng
`langchain_google_genai` warn "Unexpected argument 'gemini_api_key'… Did you mean:
'google_api_key'?" và đẩy xuống `model_kwargs` (vẫn auth được, nhưng fragile).
Dùng `google_api_key` sạch warning.

### Kết quả

- Wire API: `POST /api/threads/{tid}/runs/stream` (assistant `lead_agent`,
  `context.model_name=gemini-3.5-flash-lite`, prompt "Reply with exactly: BASELINE_OK")
  → stream trả `ai` message content `BASELINE_OK`. ✅
- UI: `localhost:3000/workspace/chats/new`, chọn model **Gemini 3.5 Flash Lite**,
  gửi "Reply with exactly: BASELINE_OK" → thread `f3ce7adb-6da5-40bf-8a78-777ae7ba01eb`
  hiển thị reply `BASELINE_OK`, "Took 1s", tokens In 10.5K / Out 3. ✅

### Những gì không hoàn hảo (ghi nhận, không sửa)

- `gemini-3.8-flash` thời điểm verify trả 503 "high demand" từ Google, client
  retry/backoff → run chạy >4 phút chưa xong. Lỗi phía provider, không phải repo.
  `gemini-3.5-flash-lite` trả lời trong ~1s. [không đo được: 3.8 có ổn định lúc khác không]
- SSE `on_disconnect` mặc định `"cancel"`: client ngắt giữa chừng giết run
  (`run_models.py`). Một run bị `interrupted` vì curl timeout — hành vi đúng thiết kế.
- Memory extraction (DeerMem) gọi model riêng theo `config.yaml -> memory:` — chưa cấu
  hình nên log warn `Memory update skipped/failed` khi nó thử model mặc định; không ảnh
  hưởng chat turn nhưng là ứng viên cho config baseline tiếp theo.

## 6. CI trên `product/vnext`

Kiểm `.github/workflows/*.yml`:

- **`pull_request`**: tất cả workflow (backend-unit-tests, frontend-unit-tests,
  lint-check, e2e-tests, backend-blocking-io-tests, …) chỉ lọc `types`, **không lọc
  `branches`** → PR vào `product/vnext` chạy CI ngay, không cần sửa. ✅
- **`push`**: giới hạn `['main', '*-dev']` → push/merge lên `product/vnext` không có
  post-merge coverage.

Vì `product/vnext` là lane dài hạn mà mọi phase sau build lên, PR này thêm
`'product/vnext'` vào `push.branches` của 5 workflow green-gate (mỗi file 1 dòng):
`backend-unit-tests`, `frontend-unit-tests`, `lint-check`, `backend-blocking-io-tests`,
`e2e-tests`. Các workflow cron/label (`nightly`, `triage`, `label-sync`,
`verify-versions`, `replay-e2e`, `jev-plugin-package`) giữ nguyên — không liên quan
lane này. Đây là config CI tối thiểu, không đụng code.

## 7. Solver Lab — ghi chú scaffolding (KHÔNG build)

Mục tiêu phase sau: corpus ≥20 task để đo parity giữa vanilla lane và EWCP lane.
Một parity corpus cần tối thiểu:

1. **Danh mục task**: 20+ prompt bao phủ các nhóm hành vi — chat đơn, tool use
   (file/bash/web), artifact production, memory, subagent delegation, scheduled task —
   mỗi task kèm `expected outcome` kiểm chứng được (không phải "model trả lời hay").
2. **Định dạng task record**: id, prompt, assistant_id, model pin, timeout,
   acceptance check (regex/state assertion/file artifact tồn tại), tags nhóm.
3. **Harness chạy**: script gọi wire API (`POST /api/threads` + `runs/stream`)
   ở chế độ headless, lưu SSE transcript + trạng thái cuối; chạy được trên cả hai lane
   với cùng input — đây là điểm parity thực sự đo.
4. **Model pin**: kết quả chỉ so sánh được khi cùng model + cùng config; corpus phải
   ghi pin (ví dụ `gemini-3.5-flash-lite`) và chấp nhận re-baseline khi đổi model.
5. **Môi trường sạch**: mỗi task chạy trên thread mới; secret qua env, không hard-code;
   kết quả lưu vào thư mục gitignored, chỉ manifest (task id + locator) mới commit.

Việc chọn 20 task cụ thể và tiêu chí "parity pass" là quyết định của founder
(theo audit doc), nên phase này chỉ ghi nhận khung, không implement.

## 8. Tổng hợp deviation so với upstream vanilla

| # | Điểm chệch | Loại | Lý do |
|---|---|---|---|
| 1 | `npm install` trong `readabilipy/javascript/` của venv | env | thiếu jsdom → 22 test fail; upstream CI không cần vì kỳ vọng fallback |
| 2 | `google_api_key` thay `gemini_api_key` trong config.yaml | config | param đúng của langchain_google_genai; example doc dùng tên cũ |
| 3 | Thêm model `gemini-3.8-flash`/`gemini-3.5-flash-lite` thay `gemini-2.5-flash` | config | 2.5-flash deprecated trên key hiện tại (Google 404) |
| 4 | `DEER_FLOW_AUTH_DISABLED=1` + tạo admin qua `/setup` | env | baseline local không SSO; UI vẫn đòi admin một lần |
| 5 | Boot từng service, không qua nginx | env | máy không có nginx; frontend rewrite đã trỏ sẵn :8001 |
| 6 | Thêm `product/vnext` vào `push.branches` 5 workflow | CI config | post-merge coverage cho lane sản phẩm |

Không một dòng code/harness/extension nào bị sửa.
