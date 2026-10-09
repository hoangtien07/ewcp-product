# C03 — Ollama local-inference spike (measured)

**Date:** 2026-10-09 · **Gate source:** EWCP master plan, item C03 (founder decision batch 2026-10-09)
**Stack:** ewcp-product `product/vnext@fa4a670c` + kernel `enterprise-work-control-plane@99909ef` + Ollama `0.40.2` on this session's VM
**Model under test:** `qwen2.5:7b-instruct` (Ollama tag `845dbda0ea48`, 4.7 GB Q4-class blob)

## Box

```
$ free -g | head -2 && nproc
               total        used        free      shared  buff/cache   available
Mem:              31           1          27           0           2          29
8
```

No GPU — inference is CPU-only (`>>> WARNING: No NVIDIA/AMD GPU detected. Ollama will run in CPU-only mode.`).

## Setup (commands + diffs)

1. `curl -fsSL https://ollama.com/install.sh -o /tmp/ollama_install.sh && sh /tmp/ollama_install.sh` → systemd unit `ollama.service` active, API on `127.0.0.1:11434`.
2. `ollama pull qwen2.5:7b-instruct` → 4.7 GB at ~297 MB/s, `success`.
3. Backend needs the harness extra (not installed by default):
   `cd backend && uv pip install 'deerflow-harness[ollama]' --python .venv/bin/python` → `+ langchain-ollama==1.1.0 + ollama==0.6.3`.
4. `config.yaml` diff (gitignored file — documented here because it cannot be committed):

```diff
 models:
+  - name: qwen25-local
+    display_name: Qwen2.5 7B Instruct (Ollama)
+    use: langchain_ollama:ChatOllama
+    model: qwen2.5:7b-instruct
+    base_url: http://localhost:11434
+    num_predict: 4096
+    temperature: 0.2
+    context_window: 32768
+    supports_thinking: false
+    supports_vision: false
-recursion_limit: 100
+recursion_limit: 1000        # same intervention as GP01_EVAL I1
 sandbox:
-  allow_host_bash: false
+  allow_host_bash: true      # GP01_EVAL I2 — without an execute tool, small models fabricate
 plugins:
-  - name: ewcp_packs          # stale from the old blueprint; package does not exist on product/vnext
-    package: ewcp-packs
-    use: ewcp_packs:install
-    enabled: true
-    required: true
+  - name: ewcp_core
+    package: ewcp-core
+    use: ewcp_core:install
+    enabled: true
+    required: false
+    table_prefix: ewcp_
+    config:
+      kernel_url: http://127.0.0.1:8080
+      budget: {cap_usd: "5.00", tenant_id: demo, usd_per_1k_tokens: "0.004",
+               max_output_tokens_per_call: 4096, general_on_policy_unavailable: local}
+      egress: {default_mode: local_only}
+      invoke: {tenant_id: demo}
+      general_recursion_limit: 1000
```

Native `ChatOllama` (`/api/chat`) was chosen over the OpenAI-compat `/v1` path per `config.example.yaml`'s own note that the native API separates tool/reasoning content correctly; OpenAI-compat llama-server wiring remains an untested alternative.

5. Boot:
   - Kernel: `EWCP_REQUIRE_AUTH=1 EWCP_TENANT_KEYS='ewcp-demo-7f3a:demo' EWCP_SEAL_KEY=<rand> EWCP_STORE_DIR=var/ewcp-c03 .venv/bin/uvicorn app.main:app --port 8080`
   - Gateway: `DEER_FLOW_AUTH_DISABLED=1 EWCP_KERNEL_URL=http://127.0.0.1:8080 EWCP_KERNEL_API_KEY=ewcp-demo-7f3a .venv/bin/uvicorn app.gateway.app:app --port 8001`
   - `GET /api/ewcp/_status` → `{"extension":"ewcp_core","kernel_configured":true,"client_started":true,"store_started":true,"egress":{"default_mode":"local_only"},"budget_cap_usd":"5.00","budget_admission_enabled":true,"general_recursion_limit":1000}` — the stack admits `qwen25-local` as the only configured model and routes it through budget admission + egress `local_only` unchanged.

