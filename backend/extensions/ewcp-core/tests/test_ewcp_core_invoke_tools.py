"""WP-A5a Wave-2 — ewcp_invoke / ewcp_capabilities consumer tools.

Contract pinned (kernel main@aeeb69e, docs/specs/006-capability-invoke-contract.md):
  GET  /outcomes                        spec wire views; each item carries a
                                        `capability` descriptor block
  GET  /outcomes/{type}                 single descriptor; 404 unknown_capability
  POST /outcomes/{type}/run             multipart: contract_version=1,
                                        tenant_id, invocation_id,
                                        execution_run_id, declared context
                                        fields + file slots; Idempotency-Key
                                        header -> replay yields 200 +
                                        Idempotent-Replay: true; same key +
                                        different payload -> 409
                                        idempotency_payload_mismatch
  errors: {error, message, ...} structured bodies with stable codes

Tests mock the HTTP boundary (httpx.MockTransport) — the contract parsing
(descriptor fetch, required-context check, slot validation) runs for real.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from deerflow.runtime.user_context import reset_current_user, set_current_user

from ewcp_core import invoke_tools
from ewcp_core.egress_policy import EgressPolicy
from ewcp_core.invoke_tools import EwcpInvokeToolsMiddleware, InvokeDeps, InvokeToolsContributor
from ewcp_core.kernel_client import KernelClient, KernelClientConfig, KernelInvokeError

API_KEY = "ek-test-secret-unit-key"
BASE = "http://kernel.test"

_DESCRIPTOR: dict[str, Any] = {
    "capability_id": "pack:invoice_recon",
    "contract_version": "1",
    "kind": "pack",
    "summary": "Đối soát hóa đơn với sổ sách",
    "when_to_use": "Reconcile invoices against books",
    "side_effect_class": "workspace_write",
    "input_schema": {
        "inputs": [
            {"name": "invoices_zip", "accept": ".zip", "label_vn": "ZIP hóa đơn", "required": True, "multiple": False},
            {"name": "books", "accept": ".csv", "label_vn": "Sổ sách", "required": True, "multiple": False},
        ],
        "context": [
            {"name": "period", "type": "str", "required": True},
            {"name": "currency", "type": "str", "required": False, "default": "VND"},
        ],
        "required_context": ["period"],
    },
    "output_schema": {"kind": "run_view"},
    "idempotency": {"key_header": "Idempotency-Key", "ttl_s": 86400},
}

_RUN_VIEW: dict[str, Any] = {
    "run_id": "wr-inv-1",
    "status": "succeeded",
    "capability_id": "pack:invoice_recon",
    "contract_version": "1",
    "invocation_id": "inv-abc",
    "execution_run_id": "er_x",
}

_EXTERNAL_WRITE_DESCRIPTOR: dict[str, Any] = {
    **_DESCRIPTOR,
    "capability_id": "pack:create_draft_po",
    "side_effect_class": "external_write",
    "input_schema": {
        **_DESCRIPTOR["input_schema"],
        "required_context": [],
    },
}


def _client(handler, *, api_key: str | None = API_KEY, max_attempts: int = 3) -> KernelClient:
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


def _form_text(req: httpx.Request) -> str:
    return req.content.decode("utf-8", errors="replace")


def _deps(client, *, store=None, tenant_id=None, paths=None) -> InvokeDeps:
    return InvokeDeps(
        client_getter=lambda: client,
        store_getter=lambda: store,
        tenant_id_getter=lambda: tenant_id,
        paths_getter=paths or _missing_paths,
    )


def _missing_paths():
    raise AssertionError("paths_getter should not be called in this test")


class _FakePaths:
    def __init__(self, root: Path, *, host_root: Path | None = None) -> None:
        self._root = root
        self._host_root = host_root

    def host_sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        return str(self._host_root if self._host_root is not None else self._root)

    def sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        return self._root


class _FakeStore:
    def __init__(self, records: list[Any]) -> None:
        self.records = records

    async def list_for_thread(self, thread_id: str, *, limit: int = 50) -> list[Any]:
        assert thread_id == "t-1"
        return list(self.records)


def _descriptor_then_run(run_payload: dict | None = None, run_status: int = 200, run_headers: dict | None = None):
    """Recorder for the descriptor GET followed by the invoke POST."""
    return _Recorder(
        [
            _json_response(_DESCRIPTOR),
            _json_response(run_payload if run_payload is not None else _RUN_VIEW, status=run_status, headers=run_headers),
        ]
    )


CTX = {"thread_id": "t-1", "run_id": "r-1", "user_id": "u-1"}

# ---------------------------------------------------------------------------
# KernelClient.invoke_outcome — wire shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_posts_contract_fields() -> None:
    rec = _Recorder([_json_response(_RUN_VIEW)])
    client = _client(rec)

    result = await client.invoke_outcome(
        "invoice_recon",
        context={"period": "2026-09", "taxpayer_vat": False, "lines": [{"a": 1}]},
        files={"invoices_zip": [("inv.zip", b"PK\x03\x04", "application/zip")]},
        tenant_id="demo",
        invocation_id="inv-1",
        execution_run_id="er_1",
        idempotency_key="key-1",
    )

    assert result.run == _RUN_VIEW
    assert result.idempotent_replay is False
    req = rec.requests[0]
    assert req.url.path == "/outcomes/invoice_recon/run"
    assert req.method == "POST"
    assert "multipart/form-data" in req.headers["content-type"]
    assert req.headers["idempotency-key"] == "key-1"
    assert req.headers["x-ewcp-api-key"] == API_KEY
    body = _form_text(req)
    assert 'name="contract_version"' in body and "1\r\n" in body or "1\n" in body
    assert 'name="tenant_id"' in body and "demo" in body
    assert 'name="invocation_id"' in body and "inv-1" in body
    assert 'name="execution_run_id"' in body and "er_1" in body
    assert 'name="period"' in body and "2026-09" in body
    # typed scalars render per contract: bool lowercase, dict as JSON
    assert 'name="taxpayer_vat"' in body and "false" in body
    assert 'name="lines"' in body and '[{"a": 1}]' in body
    # file lands under the declared slot name, not a generic field
    assert 'name="invoices_zip"' in body and "inv.zip" in body
    assert b"PK\x03\x04" in req.content


@pytest.mark.asyncio
async def test_invoke_retries_reuse_the_same_idempotency_key() -> None:
    rec = _Recorder([_json_response({"detail": "busy"}, status=503), _json_response(_RUN_VIEW)])
    client = _client(rec)

    result = await client.invoke_outcome("invoice_recon", context={"period": "p"}, idempotency_key="key-stable")

    assert result.run == _RUN_VIEW
    assert len(rec.requests) == 2
    assert rec.requests[0].headers["idempotency-key"] == "key-stable"
    assert rec.requests[1].headers["idempotency-key"] == "key-stable"

    # httpx mints a fresh multipart boundary per request; the payload is
    # byte-identical once the boundary is stripped.
    def _norm(req: httpx.Request) -> bytes:
        boundary = req.headers["content-type"].rsplit("boundary=", 1)[-1].encode()
        return req.content.replace(boundary, b"")

    assert _norm(rec.requests[0]) == _norm(rec.requests[1])


@pytest.mark.asyncio
async def test_invoke_replay_flag_from_header() -> None:
    rec = _Recorder([_json_response(_RUN_VIEW, headers={"Idempotent-Replay": "true"})])
    client = _client(rec)

    result = await client.invoke_outcome("invoice_recon", idempotency_key="key-2")

    assert result.idempotent_replay is True


@pytest.mark.asyncio
async def test_invoke_outcome_sends_actor_header() -> None:
    """C10: `actor` rides as X-Ewcp-Actor alongside the Idempotency-Key —
    the kernel binds it into ProposedAction.requester for governed
    writes."""
    rec = _Recorder([_json_response(_RUN_VIEW)])
    client = _client(rec)

    await client.invoke_outcome(
        "invoice_recon",
        actor="user:u-7",
        idempotency_key="key-actor",
    )

    req = rec.requests[0]
    assert req.headers["x-ewcp-actor"] == "user:u-7"
    assert req.headers["idempotency-key"] == "key-actor"


@pytest.mark.asyncio
async def test_invoke_409_payload_mismatch_is_correction_guided() -> None:
    rec = _Recorder([_json_response({"detail": "mismatch", "error": "idempotency_payload_mismatch", "message": "same key, different payload"}, status=409)])
    client = _client(rec)

    with pytest.raises(KernelInvokeError) as info:
        await client.invoke_outcome("invoice_recon", idempotency_key="key-3")

    assert info.value.status == 409
    assert info.value.error == "idempotency_payload_mismatch"
    assert info.value.message == "same key, different payload"
    assert info.value.guidance == "correct"


@pytest.mark.asyncio
async def test_invoke_409_unsupported_version_is_fatal() -> None:
    rec = _Recorder([_json_response({"detail": "nope", "error": "unsupported_version", "message": "contract v2 only"}, status=409)])
    client = _client(rec)

    with pytest.raises(KernelInvokeError) as info:
        await client.invoke_outcome("invoice_recon", idempotency_key="key-4")

    assert info.value.error == "unsupported_version"
    assert info.value.guidance == "fatal"


@pytest.mark.asyncio
async def test_invoke_422_carries_missing_fields() -> None:
    rec = _Recorder(
        [
            _json_response(
                {"detail": "missing", "error": "missing_required_input", "message": "missing inputs", "missing": ["period"]},
                status=422,
            )
        ]
    )
    client = _client(rec)

    with pytest.raises(KernelInvokeError) as info:
        await client.invoke_outcome("invoice_recon", idempotency_key="key-5")

    assert info.value.error == "missing_required_input"
    assert info.value.extra == {"missing": ["period"]}
    assert info.value.guidance == "correct"


@pytest.mark.asyncio
async def test_invoke_404_unknown_capability() -> None:
    rec = _Recorder([_json_response({"detail": "no", "error": "unknown_capability", "message": "no such outcome"}, status=404)])
    client = _client(rec)

    with pytest.raises(KernelInvokeError) as info:
        await client.invoke_outcome("nonsense", idempotency_key="key-6")

    assert info.value.status == 404
    assert info.value.error == "unknown_capability"
    assert info.value.guidance == "correct"


@pytest.mark.asyncio
async def test_get_outcome_descriptor_gets_descriptor() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR)])
    client = _client(rec)

    descriptor = await client.get_outcome_descriptor("invoice_recon")

    assert descriptor == _DESCRIPTOR
    assert rec.requests[0].method == "GET"
    assert rec.requests[0].url.path == "/outcomes/invoice_recon"


@pytest.mark.asyncio
async def test_get_outcome_descriptor_404_maps_to_invoke_error() -> None:
    rec = _Recorder([_json_response({"error": "unknown_capability", "message": "no such outcome"}, status=404)])
    client = _client(rec)

    with pytest.raises(KernelInvokeError) as info:
        await client.get_outcome_descriptor("nonsense")

    assert info.value.error == "unknown_capability"


# ---------------------------------------------------------------------------
# ewcp_capabilities tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capabilities_lists_descriptors_compactly() -> None:
    outcomes_item = {
        "outcome_type": "invoice_recon",
        "capability": _DESCRIPTOR,
    }
    rec = _Recorder([_json_response([outcomes_item])])
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.capabilities_impl(deps, CTX))

    assert out["ok"] is True
    assert len(out["capabilities"]) == 1
    cap = out["capabilities"][0]
    assert cap["capability_id"] == "pack:invoice_recon"
    assert cap["outcome_type"] == "invoice_recon"
    assert cap["side_effect_class"] == "workspace_write"
    assert cap["contract_version"] == "1"
    assert cap["inputs"][0]["name"] == "invoices_zip"
    assert cap["context"][0]["name"] == "period"
    assert cap["required_context"] == ["period"]


@pytest.mark.asyncio
async def test_capabilities_single_descriptor() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR)])
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.capabilities_impl(deps, CTX, outcome_type="invoice_recon"))

    assert out["ok"] is True
    assert out["descriptor"]["capability_id"] == "pack:invoice_recon"
    assert rec.requests[0].url.path == "/outcomes/invoice_recon"


@pytest.mark.asyncio
async def test_capabilities_kernel_not_configured() -> None:
    out = json.loads(await invoke_tools.capabilities_impl(_deps(None), CTX))
    assert out["ok"] is False
    assert out["error"] == "kernel_not_configured"
    assert out["guidance"] == "fatal"


# ---------------------------------------------------------------------------
# ewcp_invoke tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_success_returns_run_view_and_correlation() -> None:
    rec = _descriptor_then_run()
    deps = _deps(_client(rec), tenant_id="demo")

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            CTX,
            outcome_type="invoice_recon",
            context={"period": "2026-09"},
        )
    )

    assert out["ok"] is True
    assert out["capability_id"] == "pack:invoice_recon"
    assert out["idempotent_replay"] is False
    assert out["run"]["status"] == "succeeded"
    assert out["idempotency_key"].startswith("invoke-")
    post = rec.requests[1]
    assert post.url.path == "/outcomes/invoice_recon/run"
    body = _form_text(post)
    assert 'name="contract_version"' in body
    assert 'name="invocation_id"' in body and out["invocation_id"].startswith("inv-")
    assert 'name="execution_run_id"' in body and "r-1" in body  # run_id fallback
    assert 'name="period"' in body and "2026-09" in body
    assert 'name="tenant_id"' in body and "demo" in body


@pytest.mark.asyncio
async def test_invoke_binds_execution_run_record_for_thread() -> None:
    record = SimpleNamespace(execution_run_id="er_42", run_id="r-1", thread_id="t-1")
    rec = _descriptor_then_run()
    deps = _deps(_client(rec), store=_FakeStore([record]))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    assert 'name="execution_run_id"' in _form_text(rec.requests[1]) and "er_42" in _form_text(rec.requests[1])


@pytest.mark.asyncio
async def test_invoke_governed_workrun_identity_wins() -> None:
    record = SimpleNamespace(execution_run_id="er_42", run_id="r-1", thread_id="t-1")
    rec = _descriptor_then_run()
    deps = _deps(_client(rec), store=_FakeStore([record]))
    ctx = dict(CTX, kernel={"workrun_id": "wr-gov-9"})

    await invoke_tools.invoke_impl(deps, ctx, outcome_type="invoice_recon", context={"period": "p"})

    assert "wr-gov-9" in _form_text(rec.requests[1])
    assert "er_42" not in _form_text(rec.requests[1])


@pytest.mark.asyncio
async def test_invoke_descriptor_is_cached_not_refetched() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR), _json_response(_RUN_VIEW), _json_response(_RUN_VIEW)])
    deps = _deps(_client(rec))

    await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"})
    await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"})

    assert len(rec.requests) == 3  # one GET + two POSTs
    assert rec.requests[1].url.path.endswith("/run")


@pytest.mark.asyncio
async def test_invoke_mints_fresh_ids_per_call() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR), _json_response(_RUN_VIEW), _json_response(_RUN_VIEW)])
    deps = _deps(_client(rec))

    await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"})
    await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"})

    assert rec.requests[1].headers["idempotency-key"] != rec.requests[2].headers["idempotency-key"]


@pytest.mark.asyncio
async def test_invoke_caller_supplied_idempotency_key_passes_through() -> None:
    rec = _descriptor_then_run()
    deps = _deps(_client(rec))

    await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}, idempotency_key="agent-key-1")

    assert rec.requests[1].headers["idempotency-key"] == "agent-key-1"


@pytest.mark.asyncio
async def test_invoke_binds_actor_from_runtime_user() -> None:
    """C10: the agent's invoke asserts the session-bound user on
    X-Ewcp-Actor so ProposedAction.requester binds a product identity —
    the model cannot pick or override it (actor is not a tool arg)."""
    rec = _descriptor_then_run()
    deps = _deps(_client(rec))

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            CTX,
            outcome_type="invoice_recon",
            context={"period": "p"},
        )
    )

    assert out["ok"] is True
    assert rec.requests[1].headers["x-ewcp-actor"] == "user:u-1"


@pytest.mark.asyncio
async def test_invoke_actor_anon_scoped_without_user_context() -> None:
    """C10: with no session identity the header degrades to the
    contextvar's anon scope (`user:default`) — never attacker-chosen."""
    rec = _descriptor_then_run()
    deps = _deps(_client(rec))
    ctx = {"thread_id": "t-1", "run_id": "r-1"}  # no user_id

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            ctx,
            outcome_type="invoice_recon",
            context={"period": "p"},
        )
    )

    assert out["ok"] is True
    assert rec.requests[1].headers["x-ewcp-actor"] == "user:default"


