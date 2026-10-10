"""Budget admission for the product lane's paid model calls (A3 Task 5).

Contract: `docs/vnext/A3_BUDGET_ADMISSION.md`. The EWCP kernel is the
hard-cap authority — `BudgetAdmissionMiddleware` sits at
`Placement.MODEL_PHYSICAL` (fires once per physical provider call;
retries re-enter it) and admits BEFORE the call:

    admit (precheck + worst-case reserve) -> handler -> settle (metered)
                                                   -> release (failed call)

Identity: the admit computes it from authenticated server state — the
ExecutionRunMap row the launch path bound to this thread/run resolves the
governed workrun (client bytes never write that table), and the declared
tenant comes from the deployment tenant getter. Governed runs admit under
their workrun id — their budget account aggregates with the kernel-native
WorkRun ledger. General runs admit under the server-owned
`runtime.context["run_id"]`.

`runtime.context["kernel"]` / `runtime.context["ewcp_tenant_id"]` are never
read here: the client controls those keys until the egress middleware's
stamp pass runs, and this middleware sits OUTER of it at MODEL_PHYSICAL —
a wrap-level cleanup cannot defend the admit (PR #61 characterization).
When the map cannot be read at all, identity is unverifiable and a capped
deployment fails closed rather than risk an unaccounted governed spend.

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

GP01_EVAL F3 — transient provider quota: the same quota guard that makes
admission errors non-retriable also matches transient per-minute 429s
(Gemini RESOURCE_EXHAUSTED carries a RetryInfo `retryDelay` of seconds),
sending runs to terminal `error` on a condition that clears on its own.
This seam retries the physical provider call INSIDE the admission —
bounded attempts, honoring the provider's delay hint — before the outer
error handler ever classifies it. Hard-quota signals (billing/credit)
and delays above `max_provider_delay_s` (daily resets) stay
non-transient; auth and non-429 4xx are never retried.

The synchronous model-call path cannot reach the kernel — a governed
call there fails closed; a general call consults only the local token
gate. It likewise never retries (it cannot wait without blocking).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from deerflow.models.request_admission import AdmissionError
from deerflow_extension_api import HostPolicySnapshot
from langchain.agents.middleware import AgentMiddleware

from .egress_policy import _select_bound_workrun
from .kernel_client import BudgetDenied, KernelClient

logger = logging.getLogger(__name__)


class PolicyUnavailable(RuntimeError):
    """The admission policy could not be evaluated at all — kernel
    unreachable, unconfigured, or an unexpected admission error. Distinct
    from `BudgetDenied` (a real 402): denial is a verdict, unavailability
    is a missing verdict."""


# ---------------------------------------------------------------------------
# F3 — transient provider quota retry (GP01_EVAL)
# ---------------------------------------------------------------------------

#: Hard-quota signals — money-side failures that a retry cannot clear.
#: Deliberately narrower than the host middleware's quota matcher (which
#: includes the bare word "quota": Google's TRANSIENT RESOURCE_EXHAUSTED
#: messages all say "Quota exceeded..."). A bare "quota" mention is NOT a
#: hard signal; billing/insufficient_quota/credit/payment are.
_HARD_QUOTA_PATTERNS = re.compile(
    r"insufficient_quota|billing|credit|payment|exceeded your current quota"
    r"|quota has been exceeded",
    re.IGNORECASE,
)

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)", re.IGNORECASE)
_RETRY_DELAY_TEXT = re.compile(r"retry[_-]?delay[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_RETRY_IN_TEXT = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_STATUS_429_TEXT = re.compile(r"(?:status|code|error)[\s:='\"]+429(?!\d)")


def _parse_duration_seconds(value: Any) -> float | None:
    """Parse provider duration hints — '6s', '6.480s', '10h', '1m30s',
    '250ms', or a bare number of seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    matched = False
    for amount, unit in _DURATION_PART.findall(text):
        matched = True
        total += float(amount) * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit.lower()]
    return total if matched else None


def _retry_delay_in(obj: Any, _depth: int = 0) -> float | None:
    """Walk an error's structured body for a retryDelay/retryAfter field."""
    if _depth > 5 or obj is None:
        return None
    if isinstance(obj, Mapping):
        for key in ("retryDelay", "retry_delay", "retryAfter", "retry_after"):
            if key in obj:
                delay = _parse_duration_seconds(obj[key])
                if delay is not None:
                    return delay
        return next((d for v in obj.values() if (d := _retry_delay_in(v, _depth + 1)) is not None), None)
    if isinstance(obj, (list, tuple)):
        return next((d for v in obj if (d := _retry_delay_in(v, _depth + 1)) is not None), None)
    return None


