# A6 — Unified UX map (fixture phase)

WP-A6 deliverable per `docs/program/MASTER_EXECUTION_PLAN.md` §3 (kernel repo):
one workspace, not two products. This file inventories what `product/vnext`
actually ships today, maps the three user journeys, and ranks minimal
unification changes.

**Verified against:** `product/vnext` @ `e8f12bab` (2026-10-09). Every
component/route claim below names the file it was read from. Anything not
verifiable is labeled [giả định]. Related docs:
[`A3_SURFACE.md`](./A3_SURFACE.md) (routes + components),
[`A3_SMOKE.md`](./A3_SMOKE.md) (measured E2E),
[`A3_DURABILITY.md`](./A3_DURABILITY.md), [`A3_BUDGET_ADMISSION.md`](./A3_BUDGET_ADMISSION.md).

## 1. Surface inventory (verified)

### 1.1 EWCP-owned surfaces (`frontend/src/ewcp/` + two app routes)

| #   | Surface                                                                                                                                                                                   | File(s)                                                                         | Data consumed                                                                                                                            | Reachable from main chat flow?                                                                                                                                                                                                                                                                                             |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **EWCP workspace page** `/workspace/ewcp-runs` — intent textarea + "Chạy" button, gallery, list, detail                                                                                   | `app/workspace/ewcp-runs/page.tsx`                                              | `GET /api/ewcp/outcomes` (specs, falls back to `FALLBACK_SPECS`); `POST /api/ewcp/runs` (intent only — see §3 lane B)                    | Sidebar link only. No deep link: `activeId` is local state, so a specific run has no URL.                                                                                                                                                                                                                                  |
| 2   | **Sidebar entry "EWCP"**                                                                                                                                                                  | `components/workspace/workspace-nav-chat-list.tsx:36-44`                        | —                                                                                                                                        | It IS the only bridge into the EWCP surface. Bare label "EWCP" (English acronym), no badge/count for pending decisions.                                                                                                                                                                                                    |
| 3   | **CapabilityGallery** — governed-pack catalog cards + starter-intent chips + a "Sắp có" `bank_recon` placeholder                                                                          | `components/capability-gallery.tsx`                                             | `OutcomeSpecView[]` from `/outcomes`; `STARTER_INTENTS` in `registry.ts`                                                                 | Only on the EWCP page. Cards fill the intent box; they cannot launch anything themselves.                                                                                                                                                                                                                                  |
| 4   | **ExecutionRunList + ExecutionRunCard** — owner-scoped map rows: status pill, `general`/`governed` label, truncated ids                                                                   | `components/execution-run-list.tsx`, `execution-run-card.tsx`                   | `GET /api/ewcp/runs` (plugin.py foreground-recover + list)                                                                               | Only on the EWCP page. Status pill is the launcher projection (`launching/running/pending_interrupt/completed/failed/timeout/interrupted`), not the kernel truth.                                                                                                                                                          |
| 5   | **ExecutionRunThread** — run detail: header (status, task_mode, truncated ids), "live" SSE dot, then the governed section                                                                 | `components/execution-run-thread.tsx`                                           | `GET /api/ewcp/runs/{id}` (+`?refresh`), `GET …/workrun`, `GET /identity`, `joinRunStream(run.join_url)`                                 | Only on the EWCP page. SSE frames are parsed then **discarded** — the UI shows a "live" dot, never the agent's messages/tool calls. `thread_id` renders as truncated text, **not a link**.                                                                                                                                 |
| 6   | **DecisionCard** — pending `HumanDecision` buttons (missing_input / option_choice / confirm_value / approval / contract_approval)                                                         | `components/decision-card.tsx`                                                  | `POST /api/ewcp/runs/{id}/decisions`; gated on `identity.user_actor_binding`                                                             | Only inside ExecutionRunThread. Read-only + amber hint when binding is off.                                                                                                                                                                                                                                                |
| 7   | **Deliverables download list**                                                                                                                                                            | inside `execution-run-thread.tsx` (inline `<ul>`)                               | `RunView.deliverables` → `GET …/deliverables/{id}` blob download                                                                         | Only inside ExecutionRunThread, only when `workrun_id` bound.                                                                                                                                                                                                                                                              |
| 8   | **StudioCard** — per-outcome rich view (invoice_recon exception grid, dossier_check checklist, three_way_match PO rows)                                                                   | `components/studio-card.tsx`                                                    | `GET …/outcome`                                                                                                                          | Only inside ExecutionRunThread, only with deliverables.                                                                                                                                                                                                                                                                    |
| 9   | **ManifestCard + VerifiedSealBadge + ShareVerifyLink** — seal state, validator checks, decider, `/verify/{hash}` link + copy                                                              | `components/manifest-card.tsx`, `unverified-badge.tsx`, `share-verify-link.tsx` | `GET …/manifest` (fetched only when `run.status === "verified"`)                                                                         | Only inside ExecutionRunThread. Link leaves the app shell to the public page.                                                                                                                                                                                                                                              |
| 10  | **Public verify permalink** `/verify/[hash]` — seal lookup card (PASS/FAIL/unprovable) + verify-by-them upload (evidence.json + artifact bytes → per-artifact PASS/FAIL + hash recompute) | `app/verify/[hash]/page.tsx`, `components/verify-view.tsx`                      | `GET /api/ewcp/verify/{hash}` (public); `POST /api/ewcp/verify` (public); `GET …/evidence` (owner-scoped, for the export download field) | **Public — no session needed**, which is correct per kernel contract. But there is no `/verify` root route: the verify-by-them upload lane is only reachable after landing on a permalink. The `execution_run_id` evidence field only works for a logged-in owner — an external verifier must be handed the evidence file. |
| 11  | **EWCP-launched threads inside `/workspace/chats`**                                                                                                                                       | `run_launcher.py` (`create_thread` → `metadata.title = intent[:60]`)            | normal thread list                                                                                                                       | Reachable — but **indistinguishable**: nothing marks a thread as an ExecutionRun, and the chat page renders no governed card. The user sees a bare lead_agent conversation.                                                                                                                                                |

