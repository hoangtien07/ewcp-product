"""EWCP Product Core — kernel HTTP client + egress policy extension."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deerflow_extension_api import ExtensionInstall, ExtensionRegistry, extension

from ewcp_core.egress_policy import EgressMiddlewareContributor
from ewcp_core.plugin import EwcpCoreService, build_router


@extension(api="0.2.0", name="ewcp_core")
def install(registry: ExtensionRegistry, config: Mapping[str, Any]) -> None:
    """Register the kernel client service, status router, and egress hooks."""
    if config.get("enabled", True) is False:
        return

    service = EwcpCoreService(config=config)
    registry.service(service)
    registry.routers((build_router(service),))
    registry.middlewares(service)
    registry.middlewares(EgressMiddlewareContributor(service.egress_policy))


_entry_point: ExtensionInstall = install
