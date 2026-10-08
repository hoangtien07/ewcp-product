# UPSTREAM_TOUCH.md — files we patch inside upstream deer-flow

Re-baselined for `product/vnext` (upstream `main` pin — see
`docs/vnext/MIGRATION_AUDIT.md` Q42). Ledger discipline carried over from
`ewcp/main`: EWCP custom code lives in boxed dirs
(`backend/extensions/ewcp-core/`, `frontend/src/ewcp/`, `docs/vnext/`).
Upstream files may be touched only when unavoidable; every touch is
recorded below. Budget: **≤ 8 files**.

> The `plugins:` list lives in `config.yaml` — gitignored by design, so it
> is NOT an upstream touch (operator file, per upstream AGENTS.md).
> Runtime config likewise sits in `extensions_config.json` (gitignored).

| # | Upstream file | Change | Why | Date |
|---|---------------|--------|-----|------|
| 1 | `frontend/src/components/workspace/workspace-nav-chat-list.tsx` | One `SidebarMenuItem` + `Link` to `/workspace/ewcp-runs` (ShieldCheckIcon, label "EWCP") at the top of the workspace nav | ExecutionRun surface needs a workspace entry point; least invasive match to upstream nav-item style (re-homes the `ewcp/main` `/ewcp` nav hunk — MIGRATION_AUDIT §2b REPLACE verdict) | 2026-10-08 |
| 2 | `backend/app/gateway/auth_middleware.py` | `"/api/ewcp/verify"` added to `_PUBLIC_PATH_PREFIXES` | Seal permalink + verify-by-them are anonymous by kernel contract ("the hash is the capability"); the auth middleware is global ASGI, so a public route can only be exempted here | 2026-10-08 |
| 3 | `backend/app/gateway/csrf_middleware.py` | `"/api/ewcp/verify"` added to `_CSRF_EXEMPT_EXACT_PATHS` | The anonymous verify-by-them POST has no session to protect — the double-submit check would only 403 legitimate verifiers | 2026-10-08 |
| 4 | `backend/packages/harness/deerflow/extensions/gateway.py` | `"/api/ewcp/verify"` added to `_HOST_PUBLIC_PATH_PREFIXES` and `_HOST_CSRF_EXEMPT_EXACT_PATHS` | The host reserved-path sets must mirror the middleware sets (pin tests enforce `==`); the reservation also fences extension routers out of the public namespace | 2026-10-08 |
| 5 | `backend/app/gateway/app.py` | Import + `app.include_router(ewcp_verify.router)` | The public verify routes are HOST routes — extension route claims entering a host public prefix get the whole contributed router unmounted, so anonymous endpoints can only be mounted host-side | 2026-10-08 |
| 6 | `backend/app/gateway/routers/ewcp_verify.py` | NEW — thin router for `GET/POST /api/ewcp/verify{,/{hash}}` delegating to `ewcp_core.verify_surface` (404 when the extension is absent) | Host-mounted public surface; kernel logic stays boxed in the extension | 2026-10-08 |
| 7 | `backend/tests/test_extension_gateway_wiring.py` | `/api/ewcp/verify` (+ `/`, `//` variants) added to the CSRF-exemption parametrize | Pins the actual `should_check_csrf` behavior for the new exempt path, not just set equality | 2026-10-08 |
| 8 | `frontend/AGENTS.md` | Route inventory mentions `/workspace/ewcp-runs` + `/verify/[hash]` | Repo convention: keep the module guide in sync with the change set | 2026-10-08 |

Current count: **8 / 8** — budget exhausted; any further upstream need
requires restructuring (e.g. an upstream "extension-declared public
route" contract contribution) rather than another touch.

## Merge-sync policy

- Pin: upstream `main` @ `53df22bd` — a deliberate dev-line pin for the
  AgentRuns extension API (Q42); revert to tag-tracking once a release
  containing it ships (see `docs/vnext/MIGRATION_AUDIT.md` §7.1).
- `upstream` remote = `github.com/bytedance/deer-flow`.
- Any merge conflict outside this table means our boxing leaked.