## Criterion 1 — Tool calling: **PARTIAL**

Tool calls do reach the harness and dispatch. Receipts + thread state prove real wire-up — but arg-schema adherence fails on `write_file` and the model quits instead of self-correcting.

Probe (`POST /api/threads/{tid}/runs/stream`, `context.model_name=qwen25-local`, instruction = write `probe.txt` then read it back) — **identical result on 2/2 attempts**:

```
TC: write_file {"path": "/mnt/user-data/workspace/probe.txt", "mode": "w", "contents": "c03-probe-42"}
tool → Error invoking tool 'write_file' with kwargs {...}:  content: Field required
TC: read_file {"path": "/mnt/user-data/workspace/probe.txt"}
tool → Error: File not found: /mnt/user-data/workspace/probe.txt
TC: ask_clarification {"question": "I couldn't find the file ...", "clarification_type": "missing_info"}
```

- The JSON is well-formed and dispatchable — the failure is **argument naming**: schema wants `content`, model emits `contents` (+ hallucinated `mode: "w"`). Deterministic at `temperature: 0.2` (2/2).
- After one validation error the model does not re-read the error and retry; it pivots to `ask_clarification` (asks about the *path*, not the arg it got wrong) — 2/2. Agent-loop recovery behavior is absent at 7B.

Counter-evidence — at the **raw `/api/chat`** level the same model parses clean calls every time (3/3 probes, incl. a 3,555-token generation that returned one complete `write_file` call):

```
$ curl -s http://127.0.0.1:11434/api/chat -d @tool_probe.json   # tools=[write_file,read_file]
message.content=""  tool_calls=[{"function":{"name":"write_file","arguments":{"path":".../orders_cleaned.csv","content":"order_id,...\nORD-001,..."}}}]
eval_count=129  done_reason=stop
```

## Criterion 2 — Task correctness (GP-01-lite): **FAIL**

Fixture `orders_messy.csv` (11 rows: two date formats, duplicate `ORD-002`, impossible `2025-13-45`, two `not_a_number` amounts, pasted `TOTAL` row, one missing rep, one missing customer). Lane = `POST /api/ewcp/runs` `task_mode=general` — the pane path from GP01_EVAL.

| Attempt | Lane | Result | Files on disk |
|---|---|---|---|
| `er_7b314033` | `/api/ewcp/runs` | `completed` — but **zero task work**: first action `present_files` on the uploads path → error ("only files in /mnt/user-data/outputs can be presented") → final text asks the user to move the file and stops. Never read the CSV. | `uploads/orders_messy.csv` only |
| `046291c4` (follow-up, same thread) | `/api/threads/{tid}/runs` | `error` — `read_file` **succeeded** (CSV returned verbatim), next model call → "empty response" ×2 → `EmptyModelResponseError` kills the run. | none |
| `er_8f64826b` (fresh, intent tightened) | `/api/ewcp/runs` | `failed` — same shape: `read_file` succeeds → empty response ×2 → terminal. | none |

0/2 attempts produced `orders_cleaned.csv` or `cleaning_summary.md`. `find …/threads/{tid}/user-data -type f` shows only the uploaded input.

**The "empty response" is the interesting failure.** Gateway log + ollama journal:

```
15:36:15 WARNING llm_error_handling: Transient LLM error on attempt 1/2 ... Model returned a completed response with no content
15:37:13 WARNING llm_error_handling: LLM call failed after 2 attempt(s): Model returned a completed response with no content
15:37:13 INFO token_usage: LLM token usage: input=2050 output=296 total=2346
```

