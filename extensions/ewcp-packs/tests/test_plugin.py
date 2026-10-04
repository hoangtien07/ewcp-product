"""Extension unit tests: service lifecycle + router forwarding."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ewcp_packs.plugin import EwcpKernelService, build_router


def _app(service: EwcpKernelService) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(service))
    return app


@pytest.mark.asyncio
async def test_service_reports_missing_kernel() -> None:
    service = EwcpKernelService()
    # in the extension test env there is no `ewcp` package on path
    service._load_kernel()
    status = service.status()
    assert status["extension"] == "ewcp_packs"
    assert status["kernel_loaded"] is False
    assert "ewcp" in (status["kernel_error"] or "")


def test_status_route_serves_without_kernel() -> None:
    client = TestClient(_app(EwcpKernelService()))
    resp = client.get("/api/ewcp/_status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["extension"] == "ewcp_packs"
    assert "kernel_loaded" in body


def test_forward_returns_503_when_kernel_absent() -> None:
    client = TestClient(_app(EwcpKernelService()))
    resp = client.get("/api/ewcp/workruns")
    assert resp.status_code == 503
    assert "kernel not loaded" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_forward_reaches_mounted_kernel_app() -> None:
    """When a kernel-like ASGI app is mounted, requests are forwarded."""
    kernel = FastAPI()

    @kernel.get("/workruns")
    async def workruns() -> dict:
        return {"items": []}

    service = EwcpKernelService()
    service._client = __import__("httpx").AsyncClient(
        transport=__import__("httpx").ASGITransport(app=kernel),
        base_url="http://ewcp.kernel",
    )
    service.kernel_loaded = True
    client = TestClient(_app(service))
    resp = client.get("/api/ewcp/workruns")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}
    await service.aclose()
