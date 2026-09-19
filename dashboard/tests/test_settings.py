"""Section models, ``HubSettings``, and the store that persists them.

``test_defaults.py`` asserts the honest-empty-state guarantee end to end
(every adapter still resolves to something against an empty ``DATA_DIR``);
the tests below assert the narrower rule the section models and the store
carry on their own: no section defaults to ``fixture``, every default
matches what the hub has always shipped, and a saved section round-trips
exactly, including through a live ``Hub.reload()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import DEFAULT_HA_ENTITIES, Env
from app.db import Database, close_databases
from app.main import create_app
from app.modules.calendar.settings import CalendarSettings, Feed
from app.modules.general.settings import GeneralSettings
from app.modules.home.settings import HomeSettings
from app.modules.tasks.settings import TasksSettings
from app.modules.weather.settings import WeatherSettings
from app.settings import SECTIONS, HubSettings, SettingsStore


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "data" / "deskmate.sqlite")
    instance.migrate()
    return instance


@pytest.fixture()
def store(database: Database) -> SettingsStore:
    return SettingsStore(database)


# ---------------------------------------------------------------------------
# defaults: the live selector, never fixture
# ---------------------------------------------------------------------------
def test_no_section_default_source_is_fixture() -> None:
    for section, model in SECTIONS.items():
        instance = model()
        source = getattr(instance, "source", None)
        if source is not None:
            assert source != "fixture", f"{section} defaults to fixture"


def test_defaults_match_todays_config_defaults() -> None:
    """Every default below is the same value the hub has always shipped, so
    a fresh hub with an empty settings store behaves exactly as an
    unconfigured ``.env`` deployment used to."""
    hub = HubSettings()
    assert hub.general.timezone == "Asia/Bangkok"
    assert hub.general.units == "metric"

    assert hub.tasks.source == "push"
    assert hub.tasks.max_priority_tasks == 3
    assert hub.tasks.ttl_seconds == 300.0
    assert hub.tasks.stale_seconds == 36000.0

    assert hub.calendar.source == "ics"
    assert hub.calendar.feeds == []
    assert hub.calendar.agenda_days == 7
    assert hub.calendar.ttl_seconds == 300.0

    assert hub.weather.source == "open_meteo"
    assert hub.weather.ttl_seconds == 900.0

    assert hub.ai_usage.source == "push"
    assert hub.ai_usage.ttl_seconds == 300.0
    assert hub.ai_usage.stale_seconds == 21600.0

    assert hub.brief.source == "push"
    assert hub.brief.evening_hour == 14
    assert hub.brief.ttl_seconds == 60.0
    assert hub.brief.stale_seconds == 36000.0

    assert hub.home.source == "rest"
    assert hub.home.url == ""
    assert hub.home.token.get_secret_value() == ""
    assert hub.home.ttl_seconds == 120.0

    assert hub.device.source == "store"
    assert hub.device.retention_days == 30
    assert hub.device.ttl_seconds == 60.0

    assert hub.alert.default_duration_seconds == 90


def test_sections_are_registered_in_wizard_order() -> None:
    assert list(SECTIONS) == [
        "general",
        "tasks",
        "calendar",
        "weather",
        "ai_usage",
        "brief",
        "home",
        "device",
        "alert",
    ]


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
def test_missing_row_gives_defaults(store: SettingsStore) -> None:
    assert store.load("general") == GeneralSettings()
    assert store.updated_at("general") is None


def test_a_saved_section_round_trips(store: SettingsStore) -> None:
    saved = GeneralSettings(timezone="Europe/Berlin", units="imperial")
    store.save("general", saved)
    assert store.load("general") == saved
    assert store.updated_at("general") is not None


def test_a_corrupt_row_falls_back_to_defaults_with_a_warning(
    database: Database, store: SettingsStore, caplog: pytest.LogCaptureFixture
) -> None:
    with database.writing() as connection:
        connection.execute(
            "INSERT INTO settings (section, value_json, updated_at) VALUES (?, ?, ?)",
            ("general", "{ not json", "2026-09-19T00:00:00.000+00:00"),
        )
    with caplog.at_level(logging.WARNING, logger="app.settings"):
        assert store.load("general") == GeneralSettings()
    assert any(
        getattr(record, "fields", {}).get("section") == "general" for record in caplog.records
    )


def test_a_row_that_fails_validation_falls_back_to_defaults_with_a_warning(
    database: Database, store: SettingsStore, caplog: pytest.LogCaptureFixture
) -> None:
    with database.writing() as connection:
        connection.execute(
            "INSERT INTO settings (section, value_json, updated_at) VALUES (?, ?, ?)",
            (
                "general",
                json.dumps({"timezone": "Not/AZone", "units": "metric"}),
                "2026-09-19T00:00:00.000+00:00",
            ),
        )
    with caplog.at_level(logging.WARNING, logger="app.settings"):
        assert store.load("general") == GeneralSettings()
    assert any(
        getattr(record, "fields", {}).get("section") == "general" for record in caplog.records
    )


def test_extra_keys_in_a_row_are_ignored(store: SettingsStore, database: Database) -> None:
    with database.writing() as connection:
        connection.execute(
            "INSERT INTO settings (section, value_json, updated_at) VALUES (?, ?, ?)",
            (
                "general",
                json.dumps({"timezone": "Europe/Berlin", "units": "metric", "old_field": "gone"}),
                "2026-09-19T00:00:00.000+00:00",
            ),
        )
    assert store.load("general") == GeneralSettings(timezone="Europe/Berlin", units="metric")


def test_snapshot_builds_a_hub_settings_from_every_section(store: SettingsStore) -> None:
    store.save("general", GeneralSettings(timezone="Europe/Berlin", units="metric"))
    snapshot = store.snapshot()
    assert isinstance(snapshot, HubSettings)
    assert snapshot.general.timezone == "Europe/Berlin"
    assert snapshot.tasks == store.load("tasks")


def test_secret_str_round_trips_through_the_store(
    store: SettingsStore, database: Database
) -> None:
    saved = HomeSettings(source="rest", url="http://ha.lan:8123", token="ha-secret")
    store.save("home", saved)

    loaded = store.load("home")
    assert isinstance(loaded, HomeSettings)
    assert loaded.token.get_secret_value() == "ha-secret"

    with database.reading() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE section = 'home'"
        ).fetchone()
    # This database is the hub's own secret store: the plain value is what
    # gets written, never pydantic's masked "**********" display string.
    assert "ha-secret" in row["value_json"]
    assert "**********" not in row["value_json"]


# ---------------------------------------------------------------------------
# the store is the source of HubSettings (1.2c)
# ---------------------------------------------------------------------------
def test_config_settings_no_longer_exists() -> None:
    """1.2c deletes ``config.py:Settings``, ``get_settings`` and
    ``reset_settings_cache`` along with ``HubSettings.from_env``: the store is
    the only source of a running hub's settings now."""
    import app.config as config_module

    assert not hasattr(config_module, "Settings")
    assert not hasattr(config_module, "get_settings")
    assert not hasattr(config_module, "reset_settings_cache")
    assert not hasattr(HubSettings, "from_env")


