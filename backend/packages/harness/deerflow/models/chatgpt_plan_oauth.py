"""Sign in with ChatGPT (ChatGPT plan usage) — OAuth layer for open-source clients.

Implements the officially documented OSS authorization-code + PKCE flow:

    https://developers.openai.com/siwc/token-sharing-open-source

What this module does:
    - Stable ``ext_agent_host_id`` per installation (RFC 9278 JWK-thumbprint URN,
      the doc-recommended format; persisted keypair under the credentials dir).
    - Dynamic client registration via ``client_id=dynamic_agent_client`` plus
      ``agent_name_hint`` on first sign-in; issued ``client_id`` reuse afterwards.
    - Authorization URL construction with fresh ``state``, OIDC ``nonce`` and
      PKCE (S256) per attempt, and a ``127.0.0.1`` loopback callback listener.
    - Callback validation (state match, ``error`` handling, issued-client-id
      capture and mismatch rejection).
    - Authorization-code exchange at ``auth.openai.com`` (public client — no
      client secret), ID-token validation against OpenAI's JWKS (issuer,
      audience, expiry, nonce), and ``chatgpt.tokens.use.direct`` scope checks.
    - Credential records stored atomically with ``0600`` permissions, token
      refresh serialized through an on-disk lock, and renewable-session
      revocation via the OIDC ``revocation_endpoint``.

What this module deliberately does NOT do:
    - It never talks to ``chatgpt.com/backend-api/*``. Inference goes to the
      public Responses API ``https://api.openai.com/v1/responses``.
    - It never reads Codex CLI credentials (``~/.codex/auth.json``). Those are
      a different authorization grant and are not interchangeable with SIWC
      OAuth credentials.
    - It never writes tokens to logs, URLs, the frontend, or artifacts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

from deerflow.config.runtime_paths import runtime_home

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
OIDC_DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
MODELS_URL = "https://api.openai.com/v1/models"
RESPONSES_URL = "https://api.openai.com/v1/responses"

# Scope contract from the SIWC docs. ``chatgpt.tokens.use.direct`` is the
# inference grant; without it the sign-in is retained but plan usage is
# disabled (errors-and-recovery: "ChatGPT plan use isn't enabled").
IDENTITY_SCOPES = ("openid", "profile", "email")
PLAN_SCOPES = ("offline_access", "resource.invoke", "chatgpt.tokens.use.direct")
REQUESTED_SCOPES = " ".join((*IDENTITY_SCOPES, *PLAN_SCOPES))
REQUIRED_INFERENCE_SCOPE = "chatgpt.tokens.use.direct"

DYNAMIC_CLIENT_ID = "dynamic_agent_client"
DEFAULT_AGENT_NAME_HINT = "DeerFlow"
DEFAULT_CALLBACK_PATH = "/auth/callback"
# 1455 is the loopback port shown throughout the official docs.
DEFAULT_CALLBACK_PORT = 1455
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 60.0

# Seconds before access-token expiry at which a refresh is attempted.
_ACCESS_EXPIRY_BUFFER_SECONDS = 60


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChatGPTPlanOAuthError(Exception):
    """Base class for ChatGPT-plan OAuth failures."""


class CredentialStoreError(ChatGPTPlanOAuthError):
    """Credential storage is unreadable, corrupted, or insecure."""


class CredentialNotFoundError(ChatGPTPlanOAuthError):
    """No authorized ChatGPT-plan credential exists for the requested account."""


class AuthorizationPendingTimeout(ChatGPTPlanOAuthError):
    """The loopback callback was never received before the deadline."""


class AuthorizationDeniedError(ChatGPTPlanOAuthError):
    """The user declined consent (callback carried ``error=access_denied``)."""


class CallbackValidationError(ChatGPTPlanOAuthError):
    """The callback failed validation (bad state, mismatched client_id, ...)."""


class TokenExchangeError(ChatGPTPlanOAuthError):
    """The authorization-code or refresh-token exchange failed."""


class IdTokenValidationError(ChatGPTPlanOAuthError):
    """The returned ID token failed signature/claims validation."""


class InferenceScopeMissingError(ChatGPTPlanOAuthError):
    """The grant lacks ``chatgpt.tokens.use.direct`` — plan usage is disabled."""


class AccountIdentityMismatchError(ChatGPTPlanOAuthError):
    """A reauthorized ID token does not match the selected account."""


# ---------------------------------------------------------------------------
# Host identity (ext_agent_host_id)
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(value: int, length: int) -> str:
    return _b64url(value.to_bytes(length, "big"))


def host_id_from_public_jwk(public_jwk: dict[str, str]) -> str:
    """RFC 9278 thumbprint of a public JWK, expressed as a URN.

    The thumbprint hashes the required JWK members in lexicographic order
    (``json.dumps(sort_keys=True, separators=(",", ":"))``), exactly as
    RFC 9278 §3 specifies.
    """

    required = {key: public_jwk[key] for key in ("crv", "kty", "x", "y")}
    canonical = json.dumps(required, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return f"urn:ietf:params:oauth:jwk-thumbprint:{_b64url(digest)}"


def _generate_ec_keypair() -> tuple[Any, dict[str, str]]:
    """Generate a P-256 keypair and return (private_key, public_jwk)."""

    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.public_key().public_numbers()
    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _int_to_b64url(numbers.x, 32),
        "y": _int_to_b64url(numbers.y, 32),
    }
    return private_key, public_jwk


def _private_key_to_pem(private_key: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _private_key_from_pem(pem: str) -> Any:
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(pem.encode("ascii"), password=None)


def _public_jwk_from_private_key(private_key: Any) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _int_to_b64url(numbers.x, 32),
        "y": _int_to_b64url(numbers.y, 32),
    }


# ---------------------------------------------------------------------------
# Credential records and storage
# ---------------------------------------------------------------------------


@dataclass
class CredentialRecord:
    """One authorized ChatGPT account registration.

    Mirrors the credential record the docs prescribe: the validated identity,
    the issued client ID, tokens, granted scopes, and expiry information.
    ``ext_agent_host_id`` identifies the host that performed authorization.
    """

    email: str
    issuer: str
    subject: str
    client_id: str
    ext_agent_host_id: str
    id_token: str
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    scopes: list[str] = field(default_factory=list)
    saved_at: float = 0.0
    # Derived/operational fields (not part of the doc's record shape):
    access_expires_at: float = 0.0
    earliest_refresh_at: float = 0.0
    agent_name_hint: str = DEFAULT_AGENT_NAME_HINT
    # True when the user signed out locally but the registration mapping is
    # retained for a later sign-in (the docs require keeping the mapping).
    revoked: bool = False

    @property
    def plan_enabled(self) -> bool:
        """Whether ``chatgpt.tokens.use.direct`` was granted."""

        return REQUIRED_INFERENCE_SCOPE in self.scopes

    def access_token_expired(self, now: float | None = None) -> bool:
        if not self.access_token:
            return True
        if self.access_expires_at <= 0:
            return False
        now = time.time() if now is None else now
        return now >= self.access_expires_at - _ACCESS_EXPIRY_BUFFER_SECONDS

    def refresh_due(self, now: float | None = None) -> bool:
        """Whether the token endpoint wants a refresh now (earliest_refresh_at)."""

        if self.earliest_refresh_at <= 0:
            return False
        now = time.time() if now is None else now
        return now >= self.earliest_refresh_at

    def redacted_dict(self) -> dict[str, Any]:
        """Log/CLI-safe view: tokens and id_token are removed, not masked."""

        data = asdict(self)
        for key in ("id_token", "access_token", "refresh_token"):
            data.pop(key, None)
        data["has_access_token"] = bool(self.access_token)
        data["has_refresh_token"] = bool(self.refresh_token)
        return data

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CredentialRecord:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_token_response(
        cls,
        token_response: dict[str, Any],
        *,
        client_id: str,
        ext_agent_host_id: str,
        claims: dict[str, Any],
        agent_name_hint: str = DEFAULT_AGENT_NAME_HINT,
        previous: CredentialRecord | None = None,
    ) -> CredentialRecord:
        scopes_raw = token_response.get("scope") or ""
        scopes = scopes_raw.split() if isinstance(scopes_raw, str) else list(scopes_raw or [])
        expires_in = int(token_response.get("expires_in") or 3600)
        now = time.time()
        earliest_refresh_at = token_response.get("earliest_refresh_at") or 0
        return cls(
            email=str(claims.get("email") or (previous.email if previous else "")),
            issuer=str(claims.get("iss") or ISSUER),
            subject=str(claims.get("sub") or ""),
            client_id=client_id,
            ext_agent_host_id=ext_agent_host_id,
            id_token=str(token_response.get("id_token") or (previous.id_token if previous else "")),
            access_token=str(token_response.get("access_token") or ""),
            refresh_token=str(token_response.get("refresh_token") or (previous.refresh_token if previous else "")),
            token_type=str(token_response.get("token_type") or "Bearer"),
            expires_in=expires_in,
            scopes=scopes,
            saved_at=now,
            access_expires_at=now + expires_in,
            earliest_refresh_at=float(earliest_refresh_at),
            agent_name_hint=agent_name_hint,
        )


def default_credentials_dir() -> Path:
    """Credential root: ``$CHATGPT_PLAN_CREDENTIALS_DIR`` or ``<runtime_home>/chatgpt-plan``."""

    override = os.getenv("CHATGPT_PLAN_CREDENTIALS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return runtime_home() / "chatgpt-plan"


def _atomic_write_0600(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` atomically with owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:  # Windows / exotic filesystems
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe_account_filename(client_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in client_id)
    if not safe or safe in {".", ".."}:
        raise CredentialStoreError(f"client_id {client_id!r} does not map to a safe filename")
    return f"{safe}.json"


class CredentialStore:
    """File-backed credential storage (one record per issued client_id)."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_credentials_dir()
        self._accounts_dir = self.root / "accounts"
        self._host_file = self.root / "host.json"
        self._lock_file = self.root / ".refresh.lock"
        self._mem_lock = threading.Lock()

    # -- host identity -----------------------------------------------------

    def ensure_host_identity(self) -> str:
        """Return this host's stable ``ext_agent_host_id``, creating it once."""

        self.root.mkdir(parents=True, exist_ok=True)
        if self._host_file.is_file():
            try:
                data = json.loads(self._host_file.read_text(encoding="utf-8"))
                host_id = data.get("ext_agent_host_id")
                pem = data.get("private_key_pem")
                if isinstance(host_id, str) and host_id and isinstance(pem, str) and pem:
                    return host_id
            except (json.JSONDecodeError, OSError) as exc:
                raise CredentialStoreError(f"Unreadable host identity at {self._host_file}: {exc}") from exc
            raise CredentialStoreError(f"Host identity file at {self._host_file} is missing required fields")

        private_key, public_jwk = _generate_ec_keypair()
        host_id = host_id_from_public_jwk(public_jwk)
        payload = json.dumps(
            {
                "ext_agent_host_id": host_id,
                "public_jwk": public_jwk,
                "private_key_pem": _private_key_to_pem(private_key),
                "created_at": time.time(),
            }
        )
        _atomic_write_0600(self._host_file, payload)
        logger.info("Minted new ext_agent_host_id %s", host_id)
        return host_id

    # -- account records ---------------------------------------------------

    def _record_path(self, client_id: str) -> Path:
        return self._accounts_dir / _safe_account_filename(client_id)

    def save(self, record: CredentialRecord) -> None:
        _atomic_write_0600(self._record_path(record.client_id), json.dumps(record.to_dict(), indent=2))

    def load(self, client_id: str) -> CredentialRecord | None:
        path = self._record_path(client_id)
        if not path.is_file():
            return None
        try:
            return CredentialRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            raise CredentialStoreError(f"Unreadable credential record at {path}: {exc}") from exc

    def list(self) -> list[CredentialRecord]:
        if not self._accounts_dir.is_dir():
            return []
        records: list[CredentialRecord] = []
        for path in sorted(self._accounts_dir.glob("*.json")):
            try:
                records.append(CredentialRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError, OSError) as exc:
                logger.warning("Skipping unreadable credential record %s: %s", path.name, exc)
        return records

    def resolve(self, selector: str | None = None) -> CredentialRecord:
        """Return the record matching ``selector`` (email, subject, or client_id).

        With ``selector=None`` exactly one stored registration is allowed; a
        multi-account store requires an explicit selector so inference can
        never silently bill the wrong ChatGPT account.
        """

        records = [r for r in self.list() if not r.revoked]
        if selector is None:
            if not records:
                raise CredentialNotFoundError(f"No authorized ChatGPT account under {self.root}. Run 'python -m deerflow.models.chatgpt_plan_oauth login' first.")
            if len(records) > 1:
                choices = ", ".join(sorted(r.email or r.client_id for r in records))
                raise CredentialNotFoundError(f"Multiple authorized ChatGPT accounts exist ({choices}); set the model's 'account' config key to pick one.")
            return records[0]
        for record in records:
            if selector in {record.client_id, record.email, record.subject}:
                return record
        raise CredentialNotFoundError(f"No authorized ChatGPT account matches {selector!r} under {self.root}")

    def delete_tokens(self, record: CredentialRecord) -> None:
        """Sign out: clear tokens but retain the account/client mapping."""

        record.access_token = ""
        record.refresh_token = ""
        record.id_token = ""
        record.revoked = True
        record.saved_at = time.time()
        self.save(record)

    def import_record(self, source_path: Path | str) -> CredentialRecord:
        """Import a credential file produced on another host (VM transfer).

        Per the self-hosted-VM guide, the record is re-stamped with THIS
        host's ``ext_agent_host_id`` so a copied laptop host ID does not
        overwrite the VM's own identity.
        """

        src = Path(source_path).expanduser().resolve()
        try:
            record = CredentialRecord.from_dict(json.loads(src.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            raise CredentialStoreError(f"Cannot import credential record from {src}: {exc}") from exc
        record.ext_agent_host_id = self.ensure_host_identity()
        record.saved_at = time.time()
        self.save(record)
        return record

    def refresh_lock(self):
        """Cross-process refresh serialization (flock on POSIX, in-process otherwise)."""

        return _RefreshLock(self._lock_file, self._mem_lock)


class _RefreshLock:
    """Serializes refresh-token use across processes sharing a credentials dir.

    Refresh tokens rotate: two racing processes would invalidate each other's
    newly written token. ``fcntl.flock`` covers POSIX; elsewhere a process-local
    lock is the best available approximation.
    """

    def __init__(self, lock_path: Path, mem_lock: threading.Lock) -> None:
        self._lock_path = lock_path
        self._mem_lock = mem_lock
        self._fd: int | None = None

    def __enter__(self):
        self._mem_lock.acquire()
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX)
            except ImportError:
                pass
        except BaseException:
            self._mem_lock.release()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            if self._fd is not None:
                try:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except ImportError:
                    pass
                os.close(self._fd)
                self._fd = None
        finally:
            self._mem_lock.release()


# ---------------------------------------------------------------------------
# Authorization attempt state
# ---------------------------------------------------------------------------


@dataclass
class PendingAuthorization:
    """Per-attempt values that must be kept until the callback completes."""

    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    expected_subject: str | None = None
    created_at: float = field(default_factory=time.time)

    @classmethod
    def mint(cls, *, redirect_uri: str, client_id: str, expected_subject: str | None = None) -> PendingAuthorization:
        return cls(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
            redirect_uri=redirect_uri,
            client_id=client_id,
            expected_subject=expected_subject,
        )

    @property
    def code_challenge(self) -> str:
        digest = hashlib.sha256(self.code_verifier.encode("ascii")).digest()
        return _b64url(digest)


def build_authorization_url(
    pending: PendingAuthorization,
    *,
    agent_name_hint: str | None = None,
    ext_agent_host_id: str,
    id_token_hint: str | None = None,
    login_hint: str | None = None,
    force_reconsent: bool = False,
    authorize_url: str = AUTHORIZE_URL,
) -> str:
    """Build the authorization request URL.

    ``agent_name_hint`` is sent ONLY for first-time dynamic registration
    (``client_id=dynamic_agent_client``); the docs require omitting it on
    reauthorization with an issued client ID.
    """

    if pending.client_id == DYNAMIC_CLIENT_ID and not agent_name_hint:
        raise ValueError("agent_name_hint is required for first-time dynamic registration")

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": pending.client_id,
        "redirect_uri": pending.redirect_uri,
        "scope": REQUESTED_SCOPES,
        "resource": RESOURCE,
        "state": pending.state,
        "nonce": pending.nonce,
        "code_challenge_method": "S256",
        "code_challenge": pending.code_challenge,
        "ext_agent_host_id": ext_agent_host_id,
    }
    if pending.client_id == DYNAMIC_CLIENT_ID and agent_name_hint:
        params["agent_name_hint"] = agent_name_hint
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    if login_hint:
        params["login_hint"] = login_hint
    if force_reconsent:
        # Supported pre-rollout alternative to force_reconsent=true (docs:
        # errors-and-recovery). Ordinary sign-ins must not force consent.
        params["prompt"] = "consent"

    return f"{authorize_url}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Loopback callback listener
