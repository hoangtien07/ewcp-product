# P0 — GP-01 Secure Execution: Real Isolated Sandbox for General/Pane Runs (measured)

**Date:** 2026-10-09 · **Directive:** "Do not enable host bash for untrusted multi-user runs. Prove real isolated sandbox execution and artifact existence."
**Stack:** ewcp-product `product/vnext` (branch `devin/1791557091-p0-sandbox`) + enterprise-work-control-plane kernel.
**Lane under test:** `POST /api/ewcp/runs` `task_mode=general` (the pane lane) — the same runtime the governed lane reaches via `RunLauncher.launch` (`api_routes.py:297-306`).

## Verdict

`LocalSandboxProvider` is **not** a security boundary (self-declared in `sandbox/security.py` and `tools.py`) — with `allow_host_bash: false` it gives the agent *no* execution tool at all, which is exactly what produced the fabricated deliverable in `GP01_EVAL.md` F2. The cheapest real isolation on a single Docker host is **`AioSandboxProvider` (local Docker backend) + `network.mode: isolated`**: one all-in-one-sandbox container per (user, thread), thread user-data bind-mounted at `/mnt/user-data/{workspace,uploads,outputs}`, all egress denied (internal-only network + netproxy sidecar that 403s every CONNECT). Host bash stays OFF — the `bash` tool executes *inside* the container.

## Backend inventory (what actually exists in `backend/packages/harness/deerflow/`)

| Backend | Class | Isolation boundary | Filesystem | Network | Resource limits | Setup cost on this box | Viable? |
|---|---|---|---|---|---|---|---|
| Local sandbox | `deerflow.sandbox.local` `LocalSandboxProvider` | **None** — host process; own docstring says "not a security boundary" | Host FS (thread dirs only by convention) | `allow_host_bash` gates *tool availability*, not network | None | Zero (default) | ❌ Not isolation. With host bash off the agent has no exec tool → fabrication (F2). |
| **AIO sandbox, local Docker** | `deerflow.community.aio_sandbox:AioSandboxProvider` | Docker container per (user,thread); image `…/all-in-one-sandbox:1.11.0` | Bind mounts limited to `.deer-flow/users/{u}/threads/{t}/` dirs + RO skills; no `docker.sock` | `network.mode: isolated` → `--internal` bridge (isolated gateways, no default route) + netproxy sidecar denying all | Container limits via Docker; sandbox API manages them | `docker pull` 3.6 GB; needs Docker ≥ 28 (box has 29.7.2) | ✅ **Chosen.** Measured below. |
| AIO sandbox, remote / provisioner | same provider, other backend | Remote sandbox service or K8s pods | Same contract | Same modes | Managed | Needs provisioner service (`:8002`) + k3s | ❌ k3s absent on box; heavier for no gain vs local Docker. |
| BoxLite | `community/boxlite:BoxLiteProvider` | OCI micro-VM (KVM) per (user,thread) | virtiofs-style mounts | Own netns | VM boundaries | boxlite binary + `/dev/kvm` (exists) | ⚠️ Viable but heavier; VM pull + boxlite runtime not installed. AIO cheaper. |
| E2B | `community/e2b_sandbox:E2BSandboxProvider` | Cloud micro-VM (firecracker, e2b infra) | e2b FS | E2B-managed | e2b quotas | `$E2B_API_KEY` (paid SaaS) | ❌ External SaaS; violates data-residency posture for customer runs. |
| Tenki | `community/tenki:TenkiSandboxProvider` | Cloud microVM (tenki.cloud) | tenki FS | Managed | tenki quotas | `tenki` SDK + API key | ❌ External SaaS, same residency issue. |
| OpenSandbox | `community/opensandbox:OpenSandboxProvider` | Remote OpenSandbox server | Server-side | Server policy | Server policy | Deploy/maintain an opensandbox server | ❌ No server on box; extra infra. |
| Provisioner/K8s mode | harness provisioner (`:8002`) | K8s pod | PVC | NetworkPolicy | K8s limits | k3s + provisioner | ❌ No K8s on box; highest setup cost. |

## Wiring applied (local-only; `config.yaml` is gitignored)

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
  allow_host_bash: false          # stays OFF — execution goes through the container
  network:
    mode: isolated                # all egress denied (internal net + deny-all netproxy)
```

Provider binds thread dirs `.deer-flow/users/{user}/threads/{tid}/user-data/{workspace,uploads,outputs}` into the container at `/mnt/user-data/{workspace,uploads,outputs}` (`aio_sandbox_provider._get_thread_mounts`). The host-side artifacts API (`GET /api/threads/{tid}/artifacts/mnt/user-data/...`) reads those same dirs, so sandbox-produced files are retrievable with zero bridging code.

Egress-policy interop (`ewcp_core.egress_policy.check_sandbox_net`): AIO + `mode: isolated` → verdict `isolated`, which satisfies `local_only` and `restricted` tenant policies. `allow_host_bash: false` removes the unenforceable-host-bash verdict path entirely.

## Measured evidence

Env: Docker server 29.7.2, cgroup v2; gateway `DEER_FLOW_AUTH_DISABLED=1`, `EWCP_KERNEL_URL=http://127.0.0.1:8080`; kernel on :8080 with tenant `demo`.

### (a) General-lane file task produces a real artifact inside the sandbox

Fixture `sales_messy.csv` (482 B: mixed date formats, impossible `2025-13-45`, dup `ORD-002`, pasted `TOTAL` row, `$x,xxx.xx` amounts, missing reps). Command: `POST /api/ewcp/runs` multipart `intent`+`task_mode=general`+`files` → poll `GET /api/ewcp/runs/{er}?refresh=1`.

