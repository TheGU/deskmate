"""The one-time import of a pre-database install: ``hub.json``,
``telemetry.sqlite``, the four pushed files and the old environment.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.ai_brief import PushBriefAdapter
from app.adapters.ai_usage import PushAIUsageAdapter
from app.adapters.tasks import PushTasksAdapter
from app.alerts import AlertStore
from app.config import Env
from app.db import LEGACY_IMPORTED_KEY, Database, close_databases, get_database
from app.hub_config import HubConfigUnreadable, HubIdentity, claim_hub
from app.legacy import (
    DEFAULT_HA_ENTITIES,
    _read_hub_json,
    LegacyEnv,
    calendar_feeds,
    home_entities,
    import_legacy,
    pushed_source,
    section_documents,
)
from app.main import create_app
from app.modules.ai_usage.settings import AIUsageSettings
from app.modules.brief.settings import BriefSettings
from app.modules.general.settings import GeneralSettings
from app.modules.tasks.settings import TasksSettings

#: Every variable the section table keys off, cleared before a LegacyEnv is
#: built so these tests prove the mapping and not a developer's shell.
SECTION_ENV_VARS = (
    "TIMEZONE",
    "UNITS",
    "TASKS_SOURCE",
    "OBSIDIAN_VAULT_PATH",
    "OBSIDIAN_TASK_GLOB",
    "MAX_PRIORITY_TASKS",
    "TASKS_TTL_SECONDS",
    "TASKS_STALE_SECONDS",
    "CALENDAR_SOURCE",
    "CALENDAR_ICS_URLS",
    "CALENDAR_NAMES",
    "CALENDAR_COLORS",
    "AGENDA_DAYS",
    "CALENDAR_TTL_SECONDS",
    "WEATHER_SOURCE",
    "WEATHER_LATITUDE",
    "WEATHER_LONGITUDE",
    "WEATHER_LOCATION_NAME",
    "WEATHER_TTL_SECONDS",
    "AI_USAGE_SOURCE",
    "AI_USAGE_TTL_SECONDS",
    "AI_USAGE_STALE_SECONDS",
    "BRIEF_SOURCE",
    "BRIEF_EVENING_HOUR",
    "BRIEF_TTL_SECONDS",
    "BRIEF_STALE_SECONDS",
    "HA_SOURCE",
    "HA_URL",
    "HA_TOKEN",
    "HA_ENTITIES",
    "HOME_TTL_SECONDS",
    "DEVICE_SOURCE",
    "TELEMETRY_RETENTION_DAYS",
    "DEVICE_TTL_SECONDS",
    "ALERT_DEFAULT_DURATION_SECONDS",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECTION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[Database]:
    instance = Database(tmp_path / "data" / "deskmate.sqlite")
    instance.migrate()
    yield instance
    instance.close()


def empty_env() -> LegacyEnv:
    """A hub that never had a ``.env``: nothing is explicitly set."""
    return LegacyEnv(_env_file=None)


# ---------------------------------------------------------------------------
# the old files
# ---------------------------------------------------------------------------
def write_hub_json(data_dir: Path) -> tuple[Path, str, str]:
    config, token, device_key = claim_hub(
        existing=None, name="My Hub", base_url="http://dashboard-hub.lan:8080"
    )
    path = data_dir / "hub.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_json()), encoding="utf-8")
    return path, token, device_key


def write_legacy_telemetry(data_dir: Path, rows: int = 3) -> Path:
    """A ``telemetry.sqlite`` in the shape an older build wrote: no power
    columns, no origin columns."""
    path = data_dir / "telemetry.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path))
    raw.execute(
        """
        CREATE TABLE telemetry (
            received_at     TEXT NOT NULL,
            device          TEXT NOT NULL,
            battery_voltage REAL,
            battery_level   REAL,
            temperature     REAL,
            humidity        REAL,
            wifi_rssi       REAL,
            uptime_s        REAL,
            page            TEXT
        )
        """
    )
    for index in range(rows):
        raw.execute(
            "INSERT INTO telemetry (received_at, device, temperature) VALUES (?, ?, ?)",
            (f"2026-09-0{index + 1}T00:00:00.000+00:00", "reterminal-e1002", 30.0 + index),
        )
    raw.commit()
    raw.close()
    return path


def write_pushed_files(data_dir: Path) -> dict[str, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "brief").mkdir(parents=True, exist_ok=True)
    paths = {
        "tasks": data_dir / "tasks.json",
        "ai_usage": data_dir / "ai-usage.json",
        "brief": data_dir / "brief" / "current.json",
        "alert": data_dir / "alert.json",
    }
    paths["tasks"].write_text(
        json.dumps({"received_at": "2026-09-18T07:00:00+00:00", "items": [{"title": "Ship"}]}),
        encoding="utf-8",
    )
    paths["ai_usage"].write_text(json.dumps({"providers": []}), encoding="utf-8")
    paths["brief"].write_text(json.dumps({"summary": "morning"}), encoding="utf-8")
    paths["alert"].write_text(
        json.dumps(
            {
                "title": "Someone is at the door",
                "priority": "doorbell",
                "created_at": "2026-09-18T07:00:00+07:00",
                "duration_seconds": 90,
                "beep": True,
                "source": "legacy",
            }
        ),
        encoding="utf-8",
    )
    return paths


def rows(database: Database, sql: str) -> list[sqlite3.Row]:
    with database.reading() as connection:
        return connection.execute(sql).fetchall()


# ---------------------------------------------------------------------------
# environment to sections
# ---------------------------------------------------------------------------
def test_auto_and_file_both_become_push() -> None:
    assert pushed_source("auto") == "push"
    assert pushed_source("file") == "push"
    assert pushed_source("obsidian") == "obsidian"
    assert pushed_source("fixture") == "fixture"


def test_an_unconfigured_hub_writes_no_section_rows(clean_env: None) -> None:
    """No ``.env`` at all means no rows, so the settings models supply the
    current defaults rather than a frozen copy of the old ones."""
    assert section_documents(empty_env()) == {}


def test_only_the_sections_whose_variables_were_set_are_written(clean_env: None) -> None:
    env = LegacyEnv(_env_file=None, TIMEZONE="Europe/Berlin", HA_URL="http://ha.lan:8123")
    documents = section_documents(env)
    assert set(documents) == {"general", "home"}
    assert documents["general"] == {"timezone": "Europe/Berlin", "units": "metric"}
    assert documents["home"]["url"] == "http://ha.lan:8123"


def test_a_written_section_carries_its_defaults_too(clean_env: None) -> None:
    """One variable pulls in the whole section, so the settings page has a
    complete document to render rather than a single key."""
    env = LegacyEnv(_env_file=None, BRIEF_EVENING_HOUR="18")
    assert section_documents(env)["brief"] == {
        "source": "push",
        "evening_hour": 18,
        "ttl_seconds": 60.0,
        "stale_seconds": 36000.0,
    }


def test_the_pushed_sources_are_mapped_on_the_way_in(clean_env: None) -> None:
    env = LegacyEnv(
        _env_file=None, TASKS_SOURCE="auto", AI_USAGE_SOURCE="file", BRIEF_SOURCE="auto"
    )
    documents = section_documents(env)
    assert documents["tasks"]["source"] == "push"
    assert documents["ai_usage"]["source"] == "push"
    assert documents["brief"]["source"] == "push"


def test_feeds_are_built_from_the_three_comma_lists(clean_env: None) -> None:
    env = LegacyEnv(
        _env_file=None,
        CALENDAR_ICS_URLS="https://a.example/work.ics, https://b.example/home.ics",
        CALENDAR_NAMES="Work",
        CALENDAR_COLORS="red",
    )
    assert calendar_feeds(env) == [
        {"url": "https://a.example/work.ics", "name": "Work", "color": "red"},
        {"url": "https://b.example/home.ics", "name": "b.example", "color": "green"},
    ]


def test_home_entities_become_slot_rows(clean_env: None) -> None:
    env = LegacyEnv(_env_file=None, HA_ENTITIES='{"doorbell": "binary_sensor.bell"}')
    assert home_entities(env) == [{"slot": "doorbell", "entity_id": "binary_sensor.bell"}]


def test_home_entities_fall_back_to_the_defaults(clean_env: None) -> None:
    entities = home_entities(LegacyEnv(_env_file=None, HA_URL="http://ha.lan:8123"))
    assert len(entities) == len(DEFAULT_HA_ENTITIES)
    assert {"slot": "doorbell", "entity_id": "binary_sensor.doorbell"} in entities


# ---------------------------------------------------------------------------
# the import
# ---------------------------------------------------------------------------
def test_the_import_takes_every_piece_and_leaves_the_files_alone(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    data_dir = tmp_path / "data"
    hub_json, token, device_key = write_hub_json(data_dir)
    telemetry_file = write_legacy_telemetry(data_dir)
    pushed = write_pushed_files(data_dir)
    env = LegacyEnv(
        _env_file=None,
        TIMEZONE="Europe/Berlin",
        CALENDAR_ICS_URLS="https://a.example/work.ics",
        HA_URL="http://ha.lan:8123",
        HA_TOKEN="ha-secret",
    )

    assert import_legacy(database, env, data_dir) is True

    identity = HubIdentity(database)
    assert identity.configured is True
    assert identity.verify_token(token) is True
    assert identity.verify_device_key(device_key) is True
    assert identity.config is not None
    assert identity.config.name == "My Hub"

    assert len(rows(database, "SELECT * FROM telemetry")) == 3

    datasets = {str(row["name"]): row for row in rows(database, "SELECT * FROM datasets")}
    assert set(datasets) == {"tasks", "ai_usage", "brief", "alert"}
    assert json.loads(datasets["tasks"]["payload_json"])["items"][0]["title"] == "Ship"
    # received_at comes from the file's own key when it has one.
    assert str(datasets["tasks"]["received_at"]).startswith("2026-09-18T07:00:00")
    # ...and from the mtime when it does not.
    assert str(datasets["brief"]["received_at"]).endswith("+00:00")

    settings_rows = {str(row["section"]): row for row in rows(database, "SELECT * FROM settings")}
    assert set(settings_rows) == {"general", "calendar", "home"}
    assert json.loads(settings_rows["general"]["value_json"])["timezone"] == "Europe/Berlin"
    assert json.loads(settings_rows["home"]["value_json"])["token"] == "ha-secret"

    assert database.meta_get(LEGACY_IMPORTED_KEY) is not None

    # Nothing on disk is renamed or deleted: a rollback to the previous image
    # has to find hub.json where it left it.
    assert hub_json.is_file()
    assert telemetry_file.is_file()
    for path in pushed.values():
        assert path.is_file()


def test_a_legacy_imported_row_loads_through_the_push_adapters(
    tmp_path: Path, clean_env: None
) -> None:
    """The three pushed files, in the shape ``POST /api/tasks``,
    ``/api/ai-usage`` and ``/api/brief`` always wrote, survive the one-time
    import and load back through the matching ``Push*Adapter`` - not just
    through a raw ``SELECT`` (see
    ``test_the_import_takes_every_piece_and_leaves_the_files_alone`` above)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "brief").mkdir()
    (data_dir / "tasks.json").write_text(
        json.dumps(
            {
                "received_at": "2026-09-18T07:00:00+00:00",
                "tasks": [{"id": "agent-1", "title": "Ship it"}],
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "ai-usage.json").write_text(
        json.dumps({"providers": [{"provider": "claude"}]}), encoding="utf-8"
    )
    (data_dir / "brief" / "current.json").write_text(
        json.dumps({"headline": "From before the database"}), encoding="utf-8"
    )

    env = Env(_env_file=None, DATA_DIR=data_dir, LOG_LEVEL="WARNING")
    database = get_database(env.hub_db_file)
    database.migrate()
    assert import_legacy(database, empty_env(), data_dir) is True

    general = GeneralSettings()
    tasks = asyncio.run(PushTasksAdapter(TasksSettings(source="push"), general, env).fetch())
    assert [task.id for task in tasks] == ["agent-1"]

    providers = asyncio.run(
        PushAIUsageAdapter(AIUsageSettings(source="push"), general, env).fetch()
    )
    assert providers[0].provider == "claude"

    brief = asyncio.run(PushBriefAdapter(BriefSettings(source="push"), general, env).fetch())
    assert brief.headline == "From before the database"


def test_the_import_runs_only_once(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    data_dir = tmp_path / "data"
    write_hub_json(data_dir)
    write_legacy_telemetry(data_dir, rows=2)
    write_pushed_files(data_dir)
    env = LegacyEnv(_env_file=None, TIMEZONE="Europe/Berlin")

    assert import_legacy(database, env, data_dir) is True
    first_stamp = database.meta_get(LEGACY_IMPORTED_KEY)

    assert import_legacy(database, env, data_dir) is False
    assert database.meta_get(LEGACY_IMPORTED_KEY) == first_stamp
    assert len(rows(database, "SELECT * FROM telemetry")) == 2
    assert len(rows(database, "SELECT * FROM datasets")) == 4


def test_an_absent_data_dir_imports_nothing_but_still_marks_itself_done(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    assert import_legacy(database, empty_env(), tmp_path / "data") is True
    assert rows(database, "SELECT * FROM hub") == []
    assert rows(database, "SELECT * FROM settings") == []
    assert database.meta_get(LEGACY_IMPORTED_KEY) is not None


def test_a_claimed_hub_is_never_overwritten_by_the_old_files(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    """Somebody claimed the new hub before the old ``DATA_DIR`` was mounted.
    What they claimed wins; the import must not hand the hub back to a token
    they never saw."""
    data_dir = tmp_path / "data"
    _hub_json, legacy_token, _device_key = write_hub_json(data_dir)
    identity = HubIdentity(database)
    fresh = asyncio.run(identity.claim(name="claimed here", base_url="http://new.lan:8080"))

    import_legacy(database, empty_env(), data_dir)

    reloaded = HubIdentity(database)
    assert reloaded.verify_token(fresh.token) is True
    assert reloaded.verify_token(legacy_token) is False


def test_the_alert_file_comes_back_as_the_current_alert(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    data_dir = tmp_path / "data"
    write_pushed_files(data_dir)
    import_legacy(database, empty_env(), data_dir)

    store = AlertStore(database, "Asia/Bangkok")
    assert store.current is not None
    assert store.current.title == "Someone is at the door"


def test_the_old_telemetry_columns_survive_the_copy(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    """An old file has no ``battery_mode`` and no ``remote_addr``; its rows
    keep their NULLs there rather than being dropped for lack of a column."""
    data_dir = tmp_path / "data"
    write_legacy_telemetry(data_dir, rows=1)
    import_legacy(database, empty_env(), data_dir)
    row = rows(database, "SELECT * FROM telemetry")[0]
    assert row["temperature"] == pytest.approx(30.0)
    assert row["battery_mode"] is None
    assert row["remote_addr"] is None


def test_a_non_empty_telemetry_table_is_left_alone(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    data_dir = tmp_path / "data"
    write_legacy_telemetry(data_dir, rows=3)
    with database.writing() as connection:
        connection.execute(
            "INSERT INTO telemetry (received_at, device) VALUES (?, ?)",
            ("2026-09-19T00:00:00.000+00:00", "already-here"),
        )
    import_legacy(database, empty_env(), data_dir)
    assert len(rows(database, "SELECT * FROM telemetry")) == 1


def test_an_unreadable_tasks_json_leaves_the_gate_open_and_is_retried(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    """A broken pushed file must not be lost quietly for good: the gate has
    to stay open so a second start, with the file fixed, still imports it -
    and it must not cost the other pushed files their own import either."""
    data_dir = tmp_path / "data"
    pushed = write_pushed_files(data_dir)
    pushed["tasks"].write_text("{ not json", encoding="utf-8")

    assert import_legacy(database, empty_env(), data_dir) is True
    assert database.meta_get(LEGACY_IMPORTED_KEY) is None

    datasets = {str(row["name"]) for row in rows(database, "SELECT name FROM datasets")}
    assert "tasks" not in datasets
    assert {"ai_usage", "brief", "alert"} <= datasets

    # Fixed and retried on a second start: the gate closes.
    pushed["tasks"].write_text(
        json.dumps({"received_at": "2026-09-18T07:00:00+00:00", "items": [{"title": "Ship"}]}),
        encoding="utf-8",
    )
    assert import_legacy(database, empty_env(), data_dir) is True
    assert database.meta_get(LEGACY_IMPORTED_KEY) is not None
    datasets_after = {str(row["name"]) for row in rows(database, "SELECT name FROM datasets")}
    assert "tasks" in datasets_after


def test_an_unreadable_telemetry_file_leaves_the_gate_open_and_is_retried(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    """Same rule for the telemetry copy: an ATTACH or copy failure must not
    close the gate behind the history it lost."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = data_dir / "telemetry.sqlite"
    telemetry_path.write_text("this is not a database", encoding="utf-8")

    assert import_legacy(database, empty_env(), data_dir) is True
    assert database.meta_get(LEGACY_IMPORTED_KEY) is None
    assert rows(database, "SELECT * FROM telemetry") == []

    telemetry_path.unlink()
    write_legacy_telemetry(data_dir, rows=2)
    assert import_legacy(database, empty_env(), data_dir) is True
    assert database.meta_get(LEGACY_IMPORTED_KEY) is not None
    assert len(rows(database, "SELECT * FROM telemetry")) == 2


def test_a_fully_absent_legacy_set_closes_the_gate(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    """Nothing to import at all is success, not incompleteness: the gate
    closes on the first start, as it always has."""
    data_dir = tmp_path / "data"

    assert import_legacy(database, empty_env(), data_dir) is True
    assert database.meta_get(LEGACY_IMPORTED_KEY) is not None


def test_an_unreadable_hub_json_stops_the_import(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    """Skipping it would leave the hub unclaimed, and the next POST /setup
    would mint a second token beside an identity the operator still believes
    in. Stopping with one clear line is the only safe answer."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "hub.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hub config unreadable"):
        import_legacy(database, empty_env(), data_dir)
    assert database.meta_get(LEGACY_IMPORTED_KEY) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 2, "name": "deskmate"},
        {"schema": 3, "name": "deskmate"},
        {"schema": 1, "name": "deskmate"},
    ],
    ids=["missing-key", "wrong-schema", "schema-1"],
)
def test_every_bad_hub_json_shape_is_refused(
    tmp_path: Path, database: Database, clean_env: None, payload: dict[str, object]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "hub.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError):
        import_legacy(database, empty_env(), data_dir)


def test_schema_one_says_there_is_nothing_to_migrate(
    tmp_path: Path, database: Database, clean_env: None
) -> None:
    """Schema 1 predates the read key: no device key, no session secret, so
    the message has to send the operator to /setup rather than "fix it"."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "hub.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "name": "deskmate",
                "base_url": "http://dashboard-hub.lan:8080",
                "token_sha256": "x",
                "created_at": datetime.now(tz=dt_timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HubConfigUnreadable, match="/setup"):
        _read_hub_json(path)


# ---------------------------------------------------------------------------
# through a real start
# ---------------------------------------------------------------------------
def test_a_started_hub_comes_up_claimed_from_the_old_files(
    tmp_path: Path, clean_env: None
) -> None:
    """The deployed case: the upgrade finds hub.json and the device keeps
    fetching, without anyone opening /setup again."""
    data_dir = tmp_path / "data"
    _path, token, _device_key = write_hub_json(data_dir)
    write_legacy_telemetry(data_dir, rows=2)
    env = Env(
        _env_file=None,
        DATA_DIR=data_dir,
        LOG_LEVEL="WARNING",
    )
    try:
        app = create_app(env)
        with TestClient(app) as client:
            hub = client.app.state.hub
            assert hub.identity.configured is True
            assert hub.identity.verify_token(token) is True
            assert hub.telemetry.summary().sample_count == 2

            # A second start over the same DATA_DIR does not import again.
            stamp = hub.db.meta_get(LEGACY_IMPORTED_KEY)
            second = create_app(env)
            assert second.state.hub.db.meta_get(LEGACY_IMPORTED_KEY) == stamp
    finally:
        close_databases()