@pytest.mark.asyncio
async def test_invoke_actor_ignores_model_supplied_context_keys() -> None:
    """C10 anti-impersonation: capability `context` values are form
    fields — a model-crafted key masquerading as the actor cannot reach
    the header."""
    rec = _descriptor_then_run()
    deps = _deps(_client(rec))

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            CTX,
            outcome_type="invoice_recon",
            context={"period": "p", "x_ewcp_actor": "user:admin", "actor": "user:admin"},
        )
    )

    assert out["ok"] is True
    assert rec.requests[1].headers["x-ewcp-actor"] == "user:u-1"


# ---------------------------------------------------------------------------
# external_write actor binding (C10 council P1)
# ---------------------------------------------------------------------------


def _external_write_then_run(run_payload: dict | None = None):
    return _Recorder(
        [
            _json_response(_EXTERNAL_WRITE_DESCRIPTOR),
            _json_response(run_payload if run_payload is not None else _RUN_VIEW),
        ]
    )


@pytest.mark.asyncio
async def test_external_write_actor_minted_from_verified_session_not_context() -> None:
    """C10 rework: a forged `user_id` in runtime context/config can NEVER
    reach X-Ewcp-Actor on an external_write — the header is minted from the
    authenticated session identity (the request ContextVar) only."""
    rec = _external_write_then_run()
    deps = _deps(_client(rec))
    forged_ctx = {"thread_id": "t-1", "run_id": "r-1", "user_id": "forged-admin"}
    token = set_current_user(SimpleNamespace(id="u-real"))
    try:
        out = json.loads(
            await invoke_tools.invoke_impl(
                deps,
                forged_ctx,
                outcome_type="create_draft_po",
                context={"vendor": "V-1"},
            )
        )
    finally:
        reset_current_user(token)

    assert out["ok"] is True
    assert rec.requests[1].headers["x-ewcp-actor"] == "user:u-real"


