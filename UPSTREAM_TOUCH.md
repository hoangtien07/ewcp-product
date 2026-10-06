# UPSTREAM_TOUCH.md — files we patch inside upstream deer-flow

Vendor-fork discipline (PA-A, `BASE_INTEGRATION_OPTIONS.md` §2): EWCP custom
code lives in boxed dirs (`extensions/ewcp-packs/`, `frontend/src/ewcp/`,
`docs/ewcp/`). Upstream files may be touched only when unavoidable and every
touch is recorded below. Budget: **≤ 8 files**.

> The `plugins:` list lives in `config.yaml` — gitignored by design, so it is
> NOT an upstream touch (operator file, per upstream AGENTS.md). Runtime config
> likewise sits in `extensions_config.json` (gitignored).

| # | Upstream file | Change | Why | Date |
|---|---------------|--------|-----|------|
| 1 | `frontend/src/components/workspace/workspace-nav-chat-list.tsx` | One `SidebarMenuItem` + `Link` to `/ewcp` (ShieldCheck icon, label "EWCP") at the top of the workspace nav | No surface in the product led to `/ewcp`; sidebar nav is the least invasive entry point matching upstream nav-item style | 2026-10-05 |
| 2 | `docker/nginx/nginx.conf` | Dedicated `location /api/ewcp` with 660s connect/send/read timeouts | Governed-intake POSTs block for the whole first attempt (600s kernel turn cap); the generic `/api/` catch-all's default 60s read timeout 504s mid-run — false error while the run completes | 2026-10-06 |
| 3 | `docker/nginx/nginx.local.conf` | Same `/api/ewcp` 660s block for the local-compose proxy | Same defect on the local docker path | 2026-10-06 |
| 4 | `frontend/next.config.js` | `experimental.proxyTimeout = 660_000` | Next dev proxy dropped the upstream socket (~60s) on a 73s governed intake → false "Internal Server Error"; raises the rewrite proxy ceiling to cap + buffer | 2026-10-06 |

Current count: **4 / 8**.

## Merge-sync policy

- Pin: `v2.1.0` (`345f08be`). `upstream` remote = `github.com/bytedance/deer-flow`.
- Sync: `git fetch upstream --tags` → merge/`rebase` the next tag → this file
  drives conflict triage (any conflict outside this table = our boxing leaked).
- Contract tests from spike S (extension graft, `ask_clarification` surface,
  stream mode) must pass after each sync before landing.
