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
