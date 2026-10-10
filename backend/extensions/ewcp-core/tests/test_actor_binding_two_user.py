"""C10 two-user integration verification — REAL gateway auth → real
``invoke_impl`` → real kernel wire.

Depends on #46 (``devin/1791557113-c10-actor-binding``): the external-write
session mint, the no-session denial, and ``KernelClient``'s actor kwargs only
exist on that branch — this suite composes with its semantics and would fail
against ``product/vnext`` HEAD by design.

Real seams, nothing re-implemented:

- **REAL gateway auth** — the test app carries the production
  ``AuthMiddleware`` with ``DEER_FLOW_AUTH_DISABLED`` unset. Two real users
  live in a real sqlite ``users`` table bootstrapped by the real
  ``init_engine``; they are created through the real
  ``LocalAuthProvider.create_user`` (same code ``/api/v1/auth/register``
  uses) and each session is a real JWT minted by ``create_access_token``
  under a real ``AuthConfig`` — verified end-to-end on every request:
  signature, expiry, user lookup, ``token_version``. Only then does the
  middleware stamp ``set_current_user()`` (``auth_middleware.py`` dispatch).
- **REAL identity pipeline** — the test route runs the verbatim admission
  chain ``merge_run_context_overrides`` → ``strip_internal_context_keys`` →
  ``inject_authenticated_user_context`` (services.py:1847+ / :766+) and then
  the worker's own ``_build_runtime_context``, producing the same
  ``runtime.context`` the ``ewcp_invoke`` tool sees inside a run. A forged
  caller ``user_id`` dies at the server stamp before the tool ever runs.
- **REAL invoke wire** — ``invoke_impl`` + ``InvokeDeps`` wired like
  ``install()`` (client/store/tenant getters) with a real
  ``ExecutionRunStore`` on the same sqlite, and the real ``KernelClient``
  multipart serialization. ``get_current_user()`` inside ``invoke_impl``
  reads the ContextVar the REAL middleware set — the same scope the run task
  inherits (``record.task = asyncio.create_task(worker)`` at
  services.py:2203 copies the request context into the worker task).
- **Only the kernel is stubbed** — ``httpx.MockTransport`` records every
  request, so assertions run on the byte-for-byte wire the kernel receives
  (suite convention: the kernel is a separate HTTP service — Option A+, no
  in-process mount; it is not check-out-able in this repo's CI).

Labeled simulation: the LangGraph tool-dispatch hop (model → ``ewcp_invoke``
→ ``invoke_impl``). The route calls ``invoke_impl`` inside the request task —
the ContextVar scope a production run task inherits — and the request body
supplies exactly the four tool args a model can pass (``outcome_type``,
``context``, ``files``, ``idempotency_key``). ``files:`` scenarios are out of
scope here (upload-path resolution is covered by
``test_ewcp_core_invoke_tools.py``); the actor seam is orthogonal to them.

What the kernel sees (kernel ``src/ewcp/api/app.py`` @ 71aa185 — source read,
not executed; asserted on the wire bytes):

- ``_actor`` (app.py:940-981): under ``EWCP_TENANT_KEYS`` auth a well-formed
  ``X-Ewcp-Actor`` (grammar ``_ACTOR_HEADER``, app.py:412 — pinned below)
  binds the principal ``<actor>@tenant:<tenant>``; a missing or malformed
  value silently degrades to ``tenant:<t>``. So user A's write binds
  ``user:<A-uuid>@tenant:demo`` and user B's ``user:<B-uuid>@tenant:demo`` —
  two distinct per-user principals under the SAME tenant credential. The
  grammar assertion on the emitted header is what proves the product's mint
  is accepted, not degraded to the tenant fallback.
- ``EWCP_WRITE_APPROVERS`` (app.py:441-449 → ``approver_allowlist`` →
  ``check_approval_allowed``, ``kernel/proposed_action.py``:444/:472) is
  consulted ONLY at approve-decision time (app.py:1139). It is advisory
  w.r.t. the minted actor: it can deny a *subsequent approval* but can never
  rewrite which principal the intake bound as ``ProposedAction.requester``
  (app.py:2351-2357) — intake itself is not allowlisted.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
import pytest_asyncio

# The real admission chain + worker merge: the same functions the gateway
# runs between the HTTP boundary and ToolRuntime.context.
from app.gateway.services import (
    inject_authenticated_user_context,
    merge_run_context_overrides,
    strip_internal_context_keys,
)
from deerflow.runtime.runs.worker import _build_runtime_context
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ewcp_core import invoke_tools
from ewcp_core.execution_run_store import ExecutionRunStore
from ewcp_core.invoke_tools import InvokeDeps
from ewcp_core.kernel_client import KernelClient, KernelClientConfig

API_KEY = "ek-test-two-user-key"
BASE = "http://kernel.test"
TENANT = "demo"

#: Kernel-side actor grammar — copied verbatim from
#: ``src/ewcp/api/app.py:412`` (``_ACTOR_HEADER``) @ 71aa185 so the suite
#: pins the exact shape the kernel will bind instead of degrading.
_KERNEL_ACTOR_HEADER = re.compile(r"[a-z][a-z0-9_]*:[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")

# Descriptors the kernel serves — same shapes as test_ewcp_core_invoke_tools.
_EXTERNAL_WRITE_DESCRIPTOR: dict[str, Any] = {
    "capability_id": "pack:create_draft_po",
    "contract_version": "1",
    "kind": "pack",
    "summary": "Create a draft purchase order",
    "side_effect_class": "external_write",
    "input_schema": {"inputs": [], "context": [{"name": "vendor", "type": "str", "required": True}], "required_context": ["vendor"]},
    "output_schema": {"kind": "run_view"},
    "idempotency": {"key_header": "Idempotency-Key", "ttl_s": 86400},
}

_NON_WRITE_DESCRIPTOR: dict[str, Any] = {
    **_EXTERNAL_WRITE_DESCRIPTOR,
    "capability_id": "pack:invoice_recon",
    "side_effect_class": "workspace_write",
    "input_schema": {"inputs": [], "context": [{"name": "period", "type": "str", "required": True}], "required_context": ["period"]},
}

_RUN_VIEW: dict[str, Any] = {
    "run_id": "wr-two-1",
    "workrun_id": "wr-two-1",
    "status": "proposed",
    "capability_id": "pack:create_draft_po",
    "contract_version": "1",
    "invocation_id": "inv-two-1",
    "execution_run_id": "er-two-1",
}


class _KernelWire:
    """``httpx.MockTransport`` recording the kernel-facing wire.

    Serves the descriptor GET + invoke POST the contract requires; every
    request is captured for byte-level assertion.
    """

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path.startswith("/outcomes/"):
            return httpx.Response(200, json=self._descriptor)
        if request.method == "POST" and request.url.path.endswith("/run"):
            return httpx.Response(200, json=_RUN_VIEW)
        return httpx.Response(404, json={"error": "unhandled"})

    def run_posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path.endswith("/run")]


def _form_text(req: httpx.Request) -> str:
    return req.content.decode("utf-8", errors="replace")


class _Stack:
    """The assembled real-auth harness (see module docstring)."""

    def __init__(self) -> None:
        self.user_a: Any = None
        self.user_b: Any = None
        self.token_a: str = ""
        self.token_b: str = ""
        self.store: ExecutionRunStore | None = None
        self.wire: _KernelWire | None = None
        self.http: httpx.AsyncClient | None = None
        self.deps: InvokeDeps | None = None

    async def invoke(
        self,
        *,
        cookie: str | None = None,
        headers: dict[str, str] | None = None,
        outcome_type: str,
        context: dict[str, Any] | None = None,
        run_context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        thread_id: str = "t-1",
        run_id: str = "r-1",
    ) -> httpx.Response:
        """Drive ``invoke_impl`` over the real authenticated HTTP request.

        ``cookie`` is the raw ``access_token`` JWT value, sent as the
        session cookie header exactly as a browser would.
        """
        assert self.http is not None
        sent_headers = dict(headers or {})
        if cookie is not None:
            sent_headers["cookie"] = f"access_token={cookie}"
        return await self.http.post(
            "/ewcp-test/invoke",
            headers=sent_headers,
            json={
                "outcome_type": outcome_type,
                "context": context or {},
                "idempotency_key": idempotency_key,
                "run_context": run_context or {},
                "thread_id": thread_id,
                "run_id": run_id,
            },
        )


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Two real authenticated users + the production middleware chain."""
    # Real session-cookie auth: dev-mode bypass explicitly off.
    monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)
    for name in ("EWCP_KERNEL_URL", "EWCP_KERNEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path}/users.sqlite3", sqlite_dir=str(tmp_path))

    # Real JWT config; cached provider/repo must rebuild on the fresh factory.
    import app.gateway.auth.config as auth_config
    import app.gateway.deps as gateway_deps

    monkeypatch.setattr(auth_config, "_auth_config", auth_config.AuthConfig(jwt_secret="test-suite-only-key-material-0123456789abcdef"))
    monkeypatch.setattr(gateway_deps, "_cached_repo", None)
    monkeypatch.setattr(gateway_deps, "_cached_local_provider", None)

    # Disabled authorization = legacy all-permissions (same patch the
    # middleware unit tests use); the identity seam is unaffected.
    import app.gateway.authz as authz
    from deerflow.config.authorization_config import AuthorizationConfig

    monkeypatch.setattr(authz, "_get_route_authorization_config", lambda: AuthorizationConfig())

    from app.gateway.auth.jwt import create_access_token
    from app.gateway.deps import get_local_provider

    provider = get_local_provider()
    user_a = await provider.create_user("alice@example.com", password="alice-pass-123")
    user_b = await provider.create_user("bob@example.com", password="bob-pass-123")

    s = _Stack()
    s.user_a, s.user_b = user_a, user_b
    s.token_a = create_access_token(str(user_a.id), token_version=user_a.token_version)
    s.token_b = create_access_token(str(user_b.id), token_version=user_b.token_version)

    s.store = ExecutionRunStore(get_session_factory())
    await s.store.ensure_schema()

    s.wire = _KernelWire(_EXTERNAL_WRITE_DESCRIPTOR)
    client = KernelClient(
        KernelClientConfig(kernel_url=BASE, api_key=API_KEY),
        transport=httpx.MockTransport(s.wire),
        backoff=lambda _attempt: 0.0,
    )
    # The same getter shape install() wires: late-bound client/store/tenant.
    s.deps = InvokeDeps(
        client_getter=lambda: client,
        store_getter=lambda: s.store,
        tenant_id_getter=lambda: TENANT,
    )

    app = FastAPI()

    from app.gateway.auth_middleware import AuthMiddleware

    app.add_middleware(AuthMiddleware)

    @app.post("/ewcp-test/invoke")
    async def _invoke(request: Request) -> JSONResponse:
        """The tool-call seam: real admission stamp + real invoke.

        ``run_context`` plays ``body.config['context']`` — the bytes a
        hostile client can put in the run body. The chain below is the
        verbatim production order (services.py:1847-1851, :766+): merge
        client keys, strip internal-only, then the server stamp wins.
        """
        body = await request.json()
        config: dict[str, Any] = {"context": {}, "configurable": {}}
        merge_run_context_overrides(config, body.get("run_context") or {})
        strip_internal_context_keys(config)
        inject_authenticated_user_context(config, request)
        ctx = _build_runtime_context(
            str(body.get("thread_id") or "t-1"),
            str(body.get("run_id") or "r-1"),
            config["context"],
        )
        out = json.loads(
            await invoke_tools.invoke_impl(
                s.deps,
                ctx,
                outcome_type=body["outcome_type"],
                context=body.get("context") or {},
                idempotency_key=body.get("idempotency_key"),
            )
        )
        return JSONResponse({"tool": out, "runtime_user_id": ctx.get("user_id")})

    s.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test")
    try:
        yield s
    finally:
        await s.http.aclose()
        await close_engine()