The model **generated 296–629 tokens** per attempt (`slot print_timing … eval time = 55343 ms / 296 tokens`), HTTP 200, usage_metadata present — yet the `AIMessage` carried no content, no `tool_calls`, no `invalid_tool_calls` (`_raise_for_empty_response` passes all of those; `model_response.py:20-27`). The generation was long enough to be the file-write call; whatever was emitted parsed to nothing — consistent with an unparseable `<tool_call>` payload being stripped by llama-server (`--chat-template chatml`, no-jinja). **Not proven**: the identical-shape raw-API probes (stream and non-stream) always produced valid `tool_calls`, so the trigger needs the full harness context (2050-token system prompt + full toolset). Treat the mechanism as inferred, the outcome as measured.

## Criterion 3 — Tokens/sec (CPU, 8 vCPU)

Source: `journalctl -u ollama` `print_timing` lines, n=27 generation calls + n=14 prompt evals.

| Metric | min | median | max |
|---|---|---|---|
| prompt eval | 65.6 t/s | ~74 t/s | 82 t/s |
| generation | 3.4 t/s | ~5.5 t/s | 7.0 t/s |

Cold smoke (`/api/generate`, 2-token reply): 9.1 t/s gen / 45.7 t/s prompt incl. model load. Agent-loop generations run slower (~5.5 t/s median) because prompts are 2–3k tokens and outputs are long tool-arg payloads. A single LLM turn costs ~30–120 s wall — a 20-call task ≈ 10–40 min; probe3's 3,555-token generation took **11m43s**.

## Criterion 4 — Memory

```
$ ps -o pid,rss -p $(pgrep -f llama-server) -p $(pgrep -x ollama)
   3875    60040   ollama        # serve/API
   4631  6309880   llama-server  # runner: ≈6.0 GiB RSS (4.4 GB weights + ctx/cache)
```

Model blob on disk: 4.4 GB. Peak RSS ~6.0 GiB under load (grew from 5.0 GiB at first load as context filled; `-c 4096` slot). A 16 GB box is the comfortable floor; 8 GB would run but leave little headroom for the rest of the stack.

## Criterion 5 — Egress: **PASS (local-only)**

Captured **during** an in-flight inference run:

```
$ ss -tnp | grep ollama
ESTAB 0 0 127.0.0.1:11434  127.0.0.1:57758 users:(("ollama",pid=3875))
ESTAB 0 0 127.0.0.1:11434  127.0.0.1:57292 users:(("ollama",pid=3875))
ESTAB 0 0 127.0.0.1:35198  127.0.0.1:39745 users:(("ollama",pid=3875))
$ ss -tnp | grep "pid=3875" | grep -v 127.0.0.1   →  NONE
```

Runner command line (self-inflicted isolation, not policy): `llama-server … --host 127.0.0.1 --offline -np 1 -c 4096`. Zero non-loopback sockets on either process. Caveat: this is a socket snapshot, not a packet trace — covers the measured inference window only. Independently, egress policy `local_only` admitted `127.0.0.1:11434` as an in-boundary model host with no extra config.

## Budget lane sanity (bonus evidence)

Kernel `ledger`/`budget_admissions`: 14 rows each, all `model='qwen2.5:7b-instruct'`, `tenant_id='demo'`, priced at the configured `usd_per_1k_tokens: "0.004"` list price — e.g. `(id=14, run=7f8ba826…, tokens=2346, usd=0.009384)`. Attribution works; real billed spend is $0 (local inference).

## Verdict

| Criterion | Verdict |
|---|---|
| Tool calling through harness | **PARTIAL** — wire works, small tools dispatch; `write_file` arg-name fails 2/2; the decisive write turn returns empty 2/2 |
| GP-01-lite task correctness | **FAIL** — 0/2 runs produced a file (one silent-fabrication `completed`, one hard `failed`) |
| Tokens/sec | measured — **~5.5 t/s gen median**, ~74 t/s prompt, on 8-vCPU CPU-only |
| Memory | measured — **~6.0 GiB RSS** for 7B-Q4 + 4k ctx |
| Egress | **PASS** — loopback-only sockets during inference; `local_only` policy admits the endpoint natively |

