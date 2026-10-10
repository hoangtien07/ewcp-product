# A7a — SSE / Reconnect / Browser Verification (measured)

Scope: `ewcp-product@product/vnext` post-merge-wave (incl. #46 session-actor mint,
#62 two-user harness). Follows `A7A_AUTH_ON_ISOLATION.md` (52-probe matrix, all PASS);
same boot recipe and helpers.

Method: real `AuthMiddleware` + JWT sessions (no `DEER_FLOW_AUTH_DISABLED`), kernel
HTTP service `:8080` keyed+sealed (`EWCP_REQUIRE_AUTH=1`, `EWCP_TENANT_KEYS`,
`EWCP_SEAL_KEY`), gateway `:8001` (`EWCP_KERNEL_URL=http://127.0.0.1:8080`),
frontend Next dev `:3000`, nginx `:2026` (`docker/nginx/nginx.conf` —
`/api/langgraph/*` rewrites to gateway `/api/*`). Ollama is the only in-lane provider:
`qwen2.5:0.5b` and `qwen2.5:1.5b` via `langchain_ollama:ChatOllama`
(`qwen25-local` / `qwen15-local` in `config.yaml`, gitignored). No Gemini.
Every row below is a measured request, stream capture, or browser screenshot; no row
is assumed.

Boot checks (rerun against this session's stack): kernel rejects unkeyed → `401`;
gateway unauthenticated surface (`/api/threads`, `/api/ewcp/runs`, join URL) → `401`;
`POST /api/ewcp/runs` without `X-CSRF-Token` → `403`. Admin user
`admin@sse-verify.dev` (`85abb360-da84-46cf-9f97-f5691733f34f`) provisioned through
`/api/v1/auth/initialize` + `/api/v1/auth/login/local`. All governed runs confirmed
kernel-side `candidate_complete` (`GET :8080/workruns`, `wr-ad411…`, `wr-a3c7f…`,
`wr-e2508…`, `wr-17e96…`, `wr-5b28e…`, `wr-120fa…` all `pending_decision=true`).

## SSE routes under test

- `POST /api/threads/{tid}/runs/stream` — create + stream (launch path used by the
  pane's "Chạy kiểm chứng" buttons goes through `POST /api/ewcp/runs` instead, which
  admits the run via the launcher and returns a `join_url`).
- `GET /api/threads/{tid}/runs/{rid}/join` — read-only join; reads `Last-Event-ID`.
- Memory StreamBridge, `queue_maxsize` (default 256; set to 4 for the eviction/gap
  probe). Retained replay is a bounded live window — the bridge `cleanup`s the stream
  after run end, so post-completion joins return `end` only.

## Results

| # | Scenario | Measured | Verdict |
| --- | --- | --- | --- |
| 1 | Launch governed run, consume SSE (gateway-direct :8001) | `POST /api/ewcp/runs` (multipart, invoices zip + books csv) → `er_bc866f03bc734206bacbe6b99653945b`, thread `f1cc19b3-…`, run `5f06915e-…`, status `running`. Join stream: `metadata -0`, `values -1..-8`, `end` — full sequence, contiguous ids `<ms>-<seq>` | PASS |
| 2 | Mid-run disconnect + `Last-Event-ID` resume (general run, qwen2.5:0.5b) | Run `a3bd5ebe-…` (12 data events): client A joined, aborted after `-2` mid-run (run still `running`); client B re-joined with `Last-Event-ID: 1791624931582-2` → received `-3`,`-4` (retained) then `-5..-11` **live** over 1.49s + `end`. Control C (no cursor): full `-0..-11` + `end`. `A ∪ B == C`, order preserved, 0 duplicates, all 12 events covered | PASS |
| 3 | Resume on governed run (kernel workrun deterministic) | Run `5f06915e-…`: A aborted after `-3` while run `running`; B joined with `Last-Event-ID: …-3` → `-4..-8` + `end`. Full no-cursor replay = identical union, no dup/loss | PASS |
| 4 | Stale cursor below retained watermark → gap | `queue_maxsize=4`, run `208cf9e2-…` (>4 events emitted): `Last-Event-ID: 1-0` → `event: gap`, `data: {"code":"stream_replay_gap","requested_event_id":"1-0","earliest_available_event_id":"1791625002413-4","latest_available_event_id":"1791625006422-7","recovery":"reload_durable_state"}`, then stream closes | PASS |
| 5 | Malformed / unknown cursor | `Last-Event-ID: bogus` → full retained replay from earliest (13 events + `end`); memory bridge falls back to replay-from-earliest (documented: malformed-cursor behavior is backend-specific) | PASS |
| 6 | Anonymous join | `GET …/join` no cookie → `401` | PASS |
| 7 | Frontend rewrite path via nginx `:2026` | `GET http://localhost:2026/api/langgraph/threads/{tid}/runs/{rid}/join` with session cookie → SSE events `-1..-11` + `end` (run `3587d9fe-…`); identical semantics to gateway-direct | PASS |
| 8 | Browser pane live state (nginx `:2026`, real login) | Login `admin@sse-verify.dev` at `localhost:2026/login` (login/local → 200). Open `/workspace/ewcp-runs/{er}` while run in-flight → sidebar chip `Đã tiếp nhận` + `Hoạt động agent (N sự kiện)` accumulating from the live join stream | PASS |
| 9 | Browser pane after run end / reload | `er_83ca5c2ad6a247f0bac32dde2a6208a6` + `er_050ded2dac7a40749019a167c689f174` (governed): pane shows `Agent hoàn tất` + `Chờ quyết định`, kernel `Chờ duyệt — Kết quả chờ duyệt`, approval gate (`Duyệt & niêm phong` / `Từ chối`), exceptions table (1/6 khớp, 5 lệch), deliverables (reconciliation.xlsx, exceptions_report.md, reconciliation.json, ingest_report.json) — matches `GET /api/ewcp/runs/{er}` + kernel `GET :8080/workruns` | PASS |

## Event-sequence evidence (Scenario 2 — canonical)

Run `a3bd5ebe-7731-401f-916a-3ff1627dfadd`, thread `5b0ab3f8-…`, `task_mode=general`,
model `qwen25-local` (0.5b). Probe: `sse_reconnect_test.py` — join → abort after 3
events → rejoin with last cursor → third no-cursor join as control.

```
Part A (elapsed 0.0s, aborted mid-run):
  -0 metadata   -1 values   -2 values          [Last-Event-ID kept: 1791624931582-2]
Part B (join + Last-Event-ID:-2, elapsed 1.49s):
  -3 values     -4 values                      (retained replay)
  -5..-9 values -10 -11 values  end            (live tail — model still generating)
Part C (join, no cursor — control):
  -0 metadata   -1..-11 values  end
Verdict: A+B ids == C ids exactly; no duplicates; every event delivered exactly once.
```

Governed variant (Scenario 3, run `5f06915e-…`): A `-0..-3` → abort; B `-4..-8` + `end`
via `Last-Event-ID:-3`; full replay `-0..-8` + `end` identical.

## Gap semantics (Scenario 4, measured with `queue_maxsize: 4`)

Cursor `1-0` predates the retained buffer (`earliest=-4`, `latest=-7`) → single
`event: gap` frame with `recovery: reload_durable_state`, then the stream closes.
This is the intended degrade: a client whose cursor fell below the watermark cannot
rely on replay and must re-read durable state (run record / thread state).
Malformed cursors (`bogus`) replay from earliest under the memory bridge —
backend-specific, as documented in `backend/AGENTS.md`.

## Limits found (measured, not bugs per se — recorded for the record)

- **Pane does not send `Last-Event-ID`** (`frontend/src/ewcp/api.ts::joinRunStream`
  parses `event:`/`data:` only) and has no `gap` handler. A pane whose SSE dies
  mid-run rejoins **without a cursor** on remount: it gets the retained buffer
  (works while nothing was evicted) or `gap` + close (then state comes from the
  durable record refresh, `getExecutionRun(refresh=true)`). For ≤256-event runs on
  the default memory bridge this is invisible to the user; for longer runs the gap
  path relies on the record refresh rather than cursor resume. **[measured from
  code + buffer-eviction probe]**
- **Retained replay is live-window only**: after run end the bridge cleans up; a
  post-completion join returns `end` alone. Correct for the pane (it renders state
  from the durable record), worth knowing for third-party consumers. [measured]
- Pane event feed caps the last 50 events in UI (`streamEvents` slice) — cosmetic.
- Browser-side, the window between `launching`/`running` and `candidate_complete`
  for governed runs on qwen2.5:0.5b is ~8–15 s on this box (kernel workrun is
  deterministic and fast); a hard network-kill mid-run inside the *same* pane
  instance was not exercised — the equivalent disconnect/resume was measured at
  protocol level (Scenarios 2–4) and the pane's rejoin path is the same
  `joinRunStream` code. **[partially covered — flagged]**

## Simulated seams

None. All probes ran against the real AuthMiddleware + JWT, real Ollama models,
real kernel HTTP service (keyed), real nginx rewrite. `queue_maxsize=4` was set in
`config.yaml` for the eviction probe only — a deployment config knob, not a code
change.

## Reproduce

```bash
# boot (auth-on): kernel keyed+sealed :8080, gateway :8001, frontend :3000, nginx :2026
# then, with a session cookie jar:
curl -N -b cookies.jar -H "Last-Event-ID: <ms>-<seq>" \
  http://127.0.0.1:8001/api/threads/<tid>/runs/<rid>/join
# or via the frontend rewrite path:
curl -N -b cookies.jar http://localhost:2026/api/langgraph/threads/<tid>/runs/<rid>/join
```

Raw stream captures + probe scripts: `~/a7a-sse/` on the session box
(`run4_head/tail/full.sse`, `gen3_reconnect.json`, `gap_probe.sse`,
`run4_nginx.sse`, `sse_probe.py`, `sse_reconnect_test.py`).