def _actor_of(request: httpx.Request) -> str | None:
    return request.headers.get("x-ewcp-actor")


# ---------------------------------------------------------------------------
# user A — authenticated session → external-write invoke carries user:A
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_a_external_write_binds_session_actor(stack: _Stack) -> None:
    """A's session → external-write invoke → ``X-Ewcp-Actor: user:<A.id>`` —
    minted from the verified session, satisfying the kernel's actor grammar
    so it binds ``user:<A.id>@tenant:demo`` instead of the tenant fallback."""
    res = await stack.invoke(
        cookie=stack.token_a,
        outcome_type="create_draft_po",
        context={"vendor": "V-1"},
        idempotency_key="k-a-1",
    )
    assert res.status_code == 200, res.text
    assert res.json()["tool"]["ok"] is True

    posts = stack.wire.run_posts()
    assert len(posts) == 1
    actor = _actor_of(posts[0])
    assert actor == f"user:{stack.user_a.id}"
    # Kernel `_actor` only binds a well-formed actor — prove acceptance.
    assert _KERNEL_ACTOR_HEADER.fullmatch(actor)
    assert posts[0].headers["x-ewcp-api-key"] == API_KEY
    # The invocation map row pins the same verified principal — pane reads
    # stay owner-consistent with the kernel-bound requester.
    assert res.json()["runtime_user_id"] == str(stack.user_a.id)
    record = await stack.store.get_by_idempotency_key(str(stack.user_a.id), "k-a-1")
    assert record is not None and record.created_by == str(stack.user_a.id)


