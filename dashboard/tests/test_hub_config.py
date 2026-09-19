"""Pure functions behind hub identity: token, base URL, the private-address
check, claim_hub, the session cookie."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest

from app.hub_config import (
    AlreadyConfigured,
    ClaimedSecrets,
    HubConfig,
    HubConfigUnreadable,
    HubIdentity,
    InvalidBaseURL,
    SESSION_MAX_AGE_SECONDS,
    claim_hub,
    generate_token,
    hash_token,
    is_private_client_host,
    load_hub_config,
    mint_session_cookie,
    session_cookie_valid,
    token_matches,
    validate_base_url,
    write_hub_config,
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


def test_write_and_load_hub_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "hub.json"
    config, _token, _device_key = claim_hub(
        existing=None,
        name="deskmate",
        base_url="http://dashboard-hub.lan:8080",
    )
    write_hub_config(path, config)
    loaded = load_hub_config(path)
    assert loaded == config
    stored_text = path.read_text(encoding="utf-8")
    assert '"session_secret"' in stored_text
    assert '"device_key_sha256"' in stored_text


def test_write_hub_config_is_atomic_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "hub.json"
    write_hub_config(path, _config())
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    assert path.is_file()


def test_load_hub_config_returns_none_when_absent(tmp_path: Path) -> None:
    """Absent means unconfigured: a fresh install, safe to hand out a claim code."""
    assert load_hub_config(tmp_path / "missing.json") is None


def test_load_hub_config_raises_for_corrupt_json(tmp_path: Path) -> None:
    """Present but broken is not the same as absent: it must not be treated
    as a fresh install (that would hand out a claim code next to a hub.json
    nobody can read)."""
    path = tmp_path / "hub.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(HubConfigUnreadable):
        load_hub_config(path)


def test_load_hub_config_raises_for_a_missing_key(tmp_path: Path) -> None:
    """Schema matches, but a required key is missing - the from_json path,
    not the schema-1 path (which has its own test above)."""
    path = tmp_path / "hub.json"
    path.write_text(json.dumps({"schema": 2, "name": "deskmate"}), encoding="utf-8")
    with pytest.raises(HubConfigUnreadable):
        load_hub_config(path)


def test_load_hub_config_raises_for_the_wrong_schema(tmp_path: Path) -> None:
    path = tmp_path / "hub.json"
    config = _config()
    payload = {**config.to_json(), "schema": 3}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HubConfigUnreadable):
        load_hub_config(path)


def test_load_hub_config_raises_a_pointed_message_for_schema_1(tmp_path: Path) -> None:
    """Schema 1 predates the read key: there is no device key or session
    secret to migrate, only a config to redo. The detail must send an
    operator to /setup, not "fix the JSON"."""
    path = tmp_path / "hub.json"
    payload = {
        "schema": 1,
        "name": "deskmate",
        "base_url": "http://dashboard-hub.lan:8080",
        "token_sha256": "x",
        "created_at": datetime.now(tz=dt_timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HubConfigUnreadable, match="/setup"):
        load_hub_config(path)


# -- HubIdentity: startup state and the claim race --------------------------
def test_identity_with_an_unreadable_config_is_unconfigured_with_an_error(tmp_path: Path) -> None:
    path = tmp_path / "hub.json"
    path.write_text("{ not json", encoding="utf-8")
    identity = HubIdentity(path)
    assert identity.configured is False
    assert identity.error is not None
    assert str(path) in identity.error


def test_identity_with_no_hub_json_is_unconfigured(tmp_path: Path) -> None:
    identity = HubIdentity(tmp_path / "hub.json")
    assert identity.configured is False
    assert identity.error is None


def test_concurrent_claims_issue_exactly_one_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        identity = HubIdentity(tmp_path / "hub.json")
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


# -- session cookie -----------------------------------------------------
def test_session_cookie_round_trips_when_valid() -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now)
    assert session_cookie_valid("s3cr3t", cookie, now)


def test_session_cookie_rejects_after_expiry() -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now)
    past_expiry = now + SESSION_MAX_AGE_SECONDS + 1
    assert not session_cookie_valid("s3cr3t", cookie, past_expiry)


def test_session_cookie_rejects_a_tampered_mac() -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now)
    exp, _, mac = cookie.partition(".")
    tampered = f"{exp}.{'0' if mac[0] != '0' else '1'}{mac[1:]}"
    assert not session_cookie_valid("s3cr3t", tampered, now)


def test_session_cookie_rejects_the_wrong_secret() -> None:
    now = 1_000_000.0
    cookie = mint_session_cookie("s3cr3t", now)
    assert not session_cookie_valid("a-different-secret", cookie, now)


@pytest.mark.parametrize("malformed", ["", "no-dot-here", "not-an-int.deadbeef", "."])
def test_session_cookie_rejects_malformed_values(malformed: str) -> None:
    assert not session_cookie_valid("s3cr3t", malformed, 1_000_000.0)
