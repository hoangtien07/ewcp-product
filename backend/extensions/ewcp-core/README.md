# ewcp-core — EWCP kernel HTTP client extension

A3 Task 1 of `docs/plans/2026-10-08-a3-product-core-port.md`: extension
skeleton + `kernel_client.py` for the EWCP kernel reached **over HTTP**
(Option A+: kernel = governed capability & verification server in repo
`hoangtien07/enterprise-work-control-plane`; never mounted in-process).

## Contents

- `ewcp_core/kernel_client.py` — `KernelClient` (`httpx.AsyncClient`)
  covering the kernel's real wire surface:
  `POST /tasks`, `POST /workruns/{id}/decisions`, `GET /workruns/{id}`,
  `GET /verify/{manifest_hash}`, `GET /outcomes`.
- `ewcp_core/plugin.py` — `EwcpCoreService` (owns the shared client +
  ExecutionRunMap launcher for the Gateway lifetime) +
  `GET /api/ewcp/_status`.
- `ewcp_core/run_launcher.py` — `RunLauncher.launch(intent, mode)`:
  `agent_runs.for_plugin("ewcp.core")` → `create_thread` → uploads
  (BEFORE `start`, through `POST /api/threads/{id}/uploads` via an
  injected `ThreadUploads`) → `start(input, idempotency_key)` →
  ExecutionRunMap row. Includes the Gateway SSE observation-route
  constants, the `FileInMessage` input contract (`additional_kwargs.
  files`), `pending_interrupt != completed` projection, and
  crash-resume replay (same thread + same key converges through the
  host's `extension:{ns}:{key}` dedupe).
- `ewcp_core/execution_run_store.py` — ExecutionRunMap on the SHARED
  product DB: extension-owned table `ewcp_execution_runs` (private
  `MetaData`, `table_prefix: ewcp_`), created inside
  `ExtensionService.start()` via `deps.session_factory`.
- `ewcp_core/recovery.py` — FOREGROUND-only recovery (A3 Task 3):
  `RunRecovery.recover()` reconciles the map against live thread truth
  via the request-fresh bound handle (run-id `get` + ended-run
  `get_state` for pending interrupts — never a single-snapshot truth);
  `resume()` is gated on real pending state with a revoke check before
  the mutating call; `assert_can_start()` is the dup-start guard. No
  background/service recovery exists — `bind()` is SESSION/
  AUTH_DISABLED only; see `docs/vnext/A3_DURABILITY.md`.
- Routes: `GET /api/ewcp/_status`, `GET /api/ewcp/runs` (foreground
  reconcile + list for the caller), `POST /api/ewcp/runs/{id}/resume`
  (interrupt response, owner-scoped).

## Wiring (`config.yaml`)

Third-party extensions load from the operator-controlled `plugins:`
block of root `config.yaml` (never `extensions_config.json`):

```yaml
plugins:
  - name: ewcp_core
    package: ewcp-core
    use: ewcp_core:install
    enabled: true
    required: false
    table_prefix: ewcp_           # REQUIRED: declares ownership of ewcp_*
                                 # tables so host autogenerate never
                                 # proposes dropping them
    config:
      kernel_url: http://127.0.0.1:8080   # kernel service base URL
      kernel_api_key: ...                 # M2M key — prefer env instead
      timeout_seconds: 30                 # optional
      read_max_attempts: 3                # optional, GET retry bound
```

**Env-first config:** `EWCP_KERNEL_URL` / `EWCP_KERNEL_API_KEY`
override the `config:` keys. Prefer env for the API key so the secret
never sits in a config file. The key is sent only as the
`X-Ewcp-Api-Key` request header — never in URLs, logs, or the status
payload.

Install: `make extension-install SOURCE=backend/extensions/ewcp-core`
(from repo root) or `cd backend && uv run deerflow extensions install
extensions/ewcp-core`. Restart Gateway after any mutation.

## Retry policy

- **Safe reads** (`GET /outcomes`, `/workruns/{id}`, `/verify/{hash}`):
  bounded retry on transient faults — statuses {408, 429, 500, 502, 503,
  504} and transport errors, `read_max_attempts` (default 3) with
  `min(2**attempt, 8)`s backoff (kernel `GeminiClient` pattern).
- **`POST /tasks`**: retried **only** when the caller passes
  `idempotency_key` — the kernel dedupes `(tenant, key)` in its
  `idempotency_keys` table and replays the original run
  (`Idempotent-Replay: true`). Without a key a retry could spawn a
  duplicate WorkRun.
- **`POST /workruns/{id}/decisions`**: never retried — the kernel
  exposes no idempotency contract on this endpoint.

## Status

`GET /api/ewcp/_status` →
`{extension, kernel_url, kernel_configured, api_key_configured,
client_started, store_started}`. Reports `kernel_configured: false`
instead of failing Gateway startup when no kernel URL is set.

## ExecutionRunMap persistence (Ruling)

The plan pointed at `backend/app/gateway/persistence` — that path does
not exist on `product/vnext`. The documented convention for
extension-owned tables (persistence migrations guide) applies instead:
private `MetaData` + `table_prefix: ewcp_` declared on the `plugins:`
record + schema bootstrap inside `ExtensionService.start()` against
`deps.session_factory`. The table lives in the SAME database as the
host (this is the "shared product persistence" the plan requires —
NOT a private SQLite file like `examples/deerflow-extension-agent-
teams`' store). `table_prefix` is operator config (gitignored
`config.yaml`), so it is documented here, not committed.

Schema versioning: v0.1 uses idempotent `create_all` (Postgres
advisory-locked). When columns first need to *change*, an
extension-owned alembic chain (`version_table="ewcp_alembic_version"`)
per the migrations convention replaces it.

## ExecutionRun launch semantics

- `launch(intent, mode)` is admission-side: it creates the product
  thread + run via the bound `AgentRuns` handle and projects one
  `ewcp_execution_runs` row (`thread_id` / `run_id` / `workrun_id`
  nullable / `task_mode` / `status` / `idempotency_key` /
  `created_by`).
- Durable dedupe is owner-scoped `(created_by, idempotency_key)`: a
  replayed launch returns the stored row — no new thread, no new run.
  Same key + different intent/mode → `LaunchConflict`.
- The row is inserted BEFORE `start()` so a crash mid-launch retains
  ownership: a retry with `run_id IS NULL` re-issues `start` on the
  SAME thread with the SAME key, where the host's global
  `extension:{ns}:{key}` dedupe converges to the same run instead of
  erroring on a thread mismatch.
- `pending_interrupt != completed`: `refresh()` consults
  `get_state()` for ended runs (`success`/`interrupted`) — pending
  interrupts/`next` project to `pending_interrupt`, never
  `completed`.
- Observation is Gateway SSE only — `RUN_STREAM_PATH`,
  `RUN_JOIN_PATH`, `RUN_STREAM_EXISTING_PATH` constants; AgentRuns is
  NOT used for streaming.
- `on_disconnect="continue"` is applied by the host bound impl
  (`extension_agent_runs.py:142`) on every admission path; there is no
  interface parameter to pass.

## Tests

```bash
cd backend && .venv/bin/python -m pytest extensions/ewcp-core/tests -q
```

(also collected by `pytest -k ewcp_core` from `backend/`)