@pytest.mark.asyncio
async def test_external_write_denied_without_session_no_default_actor() -> None:
    """C10 rework: no anonymous/`user:default` fallback on external writes —
    an unauthenticated invoke is rejected at the product BEFORE dispatch, so
    the kernel never sees a write request it would bind to
    `tenant:<t>`/`dev:anonymous`."""
    rec = _Recorder([_json_response(_EXTERNAL_WRITE_DESCRIPTOR)])
    deps = _deps(_client(rec))
    ctx = {"thread_id": "t-1", "run_id": "r-1", "user_id": "u-1"}  # forged ctx can't rescue it

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            ctx,
            outcome_type="create_draft_po",
            context={"vendor": "V-1"},
        )
    )

    assert out["ok"] is False
    assert out["error"] == "unauthenticated"
    assert out["guidance"] == "fatal"
    # descriptor GET only — the /run POST was never dispatched, so no
    # X-Ewcp-Actor (and no `user:default`) was ever emitted.
    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_external_write_actor_not_overridable_via_tool_context_arg() -> None:
    """C10 rework: tool-arg `context` values are capability form fields — a
    model-crafted `user_id`/`actor` key cannot steer the session-minted
    external-write actor either."""
    rec = _external_write_then_run()
    deps = _deps(_client(rec))
    token = set_current_user(SimpleNamespace(id="u-real"))
    try:
        out = json.loads(
            await invoke_tools.invoke_impl(
                deps,
                CTX,
                outcome_type="create_draft_po",
                context={"user_id": "admin", "actor": "user:admin", "x_ewcp_actor": "user:admin"},
            )
        )
    finally:
        reset_current_user(token)

    assert out["ok"] is True
    assert rec.requests[1].headers["x-ewcp-actor"] == "user:u-real"


