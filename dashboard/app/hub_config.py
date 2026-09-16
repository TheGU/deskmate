"""Hub identity: ``hub.json``, the claim code, the bearer token, and the
``require_token`` dependency built on top of them.

``hub.json`` lives in ``DATA_DIR`` (gitignored) and holds ``{"schema": 1,
"name", "base_url", "token_sha256", "created_at"}``. Only the token's
SHA-256 hex digest is ever written to disk; the plaintext token is shown
once, on the setup-done page, and is not recoverable. Losing it means
stopping the container, deleting ``data/hub.json``, and running ``/setup``
again: there is no edit or regenerate mode.

The functions below are pure (claim code and token generation, hashing,
base URL validation, the claim decision) or plain synchronous file I/O
(``load_hub_config`` / ``write_hub_config``), so a test can drive every rule
with nothing more than a temp path. :class:`HubIdentity` is the thin,
stateful wrapper ``main.py`` holds for the life of the process.
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
#: of FastAPI's own 403, so require_token keeps the one 401/503 error shape
#: for the whole API. Declaring it this way (rather than a bare Header
#: check) is what makes /openapi.json carry a bearer security scheme, so
#: Swagger UI gets an Authorize button (docs/DATA-SOURCES.md, item 12).
_bearer_scheme = HTTPBearer(
    auto_error=False, description="Hub bearer token, shown once on /setup."
)

HUB_CONFIG_SCHEMA = 1

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
    """``hub.json`` exists but cannot be trusted: bad JSON, a missing key, or
    a schema other than 1. Distinct from "absent" (which means unconfigured):
    a present-but-broken file must never be treated as a fresh install, or
    the hub would generate a new claim code next to a config file nobody can
    read.
    """


@dataclass(frozen=True, slots=True)
class HubConfig:
    """The on-disk shape of ``hub.json``."""

    name: str
    base_url: str
    token_sha256: str
    created_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": HUB_CONFIG_SCHEMA,
            "name": self.name,
            "base_url": self.base_url,
            "token_sha256": self.token_sha256,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "HubConfig":
        return cls(
            name=str(payload["name"]),
            base_url=str(payload["base_url"]),
            token_sha256=str(payload["token_sha256"]),
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
) -> tuple[HubConfig, str]:
    """Validate one ``POST /setup`` submission and build the new config.

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
    config = HubConfig(
        name=clean_name,
        base_url=clean_url,
        token_sha256=hash_token(token),
        created_at=datetime.now(tz=dt_timezone.utc),
    )
    return config, token


# ---------------------------------------------------------------------------
# File I/O (synchronous; call through run_in_threadpool from async code)
# ---------------------------------------------------------------------------
def load_hub_config(path: Path) -> HubConfig | None:
    """Read ``hub.json``.

    Returns ``None`` only when the file is absent: that means unconfigured,
    and the caller should generate a claim code. When the file exists but is
    corrupt JSON, not an object, missing a key, or ``schema`` is not 1, this
    raises :class:`HubConfigUnreadable` instead of returning ``None`` -
    silently treating a broken file as "unconfigured" would hand out a fresh
    claim code next to a config nobody can read.
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
    if payload.get("schema") != HUB_CONFIG_SCHEMA:
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

    async def claim(self, *, submitted_code: str, name: str, base_url: str) -> str:
        """Validate and persist a setup submission. Returns the plaintext token.

        Guarded by an ``asyncio.Lock``: the "not yet configured" check and
        the write both happen while holding it, so two ``POST /setup``
        requests racing each other cannot both pass the check. The loser
        gets :class:`AlreadyConfigured` (409), not a clobbered file or two
        valid tokens.
        """
        async with self._claim_lock:
            config, token = claim_hub(
                existing=self.config,
                expected_code=self.claim_code or "",
                submitted_code=submitted_code,
                name=name,
                base_url=base_url,
            )
            await run_in_threadpool(write_hub_config, self._path, config)
            self.config = config
            self.claim_code = None
            return token


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """``Authorization: Bearer <token>``, case-insensitive scheme (HTTPBearer's
    own comparison). One error shape: HTTPException.detail.
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
