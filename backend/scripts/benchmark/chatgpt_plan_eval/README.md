# chatgpt_plan_eval — ChatGPT-plan vs Ollama benchmark harness

Reproducible, DeerFlow-level comparison harness for the `chatgpt_plan_oauth`
provider. Runs identical tasks through the production model factory
(`deerflow.models.factory.create_chat_model`) on two arms and records
completion, correctness, tool-call success, inference calls, tokens, latency,
and failure causes into JSON.

Follows the backend benchmark rules: no network access unless `--live`,
credentials from named env vars / the OAuth credential store (never CLI
arguments), versioned task file + git SHA recorded, raw model payloads stay in
the ignored run directory.

## Arms

- `chatgpt_plan` — `use: deerflow.models.chatgpt_plan_provider:ChatGPTPlanChatModel`
  against `https://api.openai.com/v1/responses` (SIWC OAuth). Requires a
  completed local authorization (`python -m deerflow.models.chatgpt_plan_oauth login`).
- `ollama` — `use: langchain_ollama:ChatOllama` against `OLLAMA_BASE_URL`
  (default `http://localhost:11434`). Requires Ollama + a pulled model.

## Usage

```bash
cd backend
# 0) Prove the live path once — single real Responses request. This is the
#    "authorized inference request" the spec requires before integration can
#    be claimed working. Model-list alone does not count.
.venv/bin/python -m scripts.benchmark.chatgpt_plan_eval smoke --arm chatgpt_plan

# 1) Full comparison run (both arms must be reachable)
.venv/bin/python -m scripts.benchmark.chatgpt_plan_eval run \
    --tasks scripts/benchmark/chatgpt_plan_eval/tasks.json \
    --output-dir /tmp/chatgpt-plan-eval-$(git rev-parse --short HEAD)

# 2) One arm only
.venv/bin/python -m scripts.benchmark.chatgpt_plan_eval run --arm ollama \
    --tasks scripts/benchmark/chatgpt_plan_eval/tasks.json --output-dir /tmp/eval-ollama
```

## Metrics (per task + aggregate)

- `completed` — agent loop produced a non-empty final answer.
- `first_pass_correct` — final answer matched the case's `expect` regex on the
  first assistant turn (no corrective tool round-trip).
- `tool_calls` / `tool_call_success` — count + args-parse-and-execute ok.
- `inference_calls`, `input_tokens`, `output_tokens`, `latency_ms` per call and
  summed, `failure_cause` (provider error class + code when present),
  `estimated_cost` — `null` unless the task config prices the model; for
  `chatgpt_plan` the number is an *API-equivalent* estimate, never billed cost.

## Task file

`tasks.json` — array of `{id, prompt, expect_regex, use_tool}` cases. Synthetic
cases only; `use_tool: true` cases bind the built-in `add(a, b)` / `now()`
tools so tool-call correlation is exercised end-to-end.

## Isolation caveat

Provider feature differences (e.g. this route rejecting `temperature`) are
provider limitations, not model quality — the harness records `config` in each
result file so diffs are attributable.