### 1.2 Extension/host API surface (`/api/ewcp/*`)

Session-scoped (browser never sees the kernel key; M2M `KernelClient` server-side):

| Route                                                                | File                                                                 | Status in UI                                    |
| -------------------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------- |
| `GET /identity`                                                      | `api_routes.py:146`                                                  | used (decision gating)                          |
| `GET/POST /runs`, `GET /runs/{id}`                                   | `api_routes.py:160,227`; `plugin.py:184`                             | used                                            |
| `GET /runs/{id}/{workrun,manifest,outcome,evidence}`                 | `api_routes.py:242-272`                                              | used                                            |
| `GET /runs/{id}/deliverables/{did}`                                  | `api_routes.py:274`                                                  | used                                            |
| `POST /runs/{id}/decisions`                                          | `api_routes.py:291`                                                  | used                                            |
| `GET /outcomes`, `GET /_status`                                      | `api_routes.py:313`; `plugin.py:180`                                 | used / ops-only                                 |
| `POST /runs/{id}/resume`                                             | `plugin.py:212`                                                      | **no frontend caller — dead route from the UI** |
| `GET /verify/{manifest_hash}`, `POST /verify` (public, host-mounted) | `app/gateway/routers/ewcp_verify.py` + `ewcp_core/verify_surface.py` | used by `/verify/[hash]`                        |

### 1.3 Verified dead/unwired code on this HEAD

These exist and are tested, but nothing in the UI or backend calls them —
the "contract without a consumer" class of gap:

- `frontend/src/ewcp/exploratory.ts` `handoffToGeneralLane` — referenced
  only by `tests/unit/ewcp/exploratory.test.ts`.
- `registry.ts` helpers `unionInputs`, `inputsFor`, `isZipInput` — the
  file-slot machinery for governed intake; `page.tsx` never imports them,
  so **the pane offers no file upload even though `launchExecutionRun`
  accepts `files` and every governed pack requires input files**
  (`invoices_zip`, `books`, `dossier_zip` in `FALLBACK_SPECS`).
- `UnverifiedBadge` (`unverified-badge.tsx`) — the "exploratory artifact"
  badge; only `VerifiedSealBadge` is imported (by `manifest-card.tsx`).
- `KernelClient.create_task` (`kernel_client.py:352`) — the kernel's
  governed intake (`POST /tasks`). **No caller in ewcp-core**: nothing on
  the product creates a kernel WorkRun.
- `RunLauncher.bind_workrun` — defined, uncalled; `workrun_id` reaches the
  map row only via the `workrun_id` multipart field at `POST /runs`.