def test_reload_serves_a_section_saved_directly_through_the_store(tmp_path: Path) -> None:
    """The plan's headline behavior for 1.2c: ``SettingsStore`` is the source
    of ``HubSettings``, so a section saved straight on the store - never
    handed to ``create_app`` as a seed - is what the next ``Hub.reload()``
    serves, and what ``/healthz`` and ``/api/hub`` report from their
    snapshot."""
    env = Env(_env_file=None, DATA_DIR=tmp_path, LOG_LEVEL="WARNING")
    app = create_app(env)
    try:
        with TestClient(app) as client:
            hub = client.app.state.hub
            token = asyncio.run(
                hub.identity.claim(name="deskmate", base_url="http://dashboard-hub.lan:8080")
            ).token

            hub.settings_store.save("weather", WeatherSettings(source="fixture"))
            hub.settings_store.save("tasks", TasksSettings(source="fixture"))
            asyncio.run(hub.reload())

            healthz = client.get("/healthz", headers=auth(token)).json()
            assert healthz["adapters"]["weather"]["source"] == "fixture"

            hub_info = client.get("/api/hub", headers=auth(token)).json()
            assert hub_info["sources"]["tasks"]["source"] == "fixture"
    finally:
        close_databases()


def test_create_apps_seed_never_overwrites_a_row_the_store_already_has(tmp_path: Path) -> None:
    """``create_app(env, hub_settings=...)``'s seed is for a fresh store
    only: once a section has a row (here, from the first start's seed
    itself), a later seed for that same section must not clobber it."""
    env = Env(_env_file=None, DATA_DIR=tmp_path, LOG_LEVEL="WARNING")
    try:
        first = create_app(env, HubSettings(general=GeneralSettings(timezone="Europe/Berlin")))
        assert first.state.hub.hub_settings.general.timezone == "Europe/Berlin"

        second = create_app(env, HubSettings(general=GeneralSettings(timezone="Asia/Tokyo")))
        assert second.state.hub.hub_settings.general.timezone == "Europe/Berlin"
    finally:
        close_databases()


# ---------------------------------------------------------------------------
# small convenience helpers
# ---------------------------------------------------------------------------
def test_bad_timezone_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        GeneralSettings(timezone="Not/AZone")


def test_feed_name_falls_back_to_host_then_number() -> None:
    calendar = CalendarSettings(
        feeds=[
            Feed(url="https://a.example/work.ics", name="Work"),
            Feed(url="https://b.example/home.ics"),
            Feed(url="not-a-url"),
        ]
    )
    assert calendar.feed_name(0) == "Work"
    assert calendar.feed_name(1) == "b.example"
    assert calendar.feed_name(2) == "calendar 3"
    assert calendar.feed_name(9) == "calendar 10"


def test_entity_map_default_equals_default_ha_entities() -> None:
    assert HomeSettings().entity_map() == DEFAULT_HA_ENTITIES
