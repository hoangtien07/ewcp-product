"""EWCP governed-outcome packs — DeerFlow extension (PA-A product fork)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deerflow_extension_api import ExtensionInstall, ExtensionRegistry, extension

from ewcp_packs.plugin import EwcpKernelService, build_router


@extension(api="0.2.0", name="ewcp_packs")
def install(registry: ExtensionRegistry, config: Mapping[str, Any]) -> None:
    """Register EWCP kernel service + governed fast-path routers."""
    if config.get("enabled", True) is False:
        return

    service = EwcpKernelService(config=config)
    registry.service(service)
    registry.routers((build_router(service),))


_entry_point: ExtensionInstall = install
