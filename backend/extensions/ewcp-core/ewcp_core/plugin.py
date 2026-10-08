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

from deerflow_extension_api import AgentBuildContext, AgentScope, ExtensionRuntimeDeps, HostPolicySnapshot, MiddlewarePlacement, Placement
from fastapi import APIRouter

from .kernel_client import KernelClient, KernelClientConfig
from .model_policy import BudgetAdmissionMiddleware, KernelBudgetClient, ModelPolicyConfig


class EwcpCoreService:
    """Owns the shared kernel HTTP client for the Gateway lifetime.

    Also a MiddlewareContributor: `contribute_middlewares` installs the
    budget-admission gate (A3 Task 5) at Placement.MODEL_PHYSICAL with
    `intercepting=True` — deny decisions must propagate, not be
    swallowed by the host's fail-open isolation wrapper. The middleware
    resolves the live client per call, so kernel-absent deployments get
    the local policy behavior instead of a broken build."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config: Mapping[str, Any] = config or {}
        self._resolved = KernelClientConfig.resolve(self.config)
        self._model_policy = ModelPolicyConfig.resolve(self.config)
        self._client: KernelClient | None = None

    @property
    def client(self) -> KernelClient | None:
        return self._client

    async def start(self, deps: ExtensionRuntimeDeps) -> None:
        if self._resolved.kernel_url:
            self._client = KernelClient(self._resolved)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def contribute_middlewares(self, app_store: Any, ctx: AgentBuildContext) -> tuple[MiddlewarePlacement, ...]:
        policy = ctx.policy if isinstance(getattr(ctx, "policy", None), HostPolicySnapshot) else HostPolicySnapshot()
        middleware = BudgetAdmissionMiddleware(
            KernelBudgetClient(lambda: self._client),
            self._model_policy,
            policy,
        )
        return (MiddlewarePlacement(middleware, Placement.MODEL_PHYSICAL, AgentScope.BOTH, intercepting=True),)

    def status(self) -> dict[str, Any]:
        return {
            "extension": "ewcp_core",
            "kernel_url": self._resolved.kernel_url,
            "kernel_configured": self._resolved.kernel_url is not None,
            "api_key_configured": self._resolved.api_key is not None,
            "client_started": self._client is not None,
            "budget_cap_usd": str(self._model_policy.cap_usd) if self._model_policy.cap_usd is not None else None,
            "budget_admission_enabled": self._model_policy.cap_usd is not None,
            "general_on_policy_unavailable": self._model_policy.general_on_policy_unavailable,
        }


def build_router(service: EwcpCoreService) -> APIRouter:
    router = APIRouter(prefix="/api/ewcp", tags=["ewcp"])

    @router.get("/_status")
    async def read_status() -> dict[str, Any]:
        return service.status()

    return router