@pytest.mark.asyncio
async def test_actor_not_forgeable_via_body_context_or_tool_args(stack: _Stack) -> None:
    """Every forge surface dies before the mint: an inbound
    ``X-Ewcp-Actor`` header is never read, a ``user_id`` planted in the run
    body's ``config.context`` is scrubbed by the server stamp, and model
    tool-arg keys are capability fields, never the actor."""
    res = await stack.invoke(
        cookie=stack.token_a,
        headers={"X-Ewcp-Actor": "user:admin"},  # forged inbound header
        outcome_type="create_draft_po",
        run_context={"user_id": "admin", "actor": "user:admin", "x_ewcp_actor": "user:admin"},  # forged run-body keys
        context={"vendor": "V-1", "user_id": "admin", "actor": "user:admin"},  # forged tool-arg fields
    )
    assert res.status_code == 200, res.text

    posts = stack.wire.run_posts()
    assert len(posts) == 1
    assert _actor_of(posts[0]) == f"user:{stack.user_a.id}"
    # The stamped runtime context carried A — the forged run-body user_id
    # never survived `inject_authenticated_user_context`.
    assert res.json()["runtime_user_id"] == str(stack.user_a.id)
    # Tool-arg forges DO land as capability form fields (the contract
    # forwards context verbatim) — but the kernel binds identity only from
    # X-Ewcp-Actor + the tenant key, so they cannot steer the principal.
    body = _form_text(posts[0])
    assert 'name="actor"' in body and "user:admin" in body


