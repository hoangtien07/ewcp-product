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
    BudgetDenied,
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


# -- A6-07 workrun_status list projection ------------------------------------


@pytest.mark.asyncio
async def test_list_workrun_statuses_batched_query() -> None:
    """A6-07: ONE GET /workruns with the caller's bound id set as a
    comma-joined `workrun_ids` param — never a per-row fetch."""
    payload = [
        {
            "workrun_id": "wr-1",
            "status": "awaiting_input",
            "pending_decision": True,
            "last_event_at": 1700000001.5,
        },
        {
            "workrun_id": "wr-2",
            "status": "running",
            "pending_decision": False,
            "last_event_at": 1700000000.0,
        },
    ]
    rec = _Recorder([_json_response(payload)])
    client = _client(rec)
    body = await client.list_workrun_statuses(["wr-1", "wr-2"], tenant_id="t-1")
    await client.aclose()

    assert body == payload
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.path == "/workruns"
    params = dict(req.url.params)
    assert params["workrun_ids"] == "wr-1,wr-2"
    assert params["tenant_id"] == "t-1"
    assert req.headers["x-ewcp-api-key"] == API_KEY


@pytest.mark.asyncio
async def test_list_workrun_statuses_omits_tenant_when_unset() -> None:
    """Keyed kernels derive tenant from the api key — no param sent."""
    rec = _Recorder([_json_response([])])
    client = _client(rec)
    await client.list_workrun_statuses(["wr-1"])
    await client.aclose()
    params = dict(rec.requests[0].url.params)
    assert params == {"workrun_ids": "wr-1"}


@pytest.mark.asyncio
async def test_list_workrun_statuses_retries_transient() -> None:
    """Read path rides bounded retry like the other safe reads."""
    rec = _Recorder(
        [
            _json_response({"detail": "down"}, status=503),
            _json_response([{"workrun_id": "wr-1", "status": "running", "pending_decision": False, "last_event_at": 1.0}]),
        ]
    )
    client = _client(rec)
    body = await client.list_workrun_statuses(["wr-1"])
    await client.aclose()
    assert len(body) == 1
    assert len(rec.requests) == 2


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
async def test_create_task_forwards_context_fields() -> None:
    rec = _Recorder([_json_response({"workrun_id": "wr-13"})])
    client = _client(rec)
    await client.create_task(
        intent="đối soát",
        fields={"mst": "0101", "ky": "2026-Q3"},
    )
    await client.aclose()

    body = rec.requests[0].content
    assert b'name="mst"' in body and b"0101" in body
    assert b'name="ky"' in body and b"2026-Q3" in body


@pytest.mark.asyncio
async def test_run_outcome_posts_to_outcome_path_with_slots_and_context() -> None:
    rec = _Recorder([_json_response({"workrun_id": "wr-20", "status": "running"})])
    client = _client(rec)
    view = await client.run_outcome(
        "invoice_recon",
        tenant_id="demo",
        fields={"mst": "0101"},
        files={"invoices_zip": [("inv.zip", b"PKfake", "application/zip")]},
    )
    await client.aclose()

    assert view["workrun_id"] == "wr-20"
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/outcomes/invoice_recon/run"
    assert b'name="tenant_id"' in req.content
    assert b'name="mst"' in req.content
    assert b'name="invoices_zip"; filename="inv.zip"' in req.content


@pytest.mark.asyncio
async def test_run_outcome_is_never_retried() -> None:
    rec = _Recorder([httpx.ConnectError("refused"), _json_response({"workrun_id": "wr-x"})])
    client = _client(rec)
    with pytest.raises(httpx.TransportError):
        await client.run_outcome("invoice_recon")
    await client.aclose()

    assert len(rec.requests) == 1


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


# -- X-Ewcp-Actor binding (C10) -----------------------------------------------
#
# The actor is a trusted M2M assertion the kernel binds into the audit
# principal (<actor>@tenant:<tenant>) — for governed writes it becomes
# ProposedAction.requester, for decisions/accepts the recorded decider.
# The value is minted by the EXTENSION from the authenticated session, so
# every mutation that reaches an actor-aware kernel endpoint accepts an
# explicit `actor` param. Callers never see the header construction, and
# nothing inbound (request headers, form fields, tool args) can set it.


@pytest.mark.asyncio
async def test_create_task_sends_actor_header() -> None:
    rec = _Recorder([_json_response({"workrun_id": "wr-30"})])
    client = _client(rec)
    await client.create_task(intent="đối soát", idempotency_key="k", actor="user:u-7")
    await client.aclose()

    assert rec.requests[0].headers["x-ewcp-actor"] == "user:u-7"


@pytest.mark.asyncio
async def test_run_outcome_sends_actor_header() -> None:
    rec = _Recorder([_json_response({"workrun_id": "wr-31"})])
    client = _client(rec)
    await client.run_outcome("invoice_recon", actor="user:u-7")
    await client.aclose()

    assert rec.requests[0].headers["x-ewcp-actor"] == "user:u-7"


@pytest.mark.asyncio
async def test_actor_header_absent_when_unset() -> None:
    """Without a bound user the header must not be minted at all — an
    anon/unauthenticated call degrades to the tenant principal kernel-side,
    never to a guessed identity."""
    rec = _Recorder(
        [
            _json_response({"workrun_id": "wr-32"}),
            _json_response({"workrun_id": "wr-33"}),
            _json_response({"workrun_id": "wr-34"}),
        ]
    )
    client = _client(rec)
    await client.create_task(intent="x")
    await client.run_outcome("invoice_recon")
    await client.invoke_outcome("invoice_recon", idempotency_key="k-9")
    await client.aclose()

    for req in rec.requests:
        assert "x-ewcp-actor" not in req.headers


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


