"""Hub identity: ``hub.json``, the claim code, the bearer token, the device
key, the browser session cookie, and the auth dependencies built on top of
them.

``hub.json`` lives in ``DATA_DIR`` (gitignored) and holds ``{"schema": 2,
"name", "base_url", "token_sha256", "device_key_sha256", "session_secret",
"created_at"}``. Only the token's and the device key's SHA-256 hex digests
are ever written to disk; the plaintext values are shown once, on the
setup-done page, and are not recoverable. ``session_secret`` is stored in
plaintext (it never leaves the server; it only signs the browser session
cookie). Losing any of this means stopping the container, deleting
``data/hub.json``, and running ``/setup`` again: there is no edit or
regenerate mode.

The functions below are pure (claim code and token generation, hashing,
base URL validation, the claim decision, the session cookie mint/verify) or
plain synchronous file I/O (``load_hub_config`` / ``write_hub_config``), so
a test can drive every rule with nothing more than a temp path.
:class:`HubIdentity` is the thin, stateful wrapper ``main.py`` holds for the
life of the process.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from app.logging_setup import log

logger = logging.getLogger("app.hub_config")

#: auto_error=False: a missing or wrong-scheme header returns None instead
#: of FastAPI's own 403, so every dependency below keeps one 401/503 error
#: shape for the whole API. Declaring it this way (rather than a bare Header
#: check) is what makes /openapi.json carry a bearer security scheme, so
#: Swagger UI gets an Authorize button (docs/DATA-SOURCES.md, item 12). One
#: instance shared by every dependency in this module, so Swagger sees one
#: bearer scheme, not four.
_bearer_scheme = HTTPBearer(
    auto_error=False, description="Hub bearer token or device key, shown once on /setup."
)

#: The browser session cookie set by POST /login (see mint_session_cookie /
#: session_cookie_valid below). Stateless: no server-side session table, just
#: an expiry and an HMAC over it keyed by the hub's session_secret.
COOKIE_NAME = "deskmate_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

HUB_CONFIG_SCHEMA = 2

#: Uppercase letters and digits with the ambiguous ones (0/O, 1/I) removed,
#: so a code read off a log line is never misheard.
_CLAIM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class HubConfigError(Exception):
    """Base for the setup-flow errors ``main.py`` turns into HTTP responses."""


class WrongClaimCode(HubConfigError):
    """The submitted claim code does not match the one printed at startup."""


class AlreadyConfigured(HubConfigError):
    """``hub.json`` already exists; there is no edit or regenerate mode."""


class InvalidBaseURL(HubConfigError):
    """The submitted base URL fails the http(s)+host, no path/query/creds rule."""


class HubConfigUnreadable(HubConfigError):
    """``hub.json`` exists but cannot be trusted: bad JSON, a missing key, a
    schema other than the current one, or (the schema-1 case) a file written
    before the read key existed. Distinct from "absent" (which means
    unconfigured): a present-but-broken file must never be treated as a
    fresh install, or the hub would generate a new claim code next to a
    config file nobody can read.
    """


@dataclass(frozen=True, slots=True)
class HubConfig:
    """The on-disk shape of ``hub.json``."""

    name: str
    base_url: str
    token_sha256: str
    device_key_sha256: str
    session_secret: str
    created_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": HUB_CONFIG_SCHEMA,
            "name": self.name,
            "base_url": self.base_url,
            "token_sha256": self.token_sha256,
            "device_key_sha256": self.device_key_sha256,
            "session_secret": self.session_secret,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "HubConfig":
        return cls(
            name=str(payload["name"]),
            base_url=str(payload["base_url"]),
            token_sha256=str(payload["token_sha256"]),
            device_key_sha256=str(payload["device_key_sha256"]),
            session_secret=str(payload["session_secret"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------
def generate_claim_code() -> str:
    """An 8-character human-typed code, grouped ``XXXX-XXXX``."""
    raw = "".join(secrets.choice(_CLAIM_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def generate_token() -> str:
    """A URL-safe bearer token. Only its hash is ever stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, token_sha256: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_sha256)


def validate_base_url(value: str) -> str:
    """http or https, a host, no path/query/fragment, no credentials.

    Returns the normalized value (trailing slash stripped) or raises
    :class:`InvalidBaseURL`.
    """
    candidate = value.strip().rstrip("/")
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise InvalidBaseURL("base URL must start with http:// or https://")
    if not parts.hostname:
        raise InvalidBaseURL("base URL must include a host")
    if parts.username or parts.password:
        raise InvalidBaseURL("base URL must not include credentials")
    if parts.path or parts.query or parts.fragment:
        raise InvalidBaseURL("base URL must not include a path or query")
    # ``.port`` raises ValueError itself for anything outside 0-65535 (e.g.
    # ":99999"); port 0 parses but is not a real port, so it is rejected too.
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidBaseURL("base URL port must be between 1 and 65535") from exc
    if port is not None and not (1 <= port <= 65535):
        raise InvalidBaseURL("base URL port must be between 1 and 65535")
    return candidate


