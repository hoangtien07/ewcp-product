"""EWCP extension contributions: kernel service + governed fast-path router.

Topology (PA-A): the EWCP kernel lives in its own package (`ewcp`, repo
enterprise-work-control-plane) and is mounted IN-PROCESS via an ASGI
transport — no HTTP hop, no upstream edits. Every /api/ewcp/* request is
forwarded to the kernel's own FastAPI app so all kernel endpoints (tasks,
decisions, manifest, /verify) work identically to the standalone deploy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import os
from pathlib import Path

import httpx
from deerflow_extension_api import ExtensionRuntimeDeps
from fastapi import APIRouter, Request, Response

_FORWARDED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}
# Request headers allowlisted into the kernel: identity is carried only by
# x-ewcp-api-key (the kernel binds key<->tenant itself); nothing else a client
# sends may reach kernel authorization.
_ALLOWED_HEADERS = {"content-type", "accept", "x-ewcp-api-key"}
# Response headers that must not pass through: httpx already decoded the body,
# and content-length no longer matches the decoded payload.
_RESP_STRIP_HEADERS = _HOP_HEADERS | {"content-encoding"}


class EwcpKernelService:
    """In-process mount of the EWCP kernel FastAPI app.

    Lazily imports `ewcp.api.app`; when the kernel package is absent the
    service still loads (health reports kernel_loaded=false) so a missing
    dependency degrades loudly instead of breaking Gateway startup.
    """

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config: Mapping[str, Any] = config or {}
        self.kernel_loaded: bool = False
        self.kernel_error: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._kernel_app: Any = None
        self._lifespan_cm: Any = None

    async def start(self, deps: ExtensionRuntimeDeps) -> None:
        self._load_kernel()
        # ASGITransport never runs the app's lifespan — drive it manually so
        # kernel startup hooks (if any) execute before reporting loaded.
        if self._kernel_app is not None:
            self._lifespan_cm = self._kernel_app.router.lifespan_context(self._kernel_app)
            await self._lifespan_cm.__aenter__()

    async def stop(self) -> None:
        if self._lifespan_cm is not None:
            await self._lifespan_cm.__aexit__(None, None, None)
            self._lifespan_cm = None
        await self.aclose()

    def _load_kernel(self) -> None:
        try:
            from ewcp.api.app import create_app
        except Exception as exc:  # noqa: BLE001 — surface any import/boot failure
            self.kernel_error = f"{type(exc).__name__}: {exc}"
            self._client = None
            self.kernel_loaded = False
            return
        home = Path(self.config.get("data_dir") or os.environ.get("DEER_FLOW_HOME", ".deer-flow")) / "ewcp"
        self._kernel_app = create_app(
            store_dir=home / "store",
            work_dir=home / "work",
        )
        transport = httpx.ASGITransport(app=self._kernel_app)
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url="http://ewcp.kernel",
            timeout=120.0,
        )
        self.kernel_loaded = True

    def status(self) -> dict[str, Any]:
        return {
            "extension": "ewcp_packs",
            "kernel_loaded": self.kernel_loaded,
            "kernel_error": self.kernel_error,
        }

    async def forward(self, request: Request, path: str) -> Response:
        if self._client is None:
            return Response(
                content='{"detail":"EWCP kernel not loaded: %s"}' % (self.kernel_error or "unknown"),
                status_code=503,
                media_type="application/json",
            )
        # Raw query_string preserves repeated keys (a params= mapping would
        # collapse them to the last value).
        url = httpx.URL(
            path="/" + path,
            query=bytes(request.scope.get("query_string", b"")),
        )
        headers = {k: v for k, v in request.headers.items() if k.lower() in _ALLOWED_HEADERS}
        body = await request.body()
        upstream = await self._client.request(
            request.method,
            url,
            content=body if body else None,
            headers=headers,
        )
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _RESP_STRIP_HEADERS}
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type", "").split(";")[0] or None,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def build_router(service: EwcpKernelService) -> APIRouter:
    router = APIRouter(prefix="/api/ewcp", tags=["ewcp"])

    @router.get("/_status")
    async def ewcp_status() -> dict[str, Any]:
        return service.status()

    @router.api_route("/{path:path}", methods=list(_FORWARDED_METHODS))
    async def ewcp_forward(path: str, request: Request) -> Response:
        return await service.forward(request, path)

    return router
