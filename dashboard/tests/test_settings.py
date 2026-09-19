"""Section models, ``HubSettings``, and the store that persists them.

``test_defaults.py`` already asserts the seven ``*_SOURCE`` env defaults are
the live selectors on ``config.py:Settings``; the tests below assert the same
rule where it will actually live once 1.2b and 1.2c switch call sites: on the
section models themselves.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_HA_ENTITIES, Settings
from app.db import Database
from app.legacy import LegacyEnv, import_legacy
from app.modules.calendar.settings import CalendarSettings, Feed
from app.modules.general.settings import GeneralSettings
from app.modules.home.settings import HomeSettings
from app.settings import SECTIONS, HubSettings, SettingsStore


#: Every variable a from_env test builds ``LegacyEnv``/``Settings`` from,
#: cleared first so a developer's shell cannot leak into either side of the
#: comparison (the same guard ``tests/test_legacy.py`` uses).
_FROM_ENV_VARS = (
    "TIMEZONE",
    "UNITS",
    "TASKS_SOURCE",
    "CALENDAR_ICS_URLS",
    "CALENDAR_NAMES",
    "CALENDAR_COLORS",
    "AI_USAGE_SOURCE",
    "BRIEF_SOURCE",
    "HA_URL",
    "HA_TOKEN",
    "HA_ENTITIES",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _FROM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


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
    """Every default below is the same value ``config.py:Settings`` ships,
    so a fresh hub behaves exactly as an unconfigured ``.env`` deployment
    did."""
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
# from_env
# ---------------------------------------------------------------------------
def test_from_env_maps_the_same_way_legacy_import_does(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    env = LegacyEnv(
        _env_file=None,
        TIMEZONE="Europe/Berlin",
        CALENDAR_ICS_URLS="https://a.example/work.ics",
        HA_URL="http://ha.lan:8123",
        HA_TOKEN="ha-secret",
    )
    import_legacy(database, env, tmp_path / "data")
    imported = SettingsStore(database)

    settings = Settings(
        _env_file=None,
        TIMEZONE="Europe/Berlin",
        CALENDAR_ICS_URLS="https://a.example/work.ics",
        HA_URL="http://ha.lan:8123",
        HA_TOKEN="ha-secret",
    )
    from_env = HubSettings.from_env(settings)

    assert from_env.general == imported.load("general")
    assert from_env.calendar.model_dump(mode="json") == imported.load(
        "calendar"
    ).model_dump(mode="json")
    assert from_env.home.model_dump(mode="json") == imported.load("home").model_dump(
        mode="json"
    )
    assert from_env.home.token.get_secret_value() == "ha-secret"


def test_from_env_maps_pushed_sources_the_same_way(tmp_path: Path, clean_env: None) -> None:
    settings = Settings(
        _env_file=None,
        DATA_DIR=tmp_path,
        TASKS_SOURCE="auto",
        AI_USAGE_SOURCE="file",
        BRIEF_SOURCE="auto",
    )
    hub = HubSettings.from_env(settings)
    assert hub.tasks.source == "push"
    assert hub.ai_usage.source == "push"
    assert hub.brief.source == "push"


def test_from_env_default_entities_when_ha_entities_unset(
    tmp_path: Path, clean_env: None
) -> None:
    settings = Settings(_env_file=None, DATA_DIR=tmp_path)
    hub = HubSettings.from_env(settings)
    assert hub.home.entity_map() == DEFAULT_HA_ENTITIES


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
