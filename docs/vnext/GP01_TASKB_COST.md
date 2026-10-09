# GP-01 task-B — measured cost attribution (step-level decomposition)

Status: measured on a **fresh comparable run**, 2026-10-09. The original
453-step run's trace was destroyed with the eval box — this doc instruments
a re-run of the same fixture/task shape and answers the founder's question
at hypothesis level. **Docs-only change; no production code touched.**

**Model caveat (founder directive, 2026-10-09):** `GEMINI_API_KEY` was
retired mid-task; the re-run uses **Ollama local `qwen2.5:7b-instruct`**
(the C03 spike model), not `gemini-3.5-flash-lite`. The numbers below
therefore measure a *different, weaker* model than the original 453-step
run — cross-run comparison is hypothesis-level only, as directed. The
`ToolArgAliasMiddleware` commit `2a1e8ad4` is an ancestor of the measured
HEAD (`37e1cebe`), so the arg-alias shim was active for all runs.

## Founder question (verbatim)

> "GP-01 có task sử dụng 35 LLM calls và 453 graph super-steps, trong khi
> DeerFlow đã có các middleware quản lý vòng lặp và context. Cần xác định
> liệu chi phí đó xuất phát từ độ phức tạp thật của task, model chưa đủ
> mạnh, cấu hình prompt/context chưa hiệu quả, hay việc middleware chưa
> xử lý được những vòng lặp thực tế."

## Prior measured numbers (cited, not re-measured)

From `docs/vnext/GP01_REEVAL.md` @ `37e1cebe`: task B on
`gemini-3.5-flash-lite`, AIO sandbox (`allow_host_bash: false`,
`network.mode: isolated`), pane lane `POST /api/ewcp/runs
task_mode=general`: **453 super-steps / 35 llm_calls / $2.8832 / 231.5 s
wall**, of which **~157 s was provider-429 backoff sleeps**. Deliverable
was correct (all 4 seeded defects found). super-steps = max
`checkpoints.metadata.step`.

## Regenerated fixture (original xlsx unrecoverable; spec preserved)

`inventory.xlsx` (7.1 KB) regenerated with openpyxl per the
`GP01_EVAL.md` description + the defect arithmetic recorded in
`GP01_REEVAL.md` ("ITM-02 45+10-3=52 vs counted 47"):

| sheet | rows | seeded content |
|---|---|---|
| `stock` | 10 (+hdr) | `item_id, description, unit, qty_on_hand`; **ITM-02 twice — 45 vs 47** (duplicate key, divergent qty) |
| `movements` | 30 (+hdr) | `movement_id, date, item_id, direction, qty`; **orphan `ITM-04` in+15**; **`not a date`** at M-025; **`2025-02-30`** at M-028; ITM-02 in+10 / out-3 |
| `count` | 9 (+hdr) | `item_id, counted_qty, counted_by`; **ITM-02 counted 47 vs expected 52 (45+10-3)** |

Row counts (~50 total) and all 4 defect classes match the eval
description; exact cell layout of the original is unrecoverable
[giả định — regenerated faithfully, not byte-identical].

## Task intent (verbatim — original prompt text unrecoverable)

Runs 1–2 used:

> "Attached is inventory.xlsx, an inventory workbook with 3 sheets: a
> stock baseline, a movements log, and a physical count. Diagnose every
> data-quality issue: duplicate item keys, orphan references to missing
> items, invalid dates, and quantity mismatches between stock, movements,
> and the count. Reconcile ITM-02: compute its expected on-hand quantity
> from the baseline plus movements and compare it against the physical
> count. Write all findings to outputs/inventory_diagnosis.md, naming
> each defect with its sheet name and row number."

Run 3 (discrimination attempt) prepended explicit tool guidance: path +
"read it first with read_file or bash + python openpyxl (do NOT call
present_files on it — present_files is only for /mnt/user-data/outputs)".

## Run matrix (Ollama qwen2.5:7b-instruct, CPU ~5.5 tok/s)

| # | intent | llm_calls | input | output | wall | tool calls | outcome |
|---|--------|-----------|-------|--------|------|------------|---------|
| 1 | base | 2 | 4 100 | 103 | ~90 s | 1× `present_files` → Error | **FAIL** — surrendered, told user to move file; `claimed_artifacts_missing:["outputs"]` |
| 2 | base | 2 | 4 100 | 97 | ~81 s | 1× `present_files` → Error (byte-identical retry) | **FAIL** — identical surrender (deterministic at temperature 0.2) |
| 3 | +tool guidance | 2 | 4 100 | 417 | ~201 s | 1× `read_file` → Error (harness-side pydantic crash) | **FAIL** — correct tool this time; surrendered mid-reasoning; `claimed_artifacts_missing` |