@pytest.mark.asyncio
async def test_non_write_invoke_keeps_context_actor_minting() -> None:
    """Non-external-write lanes keep the prior minting (runtime-context user
    first, ContextVar fallback) — the session-only rule is write-scoped."""
    rec = _descriptor_then_run()
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    assert rec.requests[1].headers["x-ewcp-actor"] == "user:u-1"


@pytest.mark.asyncio
async def test_invoke_missing_required_context_fails_before_post() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR)])
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={}))

    assert out["ok"] is False
    assert out["error"] == "missing_required_input"
    assert out["missing"] == ["period"]
    assert out["guidance"] == "correct"
    assert len(rec.requests) == 1  # descriptor GET only — no /run POST


@pytest.mark.asyncio
async def test_invoke_unknown_file_slot_rejected_before_post() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR)])
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}, files={"not_a_slot": ["x.zip"]}))

    assert out["ok"] is False
    assert out["error"] == "invalid_request"
    assert "not_a_slot" in out["message"]
    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_invoke_resolves_thread_upload_files(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "inv.zip").write_bytes(b"PK\x03\x04zip-body")
    (uploads / "books.csv").write_bytes("mã,tổng\n1,100".encode())
    rec = _descriptor_then_run()
    deps = _deps(_client(rec), paths=lambda: _FakePaths(tmp_path))

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            CTX,
            outcome_type="invoice_recon",
            context={"period": "p"},
            files={"invoices_zip": ["inv.zip"], "books": ["/mnt/user-data/uploads/books.csv"]},
        )
    )

    assert out["ok"] is True
    content = rec.requests[1].content
    assert b"PK\x03\x04zip-body" in content
    assert "mã,tổng".encode() in content
    assert b'name="invoices_zip"' in content
    assert b'name="books"' in content


