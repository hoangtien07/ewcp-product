# PA-A layout — EWCP product fork of deer-flow

Product repo = deep fork of `bytedance/deer-flow`, pinned at `v2.1.0`.
EWCP kernel stays in repo `enterprise-work-control-plane` (Q20); this fork
mounts it **in-process** — kernel + gateway + extension share one Python proc.

## Boxed layout (all EWCP code lives here — see `UPSTREAM_TOUCH.md`)

```
extensions/ewcp-packs/        # Python extension package (entry point: deerflow.extensions)
  ewcp_packs/
    __init__.py               # @extension(api="0.2.0") install()
    plugin.py                 # EwcpKernelService (ASGI in-process mount) + /api/ewcp/* router
frontend/src/ewcp/            # EWCP UI surfaces (task thread, manifest card, verify, UNVERIFIED badge)
docs/ewcp/                    # this doc + decision log
UPSTREAM_TOUCH.md             # ≤8 upstream files we may patch, each recorded
```

## Topology (governed fast-path)

```
user intent (chat)
  → POST /api/ewcp/tasks                       [extension router]
      intent ∈ registered outcomes  → kernel WorkRun (in-process ASGI mount)
                                      → governed: validators → HumanDecision → seal
      (exploratory lane stays upstream: lead_agent + artifact UNVERIFIED badge)
```

- Governed lane never crosses the upstream agent loop — LLM router output is a
  *choice in the registered set*, not free execution (invariant 6).
- Exploratory artifacts carry ProvenanceRecord + UNVERIFIED; they can never be
  promoted into a governed run (Q24 binding condition).
- Deploy: 3 procs (nginx :2026 → gateway :8001 → frontend :3000), sqlite.

## Local dev

```bash
make config                        # root: generates config.yaml + extensions_config.json
cd backend && uv sync
# kernel (separate repo, editable for dev):
uv pip install -e ../../enterprise-work-control-plane
# install extension into backend env:
uv run deerflow extensions install ../extensions/ewcp-packs --yes
# add to config.yaml plugins block (see config.example.yaml §plugins):
#   plugins:
#     - name: ewcp_packs
#       package: ewcp-packs
#       use: ewcp_packs:install
#       enabled: true
DEER_FLOW_AUTH_DISABLED=1 PYTHONPATH=. .venv/bin/uvicorn app.gateway.app:app --port 8001
curl localhost:8001/api/ewcp/_status
```

## Decision log

- 2026-10-04: base = v2.1.0 tag (latest stable; `2.x.x-dev` moving).
  Kernel NOT vendored — lazy import + health surface (Q20 provisional).
- File ask-back: upstream clarification form has no file field (verified
  spike) → workaround: ask-back renders in EWCP pane with an upload slot
  backed by `/api/uploads`; no upstream patch.