# ---------------------------------------------------------------------------


@dataclass
class CallbackResult:
    params: dict[str, str]


class OAuthCallbackListener:
    """Loopback-only callback listener.

    Binds ``127.0.0.1`` immediately so the redirect URI minted for the
    authorization request is the exact URI the browser returns to — the docs
    require loopback (never ``localhost``) and allow only the port to vary.
    """

    def __init__(self, port: int, callback_path: str) -> None:
        self._callback_path = callback_path
        self._result: dict[str, str] | None = None
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != outer._callback_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                outer._result = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                # Plain text only — paired HTML tags are inventory-scanned by the
                # framework-tag denylist test.
                body = b"Sign in with ChatGPT complete. You can return to your application.\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                # Callback URLs carry the authorization code — never log the path.
                logger.debug("OAuth callback request: %s", self.command)

        try:
            self._server = HTTPServer(("127.0.0.1", port), _Handler)
        except OSError:
            self._server = HTTPServer(("127.0.0.1", 0), _Handler)
            logger.info("Callback port %d busy; bound ephemeral port %d", port, self._server.server_address[1])
        self._server.timeout = 0.25

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self._callback_path}"

    def wait(self, timeout_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS) -> CallbackResult:
        deadline = time.monotonic() + timeout_seconds
        try:
            while self._result is None:
                if time.monotonic() > deadline:
                    raise AuthorizationPendingTimeout(f"No OAuth callback received on {self.redirect_uri} within {timeout_seconds}s")
                try:
                    self._server.handle_request()
                except (OSError, ValueError) as exc:
                    raise AuthorizationPendingTimeout(f"OAuth callback listener closed while waiting: {exc}") from exc
        finally:
            self._server.server_close()
        return CallbackResult(params=self._result)

    def close(self) -> None:
        self._server.server_close()