@pytest.mark.asyncio
async def test_invoke_resolves_uploads_via_gateway_local_dir(tmp_path: Path) -> None:
    """F6: composer uploads land in the gateway-local base_dir namespace; the
    host_* namespace is a docker-daemon mount source (DEER_FLOW_HOST_BASE_DIR)
    and is not readable in-process on provisioner/DooD deployments."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "data.csv").write_bytes(b"a,b\n1,2\n")
    host_root = tmp_path / "docker-host-side"  # exists on the host, not in-gateway
    rec = _descriptor_then_run()
    deps = _deps(_client(rec), paths=lambda: _FakePaths(tmp_path, host_root=host_root))

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            CTX,
            outcome_type="invoice_recon",
            context={"period": "p"},
            files={"books": ["data.csv"]},
        )
    )

    assert out["ok"] is True
    assert b"a,b\n1,2\n" in rec.requests[1].content


@pytest.mark.asyncio
async def test_invoke_file_not_found_is_correction_error(tmp_path: Path) -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR)])
    deps = _deps(_client(rec), paths=lambda: _FakePaths(tmp_path))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}, files={"invoices_zip": ["missing.zip"]}))

    assert out["ok"] is False
    assert out["error"] == "invalid_request"
    assert "missing.zip" in out["message"]
    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_invoke_file_traversal_rejected(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    rec = _Recorder([_json_response(_DESCRIPTOR)])
    deps = _deps(_client(rec), paths=lambda: _FakePaths(tmp_path))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}, files={"invoices_zip": ["../secret.txt"]}))

    assert out["ok"] is False
    assert len(rec.requests) == 1  # traversal rejected before any /run POST


@pytest.mark.asyncio
async def test_invoke_kernel_409_payload_mismatch_tool_error() -> None:
    rec = _Recorder(
        [
            _json_response(_DESCRIPTOR),
            _json_response({"error": "idempotency_payload_mismatch", "message": "same key, different payload"}, status=409),
        ]
    )
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}, idempotency_key="key-x"))

    assert out["ok"] is False
    assert out["error"] == "idempotency_payload_mismatch"
    assert out["guidance"] == "correct"
    assert "payload" in out["hint"]


@pytest.mark.asyncio
async def test_invoke_kernel_unsupported_version_is_fatal() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR), _json_response({"error": "unsupported_version", "message": "v2 only"}, status=409)])
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is False
    assert out["error"] == "unsupported_version"
    assert out["guidance"] == "fatal"


@pytest.mark.asyncio
async def test_invoke_unknown_capability_suggests_listing() -> None:
    rec = _Recorder([_json_response({"error": "unknown_capability", "message": "no such outcome"}, status=404)])
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="nonsense", context={}))

    assert out["ok"] is False
    assert out["error"] == "unknown_capability"
    assert "ewcp_capabilities" in out["hint"]


@pytest.mark.asyncio
async def test_invoke_kernel_not_configured() -> None:
    out = json.loads(await invoke_tools.invoke_impl(_deps(None), CTX, outcome_type="invoice_recon"))
    assert out["ok"] is False
    assert out["error"] == "kernel_not_configured"


@pytest.mark.asyncio
async def test_invoke_transport_failure_is_retryable() -> None:
    rec = _Recorder([_json_response(_DESCRIPTOR), httpx.ConnectError("refused"), httpx.ConnectError("refused"), httpx.ConnectError("refused")])
    deps = _deps(_client(rec, max_attempts=3))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is False
    assert out["error"] == "kernel_unreachable"
    assert out["guidance"] == "retry"


# ---------------------------------------------------------------------------
# ewcp_invoke → ExecutionRunMap binding (A6 #13 hybrid lane)
# ---------------------------------------------------------------------------


class _MapStore:
    """Fake ExecutionRunStore: list/get over a record list, records writes."""

    def __init__(self, records: list[Any] | None = None, *, fail: bool = False) -> None:
        self.records = list(records or [])
        self.inserts: list[Any] = []
        self.binds: list[tuple[str, str]] = []
        self.fail = fail

    async def list_for_thread(self, thread_id: str, *, limit: int = 50) -> list[Any]:
        return [r for r in self.records if getattr(r, "thread_id", None) == thread_id]

    async def get_by_idempotency_key(self, created_by: str, key: str) -> Any:
        return next(
            (r for r in self.records if getattr(r, "created_by", None) == created_by and getattr(r, "idempotency_key", None) == key),
            None,
        )

    async def insert(self, record: Any) -> Any:
        if self.fail:
            raise RuntimeError("store down")
        self.records.append(record)
        self.inserts.append(record)
        return record

    async def bind_workrun(self, execution_run_id: str, workrun_id: str) -> None:
        self.binds.append((execution_run_id, workrun_id))


_RUN_VIEW_WR = {**_RUN_VIEW, "workrun_id": "wr-inv-1"}


@pytest.mark.asyncio
async def test_invoke_success_projects_map_row_bound_to_workrun() -> None:
    """Hybrid lane: the tool result must name the ExecutionRunMap row that
    binds the kernel workrun to this thread — the chat card deep-links
    /workspace/ewcp-runs/{execution_run_map_id}."""
    rec = _descriptor_then_run(_RUN_VIEW_WR)
    store = _MapStore()
    deps = _deps(_client(rec), store=store)

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    assert len(store.inserts) == 1
    row = store.inserts[0]
    assert out["execution_run_map_id"] == row.execution_run_id
    assert row.thread_id == "t-1"
    assert row.run_id == "r-1"
    assert row.workrun_id == "wr-inv-1"
    assert row.task_mode == "invoke"
    assert row.created_by == "u-1"
    assert row.idempotency_key == out["idempotency_key"]


@pytest.mark.asyncio
async def test_invoke_replayed_key_reuses_map_row() -> None:
    """A second invoke under the same idempotency key converges on the
    existing row — no duplicate ExecutionRunMap entries."""
    existing = SimpleNamespace(
        execution_run_id="er_existing",
        thread_id="t-1",
        run_id="r-1",
        workrun_id="wr-inv-1",
        task_mode="invoke",
        created_by="u-1",
        idempotency_key="agent-key-9",
    )
    rec = _descriptor_then_run(_RUN_VIEW_WR)
    store = _MapStore([existing])
    deps = _deps(_client(rec), store=store)

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            CTX,
            outcome_type="invoice_recon",
            context={"period": "p"},
            idempotency_key="agent-key-9",
        )
    )

    assert out["ok"] is True
    assert out["execution_run_map_id"] == "er_existing"
    assert store.inserts == []
    assert store.binds == []  # workrun already bound — no rewrite


@pytest.mark.asyncio
async def test_invoke_unbound_existing_row_gets_workrun_bound() -> None:
    existing = SimpleNamespace(
        execution_run_id="er_existing",
        thread_id="t-1",
        run_id="r-1",
        workrun_id=None,
        task_mode="invoke",
        created_by="u-1",
        idempotency_key="agent-key-10",
    )
    rec = _descriptor_then_run(_RUN_VIEW_WR)
    store = _MapStore([existing])
    deps = _deps(_client(rec), store=store)

    out = json.loads(
        await invoke_tools.invoke_impl(
            deps,
            CTX,
            outcome_type="invoice_recon",
            context={"period": "p"},
            idempotency_key="agent-key-10",
        )
    )

    assert out["execution_run_map_id"] == "er_existing"
    assert store.inserts == []
    assert store.binds == [("er_existing", "wr-inv-1")]


@pytest.mark.asyncio
async def test_invoke_map_identity_never_selects_invoke_rows() -> None:
    """Identity regression: an invoke row on the thread is an invocation
    record, not the calling run's own ExecutionRun — a later invoke in the
    same run must keep keying the raw run_id, not join the earlier
    invocation's er_ account."""
    invoke_row = SimpleNamespace(
        execution_run_id="er_prev_invoke",
        thread_id="t-1",
        run_id="r-1",
        workrun_id="wr-inv-0",
        task_mode="invoke",
        created_by="u-1",
        idempotency_key="k0",
    )
    rec = _descriptor_then_run(_RUN_VIEW_WR)
    store = _MapStore([invoke_row])
    deps = _deps(_client(rec), store=store)

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    body = _form_text(rec.requests[1])
    assert 'name="execution_run_id"' in body and "r-1" in body
    assert "er_prev_invoke" not in body


