"""Budget admission middleware tests (A3 Task 5 contract).

The kernel is the hard-cap authority for the product lane's paid model
calls: `BudgetAdmissionMiddleware` sits at Placement.MODEL_PHYSICAL
(fires once per physical provider call; retries re-enter it) and admits
BEFORE the call. These tests pin the behavior matrix offline — a
`_SpyHandler` stands in for the provider and must stay at zero calls on
every deny path.

Contract doc: docs/vnext/A3_BUDGET_ADMISSION.md. Kernel endpoints:
kernel PR on `devin/1791475418-budget-admission` (POST /budget/admissions,
…/settle, …/release, GET /budget/accounts/{id}).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from deerflow.models.request_admission import AdmissionError
from deerflow_extension_api import HostPolicySnapshot
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from ewcp_core.kernel_client import BudgetDenied
from ewcp_core.model_policy import (
    BudgetAdmissionMiddleware,
    KernelBudgetClient,
    ModelPolicyConfig,
    PolicyUnavailable,
)


class _SpyHandler:
    """Async handler standing in for the physical provider call."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.calls = 0
        self._result = result if result is not None else ModelResponse(result=[AIMessage(content="ok")])
        self._error = error

    async def __call__(self, request: Any) -> Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


class _SyncSpyHandler:
    def __init__(self, result: Any = None) -> None:
        self.calls = 0
        self._result = result if result is not None else ModelResponse(result=[AIMessage(content="ok")])

    def __call__(self, request: Any) -> Any:
        self.calls += 1
        return self._result


class _FakeBudgetClient:
    """In-memory BudgetAdmissionClient: records admits/settles/releases."""

    def __init__(
        self,
        *,
        admit_error: BaseException | None = None,
        settle_error: BaseException | None = None,
        release_error: BaseException | None = None,
    ) -> None:
        self.admit_error = admit_error
        self.settle_error = settle_error
        self.release_error = release_error
        self.admits: list[dict[str, Any]] = []
        self.settles: list[dict[str, Any]] = []
        self.releases: list[str] = []
        self._seq = 0

    async def admit(self, **kwargs: Any) -> dict[str, Any]:
        self.admits.append(kwargs)
        if self.admit_error is not None:
            raise self.admit_error
        self._seq += 1
        return {
            "admission_id": f"adm-{self._seq}",
            "execution_run_id": kwargs["execution_run_id"],
            "cap_usd": str(kwargs["cap_usd"]),
            "spent_usd": "0",
            "reserved_usd": str(kwargs["reserve_usd"]),
            "reserve_usd": str(kwargs["reserve_usd"]),
            "expires_at": 999.0,
            "idempotent_replay": False,
        }

    async def settle(self, admission_id: str, **kwargs: Any) -> dict[str, Any]:
        self.settles.append({"admission_id": admission_id, **kwargs})
        if self.settle_error is not None:
            raise self.settle_error
        return {"admission_id": admission_id, "settled": True, "charged_usd": str(kwargs["usd"]), "overshoot": False}

    async def release(self, admission_id: str) -> dict[str, Any]:
        self.releases.append(admission_id)
        if self.release_error is not None:
            raise self.release_error
        return {"admission_id": admission_id, "released": True}


def _config(**overrides: Any) -> ModelPolicyConfig:
    base = ModelPolicyConfig(cap_usd=Decimal("5.00"), usd_per_1k_tokens=Decimal("0.004"), max_output_tokens_per_call=1000)
    for key, value in overrides.items():
        object.__setattr__(base, key, value)
    return base


def _request(*, context: dict[str, Any] | None = None, content: str = "x" * 400) -> Any:
    """Minimal ModelRequest stand-in: the middleware reads `.messages`,
    `.runtime.context`, `.model` and `.model_settings`."""
    return SimpleNamespace(
        messages=[HumanMessage(content=content)],
        runtime=SimpleNamespace(context=context or {}),
        model=SimpleNamespace(),
        model_settings={},
    )


def _middleware(
    budget: Any,
    config: ModelPolicyConfig | None = None,
    policy: HostPolicySnapshot | None = None,
) -> BudgetAdmissionMiddleware:
    return BudgetAdmissionMiddleware(budget, config or _config(), policy or HostPolicySnapshot())


