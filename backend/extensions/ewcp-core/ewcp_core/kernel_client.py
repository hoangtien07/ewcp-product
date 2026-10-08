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

The kernel API key is request-scoped, never stored on the httpx client,
never interpolated into URLs or log lines.
"""

from __future__ import annotations

import asyncio
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

    # -- mutations (retry only under the kernel's real replay contract) --------

    async def create_task(
        self,
        *,
        intent: str,
        tenant_id: str | None = None,
        files: Mapping[str, Sequence[UploadTuple]] | None = None,
        idempotency_key: str | None = None,
    ) -> TaskSubmitResult:
        """POST /tasks — multipart intake.

        `files` maps the spec's declared input slot (e.g. `invoices_zip`)
        to (filename, body, content_type) members; the general lane uses
        the fixed slot name `files`. Retry is allowed only when
        `idempotency_key` is set — the kernel dedupes on (tenant, key)
        and replays the original run response."""
        # Always multipart — the kernel's intake contract is form fields
        # read via request.form() (app.py:2543). `(None, value)` tuples
        # render as plain fields inside the multipart body, so intent/
        # tenant_id ride the same encoding as file uploads even when no
        # files are present (a bare `data=` would go urlencoded).
        fields: list[tuple[str, tuple]] = [("intent", (None, intent))]
        if tenant_id:
            fields.append(("tenant_id", (None, tenant_id)))
        fields += [(slot, (filename, body) if content_type is None else (filename, body, content_type)) for slot, members in (files or {}).items() for filename, body, content_type in members]
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        resp = await self._request(
            "POST",
            "/tasks",
            files=fields,
            headers=extra,
            allow_retry=idempotency_key is not None,
        )
        return TaskSubmitResult(
            run=resp.json(),
            idempotent_replay=resp.headers.get("idempotent-replay") == "true",
        )

    async def decide(
        self,
        workrun_id: str,
        *,
        answer: str,
        decision_id: str | None = None,
        decided_by: str | None = None,
    ) -> dict[str, Any]:
        """POST /workruns/{id}/decisions — never retried (no kernel-side
        replay contract; `decide()` rejects non-PENDING anyway)."""
        body: dict[str, Any] = {"answer": answer}
        if decision_id is not None:
            body["decision_id"] = decision_id
        if decided_by is not None:
            body["decided_by"] = decided_by
        resp = await self._request(
            "POST",
            f"/workruns/{workrun_id}/decisions",
            json=body,
            allow_retry=False,
        )
        return resp.json()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