@pytest.mark.asyncio
async def test_sessions_do_not_bleed_between_requests(stack: _Stack) -> None:
    """ContextVar isolation is per-request-task: A → B → A mints three
    distinct session actors, never a stale predecessor."""
    for cookie, user in ((stack.token_a, stack.user_a), (stack.token_b, stack.user_b), (stack.token_a, stack.user_a)):
        res = await stack.invoke(cookie=cookie, outcome_type="create_draft_po", context={"vendor": "V-1"})
        assert res.json()["tool"]["ok"] is True
    actors = [_actor_of(req) for req in stack.wire.run_posts()]
    assert actors == [f"user:{stack.user_a.id}", f"user:{stack.user_b.id}", f"user:{stack.user_a.id}"]


# ---------------------------------------------------------------------------
# user B — cross-user impersonation denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_b_cannot_impersonate_user_a(stack: _Stack) -> None:
    """B authenticating and aiming EVERY forge vector at A still mints
    ``user:<B.id>`` — there is no caller-reachable path to A's principal."""
    res = await stack.invoke(
        cookie=stack.token_b,
        headers={"X-Ewcp-Actor": f"user:{stack.user_a.id}"},
        outcome_type="create_draft_po",
        run_context={"user_id": str(stack.user_a.id)},
        context={"vendor": "V-1", "user_id": str(stack.user_a.id), "actor": f"user:{stack.user_a.id}"},
        idempotency_key="k-b-1",
    )
    assert res.status_code == 200, res.text

    posts = stack.wire.run_posts()
    assert len(posts) == 1
    assert _actor_of(posts[0]) == f"user:{stack.user_b.id}"
    # A's id is confined to the forged form fields — absent from every
    # header the kernel reads for identity.
    assert str(stack.user_a.id) not in json.dumps(dict(posts[0].headers))
    record = await stack.store.get_by_idempotency_key(str(stack.user_b.id), "k-b-1")
    assert record is not None and record.created_by == str(stack.user_b.id)