**Not production-ready** — per the directive, no claim otherwise. Blocking defects at this size: (a) `write_file` schema adherence (`contents` vs `content`, hallucinated `mode`), (b) zero recovery after tool errors (clarification dodge, or stop), (c) empty-response kill at the long-file-write turn (2/2), (d) ~5.5 t/s makes agent loops impractically slow on CPU.

**What would plausibly work** [suy luận — not measured here]: a GPU box (≥16 GB VRAM) running a tool-specialized model at ≥30 t/s — e.g. `qwen3:14b`, `qwen2.5-coder:32b-instruct`, or a function-calling fine-tune (xLAM-class) — plus harness-side mitigations that are cheap regardless: (1) arg-name retry shim that re-asks with the validation error echoed (turns the `contents` bug into a self-fixing retry), (2) a second empty-response retry (budget already exists — it consumed 1 retry and still died), (3) `num_predict` and prompt hygiene to keep tool-call generations small. Local inference for single-shot/non-agentic calls (summaries, classification) is already viable today at these numbers.

## Reproduce

```bash
sh /tmp/ollama_install.sh && ollama pull qwen2.5:7b-instruct
cd backend && uv pip install 'deerflow-harness[ollama]' --python .venv/bin/python
# config.yaml diff above; boot kernel :8080 + gateway :8001 as above
curl -X POST :8001/api/ewcp/runs -F intent='<task>' -F task_mode=general -F 'files=@orders_messy.csv'
```

## Follow-up 2026-10-09 — arg-name alias shim + re-probe

**Change:** `ToolArgAliasMiddleware` (always-on, harness layer) normalizes
tool-call args before schema validation — `contents`/`text`/`body`/`data` →
`content` on `write_file`; `file_path`/`filepath`/`file_name`/`filename` →
`path` on `write_file`/`read_file`/`str_replace`. Exact-match only: a
canonical field already present wins, the first listed alias wins, unknown
extra args pass through untouched (still error where the schema is strict),
and every rewrite is logged under `arg_alias_applied`. Unit tests replay the
measured C03 payload `{"path": ..., "mode": "w", "contents": "c03-probe-42"}`
→ normalized and dispatched.

**Re-probe (same box class, `qwen2.5:7b-instruct`, embedded `DeerFlowClient`
thread `c03-reprobe`, instruction = write `c03-reprobe-42` to
`/mnt/user-data/workspace/probe.txt` then read it back):**

```
TC: write_file {"description": "", "path": "/mnt/user-data/workspace/probe.txt", "content": "c03-reprobe-42"}
tool -> write_file: 'OK'
TC: read_file {"description": "", "path": "/mnt/user-data/workspace/probe.txt"}
tool -> read_file: 'c03-reprobe-42'
# file on disk: c03-reprobe-42 (verified)
```

Verdict: **write_file round-trip now dispatches and executes end-to-end** —
vs. the 2/2 validation failure above. Caveat, reported honestly: this run
the model emitted the *canonical* `content` itself, so the alias rewrite did
not fire (no `arg_alias_applied` line). The earlier "deterministic" drift
was measured 2/2 through the gateway+ewcp stack (more tools, ewcp
middlewares, different system-prompt context); determinism at temperature
0.2 is not guaranteed across stacks. The shim's coverage of the measured
`contents`/`mode` shape is proven at unit level; live-model exercise of the
rewrite remains sample-dependent [not re-measured at the gateway stack].

Other drift observed in the same run (out of scope, later pass): after
completing the task the model looped on `list_uploaded_files {}` (4× until
`LoopDetectionMiddleware` hard-stopped) and wrote an unsolicited
`example.txt` — arg-name compat does not fix agent-loop quality. The other
C03 blockers — clarification dodge, empty-response kill, ~5.5 t/s — are
unchanged. Arg names still uncovered (only file r/w tools are aliased per
scope): `mode: "w"` passes silently (pydantic ignores extras), and no alias
exists for `old_str`/`new_str` variants on `str_replace`, nor for any
non-file tool.
