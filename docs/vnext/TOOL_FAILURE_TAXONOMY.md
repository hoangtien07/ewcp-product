# N03 — Tool-invocation failure taxonomy + bounded recovery checks

Base: `product/vnext` @ `e0ac3aea`. Sources: `docs/vnext/GP01_TASKB_COST.md`
(measured failure classes from the Ollama qwen2.5:7b re-runs and the cited
gemini-3.5-flash-lite run), `docs/vnext/A7A_RELIABILITY.md`, and code-verified
mechanism reads at the base SHA. Nightly item N03 (overnight W3).

## Verdict vocabulary

| Verdict | Meaning |
|---|---|
| **enforced** (detected-by-middleware) | A middleware bounds the class — warn/hard-stop/correct/deny — without relying on model judgment. |
| **surfaced** (surfaced-to-model) | The failure reaches the model as an actionable ToolMessage/hint; recovery itself is the model's job. |
| **silent** (silent-fail) | The failure produces no signal the model can learn from (or a generic crash that hides the designed hint). |
| **MODEL_LIMIT** | What remains is model incapacity, not a missing bound; the product owns only honest surfacing of it. |

## Taxonomy — measured failure classes → mechanism → verdict

| # | Observed class (GP01 evidence) | Mechanism (file:line @ e0ac3aea) | Verdict | Bound today? |
|---|---|---|---|---|
| F1 | `present_files` on an uploads path — "Only files in /mnt/user-data/outputs can be presented" (runs 1–2) | `backend/packages/harness/deerflow/tools/builtins/present_file_tool.py:78` raises `ValueError` for a path outside the outputs dir; `:110-113` converts it to a `Command` carrying `ToolMessage("Error: Only files in /mnt/user-data/outputs can be presented…")`. | **surfaced** — the confinement rule is enforced and the rejection text reaches the model. | Yes — confinement itself is the bound; identical retries are bounded by F4. |
| F2 | `read_file` on a binary XLSX via AIO sandbox | Pre-#59: the AIO `success:false` envelope failed the SDK's typed `ResponseFileReadResult` parse → `tools.py:2855` generic "Unexpected error" — the designed hint was unreachable (**silent**). Post-#59 (`5a79e31d`): `community/aio_sandbox/aio_sandbox.py:1089-1096` catches `ValidationError`/`ApiError` → `_aio_file_read_error` (`:93-113`) re-issues `v1/file/read` → `_aio_file_error_from_body` (`:71-90`) maps `decode_error`→`UnicodeDecodeError` → `sandbox/tools.py:2849-2854` returns the designed hint ("binary file … use bash with pandas/openpyxl"). | **surfaced** (post-#59); was **silent** before. | Yes — every error class in `read_file` maps to a message the model can act on. |
| F3 | Schema drift — phantom non-schema arg `toolbench_rapidapi_key=null` (run 3) | `agents/middlewares/tool_arg_alias_middleware.py:42-53` (`_TOOL_ARG_ALIASES`) only renames its fixed table; any other non-schema arg reaches pydantic `tool_call_schema` validation with `extra='ignore'` (default) → **silently dropped**. The model never learns the arg it believed it passed never reached the tool. | **silent** → patched in this PR (`ToolArgFeedbackMiddleware`, below). | **No** → this PR adds the bound. |
| F3b | Schema drift — known alias (`contents`→`content`, `filepath`→`path`, …) | `tool_arg_alias_middleware.py:56-79` `normalize_tool_call_args` + `:101-112` `wrap_tool_call` rewrite the request; the canonical name wins; a `arg_alias_applied` transform stamp is recorded. | **enforced** (corrected by middleware). | Yes — alias table covers the common drift; unmapped drift is F3/F3c. |
| F3c | Schema drift — unknown arg leaving a required field unset | Validation raises `pydantic.ValidationError` → `agents/middlewares/tool_error_handling_middleware.py:307-343` wraps it as `ToolMessage(status="error", "Error: Tool '<name>' failed with ValidationError: …")`. | **surfaced** — the error names the missing canonical field (not the typo'd name; the F3 note now covers that too). | Yes. |
| F4 | Repeated identical call set, failed or not (GP01 retried the same present_files call byte-identically) | `agents/middlewares/loop_detection_middleware.py`: identical-call layer — `_hash_tool_calls`/`_stable_tool_key` (`:124-196`) hashes the (name, normalized-args) multiset over a 20-call window; `warn_threshold=3`, `hard_limit=5` (`:94-99`). Warn → `_queue_pending_warning` (`:542-727`) → `wrap_model_call` appends `HumanMessage` (`:921-945`); hard → `clone_ai_message_with_tool_calls(last_msg, [])` strips the turn's tool calls + `stop_reason="loop_capped"` (`:781-818`). | **enforced** — bounded at warn 3 / hard 5. Below 3 repetitions: no signal **by design** (one retry is legitimate recovery). | Yes. |
| F4b | Same tool called repeatedly with varying args | Loop-detection frequency layer — `loop_detection_middleware.py:633-727`, warn 30 / hard 50 plus per-tool overrides. | **enforced**. | Yes. |
| F4c | Per-tool stagnation (repeated errors / no-results with different args) | `agents/middlewares/tool_progress_middleware.py` — per-(thread, tool) state machine driven by `tool_result_meta.py` status stamps: `recoverable_by_model` classes stay WARNED with hints; non-recoverable escalate to per-tool BLOCKED (`auth`/`config`/`internal` block immediately). **Config default `tool_progress.enabled: false`** (`config/tool_progress_config.py:9-12`). | **enforced — when enabled**. Off by default ⇒ this bound is inert in the current product config. | Partially — machinery exists, disabled by default (deliberate upstream default; not patched — see residual risks). |
| F5 | Provider 429 / transient model errors (~68% of original wall clock in GP01 run 1) | `agents/middlewares/llm_error_handling_middleware.py:83` `_RETRIABLE_STATUS_CODES={408,409,425,429,500,502,503,504}`; `:149-165` bounded budgets (`_RETRY_BUDGET_OVERRIDES`, `_REASON_RETRY_BUDGETS` burst_rate=2); `model_retry.max_provider_delay_s` caps backoff at 300 s; exhaustion → recoverable assistant error. | **enforced** — bounded retries with a hard ceiling. | Yes. |
| F6 | Surrender mid-run — model stops with prose but produces nothing (all 3 GP01 runs; `claimed_artifacts_missing:["outputs"]` in run 2) | No middleware forces continuation: an AIMessage without `tool_calls` ends the turn legitimately. Sub-bounds that do exist: `agents/middlewares/terminal_response_middleware.py:42-66` replaces an *empty* terminal AIMessage with honest fallback text (covers the empty-response subclass only — the GP01 surrender carried prose); `extensions/ewcp-core/ewcp_core/deliverable_integrity.py:145-183,243-293` extracts claimed artifacts and persists `claimed_artifacts_missing` (advisory, post-run, durable) — called from `recovery.py:310-311` and `run_launcher.py:605-613`. | **MODEL_LIMIT** — deciding "the task isn't done" is model judgment, not a missing bound. The honest product surface = terminal fallback (bounded subclass) + integrity flag (advisory). | Partially — honesty surfaces exist; the surrender itself is not and cannot be forcibly prevented. |
| F7 | Any other tool exception | `tool_error_handling_middleware.py:111-130,307-343` — every wrapped tool exception → `ToolMessage(status="error", "Error: Tool '<name>' failed with <ExcClass>: <detail>. Continue with available context or try an alternative.")`. | **surfaced**. | Yes — no unhandled tool crash reaches the run boundary. |

## What this PR adds (the ONE permitted patch)

**Class F3 — silent non-schema arg drop** was the only measured class with
neither a bound nor a signal. The patch is a single ewcp-core middleware:

- `extensions/ewcp-core/ewcp_core/tool_arg_feedback.py` —
  `ToolArgFeedbackMiddleware`, contributed at `Placement.TOOL_VISIBLE`
  (outermost on the tool axis — sees the raw model args *before* alias
  normalization and the final model-visible result *after* error handling),
  `AgentScope.BOTH`, **`intercepting=False`** (observational; a defect fails
  open via `IsolatedMiddleware`).

- For each tool call, it diffs `tool_call.args` keys against
  `tool.tool_call_schema` properties, **crediting names
  `ToolArgAliasMiddleware` would rename** (via `normalize_tool_call_args`) so
  consumed aliases are never flagged. Any remainder = args pydantic's
  `extra='ignore'` will drop → a one-line note is appended to the result
  `ToolMessage` (including `ToolMessage`s carried inside
  `Command.update["messages"]`), and a `deerflow_tool_transforms`
  `arg_drift_note` stamp records the annotation.

- **Bounded by construction**: no retries, no argument rewriting, no
  blocking — the call executes exactly as today; only the model-visible text
  gains the ignored-args note. Schemas with `extra='allow'` (args retained)
  or `extra='forbid'` (already an error) are skipped. Registered alongside
  the existing `BudgetAdmissionMiddleware` placement in
  `extensions/ewcp-core/ewcp_core/plugin.py`.

## Regression coverage (extension-owned, offline, deterministic)

| Test | Pins |
|---|---|
| `tests/test_tool_failure_bounds.py::test_present_files_rejects_non_outputs_path_with_message` | F1 surfaced — confinement rejection is a ToolMessage. |
| `…::test_aio_error_envelope_maps_decode_error_to_unicode_decode_error` | F2 — the #59 envelope→builtin mapping (silent→surfaced fix stays fixed). |
| `…::test_read_file_binary_returns_designed_hint` | F2 — the designed binary hint text reaches the model. |
| `…::test_non_schema_args_are_silently_dropped_by_validation` | F3 — the silent-drop mechanism itself (pre-patch evidence). |
| `…::test_alias_middleware_renames_drifted_args` | F3b — alias normalization corrects drifted names. |
| `…::test_identical_calls_below_warn_threshold_produce_no_signal` | F4 — sub-threshold repetition is signal-free by design. |
| `…::test_identical_calls_warn_at_threshold_and_hard_stop_at_limit` | F4 — warn-at-3 injects a HumanMessage; hard-at-5 strips tool_calls (`loop_capped`). |
| `…::test_empty_terminal_response_gets_fallback_not_silent_success` | F6 — the bounded surrender subclass (empty terminal → honest fallback). |
| `…::test_nonempty_surrender_is_not_caught_by_terminal_middleware` | F6 — prose surrender is MODEL_LIMIT; nothing catches it (post-run flag covers detection). |
| `…::test_tool_exception_becomes_error_toolmessage` | F7 — exceptions surface as error ToolMessages. |
| `tests/test_arg_drift_feedback.py` (7 tests) | The F3 patch: note fires on dropped args, lists accepted names, skips alias-covered/clean calls, annotates `Command` payloads, stamps the transform trail, works on sync+async paths, is idempotent. |

Sibling extension tests pin the remaining advisory surfaces:
`test_ewcp_core_deliverable_integrity.py` covers `claimed_artifacts_missing`
(F6's post-run flag); upstream `backend/tests/test_loop_detection_*` and
`test_read_file_tool_binary.py` cover F4/F2 more deeply.

## Deliberately not patched (and why)

1. **`tool_progress.enabled` default (F4c).** Enabling the stagnation state
   machine is a config choice, not a code gap — the bound exists, it is opt-in.
   Flipping the default is an upstream/product decision with real behavior cost
   (per-tool blocks change run dynamics); flag for the operator, don't smuggle
   it into a test PR.
2. **Sub-warn repetition signal (F4, <3 identical calls).** Surfacing every
   second call would punish legitimate retry-after-error recovery. The 3/5
   split is a deliberate upstream trade-off; GP01's single identical retry is
   below warn by design, and the F3 note now covers the case where the retry
   itself was malformed.
3. **Surrender prevention (F6).** A bound that forces "keep working" on a
   legitimately-finished run would break every normal completion. The honest
   surface (`claimed_artifacts_missing`) is already advisory and durable —
   MODEL_LIMIT stands.
4. **`present_files` upload→outputs bridging (F1).** The confinement error is
   correct; teaching the model the copy-file workaround belongs to prompting,
   not middleware.
5. **Anything upstream** — UPSTREAM_TOUCH.md stands at 19/8 (over budget); the
   whole patch is extension-owned.

## Residual risks

- The F3 note annotates the result **after** the call ran — a dropped
  `format`-shaping arg could still produce a wrong-shaped success. The note
  teaches the model to correct the *next* call; it cannot retroactively fix
  the current one. (This is the honest bound for a post-execution check.)
- `tool_call_schema` introspection assumes a pydantic `model_json_schema()`;
  tools exposing a raw dict schema are skipped rather than risk a false note.
- Alias credit uses `normalize_tool_call_args` at TOOL_VISIBLE position —
  sees pre-normalization args exactly as the model emitted them, so the
  flagged set is accurate even when the canonical name is also present.
- The note fires on successful results too — intentional (the arg still did
  nothing), but a chatty edge case exists if a tool's schema legitimately
  tolerates extras under `extra='ignore'`; none of the measured tools do.