# -- budget admission (A3 Task 5; kernel /budget/* surface) -------------------


def _admission_payload(**overrides):
    body = {
        "admission_id": "adm-1",
        "execution_run_id": "wr-1",
        "cap_usd": "5.00",
        "spent_usd": "0.10",
        "reserved_usd": "0.20",
        "reserve_usd": "0.10",
        "expires_at": 1700.0,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_admit_budget_posts_contract_and_returns_admission() -> None:
    rec = _Recorder([_json_response(_admission_payload(), status=201)])
    client = _client(rec)

    result = await client.admit_budget(
        execution_run_id="wr-1",
        cap_usd="5.00",
        reserve_usd="0.10",
        tenant_id="acme",
        idempotency_key="budget:wr-1:abc",
    )
    await client.aclose()

    (req,) = rec.requests
    assert req.method == "POST"
    assert req.url.path == "/budget/admissions"
    assert json.loads(req.content) == {
        "execution_run_id": "wr-1",
        "cap_usd": "5.00",
        "reserve_usd": "0.10",
        "tenant_id": "acme",
    }
    assert req.headers["idempotency-key"] == "budget:wr-1:abc"
    assert result.admission["admission_id"] == "adm-1"
    assert result.idempotent_replay is False


@pytest.mark.asyncio
async def test_admit_budget_402_raises_budget_denied_with_detail() -> None:
    detail = {"error": "budget_exceeded", "message": "over cap", "cap_usd": "5.00"}
    rec = _Recorder([_json_response({"detail": detail}, status=402)])
    client = _client(rec)

    with pytest.raises(BudgetDenied) as exc_info:
        await client.admit_budget(execution_run_id="wr-1", cap_usd="5.00", reserve_usd="0.10")
    await client.aclose()

    assert exc_info.value.detail["error"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_admit_budget_retry_reuses_idempotency_key() -> None:
    """A transport retry must send the SAME Idempotency-Key so the kernel
    dedupes to one reservation instead of double-reserving."""
    rec = _Recorder(
        [
            httpx.ConnectError("refused"),
            _json_response(_admission_payload(), status=201),
        ]
    )
    client = _client(rec)

    result = await client.admit_budget(
        execution_run_id="wr-1",
        cap_usd="5.00",
        reserve_usd="0.10",
        idempotency_key="budget:wr-1:k1",
    )
    await client.aclose()

    assert len(rec.requests) == 2
    assert [r.headers["idempotency-key"] for r in rec.requests] == ["budget:wr-1:k1"] * 2
    assert result.admission["admission_id"] == "adm-1"


@pytest.mark.asyncio
async def test_admit_budget_without_key_is_never_retried() -> None:
    rec = _Recorder([httpx.ConnectError("refused"), _json_response(_admission_payload(), status=201)])
    client = _client(rec)

    with pytest.raises(httpx.TransportError):
        await client.admit_budget(execution_run_id="wr-1", cap_usd="5.00", reserve_usd="0.10")
    await client.aclose()

    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_admit_budget_idempotent_replay_flag() -> None:
    rec = _Recorder([_json_response(_admission_payload(), status=200, headers={"Idempotent-Replay": "true"})])
    client = _client(rec)

    result = await client.admit_budget(
        execution_run_id="wr-1",
        cap_usd="5.00",
        reserve_usd="0.10",
        idempotency_key="budget:wr-1:k2",
    )
    await client.aclose()

    assert result.idempotent_replay is True


@pytest.mark.asyncio
async def test_settle_budget_posts_metered_charge() -> None:
    rec = _Recorder([_json_response({"admission_id": "adm-1", "settled": True, "charged_usd": "0.11", "overshoot": False})])
    client = _client(rec)

    body = await client.settle_budget("adm-1", tokens=550, usd="0.11", model="gpt-x")
    await client.aclose()

    (req,) = rec.requests
    assert req.method == "POST"
    assert req.url.path == "/budget/admissions/adm-1/settle"
    assert json.loads(req.content) == {"tokens": 550, "usd": "0.11", "model": "gpt-x"}
    assert body["settled"] is True


@pytest.mark.asyncio
async def test_settle_budget_retries_transport_errors() -> None:
    """Settle is idempotent by admission_id on the kernel side — a retried
    transport failure returns the recorded charge, never double-charges."""
    rec = _Recorder(
        [
            httpx.ConnectError("refused"),
            _json_response({"admission_id": "adm-1", "settled": True, "replayed": True, "charged_usd": "0.11"}),
        ]
    )
    client = _client(rec)

    body = await client.settle_budget("adm-1", tokens=550, usd="0.11")
    await client.aclose()

    assert len(rec.requests) == 2
    assert body["settled"] is True


@pytest.mark.asyncio
async def test_release_budget_posts_release() -> None:
    rec = _Recorder([_json_response({"admission_id": "adm-1", "released": True})])
    client = _client(rec)

    body = await client.release_budget("adm-1")
    await client.aclose()

    (req,) = rec.requests
    assert req.method == "POST"
    assert req.url.path == "/budget/admissions/adm-1/release"
    assert body["released"] is True


@pytest.mark.asyncio
async def test_get_budget_account_is_a_safe_read() -> None:
    rec = _Recorder([_json_response({"execution_run_id": "wr-1", "cap_usd": "5.00", "spent_usd": "0.10", "reserved_usd": "0"})])
    client = _client(rec)

    body = await client.get_budget_account("wr-1")
    await client.aclose()

    (req,) = rec.requests
    assert req.method == "GET"
    assert req.url.path == "/budget/accounts/wr-1"
    assert body["cap_usd"] == "5.00"
