"""Pure functions behind hub identity: claim code, token, base URL, claim_hub."""

from __future__ import annotations

import re
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest

from app.hub_config import (
    AlreadyConfigured,
    HubConfig,
    InvalidBaseURL,
    WrongClaimCode,
    claim_hub,
    generate_claim_code,
    generate_token,
    hash_token,
    load_hub_config,
    token_matches,
    validate_base_url,
    write_hub_config,
)


def test_claim_code_matches_the_xxxx_xxxx_shape() -> None:
    code = generate_claim_code()
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code)


def test_claim_code_excludes_ambiguous_characters() -> None:
    codes = "".join(generate_claim_code() for _ in range(200))
    assert not set(codes) & set("O0I1-").difference({"-"})


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
    ],
)
def test_validate_base_url_rejects_the_bad_shapes(value: str) -> None:
    with pytest.raises(InvalidBaseURL):
        validate_base_url(value)


def _config(token_sha256: str = "x") -> HubConfig:
    return HubConfig(
        name="deskmate",
        base_url="http://dashboard-hub.lan:8080",
        token_sha256=token_sha256,
        created_at=datetime.now(tz=dt_timezone.utc),
    )


def test_claim_hub_rejects_the_wrong_code() -> None:
    with pytest.raises(WrongClaimCode):
        claim_hub(
            existing=None,
            expected_code="AAAA-AAAA",
            submitted_code="BBBB-BBBB",
            name="deskmate",
            base_url="http://dashboard-hub.lan:8080",
        )


def test_claim_hub_rejects_when_already_configured() -> None:
    with pytest.raises(AlreadyConfigured):
        claim_hub(
            existing=_config(),
            expected_code="AAAA-AAAA",
            submitted_code="AAAA-AAAA",
            name="deskmate",
            base_url="http://dashboard-hub.lan:8080",
        )


def test_claim_hub_succeeds_and_only_the_hash_is_kept() -> None:
    config, token = claim_hub(
        existing=None,
        expected_code="AAAA-AAAA",
        submitted_code="aaaa-aaaa",
        name="  My Hub  ",
        base_url="http://dashboard-hub.lan:8080/",
    )
    assert config.name == "My Hub"
    assert config.base_url == "http://dashboard-hub.lan:8080"
    assert config.token_sha256 == hash_token(token)
    assert token not in config.to_json().values()


def test_write_and_load_hub_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "hub.json"
    config, _token = claim_hub(
        existing=None,
        expected_code="AAAA-AAAA",
        submitted_code="AAAA-AAAA",
        name="deskmate",
        base_url="http://dashboard-hub.lan:8080",
    )
    write_hub_config(path, config)
    loaded = load_hub_config(path)
    assert loaded == config


def test_write_hub_config_is_atomic_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "hub.json"
    write_hub_config(path, _config())
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    assert path.is_file()


def test_load_hub_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_hub_config(tmp_path / "missing.json") is None


def test_load_hub_config_returns_none_for_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "hub.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_hub_config(path) is None