GOVERNED_CTX = {"kernel": {"workrun_id": "wr-77"}, "run_id": "run-9", "thread_id": "t-1"}
GENERAL_CTX = {"run_id": "run-9", "thread_id": "t-1"}


def _used_response(input_tokens: int = 120, output_tokens: int = 30) -> ModelResponse:
    total = input_tokens + output_tokens
    return ModelResponse(result=[AIMessage(content="ok", usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total})])


# -- governed mode: fail-closed ----------------------------------------------


@pytest.mark.asyncio
async def test_governed_deny_before_call_spy_never_invoked() -> None:
    """402 at admission -> AdmissionError; the provider handler is never
    entered (the cap's purpose is that no call happens once money is gone)."""
    budget = _FakeBudgetClient(admit_error=BudgetDenied({"error": "budget_exceeded", "cap_usd": "0.05"}))
    spy = _SpyHandler()

    with pytest.raises(AdmissionError):
        await _middleware(budget).awrap_model_call(_request(context=GOVERNED_CTX), spy)

    assert spy.calls == 0


@pytest.mark.asyncio
async def test_governed_policy_unavailable_failclosed() -> None:
    """Governed run + unreachable/missing budget policy -> deny before the
    call (fail-closed; a governed run without accounting must not spend)."""
    budget = _FakeBudgetClient(admit_error=PolicyUnavailable("kernel unreachable"))
    spy = _SpyHandler()

    with pytest.raises(AdmissionError):
        await _middleware(budget).awrap_model_call(_request(context=GOVERNED_CTX), spy)

    assert spy.calls == 0


@pytest.mark.asyncio
async def test_governed_admit_settle_attributes_to_workrun() -> None:
    """Happy path: admit under the kernel workrun_id (the governed identity),
    settle the metered charge against the issued admission."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=_used_response(input_tokens=200, output_tokens=50))

    result = await _middleware(budget).awrap_model_call(_request(context=GOVERNED_CTX), spy)

    assert spy.calls == 1
    assert result is spy._result
    assert len(budget.admits) == 1
    admit = budget.admits[0]
    assert admit["execution_run_id"] == "wr-77"  # governed identity, not the product run id
    assert admit["cap_usd"] == Decimal("5.00")
    assert admit["reserve_usd"] > 0
    assert len(budget.settles) == 1
    settle = budget.settles[0]
    assert settle["admission_id"] == "adm-1"
    assert settle["tokens"] == 250
    assert settle["usd"] == Decimal("250") * Decimal("0.004") / 1000
    assert budget.releases == []


@pytest.mark.asyncio
async def test_governed_handler_failure_releases_reservation() -> None:
    """A provider failure after admission frees the reservation — else every
    retry would shrink the remaining budget forever."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(error=RuntimeError("provider exploded"))

    with pytest.raises(RuntimeError, match="provider exploded"):
        await _middleware(budget).awrap_model_call(_request(context=GOVERNED_CTX), spy)

    assert budget.releases == ["adm-1"]
    assert budget.settles == []


@pytest.mark.asyncio
async def test_governed_settle_failure_is_failclosed() -> None:
    """Settlement failing after a charged call means unaccounted spend —
    for governed runs that surfaces as an admission error, not a silent pass."""
    budget = _FakeBudgetClient(settle_error=PolicyUnavailable("kernel gone"))
    spy = _SpyHandler(result=_used_response())

    with pytest.raises(AdmissionError):
        await _middleware(budget).awrap_model_call(_request(context=GOVERNED_CTX), spy)


@pytest.mark.asyncio
async def test_missing_usage_settles_the_reserved_amount() -> None:
    """Providers that omit usage still cost money — the reservation is the
    conservative charge (fail-closed accounting, never zero)."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=ModelResponse(result=[AIMessage(content="no usage here")]))

    await _middleware(budget).awrap_model_call(_request(context=GOVERNED_CTX), spy)

    assert len(budget.settles) == 1
    settle = budget.settles[0]
    assert settle["usd"] == budget.admits[0]["reserve_usd"]
    assert settle["tokens"] > 0


# -- general mode: configurable when policy unavailable ------------------------


@pytest.mark.asyncio
async def test_general_admits_under_run_id() -> None:
    """General runs (no kernel.workrun_id) admit under the product run id —
    the kernel pins the cap on that account the same way."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=_used_response())

    await _middleware(budget).awrap_model_call(_request(context=GENERAL_CTX), spy)

    assert budget.admits[0]["execution_run_id"] == "run-9"
    assert budget.settles[0]["admission_id"] == "adm-1"