@pytest.mark.asyncio
async def test_invoke_map_row_for_launcher_thread_still_joins_er_identity() -> None:
    """A pane-launched (non-invoke) row still wins the identity lookup —
    invoke rows are skipped, the launcher's er_ is sent to the kernel."""
    launcher_row = SimpleNamespace(
        execution_run_id="er_launch",
        thread_id="t-1",
        run_id="r-1",
        workrun_id=None,
        task_mode="general",
        created_by="u-1",
        idempotency_key="kl",
    )
    invoke_row = SimpleNamespace(
        execution_run_id="er_prev_invoke",
        thread_id="t-1",
        run_id="r-1",
        workrun_id="wr-0",
        task_mode="invoke",
        created_by="u-1",
        idempotency_key="k0",
    )
    rec = _descriptor_then_run(_RUN_VIEW_WR)
    store = _MapStore([invoke_row, launcher_row])
    deps = _deps(_client(rec), store=store)

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    body = _form_text(rec.requests[1])
    assert "er_launch" in body
    assert "er_prev_invoke" not in body


@pytest.mark.asyncio
async def test_invoke_store_failure_does_not_downgrade_result() -> None:
    """The invoke succeeded kernel-side — a projection failure yields a
    result without the map link, never a tool error."""
    rec = _descriptor_then_run(_RUN_VIEW_WR)
    store = _MapStore(fail=True)
    deps = _deps(_client(rec), store=store)

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    assert out["execution_run_map_id"] is None


