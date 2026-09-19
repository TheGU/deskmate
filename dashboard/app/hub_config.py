"""Hub identity: the ``hub`` row, the bearer token, the device key, the
browser session cookie, and the auth dependencies built on top of them.

The identity lives in the hub's one database (``DATA_DIR/deskmate.sqlite``,
see ``app/db.py``) as the single row of the ``hub`` table: ``{name, base_url,
token_sha256, device_key_sha256, session_secret, created_at}``. Only the
token's and the device key's SHA-256 hex digests are ever stored; the
plaintext values are shown once, on the setup-done page, and are not
recoverable. ``session_secret`` is stored in plaintext (it never leaves the
server; it only signs the browser session cookie). Losing any of this means
stopping the container, deleting ``data/deskmate.sqlite``, and running
``/setup`` again: there is no edit or regenerate mode yet.

**Roles.** The session cookie carries a :data:`Role`: ``admin`` or
``reader``. Signing in with the bearer token grants ``admin``; signing in
with the device key grants ``reader`` (:func:`mint_session_cookie`,
``main.py:post_login``). ``admin`` cookies last
:data:`ADMIN_SESSION_MAX_AGE_SECONDS` (7 days); ``reader`` cookies last
:data:`READER_SESSION_MAX_AGE_SECONDS` (30 days). :func:`session_role`
never falls back to admin: a malformed, expired, tampered or unknown-role
cookie - including one in the pre-role ``"<exp>.<mac>"`` format - decodes to
``None``, same as no cookie at all. The device key is deliberately never an
admin credential, even though it is a valid reader credential: it sits in
the device's unencrypted flash, so :func:`require_admin` and
:func:`require_admin_html` check a bearer only against
:meth:`HubIdentity.verify_token`, never :meth:`HubIdentity.verify_device_key`
or :meth:`HubIdentity.verify_reader`. Losing a flashed device therefore
never hands out admin access to the hub's settings.

An install from before the database read ``data/hub.json``. That file is
imported once by ``app/legacy.py`` and then left alone, which is the only
thing :data:`HUB_CONFIG_SCHEMA` and :meth:`HubConfig.from_json` are still for.

There is no claim code: the first ``POST /setup`` to reach an unconfigured
hub claims it, first come first served. The only guard is the caller's
address (see :func:`is_private_client_host`) - loopback, RFC1918/ULA
private, or link-local only - so open ``/setup`` right after the first
start, on this machine's own network.

The functions below are pure (token generation, hashing, base URL
validation, the private-address check, the claim decision, the session
cookie mint/verify) or plain synchronous database I/O (``load_hub_config`` /
``write_hub_config``), so a test can drive every rule with nothing more
than a temp database. :class:`HubIdentity` is the thin, stateful wrapper
``main.py`` holds for the life of the process.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from app.db import Database
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
#: session_role below). Stateless: no server-side session table, just a
#: role, an expiry and an HMAC over both keyed by the hub's session_secret.
COOKIE_NAME = "deskmate_session"

#: The two roles a session cookie can carry (see the module docstring).
Role = Literal["admin", "reader"]
_ROLE_WORDS: frozenset[str] = frozenset({"admin", "reader"})

#: The bearer token signs in as admin; the device key signs in as reader.
#: An admin cookie is shorter-lived: it is the credential that reaches the
#: settings pages, so a stolen one should stop working sooner.
ADMIN_SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
READER_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

#: The ``schema`` number the old ``data/hub.json`` carried. Read only by the
#: legacy import in ``app/legacy.py``; the ``hub`` row has no schema of its
#: own (``meta.db_schema_version`` in ``app/db.py`` numbers the database).
HUB_CONFIG_SCHEMA = 2


class HubConfigError(Exception):
    """Base for the setup-flow errors ``main.py`` turns into HTTP responses."""


class AlreadyConfigured(HubConfigError):
    """The ``hub`` row already exists; there is no edit or regenerate mode."""


class InvalidBaseURL(HubConfigError):
    """The submitted base URL fails the http(s)+host, no path/query/creds rule."""


class HubConfigUnreadable(HubConfigError):
    """The ``hub`` row exists but cannot be trusted: a missing value, or a
    ``created_at`` that is not a timestamp. Distinct from "absent" (which
    means unconfigured): a present-but-broken row must never be treated as a
    fresh install, or the hub would accept a new ``POST /setup`` and mint a
    fresh token and device key next to a config nobody can read.

    The legacy import raises it for the same reason against a ``hub.json``
    that is bad JSON, missing a key, or at a schema other than
    :data:`HUB_CONFIG_SCHEMA` (the schema-1 case, a file written before the
    read key existed, gets its own message: there is nothing to migrate).
    """


@dataclass(frozen=True, slots=True)
class HubConfig:
    """The hub's identity: one ``hub`` row, or one legacy ``hub.json``.

    :meth:`to_json` and :meth:`from_json` describe that legacy file, not the
    row; they survive because ``app/legacy.py`` reads ``hub.json`` once at
    the first start after the upgrade.
    """

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
def is_private_client_host(host: str | None) -> bool:
    """True for loopback, RFC1918/ULA private, or link-local: the only
    callers ``POST /setup`` accepts on an unconfigured hub, now that there
    is no claim code to guard it instead.

    ``None`` (the ASGI scope carries no client at all, e.g. a unix socket)
    is treated as allowed: a real deployment always hands this a real client
    IP, so there is no live listener this can silently open up. A host that
    fails to parse as an IP address - including Starlette's ``TestClient``
    default, ``"testclient"`` - is refused rather than waved through:
    failing open here would let a caller behind ``uvicorn --proxy-headers``
    bypass the guard with a forged, unparseable ``X-Forwarded-For`` value.
    Tests that need an unconfigured ``POST /setup`` to succeed must build
    their ``TestClient`` with an explicit private ``client=(host, port)``.
    """
    if host is None:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


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
    name: str,
    base_url: str,
) -> tuple[HubConfig, str, str]:
    """Validate one ``POST /setup`` submission and build the new config.

    Returns ``(config, token, device_key)``: two independent plaintext
    secrets, only their hashes kept in ``config``. The token is for agents
    (write routes); the device key is for the firmware and doubles as a
    reader credential (bearer or /login) once the hub is claimed.

    Raises :class:`AlreadyConfigured` or :class:`InvalidBaseURL`. Touches no
    filesystem; the caller persists the result with :func:`write_hub_config`
    (typically inside ``run_in_threadpool``).
    """
    if existing is not None:
        raise AlreadyConfigured("hub is already configured")
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


def mint_session_cookie(session_secret: str, now: float, role: Role) -> str:
    """A stateless browser session cookie: ``"<role>|<exp>.<mac>"``.

    No server-side session table - ``exp`` is the expiry (unix seconds,
    :data:`ADMIN_SESSION_MAX_AGE_SECONDS` or
    :data:`READER_SESSION_MAX_AGE_SECONDS` out from ``now`` depending on
    ``role``) and ``mac`` is an HMAC over ``"<role>|<exp>"`` keyed by the
    hub's ``session_secret``, so :func:`session_role` can check a cookie
    against nothing but that one secret and the clock. The role sits inside
    the MAC'd payload, not just alongside it, so a cookie cannot be edited
    from reader to admin without invalidating the MAC.
    """
    max_age = ADMIN_SESSION_MAX_AGE_SECONDS if role == "admin" else READER_SESSION_MAX_AGE_SECONDS
    exp = int(now + max_age)
    payload = f"{role}|{exp}"
    mac = hmac.new(session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def session_role(session_secret: str, value: str, now: float) -> Role | None:
    """The role a valid cookie carries, or ``None`` for anything else:
    malformed, expired, a tampered MAC, an unrecognized role word, or the
    pre-role ``"<exp>.<mac>"`` format (the MAC was over ``"browser|<exp>"``
    there, which never parses as ``"<role>|<exp>"`` here, so it always
    fails the role check below - it never falls back to admin, or to
    anything else). Never raises: every malformed shape returns ``None``.
    """
    payload, sep, mac = value.rpartition(".")
    if not sep:
        return None
    role_text, role_sep, exp_text = payload.partition("|")
    if not role_sep or role_text not in _ROLE_WORDS:
        return None
    try:
        exp = int(exp_text)
    except ValueError:
        return None
    if exp <= now:
        return None
    expected = hmac.new(session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    return role_text  # type: ignore[return-value]  # narrowed by the _ROLE_WORDS check above


def session_cookie_valid(session_secret: str, value: str, now: float) -> bool:
    """True when ``value`` decodes to any known role. A thin wrapper around
    :func:`session_role` for a caller that only needs to know a cookie is
    valid at all, not which role it grants."""
    return session_role(session_secret, value, now) is not None


# ---------------------------------------------------------------------------
# Database I/O (synchronous; call through run_in_threadpool from async code)
# ---------------------------------------------------------------------------
_HUB_COLUMNS = (
    "name",
    "base_url",
    "token_sha256",
    "device_key_sha256",
    "session_secret",
    "created_at",
)


def load_hub_config(db: Database) -> HubConfig | None:
    """Read the single ``hub`` row (id 1).

    Returns ``None`` only when the row is absent: that means unconfigured,
    and the next ``POST /setup`` to arrive claims the hub. When the row is
    there but a value is missing or ``created_at`` does not parse, this
    raises :class:`HubConfigUnreadable` instead of returning ``None`` -
    silently treating a broken row as "unconfigured" would let a new
    ``POST /setup`` mint a fresh token and device key next to an identity
    nobody can read.
    """
    with db.reading() as connection:
        row = connection.execute(
            "SELECT " + ", ".join(_HUB_COLUMNS) + " FROM hub WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    message = f"hub config unreadable in {db.path}, fix or delete the database"
    values = dict(row)
    if any(values.get(name) in (None, "") for name in _HUB_COLUMNS):
        raise HubConfigUnreadable(message)
    try:
        created_at = datetime.fromisoformat(str(values["created_at"]))
    except ValueError as exc:
        raise HubConfigUnreadable(message) from exc
    return HubConfig(
        name=str(values["name"]),
        base_url=str(values["base_url"]),
        token_sha256=str(values["token_sha256"]),
        device_key_sha256=str(values["device_key_sha256"]),
        session_secret=str(values["session_secret"]),
        created_at=created_at,
    )


def write_hub_config(db: Database, config: HubConfig) -> None:
    """Write the single ``hub`` row (id 1), replacing whatever was there.

    One statement inside one committed transaction, which is the whole
    reason identity moved out of a JSON file: no temp file, no rename, no
    window where the file is half a config.
    """
    with db.writing() as connection:
        connection.execute(
            "INSERT INTO hub (id, " + ", ".join(_HUB_COLUMNS) + ")"
            " VALUES (1, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{name} = excluded.{name}" for name in _HUB_COLUMNS),
            (
                config.name,
                config.base_url,
                config.token_sha256,
                config.device_key_sha256,
                config.session_secret,
                config.created_at.isoformat(),
            ),
        )


# ---------------------------------------------------------------------------
# Process-lifetime identity
# ---------------------------------------------------------------------------
class HubIdentity:
    """The hub's config, once claimed.

    One instance lives on ``Hub`` (``app/main.py``) for the life of the
    process, and ``Hub.reload()`` builds a fresh one after the ``hub`` row
    changes. Reads the row at construction time. When it exists but cannot
    be trusted (:class:`HubConfigUnreadable`), ``self.error`` carries the
    detail every 503 on this hub repeats until the row is fixed or the
    database is deleted.

    Process assumes a single worker (see the ``workers=1`` note by the
    uvicorn command in ``Dockerfile``): the claim lock below serializes
    concurrent ``POST /setup`` calls within this process, not across
    processes.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._claim_lock = asyncio.Lock()
        self.error: str | None = None
        try:
            self.config: HubConfig | None = load_hub_config(db)
        except (HubConfigUnreadable, sqlite3.Error) as exc:
            log(
                logger,
                logging.ERROR,
                "hub config unreadable",
                path=str(db.path),
                error=str(exc),
            )
            self.config = None
            self.error = str(exc)

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

    async def claim(self, *, name: str, base_url: str) -> "ClaimedSecrets":
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
                name=name,
                base_url=base_url,
            )
            await run_in_threadpool(write_hub_config, self._db, config)
            self.config = config
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


