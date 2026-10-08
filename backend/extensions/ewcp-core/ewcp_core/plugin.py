"""ewcp-core extension contributions: kernel client service + status router.

Topology (A3, Option A+): the EWCP kernel lives in its own repo and is
reached over HTTP/M2M via `KernelClient` — no in-process ASGI mount.
The service resolves `kernel_url`/`kernel_api_key` env-first
(EWCP_KERNEL_URL / EWCP_KERNEL_API_KEY beat the plugin `config:` block),
starts one shared AsyncClient at Gateway startup, and reports wiring on
`GET /api/ewcp/_status`. The API key is only ever a request header —
the status payload carries `api_key_configured`, never the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deerflow_extension_api import ExtensionRuntimeDeps
from fastapi import APIRouter

from .egress_policy import EgressPolicy
from .execution_run_store import ExecutionRunStore
from .kernel_client import KernelClient, KernelClientConfig
from .run_launcher import RunLauncher


class EwcpCoreService:
    """Owns the shared kernel HTTP client and the ExecutionRunMap launcher
    for the Gateway lifetime.

    The store binds to the shared product DB via
    `ExtensionRuntimeDeps.session_factory` (extension-owned tables under the
    `ewcp_` prefix — declare `table_prefix: ewcp_` on the `plugins:` record;
    see README). The launcher it exposes needs the request-bound `AgentRuns`
    handle (`resolve_agent_runs(request)`) passed per call — never cached.
    """

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config: Mapping[str, Any] = config or {}
        self._resolved = KernelClientConfig.resolve(self.config)
        self.egress_policy = EgressPolicy(EgressPolicy.resolve_config(self.config))
        self._client: KernelClient | None = None
        self._store: ExecutionRunStore | None = None
        self._launcher: RunLauncher | None = None

    @property
    def client(self) -> KernelClient | None:
        return self._client

    @property
    def store(self) -> ExecutionRunStore | None:
        return self._store

    @property
    def launcher(self) -> RunLauncher | None:
        return self._launcher

    async def start(self, deps: ExtensionRuntimeDeps) -> None:
        if self._resolved.kernel_url:
            self._client = KernelClient(self._resolved)
        session_factory = getattr(deps, "session_factory", None) if deps is not None else None
        if session_factory is not None:
            self._store = ExecutionRunStore(session_factory)
            await self._store.ensure_schema()
            self._launcher = RunLauncher(self._store)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._launcher = None
        self._store = None

    def status(self) -> dict[str, Any]:
        return {
            "extension": "ewcp_core",
            "kernel_url": self._resolved.kernel_url,
            "kernel_configured": self._resolved.kernel_url is not None,
            "api_key_configured": self._resolved.api_key is not None,
            "client_started": self._client is not None,
            "store_started": self._store is not None,
            "egress": {
                "default_mode": self.egress_policy.config.default_mode,
                "tenant_modes": dict(self.egress_policy.config.tenant_modes),
            },
        }


def build_router(service: EwcpCoreService) -> APIRouter:
    router = APIRouter(prefix="/api/ewcp", tags=["ewcp"])

    @router.get("/_status")
    async def read_status() -> dict[str, Any]:
        return service.status()

    return router