def _provider_retry_delay_seconds(exc: BaseException) -> float | None:
    """The provider's own wait hint, when one was sent:
    Retry-After headers (OpenAI shape), RetryInfo retryDelay in structured
    bodies (Google shape), or a 'retryDelay'/'retry in Ns' string."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        delay = _parse_duration_seconds(raw)
        if delay is not None:
            return delay
    for source in (getattr(exc, "details", None), getattr(exc, "body", None), getattr(exc, "error", None)):
        delay = _retry_delay_in(source)
        if delay is not None:
            return delay
    text = str(exc)
    match = _RETRY_DELAY_TEXT.search(text) or _RETRY_IN_TEXT.search(text)
    if match:
        return _parse_duration_seconds(match.group(1))
    return None


def _http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status across provider SDK shapes:
    openai `.status_code`, google.genai `.code` (int), httpx/requests
    `.response.status_code`, plus a bounded message-text fallback."""
    for attr in ("status_code", "http_status", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    if _STATUS_429_TEXT.search(str(exc)):
        return 429
    return None


def _is_transient_quota_error(exc: BaseException, provider_delay: float | None, policy: TransientRetryPolicy) -> bool:
    """Transient iff: HTTP 429 AND (a bounded provider wait hint OR a plain
    rate-limit with no hard-quota signal). Billing-class 429s without a
    delay hint and daily-reset delays (e.g. '10h') are terminal here."""
    if _http_status(exc) != 429:
        return False
    if provider_delay is not None:
        return provider_delay <= policy.max_provider_delay_s
    return not _HARD_QUOTA_PATTERNS.search(str(exc))


async def _noop_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass(frozen=True)
class TransientRetryPolicy:
    """Bounded retry for transient provider 429s (F3), resolved from the
    plugin `model_retry` block.

    `max_attempts` counts ALL tries including the first (1 disables).
    `max_provider_delay_s` is the transience bound: a provider delay above
    it is a quota-reset horizon a run cannot wait out. `sleep` is the
    async wait — injectable for tests; never blocks the event loop.
    """

    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 15.0
    max_provider_delay_s: float = 300.0
    jitter: float = 0.25
    sleep: Callable[[float], Awaitable[None]] = field(default=_noop_sleep, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.max_provider_delay_s < 0:
            raise ValueError("retry delays must be >= 0")

    @classmethod
    def resolve(cls, config: Mapping[str, Any] | None) -> TransientRetryPolicy:
        block = (config or {}).get("model_retry") or {}
        if not isinstance(block, Mapping):
            raise ValueError("plugin config `model_retry` must be a mapping")
        return cls(
            max_attempts=int(block.get("max_attempts", 4)),
            base_delay_s=float(block.get("base_delay_s", 1.0)),
            max_delay_s=float(block.get("max_delay_s", 15.0)),
            max_provider_delay_s=float(block.get("max_provider_delay_s", 300.0)),
            jitter=float(block.get("jitter", 0.25)),
        )


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

    `tenant_id` declares the tenant on a budget admission — the dev-mode
    kernel (no API keys configured) requires it, and without it
    `POST /budget/admissions` 422s and the admission lane degrades to
    `local`, leaving the kernel ledger empty. `budget.tenant_id` wins,
    falling back to `invoke.tenant_id` — the same operator tenant the
    capability-invoke lane declares — so one tenant covers both lanes.
    Keyed kernels ignore the declared tenant unless it mismatches the
    key (403).
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
        invoke = (config or {}).get("invoke") or {}
        invoke_tenant = invoke.get("tenant_id") if isinstance(invoke, Mapping) else None
        return cls(
            cap_usd=Decimal(str(cap)) if cap is not None else None,
            tenant_id=str(budget["tenant_id"]) if budget.get("tenant_id") else (str(invoke_tenant) if invoke_tenant else None),
            usd_per_1k_tokens=Decimal(str(budget.get("usd_per_1k_tokens", "0.004"))),
            max_output_tokens_per_call=int(budget.get("max_output_tokens_per_call", 4096)),
            general_on_policy_unavailable=mode,
        )


def _general_run_id(context: Mapping[str, Any]) -> str:
    """The server-owned general-lane attribution key — `run_id`/`thread_id`
    are set by the run worker and cannot be overridden by caller context."""
    return str(context.get("run_id") or context.get("thread_id") or "unknown")


async def _bound_workrun_id(store: Any, context: Mapping[str, Any]) -> str | None:
    """The governed workrun the ExecutionRunMap binds to this run — the ONLY
    governed-identity proof the admit accepts. Strict variant of the egress
    gate's lookup: a store read failure PROPAGATES (egress's is fail-open),
    so the caller can fail closed on an unverifiable identity."""
    if store is None:
        return None
    thread_id = context.get("thread_id")
    if not thread_id:
        return None
    records = await store.list_for_thread(str(thread_id))
    return _select_bound_workrun(records, context.get("run_id"))


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
        retry: TransientRetryPolicy | None = None,
        tenant_id_getter: Callable[[], str | None] | None = None,
        store_getter: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self._budget = budget_client
        self._config = config
        self._retry = retry or TransientRetryPolicy()
        self._local = LocalTokenBudget(policy or HostPolicySnapshot())
        self._tenant_id_getter = tenant_id_getter
        self._store_getter = store_getter

    def _context(self, request: Any) -> Mapping[str, Any]:
        context = getattr(getattr(request, "runtime", None), "context", None)
        return context if isinstance(context, Mapping) else {}

    def _admission_tenant(self) -> str | None:
        """Tenant declared on an admission: the deployment tenant getter (the
        same authenticated source the egress stamp pass writes) first, then
        the configured `budget.tenant_id` fallback. Never the context value
        — that key is client-controllable until re-stamped."""
        tenant = self._tenant_id_getter() if self._tenant_id_getter is not None else None
        return tenant or self._config.tenant_id

    async def _resolve_identity(self, context: Mapping[str, Any]) -> tuple[str, bool]:
        """(execution_run_id, governed) resolved from server state.

        No ExecutionRunMap bound (unstarted store) means no governed launch
        could have been admitted — the run is general by construction. A
        store read failure under a configured cap fails closed: an
        unverifiable run might be governed, and a governed call must not
        spend unaccounted."""
        run_id = _general_run_id(context)
        store = self._store_getter() if self._store_getter is not None else None
        if store is None:
            return run_id, False
        try:
            workrun_id = await _bound_workrun_id(store, context)
        except Exception as exc:
            if self._config.cap_usd is not None:
                raise AdmissionError(f"ewcp budget identity unverifiable for run {run_id} — execution-run map unreadable, fail-closed under cap: {exc}") from exc
            logger.warning("ewcp budget identity lookup failed for run %s — proceeding as general (no cap configured): %s", run_id, exc)
            return run_id, False
        if workrun_id:
            return workrun_id, True
        return run_id, False

    def _resolve_identity_sync(self, context: Mapping[str, Any]) -> tuple[str, bool, bool]:
        """(execution_run_id, governed, verifiable) for the sync wrap.

        The map read is async; on a thread without a running loop it is
        driven on a private loop. When no safe read exists (a live loop in
        this thread, or the lookup itself failed) the identity is
        unverifiable — the caller fails closed under a configured cap."""
        run_id = _general_run_id(context)
        store = self._store_getter() if self._store_getter is not None else None
        if store is None:
            return run_id, False, True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.warning("ewcp budget identity lookup skipped for run %s — sync wrap on a thread with a running loop; unverifiable", run_id)
            return run_id, False, False
        try:
            workrun_id = asyncio.run(_bound_workrun_id(store, context))
        except Exception:  # noqa: BLE001 — any lookup failure = unverifiable
            logger.warning("ewcp budget identity lookup failed for run %s on the sync path — unverifiable", run_id, exc_info=True)
            return run_id, False, False
        if workrun_id:
            return workrun_id, True, True
        return run_id, False, True

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        context = self._context(request)
        run_id, governed = await self._resolve_identity(context)
        est_input = _est_input_tokens(request.messages)
        reserve_tokens = est_input + self._config.max_output_tokens_per_call
        reserve_usd = Decimal(reserve_tokens) * self._config.usd_per_1k_tokens / Decimal(1000)

        admission_id: str | None = None
        if self._config.cap_usd is not None:
            try:
                admission = await self._budget.admit(
                    tenant_id=self._admission_tenant(),
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
            response = await self._call_with_transient_retry(handler, request, run_id)
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

    async def _call_with_transient_retry(self, handler: Callable, request: Any, run_id: str) -> Any:
        """Invoke the physical provider call with bounded transient-429 retry
        (F3). The retry sits inside the admission: one reservation covers the
        logical call, released once if every attempt fails. The provider's
        own delay hint is honored (never shortened); absent a hint the
        backoff decorrelates (base, ×3, capped). Non-transient errors —
        auth, other 4xx, billing-class quota without a wait hint — raise
        immediately. Cancellation propagates (asyncio.sleep is the only
        wait, never a blocking call).
        """
        policy = self._retry
        attempt = 0
        backoff = policy.base_delay_s
        while True:
            attempt += 1
            try:
                return await handler(request)
            except Exception as exc:
                provider_delay = _provider_retry_delay_seconds(exc)
                if attempt >= policy.max_attempts or not _is_transient_quota_error(exc, provider_delay, policy):
                    raise
                if provider_delay is not None:
                    delay = provider_delay * (1 + random.uniform(0, policy.jitter))
                else:
                    delay = min(policy.max_delay_s, backoff) * (1 + random.uniform(-policy.jitter, policy.jitter))
                    backoff = min(policy.max_delay_s, backoff * 3)
                delay = max(0.0, delay)
                logger.warning(
                    "ewcp transient provider 429 on run %s (attempt %d/%d) — retrying in %.1fs",
                    run_id,
                    attempt,
                    policy.max_attempts,
                    delay,
                )
                await policy.sleep(delay)

    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        """Sync path: no kernel round-trip exists here — governed calls
        fail closed (an unaccounted governed call is worse than a denied
        one); general calls consult the local token gate only. Identity
        comes from the ExecutionRunMap like the async path; when it cannot
        be read on this thread the run is unverifiable and a capped
        deployment denies rather than risk an unaccounted governed call."""
        context = self._context(request)
        run_id, governed, verifiable = self._resolve_identity_sync(context)
        if not verifiable and self._config.cap_usd is not None:
            raise AdmissionError(f"ewcp budget identity unverifiable for run {run_id} on the synchronous path — fail-closed under cap")
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
