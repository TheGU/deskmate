"""The hub's one database: the schema, the version gate, the process-wide
registry, and the two things ``Hub`` gets from all of it (one shared
connection per ``DATA_DIR``, and a reload that picks up a new ``hub`` row).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, Settings
from app.db import (
    DB_SCHEMA_VERSION,
    VERSION_KEY,
    Database,
    close_databases,
    get_database,
)
from app.hub_config import HubIdentity, claim_hub, write_hub_config
from app.main import create_app
from app.models import AlertRequest

FIXTURES_DIR = REPO_ROOT / "fixtures"

TABLES = ("meta", "hub", "settings", "datasets", "telemetry")


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[Database]:
    instance = Database(tmp_path / "deskmate.sqlite")
    yield instance
    instance.close()


def hub_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def test_a_fresh_database_is_at_version_one(database: Database) -> None:
    assert database.schema_version is None
    assert database.migrate() == 1
    assert DB_SCHEMA_VERSION == 1
    assert database.schema_version == 1


def test_migrate_creates_every_table(database: Database) -> None:
    database.migrate()
    with database.reading() as connection:
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    for table in TABLES:
        assert table in names


def test_the_telemetry_table_carries_the_migration_only_columns(database: Database) -> None:
    """The four power/wake columns and the two origin columns were added to
    ``telemetry.sqlite`` by migration after it shipped; a fresh table has to
    be born with them or an import would drop those values."""
    database.migrate()
    with database.reading() as connection:
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(telemetry)")
        }
    assert {
        "battery_mode",
        "usb_present",
        "charge_state",
        "wake_cause",
        "remote_addr",
        "hub_host",
    } <= columns


def test_migrate_is_idempotent(database: Database) -> None:
    assert database.migrate() == database.migrate() == DB_SCHEMA_VERSION


def test_the_hub_table_holds_exactly_one_row(database: Database) -> None:
    """``id`` is pinned to 1 by a CHECK, so a second identity cannot be
    inserted beside the first even by a caller that forgets the upsert."""
    database.migrate()
    with pytest.raises(sqlite3.IntegrityError):
        with database.writing() as connection:
            connection.execute(
                "INSERT INTO hub (id, name, base_url, token_sha256, device_key_sha256,"
                " session_secret, created_at) VALUES (2, 'x', 'y', 'z', 'k', 's', 't')"
            )


def test_a_newer_database_refuses_to_start(tmp_path: Path) -> None:
    path = tmp_path / "deskmate.sqlite"
    first = Database(path)
    first.migrate()
    first.meta_set(VERSION_KEY, str(DB_SCHEMA_VERSION + 1))
    first.close()

    later = Database(path)
    try:
        with pytest.raises(RuntimeError, match="newer dashboard-hub"):
            later.migrate()
    finally:
        later.close()


def test_an_unparseable_version_also_refuses_to_start(tmp_path: Path) -> None:
    """A version this build cannot even read is treated as newer than
    anything: refusing is the only safe reading of it."""
    path = tmp_path / "deskmate.sqlite"
    first = Database(path)
    first.migrate()
    first.meta_set(VERSION_KEY, "banana")
    first.close()

    later = Database(path)
    try:
        with pytest.raises(RuntimeError):
            later.migrate()
    finally:
        later.close()


def test_a_newer_database_stops_create_app(tmp_path: Path) -> None:
    seed = Database(tmp_path / "deskmate.sqlite")
    seed.migrate()
    seed.meta_set(VERSION_KEY, str(DB_SCHEMA_VERSION + 1))
    seed.close()
    with pytest.raises(RuntimeError, match="newer dashboard-hub"):
        create_app(hub_settings(tmp_path))
    close_databases()


# ---------------------------------------------------------------------------
# writes, meta, lifetime
# ---------------------------------------------------------------------------
def test_a_failed_write_rolls_back(database: Database) -> None:
    database.migrate()
    with pytest.raises(sqlite3.IntegrityError):
        with database.writing() as connection:
            connection.execute(
                "INSERT INTO datasets (name, payload_json, received_at) VALUES ('a', '1', 't')"
            )
            connection.execute(
                "INSERT INTO datasets (name, payload_json, received_at) VALUES ('a', '2', 't')"
            )
    with database.reading() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM datasets").fetchone()["n"] == 0


def test_meta_round_trips(database: Database) -> None:
    database.migrate()
    assert database.meta_get("thing") is None
    database.meta_set("thing", "one")
    database.meta_set("thing", "two")
    assert database.meta_get("thing") == "two"


def test_the_registry_hands_back_one_database_per_path(tmp_path: Path) -> None:
    first = get_database(tmp_path / "deskmate.sqlite")
    second = get_database(tmp_path / "sub" / ".." / "deskmate.sqlite")
    try:
        assert first is second
    finally:
        first.close()


def test_closing_drops_the_database_out_of_the_registry(tmp_path: Path) -> None:
    first = get_database(tmp_path / "deskmate.sqlite")
    first.close()
    second = get_database(tmp_path / "deskmate.sqlite")
    try:
        assert second is not first
    finally:
        second.close()


def test_reopen_brings_a_closed_database_back(tmp_path: Path) -> None:
    """What the restore flow needs: close, swap the file, reopen, migrate,
    with every holder of the object still pointing at the same instance."""
    database = get_database(tmp_path / "deskmate.sqlite")
    database.migrate()
    database.meta_set("thing", "one")
    database.close()

    database.reopen()
    try:
        assert database.migrate() == DB_SCHEMA_VERSION
        assert database.meta_get("thing") == "one"
        assert get_database(tmp_path / "deskmate.sqlite") is database
    finally:
        database.close()


# ---------------------------------------------------------------------------
# Hub over the shared database
# ---------------------------------------------------------------------------
def test_two_apps_over_one_data_dir_share_one_database(tmp_path: Path) -> None:
    settings = hub_settings(tmp_path)
    first = create_app(settings)
    second = create_app(settings)
    try:
        assert first.state.hub.db is second.state.hub.db
        assert first.state.hub.db.path == tmp_path / "deskmate.sqlite"
    finally:
        close_databases()


def test_reload_rebuilds_the_identity_from_the_hub_row(tmp_path: Path) -> None:
    """Write a new ``hub`` row behind the running Hub, reload, and the token
    it was verifying a moment ago stops verifying."""
    settings = hub_settings(tmp_path)
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            hub = client.app.state.hub
            first = asyncio.run(
                hub.identity.claim(name="deskmate", base_url="http://dashboard-hub.lan:8080")
            )
            assert hub.identity.verify_token(first.token) is True

            replacement, token, device_key = claim_hub(
                existing=None, name="second", base_url="http://other.lan:8080"
            )
            write_hub_config(hub.db, replacement)
            # Still the old identity until the reload: the row changed, the
            # object did not.
            assert hub.identity.verify_token(first.token) is True

            identity_before = hub.identity
            asyncio.run(hub.reload())

            assert hub.identity is not identity_before
            assert isinstance(hub.identity, HubIdentity)
            assert hub.identity.verify_token(first.token) is False
            assert hub.identity.verify_token(token) is True
            assert hub.identity.verify_device_key(device_key) is True
            assert hub.identity.config is not None
            assert hub.identity.config.name == "second"
    finally:
        close_databases()


def test_reload_keeps_the_alert_and_drops_the_render_cache(tmp_path: Path) -> None:
    settings = hub_settings(tmp_path)
    app = create_app(settings)
    try:
        hub = app.state.hub
        hub.alerts.set(AlertRequest(title="Someone is at the door"))
        hub._cache["today"] = object()  # noqa: SLF001 - the cache drop is the point

        asyncio.run(hub.reload())

        assert hub.alerts.current is not None
        assert hub.alerts.current.title == "Someone is at the door"
        assert hub._cache == {}  # noqa: SLF001
    finally:
        close_databases()
