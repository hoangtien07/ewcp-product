# A3 — Budget Admission Contract (Task 5)

**Status:** implemented (product branch `devin/1791474552-a3-budget`, kernel PR
`hoangtien07/enterprise-work-control-plane` `devin/1791475418-budget-admission`).

**Scope:** the USD hard cap for the product lane's paid model calls — general
runs *and* governed runs. The kernel is the sole cap authority; this document is
the wire + enforcement contract. A product-side counter reconciled later is
audit only, never the cap.

## Why

Invariant 7 (`LLM qua gateway … + budget cap`) covers kernel-native WorkRun
calls via the in-process `ModelBroker`. Under Option A+ the kernel is a remote
capability server, so product-side agent runs (DeerFlow lead/subagent model
calls) had **no** cap at all. Task 5 closes that with kernel-owned admission:
reserve BEFORE the provider is invoked, settle the metered charge after.

## Wire surface (kernel `main`, `src/ewcp/api/app.py`)

Money round-trips as Decimal strings — never floats.

| Route | Semantics |
|---|---|
| `POST /budget/admissions` | `{execution_run_id, cap_usd, reserve_usd, tenant_id?}` → `201` admission `{admission_id, cap_usd, spent_usd, reserved_usd, expires_at}` · `200` + `Idempotent-Replay: true` on `Idempotency-Key` replay · **`402` `{error:"budget_exceeded", cap_usd, spent_usd, reserved_usd}` — deny BEFORE the call** · `409` `{error:"cap_mismatch"}` (declared cap ≠ pinned cap) · `401`/`403`/`422` auth+shape |
| `POST /budget/admissions/{id}/settle` | `{tokens, usd, model}` → `200` `{settled, charged_usd, spent_usd, overshoot}`; idempotent replay; `404` unknown; `409` released |
| `POST /budget/admissions/{id}/release` | close a pending reservation without charge (admitted call failed); idempotent |
| `GET /budget/accounts/{execution_run_id}` | audit view `{cap_usd, spent_usd, reserved_usd}` |

- `cap_usd` is **pinned at the account's first admission** and immutable — the
  product must declare a stable cap per `execution_run_id`.
- Pending reservations expire lazily after `budget_admission_ttl_s` (kernel
  `create_app` param, default 300 s) so crashed calls can't hold a cap forever;
  a late settle still records the charge.
- Settled spend lands in the kernel spend ledger keyed by `execution_run_id`
  (`workrun_id` column; `virtual_key = "adm:<id>"`) — the same attribution store
  `GET /workruns/{id}.spend` reads.
- `Idempotency-Key` (≤255 chars) dedupes client retries of the *same* physical
  call: distinct calls mint distinct keys.

## Product side (`ewcp-core` extension)

`BudgetAdmissionMiddleware` at `Placement.MODEL_PHYSICAL` — inner of retry and
error handling, so it fires **once per physical provider call** and
`LLMErrorHandlingMiddleware` retries re-enter it (each retried call re-admits).

```
est_input  = Σ len(str(msg.content)) // 2        # kernel's ~2 chars/token
reserve    = (est_input + max_output_tokens_per_call) * usd_per_1k / 1000
admit → 402          : raise AdmissionError (provider never invoked)
admit → unavailable  : governed → AdmissionError (fail-closed)
                     : general  → budget.general_on_policy_unavailable
handler raises       : release(admission_id), propagate
success              : settle(tokens=usage.total, usd=usage.total*price/1k)
                       — missing usage settles the RESERVED amount
```

`AdmissionError` is non-retriable in the host's error middleware
(`_classify_error` → `"admission"`), so a deny surfaces as the run's fallback
message — not a retry loop, not a crash.

**Identity**: `runtime.context["kernel"]["workrun_id"]` (stamped by the Task-2
launcher) → governed, `execution_run_id = workrun_id`. Otherwise general,
`execution_run_id = context["run_id"]`.

**`intercepting=True`** (`MiddlewarePlacement`): contributions default to the
host's fail-open `IsolatedMiddleware`, which swallows exceptions into
diagnostics — a deny raised that way would *still call the provider*. The
intercepting flag inserts the raw middleware so deny propagates. This is the
host seam the isolation module docstring anticipates ("must opt out of this
wrapper explicitly").

### Config (`config.yaml` → plugin `ewcp_core.config.budget`)

```yaml
budget:
  cap_usd: "5.00"               # per execution_run_id; unset = USD gate off
  tenant_id: null               # dev-mode kernel only; keyed kernels use the API key
  usd_per_1k_tokens: "0.004"    # flat worst-case price for reserve + settle
  max_output_tokens_per_call: 4096
  general_on_policy_unavailable: local   # local | allow | deny
```

- Governed policy-unavailable is **always deny** — not configurable.
- `local` consults `LocalTokenBudget`: the host's declared token bounds
  (`HostPolicySnapshot.max_{input,output,total}_tokens`, `budget_hard_fraction`),
  counting actual usage per run. It gates TOKENS only — explicitly not USD
  accounting.
- Sync `wrap_model_call` cannot reach the kernel: governed + cap configured →
  deny; general → local gate only.

## Enforcement granularity — honest limits

- **Unit:** one physical provider call = one admission. A logical model step's
  retries each reserve independently; a denied retry still produces the fallback
  (non-retriable).
- **Hard before-call check:** `spent + open_reservations + reserve > cap → 402`.
  No partial or queued admission.
- **Bounded overshoot:** providers expose no hard cap — an in-flight admitted
  call can land a bill above the cap. Overshoot is bounded by `reserve_usd` of
  the in-flight admissions at the instant the cap is crossed; settle records the
  real charge and flags `overshoot`; all subsequent admissions 402.
- **Missing usage:** settle charges the reservation (conservative; kernel Q8
  analog) — a provider that omits usage is billed at its own worst-case.
- **Settle-failure gap:** a governed call whose settle fails raises
  `AdmissionError` post-call (spend unaccounted = fail closed). A general call
  logs the audit gap and returns the response — recorded exception, not a
  silent zero.
- **Token-price fidelity:** the flat `usd_per_1k_tokens` is a worst-case
  approximation — deployments serving mixed-price models must set it to the
  most expensive model's rate [giả định: single flat price mirrors the kernel
  gateway's own convention].

## Rulings recorded for this contract

1. `docs/strategy/COST_ATTRIBUTION_PLAN.md` cited by the dispatch does not
   exist on kernel `main` — attribution reuses the spend ledger's existing
   `workrun_id` column carrying `execution_run_id`; no second store.
2. Upstream seam added (`MiddlewarePlacement.intercepting`): deny decisions
   require exception propagation that the v1 isolation wrapper cannot express.
   UPSTREAM_TOUCH — deliberate, additive (default stays fail-open).
3. `tenant_id` in the admit body exists for dev-mode kernels only; keyed
   deployments resolve tenant from `X-Ewcp-Api-Key` (declared mismatch → 403).