@pytest.mark.asyncio
async def test_general_cap_denied_also_blocks() -> None:
    """A reached cap denies general runs too — 'configurable' only covers
    policy-unavailable, never a real 402."""
    budget = _FakeBudgetClient(admit_error=BudgetDenied({"error": "budget_exceeded"}))
    spy = _SpyHandler()

    with pytest.raises(AdmissionError):
        await _middleware(budget).awrap_model_call(_request(context=GENERAL_CTX), spy)

    assert spy.calls == 0


@pytest.mark.asyncio
async def test_general_unavailable_allow_proceeds() -> None:
    budget = _FakeBudgetClient(admit_error=PolicyUnavailable("kernel unreachable"))
    spy = _SpyHandler(result=_used_response())
    middleware = _middleware(budget, _config(general_on_policy_unavailable="allow"))

    result = await middleware.awrap_model_call(_request(context=GENERAL_CTX), spy)

    assert result is spy._result
    assert spy.calls == 1
    assert budget.settles == []


@pytest.mark.asyncio
async def test_general_unavailable_deny_blocks() -> None:
    budget = _FakeBudgetClient(admit_error=PolicyUnavailable("kernel unreachable"))
    spy = _SpyHandler()

    with pytest.raises(AdmissionError):
        await _middleware(budget, _config(general_on_policy_unavailable="deny")).awrap_model_call(_request(context=GENERAL_CTX), spy)

    assert spy.calls == 0


@pytest.mark.asyncio
async def test_general_unavailable_local_falls_back_to_host_token_budget() -> None:
    """'local' = the host's declared token bounds still apply when the kernel
    is unreachable — never treated as usd accounting."""
    budget = _FakeBudgetClient(admit_error=PolicyUnavailable("kernel unreachable"))
    policy = HostPolicySnapshot(token_budget_enabled=True, max_total_tokens=1000, budget_hard_fraction=1.0)
    spy = _SpyHandler(result=_used_response())
    middleware = _middleware(budget, _config(general_on_policy_unavailable="local"), policy)

    # est input (400 chars // 2 = 200) + max_output 1000 = 1200 > cap 1000
    with pytest.raises(AdmissionError):
        await middleware.awrap_model_call(_request(context=GENERAL_CTX, content="x" * 400), spy)
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_general_settle_failure_does_not_break_the_run() -> None:
    """A settled-side outage must not eat a paid-for general response —
    it is logged and the response is returned (documented audit gap)."""
    budget = _FakeBudgetClient(settle_error=PolicyUnavailable("kernel gone"))
    spy = _SpyHandler(result=_used_response())
    middleware = _middleware(budget, _config(general_on_policy_unavailable="allow"))

    result = await middleware.awrap_model_call(_request(context=GENERAL_CTX), spy)

    assert result is spy._result


# -- no USD cap configured -> host token gate only ----------------------------


@pytest.mark.asyncio
async def test_no_cap_configured_skips_admission() -> None:
    """budget.cap_usd unset -> USD admission off; no kernel call at all."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=_used_response())

    await _middleware(budget, _config(cap_usd=None)).awrap_model_call(_request(context=GOVERNED_CTX), spy)

    assert spy.calls == 1
    assert budget.admits == []


@pytest.mark.asyncio
async def test_local_token_budget_accumulates_across_calls() -> None:
    """The local gate counts ACTUAL usage per run — a second physical call
    that would cross the host cap is denied even though the first fit."""
    budget = _FakeBudgetClient()
    policy = HostPolicySnapshot(token_budget_enabled=True, max_total_tokens=399, budget_hard_fraction=1.0)
    middleware = _middleware(budget, _config(cap_usd=None, max_output_tokens_per_call=50), policy)

    # Call 1: est 200+50=250 <= 399, actual 150 recorded.
    spy = _SpyHandler(result=_used_response(input_tokens=120, output_tokens=30))
    await middleware.awrap_model_call(_request(context=GENERAL_CTX, content="x" * 400), spy)
    # Call 2: 150 + 250 = 400 > 399 -> deny.
    with pytest.raises(AdmissionError):
        await middleware.awrap_model_call(_request(context=GENERAL_CTX, content="x" * 400), spy)

    assert spy.calls == 1


@pytest.mark.asyncio
async def test_local_gate_disabled_without_host_budget() -> None:
    """No host token budget -> no local gate (the host enforces nothing,
    the policy mirrors that honestly rather than inventing a limit)."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=_used_response())

    result = await _middleware(budget, _config(cap_usd=None), HostPolicySnapshot()).awrap_model_call(_request(context=GENERAL_CTX), spy)

    assert result is spy._result


