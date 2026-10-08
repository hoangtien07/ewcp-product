"""KernelClient contract tests — real kernel wire shapes, offline transport.

The kernel contract these pin (enterprise-work-control-plane@main):
  POST   /tasks                              multipart form; optional
                                             `Idempotency-Key` header ->
                                             200 + `Idempotent-Replay: true`
                                             on replay (app.py:2528)
  POST   /workruns/{id}/decisions            JSON {answer, decision_id?,
                                             decided_by?} -> run view
                                             (app.py:2037)
  GET    /workruns/{id}                      run view (app.py:1748)
  GET    /verify/{manifest_hash}             {manifest, seal_ok, workrun_id}
                                             (app.py:3273) — public, no auth
  GET    /outcomes                           spec wire views (app.py:1599)
  auth:  X-Ewcp-Api-Key header (extension contract name; app.py:614-618)
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from ewcp_core.kernel_client import (
    KernelClient,
    KernelClientConfig,
    KernelNotConfigured,
)

API_KEY = "ek-test-secret-unit-key"
BASE = "http://kernel.test"


def _client(
    handler,
    *,
    api_key: str | None = API_KEY,
    max_attempts: int = 3,
) -> KernelClient:
    return KernelClient(
        KernelClientConfig(kernel_url=BASE, api_key=api_key, max_attempts=max_attempts),
        transport=httpx.MockTransport(handler),
        backoff=lambda _attempt: 0.0,
    )


class _Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _json_response(payload, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


# -- construction / config ---------------------------------------------------


@pytest.mark.asyncio
async def test_client_requires_kernel_url() -> None:
    client = KernelClient(KernelClientConfig(kernel_url=None, api_key=None))
    with pytest.raises(KernelNotConfigured):
        await client.list_outcomes()


@pytest.mark.asyncio
async def test_repr_never_shows_api_key() -> None:
    client = _client(_Recorder([_json_response([])]))
    assert API_KEY not in repr(client)
    assert API_KEY not in str(client)
    await client.aclose()


# -- safe reads: bounded retry -----------------------------------------------


@pytest.mark.asyncio
async def test_get_workrun_path_and_auth_header() -> None:
    run = {"workrun_id": "wr-1", "status": "running"}
    rec = _Recorder([_json_response(run)])
    client = _client(rec)
    body = await client.get_workrun("wr-1")
    await client.aclose()

    assert body == run
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.path == "/workruns/wr-1"
    assert req.headers["x-ewcp-api-key"] == API_KEY


@pytest.mark.asyncio
async def test_list_outcomes_path() -> None:
    rec = _Recorder([_json_response([{"outcome_type": "invoice_recon"}])])
    client = _client(rec)
    body = await client.list_outcomes()
    await client.aclose()

    assert body == [{"outcome_type": "invoice_recon"}]
    assert rec.requests[0].url.path == "/outcomes"


@pytest.mark.asyncio
async def test_verify_manifest_path() -> None:
    payload = {"manifest": {"manifest_hash": "abc"}, "seal_ok": True, "workrun_id": "wr-9"}
    rec = _Recorder([_json_response(payload)])
    client = _client(rec)
    body = await client.verify_manifest("abc")
    await client.aclose()

    assert body == payload
    assert rec.requests[0].url.path == "/verify/abc"


@pytest.mark.asyncio
async def test_get_retries_transient_status_then_succeeds() -> None:
    rec = _Recorder(
        [
            _json_response({"detail": "x"}, status=503),
            _json_response({"detail": "x"}, status=500),
            _json_response({"workrun_id": "wr-2"}),
        ]
    )
    client = _client(rec)
    body = await client.get_workrun("wr-2")
    await client.aclose()

    assert body["workrun_id"] == "wr-2"
    assert len(rec.requests) == 3


@pytest.mark.asyncio
async def test_get_retries_transport_error_then_succeeds() -> None:
    rec = _Recorder([httpx.ConnectError("refused"), _json_response({"workrun_id": "wr-3"})])
    client = _client(rec)
    body = await client.get_workrun("wr-3")
    await client.aclose()

    assert body["workrun_id"] == "wr-3"
    assert len(rec.requests) == 2


@pytest.mark.asyncio
async def test_get_retry_is_bounded() -> None:
    rec = _Recorder([_json_response({}, status=503)] * 10)
    client = _client(rec, max_attempts=3)
    with pytest.raises(httpx.HTTPStatusError):
        await client.list_outcomes()
    await client.aclose()

    assert len(rec.requests) == 3


@pytest.mark.asyncio
async def test_get_no_retry_on_4xx() -> None:
    rec = _Recorder([_json_response({"detail": "nope"}, status=404)] * 3)
    client = _client(rec)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_workrun("missing")
    await client.aclose()

    assert len(rec.requests) == 1


# -- POST /tasks: mutation, retry only under the real idempotency contract ----


@pytest.mark.asyncio
async def test_create_task_posts_multipart_form() -> None:
    run = {"workrun_id": "wr-10", "status": "awaiting_input"}
    rec = _Recorder([_json_response(run)])
    client = _client(rec)
    result = await client.create_task(intent="đối soát hóa đơn", tenant_id="demo")
    await client.aclose()

    assert result.run == run
    assert result.idempotent_replay is False
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/tasks"
    assert "multipart/form-data" in req.headers["content-type"]
    assert b'name="intent"' in req.content
    assert "đối soát hóa đơn".encode() in req.content
    assert b'name="tenant_id"' in req.content
    assert "idempotency-key" not in req.headers


@pytest.mark.asyncio
async def test_create_task_uploads_use_named_slots() -> None:
    rec = _Recorder([_json_response({"workrun_id": "wr-11"})])
    client = _client(rec)
    await client.create_task(
        intent="đối soát",
        tenant_id="demo",
        files={
            "invoices_zip": [("invoices.zip", b"PK\x03\x04fake", "application/zip")],
            "books": [("misa.csv", b"a,b\n1,2", "text/csv")],
        },
    )
    await client.aclose()

    body = rec.requests[0].content
    assert b'name="invoices_zip"; filename="invoices.zip"' in body
    assert b'name="books"; filename="misa.csv"' in body


@pytest.mark.asyncio
async def test_create_task_sends_idempotency_key_and_parses_replay() -> None:
    rec = _Recorder([_json_response({"workrun_id": "wr-12"}, headers={"Idempotent-Replay": "true"})])
    client = _client(rec)
    result = await client.create_task(intent="đối soát", idempotency_key="key-1")
    await client.aclose()

    assert rec.requests[0].headers["idempotency-key"] == "key-1"
    assert result.idempotent_replay is True


@pytest.mark.asyncio
async def test_create_task_without_key_is_never_retried() -> None:
    rec = _Recorder([httpx.ConnectError("refused"), _json_response({"workrun_id": "wr-x"})])
    client = _client(rec)
    with pytest.raises(httpx.TransportError):
        await client.create_task(intent="đối soát")
    await client.aclose()

    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_create_task_without_key_not_retried_on_5xx() -> None:
    rec = _Recorder([_json_response({}, status=503)] * 3)
    client = _client(rec)
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_task(intent="đối soát")
    await client.aclose()

    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_create_task_with_key_retries_and_replay_is_safe() -> None:
    """With an Idempotency-Key a retried POST /tasks is safe: the kernel's
    idempotency_keys table dedupes (tenant, key) -> same workrun_id and
    `Idempotent-Replay: true` (kernel/persistence.py:105-111)."""
    rec = _Recorder(
        [
            httpx.ConnectError("refused"),
            _json_response({"workrun_id": "wr-13"}, headers={"Idempotent-Replay": "true"}),
        ]
    )
    client = _client(rec)
    result = await client.create_task(intent="đối soát", idempotency_key="key-2")
    await client.aclose()

    assert result.run["workrun_id"] == "wr-13"
    assert result.idempotent_replay is True
    assert len(rec.requests) == 2
    assert rec.requests[1].headers["idempotency-key"] == "key-2"


# -- POST /workruns/{id}/decisions: mutation, no kernel replay contract ------


@pytest.mark.asyncio
async def test_decide_posts_json_body() -> None:
    run = {"workrun_id": "wr-20", "status": "verified"}
    rec = _Recorder([_json_response(run)])
    client = _client(rec)
    body = await client.decide("wr-20", answer="approve")
    await client.aclose()

    assert body == run
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/workruns/wr-20/decisions"
    assert req.headers["content-type"].startswith("application/json")
    assert json.loads(req.content) == {"answer": "approve"}


@pytest.mark.asyncio
async def test_decide_forwards_decision_id_and_decided_by() -> None:
    rec = _Recorder([_json_response({"workrun_id": "wr-21"})])
    client = _client(rec)
    await client.decide("wr-21", answer="xác nhận đúng", decision_id="d-1", decided_by="kt@x.vn")
    await client.aclose()

    assert json.loads(rec.requests[0].content) == {
        "answer": "xác nhận đúng",
        "decision_id": "d-1",
        "decided_by": "kt@x.vn",
    }


@pytest.mark.asyncio
async def test_decide_is_never_retried() -> None:
    """The kernel decisions endpoint has NO idempotency contract
    (app.py:2037-2260 — no Idempotency-Key handling), so the client must
    never silently re-apply a decision."""
    rec = _Recorder([httpx.ConnectError("refused"), _json_response({"workrun_id": "wr-22"})])
    client = _client(rec)
    with pytest.raises(httpx.TransportError):
        await client.decide("wr-22", answer="approve")
    await client.aclose()

    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_decide_not_retried_on_5xx() -> None:
    rec = _Recorder([_json_response({}, status=503)] * 3)
    client = _client(rec)
    with pytest.raises(httpx.HTTPStatusError):
        await client.decide("wr-23", answer="approve")
    await client.aclose()

    assert len(rec.requests) == 1


# -- the kernel API key must never appear in logs/errors ----------------------


@pytest.mark.asyncio
async def test_api_key_never_in_logs_or_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    secret = "ek-must-never-appear-7f3a"
    rec = _Recorder([_json_response({"detail": "boom"}, status=500)] * 5)
    client = _client(rec, api_key=secret)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.list_outcomes()
    await client.aclose()

    for record in caplog.records:
        assert secret not in record.getMessage()
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_transport_error_does_not_leak_key(caplog: pytest.LogCaptureFixture) -> None:
    secret = "ek-must-never-appear-9b1c"
    rec = _Recorder([httpx.ConnectError("kernel unreachable")] * 5)
    client = _client(rec, api_key=secret)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(httpx.TransportError) as exc_info:
            await client.list_outcomes()
    await client.aclose()

    for record in caplog.records:
        assert secret not in record.getMessage()
    assert secret not in str(exc_info.value)