- `POST /runs/{id}/resume` — as above.
- `runtime.context["kernel"]["workrun_id"]` — the governed budget-identity
  contract (`model_policy.py:168-171`, doc `A3_BUDGET_ADMISSION.md` §Identity).
  `RunLauncher.launch(context=)` accepts a context dict, but the only caller
  (`api_routes.py` `POST /runs`) never builds one, so the stamping the doc
  describes does not happen on this path. [low blast radius: budget
  admission still works; it attributes as `context["run_id"]` instead of
  the bound workrun]

### 1.4 Adjacent upstream surface (name collision, not EWCP)

`/workspace/capabilities` (`app/workspace/capabilities/page.tsx` →
`components/workspace/capabilities/capability-center.tsx`) is the upstream
tools/skills/extensions center — a different concept sharing the word
"capability" with EWCP's `CapabilityGallery`. Users see two unrelated
"capability" surfaces in the same sidebar.

## 2. Journey map — three lanes

| Stage                      | (a) General chat task                                                                                                                 | (b) Governed pack run                                                                                                                                                                                                                                                            | (c) Hybrid (chat invokes governed capability — A5a)                                                                                                     |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Entry**                  | `/workspace` → `/workspace/chats/new` composer (`chat-page.tsx`): full-featured — attachments, model picker, skills, projects, agents | **No product-UI path end-to-end.** Kernel intake is `POST /tasks` on the kernel itself (curl/kernel demo — `A3_SMOKE.md` §B), then `POST /api/ewcp/runs -F task_mode=governed -F workrun_id=wr-…` binds a map row. The pane's intent box only ever launches `task_mode=general`. | Does not exist. No tool on `lead_agent` reaches the kernel; `KernelClient.create_task` is uncalled; nothing stamps `runtime.context.kernel.workrun_id`. |
| **Status visibility**      | Streaming message list, TodoList, ThreadSubagentBatches, GoalStatus, TokenUsageIndicator — rich, in-flow                              | ExecutionRunCard pill (launcher projection) + ExecutionRunThread "live" dot + kernel `statusLabel` line. No pending-decision badge anywhere — `awaiting_input`/`candidate_complete` is invisible until the user opens the run.                                                   | —                                                                                                                                                       |
| **Deliverable surface**    | Artifacts panel (`ArtifactTrigger`), files in thread workspace, export/share                                                          | `RunView.deliverables` download links + `StudioCard` outcome grid inside the run detail — kernel storage, not thread workspace, so they never appear in artifacts                                                                                                                | —                                                                                                                                                       |
| **Evidence / verify path** | None (general lane produces no seal)                                                                                                  | ManifestCard → `/verify/{hash}` public permalink + verify-by-them upload on that page                                                                                                                                                                                            | —                                                                                                                                                       |

### Where the lanes diverge today

1. **Two intakes, two mental models.** The chat composer is the product's
   primary input (files, models, agents); the EWCP intake is a bare
   textarea with no file picker — yet governed packs cannot run without
   files. The user must know that "governed" work means leaving the app
   and driving the kernel directly. This is the single largest
   unification gap.

2. **One run, two ids, no bridge.** An ExecutionRun _is_ a product thread
   (`run_launcher.launch` → `create_thread` → `runs.start`), but the pane
   shows `er_…`/`wr-…`/`t-…` as truncated text and the chat page knows
   nothing about the map row. You cannot click from run → conversation or
   conversation → governed state.

3. **Three status vocabularies.** `ExecutionRunStatus` (launcher
   projection, 7 values) vs kernel `TaskStatus` on `RunView` (9 values)
   vs the chat's own run states. WP-A6's required vocabulary —
   `agent_finished`, `verified`, `accepted`, `approved_to_act`,
   `external_executed`, `UNKNOWN` — exists nowhere; today only
   `candidate_complete` and `verified` are reachable labels.

4. **Observation inconsistency.** The chat lane renders the agent's full
   streamed work; the EWCP lane parses the same-class SSE stream and
   shows a "live" dot. A governed user cannot see _what the agent is
   doing_ — only that it is alive.

5. **"Work waiting on me" has no surface.** DecisionCards only exist
   inside an open run. No inbox, no sidebar count, no list filter. The
   pilot demo's most important moment (human approves a governed outcome)
   requires the user to already know which run to open.

6. **Duplicated UI vocabulary.** `src/ewcp/` components hand-roll
   zinc-palette buttons/inputs/cards; the app uses `components/ui`
   primitives (Button, Input, Tabs) + design tokens (`text-muted-foreground`).
   Same visual language re-implemented, subtly different look.