def run_loopback_listener(
    *,
    port: int,
    callback_path: str,
    timeout_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
) -> tuple[CallbackResult, str]:
    """Convenience wrapper: listen once and return (result, redirect_uri)."""

    listener = OAuthCallbackListener(port, callback_path)
    try:
        return listener.wait(timeout_seconds), listener.redirect_uri
    finally:
        listener.close()


def validate_callback(callback: CallbackResult, pending: PendingAuthorization) -> str:
    """Validate the callback; return the issued/expected client_id to exchange.

    Order per the docs: handle OAuth ``error`` first, then validate ``state``,
    then resolve the client ID (new-registration callbacks carry the issued
    one; reauthorization may omit it and must then keep the pending one).
    """

    params = callback.params

    returned_state = params.get("state")
    if returned_state is not None and returned_state != pending.state:
        raise CallbackValidationError("OAuth callback state does not match the pending authorization attempt")

    error = params.get("error")
    if error:
        if error == "access_denied":
            raise AuthorizationDeniedError("The user declined the authorization request (access_denied)")
        description = params.get("error_description") or error
        raise CallbackValidationError(f"OAuth authorization failed: {description}")

    if returned_state is None:
        raise CallbackValidationError("OAuth callback did not return the state parameter")
    if not params.get("code"):
        raise CallbackValidationError("OAuth callback did not return an authorization code")

    issued_client_id = params.get("client_id")
    if pending.client_id == DYNAMIC_CLIENT_ID:
        # Dynamic registration MUST come back with an issued client id.
        if not issued_client_id:
            raise CallbackValidationError("Dynamic registration callback is missing the issued client_id")
        return issued_client_id
    if issued_client_id and issued_client_id != pending.client_id:
        # The docs require rejecting rather than swapping registrations.
        raise CallbackValidationError("Callback returned a different client_id than the pending reauthorization")
    return pending.client_id


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


