"""Pure functions behind hub identity: token, base URL, the private-address
check, claim_hub, the session cookie."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Iterator
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest

from app.db import Database
from app.hub_config import (
    ADMIN_SESSION_MAX_AGE_SECONDS,
    AlreadyConfigured,
    ClaimedSecrets,
    HubConfig,
    HubConfigUnreadable,
    HubIdentity,
    InvalidBaseURL,
    READER_SESSION_MAX_AGE_SECONDS,
    claim_hub,
    generate_token,
    hash_token,
    is_private_client_host,
    load_hub_config,
    mint_session_cookie,
    session_cookie_valid,
    session_role,
    token_matches,
    validate_base_url,
    write_hub_config,
)


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[Database]:
    instance = Database(tmp_path / "deskmate.sqlite")
    instance.migrate()
    yield instance
    instance.close()


def corrupt_the_hub_row(database: Database) -> None:
    """A row that is present but cannot be trusted: ``created_at`` is not a
    timestamp. Present-but-broken must never read as "unconfigured"."""
    with database.writing() as connection:
        connection.execute(
            "INSERT INTO hub (id, name, base_url, token_sha256, device_key_sha256,"
            " session_secret, created_at) VALUES (1, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET created_at = excluded.created_at",
            ("deskmate", "http://dashboard-hub.lan:8080", "x", "y", "s", "not-a-timestamp"),
        )


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.1.2.3", "192.168.1.5", "172.16.0.1", "169.254.1.1", "::1", "fe80::1"],
)
def test_is_private_client_host_accepts_local_addresses(host: str) -> None:
    assert is_private_client_host(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1"])
def test_is_private_client_host_rejects_public_addresses(host: str) -> None:
    assert is_private_client_host(host) is False


def test_is_private_client_host_allows_a_missing_host_but_not_an_unparseable_one() -> None:
    """``None`` (no client in the ASGI scope, e.g. a unix socket) is allowed
    - a real deployment always hands this a real client IP. A host that is
    not a parseable IP address - including Starlette's ``TestClient``
    default, "testclient" - must fail closed: waving it through would let a
    caller behind ``uvicorn --proxy-headers`` bypass the guard with a
    forged, unparseable ``X-Forwarded-For`` value."""
    assert is_private_client_host(None) is True
    assert is_private_client_host("testclient") is False


def test_is_private_client_host_rejects_an_ipv4_mapped_public_address() -> None:
    """``::ffff:8.8.8.8`` is a public IPv4 address wrapped in IPv6 notation;
    Python's ``ipaddress`` treats it as a plain, non-private IPv6 literal, so
    this must still come out refused."""
    assert is_private_client_host("::ffff:8.8.8.8") is False


def test_token_hash_round_trips() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert token_matches(token, digest)
    assert not token_matches("wrong-token", digest)


def test_validate_base_url_accepts_a_plain_host() -> None:
    assert validate_base_url("http://dashboard-hub.lan:8080/") == "http://dashboard-hub.lan:8080"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://dashboard-hub.lan",
        "dashboard-hub.lan",
        "http://user:pass@dashboard-hub.lan",
        "http://dashboard-hub.lan/setup",
        "http://dashboard-hub.lan?x=1",
        "http://dashboard-hub.lan:99999",
        "http://dashboard-hub.lan:0",
    ],
)
def test_validate_base_url_rejects_the_bad_shapes(value: str) -> None:
    with pytest.raises(InvalidBaseURL):
        validate_base_url(value)


def test_validate_base_url_accepts_the_edge_ports() -> None:
    assert validate_base_url("http://dashboard-hub.lan:1") == "http://dashboard-hub.lan:1"
    assert validate_base_url("http://dashboard-hub.lan:65535") == "http://dashboard-hub.lan:65535"


def _config(token_sha256: str = "x", device_key_sha256: str = "y") -> HubConfig:
    return HubConfig(
        name="deskmate",
        base_url="http://dashboard-hub.lan:8080",
        token_sha256=token_sha256,
        device_key_sha256=device_key_sha256,
        session_secret="session-secret",
        created_at=datetime.now(tz=dt_timezone.utc),
    )


def test_claim_hub_rejects_when_already_configured() -> None:
    with pytest.raises(AlreadyConfigured):
        claim_hub(
            existing=_config(),
            name="deskmate",
            base_url="http://dashboard-hub.lan:8080",
        )


def test_claim_hub_succeeds_and_only_the_hash_is_kept() -> None:
    config, token, device_key = claim_hub(
        existing=None,
        name="  My Hub  ",
        base_url="http://dashboard-hub.lan:8080/",
    )
    assert config.name == "My Hub"
    assert config.base_url == "http://dashboard-hub.lan:8080"
    assert config.token_sha256 == hash_token(token)
    assert config.device_key_sha256 == hash_token(device_key)
    # Two independent secrets: neither can stand in for the other.
    assert token != device_key
    assert token not in config.to_json().values()
    assert device_key not in config.to_json().values()
    # session_secret is stored in plaintext (server side only, never shown),
    # so it is present verbatim - unlike the two hashed secrets above.
    assert config.session_secret and len(config.session_secret) > 20


def test_write_and_load_hub_config_round_trip(database: Database) -> None:
    config, _token, _device_key = claim_hub(
        existing=None,
        name="deskmate",
        base_url="http://dashboard-hub.lan:8080",
    )
    write_hub_config(database, config)
    assert load_hub_config(database) == config


def test_write_hub_config_replaces_the_single_row(database: Database) -> None:
    """``hub`` holds exactly one row, id 1: a second write is an update, not
    a second identity sitting next to the first."""
    write_hub_config(database, _config(token_sha256="first"))
    write_hub_config(database, _config(token_sha256="second"))
    with database.reading() as connection:
        rows = connection.execute("SELECT id, token_sha256 FROM hub").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["token_sha256"] == "second"


def test_load_hub_config_returns_none_when_the_row_is_absent(database: Database) -> None:
    """Absent means unconfigured: a fresh install, safe to claim."""
    assert load_hub_config(database) is None


def test_load_hub_config_raises_for_a_corrupt_row(database: Database) -> None:
    """Present but broken is not the same as absent: it must not be treated
    as a fresh install (that would mint a fresh token and device key next to
    an identity nobody can read)."""
    corrupt_the_hub_row(database)
    with pytest.raises(HubConfigUnreadable):
        load_hub_config(database)


# -- HubIdentity: startup state and the claim race --------------------------
def test_identity_with_an_unreadable_config_is_unconfigured_with_an_error(
    database: Database,
) -> None:
    corrupt_the_hub_row(database)
    identity = HubIdentity(database)
    assert identity.configured is False
    assert identity.error is not None
    assert str(database.path) in identity.error


def test_identity_with_no_hub_row_is_unconfigured(database: Database) -> None:
    identity = HubIdentity(database)
    assert identity.configured is False
    assert identity.error is None


def test_concurrent_claims_issue_exactly_one_token(database: Database) -> None:
    async def scenario() -> None:
        identity = HubIdentity(database)
        results = await asyncio.gather(
            identity.claim(name="first", base_url="http://a.lan:8080"),
            identity.claim(name="second", base_url="http://b.lan:8080"),
            return_exceptions=True,
        )
        secrets = [item for item in results if isinstance(item, ClaimedSecrets)]
        errors = [item for item in results if isinstance(item, BaseException)]
        assert len(secrets) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], AlreadyConfigured)
        assert identity.configured is True

    asyncio.run(scenario())


def test_a_claim_is_what_a_fresh_identity_reads_back(database: Database) -> None:
    """The claim writes the row; a second HubIdentity over the same database
    (what ``Hub.reload()`` builds) verifies the same token."""
    identity = HubIdentity(database)
    secrets = asyncio.run(identity.claim(name="deskmate", base_url="http://a.lan:8080"))
    reloaded = HubIdentity(database)
    assert reloaded.configured is True
    assert reloaded.verify_token(secrets.token) is True
    assert reloaded.verify_device_key(secrets.device_key) is True


# -- session cookie -----------------------------------------------------
@pytest.mark.parametrize("role", ["admin", "reader"])
def test_session_cookie_round_trips_when_valid(role: str) -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now, role)
    assert session_role("s3cr3t", cookie, now) == role
    assert session_cookie_valid("s3cr3t", cookie, now)


def test_admin_and_reader_cookies_have_different_lifetimes() -> None:
    """Admin cookies (from the token) last 7 days; reader cookies (from the
    device key) last 30: an admin credential is shorter-lived on purpose."""
    now = 1_000_000.0
    admin_cookie = mint_session_cookie("s3cr3t", now, "admin")
    reader_cookie = mint_session_cookie("s3cr3t", now, "reader")
    assert session_role("s3cr3t", admin_cookie, now + ADMIN_SESSION_MAX_AGE_SECONDS - 1) == "admin"
    assert session_role("s3cr3t", admin_cookie, now + ADMIN_SESSION_MAX_AGE_SECONDS + 1) is None
    assert (
        session_role("s3cr3t", reader_cookie, now + READER_SESSION_MAX_AGE_SECONDS - 1)
        == "reader"
    )
    assert session_role("s3cr3t", reader_cookie, now + READER_SESSION_MAX_AGE_SECONDS + 1) is None


def test_session_cookie_rejects_after_expiry() -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now, "reader")
    past_expiry = now + READER_SESSION_MAX_AGE_SECONDS + 1
    assert session_role("s3cr3t", cookie, past_expiry) is None
    assert not session_cookie_valid("s3cr3t", cookie, past_expiry)


def test_session_cookie_rejects_a_tampered_mac() -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now, "reader")
    payload, _, mac = cookie.rpartition(".")
    tampered = f"{payload}.{'0' if mac[0] != '0' else '1'}{mac[1:]}"
    assert session_role("s3cr3t", tampered, now) is None
    assert not session_cookie_valid("s3cr3t", tampered, now)


def test_session_cookie_rejects_the_wrong_secret() -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now, "reader")
    assert session_role("a-different-secret", cookie, now) is None


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "no-dot-here",
        "not-an-int.deadbeef",
        ".",
        "reader|not-an-int.deadbeef",
        "owner|123456.deadbeef",
    ],
)
def test_session_cookie_rejects_malformed_values(malformed: str) -> None:
    assert session_role("s3cr3t", malformed, 1_000_000.0) is None
    assert not session_cookie_valid("s3cr3t", malformed, 1_000_000.0)


def test_session_cookie_rejects_the_old_pre_role_cookie_format() -> None:
    """Before roles existed, the cookie was ``"<exp>.<mac>"`` with the MAC
    over ``"browser|<exp>"``. That must never be read as any role - the
    upgrade note says every browser is logged out once, not silently
    upgraded to admin."""
    now = 1_000_000.0
    exp = int(now + 1000)
    old_style_mac = hmac.new(
        b"s3cr3t", f"browser|{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    old_cookie = f"{exp}.{old_style_mac}"
    assert session_role("s3cr3t", old_cookie, now) is None
    assert not session_cookie_valid("s3cr3t", old_cookie, now)
