# A6 browser E2E — governed surfaces (verification)

Recorded browser run of the merged A6 slices on `product/vnext`
(`fa4a670c`). Stack: kernel `:8080` (sealed, `EWCP_REQUIRE_AUTH=1`) +
gateway `:8001` (`ewcp_core` plugin, editable install) + frontend
`:3000`. One continuous annotated recording (165s) by the testing agent:
`/home/ubuntu/screencasts/rec-d026aa9a-12a2-45a5-9995-0dfc48ad4341/rec-d026aa9a-12a2-45a5-9995-0dfc48ad4341-speedramp.mp4`
+ screenshots under `/tmp/` (linked per surface). Verdicts report what
was observed.

## Verdicts

| Surface | Verdict | Observed |
| --- | --- | --- |
| #40 governed-create — gallery → form → submit → run/thread → map row | **PASS** | 5 spec cards from kernel `/outcomes` + inert "Sắp có" bank card. dossier_check intake → slot attach → `er_57f34f3b…`: LifecycleBadge, `governed · wr:` chip, thread link, list row, kernel section + deliverables + 9 checks. |
| #40 idempotent replay | **PASS** | Same form POST ×2 (key `a6e2e-idem-1`): second response `idempotent_replay: true`, same `er_507fd5c9…`, single map row. |
| #40 governed-create — `required_context` packs (invoice_recon, three_way_match, erp_reads) | **FAIL** | Intake renders file slots only; kernel 422s for missing context, surfaced as generic "kernel rejected the request". 3 of 5 gallery cards are dead ends (F2). |
| #42 unified status labels | **PASS** | Observed "Đã tiếp nhận" (accepted), "Agent hoàn tất" (candidate_complete), "Đã niêm phong" (verified) across runs. |
| #42 `/verify` | **PASS** | Root `/verify` resolves (two lanes). `/verify/6e316074…` → emerald PASS card + validator checks + artifacts. `/verify/deadbeef0000` → honest error, no fabricated PASS. |
| #44 `EwcpInvokeStep` card | **PASS** (post-workaround, see F1) | Real model called `ewcp_capabilities` → `ewcp_invoke`; card rendered `pack:dossier_check`, LifecycleBadge, `wr:`/`inv:` chips, "Xem run được quản trị →" → `er_523e7450…` invoke row keyed `(created_by, idempotency_key)`. Follow-up invoke → second card with **replay** chip, same `wr`/`inv`/`er`, `idempotent_replay: true`. Screenshots `s3_invoke_card.png`, `s3_replay_card.png`. |
| Cross-links run ↔ thread | **PASS** | Both directions, both modes: run detail → "thread …" → chat header "EWCP · governed" / "EWCP · invoke" chip → back to run detail. |
| Stale/degraded | **PASS** | Kernel killed: list still renders stored rows; gallery falls back to 2 hard-coded spec cards; governed detail shows red "kernel unreachable" — no fabricated status/checks. (`s5_degraded.png`.) No UNKNOWN/accepted row existed to capture — the egress-denied chat runs never minted map rows. |
| Approve → seal (human gate) | **PASS** | Synthesized `DecisionCard` (this PR's fix) rendered at `candidate_complete` on invoke run `er_523e7450…`; **"Duyệt & niêm phong" clicked in UI** → badge "Đã niêm phong", manifest `1b9b3644…`, `human_approval` check bound to `user:8ce9eef9@tenant:demo`. (`decision_card.png`, `after_seal.png`.) |

## Fixes in this PR

1. **Seal approval gate unreachable at `candidate_complete`** — the run
   thread rendered `DecisionCard`s only for emitted `pending_questions`;
   a clean governed run (zero questions) had no UI path to `verified`.
   Now synthesizes the approval card when the workrun sits at
   `candidate_complete`/`awaiting_approval` with no approval question;
   `PendingQuestion.decision_id` widened to `string | null` (null = the
   open gate, which the kernel accepts). Tests:
   `execution-run-thread.dom.test.tsx` +2 cases (76/76 ewcp tests green);
   eslint + prettier + tsc clean. Live-verified in-browser (row above).

## Findings (report-only — structural, need owner decision)

- **F1 [severe] — egress `approved_cloud` fail-closes every Google-GenAI
  model call.** `egress_policy._model_endpoint_host()` probes
  `base_url`/`api_base`/… — `ChatGoogleGenerativeAI` exposes none →
  endpoint `None` → "model endpoint unprovable under approved_cloud —
  denied (fail-closed)" even with `generativelanguage.googleapis.com` in
  `approved_model_endpoints`. Compounded by `ewcp_tenant_id` never
  entering the runtime context → tenant resolves to `user_id` →
  unclassified → sensitive → gated. Effect: every interactive chat run
  (incl. invoke-card threads) died at the first model call; governed
  lanes survived because dispatch needs no model. Verified empirically
  in the gateway venv (`egress_denial.png`). This E2E used the
  `egress.tenant_classes: {<uid>: non_sensitive}` operator workaround to
  reach the model. Fix direction: probe the google-genai client's real
  endpoint attribute (`client._api_client`/`_http_options`), and/or wire
  `ewcp_tenant_id` into the runtime context.
- **F2 — `required_context` packs unlaunchable from the gallery** (see
  FAIL row). `GovernedIntake` renders file slots only; the
  `OutcomeSpecView` wire carries no `required_context`, so the UI cannot
  prompt for it. Needs a wire + form extension, or drop those packs from
  the gallery.
- **F3 — list-row badge stale vs detail**: rows keep "Agent hoàn tất"
  post-seal (launcher record only); detail correctly shows "Đã niêm
  phong". Designed projection gap — the list endpoint carries no workrun
  view — but reads as inconsistent.
- **F4 — transient empty 400** on the first approve POST; identical
  retry 200 → `verified`. Possibly a decision-resolution race.
- **F5 — `/verify` bad-hash error** is a generic "kernel rejected the
  request"; honest but uninformative.
- **F6 — composer-attached files don't reach the thread `uploads/`
  dir** → `ewcp_invoke files:` can't resolve them (`list_uploaded_files`
  → empty). Worked around via the uploads API; check whether message
  attachments should land in `uploads/`.
  **Resolved (F6 fix):** live re-probe on `product/vnext` shows composer
  attach DOES land `users/{uid}/threads/{tid}/user-data/uploads/` under
  the run's own thread+user — the "empty list" observation was
  `list_uploaded_files`'s by-design exclusion of current-run uploads
  (surfaced via `<current_uploads>` instead). The real divergence:
  `ewcp_invoke._resolve_files` read bytes via
  `host_sandbox_user_data_dir` — the docker-daemon mount-source
  namespace (`DEER_FLOW_HOST_BASE_DIR`) — instead of the gateway-local
  `sandbox_user_data_dir` the upload write path uses, so `files:` could
  never resolve on provisioner/DooD deployments. Fixed to read the
  gateway-local namespace; regression test
  `test_invoke_resolves_uploads_via_gateway_local_dir`.

## Environment notes (operator-side, not shipped)

- `config.yaml` plugins block needs the `ewcp_core` entry
  (`use: ewcp_core:install`) plus `uv pip install -e
  backend/extensions/ewcp-core`.
- `ewcp.tenant_id` cannot be the builtin `demo` (a sensitive egress
  tenant → governed dispatch denied); dev config uses `demo-tenant`.
- Model: `gemini-3.5-flash-lite` via `ChatGoogleGenerativeAI` stayed
  within free tier (~122K tokens across the retest, no 429s).

## Untested

- `UNKNOWN` lifecycle label — no row in that state could be produced
  honestly (denied runs never mint map rows).
- `pending_interrupt` resume path — covered by unit test only, no live
  interrupt occurred.
