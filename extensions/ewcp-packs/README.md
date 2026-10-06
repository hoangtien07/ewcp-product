# ewcp-packs — EWCP extension for the DeerFlow product fork

Registers EWCP governed-outcome surfaces inside the DeerFlow Gateway:

- `GET  /api/ewcp/health` — extension liveness + whether the `ewcp` kernel package is importable.
- `GET  /api/ewcp/outcomes` — registered outcome packs (kernel `OutcomeRegistry`).
- `POST /api/ewcp/tasks` — governed fast-path intake: intent ∈ outcomes → kernel `WorkRun` (in-process, same proc — PA-A topology).
- `GET  /api/ewcp/runs/{workrun_id}` — run status + pending `HumanDecision`s.
- `POST /api/ewcp/runs/{workrun_id}/decisions` — submit a decision (approve / reject / option / clarify / file-provided).
- `GET  /api/ewcp/runs/{workrun_id}/manifest` — sealed `VerificationManifest` once `VERIFIED`.
- `POST /api/ewcp/verify` — public re-verification (tamper check, byte-level).

Install (from repo root, after `make config`):

```bash
cd backend && uv run deerflow extensions install ../extensions/ewcp-packs --yes
```

Kernel dependency (Q20 — kernel repo riêng): the extension imports `ewcp.*`
lazily. Dev: `cd backend && uv pip install -e ../../enterprise-work-control-plane`.
Health reports `kernel_loaded` so a missing kernel never silently degrades.

## General lane (M-EA1 / spec 005)

`ewcp_packs.foundation_port` wraps the embedded `DeerFlowClient` into the
kernel's `FoundationPort` (`DeerFlowPortImpl`): 1 WorkRun = 1 deer-flow thread
(`ewcp-<workrun_id>`, user `ewcp-<tenant_id>`), 1 attempt = 1 `stream()` turn.
`plugin.py` passes `foundation_port=`/`foundation_cfg=` into `create_app`
only when the kernel signature accepts them — pre-merge kernels keep the
clarify fallback (AC1); `GET /api/ewcp/_status` reports `general_lane.wired`.

Deployment notes (operator, config.yaml is gitignored):

- `sandbox.use` → `ewcp.sandbox.provider.EwcpSandboxProvider` (kernel-side
  provider; absent until the kernel general lane merges — keep
  `local`/`aio` until then).
- Agent profile `ewcp-general` is materialized on demand by
  `ensure_agent_profile()` into `{base_dir}/agents/ewcp-general/` (shared
  layout so every `ewcp-<tenant>` user bucket resolves it).
- The lane tool set is fixed in code (`file:read`, `file:write`, `bash`;
  no MCP/subagents/clarification) — the embedded client subclass
  `EwcpDeerFlowClient` enforces it regardless of config.yaml tool_groups.

## Model governance (M-EA3 / invariant 7)

Every paid model call in the lane rides the kernel `ModelBroker`
(LLMGateway → BudgetGuard $-cap) under a per-WorkRun virtual key minted
as `vk-<wid>-<hex>` by the kernel executor. The virtual key reaches the
agent loop via `turn(virtual_key=…)` → `stream(ewcp_model_key=…)` →
`configurable.ewcp_model_key`, where `GovernedChatModel`
(`ewcp_packs.governed_model`) resolves it. The adapter is fail-closed:
missing key or missing broker raises `ModelGovernanceError` **before**
any provider wire call — an ungoverned turn produces no LLM traffic.

Operator config (`config.yaml`, gitignored — add manually):

```yaml
models:
  - name: ewcp-governed
    use: ewcp_packs.governed_model:GovernedChatModel
    model: gemini-3.1-flash-lite   # wire model the kernel client calls
```

- Unset turn `model` resolves `ewcp-governed` by default, so a governed
  run can never fall back to a direct-provider profile. An explicit
  model name still forwards (operator override); when the profile is
  absent `create_chat_model` raises — fail-closed.
- Kernel env: `EWCP_LLM_API_KEY` (required for the broker to wire),
  `EWCP_FOUNDATION_BUDGET_USD` (cap per WorkRun, default 2.0),
  `EWCP_FOUNDATION_MAX_OUTPUT_TOKENS` (default 8192),
  `EWCP_FOUNDATION_TOKEN_USD_PER_MTOK` (price map for `$`).
- `GET /api/ewcp/_status` → `general_lane.model_governance`
  reports `governed_profile` + `broker` booleans.
- Gemini 3.x `thoughtSignature` replay: the adapter stores the verbatim
  assistant payload under `AIMessage.additional_kwargs["ewcp_model_response"]`
  and re-emits it on the next call (plus honors the
  `__gemini_function_call_thought_signatures__` map produced by
  `ChatGoogleGenerativeAI`).