def _post_form(client: httpx.Client, url: str, form: dict[str, str]) -> dict[str, Any]:
    response = client.post(url, data=form)
    if response.status_code != 200:
        detail = response.text[:500]
        raise TokenExchangeError(f"Token endpoint returned HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise TokenExchangeError("Token endpoint returned a non-JSON body") from exc


def exchange_code(
    pending: PendingAuthorization,
    code: str,
    client_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Exchange the authorization code. Public client — no client secret."""

    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": pending.code_verifier,
        "redirect_uri": pending.redirect_uri,
        "resource": RESOURCE,
    }
    if client is not None:
        return _post_form(client, TOKEN_URL, form)
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as new_client:
        return _post_form(new_client, TOKEN_URL, form)


def fetch_oidc_discovery(*, client: httpx.Client | None = None, discovery_url: str = OIDC_DISCOVERY_URL) -> dict[str, Any]:
    if client is not None:
        return client.get(discovery_url).json()
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as new_client:
        return new_client.get(discovery_url).json()


def fetch_jwks(*, client: httpx.Client | None = None, discovery_url: str = OIDC_DISCOVERY_URL) -> dict[str, Any]:
    discovery = fetch_oidc_discovery(client=client, discovery_url=discovery_url)
    jwks_uri = discovery.get("jwks_uri")
    if not jwks_uri:
        raise IdTokenValidationError("OIDC discovery document has no jwks_uri")
    if client is not None:
        return client.get(jwks_uri).json()
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as new_client:
        return new_client.get(jwks_uri).json()


def validate_id_token(
    id_token: str,
    *,
    client_id: str,
    nonce: str,
    jwks: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    issuer: str = ISSUER,
) -> dict[str, Any]:
    """Verify signature, issuer, audience, expiry, and nonce; return claims."""

    import jwt

    if jwks is None:
        jwks = fetch_jwks(client=client)
    try:
        signing_key = jwt.PyJWKSet.from_dict(jwks)
    except Exception as exc:  # PyJWKSet raises generic errors on bad material
        raise IdTokenValidationError(f"Cannot load OpenAI JWKS: {exc}") from exc

    try:
        header = jwt.get_unverified_header(id_token)
        key = next(k for k in signing_key.keys if k.key_id == header.get("kid"))
    except Exception as exc:
        raise IdTokenValidationError(f"ID token signing key not found in JWKS: {exc}") from exc

    try:
        claims = jwt.decode(
            id_token,
            key=key.key,
            algorithms=["RS256", "ES256"],
            audience=client_id,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise IdTokenValidationError(f"ID token validation failed: {exc}") from exc

    if claims.get("nonce") != nonce:
        raise IdTokenValidationError("ID token nonce does not match the pending authorization attempt")
    return claims


def require_inference_scope(record: CredentialRecord) -> None:
    """Raise unless the grant includes ``chatgpt.tokens.use.direct``."""

    if not record.plan_enabled:
        raise InferenceScopeMissingError(f"Account {record.email or record.subject} did not grant '{REQUIRED_INFERENCE_SCOPE}'. ChatGPT plan usage is disabled for this sign-in; re-run login to re-consent or configure an API-key provider.")


# ---------------------------------------------------------------------------
# Refresh / access
# ---------------------------------------------------------------------------


def refresh_tokens(
    record: CredentialRecord,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run the refresh grant and return the raw token response."""

    if not record.refresh_token:
        raise CredentialNotFoundError("This account has no refresh token; sign in again")
    form = {
        "grant_type": "refresh_token",
        "client_id": record.client_id,
        "refresh_token": record.refresh_token,
        "resource": RESOURCE,
        # ``scope`` is intentionally omitted to retain the original grant.
    }
    if client is not None:
        return _post_form(client, TOKEN_URL, form)
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as new_client:
        return _post_form(new_client, TOKEN_URL, form)


def get_valid_access_token(
    store: CredentialStore,
    selector: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> tuple[str, CredentialRecord]:
    """Return a usable access token, refreshing (serialized) when needed.

    The refresh lock is taken before re-reading the record so a racing process
    cannot spend a refresh token that a peer just rotated.
    """

    with store.refresh_lock():
        record = store.resolve(selector)
        if not record.access_token_expired() and not record.refresh_due():
            return record.access_token, record

        token_response = refresh_tokens(record, client=client)
        refreshed = CredentialRecord.from_token_response(
            token_response,
            client_id=record.client_id,
            ext_agent_host_id=record.ext_agent_host_id,
            claims={"email": record.email, "iss": record.issuer, "sub": record.subject},
            agent_name_hint=record.agent_name_hint,
            previous=record,
        )
        require_inference_scope(refreshed)
        store.save(refreshed)
        logger.info("Refreshed ChatGPT-plan access token for account %s", refreshed.email or refreshed.subject)
        return refreshed.access_token, refreshed


def revoke_refresh_token(record: CredentialRecord, *, client: httpx.Client | None = None) -> bool:
    """Revoke the renewable session via the OIDC revocation endpoint.

    Returns True when revocation was confirmed (empty HTTP 200, which is also
    returned for already-invalid tokens). On transport failure returns False —
    callers must still clear local tokens and tell the user remote revocation
    was not confirmed.
    """

    if not record.refresh_token:
        return False
    try:
        discovery = fetch_oidc_discovery(client=client)
        endpoint = discovery.get("revocation_endpoint")
        if not endpoint:
            return False
        form = {
            "token": record.refresh_token,
            "token_type_hint": "refresh_token",
            "client_id": record.client_id,
        }
        if client is not None:
            response = client.post(endpoint, data=form)
        else:
            with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as new_client:
                response = new_client.post(endpoint, data=form)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def list_account_models(access_token: str, *, client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Return the account's model catalog entries with ``visibility == "list"``."""

    headers = {"Authorization": f"Bearer {access_token}"}
    if client is not None:
        response = client.get(MODELS_URL, headers=headers)
    else:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as new_client:
            response = new_client.get(MODELS_URL, headers=headers)
    response.raise_for_status()
    payload = response.json()
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    return [{"slug": m.get("slug") or "", "display_name": m.get("display_name") or m.get("slug") or ""} for m in models if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug")]


# ---------------------------------------------------------------------------
# Interactive login (CLI)
# ---------------------------------------------------------------------------


def login(
    *,
    credentials_dir: Path | str | None = None,
    account: str | None = None,
    agent_name_hint: str = DEFAULT_AGENT_NAME_HINT,
    port: int = DEFAULT_CALLBACK_PORT,
    callback_path: str = DEFAULT_CALLBACK_PATH,
    timeout_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    open_browser: bool = True,
    out=None,
    client: httpx.Client | None = None,
) -> CredentialRecord:
    """Drive the full authorization dance and persist the credential record.

    ``out`` is the stream the auth URL is printed on (defaults to stdout);
    the URL is printed even when a browser is opened so headless users can
    copy it to any machine that can reach their ChatGPT sign-in — the
    callback must still land on THIS host's loopback listener.
    """

    out = out if out is not None else sys.stdout
    store = CredentialStore(credentials_dir)
    ext_agent_host_id = store.ensure_host_identity()

    previous: CredentialRecord | None = None
    if account:
        previous = store.resolve(account)
        client_id = previous.client_id
    else:
        client_id = DYNAMIC_CLIENT_ID

    # Bind the listener BEFORE minting the pending attempt: the redirect URI
    # (including the bound port) must be identical in the auth request and the
    # code exchange, so the port has to be known up front.
    listener = OAuthCallbackListener(port, callback_path)
    redirect_uri = listener.redirect_uri

    pending = PendingAuthorization.mint(
        redirect_uri=redirect_uri,
        client_id=client_id,
        expected_subject=previous.subject if previous else None,
    )

    url = build_authorization_url(
        pending,
        agent_name_hint=agent_name_hint if client_id == DYNAMIC_CLIENT_ID else None,
        ext_agent_host_id=ext_agent_host_id,
        id_token_hint=previous.id_token if previous else None,
        login_hint=previous.email if previous else None,
    )

    print("\nOpen this URL to sign in with ChatGPT:\n", file=out)
    print(f"  {url}\n", file=out)
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    callback = listener.wait(timeout_seconds)
    issued_client_id = validate_callback(callback, pending)
    code = callback.params["code"]

    token_response = exchange_code(pending, code, issued_client_id, client=client)
    claims = validate_id_token(
        token_response.get("id_token", ""),
        client_id=issued_client_id,
        nonce=pending.nonce,
        client=client,
    )
    if previous and claims.get("sub") != previous.subject:
        raise AccountIdentityMismatchError("Reauthorization returned a different ChatGPT account than the selected registration; refusing to replace credentials")

    record = CredentialRecord.from_token_response(
        token_response,
        client_id=issued_client_id,
        ext_agent_host_id=ext_agent_host_id,
        claims=claims,
        agent_name_hint=agent_name_hint,
        previous=previous,
    )
    store.save(record)

    if not record.plan_enabled:
        print(
            f"\nSigned in as {record.email or record.subject} — BUT the grant lacks '{REQUIRED_INFERENCE_SCOPE}'. ChatGPT plan inference is DISABLED for this account until re-consented.\n",
            file=out,
        )
    else:
        print(f"\nSigned in as {record.email or record.subject}; client_id {record.client_id}. Credentials saved under {store.root}.\n", file=out)
    return record


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m deerflow.models.chatgpt_plan_oauth", description="Sign in with ChatGPT — ChatGPT plan usage OAuth for DeerFlow")
    parser.add_argument("--credentials-dir", default=None, help="Override the credential store directory (default: $CHATGPT_PLAN_CREDENTIALS_DIR or <runtime_home>/chatgpt-plan)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Authorize a ChatGPT account for plan usage")
    p_login.add_argument("--account", default=None, help="Reauthorize an existing registration (email, subject, or client_id)")
    p_login.add_argument("--agent-name", default=DEFAULT_AGENT_NAME_HINT, help="agent_name_hint shown during first registration")
    p_login.add_argument("--port", type=int, default=DEFAULT_CALLBACK_PORT)
    p_login.add_argument("--timeout", type=float, default=DEFAULT_CALLBACK_TIMEOUT_SECONDS)
    p_login.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")

    sub.add_parser("accounts", help="List authorized accounts (redacted)")

    p_models = sub.add_parser("models", help="List models available to an authorized account")
    p_models.add_argument("--account", default=None, help="Account selector (email, subject, or client_id)")

    p_logout = sub.add_parser("logout", help="Revoke the renewable session and clear local tokens")
    p_logout.add_argument("--account", default=None, help="Account selector (email, subject, or client_id)")

    p_import = sub.add_parser("import", help="Import a credential file transferred from another host (self-hosted VM flow)")
    p_import.add_argument("path", help="Path to the credential JSON file")

    args = parser.parse_args(argv)
    store = CredentialStore(args.credentials_dir)

    if args.command == "login":
        login(
            credentials_dir=args.credentials_dir,
            account=args.account,
            agent_name_hint=args.agent_name,
            port=args.port,
            timeout_seconds=args.timeout,
            open_browser=not args.no_browser,
        )
        return 0

    if args.command == "accounts":
        records = store.list()
        if not records:
            print(f"No authorized accounts under {store.root}")
            return 0
        for record in records:
            print(json.dumps(record.redacted_dict(), indent=2))
        return 0

    if args.command == "models":
        token, record = get_valid_access_token(store, args.account)
        for model in list_account_models(token):
            print(f"{model['slug']:<40} {model['display_name']}")
        return 0

    if args.command == "logout":
        record = store.resolve(args.account)
        confirmed = revoke_refresh_token(record)
        store.delete_tokens(record)
        if confirmed:
            print("Remote session revoked and local tokens cleared.")
        else:
            print("Local tokens cleared; remote revocation was NOT confirmed. Disconnect the app in ChatGPT settings to be certain.")
        return 0

    if args.command == "import":
        record = store.import_record(args.path)
        print(f"Imported account {record.email or record.subject} (client_id {record.client_id}) onto this host.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
