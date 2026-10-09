"""Extension skeleton tests: install() registers, service lifecycle, status route."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ewcp_core import install
from ewcp_core.plugin import EwcpCoreService, build_router


class FakeRegistry:
    def __init__(self) -> None:
        self.middleware_contributors: list[Any] = []
        self.services: list[Any] = []
        self.contributed_routers: list[Any] = []

    def middlewares(self, contributor: Any) -> None:
        self.middleware_contributors.append(contributor)

    def service(self, service: Any) -> None:
        self.services.append(service)

    def routers(self, routers: Any) -> None:
        self.contributed_routers.extend(routers)


def test_install_registers_service_and_router() -> None:
    registry = FakeRegistry()

    install(registry, {})

    assert len(registry.services) == 1
    assert isinstance(registry.services[0], EwcpCoreService)
    assert len(registry.contributed_routers) == 1
    paths = [r.path for r in registry.contributed_routers[0].routes]
    # Task-3 durability routes + Task-6 product surface (list/detail/
    # launch, kernel proxies, gated decisions). The public verify routes
    # are NOT contributed paths — they are host-mounted at
    # app.gateway.routers.ewcp_verify (Task 7; the public namespace is
    # host-reserved upstream).
    for expected in (
        "/api/ewcp/_status",
        "/api/ewcp/runs",
        "/api/ewcp/runs/{execution_run_id}/resume",
        "/api/ewcp/identity",
        "/api/ewcp/runs/{execution_run_id}",
        "/api/ewcp/runs/{execution_run_id}/workrun",
        "/api/ewcp/runs/{execution_run_id}/decisions",
        "/api/ewcp/outcomes",
    ):
        assert expected in paths
    assert "/api/ewcp/verify/{manifest_hash}" not in paths
    assert "/api/ewcp/verify" not in paths
    assert install.__deerflow_api__ == "0.2.0"
    assert install.__deerflow_name__ == "ewcp_core"


def test_disabled_extension_registers_nothing() -> None:
    registry = FakeRegistry()

    install(registry, {"enabled": False})

    assert registry.services == []
    assert registry.contributed_routers == []


def _app(service: EwcpCoreService) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(service))
    return app


def test_status_route_reports_unconfigured() -> None:
    service = EwcpCoreService(config={})
    client = TestClient(_app(service))

    resp = client.get("/api/ewcp/_status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["extension"] == "ewcp_core"
    assert body["kernel_configured"] is False
    assert body["client_started"] is False


@pytest.mark.asyncio
async def test_service_start_creates_client_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EWCP_KERNEL_URL", "http://kernel.test")
    monkeypatch.setenv("EWCP_KERNEL_API_KEY", "ek-service-key")
    service = EwcpCoreService(config={})

    await service.start(deps=None)
    try:
        status = service.status()
        assert status["kernel_configured"] is True
        assert status["api_key_configured"] is True
        assert status["client_started"] is True
        assert service.client is not None
    finally:
        await service.stop()
    assert service.client is None


@pytest.mark.asyncio
async def test_service_start_without_url_stays_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EWCP_KERNEL_URL", raising=False)
    service = EwcpCoreService(config={})

    await service.start(deps=None)
    await service.stop()

    status = service.status()
    assert status["kernel_configured"] is False
    assert status["client_started"] is False


def test_status_route_never_exposes_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "ek-status-must-not-leak-4c2d"
    monkeypatch.setenv("EWCP_KERNEL_URL", "http://kernel.test")
    monkeypatch.setenv("EWCP_KERNEL_API_KEY", secret)
    service = EwcpCoreService(config={})
    client = TestClient(_app(service))

    resp = client.get("/api/ewcp/_status")

    assert resp.status_code == 200
    assert secret not in resp.text


def test_service_resolves_general_recursion_limit() -> None:
    """F1: `general_recursion_limit` in the plugin config overrides the
    UI-matching default (1000); invalid/absent values fall back to it."""
    assert EwcpCoreService(config={}).run_recursion_limit == 1000
    assert EwcpCoreService(config={"general_recursion_limit": 250}).run_recursion_limit == 250
    assert EwcpCoreService(config={"general_recursion_limit": "400"}).run_recursion_limit == 400
    assert EwcpCoreService(config={"general_recursion_limit": "junk"}).run_recursion_limit == 1000
    assert EwcpCoreService(config={"general_recursion_limit": 0}).run_recursion_limit == 1000
    assert EwcpCoreService(config={"general_recursion_limit": -5}).run_recursion_limit == 1000


def test_status_reports_run_recursion_limit() -> None:
    service = EwcpCoreService(config={"general_recursion_limit": 300})
    assert service.status()["general_recursion_limit"] == 300
