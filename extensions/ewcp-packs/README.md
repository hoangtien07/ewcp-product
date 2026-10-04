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