# ---------------------------------------------------------------------------
# unauthenticated — external-write rejected before dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected_at_auth_gate(stack: _Stack) -> None:
    """No session cookie → the real middleware 401s BEFORE the route (and
    therefore before ``invoke_impl``): the kernel sees nothing at all."""
    res = await stack.invoke(outcome_type="create_draft_po", context={"vendor": "V-1"})
    assert res.status_code == 401
    assert stack.wire.requests == []


@pytest.mark.asyncio
async def test_external_write_denied_when_run_has_no_session(stack: _Stack) -> None:
    """The ContextVar-absent boundary a request cannot reach (run tasks
    dispatched outside request scope — scheduler, cross-process drivers):
    ``invoke_impl`` still refuses the external write pre-dispatch, emitting
    no ``user:default`` and no actor header."""
    wire = stack.wire
    ctx = _build_runtime_context("t-1", "r-offreq", {"user_id": "u-forged"})

    out = json.loads(await invoke_tools.invoke_impl(stack.deps, ctx, outcome_type="create_draft_po", context={"vendor": "V-1"}))

    assert out["ok"] is False
    assert out["error"] == "unauthenticated"
    assert out["guidance"] == "fatal"
    # Descriptor GET only — the /run POST was never dispatched.
    assert wire.run_posts() == []


# ---------------------------------------------------------------------------
# non-write lanes keep anon/context scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_write_lane_mints_stamped_context_user(stack: _Stack) -> None:
    """Non-write side_effect classes keep the context-based mint — under a
    real session the stamped runtime user is the session user (a forged
    run-body ``user_id`` still dies at the server stamp)."""
    stack.wire._descriptor = _NON_WRITE_DESCRIPTOR
    res = await stack.invoke(
        cookie=stack.token_a,
        outcome_type="invoice_recon",
        run_context={"user_id": "admin"},
        context={"period": "2026-10"},
    )
    assert res.json()["tool"]["ok"] is True
    posts = stack.wire.run_posts()
    assert len(posts) == 1
    assert _actor_of(posts[0]) == f"user:{stack.user_a.id}"
    assert res.json()["runtime_user_id"] == str(stack.user_a.id)


@pytest.mark.asyncio
async def test_non_write_lane_keeps_anon_scope_without_session(stack: _Stack) -> None:
    """Sessionless contexts (internal channels, CLI/tests) keep their
    caller-resolved identity on non-write lanes: a stamped context user is
    honored, a bare one degrades to the ``user:default`` anon scope —
    external writes alone require the verified session."""
    wire_ctx = _build_runtime_context("t-1", "r-chan", {"user_id": "chan-owner-9"})
    stack.wire._descriptor = _NON_WRITE_DESCRIPTOR

    out = json.loads(await invoke_tools.invoke_impl(stack.deps, wire_ctx, outcome_type="invoice_recon", context={"period": "p"}))
    assert out["ok"] is True
    assert _actor_of(stack.wire.run_posts()[-1]) == "user:chan-owner-9"

    anon_ctx = _build_runtime_context("t-2", "r-anon", {})
    out = json.loads(await invoke_tools.invoke_impl(stack.deps, anon_ctx, outcome_type="invoice_recon", context={"period": "p"}))
    assert out["ok"] is True
    assert _actor_of(stack.wire.run_posts()[-1]) == "user:default"