7. **Language divergence.** Every `src/ewcp` string is hardcoded
   Vietnamese; the rest of the app runs `useI18n` (en-US/zh-CN). For a
   Claude-like general-purpose workspace this is a structural fork, not
   just missing translations.

8. **Verify is an island.** `/verify/[hash]` is reachable only via the
   manifest card's link (or a shared URL). There is no `/verify` root, so
   the verify-by-them lane — the third-party trust story — is buried one
   level deep.

## 3. Gap list + prioritized proposals

Ranked by (pilot-demo impact, effort). Every proposal cites an existing
surface — none invents features.

| #   | Proposal                                                                                                                                                                                                                                                                                                                                                                                                                   | Class                                                                 | Impact                         | Effort | Anchors                                                                          |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- | ------------------------------ | ------ | -------------------------------------------------------------------------------- |
| 1   | **Governed intake through the pane**: extend `POST /api/ewcp/runs` so `task_mode=governed` calls `kernel_client.create_task` (already written, uncalled) and binds the returned `workrun_id`; wire the existing `inputsFor`/`isZipInput` file slots into `page.tsx` + `launchExecutionRun({files, taskMode, workrunId})` (all already in `api.ts`). Without this the demo's central flow cannot happen inside the product. | [implementation] — but see #2's decision on _when_ governed is chosen | critical                       | M      | `kernel_client.py:352`, `registry.ts:12-47`, `api.ts:212-226`, `page.tsx:34-49`  |
| 2   | **Who picks `task_mode`?** Options: (a) explicit per-card "Chạy governed" button on CapabilityGallery cards (cards already exist, currently only prefill text); (b) kernel-driven after intent submit. For the pilot, (a) is the smallest truthful step — the pack card _is_ the governed entry point.                                                                                                                     | [decision-needed]                                                     | critical                       | S      | `capability-gallery.tsx:69-84`, `page.tsx`                                       |
| 3   | **Bidirectional run↔thread bridge**: `ExecutionRunThread` header renders `thread_id` as a `Link` to `/workspace/chats/{id}`; `chat-page` looks up `GET /api/ewcp/runs?thread_id=` (needs a small store filter — `execution_run_store.py` already indexes `thread_id`) and shows a compact governed badge linking back. Makes lanes read as one object.                                                                     | [implementation]                                                      | high                           | S-M    | `execution-run-thread.tsx:136`, `chat-page.tsx`, `plugin.py:184`                 |
| 4   | **Deep-linkable runs**: `/workspace/ewcp-runs/[execution_run_id]` (or `?run=`): `activeId` becomes URL state. Enables every other bridge, badge link, and shareable run URL.                                                                                                                                                                                                                                               | [implementation]                                                      | high                           | S      | `page.tsx:25`                                                                    |
| 5   | **Render agent activity in ExecutionRunThread**: `joinRunStream` already parses events — render a collapsed message/tool-call view (or link to the thread once #3 lands) so a running governed task is not a black box.                                                                                                                                                                                                    | [implementation]                                                      | high                           | M      | `api.ts:342-404`, `execution-run-thread.tsx:81-106`                              |
| 6   | **Resume affordance for `pending_interrupt`**: the route exists (`POST /runs/{id}/resume`); add an api.ts `resumeRun` + an inline box on the run detail — currently a dead end that costs a curl.                                                                                                                                                                                                                          | [implementation]                                                      | medium                         | S      | `plugin.py:212`, `api.ts` (no `resume` fn), `A3_SMOKE.md` scenario D             |
| 7   | **Pending-decision visibility**: sidebar badge or a `?filter=awaiting` state on ExecutionRunList so `awaiting_input`/`candidate_complete` is findable without opening each run. Cheapest version: sort/filter in `execution-run-list.tsx` using the already-fetched `workrun` status… which is not on the map row — needs either a `workrun_status` projection on `GET /runs` or per-row workrun fetch.                    | [decision-needed] (data-source choice)                                | high for demo                  | S-M    | `execution-run-list.tsx`, `plugin.py:184-211`, `STATUS_LABEL`                    |
| 8   | **Unified status vocabulary**: one `lifecycleStatus(run, workrun)` projection in `labels.ts` emitting the WP-A6 set (`agent_finished`, `verified`, `accepted`, `approved_to_act`, `external_executed`, `UNKNOWN`) over today's two enums; replace `runStatusLabel`/`statusLabel` split.                                                                                                                                    | [implementation]                                                      | medium                         | M      | `execution-run-card.tsx:24-43`, `labels.ts:9-19`, `api.ts:18-25`                 |
| 9   | **Component dedup onto `components/ui` + adopt app tokens**: mechanical restyle of `src/ewcp` buttons/inputs/cards; same props, no behavior change. Includes wiring or deleting `UnverifiedBadge`, `exploratory.ts`, dead registry helpers (A3 rule: no dead code).                                                                                                                                                        | [implementation]                                                      | medium                         | M      | all of `src/ewcp/components`, `components/ui/*`                                  |
| 10  | **`/verify` root route** mounting `VerifyView` without a hash → the verify-by-them lane gets a stable entry independent of a permalink.                                                                                                                                                                                                                                                                                    | [implementation]                                                      | low-medium                     | S      | `app/verify/[hash]/page.tsx`, `verify-view.tsx`                                  |
| 11  | **Rename/situate the "EWCP" nav entry**: for a Claude-like workspace the governed lane should read as product vocabulary (e.g. "Verified work" / "Kiểm chứng"), not a program acronym. Placement vs folding into `/workspace/chats` is a product call.                                                                                                                                                                     | [decision-needed]                                                     | medium                         | S      | `workspace-nav-chat-list.tsx:36-44`                                              |
| 12  | **i18n the ewcp components** or declare pilot = vi-only explicitly: add `useI18n` keys (en/zh) behind the existing VN copy.                                                                                                                                                                                                                                                                                                | [decision-needed] (pilot scope)                                       | low for demo, real for product | M      | every `src/ewcp/components/*.tsx`                                                |
| 13  | **Hybrid lane (c)**: governed capability invoked mid-chat with a rendered result card — requires the A5a typed invoke contract + a kernel producer PR (`create_task` is client-ready but there's no tool/MCP surfacing it to `lead_agent`), plus a governed-result card type in the message list. Do not stub the card on fixtures — it would fake the contract A5a exists to pin.                                         | [blocked] on A5a                                                      | high eventually                | —      | A5a WP in master plan; `kernel_client.py:352`; `components/workspace/messages/*` |
| 14  | **`UNKNOWN` + `external_executed` surfaces** (write-path states): no producer exists until A5b's governed write lands.                                                                                                                                                                                                                                                                                                     | [blocked] on A5b                                                      | —                              | —      | master plan WP-A5b                                                               |

### Top-3 gaps (for the structured output / sequencing)

1. **Governed runs cannot be created from the product UI** — intake,
   files, and `create_task` all exist but are unwired (#1, #2).
2. **Lanes have no cross-references** — run↔thread is one object with two
   unlinked ids and no deep link (#3, #4).
3. **The run detail is blind** — SSE parsed and discarded, no resume UI,
   no pending-decision visibility (#5, #6, #7).

### Out of scope for fixture phase

Feature code, kernel changes, live governed intake (#1/#2 land as their own
implementation WPs), the A5a hybrid card (#13), A5b write states (#14).
This doc is the map; each proposal above names the surface it would touch.

## 4. Fixture contract aid

`frontend/tests/unit/ewcp/fixtures.ts` adds one canned `RunView` +
`ExecutionRun` pair (verified governed run with decisions, deliverables,
counts) exercising the shared components' wire assumptions — docs aid only,
mirrors the kernel `test_api_contract.py` shapes (lowercase TaskStatus,
`options_v2` slugs, `seal_ok` tri-state).

## 5. Proposal status (post-map)

Implementation slices since `e8f12bab` — the §1-3 inventory above stays
verified as of that commit:

- #1+#2 (governed intake, task-mode pick), #3 (run↔thread bridge), #4
  (deep link), #5 (SSE activity render), #6 (resume box): shipped —
  product PRs #40/#41+.
- #8 (unified `lifecycleStatus` + `LifecycleBadge`), #9 (`components/ui`
  dedup + token adoption; `exploratory.ts`, `UnverifiedBadge`, and dead
  `unionInputs`/`inputsFor` deleted — `isZipInput` survives, wired by
  #1), #10 (`/verify` root route): shipped — A6 slice 2 PR.
- Still open: #7 [decision-needed], #11 [decision-needed], #12
  [decision-needed], #13 (A5a merged — mechanically feasible now,
  unimplemented), #14 [blocked on A5b].
