# A7a — Auth-On Multi-User Isolation Matrix (measured)

Scope: `ewcp-product@product/vnext` post-merge-wave (#55, #59, #63, #61, #64, #46, #62).
Method: real `AuthMiddleware` + JWT sessions (no `DEER_FLOW_AUTH_DISABLED`), keyed kernel
(`EWCP_REQUIRE_AUTH=1`, `EWCP_TENANT_KEYS`, `EWCP_SEAL_KEY`). Every row below is a
measured HTTP request; no row is assumed. Probed surface: gateway `:8001` (nginx `:2026`
was not started — task permits either; frontend `:3000` verified separately).

## Boot evidence (Task 1)

| Check | Command/result | Verdict |
| --- | --- | --- |
| Kernel rejects unkeyed | `GET :8080/workruns` no key → `401 {"detail":"tenant key required"}` | PASS |
| Kernel accepts tenant key | same + `X-Ewcp-Api-Key` → `200` | PASS |
| Gateway auth on | `GET :8001/api/threads`, `/api/ewcp/_status`, `/api/ewcp/runs`, `/api/ewcp/identity` no cookie → all `401 {"detail":"Not authenticated"}` | PASS |
| CSRF on | `POST /api/ewcp/runs` session cookie, no `X-CSRF-Token` → `403 {"detail":"CSRF token invalid or missing"}` | PASS |
| Frontend lane | `GET :3000/login` → 200; `GET :3000/api/ewcp/_status` (Next rewrite → gateway) → 401 unauthenticated | PASS |

Users provisioned through the real auth routes (`POST /api/v1/auth/initialize`,
`/api/v1/auth/register`, `/api/v1/auth/login/local` → JWT `access_token` cookie +
`csrf_token` cookie): admin, alice (`f5b7065a-ec70-4cb1-b8ac-9561991a7cd3`), bob
(`7bff8af5-c738-48bb-be4a-397ad7ae1ff4`).

## Isolation matrix (Task 2)

Attacker = alice (cookie jar + CSRF), victim = bob unless noted. Bob assets: thread
`92dc9026-fab4-4997-ab84-6d8d6077c997` (artifact `mnt/user-data/uploads/bob_secret.txt`),
governed run `er_320b8776fb134ef08e3f2c81ffd57fe7` → `wr-679bcd01becf` (manifest sealed,
hash `55a0d8ca…f21a`), general run `er_9ad92db7f20f448ba5b62ed1968f9212`, threads
`683f433c-…`, `049839ef-…`, agent runs `e73b0f60-…`, `78ff4018-…`. Alice assets used as
positive controls: thread `d6db9c81-…`, governed run `er_b1a58f6b8d0649758f1c5dbf4fcb0f76`
→ `wr-e437a1611348`.

### Threads (surface `/api/threads/*`)

| Probe | Result | Verdict |
| --- | --- | --- |
| GET thread | `404 "Thread … not found"` | PASS |
| GET state / runs / messages / token-usage / history | `404 "Thread … not found"` each | PASS |
| POST state / runs / history / move / compact / goal / mcp-tasks | `404` each | PASS |
| POST branches `{message_id, message_ids}` (well-formed) | `404` | PASS |
| DELETE thread | `404` | PASS |
| GET uploads list / artifact bytes / download | `404` each | PASS |
| PUT artifact (well-formed body: `content` + `expected_sha256`) | `404` | PASS |
| POST / DELETE `/uploads/{filename}` | `404` | PASS |
| GET `/api/threads` list | contains only alice's threads; grep for all bob ids → 0 hits | PASS |

404 (not 403) normalizes existence — cross-user probing discloses neither existence nor
ownership. Uniform 422 on malformed bodies precedes the owner check for both roles, so it
is not an ownership oracle.

### Runs (surface `/api/threads/{t}/runs*`)

| Probe | Result | Verdict |
| --- | --- | --- |
| GET run / join / messages / events / stream | `404` each | PASS |
| POST cancel | `404` | PASS |
| GET `/api/threads/{bob-t}/runs` (list) | `404` | PASS |

### /ewcp pane surfaces (extension `ewcp-core`)

| Probe | Result | Verdict |
| --- | --- | --- |
| GET `/api/ewcp/runs` (list) | only alice's own run rows; grep bob's run ids → 0 hits | PASS |
| GET `/api/ewcp/runs?thread_id=<bob's>` | `{"runs":[]}` | PASS |
| GET `/api/ewcp/runs/{er}` / `/workrun` / `/manifest` / `/outcome` / `/evidence` | `404 "unknown execution run"` each | PASS |
| GET `/api/ewcp/runs/{er}/deliverables/{did}` | `404` | PASS |
| POST `/api/ewcp/runs/{er}/decisions` | `404` | PASS |
| POST `/api/ewcp/runs/{er}/resume` | `404` | PASS |
| GET on bob's general (non-governed) run id | `404` | PASS |
| GET `/api/ewcp/identity` | returns caller's own identity | PASS |

Owner filter is `record.created_by != user_id → 404` in `api_routes.py`/`plugin.py`
(extension layer), measured for both governed and general run kinds.

### Console / misc authenticated surfaces

| Probe | Result | Verdict |
| --- | --- | --- |
| GET `/api/console/runs` | only alice's run rows; bob's agent-run ids absent | PASS |
| GET `/api/console/threads/{bob-t}/…` | `404` | PASS |
| GET `/api/memory` | alice's own memory rows only | PASS |
| GET `/api/v1/auth/users` (user enumeration) | `404` (no such route) | PASS |

### Symmetry (bob → alice)

| Probe | Result | Verdict |
| --- | --- | --- |
| GET `/api/threads/{alice-t}` | `404` | PASS |
| GET `/api/ewcp/runs/{alice-er}` | `404` | PASS |
| GET `/api/ewcp/runs` | only bob's rows; alice's run id absent | PASS |

### Anonymous floor

`GET` on every probed path (threads, runs, ewcp, console, memory) with no cookie → `401`.
Positive controls: alice on all own surfaces → `200`.

## Kernel-bound identity (C10 actor mint)

| Probe | Result | Verdict |
| --- | --- | --- |
| Bob launches governed intake with forged inbound header `X-Ewcp-Actor: user:<alice>` | all 3 kernel decisions (`option_choice`, `anomaly_confirm`, `approval`) recorded `decided_by = user:7bff8af5-…@tenant:demo` | PASS |
| Alice launches with forged form fields `actor=user:admin`, `user_id=admin`, `x_ewcp_actor=user:admin` | run `wr-c2a0490a3381` kernel `task_json.context` contains only declared keys (`ky`, `mst_doanh_nghiep`) — forged fields absent from kernel state | PASS |
| Alice's decisions on that run | `decided_by = user:f5b7065a-…@tenant:demo` on both `human_decision` rows | PASS |
| Tenant forge at launch (`tenant_id=other-tenant` vs bound tenant `demo`) | gateway → `403 "kernel rejected the request"` (kernel tenant_mismatch) | PASS |

Mechanism measured: `api_routes.py` mints `X-Ewcp-Actor: user:<uid>` server-side from the
session at the launch boundary (inbound header stripped); kernel honors `x-ewcp-actor`
only alongside tenant-key auth and persists `<actor>@tenant:<tenant>`.

## Public-by-design surfaces

| Probe | Result | Note |
| --- | --- | --- |
| GET `/api/ewcp/verify/{hash}` (no cookie) | `200` + manifest + seal verdict | Intentional: hash is the capability (kernel contract; CSRF-exempt path). Not an isolation gap. |

## Known limits of this measurement

- Agent runs carried status `error`/`candidate_complete` (no LLM provider in lane —
  Ollama not running; governed lanes are deterministic). All read/mutate probes were
  still exercised against the real rows.
- `uploads` probes used `POST/DELETE` on bob's upload filename; both `404`.
- PAT path not exercised (sessions only per task); `is_pat_allowed_route` is
  default-deny in `auth_middleware.py` [measured only via code read, not wire].

## Verdict

**PASS — no measured cross-user isolation gaps.** All mutations and reads on foreign
threads, runs, uploads, artifacts, console, and ewcp surfaces deny with 404; lists carry
zero foreign ids; kernel-side identity binds to the session principal on every decision;
tenant forge is rejected at the kernel. No fix PRs required; docs-only evidence PR.

Raw probe bodies: `/home/ubuntu/a7a/out/*.body`, manifest `matrix.tsv` (52 probes);
kernel state at `/home/ubuntu/a7a/kernel/store/runs.db`.