All three runs: `status=success` at the run layer, zero work product —
the thread workspace contains only the uploaded file. Attempts exhausted
(the task's ≤3 cap). A weak/failed run is still attribution evidence per
directive: it decomposes *where* the steps went.

## Measured: what one "super-step" actually is

Decoding `checkpoints` + `writes` (`checkpoint_ns=''`): `metadata.step`
counts **LangGraph node executions**, and in this graph most nodes are
middleware hooks. All three runs wrote 39 checkpoints (2 `input` +
36 `loop` + 1 `update`) for just 2 LLM calls. Run 1's loop-step
sequence:

| steps | nodes | count |
|-------|-------|-------|
| 1–7 | `before_agent` chain: KnowledgeScope → ThreadData → Uploads → Sandbox → DynamicContext → LoopDetection → EgressPolicy | 7 (once per run) |
| 8–10 | `before_model`: ArtifactCapture → DurableContext → DeerFlowSummarization | 3 |
| 11 | **`model` (LLM call 1)** | 1 |
| 12–19 | `after_model`: Clarification → SafetyFinishReason → ModelLengthFinishReason → TerminalResponse → LoopDetection → Title → TokenUsage → DurableContext | 8 |
| 19–20 | Send→`tools` + tool exec (`present_files` → Error) | 2 |
| 21–23 | `before_model` again: ArtifactCapture → DurableContext → Summarization | 3 |
| 24 | **`model` (LLM call 2)** | 1 |
| 25–32 | `after_model` chain (8) | 8 |
| 33–35 | `after_agent` teardown: LoopDetection → Memory → Sandbox | 3 (once per run) |
| 36 | tail loop step; then `update` checkpoint `runtime_run_duration` | 2 |

**Steps-per-LLM-call (measured): ~14 middleware/tool hops per model
turn** (10 hook nodes around each `model` + Send/tool steps, amortizing
the once-per-run before_agent/after_agent chains; 36 loop steps ÷
2 calls ≈ 18 here because a 2-call run is mostly one-time chains —
the per-turn steady-state is ~13–14). Check the arithmetic against the
original run:
`453 super-steps ÷ 35 llm_calls = 12.94` — the same ratio. **The "453
super-steps" figure is ~92% middleware plumbing by construction**; it is
not 453 decisions or 453 model calls. The LangGraph agent loop
checkpoints every middleware hook, so headline step counts scale with
middleware-chain length, not task depth.

## Measured: per-LLM-call tokens and context growth

From `token_usage_middleware` log lines + `runs` row (kernel-side
metering degraded — see below):

| call | run 1 | run 2 | run 3 |
|------|-------|-------|-------|
| 1 | in 2 050 / out 29 | in 2 050 / out 29 | in 2 050 / out 36 |
| 2 | in 2 050 / out 74 | in 2 050 / out 68 | in 2 050 / out 381 |

- Input context did **not** grow between calls (2 050 both times) — the
  run died before history accumulated. Cannot measure the original run's
  input-growth curve [suy luận: with 35 calls + tool outputs it plausibly
  approached the 32 000-token summarization trigger].
- `DeerFlowSummarizationMiddleware` executed as a node every turn (steps
  10, 23) but **never fired a compaction** — no summary write in any
  checkpoint; configured trigger `tokens: 32000` never approached here.
- Output tokens are tiny in runs 1–2 (29/74 — terse surrender); run 3's
  call 2 spent 381 tokens reasoning aloud about encodings, then stopped
  with no tool call.

## Measured: wall-time decomposition

| component | this run | original (cited) |
|-----------|----------|------------------|
| model latency | ~90 s of ~91 s wall ≈ **98%** (2 calls ≈ 40–50 s each at ~5.5 tok/s CPU) | ~74 s model+tools of 231.5 s ≈ 32% |
| tool exec | <1 s (present_files rejects instantly; read_file round-trip ~1 s) | (not isolated) |
| middleware/hooks | unmeasurable (<50 ms total for 37 hops) [suy luận — local Python, no I/O in these hooks] | — |
| provider 429 backoff | 0 s (local Ollama) | **~157 s ≈ 68% of wall** |
| kernel admission | 2× 422 round-trips, ms-scale | — |

For this run wall ≈ model latency, exactly as C03 predicted for
CPU-hosted 7B. For the original, the dominant wall component was
provider-side rate limiting, not the graph.

## Measured: tool-call histogram and loop check

- Run 1+2: `present_files(/mnt/user-data/uploads/inventory.xlsx)` ×1
  (unique signature; the second AI message in the checkpoint stream is a
  re-persisted write, not a second call — verified by message id +
  `runs.llm_call_count=2`). Error: "Only files in /mnt/user-data/outputs
  can be presented" (`present_file_tool.py:78` — correct rejection;
  `present_files` is a *presentation* tool, not a reader).