def claim_hub(
    *,
    existing: HubConfig | None,
    expected_code: str,
    submitted_code: str,
    name: str,
    base_url: str,
) -> tuple[HubConfig, str, str]:
    """Validate one ``POST /setup`` submission and build the new config.

    Returns ``(config, token, device_key)``: two independent plaintext
    secrets, only their hashes kept in ``config``. The token is for agents
    (write routes); the device key is for the firmware and doubles as a
    reader credential (bearer or /login) once the hub is claimed.

    Raises :class:`AlreadyConfigured`, :class:`WrongClaimCode` or
    :class:`InvalidBaseURL`. Touches no filesystem; the caller persists the
    result with :func:`write_hub_config` (typically inside
    ``run_in_threadpool``).
    """
    if existing is not None:
        raise AlreadyConfigured("hub is already configured")
    if not hmac.compare_digest(submitted_code.strip().upper(), expected_code.strip().upper()):
        raise WrongClaimCode("wrong claim code")
    clean_name = name.strip() or "deskmate"
    clean_url = validate_base_url(base_url)
    token = generate_token()
    device_key = generate_token()
    config = HubConfig(
        name=clean_name,
        base_url=clean_url,
        token_sha256=hash_token(token),
        device_key_sha256=hash_token(device_key),
        # Plaintext, server-side only: it signs the session cookie, it is
        # never shown or sent to a client itself.
        session_secret=secrets.token_urlsafe(32),
        created_at=datetime.now(tz=dt_timezone.utc),
    )
    return config, token, device_key