# -- reservation sizing + idempotency -----------------------------------------


@pytest.mark.asyncio
async def test_reserve_is_estimated_worst_case() -> None:
    """reserve_usd = (est_input chars//2 + max_output) * price/1k — same
    estimator the kernel gateway uses internally."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=_used_response())
    # 400 chars -> 200 est + 1000 max_out = 1200 * 0.004/1000 = 0.0048
    await _middleware(budget).awrap_model_call(_request(context=GENERAL_CTX, content="x" * 400), spy)

    assert budget.admits[0]["reserve_usd"] == Decimal("1200") * Decimal("0.004") / 1000


@pytest.mark.asyncio
async def test_each_physical_call_gets_a_distinct_idempotency_key() -> None:
    """Distinct physical calls must mint distinct admissions — the key exists
    to dedupe client RETRIES of the same call, not to collapse calls."""
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=_used_response())
    middleware = _middleware(budget)
    req = _request(context=GENERAL_CTX)

    await middleware.awrap_model_call(req, spy)
    await middleware.awrap_model_call(req, spy)

    keys = [a["idempotency_key"] for a in budget.admits]
    assert len(keys) == 2 and keys[0] != keys[1]
    assert all(k.startswith("budget:run-9:") for k in keys)


@pytest.mark.asyncio
async def test_tenant_id_passes_through_to_admission() -> None:
    budget = _FakeBudgetClient()
    spy = _SpyHandler(result=_used_response())
    middleware = _middleware(budget, _config(tenant_id="acme"))

    await middleware.awrap_model_call(_request(context=GENERAL_CTX), spy)

    assert budget.admits[0]["tenant_id"] == "acme"


# -- sync path -----------------------------------------------------------------


def test_sync_governed_call_fails_closed() -> None:
    """The sync model-call path cannot reach the kernel — a governed run
    there denies rather than spending unaccounted."""
    spy = _SyncSpyHandler()

    with pytest.raises(AdmissionError):
        _middleware(_FakeBudgetClient()).wrap_model_call(_request(context=GOVERNED_CTX), spy)

    assert spy.calls == 0


def test_sync_general_call_uses_local_gate() -> None:
    spy = _SyncSpyHandler()

    result = _middleware(_FakeBudgetClient()).wrap_model_call(_request(context=GENERAL_CTX), spy)

    assert result is spy._result
    assert spy.calls == 1


# -- config resolution ----------------------------------------------------------


def test_config_resolve_reads_budget_block() -> None:
    cfg = ModelPolicyConfig.resolve({"budget": {"cap_usd": "7.5", "tenant_id": "acme", "usd_per_1k_tokens": "0.01", "max_output_tokens_per_call": 2048, "general_on_policy_unavailable": "deny"}})

    assert cfg.cap_usd == Decimal("7.5")
    assert cfg.tenant_id == "acme"
    assert cfg.usd_per_1k_tokens == Decimal("0.01")
    assert cfg.max_output_tokens_per_call == 2048
    assert cfg.general_on_policy_unavailable == "deny"


def test_config_resolve_defaults() -> None:
    cfg = ModelPolicyConfig.resolve({})

    assert cfg.cap_usd is None
    assert cfg.tenant_id is None
    assert cfg.general_on_policy_unavailable == "local"


def test_config_resolve_rejects_unknown_unavailable_mode() -> None:
    with pytest.raises(ValueError, match="general_on_policy_unavailable"):
        ModelPolicyConfig.resolve({"budget": {"general_on_policy_unavailable": "yolo"}})


# -- KernelBudgetClient adapter ---------------------------------------------------


@pytest.mark.asyncio
async def test_kernel_budget_client_unconfigured_raises_policy_unavailable() -> None:
    adapter = KernelBudgetClient(lambda: None)

    with pytest.raises(PolicyUnavailable):
        await adapter.admit(tenant_id=None, execution_run_id="r", cap_usd=Decimal("1"), reserve_usd=Decimal("0.1"), idempotency_key="k")
