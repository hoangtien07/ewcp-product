"""HTTP client for the EWCP kernel (repo enterprise-work-control-plane).

The kernel is a governed capability & verification server reached over
HTTP (Option A+) — never mounted in-process. Verified wire contract
(kernel `src/ewcp/api/app.py` @ main):

  POST   /tasks                       multipart form (`intent`, `tenant_id`,
                                      declared input slots / `files`);
                                      optional `Idempotency-Key` header —
                                      a repeat under the same (tenant, key)
                                      returns 200 + `Idempotent-Replay:
                                      true` (app.py:2528-2577;
                                      idempotency_keys table,
                                      kernel/persistence.py:105-111)
  POST   /workruns/{id}/decisions     JSON {answer, decision_id?,
                                      decided_by?} -> run view
                                      (app.py:2037) — NO idempotency
                                      contract on this endpoint
  GET    /workruns/{id}               run view (app.py:1748)
  GET    /verify/{manifest_hash}      {manifest, seal_ok, workrun_id}
                                      (app.py:3273) — public by design
  GET    /outcomes                    outcome spec wire views incl.
                                      requires_inputs + context_schema
                                      (app.py:1599)
  auth   `X-Ewcp-Api-Key` header — the extension-contract key name
                                      (app.py:614-618)

Retry policy (spec A3 Task 1): safe reads retry bounded on transient
faults (408/429/5xx, transport errors; kernel's own GeminiClient pattern —
runtime/gemini_client.py:192-278); mutations retry ONLY under the
kernel's real idempotency contract — i.e. `create_task` when the caller
supplies `idempotency_key`. `decide` is never retried: a replayed answer
has no dedupe surface on the kernel side.

Budget admission (A3 Task 5 — kernel hard-cap authority for paid model
calls; contract doc `docs/vnext/A3_BUDGET_ADMISSION.md`):

  POST   /budget/admissions                  precheck + reserve BEFORE the
                                             call; `Idempotency-Key`
                                             dedupes client retries
                                             (replay -> 200 +
                                             `Idempotent-Replay: true`)
  POST   /budget/admissions/{id}/settle      charge metered spend;
                                             idempotent by admission_id
  POST   /budget/admissions/{id}/release     close a pending reservation
                                             (failed call), idempotent
  GET    /budget/accounts/{execution_run_id} audit view: cap/spent/reserved

  Settle/release retry on transient faults because the kernel's replay
  contract makes a repeated call return the recorded outcome, never a
  double charge.

The kernel API key is request-scoped, never stored on the httpx client,
never interpolated into URLs or log lines.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

# Transient statuses worth another attempt — same set the kernel's
# GeminiClient retries (runtime/gemini_client.py:194).
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _DEFAULT_BACKOFF(attempt: int) -> float:
    return min(2.0**attempt, 8.0)


ENV_KERNEL_URL = "EWCP_KERNEL_URL"
ENV_KERNEL_API_KEY = "EWCP_KERNEL_API_KEY"


class KernelNotConfigured(RuntimeError):
    """Raised when a call is made without a configured kernel_url."""


@dataclass(frozen=True)
class KernelClientConfig:
    """Resolved client settings. `api_key` is held for header injection
    per request and must never be rendered."""

    kernel_url: str | None = None
    api_key: str | None = None
    timeout: float = 30.0
    max_attempts: int = 3

    @classmethod
    def resolve(
        cls,
        config: Mapping[str, Any] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> KernelClientConfig:
        """Env-first resolution: EWCP_KERNEL_URL / EWCP_KERNEL_API_KEY
        override the plugin `config:` keys `kernel_url` / `kernel_api_key`
        (and the optional `timeout_seconds` / `read_max_attempts`)."""
        env = os.environ if env is None else env
        cfg = config or {}
        kernel_url = env.get(ENV_KERNEL_URL) or cfg.get("kernel_url")
        api_key = env.get(ENV_KERNEL_API_KEY) or cfg.get("kernel_api_key")
        return cls(
            kernel_url=str(kernel_url) if kernel_url else None,
            api_key=str(api_key) if api_key else None,
            timeout=float(cfg.get("timeout_seconds", cls.timeout)),
            max_attempts=int(cfg.get("read_max_attempts", cls.max_attempts)),
        )


@dataclass(frozen=True)
class TaskSubmitResult:
    """`POST /tasks` outcome — the run view plus whether the kernel
    reported an idempotent replay (`Idempotent-Replay: true`)."""

    run: dict[str, Any]
    idempotent_replay: bool


@dataclass(frozen=True)
class BudgetAdmissionResult:
    """`POST /budget/admissions` outcome — the admission record plus
    whether the kernel replayed an earlier reservation under the same
    `Idempotency-Key`."""

    admission: dict[str, Any]
    idempotent_replay: bool


class BudgetDenied(RuntimeError):
    """`POST /budget/admissions` returned 402 — the execution_run's pinned
    cap is exhausted; the caller must NOT invoke the provider. `detail`
    carries the kernel's deny body (error, cap_usd, spent_usd, ...)."""

    def __init__(self, detail: Mapping[str, Any] | None = None) -> None:
        self.detail = dict(detail or {})
        super().__init__(str(self.detail.get("message") or self.detail.get("error") or "budget exceeded"))