@pytest.mark.asyncio
async def test_invoke_without_store_has_no_map_link() -> None:
    rec = _descriptor_then_run(_RUN_VIEW_WR)
    deps = _deps(_client(rec))

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    assert out["execution_run_map_id"] is None


@pytest.mark.asyncio
async def test_invoke_run_without_workrun_id_creates_no_row() -> None:
    """Non-invoke kernel intakes echo no workrun — nothing to bind."""
    run_view = {k: v for k, v in _RUN_VIEW_WR.items() if k != "workrun_id"}
    rec = _descriptor_then_run(run_view)
    store = _MapStore()
    deps = _deps(_client(rec), store=store)

    out = json.loads(await invoke_tools.invoke_impl(deps, CTX, outcome_type="invoice_recon", context={"period": "p"}))

    assert out["ok"] is True
    assert out["execution_run_map_id"] is None
    assert store.inserts == []


# ---------------------------------------------------------------------------
# middleware contribution
# ---------------------------------------------------------------------------


def test_middleware_carries_both_capability_tools() -> None:
    middleware = EwcpInvokeToolsMiddleware(_deps(_client(_Recorder([]))))
    names = [t.name for t in middleware.tools]
    assert names == ["ewcp_capabilities", "ewcp_invoke"]


def test_middleware_survives_copy_for_declared_view_narrowing() -> None:
    middleware = EwcpInvokeToolsMiddleware(_deps(_client(_Recorder([]))))
    clone = copy.copy(middleware)
    assert clone is not middleware
    assert [t.name for t in clone.tools] == ["ewcp_capabilities", "ewcp_invoke"]


