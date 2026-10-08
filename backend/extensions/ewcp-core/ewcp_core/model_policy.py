"""Budget admission for the product lane's paid model calls (A3 Task 5).

Contract: `docs/vnext/A3_BUDGET_ADMISSION.md`. The EWCP kernel is the
hard-cap authority — `BudgetAdmissionMiddleware` sits at
`Placement.MODEL_PHYSICAL` (fires once per physical provider call;
retries re-enter it) and admits BEFORE the call:

    admit (precheck + worst-case reserve) -> handler -> settle (metered)
                                                   -> release (failed call)

Identity: governed runs carry `runtime.context["kernel"]["workrun_id"]`
(stamped by the ExecutionRun launcher, Task 2) and admit under that id —
their budget account aggregates with the kernel-native WorkRun ledger.
General runs admit under `runtime.context["run_id"]`.

Deny semantics: a real 402 (cap reached) always denies — the handler is
never invoked. When the admission *policy itself* is unavailable the
behavior differs by mode: governed runs fail closed (`AdmissionError`);
general runs follow `budget.general_on_policy_unavailable`
(`local` default = host token bounds still apply; `allow`; `deny`).

`AdmissionError` (deerflow.models.request_admission) is structurally
non-retriable in LLMErrorHandlingMiddleware (`_classify_error` ->
"admission"), so a deny produces the user-facing fallback message rather
than a retry storm. The middleware is contributed with
`intercepting=True`: only then do its exceptions propagate past the
host's fail-open IsolatedMiddleware wrapper.

The synchronous model-call path cannot reach the kernel — a governed
call there fails closed; a general call consults only the local token
gate.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from deerflow.models.request_admission import AdmissionError
from deerflow_extension_api import HostPolicySnapshot
from langchain.agents.middleware import AgentMiddleware

from .kernel_client import BudgetDenied, KernelClient

logger = logging.getLogger(__name__)


class PolicyUnavailable(RuntimeError):
    """The admission policy could not be evaluated at all — kernel
    unreachable, unconfigured, or an unexpected admission error. Distinct
    from `BudgetDenied` (a real 402): denial is a verdict, unavailability
    is a missing verdict."""


class BudgetAdmissionClient(Protocol):
    """The admission surface the middleware needs — satisfied by
    `KernelBudgetClient` (live kernel) or a test fake."""

    async def admit(
        self,
        *,
        tenant_id: str | None,
        execution_run_id: str,
        cap_usd: Decimal,
        reserve_usd: Decimal,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    async def settle(self, admission_id: str, *, tokens: int, usd: Decimal, model: str) -> Mapping[str, Any]: ...

    async def release(self, admission_id: str) -> Mapping[str, Any]: ...


class KernelBudgetClient:
    """`BudgetAdmissionClient` over the lazily-started KernelClient.

    The service starts the httpx client at Gateway startup while
    middlewares are contributed at agent-build time, so the client is
    resolved per call through `client_getter` — a missing client is
    PolicyUnavailable, not an AttributeError.
    """

    def __init__(self, client_getter: Callable[[], KernelClient | None]) -> None:
        self._client_getter = client_getter

    def _client(self) -> KernelClient:
        client = self._client_getter()
        if client is None:
            raise PolicyUnavailable("ewcp kernel client is not configured")
        return client

    async def admit(
        self,
        *,
        tenant_id: str | None,
        execution_run_id: str,
        cap_usd: Decimal,
        reserve_usd: Decimal,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        result = await self._client().admit_budget(
            execution_run_id=execution_run_id,
            cap_usd=str(cap_usd),
            reserve_usd=str(reserve_usd),
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )
        return result.admission

    async def settle(self, admission_id: str, *, tokens: int, usd: Decimal, model: str) -> Mapping[str, Any]:
        return await self._client().settle_budget(admission_id, tokens=tokens, usd=str(usd), model=model)

    async def release(self, admission_id: str) -> Mapping[str, Any]:
        return await self._client().release_budget(admission_id)


@dataclass(frozen=True)
class ModelPolicyConfig:
    """Plugin `config.budget` block.

    `cap_usd` is the per-`execution_run_id` hard cap the kernel pins at
    the account's first admission — keep it stable per deployment (a later
    admission declaring a different cap gets 409 cap_mismatch). Unset ->
    USD admission is off entirely; only the local token gate applies.

    `usd_per_1k_tokens` prices reservations AND the settle charge — flat
    worst-case for every model the deployment serves (the kernel gateway
    uses the same single-price convention internally).

    `general_on_policy_unavailable`: `local` (default) applies the host's
    declared token bounds; `allow` passes the call unadmitted; `deny`
    fails closed. Governed runs ignore this knob: they always deny.
    """

    cap_usd: Decimal | None = None
    tenant_id: str | None = None
    usd_per_1k_tokens: Decimal = Decimal("0.004")
    max_output_tokens_per_call: int = 4096
    general_on_policy_unavailable: str = "local"

    @classmethod
    def resolve(cls, config: Mapping[str, Any] | None) -> ModelPolicyConfig:
        budget = (config or {}).get("budget") or {}
        if not isinstance(budget, Mapping):
            raise ValueError("plugin config `budget` must be a mapping")
        mode = str(budget.get("general_on_policy_unavailable", "local"))
        if mode not in {"local", "allow", "deny"}:
            raise ValueError(f"general_on_policy_unavailable must be one of local|allow|deny, got {mode!r}")
        cap = budget.get("cap_usd")
        return cls(
            cap_usd=Decimal(str(cap)) if cap is not None else None,
            tenant_id=str(budget["tenant_id"]) if budget.get("tenant_id") else None,
            usd_per_1k_tokens=Decimal(str(budget.get("usd_per_1k_tokens", "0.004"))),
            max_output_tokens_per_call=int(budget.get("max_output_tokens_per_call", 4096)),
            general_on_policy_unavailable=mode,
        )


def _identity(context: Mapping[str, Any]) -> tuple[str, bool]:
    """(execution_run_id, governed). Governed identity is the kernel
    workrun id stamped at launch; a general run attributes to its own
    run id."""
    kernel_ctx = context.get("kernel")
    workrun_id = kernel_ctx.get("workrun_id") if isinstance(kernel_ctx, Mapping) else None
    if workrun_id:
        return str(workrun_id), True
    run_id = context.get("run_id") or context.get("thread_id") or "unknown"
    return str(run_id), False


def _est_input_tokens(messages: object) -> int:
    """Worst-case input estimate — ~2 chars/token, mirroring the kernel
    gateway's estimator (`llm_gateway._est_prompt_tokens`): it
    over-estimates English prompts, which is the safe direction for a
    reservation."""
    total = 0
    for message in messages if isinstance(messages, (list, tuple)) else ():
        total += len(str(getattr(message, "content", "")))
    return max(1, total // 2)


def _total_tokens(response: Any) -> int | None:
    """Extract total usage from a ModelCallResult — ModelResponse.result
    is a message list; a bare AIMessage is also legal."""
    if response is None:
        return None
    messages = getattr(response, "result", None)
    if messages is None:
        messages = (response,)
    for message in reversed(list(messages)):
        usage = getattr(message, "usage_metadata", None)
        if usage:
            total = usage.get("total_tokens")
            if total is None:
                total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            if total:
                return int(total)
    return None


def _model_name(request: Any) -> str:
    model = getattr(request, "model", None)
    for attr in ("model_name", "model", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


class LocalTokenBudget:
    """Host-declared token bounds for calls without kernel admission.

    Enforces `HostPolicySnapshot` limits only — it counts TOKENS per
    execution_run_id, never USD: it is not the cost-cap authority (the
    plan's "no fallback counter" rule — a product-side usd counter
    reconciled later cannot be the hard cap; only the kernel's
    admit/settle ledger can).
    """

    def __init__(self, policy: HostPolicySnapshot) -> None:
        self._policy = policy
        self._spent: dict[str, int] = {}
        self._lock = threading.Lock()

    def check(self, execution_run_id: str, est_input_tokens: int, max_output_tokens: int) -> None:
        policy = self._policy
        if not policy.token_budget_enabled:
            return
        if policy.max_input_tokens is not None and est_input_tokens > policy.max_input_tokens:
            raise AdmissionError(f"estimated input {est_input_tokens} exceeds host max_input_tokens {policy.max_input_tokens}")
        if policy.max_output_tokens is not None and max_output_tokens > policy.max_output_tokens:
            raise AdmissionError(f"configured max_output_tokens_per_call {max_output_tokens} exceeds host max_output_tokens {policy.max_output_tokens}")
        if policy.max_total_tokens is not None:
            fraction = policy.budget_hard_fraction if policy.budget_hard_fraction is not None else 1.0
            cap = int(policy.max_total_tokens * fraction)
            with self._lock:
                spent = self._spent.get(execution_run_id, 0)
            if spent + est_input_tokens + max_output_tokens > cap:
                raise AdmissionError(f"run {execution_run_id} would exceed host token budget: {spent} spent + {est_input_tokens + max_output_tokens} est > {cap}")

    def record(self, execution_run_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        with self._lock:
            self._spent[execution_run_id] = self._spent.get(execution_run_id, 0) + tokens


class BudgetAdmissionMiddleware(AgentMiddleware):
    """Pre-call budget gate at Placement.MODEL_PHYSICAL.

    Not the cap authority — it forwards admit/settle/release to the
    kernel; denial and unavailability map onto the mode matrix documented
    in `docs/vnext/A3_BUDGET_ADMISSION.md`.
    """

    def __init__(
        self,
        budget_client: BudgetAdmissionClient,
        config: ModelPolicyConfig,
        policy: HostPolicySnapshot | None = None,
    ) -> None:
        super().__init__()
        self._budget = budget_client
        self._config = config
        self._local = LocalTokenBudget(policy or HostPolicySnapshot())

    def _context(self, request: Any) -> Mapping[str, Any]:
        context = getattr(getattr(request, "runtime", None), "context", None)
        return context if isinstance(context, Mapping) else {}

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        context = self._context(request)
        run_id, governed = _identity(context)
        est_input = _est_input_tokens(request.messages)
        reserve_tokens = est_input + self._config.max_output_tokens_per_call
        reserve_usd = Decimal(reserve_tokens) * self._config.usd_per_1k_tokens / Decimal(1000)

        admission_id: str | None = None
        if self._config.cap_usd is not None:
            try:
                admission = await self._budget.admit(
                    tenant_id=self._config.tenant_id,
                    execution_run_id=run_id,
                    cap_usd=self._config.cap_usd,
                    reserve_usd=reserve_usd,
                    idempotency_key=f"budget:{run_id}:{uuid.uuid4().hex[:16]}",
                )
                admission_id = str(admission["admission_id"])
            except BudgetDenied as exc:
                raise AdmissionError(f"ewcp budget denied {run_id}: {exc}") from exc
            except Exception as exc:
                self._on_policy_unavailable(governed, run_id, est_input, exc)
        else:
            self._local.check(run_id, est_input, self._config.max_output_tokens_per_call)

        try:
            response = await handler(request)
        except BaseException:
            if admission_id is not None:
                await self._release(admission_id, run_id)
            raise

        used = _total_tokens(response)
        if used is not None:
            self._local.record(run_id, used)
        if admission_id is not None:
            await self._settle(admission_id, governed, run_id, used, reserve_tokens, reserve_usd, request)
        return response

    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        """Sync path: no kernel round-trip exists here — governed calls
        fail closed (an unaccounted governed call is worse than a denied
        one); general calls consult the local token gate only."""
        context = self._context(request)
        run_id, governed = _identity(context)
        if governed and self._config.cap_usd is not None:
            raise AdmissionError(f"ewcp governed run {run_id} cannot admit budget on the synchronous model path — use the async runtime")
        self._local.check(run_id, _est_input_tokens(request.messages), self._config.max_output_tokens_per_call)
        response = handler(request)
        used = _total_tokens(response)
        if used is not None:
            self._local.record(run_id, used)
        return response

    def _on_policy_unavailable(self, governed: bool, run_id: str, est_input: int, exc: Exception) -> None:
        if governed or self._config.general_on_policy_unavailable == "deny":
            raise AdmissionError(f"ewcp budget policy unavailable for {'governed' if governed else 'general'} run {run_id} (fail-closed): {exc}") from exc
        if self._config.general_on_policy_unavailable == "local":
            self._local.check(run_id, est_input, self._config.max_output_tokens_per_call)
        logger.warning("ewcp budget admission unavailable for general run %s — proceeding per %s policy: %s", run_id, self._config.general_on_policy_unavailable, exc)

    async def _release(self, admission_id: str, run_id: str) -> None:
        """Best-effort: a failed release frees nothing immediately but the
        reservation expires on the kernel side at its TTL — never mask the
        provider's original error."""
        try:
            await self._budget.release(admission_id)
        except Exception as exc:
            logger.warning("ewcp budget release failed for %s (admission %s; expires by TTL): %s", run_id, admission_id, exc)

    async def _settle(
        self,
        admission_id: str,
        governed: bool,
        run_id: str,
        used: int | None,
        reserve_tokens: int,
        reserve_usd: Decimal,
        request: Any,
    ) -> None:
        # Missing provider usage still cost money: the reservation is the
        # conservative charge (fail-closed accounting, kernel Q8 analog).
        tokens = used if used is not None else reserve_tokens
        usd = Decimal(used) * self._config.usd_per_1k_tokens / Decimal(1000) if used is not None else reserve_usd
        try:
            await self._budget.settle(admission_id, tokens=tokens, usd=usd, model=_model_name(request))
        except Exception as exc:
            if governed:
                raise AdmissionError(f"ewcp budget settlement failed for governed run {run_id} — spend unaccounted: {exc}") from exc
            logger.error("ewcp budget settlement failed for general run %s (admission %s) — charge unaccounted, audit gap: %s", run_id, admission_id, exc)