- Run 3: `read_file(path=…uploads/inventory.xlsx, toolbench_rapidapi_key=null)`
  ×1. Two anomalies: (a) **phantom arg** `toolbench_rapidapi_key` —
  ToolArgAliasMiddleware (`2a1e8ad4`) maps aliases, it does not strip
  non-schema extras; the arg was ignored but marks the arg-shape fumble
  the directive asked us to look for. (b) the tool call **crashed
  harness-side**: the AIO sandbox `file/read` endpoint returned an error
  body that fails the `agent_sandbox` SDK's `ResponseFileReadResult`
  pydantic model (`data.content`, `data.file` missing). The designed
  friendly error at `sandbox/tools.py:2849` ("it appears to be a binary
  file … use bash with pandas/openpyxl") never reaches the model — a
  remote-contract gap in the AIO sandbox path, not in `read_file` itself.
- **No repeated identical tool+arg signatures, no write→exists→read→write
  cycles** in any run. `LoopDetectionMiddleware` ran 4 hook nodes/run and
  had nothing to catch (thresholds `warn 3 / hard 5 / window 20`).

## Verdict per hypothesis

**1. Real task complexity — NOT the driver (of step count).** The fixture
is a ~50-row, 3-sheet reconcile: an honest minimal plan is read 3 sheets
→ cross-check → write 1 file ≈ **6–10 LLM turns** [suy luận — estimated
from task structure, not measured end-to-end]. The original used 35 —
i.e. the call count overshot the task's intrinsic need ~3–4×. The
**453-step headline is fully explained by graph shape**: 35 turns × ~13
middleware hops ≈ 455 ≈ 453 (measured ratio this run: 13–14/turn). Steps
are not a cost unit; LLM calls and wall time are.

**2. Model not strong enough — DOMINANT for this re-run; contributing for
the original.** Measured here: qwen2.5:7b-instruct surrendered 3/3 times.
Twice deterministically wrong tool (`present_files` on an uploads path —
a semantic choice error the alias middleware cannot fix); once correct
tool after prompt guidance, then surrender mid-reasoning after a
harness-side crash it could have routed around (`bash` + openpyxl was
available and named in both the intent and the tool's own error text).
This is the same "clarification-dodge / early-surrender" failure class
C03_OLLAMA_SPIKE.md measured for the 7B tier. For the original Gemini
run, 35 calls for a ~10-call task reads as model-limited efficiency, not
failure [suy luận — original trace gone].

**3. Prompt/context inefficiency — small but real, and fixable.** Run 3's
one-line tool hint changed model behavior (right tool first try) → prompt
content demonstrably steers a weak model [measured]. But it did not fix
the outcome (surrender persisted). Context-side: input stayed flat at
2 050 in-window; `token_budget` is off; summarization at 32 k never
approached. For the original run: 35 calls × tool outputs would have
grown input each turn [suy luận]; whether summarization fired there is
unrecoverable.

**4. Middleware not handling real loops — NOT what happened; but two real
gaps measured.** (a) There were **no loops to handle** — zero repeated
tool+arg signatures; `loop_detection` had nothing to fire on. The 453
steps aren't loops either; they're plumbing. (b) Measured new gap: **AIO
sandbox binary-read contract** — `file/read` returns a non-schema error
body for binary files → pydantic crash instead of the designed
recoverable error. (c) Measured config gap: `POST /budget/admissions`
→ **422 tenant_id required when no api keys are configured** — plugin
config sets `invoke.tenant_id: demo` but not `budget.tenant_id`, so
kernel metering degrades to local policy and `budget_admissions`/`ledger`
stay empty; gateway `token_usage_middleware` is the only meter.

## The middleware knob that would have cut this

For the original run's *wall* cost (68% = 429 backoff): none — provider
rate limit is upstream of the graph; `model_retry.max_provider_delay_s`
(300 s) already bounds it. For the *step count*: none — steps are
structural, and cheap (<1 ms hops). For the *LLM call count* (35):
`loop_detection.hard_limit`/`tool_freq_hard_limit` would only have helped
if the calls were identical-signature repeats, which this run shows was
not the failure mode [suy luận for the original]. The honest answer: **no
existing loop/context middleware knob would have cut the original run's
cost** — its cost was ~35 genuinely different model turns plus provider
throttling; the lever is model quality (hypothesis 2), not middleware
(hypothesis 4). The one config knob that *would* have changed the
measurement itself: `budget.tenant_id` (kernel metering), and the one
code fix that would have given the weak model a survivable error: the AIO
`file/read` binary-error contract (`agent_sandbox` SDK /
`ResponseFileReadResult`), so `sandbox/tools.py:2849`'s "use bash +
openpyxl" hint actually reaches the model.

## Reproduction

```bash
# stack (no Gemini env anywhere):
ollama serve & ollama pull qwen2.5:7b-instruct
# kernel:   uvicorn ewcp.api.app:create_app(store_dir='var/ewcp-gp01-taskb') :8080
# gateway:  DEER_FLOW_AUTH_DISABLED=1 .venv/bin/uvicorn app.gateway.app:app :8001
# config.yaml models: ChatOllama qwen2.5:7b-instruct base_url http://localhost:11434
# run: POST /api/ewcp/runs  multipart intent=… task_mode=general files=inventory.xlsx
# instrument: sqlite3 .deer-flow/data/deerflow.db  (checkpoints.metadata.step,
#             writes.channel 'branch:to:*' for node names, runs.llm_call_count,
#             token_usage_middleware log lines for per-call tokens)
```

Fixture generator + run/instrument/analyze harness:
`~/gp01_taskb/{gen_fixture,run_and_instrument,analyze}.py` on the session
box (not committed — session-local tooling).
