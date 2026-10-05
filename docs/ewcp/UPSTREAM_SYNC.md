# UPSTREAM_SYNC.md — adopting new deer-flow releases

Vendor-fork model (PA-A): `ewcp/main` is our integration branch tracking
`bytedance/deer-flow` at a pinned tag. This doc is the procedure for moving
the pin forward. `UPSTREAM_TOUCH.md` records what we patched upstream;
this doc records *how* a new tag gets adopted.

## Pin policy

- **Track tags, not `main`.** Upstream releases are tagged (`v2.0.0`,
  `v2.1.0`, …). We adopt tags only — never a moving branch.
- **Cadence: lag, don't chase.** Re-evaluate the pin on each new upstream
  minor/major tag. Skip patch tags unless they fix something that affects
  us (check the changelog for the surfaces we use — see checklist below).
- Pin today: `v2.1.0` (`345f08be`).

## Sync procedure (one PR per tag adoption)

1. `git remote add upstream https://github.com/bytedance/deer-flow` (once)
   → `git fetch upstream --tags`.
2. **Read the upstream changelog** for the delta `v<old>..v<new>` —
   releases page + `CHANGELOG.md`. Flag anything touching our surfaces:
   - `packages/extension-api` (the `@extension(api=...)` contract and
     `ExtensionRegistry` — our graft is versioned against `api="0.2.0"`)
   - `ask_clarification` / clarification structured-form rendering
     (maps onto our decision kinds)
   - Gateway router mounting / lifespan ordering (our kernel lifespan
     is driven manually in `EwcpKernelService.start/stop`)
   - CSRF / double-submit rules on POSTs (pane sends `x-csrf-token`)
   - Anything under `frontend/src/` we do NOT use stays free to change.
3. Merge the tag into a branch off `origin/ewcp/main`:
   `git checkout -b devin/<ts>-sync-v2.x.y origin/ewcp/main`
   `git merge v2.x.y` (merge, not rebase — keeps upstream history legible
   for the next sync).
4. **Conflict triage against `UPSTREAM_TOUCH.md`:**
   - Conflict inside a file listed in the table → resolve by hand; update
     the row's description if the patch's context shifted.
   - Conflict **outside** the table → stop: our boxing leaked. Either the
     file belongs in the table (add it, justify) or our edit must be
     re-homed into the boxed dirs. Do not silently resolve.
5. **Contract gate before merge** (all must pass):
   - `cd backend && uv sync && make test` — extension loads,
     `kernel_loaded:true` on `/health`-equivalent probe.
   - `cd frontend && pnpm check && pnpm test`.
   - Boot the full stack (`make dev` or gateway+frontend) and drive one
     governed golden path: intent → ask-back → approve → seal →
     `/ewcp/verify` PASS. The kernel↔pane contract tests (kernel repo
     `tests/test_api_contract.py`) must still pass against the mounted
     kernel — a wire change there is a pane-breaking change, not a sync
     detail.
6. Update the pin in this doc + `UPSTREAM_TOUCH.md` + `PA_A_LAYOUT.md`,
   then PR into `ewcp/main`.

## Version-lockstep rule (upstream)

Upstream enforces `backend/pyproject.toml` == `frontend/package.json`
== `deploy/helm/deer-flow/Chart.yaml` (`scripts/verify_versions.sh`,
`v*` tag CI). We do not cut `v*` tags ourselves, so this CI job is
inert for us — do not bump those version fields manually; a sync merge
carries upstream's bump.

## Rollback

The pin lives only in git. Reverting a sync = revert the merge commit on
`ewcp/main` — our boxed code is untouched by upstream merges unless a
conflict was resolved wrong, which is why step 4's outside-the-table rule
exists.

## What we deliberately do NOT track

- Upstream `main` / unreleased work — instability without a release
  contract.
- Their `config.yaml`/`extensions_config.json` examples — operator files,
  gitignored, ours already exist.