def test_contributor_places_middleware_at_standard() -> None:
    contributor = InvokeToolsContributor(lambda: _deps(_client(_Recorder([]))))
    placements = contributor.contribute_middlewares(None, None)
    assert len(placements) == 1
    assert placements[0].placement == "standard"


# ---------------------------------------------------------------------------
# egress — kernel-bound tool destination policy (#37 verified for invoke)
# ---------------------------------------------------------------------------


def _policy(kernel_url: str | None, **egress: Any) -> EgressPolicy:
    return EgressPolicy(EgressPolicy.resolve_config({"egress": egress}), kernel_url=kernel_url)


def _ctx(**extra: Any) -> dict[str, Any]:
    ctx = {"user_id": "u-sensitive"}
    ctx.update(extra)
    return ctx


def test_kernel_tool_denied_under_local_only_without_kernel_url() -> None:
    pol = _policy(None, default_mode="local_only")
    for name in ("ewcp_invoke", "ewcp_capabilities"):
        d = pol.check_tool_call(name, {}, _ctx())
        assert not d.allowed, name
        assert "unconfigured" in d.reason


def test_kernel_tool_denied_under_restricted_without_kernel_url() -> None:
    pol = _policy(None, default_mode="restricted", allowed_domains=["kernel.local"])
    d = pol.check_tool_call("ewcp_invoke", {}, _ctx())
    assert not d.allowed


def test_kernel_tool_loopback_kernel_allowed_local_only() -> None:
    pol = _policy("http://127.0.0.1:8080", default_mode="local_only")
    for name in ("ewcp_invoke", "ewcp_capabilities"):
        assert pol.check_tool_call(name, {}, _ctx()).allowed, name


def test_kernel_tool_lan_kernel_allowed_local_only() -> None:
    pol = _policy("http://10.0.0.5:8080", default_mode="local_only")
    assert pol.check_tool_call("ewcp_invoke", {}, _ctx()).allowed


def test_kernel_tool_public_dns_kernel_denied_local_only() -> None:
    pol = _policy("https://kernel.example.com", default_mode="local_only")
    d = pol.check_tool_call("ewcp_invoke", {}, _ctx())
    assert not d.allowed
    assert "local_only" in d.reason


def test_kernel_tool_declared_domain_allowed_local_only() -> None:
    pol = _policy("https://kernel.internal.example.com", default_mode="local_only", allowed_domains=["kernel.internal.example.com"])
    assert pol.check_tool_call("ewcp_invoke", {}, _ctx()).allowed


def test_kernel_tool_restricted_requires_allowed_domains() -> None:
    pol = _policy("http://10.0.0.5:8080", default_mode="restricted", allowed_domains=["10.0.0.5"])
    assert pol.check_tool_call("ewcp_invoke", {}, _ctx()).allowed

    pol2 = _policy("http://10.0.0.5:8080", default_mode="restricted")
    d = pol2.check_tool_call("ewcp_invoke", {}, _ctx())
    assert not d.allowed
    assert "allowed_domains" in d.reason


def test_kernel_tool_non_sensitive_tenant_allows() -> None:
    pol = _policy("https://kernel.example.com", default_mode="restricted", tenant_classes={"demo": "non_sensitive"})
    assert pol.check_tool_call("ewcp_invoke", {}, _ctx(ewcp_tenant_id="demo")).allowed


def test_kernel_tool_approved_cloud_allows() -> None:
    pol = _policy("https://kernel.example.com", default_mode="approved_cloud")
    assert pol.check_tool_call("ewcp_invoke", {}, _ctx()).allowed


def test_kernel_bound_tools_do_not_fall_through_to_name_deny() -> None:
    """Regression: without the kernel-bound branch, ewcp_invoke is an
    unknown tool name → denied under restricted even for a loopback
    kernel. The destination check must run instead of the name allowlist."""
    pol = _policy("http://127.0.0.1:8080", default_mode="restricted", allowed_domains=["127.0.0.1"], allowed_tools=[])
    assert pol.check_tool_call("ewcp_invoke", {}, _ctx()).allowed
    # unrelated unknown tools still deny
    assert not pol.check_tool_call("acme_mcp_fetch", {}, _ctx()).allowed
