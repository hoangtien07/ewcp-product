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
| 9 | `frontend/src/components/workspace/chats/chat-page.tsx` | `ThreadExecutionRunBadge` chips in the chat header linking to `/workspace/ewcp-runs/{id}` (from `GET /api/ewcp/runs?thread_id=`) | A6 #3 reverse cross-link: chat header is the only place a per-thread run badge can live; shipped in #40, logged late | 2026-10-09 |
| 10 | `frontend/src/components/workspace/messages/message-group.tsx` | `case "ewcp_invoke"` in `getToolCallKind` + one `kind === "ewcp_invoke"` branch + the `@/ewcp` import (~10 lines) | A6 #13 hybrid lane: `getToolCallKind` is the designed name-keyed dispatch for per-tool renderers; the card itself is boxed in `frontend/src/ewcp/components/ewcp-invoke-step.tsx` | 2026-10-09 |
| 11 | `backend/packages/harness/deerflow/agents/middlewares/tool_arg_alias_middleware.py` | NEW — `ToolArgAliasMiddleware` + `normalize_tool_call_args` exact-match alias map (`contents`/`text`/`body`/`data`→`content` on `write_file`; `file_path`/`filepath`/`file_name`/`filename`→`path` on `write_file`/`read_file`/`str_replace`), logs `arg_alias_applied` | C03 follow-up: `wrap_tool_call` is the only seam that sees args before pydantic validation; the middleware must live in the harness package — no boxed dir is importable from `deerflow` (verified: `ewcp_core` is not a harness dependency and not wired on this branch) | 2026-10-09 |
| 12 | `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py` | Import + `tail.append(ToolArgAliasMiddleware())` in `_build_runtime_middlewares` | The always-on tail list is the only registration point for core middleware; `extensions.middlewares` config is operator-level (gitignored) and can't ship the fix in code | 2026-10-09 |
| 13 | `backend/packages/harness/deerflow/extensions/ordering.py` | Import + 5 `OrderingConstraint`s: `ToolArgAliasMiddleware` outer to `ArtifactResolution`/`Guardrail`/`SandboxAudit`/`ReadBeforeWrite`/`ToolProgress` | Aliases must normalize before arg-inspecting policies run; the invariants table is the designed declaration point | 2026-10-09 |
| 14 | `backend/packages/harness/deerflow/agents/middlewares/AGENTS.md` | One numbered entry for `ToolArgAliasMiddleware`; list renumbered | Repo convention: middleware guide kept in sync with the chain | 2026-10-09 |
| 15 | `backend/tests/test_tool_arg_alias_middleware.py` | NEW — 24 tests: C03 payload replay, alias matrix, canonical-wins, extras preserved, log field, sync+async chain, registration order | TDD; test dir is upstream's `backend/tests/` | 2026-10-09 |
| 16 | `backend/tests/test_tool_error_handling_middleware.py` | Count pin 22→23, import, position assertion (`receipt < alias < error_handling`) | Existing pin asserts exact middleware count — the new always-on entry legitimately adds one | 2026-10-09 |

Current count: **16 / 8** — OVER BUDGET (was 10/8). Two rows (#9 chat-page.tsx;
the `src/app/workspace/ewcp-runs/` + `src/app/verify/` route files were
also added in upstream dirs without rows) were shipped by #40/#42
without ledger entries — recorded here retroactively. The `getToolCallKind`
dispatch is the intended extension seam for per-tool cards (thin, additive,
name-keyed), so #10 was judged a legitimate touch rather than core surgery;
flag for founder review — if the seam is contested, the alternative is an
upstream "tool-renderer registry" contribution so extensions can register
cards without patching message-group.

## Merge-sync policy

- Pin: upstream `main` @ `53df22bd` — a deliberate dev-line pin for the
  AgentRuns extension API (Q42); revert to tag-tracking once a release
  containing it ships (see `docs/vnext/MIGRATION_AUDIT.md` §7.1).
- `upstream` remote = `github.com/bytedance/deer-flow`.
- Any merge conflict outside this table means our boxing leaked.
