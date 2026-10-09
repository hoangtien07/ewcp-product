"""Mocked tests for the Sign-in-with-ChatGPT OAuth layer.

No network access: every HTTP hop goes through ``httpx.MockTransport``, JWKS
are generated locally, and the loopback listener is exercised over a real
127.0.0.1 socket (no external calls).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from deerflow.models import chatgpt_plan_oauth as oauth

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk.update({"kid": "test-kid", "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def make_id_token(rsa_key, *, client_id: str, nonce: str, sub: str = "sub-1", email: str = "user@example.com", exp: int | None = None) -> str:
    claims = {
        "iss": oauth.ISSUER,
        "sub": sub,
        "aud": client_id,
        "iat": int(time.time()),
        "exp": exp or int(time.time()) + 300,
        "nonce": nonce,
        "email": email,
    }
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid"})


def token_payload(**overrides) -> dict:
    base = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "id_token": "idt-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": oauth.REQUESTED_SCOPES,
    }
    base.update(overrides)
    return base


def make_record(**overrides) -> oauth.CredentialRecord:
    base = {
        "email": "user@example.com",
        "issuer": oauth.ISSUER,
        "subject": "sub-1",
        "client_id": "oaiapp_abc123",
        "ext_agent_host_id": "urn:uuid:host-1",
        "id_token": "idt",
        "access_token": "at",
        "refresh_token": "rt",
        "scopes": oauth.REQUESTED_SCOPES.split(),
        "access_expires_at": time.time() + 3600,
    }
    base.update(overrides)
    return oauth.CredentialRecord(**base)


def make_pending(**overrides) -> oauth.PendingAuthorization:
    base = {
        "redirect_uri": "http://127.0.0.1:1455/auth/callback",
        "client_id": oauth.DYNAMIC_CLIENT_ID,
    }
    base.update(overrides)
    return oauth.PendingAuthorization.mint(**base)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------


class TestHostIdentity:
    def test_thumbprint_format(self):
        jwk = {"kty": "EC", "crv": "P-256", "x": "AQ" * 32, "y": "Ag" * 32}
        # Length is irrelevant — thumbprint input is the canonical JSON.
        host_id = oauth.host_id_from_public_jwk(jwk)
        assert host_id.startswith("urn:ietf:params:oauth:jwk-thumbprint:")

    def test_host_id_stable_and_permissions(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        host_id = store.ensure_host_identity()
        assert host_id == store.ensure_host_identity()  # persisted, stable
        mode = os.stat(tmp_path / "host.json").st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Authorization URL / pending authorization
# ---------------------------------------------------------------------------


class TestAuthorizationUrl:
    def test_dynamic_registration_params(self):
        pending = make_pending()
        url = oauth.build_authorization_url(pending, agent_name_hint="DeerFlow", ext_agent_host_id="urn:uuid:h")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert url.startswith(oauth.AUTHORIZE_URL)
        assert params["response_type"] == "code"
        assert params["client_id"] == oauth.DYNAMIC_CLIENT_ID
        assert params["redirect_uri"] == pending.redirect_uri
        assert params["redirect_uri"].startswith("http://127.0.0.1:")
        assert "localhost" not in params["redirect_uri"]
        assert params["scope"] == oauth.REQUESTED_SCOPES
        assert params["resource"] == oauth.RESOURCE
        assert params["state"] == pending.state
        assert params["nonce"] == pending.nonce
        assert params["code_challenge_method"] == "S256"
        assert params["code_challenge"] == pending.code_challenge
        assert params["ext_agent_host_id"] == "urn:uuid:h"
        assert params["agent_name_hint"] == "DeerFlow"
        assert "prompt" not in params

    def test_dynamic_registration_requires_name_hint(self):
        with pytest.raises(ValueError, match="agent_name_hint"):
            oauth.build_authorization_url(make_pending(), ext_agent_host_id="urn:uuid:h")

    def test_issued_client_id_omits_name_hint(self):
        pending = make_pending(client_id="oaiapp_issued")
        url = oauth.build_authorization_url(pending, ext_agent_host_id="urn:uuid:h", id_token_hint="idt", login_hint="u@e.com")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert params["client_id"] == "oaiapp_issued"
        assert "agent_name_hint" not in params
        assert params["id_token_hint"] == "idt"
        assert params["login_hint"] == "u@e.com"

    def test_pkce_challenge_is_s256(self):
        pending = make_pending()
        import base64
        import hashlib

        expected = base64.urlsafe_b64encode(hashlib.sha256(pending.code_verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        assert pending.code_challenge == expected
        assert "=" not in pending.code_challenge

    def test_state_nonce_unique_per_attempt(self):
        a, b = make_pending(), make_pending()
        assert a.state != b.state
        assert a.nonce != b.nonce
        assert a.code_verifier != b.code_verifier


# ---------------------------------------------------------------------------
# Loopback listener + callback validation
# ---------------------------------------------------------------------------


class TestLoopbackListener:
    def test_listener_captures_callback(self):
        listener = oauth.OAuthCallbackListener(0, "/auth/callback")
        try:
            assert listener.redirect_uri.startswith("http://127.0.0.1:")

            result_holder: dict = {}

            def _wait():
                result_holder["result"] = listener.wait(timeout_seconds=15)

            thread = threading.Thread(target=_wait)
            thread.start()
            resp = httpx.get(listener.redirect_uri, params={"code": "c1", "state": "s1", "client_id": "oaiapp_x"}, timeout=10)
            thread.join(timeout=15)
            assert resp.status_code == 200
            result = result_holder["result"]
            assert result.params == {"code": "c1", "state": "s1", "client_id": "oaiapp_x"}
        finally:
            listener.close()

    def test_listener_404s_wrong_path(self):
        listener = oauth.OAuthCallbackListener(0, "/auth/callback")
        try:

            def _wait():
                try:
                    listener.wait(timeout_seconds=15)
                except oauth.AuthorizationPendingTimeout:
                    pass

            thread = threading.Thread(target=_wait)
            thread.start()
            try:
                resp = httpx.get(listener.redirect_uri.replace("/auth/callback", "/nope"), timeout=10)
                assert resp.status_code == 404
            finally:
                listener.close()
            thread.join(timeout=15)
        finally:
            listener.close()

    def test_wait_timeout(self):
        listener = oauth.OAuthCallbackListener(0, "/auth/callback")
        try:
            with pytest.raises(oauth.AuthorizationPendingTimeout):
                listener.wait(timeout_seconds=0.3)
        finally:
            listener.close()


class TestValidateCallback:
    def test_success_returns_issued_client_id(self):
        pending = make_pending()
        cb = oauth.CallbackResult(params={"code": "c", "state": pending.state, "client_id": "oaiapp_issued"})
        assert oauth.validate_callback(cb, pending) == "oaiapp_issued"

    def test_state_mismatch_rejected(self):
        pending = make_pending()
        cb = oauth.CallbackResult(params={"code": "c", "state": "forged", "client_id": "oaiapp_x"})
        with pytest.raises(oauth.CallbackValidationError, match="state"):
            oauth.validate_callback(cb, pending)

    def test_access_denied(self):
        pending = make_pending()
        cb = oauth.CallbackResult(params={"error": "access_denied", "state": pending.state})
        with pytest.raises(oauth.AuthorizationDeniedError):
            oauth.validate_callback(cb, pending)

    def test_missing_state_rejected(self):
        pending = make_pending()
        cb = oauth.CallbackResult(params={"code": "c", "client_id": "oaiapp_x"})
        with pytest.raises(oauth.CallbackValidationError, match="state"):
            oauth.validate_callback(cb, pending)

    def test_missing_code_rejected(self):
        pending = make_pending()
        cb = oauth.CallbackResult(params={"state": pending.state, "client_id": "oaiapp_x"})
        with pytest.raises(oauth.CallbackValidationError, match="code"):
            oauth.validate_callback(cb, pending)

    def test_dynamic_registration_requires_issued_client_id(self):
        pending = make_pending()
        cb = oauth.CallbackResult(params={"code": "c", "state": pending.state})
        with pytest.raises(oauth.CallbackValidationError, match="client_id"):
            oauth.validate_callback(cb, pending)

    def test_reauth_client_id_mismatch_rejected(self):
        pending = make_pending(client_id="oaiapp_A")
        cb = oauth.CallbackResult(params={"code": "c", "state": pending.state, "client_id": "oaiapp_B"})
        with pytest.raises(oauth.CallbackValidationError, match="different client_id"):
            oauth.validate_callback(cb, pending)

    def test_reauth_without_client_id_keeps_pending(self):
        pending = make_pending(client_id="oaiapp_A")
        cb = oauth.CallbackResult(params={"code": "c", "state": pending.state})
        assert oauth.validate_callback(cb, pending) == "oaiapp_A"


# ---------------------------------------------------------------------------
# Token exchange / refresh / revocation
# ---------------------------------------------------------------------------


class TestTokenExchange:
    def test_code_exchange_form(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(oauth.TOKEN_URL)
            captured.update(dict(urllib.parse.parse_qsl(request.content.decode())))
            return httpx.Response(200, json=token_payload())

        pending = make_pending()
        result = oauth.exchange_code(pending, "the-code", "oaiapp_issued", client=mock_client(handler))
        assert result["access_token"] == "at-1"
        assert captured["grant_type"] == "authorization_code"
        assert captured["client_id"] == "oaiapp_issued"
        assert captured["code"] == "the-code"
        assert captured["code_verifier"] == pending.code_verifier
        assert captured["redirect_uri"] == pending.redirect_uri
        assert captured["resource"] == oauth.RESOURCE
        assert "client_secret" not in captured

    def test_exchange_error_raises(self):
        client = mock_client(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
        with pytest.raises(oauth.TokenExchangeError):
            oauth.exchange_code(make_pending(), "c", "oaiapp_x", client=client)


class TestRefresh:
    def test_refresh_omits_scope_and_posts_grant(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(urllib.parse.parse_qsl(request.content.decode())))
            return httpx.Response(200, json=token_payload(access_token="at-2", refresh_token="rt-2"))

        oauth.refresh_tokens(make_record(refresh_token="rt-1"), client=mock_client(handler))
        assert captured["grant_type"] == "refresh_token"
        assert captured["refresh_token"] == "rt-1"
        assert captured["client_id"] == "oaiapp_abc123"
        assert "scope" not in captured

    def test_get_valid_access_token_no_refresh_when_fresh(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record())

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("token endpoint must not be hit")

        token, record = oauth.get_valid_access_token(store, client=mock_client(handler))
        assert token == "at"
        assert record.refresh_token == "rt"

    def test_get_valid_access_token_refreshes_and_rotates(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record(access_expires_at=time.time() - 10))  # expired

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=token_payload(access_token="at-2", refresh_token="rt-2", scope=oauth.REQUESTED_SCOPES))

        token, record = oauth.get_valid_access_token(store, client=mock_client(handler))
        assert token == "at-2"
        persisted = store.load("oaiapp_abc123")
        assert persisted.access_token == "at-2"
        assert persisted.refresh_token == "rt-2"

    def test_refresh_without_inference_scope_fails(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record(access_expires_at=time.time() - 10))
        identity_scope_only = "openid profile email offline_access resource.invoke"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=token_payload(scope=identity_scope_only))

        with pytest.raises(oauth.InferenceScopeMissingError):
            oauth.get_valid_access_token(store, client=mock_client(handler))


class TestRevocation:
    def test_revoke_uses_discovery_endpoint(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(oauth.OIDC_DISCOVERY_URL):
                return httpx.Response(200, json={"revocation_endpoint": "https://auth.openai.com/oauth/revoke"})
            seen.update(dict(urllib.parse.parse_qsl(request.content.decode())))
            return httpx.Response(200)

        assert oauth.revoke_refresh_token(make_record(refresh_token="rt-9"), client=mock_client(handler)) is True
        assert seen["token"] == "rt-9"
        assert seen["token_type_hint"] == "refresh_token"
        assert seen["client_id"] == "oaiapp_abc123"

    def test_revoke_without_refresh_token_is_noop(self):
        assert oauth.revoke_refresh_token(make_record(refresh_token=""), client=mock_client(lambda r: httpx.Response(500))) is False

    def test_revoke_transport_failure_returns_false(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        assert oauth.revoke_refresh_token(make_record(), client=mock_client(handler)) is False


# ---------------------------------------------------------------------------
# ID-token validation
# ---------------------------------------------------------------------------


class TestIdToken:
    def test_valid(self, rsa_key, jwks):
        token = make_id_token(rsa_key, client_id="oaiapp_x", nonce="n-1")
        claims = oauth.validate_id_token(token, client_id="oaiapp_x", nonce="n-1", jwks=jwks)
        assert claims["sub"] == "sub-1"
        assert claims["email"] == "user@example.com"

    def test_wrong_nonce(self, rsa_key, jwks):
        token = make_id_token(rsa_key, client_id="oaiapp_x", nonce="n-1")
        with pytest.raises(oauth.IdTokenValidationError, match="nonce"):
            oauth.validate_id_token(token, client_id="oaiapp_x", nonce="different", jwks=jwks)

    def test_wrong_audience(self, rsa_key, jwks):
        token = make_id_token(rsa_key, client_id="oaiapp_x", nonce="n-1")
        with pytest.raises(oauth.IdTokenValidationError):
            oauth.validate_id_token(token, client_id="oaiapp_other", nonce="n-1", jwks=jwks)

    def test_expired(self, rsa_key, jwks):
        token = make_id_token(rsa_key, client_id="oaiapp_x", nonce="n-1", exp=int(time.time()) - 10)
        with pytest.raises(oauth.IdTokenValidationError):
            oauth.validate_id_token(token, client_id="oaiapp_x", nonce="n-1", jwks=jwks)

    def test_forged_signature(self, jwks):
        forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = make_id_token(forged, client_id="oaiapp_x", nonce="n-1")
        with pytest.raises(oauth.IdTokenValidationError):
            oauth.validate_id_token(token, client_id="oaiapp_x", nonce="n-1", jwks=jwks)


# ---------------------------------------------------------------------------
# Credential store
# ---------------------------------------------------------------------------


class TestCredentialStore:
    def test_save_load_roundtrip_and_permissions(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record())
        loaded = store.load("oaiapp_abc123")
        assert loaded is not None
        assert loaded.access_token == "at"
        assert loaded.plan_enabled
        mode = os.stat(tmp_path / "accounts" / "oaiapp_abc123.json").st_mode & 0o777
        assert mode == 0o600

    def test_resolve_single_account(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record())
        assert store.resolve().client_id == "oaiapp_abc123"

    def test_resolve_multi_account_requires_selector(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record(client_id="oaiapp_A", email="a@x.com"))
        store.save(make_record(client_id="oaiapp_B", email="b@x.com", subject="sub-2"))
        with pytest.raises(oauth.CredentialNotFoundError, match="Multiple"):
            store.resolve()
        assert store.resolve("b@x.com").client_id == "oaiapp_B"

    def test_resolve_missing(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        with pytest.raises(oauth.CredentialNotFoundError):
            store.resolve()

    def test_delete_tokens_retains_mapping(self, tmp_path):
        store = oauth.CredentialStore(tmp_path)
        store.save(make_record())
        record = store.resolve()
        store.delete_tokens(record)
        cleared = store.load("oaiapp_abc123")
        assert cleared.revoked
        assert cleared.access_token == ""
        assert cleared.refresh_token == ""
        # Revoked records are invisible to resolution — no silent reuse.
        with pytest.raises(oauth.CredentialNotFoundError):
            store.resolve()

    def test_import_restamps_host_id(self, tmp_path):
        source_store = oauth.CredentialStore(tmp_path / "src")
        source_record = make_record(ext_agent_host_id="urn:uuid:laptop")
        source_store.save(source_record)
        source_path = tmp_path / "src" / "accounts" / "oaiapp_abc123.json"

        vm_store = oauth.CredentialStore(tmp_path / "vm")
        vm_host = vm_store.ensure_host_identity()
        imported = vm_store.import_record(source_path)
        assert imported.ext_agent_host_id == vm_host
        assert vm_store.resolve().access_token == "at"

    def test_redacted_dict_contains_no_tokens(self):
        redacted = make_record().redacted_dict()
        assert "access_token" not in redacted
        assert "refresh_token" not in redacted
        assert "id_token" not in redacted
        blob = json.dumps(redacted)
        assert "at" not in blob.split()
        assert redacted["has_access_token"] is True


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


class TestModelDiscovery:
    def test_lists_visible_models(self):
        seen_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(dict(request.headers))
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "gpt-5.2-codex", "display_name": "GPT-5.2 Codex", "visibility": "list"},
                        {"slug": "internal", "visibility": "hidden"},
                        {"slug": "gpt-5.1", "visibility": "list"},
                    ]
                },
            )

        models = oauth.list_account_models("at-token", client=mock_client(handler))
        assert seen_headers["authorization"] == "Bearer at-token"
        assert [m["slug"] for m in models] == ["gpt-5.2-codex", "gpt-5.1"]
