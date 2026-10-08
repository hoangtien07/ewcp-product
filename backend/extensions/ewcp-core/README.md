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
- `ewcp_core/plugin.py` — `EwcpCoreService` (owns the shared client for
  the Gateway lifetime) + `GET /api/ewcp/_status`.

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
`{extension, kernel_url, kernel_configured, api_key_configured, client_started}`.
Reports `kernel_configured: false` instead of failing Gateway startup
when no kernel URL is set.

## Tests

```bash
cd backend && .venv/bin/python -m pytest extensions/ewcp-core/tests -q
```

(also collected by `pytest -k ewcp_core` from `backend/`)