def mint_session_cookie(session_secret: str, now: float) -> str:
    """A stateless browser session cookie: ``"<exp>.<mac>"``.

    No server-side session table - ``exp`` is the expiry (unix seconds) and
    ``mac`` is an HMAC over it keyed by the hub's ``session_secret``, so
    :func:`session_cookie_valid` can check a cookie against nothing but that
    one secret and the clock.
    """
    exp = int(now + SESSION_MAX_AGE_SECONDS)
    mac = hmac.new(
        session_secret.encode("utf-8"), f"browser|{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{exp}.{mac}"


def session_cookie_valid(session_secret: str, value: str, now: float) -> bool:
    """Reject a malformed value, an expired one, or a tampered MAC."""
    exp_text, sep, mac = value.partition(".")
    if not sep:
        return False
    try:
        exp = int(exp_text)
    except ValueError:
        return False
    if exp <= now:
        return False
    expected = hmac.new(
        session_secret.encode("utf-8"), f"browser|{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(mac, expected)


# ---------------------------------------------------------------------------
# File I/O (synchronous; call through run_in_threadpool from async code)
# ---------------------------------------------------------------------------
def load_hub_config(path: Path) -> HubConfig | None:
    """Read ``hub.json``.

    Returns ``None`` only when the file is absent: that means unconfigured,
    and the caller should generate a claim code. When the file exists but is
    corrupt JSON, not an object, missing a key, or ``schema`` does not match
    :data:`HUB_CONFIG_SCHEMA`, this raises :class:`HubConfigUnreadable`
    instead of returning ``None`` - silently treating a broken file as
    "unconfigured" would hand out a fresh claim code next to a config nobody
    can read. A file at schema 1 (from before the read key existed, so it
    has no device key or session secret to serve reads with) gets its own
    detail: there is nothing to migrate, only to redo.
    """
    if not path.is_file():
        return None
    message = f"hub config unreadable: {path}, fix or delete it"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HubConfigUnreadable(message) from exc
    if not isinstance(payload, dict):
        raise HubConfigUnreadable(message)
    schema = payload.get("schema")
    if schema == 1:
        raise HubConfigUnreadable(
            "hub.json is schema 1, from before the read key; stop the container, "
            "delete data/hub.json and run /setup again"
        )
    if schema != HUB_CONFIG_SCHEMA:
        raise HubConfigUnreadable(message)
    try:
        return HubConfig.from_json(payload)
    except (KeyError, ValueError) as exc:
        raise HubConfigUnreadable(message) from exc


def write_hub_config(path: Path, config: HubConfig) -> None:
    """Atomic: a temp file in the same directory, then ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    with handle:
        json.dump(config.to_json(), handle, indent=2)
    os.replace(handle.name, path)


# ---------------------------------------------------------------------------
# Process-lifetime identity
# ---------------------------------------------------------------------------
class HubIdentity:
    """The hub's config (once claimed) plus the in-memory claim code.

    One instance lives on ``Hub`` (``app/main.py``) for the life of the
    process. Loads ``hub.json`` at construction time; when it is absent, a
    claim code is generated and kept only in memory until the hub is
    claimed. When it exists but cannot be trusted (:class:`HubConfigUnreadable`),
    no claim code is generated either: ``self.error`` carries the detail
    every 503 on this hub repeats until the file is fixed or deleted.

    Process assumes a single worker (see the ``workers=1`` note by the
    uvicorn command in ``Dockerfile``): the claim lock below serializes
    concurrent ``POST /setup`` calls within this process, not across
    processes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._claim_lock = asyncio.Lock()
        self.error: str | None = None
        try:
            self.config: HubConfig | None = load_hub_config(path)
        except HubConfigUnreadable as exc:
            log(logger, logging.ERROR, "hub config unreadable", path=str(path), error=str(exc))
            self.config = None
            self.error = str(exc)
        self.claim_code: str | None = (
            None if (self.config is not None or self.error is not None) else generate_claim_code()
        )

    @property
    def configured(self) -> bool:
        return self.config is not None

    def verify_token(self, token: str) -> bool:
        return self.config is not None and token_matches(token, self.config.token_sha256)

    def verify_device_key(self, value: str) -> bool:
        return self.config is not None and token_matches(value, self.config.device_key_sha256)

    def verify_reader(self, value: str) -> bool:
        """True for either credential a reader may present: the bearer
        token (an agent) or the device key (the firmware, or a human typing
        it at /login)."""
        return self.verify_token(value) or self.verify_device_key(value)

    async def claim(self, *, submitted_code: str, name: str, base_url: str) -> "ClaimedSecrets":
        """Validate and persist a setup submission. Returns both plaintext
        secrets (the caller shows each once; only their hashes are stored).

        Guarded by an ``asyncio.Lock``: the "not yet configured" check and
        the write both happen while holding it, so two ``POST /setup``
        requests racing each other cannot both pass the check. The loser
        gets :class:`AlreadyConfigured` (409), not a clobbered file or two
        valid tokens.
        """
        async with self._claim_lock:
            config, token, device_key = claim_hub(
                existing=self.config,
                expected_code=self.claim_code or "",
                submitted_code=submitted_code,
                name=name,
                base_url=base_url,
            )
            await run_in_threadpool(write_hub_config, self._path, config)
            self.config = config
            self.claim_code = None
            return ClaimedSecrets(token=token, device_key=device_key)


@dataclass(frozen=True, slots=True)
class ClaimedSecrets:
    """The two plaintext secrets :meth:`HubIdentity.claim` hands back, each
    shown exactly once on the setup-done page."""

    token: str
    device_key: str


class LoginRedirect(Exception):
    """Raised by :func:`require_reader_html` instead of an HTTPException: a
    browser hitting an unauthenticated preview route should land on
    ``/login``, not a bare 401 page. ``main.py`` registers the exception
    handler that turns this into the actual redirect.
    """

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path
        super().__init__(next_path)


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """``Authorization: Bearer <token>``, case-insensitive scheme (HTTPBearer's
    own comparison). One error shape: HTTPException.detail. Write routes
    only: never accepts the device key or the session cookie.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        raise HTTPException(status_code=503, detail="hub is not set up; open /setup")
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = credentials.credentials.strip()
    if not token or not identity.verify_token(token):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def reader_authenticated(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> bool:
    """True when this request already carries a valid reader credential:
    the bearer token, the device key, or a valid session cookie. Shared by
    :func:`require_reader`, :func:`require_reader_html` and ``/healthz``
    (which decides its own response shape rather than raising).

    Callers check ``identity.configured`` themselves first: this only knows
    how to check a credential against a config that exists.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if credentials is not None:
        value = credentials.credentials.strip()
        if value and identity.verify_reader(value):
            return True
    config = identity.config
    if config is None:
        return False
    cookie = request.cookies.get(COOKIE_NAME)
    return cookie is not None and session_cookie_valid(config.session_secret, cookie, time.time())


async def require_device(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """``POST /api/device/telemetry``: open (no credential at all) until the
    hub is claimed, matching today's firmware; token or device key bearer
    required afterward. The session cookie is never accepted here - a
    browser tab must not be able to inject a reading just by being signed
    in to /preview.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        return
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    value = credentials.credentials.strip()
    if not value or not identity.verify_reader(value):
        raise HTTPException(status_code=401, detail="invalid bearer token")


async def require_reader(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Read routes (JSON and images): open until the hub is claimed, then
    the bearer token, the device key, or a signed-in browser session.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        return
    if not reader_authenticated(request, credentials):
        raise HTTPException(status_code=401, detail="sign in at /login or send a bearer token")


async def require_reader_html(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Same rule as :func:`require_reader`, for the two HTML preview routes:
    a browser without a credential is sent to /login instead of a bare 401.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        return
    if not reader_authenticated(request, credentials):
        raise LoginRedirect(request.url.path)