class KernelInvokeError(RuntimeError):
    """Structured rejection from the capability-invoke contract
    (`GET /outcomes/{type}` or `POST /outcomes/{type}/run`).

    `error` is the contract's stable machine code (unauthenticated,
    tenant_mismatch, unknown_capability, conflict, missing_required_input,
    schema_invalid, unsupported_version, idempotency_payload_mismatch,
    payload_too_large, invalid_request, run_failed); `message` is the
    human-readable line; `extra` carries code-specific fields such as
    `missing: [...]` from a 422."""

    # guidance vocabulary: "correct" = fix payload/args then retry;
    # "retry" = transient, resend as-is; "fatal" = do not retry.
    _CORRECTABLE = frozenset(
        {
            "missing_required_input",
            "schema_invalid",
            "invalid_request",
            "unknown_capability",
            "idempotency_payload_mismatch",
            "payload_too_large",
        }
    )
    _FATAL = frozenset({"unsupported_version", "unauthenticated", "tenant_mismatch"})

    def __init__(
        self,
        *,
        status: int,
        error: str,
        message: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.error = error or "invalid_request"
        self.message = message or self.error
        self.extra = dict(extra or {})
        super().__init__(f"{status} {self.error}: {self.message}")

    @property
    def guidance(self) -> str:
        """What the caller should do next (contract §error-table):
        payload mismatch / validation errors are retryable *with
        correction*; unsupported_version is fatal; transient statuses
        retry as-is; anything unrecognized is fatal (fail-safe)."""
        if self.error in self._CORRECTABLE:
            return "correct"
        if self.error in self._FATAL:
            return "fatal"
        if self.status in _RETRYABLE_STATUS:
            return "retry"
        return "fatal"


def _invoke_error(exc: httpx.HTTPStatusError) -> KernelInvokeError:
    """Map a kernel error response to KernelInvokeError, preserving the
    contract's `{error, message}` pair plus any extra fields."""
    resp = exc.response
    error = "invalid_request"
    message = ""
    extra: dict[str, Any] = {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON error page
        body = None
    if isinstance(body, dict):
        error = str(body.get("error") or error)
        message = str(body.get("message") or body.get("detail") or "")
        extra = {k: v for k, v in body.items() if k not in {"error", "message", "detail"}}
    if not message:
        message = (resp.text or "")[:300] or type(exc).__name__
    return KernelInvokeError(status=resp.status_code, error=error, message=message, extra=extra)


def _ctx_scalar(value: Any) -> str:
    """Render a typed context value as a multipart form scalar.
    bools as true/false, numbers as str, structured values as JSON
    (the kernel re-parses by the declared context type)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value)


@dataclass(frozen=True)
class InvokeResult:
    """`POST /outcomes/{type}/run` outcome — the run view plus whether
    the kernel replayed an earlier run under the same Idempotency-Key."""

    run: dict[str, Any]
    idempotent_replay: bool


# (field-name, filename, body-bytes-or-str, optional content-type)
UploadTuple = tuple[str, "bytes | str", "str | None"]


class KernelClient:
    """Async kernel HTTP client. One per extension service lifecycle."""

    def __init__(
        self,
        config: KernelClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff: Callable[[int], float] | None = None,
    ) -> None:
        self._config = config
        self._api_key = config.api_key  # request-scoped; never on the client
        self._max_attempts = max(1, config.max_attempts)
        self._backoff = backoff or _DEFAULT_BACKOFF
        self._client = (
            httpx.AsyncClient(
                base_url=config.kernel_url,
                timeout=config.timeout,
                transport=transport,
            )
            if config.kernel_url
            else None
        )

    def __repr__(self) -> str:
        return f"KernelClient(kernel_url={self._config.kernel_url!r}, api_key={'set' if self._api_key else 'unset'})"

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise KernelNotConfigured("ewcp kernel_url is not configured — set EWCP_KERNEL_URL or the plugin config key `kernel_url`")
        return self._client

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-Ewcp-Api-Key"] = self._api_key
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_retry: bool,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        client = self._require_client()
        attempts = self._max_attempts if allow_retry else 1
        for attempt in range(attempts):
            try:
                resp = await client.request(method, path, headers=self._headers(headers), **kwargs)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS or attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(self._backoff(attempt))
            except httpx.TransportError:
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(self._backoff(attempt))
            else:
                return resp
        raise AssertionError("unreachable")

    # -- safe reads (bounded retry) -------------------------------------------

    async def list_outcomes(self) -> list[dict[str, Any]]:
        """GET /outcomes — outcome spec wire views (input slots + context
        schema included; the kernel exposes no grants surface)."""
        resp = await self._request("GET", "/outcomes", allow_retry=True)
        return resp.json()

    async def get_outcome_descriptor(self, outcome_type: str) -> dict[str, Any]:
        """GET /outcomes/{type} — the capability descriptor (contract v1):
        capability_id, side_effect_class, input_schema, idempotency block.
        404 surfaces as KernelInvokeError(error='unknown_capability')."""
        try:
            resp = await self._request("GET", f"/outcomes/{outcome_type}", allow_retry=True)
        except httpx.HTTPStatusError as exc:
            raise _invoke_error(exc) from exc
        return resp.json()

    async def get_workrun(self, workrun_id: str) -> dict[str, Any]:
        """GET /workruns/{id} — governed truth: status, pending_questions,
        deliverables, attempts, spend."""
        resp = await self._request("GET", f"/workruns/{workrun_id}", allow_retry=True)
        return resp.json()

    async def verify_manifest(self, manifest_hash: str) -> dict[str, Any]:
        """GET /verify/{hash} — public seal permalink: {manifest, seal_ok,
        workrun_id}. Attests a seal exists; byte-integrity is the
        POST /verify upload path (out of Task-1 scope)."""
        resp = await self._request("GET", f"/verify/{manifest_hash}", allow_retry=True)
        return resp.json()

    # -- budget admission (kernel hard-cap authority; A3 Task 5) -------------

    async def admit_budget(
        self,
        *,
        execution_run_id: str,
        cap_usd: str,
        reserve_usd: str,
        tenant_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BudgetAdmissionResult:
        """POST /budget/admissions — precheck + worst-case reserve in one
        transaction, BEFORE the provider call. 402 -> BudgetDenied.

        `idempotency_key` dedupes transport retries (same key -> same
        reservation, `Idempotent-Replay: true`); retries only run when a
        key is supplied — without one a retry could double-reserve."""
        body: dict[str, Any] = {
            "execution_run_id": execution_run_id,
            "cap_usd": cap_usd,
            "reserve_usd": reserve_usd,
        }
        if tenant_id:
            body["tenant_id"] = tenant_id
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            resp = await self._request(
                "POST",
                "/budget/admissions",
                json=body,
                headers=extra,
                allow_retry=idempotency_key is not None,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 402:
                detail = exc.response.json().get("detail") if exc.response.headers.get("content-type", "").startswith("application/json") else None
                raise BudgetDenied(detail if isinstance(detail, Mapping) else {"message": exc.response.text}) from exc
            raise
        return BudgetAdmissionResult(
            admission=resp.json(),
            idempotent_replay=resp.headers.get("idempotent-replay") == "true",
        )

    async def settle_budget(self, admission_id: str, *, tokens: int, usd: str, model: str = "") -> dict[str, Any]:
        """POST /budget/admissions/{id}/settle — charge the metered cost.
        Retriable: replaying a settled admission returns the recorded
        charge instead of double-writing the ledger."""
        resp = await self._request(
            "POST",
            f"/budget/admissions/{admission_id}/settle",
            json={"tokens": int(tokens), "usd": usd, "model": model},
            allow_retry=True,
        )
        return resp.json()

    async def release_budget(self, admission_id: str) -> dict[str, Any]:
        """POST /budget/admissions/{id}/release — close a pending
        reservation with no charge (the admitted call failed before
        metering). Retriable: idempotent on released rows."""
        resp = await self._request("POST", f"/budget/admissions/{admission_id}/release", allow_retry=True)
        return resp.json()

    async def get_budget_account(self, execution_run_id: str) -> dict[str, Any]:
        """GET /budget/accounts/{id} — the run's cap, ledger spend, and
        open reservations (audit view)."""
        resp = await self._request("GET", f"/budget/accounts/{execution_run_id}", allow_retry=True)
        return resp.json()

    # -- additional reads (A3 Task 6 surface) --------------------------------
    async def get_manifest(self, workrun_id: str) -> dict[str, Any]:
        """GET /workruns/{id}/manifest — sealed VerificationManifest
        (manifest_hash, seal, checks[], deliverables[])."""
        resp = await self._request("GET", f"/workruns/{workrun_id}/manifest", allow_retry=True)
        return resp.json()

    async def get_outcome(self, workrun_id: str) -> dict[str, Any]:
        """GET /workruns/{id}/outcome — the pack-written outcome.json
        ({outcome_type, result})."""
        resp = await self._request("GET", f"/workruns/{workrun_id}/outcome", allow_retry=True)
        return resp.json()

    async def get_evidence(self, workrun_id: str) -> dict[str, Any]:
        """GET /workruns/{id}/evidence — evidence export: sealed manifest
        + declared digests per deliverable."""
        resp = await self._request("GET", f"/workruns/{workrun_id}/evidence", allow_retry=True)
        return resp.json()

    async def download_deliverable(self, workrun_id: str, deliverable_id: str) -> httpx.Response:
        """GET /workruns/{id}/deliverables/{did} — raw file bytes; the
        caller forwards content-type/attachment headers (never retried
        is unnecessary: reads are safe — retried bounded like reads)."""
        return await self._request(
            "GET",
            f"/workruns/{workrun_id}/deliverables/{deliverable_id}",
            allow_retry=True,
        )

    async def verify_evidence(
        self,
        *,
        evidence_json: tuple[str, bytes],
        files: Sequence[tuple[str, bytes]],
    ) -> dict[str, Any]:
        """POST /verify — byte-integrity check: evidence.json + the
        artifacts it declares. Side-effect-free by contract, so bounded
        retry is safe."""
        multipart: list[tuple[str, tuple]] = [
            ("evidence_json", (evidence_json[0], evidence_json[1], "application/json")),
        ]
        multipart += [("files", (name, body)) for name, body in files]
        resp = await self._request("POST", "/verify", files=multipart, allow_retry=True)
        return resp.json()

    # -- mutations (retry only under the kernel's real replay contract) --------

    async def create_task(
        self,
        *,
        intent: str,
        tenant_id: str | None = None,
        fields: Mapping[str, str] | None = None,
        files: Mapping[str, Sequence[UploadTuple]] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> TaskSubmitResult:
        """POST /tasks — multipart intake.

        `fields` carries spec context keys (mst, ky…) — the kernel router
        reads declared context keys from the form alongside the intent.
        `files` maps the spec's declared input slot (e.g. `invoices_zip`)
        to (filename, body, content_type) members; the general lane uses
        the fixed slot name `files`. Retry is allowed only when
        `idempotency_key` is set — the kernel dedupes on (tenant, key)
        and replays the original run response.

        `timeout` overrides the client default for this call: governed
        intake dispatches the pack pipeline synchronously inside the
        kernel request (app.py `_sync_execute`), so callers that expect
        a completed run view should pass a wider bound."""
        # Always multipart — the kernel's intake contract is form fields
        # read via request.form() (app.py:2543). `(None, value)` tuples
        # render as plain fields inside the multipart body, so intent/
        # tenant_id ride the same encoding as file uploads even when no
        # files are present (a bare `data=` would go urlencoded).
        form_fields: list[tuple[str, tuple]] = [("intent", (None, intent))]
        if tenant_id:
            form_fields.append(("tenant_id", (None, tenant_id)))
        form_fields += [(name, (None, value)) for name, value in (fields or {}).items()]
        form_fields += [(slot, (filename, body) if content_type is None else (filename, body, content_type)) for slot, members in (files or {}).items() for filename, body, content_type in members]
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        request_kwargs: dict[str, Any] = {"files": form_fields, "headers": extra}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        resp = await self._request(
            "POST",
            "/tasks",
            allow_retry=idempotency_key is not None,
            **request_kwargs,
        )
        return TaskSubmitResult(
            run=resp.json(),
            idempotent_replay=resp.headers.get("idempotent-replay") == "true",
        )

    async def invoke_outcome(
        self,
        outcome_type: str,
        *,
        context: Mapping[str, Any] | None = None,
        files: Mapping[str, Sequence[UploadTuple]] | None = None,
        tenant_id: str | None = None,
        invocation_id: str | None = None,
        execution_run_id: str | None = None,
        idempotency_key: str,
        contract_version: str = "1",
    ) -> InvokeResult:
        """POST /outcomes/{type}/run — the typed capability contract
        (contract_version=1). Always multipart so declared context fields
        and file slots ride the same encoding.

        `idempotency_key` is REQUIRED: the kernel dedupes (tenant, key) and
        replays the stored run on identical payload, so bounded transport
        retry is safe — retries reuse the SAME key and identical body.
        `invocation_id`/`execution_run_id` are the contract's correlation
        fields, echoed back in the run view."""
        form: list[tuple[str, tuple]] = [("contract_version", (None, contract_version))]
        if tenant_id:
            form.append(("tenant_id", (None, tenant_id)))
        if invocation_id:
            form.append(("invocation_id", (None, invocation_id)))
        if execution_run_id:
            form.append(("execution_run_id", (None, execution_run_id)))
        form += [(key, (None, _ctx_scalar(value))) for key, value in (context or {}).items() if value is not None]
        form += [(slot, (filename, body) if content_type is None else (filename, body, content_type)) for slot, members in (files or {}).items() for filename, body, content_type in members]
        try:
            resp = await self._request(
                "POST",
                f"/outcomes/{outcome_type}/run",
                files=form,
                headers={"Idempotency-Key": idempotency_key},
                allow_retry=True,
            )
        except httpx.HTTPStatusError as exc:
            raise _invoke_error(exc) from exc
        return InvokeResult(
            run=resp.json(),
            idempotent_replay=resp.headers.get("idempotent-replay") == "true",
        )

    async def run_outcome(
        self,
        outcome_type: str,
        *,
        tenant_id: str | None = None,
        fields: Mapping[str, str] | None = None,
        files: Mapping[str, Sequence[UploadTuple]] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST /outcomes/{type}/run — explicit per-pack dispatch: the
        caller picks the outcome (kernel stamps intent from the spec's
        `intent_template`), spec context keys ride as plain form fields,
        declared input slots as files. Hard-422s on missing required
        inputs/context — surfacing them to the caller verbatim.

        The product lane sends no Idempotency-Key here (the contract's
        typed binding is `invoke_outcome`): transport never retries and
        the ExecutionRunMap row's owner+key unique index is the dedupe.
        Kernel `main` does dedupe (tenant, key) on this endpoint too —
        contract_version/invocation_id fields are optional there."""
        form_fields: list[tuple[str, tuple]] = []
        if tenant_id:
            form_fields.append(("tenant_id", (None, tenant_id)))
        form_fields += [(name, (None, value)) for name, value in (fields or {}).items()]
        form_fields += [(slot, (filename, body) if content_type is None else (filename, body, content_type)) for slot, members in (files or {}).items() for filename, body, content_type in members]
        request_kwargs: dict[str, Any] = {"files": form_fields}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        resp = await self._request(
            "POST",
            f"/outcomes/{outcome_type}/run",
            allow_retry=False,
            **request_kwargs,
        )
        return resp.json()

    async def decide(
        self,
        workrun_id: str,
        *,
        answer: str,
        decision_id: str | None = None,
        decided_by: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """POST /workruns/{id}/decisions — never retried (no kernel-side
        replay contract; `decide()` rejects non-PENDING anyway).

        `actor` is sent as `X-Ewcp-Actor` — the kernel's trusted M2M
        user-binding contract (kernel PR #108): under tenant-key auth the
        audit principal becomes `<actor>@tenant:<tenant>`. Kernels
        predating it ignore the header (tenant principal + label)."""
        body: dict[str, Any] = {"answer": answer}
        if decision_id is not None:
            body["decision_id"] = decision_id
        if decided_by is not None:
            body["decided_by"] = decided_by
        extra = {"X-Ewcp-Actor": actor} if actor else None
        resp = await self._request(
            "POST",
            f"/workruns/{workrun_id}/decisions",
            json=body,
            headers=extra,
            allow_retry=False,
        )
        return resp.json()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
