# provider_eval — provider-neutral benchmark harness

Same task set, same metrics, provider plugged via config (env vars) only —
adapted from `chatgpt_plan_eval` so a new arm runs the moment its credentials
exist. Built for A7A readiness so **Workers AI can be benchmarked instantly
when a scoped `CF_API_TOKEN` lands**; `ollama` is the same-task control arm.

## Arms

| arm | provider | config (env only — never CLI, never committed) |
|-----|----------|-------------------------------------------------|
| `ollama` | local Ollama OpenAI-compatible | `OLLAMA_BASE_URL` (default `http://localhost:11434`), `OLLAMA_MODEL` (default `qwen2.5:0.5b`) |
| `workers_ai` | Cloudflare Workers AI (`@cf/openai/gpt-oss-120b`) | `CF_ACCOUNT_ID`, `CF_API_TOKEN` (scoped Workers AI token), `CF_MODEL` (optional override) |

Workers AI endpoint: `https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/ai/v1` (OpenAI-compatible → `langchain_openai.ChatOpenAI`).

## Commands (run from `backend/`)

```bash
# authorized-call smoke per arm
uv run python -m scripts.benchmark.provider_eval smoke --arm ollama
uv run python -m scripts.benchmark.provider_eval smoke --arm workers_ai   # needs CF_* env

# full task set, serial
uv run python -m scripts.benchmark.provider_eval run \
  --arm ollama --tasks scripts/benchmark/provider_eval/tasks.json \
  --output-dir /tmp/provider_eval/ollama

# the 2-parallel probe (bounded concurrency + wall time)
uv run python -m scripts.benchmark.provider_eval run \
  --arm workers_ai --parallel 2 \
  --tasks scripts/benchmark/provider_eval/tasks.json \
  --output-dir /tmp/provider_eval/workers_ai
```

`run` with no `--arm` runs **all** arms (workers_ai skips itself only if you
forget CF_* — it exits with instructions rather than a silent skip).

## Metrics (identical shape to `chatgpt_plan_eval`)

Per task: `completed`, `correct`, `first_pass_correct`, `tool_calls`,
`tool_call_success`, `inference_calls`, `input_tokens`, `output_tokens`,
`latency_ms` (+ per-call), `failure_cause`. Summary adds
`parallel_workers`, `wall_ms`, `wall_ms_per_task` for the concurrency probe.
Results land in `results.json` per output dir — keep provider payloads local.

## Offline tests

```bash
cd backend && uv run pytest tests/test_bench_provider_eval.py -q
```

Scripted `GenericFakeChatModel` — no network, no credentials.
