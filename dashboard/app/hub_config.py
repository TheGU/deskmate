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

from fastapi import Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.logging_setup import log

logger = logging.getLogger("app.hub_config")

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
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return HubConfig.from_json(payload)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log(logger, logging.WARNING, "unreadable hub.json", path=str(path), error=str(exc))
        return None


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
    claimed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.config: HubConfig | None = load_hub_config(path)
        self.claim_code: str | None = None if self.config else generate_claim_code()

    @property
    def configured(self) -> bool:
        return self.config is not None

    def verify_token(self, token: str) -> bool:
        return self.config is not None and token_matches(token, self.config.token_sha256)

    async def claim(self, *, submitted_code: str, name: str, base_url: str) -> str:
        """Validate and persist a setup submission. Returns the plaintext token."""
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
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """``Authorization: Bearer <token>``. One error shape: HTTPException.detail."""
    identity: HubIdentity = request.app.state.hub.identity
    if not identity.configured:
        raise HTTPException(status_code=503, detail="hub is not set up; open /setup")
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not identity.verify_token(token):
        raise HTTPException(status_code=401, detail="invalid bearer token")