class SetupRedirect(Exception):
    """Raised by :func:`require_reader_html` instead of an HTTPException
    when the hub is not set up yet: a browser hitting a preview route
    should land on ``/setup``, not a bare 503. ``main.py`` registers the
    exception handler that turns this into the actual redirect.
    """


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
    the bearer token, the device key, or a session cookie of either role
    (``admin`` or ``reader`` - an admin is a reader too). Shared by
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
    if cookie is None:
        return False
    return session_role(config.session_secret, cookie, time.time()) is not None


def admin_authenticated(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> bool:
    """True when this request already carries a valid admin credential: the
    bearer token, or a session cookie whose role is ``admin``. Shared by
    :func:`require_admin` and :func:`require_admin_html`.

    Deliberately narrower than :func:`reader_authenticated`: the device key
    is never accepted here, as a bearer or through a cookie. It lives in the
    device's unencrypted flash and is a read-only credential by design (see
    the module docstring and :meth:`HubIdentity.verify_reader`); trusting it
    with admin actions (rotating the Home Assistant token, editing
    calendars, restoring the database) would let anyone who dumps a flashed
    device's storage manage the hub, not just read its pages. So this checks
    the bearer with :meth:`HubIdentity.verify_token` only, never
    :meth:`HubIdentity.verify_reader` or :meth:`HubIdentity.verify_device_key`.

    Callers check ``identity.configured`` themselves first: this only knows
    how to check a credential against a config that exists.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if credentials is not None:
        value = credentials.credentials.strip()
        if value and identity.verify_token(value):
            return True
    config = identity.config
    if config is None:
        return False
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie is None:
        return False
    return session_role(config.session_secret, cookie, time.time()) == "admin"


async def require_device(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """``POST /api/device/telemetry``: 503 until the hub is set up (see
    :func:`require_token`'s detail string, reused here), then the bearer
    token or the device key. The session cookie is never accepted here - a
    browser tab must not be able to inject a reading just by being signed
    in to /preview.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        raise HTTPException(status_code=503, detail="hub is not set up; open /setup")
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    value = credentials.credentials.strip()
    if not value or not identity.verify_reader(value):
        raise HTTPException(status_code=401, detail="invalid bearer token")


async def require_reader(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Read routes (JSON and images): 503 until the hub is set up, then the
    bearer token, the device key, or a signed-in browser session.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        raise HTTPException(status_code=503, detail="hub is not set up; open /setup")
    if not reader_authenticated(request, credentials):
        raise HTTPException(status_code=401, detail="sign in at /login or send a bearer token")


async def require_reader_html(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Same rule as :func:`require_reader`, for the two HTML preview routes:
    a browser is sent to /setup while the hub is unconfigured, or to /login
    when it is configured but the browser carries no credential, instead of
    a bare 503 or 401.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        raise SetupRedirect()
    if not reader_authenticated(request, credentials):
        raise LoginRedirect(request.url.path)


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Admin routes (JSON, e.g. the settings POSTs): 503 until the hub is set
    up, then the bearer token or an admin session cookie - never the device
    key (see :func:`admin_authenticated` for why).
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        raise HTTPException(status_code=503, detail="hub is not set up; open /setup")
    if not admin_authenticated(request, credentials):
        raise HTTPException(
            status_code=401,
            detail="sign in at /login with the token or send the bearer token",
        )


async def require_admin_html(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Same rule as :func:`require_admin`, for the HTML settings pages: a
    browser is sent to /setup while the hub is unconfigured, or to /login
    when it is configured but carries no admin credential, instead of a bare
    503 or 401.
    """
    identity: HubIdentity = request.app.state.hub.identity
    if identity.error is not None:
        raise HTTPException(status_code=503, detail=identity.error)
    if not identity.configured:
        raise SetupRedirect()
    if not admin_authenticated(request, credentials):
        raise LoginRedirect(request.url.path)