- `er_1203ba8fa9bb4cc481bcc8c89e8e236b`, thread `3804ed75-8fd6-46e8-ba86-a45880f442d2`, run `7ea7794b-…` → **`completed` in ~50 s** (one retry after a free-tier 429 on `gemini-3.5-flash-lite`; first attempt `er_9057adba` `failed`, counted honestly).
- Tools ran **inside the container** — run events show `bash` calls like `export DEERFLOW_USER_ID=default; cd /mnt/user-data/workspace; python3 -c "import openpyxl…"`; `thread.values.sandbox.sandbox_id = e62927749bb74fdf` = running container `deer-flow-sandbox-e62927749bb74fdf`.
- Inside the sandbox (`docker exec … ls -la`):

```
/mnt/user-data/outputs/: September_Orders_Cleaned.xlsx (6182 B), cleaning_summary.md (4439 B)
/mnt/user-data/uploads/: sales_messy.csv (482 B)
/mnt/user-data/workspace/: generate_excel.py (7121 B)
```

- Artifact retrieval via API (host side):

```
GET /api/threads/3804ed75…/artifacts/mnt/user-data/outputs/September_Orders_Cleaned.xlsx → 200, 6182 B
GET /api/threads/3804ed75…/artifacts/mnt/user-data/outputs/cleaning_summary.md           → 200, 4439 B
```

Byte counts match container listing exactly. The workbook is real (F2 fabrication class closed: file exists + retrievable).

### (b) Sandbox-escape probes fail (measured, inside the live container)

Container topology (all measured via `docker inspect`/`docker exec`):

- `deer-flow-sandbox-e62927749bb74fdf` attached **only** to `deer-flow-sandbox-net-…` with `internal=true`, `gateway_mode_ipv4/ipv6=isolated`, `Gateway:""` — no default route.
- `deer-flow-netproxy-…` sidecar published on `127.0.0.1:8081` is the sandbox's only egress path (`HTTP_PROXY/HTTPS_PROXY/all_proxy=http://deer-flow-netproxy-…:3128` set in container env).
- Mounts = thread `user-data/{workspace,uploads,outputs}` (RW) + `acp-workspace`, `skills/*`, `integrations/lark-cli/*` (RO/RW, no host root, **no `/var/run/docker.sock`**).

Probes and recorded denials:

| Probe | Result (measured) |
|---|---|
| `curl https://raw.githubusercontent.com/…` by the agent's `bash` tool (run `4187630c-…`, intent: download remote CSV) | `Error: <urlopen error Tunnel connection failed: 403 Forbidden>` — netproxy denied CONNECT |
| Same, agent retry with proxies disabled (`ProxyHandler({})`) | `Failed: <urlopen error [Errno -3] Temporary failure in name resolution>` — no DNS/route on the internal net |
| `curl --noproxy "*" https://example.com` inside container | `(28) Resolving timed out` — direct egress impossible |
| Read host `/etc/shadow` | Container's own file: sha `9aa12d72…` vs host `77d2718e…`; users `…gem` (no `ubuntu`/`devin`) — host file unreachable by construction |
| `echo CANARY > /tmp/host_escape_canary.txt` (then check host) | Written inside container FS; `ls /tmp/host_escape_canary.txt` on host → **No such file** |
| `echo CANARY > /mnt/outside_mount.txt` (outside any bind mount) | Succeeds in container; absent on host |
| `ls /var/run/docker.sock` inside container | `No such file or directory` — no Docker-API breakout |

Honest framing: the model refused intents phrased as security probes ("I cannot fulfill this request…") twice — recorded as model-level guardrail, not isolation. The boundary denials above are measured at container level (`docker exec`/`docker inspect`) plus agent-tool level (the egress 403/DNS-failure tool results in run `4187630c-…`'s events).

### (c) Run record honest about backend

- `thread.values.sandbox.sandbox_id = e62927749bb74fdf` → resolves to the running `deer-flow-sandbox-e62927749bb74fdf` container (`docker ps`). The record binds to a concrete container, not a label.
- Provider identity is logged: `deerflow.community.aio_sandbox.local_backend` acquire/release lines per run (`Releasing sandbox e62927749bb74fdf`).
- Tool-call arguments in run events embed the sandbox mount contract (`/mnt/user-data/…`), so an auditor can verify from the record alone that execution happened in a mounted sandbox, not on the host FS.

### Governed lane

`/api/ewcp/runs` `task_mode=governed` goes kernel-first (`create_task`/`run_outcome` → `workrun_id`), then calls the **same** `RunLauncher.launch` → same thread → same agent runtime → same `SandboxMiddleware` → tools execute inside the same AIO container (`api_routes.py:236-306`). Measured: a bare non-domain intent returns kernel 422 clarify ("…không thuộc các nghiệp vụ …") — contract works; a governed run routed to a real outcome type executes on this identical seam. Governed tool-execution inside the sandbox is therefore inherited by construction, not a separate backend.

## Remaining gaps (recorded honestly)

1. `execution_run` record view lacks an explicit `sandbox.backend`/`sandbox.sandbox_id` field — attribution today is via `thread.values.sandbox.sandbox_id` + provider logs + tool-arg paths. Worth a follow-up field for auditors.
2. `isolated` mode also blocks package installs inside the sandbox (pip/apt egress denied). Image carries python3/pandas/openpyxl out of the box; anything beyond that needs `mode: allowlist` + explicit approver, or image-side pre-baking.
3. Warm-pool containers persist between runs (`3 running sandbox container(s)` reconciliation log) — clean-up policy/TTL to be reviewed before multi-user.
4. Free-tier `gemini-3.5-flash-lite` 15-RPM quota caused one failed attempt; unrelated to sandboxing but visible in run history.
