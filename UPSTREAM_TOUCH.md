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
| — | _(none yet)_ | — | — | — |

Current count: **0 / 8**.

## Merge-sync policy

- Pin: `v2.1.0` (`345f08be`). `upstream` remote = `github.com/bytedance/deer-flow`.
- Sync: `git fetch upstream --tags` → merge/`rebase` the next tag → this file
  drives conflict triage (any conflict outside this table = our boxing leaked).
- Contract tests from spike S (extension graft, `ask_clarification` surface,
  stream mode) must pass after each sync before landing.
