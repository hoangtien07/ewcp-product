# A3 — EWCP vNext surface map

Replaces the legacy `frontend/src/ewcp/README.md` (pane doc, dropped with
the pane). The A3 port (Option A+) keeps every EWCP surface boxed:
product code under `frontend/src/ewcp/` + `backend/extensions/ewcp-core/`;
the kernel (`hoangtien07/enterprise-work-control-plane`) is a separate
HTTP service reached M2M — never in-process.

## Routes

| Route | Access | Surface |
|---|---|---|
| `/workspace/ewcp-runs` | session | ExecutionRun workspace view: intent intake, capability gallery (`GET /api/ewcp/outcomes`), owner-scoped run list, SSE thread view via the run's `join_url`. Sidebar entry "EWCP". |
| `/verify/[hash]` | **public** | Seal permalink. Any link holder resolves the sealed manifest (PASS/FAIL/unprovable) and can run verify-by-them byte-integrity upload — no account required, the manifest hash is the capability. |

## Extension API (`/api/ewcp/*`)

Session-authenticated — the browser never sees the kernel key (M2M
`KernelClient` server-side):

- `GET /identity`, `GET/POST /runs`, `GET /runs/{id}`, `POST
  /runs/{id}/resume`, `GET /runs/{id}/{workrun,manifest,outcome,evidence}`,
  `GET /runs/{id}/deliverables/{deliverable_id}`,
  `POST /runs/{id}/decisions` (gated on `decision_user_binding`),
  `GET /outcomes`, `GET /_status`.

Public (anonymous — mirrors the kernel's public verify contract,
`kernel:app.py` "the hash is the capability"):

- `GET /api/ewcp/verify/{manifest_hash}` — permalink lookup.
- `POST /api/ewcp/verify` — verify-by-them: evidence.json + artifact
  bytes → PASS/FAIL per artifact + manifest recompute.

These two are **host-mounted** (`app/gateway/routers/ewcp_verify.py`),
not extension-contributed: the harness fences extension route claims out
of the host public namespace — a contributed claim under
`/api/ewcp/verify` would unmount the whole ewcp-core router. The host
handlers delegate to `ewcp_core.verify_surface`, which finds the running
`EwcpCoreService` on `app.state.extensions` and 404s when the extension
is absent. `/api/ewcp/verify` sits in the gateway auth public prefixes +
CSRF exact-exempt list, mirrored by the harness reserved sets (pin tests
enforce equality).

## Components (`frontend/src/ewcp/`)

`execution-run-list`/`execution-run-card`/`execution-run-thread` (list +
detail on the SSE join route), `capability-gallery`, `decision-card`
(identity-gated), `manifest-card`, `studio-card`, `verify-view`,
`share-verify-link`, `unverified-badge`; `api.ts` (session client +
`joinRunStream` SSE parser), `registry.ts` (spec fallbacks), `labels.ts`,
`exploratory.ts`.

## Dropped with the pane (`ewcp/main` era)

- `frontend/src/app/ewcp/{page,verify/page}.tsx` — workbench frame and
  the `?manifest=` query-param verify page → replaced by
  `/workspace/ewcp-runs` and `/verify/[hash]`.
- `frontend/src/ewcp/README.md` — pane doc → this file.
- `frontend/src/ewcp/components/{task-list,task-thread,pack-gallery,
  general-card}.tsx` — superseded by the `execution-run-*` set +
  `capability-gallery`.
- `docker/nginx/nginx{,.local}.conf` `location /api/ewcp` 660s hunks and
  `frontend/next.config.js` `proxyTimeout` — workaround for the old
  synchronous governed-intake POST (600s kernel turn cap). Intake is now
  AgentRuns + SSE observation, so the generic `/api/` proxy suffices;
  `/api/ewcp/*` still proxies to the gateway through the catch-all.
- `x-ewcp-api-key` in the browser — the kernel key is M2M-only now.

Guard: `frontend/tests/unit/ewcp/dropped-paths.test.ts` fails if any
dropped path, component import, browser kernel-key reference, or proxy
hunk comes back.
