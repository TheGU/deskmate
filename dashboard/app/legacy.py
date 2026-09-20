"""One-time import of the files and the environment a pre-database install had.

An install from before ``DATA_DIR/deskmate.sqlite`` kept its identity in
``hub.json``, its history in ``telemetry.sqlite``, its pushed datasets in four
JSON files, and everything else in ``.env``. This module moves all of it into
the database once, at the first start after the upgrade, so the deployed hub
comes up claimed, with its history, its sources and its location, and the
device keeps fetching without anyone touching it.

Two rules shape the whole file:

* **Nothing on disk is renamed or deleted.** A rollback to the previous image
  has to find ``hub.json`` where it left it, or it would re-claim the hub and
  mint a second token. docs/DEPLOY.md is where the operator is told to delete
  the old files by hand once the upgrade is confirmed.
* **The import never overwrites.** It is gated by ``meta.legacy_imported_at``,
  and each piece is additionally skipped when the database already has it (a
  ``hub`` row, a non-empty ``telemetry`` table, a ``datasets`` row). So an
  operator who claimed the new hub before the old files were mounted keeps
  what they claimed.
* **A failed piece keeps the gate open.** ``hub.json`` is the identity: an
  unreadable one still stops the start outright (see :func:`_import_hub`).
  But an unreadable ``telemetry.sqlite`` or pushed dataset file is not fatal
  and must not be either - the dashboard has to come up either way. Losing
  it *quietly*, forever, on the next restart is the failure mode this rule
  closes: :func:`_import_telemetry` and :func:`_import_datasets` each log at
  ERROR and report whether they fully succeeded, and
  ``meta.legacy_imported_at`` is only written when every piece succeeded or
  was genuinely absent (an absent file is success). An incomplete import
  therefore retries its failed piece - and only that piece, since a
  succeeded one already has its row - on every subsequent start until the
  file is fixed or removed.

:class:`LegacyEnv` is a copy of the ``config.py:Settings`` field list as it
stood before the settings moved into the database: same environment names,
same defaults, same ``.env`` handling. It lives here and nowhere else, and it
is the last thing in the codebase that reads those variables. Only a section
with at least one variable explicitly set (pydantic's ``model_fields_set``)
gets a ``settings`` row, so a hub that never had a ``.env`` gets no rows and
picks up the current defaults instead of a frozen copy of the old ones.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db import LEGACY_IMPORTED_KEY, Database, utc_now_iso
from app.hub_config import HUB_CONFIG_SCHEMA, HubConfig, HubConfigUnreadable
from app.logging_setup import log

logger = logging.getLogger("app.legacy")

#: Repository root, the default ``DATA_DIR`` parent, as ``config.py`` had it.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The default Home Assistant entity map ``HA_ENTITIES`` overrode. Kept here
#: verbatim so an install that never set the variable imports the same slots
#: it was actually rendering.
DEFAULT_HA_ENTITIES: Final[dict[str, str]] = {
    "front_door": "binary_sensor.front_door",
    "doorbell": "binary_sensor.doorbell",
    "motion": "binary_sensor.living_room_motion",
    "room_temperature": "sensor.office_temperature",
    "room_humidity": "sensor.office_humidity",
    "internet": "binary_sensor.internet_up",
    "home_assistant": "sensor.ha_status",
    "nas": "binary_sensor.nas_online",
    "proxmox": "binary_sensor.proxmox_online",
    "backup": "sensor.backup_last_result",
    "assistant": "sensor.local_assistant_status",
    "assistant_last_run": "sensor.local_assistant_last_run",
}

#: Colours handed out to calendars nobody named, in the order they appear.
#: The same cycle ``view.py:calendar_colors`` applies at render time, applied
#: once here so the feed rows carry a real colour instead of a blank.
DEFAULT_CALENDAR_COLORS: Final[tuple[str, ...]] = ("blue", "green", "yellow")

#: ``auto`` picked the pushed file when it existed and fixture otherwise;
#: ``file`` was the pushed file, strictly. With pushes stored in the database
#: there is one path, and demo data is the explicit ``fixture`` choice.
_PUSHED_SOURCES: Final[frozenset[str]] = frozenset({"auto", "file"})

#: The tasks section's own removed selector: the hub reading a mounted
#: Obsidian vault directly. Tasks now reach the hub only through the push
#: API (a local agent reads the owner's own vault and pushes instead, see
#: docs/LOCAL-AGENT.md), so an old ``TASKS_SOURCE=obsidian`` also imports as
#: ``push``, logged since silently dropping it would look like data loss.
_REMOVED_TASKS_SOURCE: Final[str] = "obsidian"


class LegacyEnv(BaseSettings):
    """The pre-database ``config.py:Settings`` field list, frozen in place.

    Every field, alias and default below matched the shipped ``.env.example``
    on the day the settings moved into the database. Nothing reads this at
    runtime: :func:`import_legacy` reads it once and then the ``settings``
    rows are the truth.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # -- global ---------------------------------------------------------
    timezone: str = Field(default="Asia/Bangkok", alias="TIMEZONE")
    units: Literal["metric", "imperial"] = Field(default="metric", alias="UNITS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    data_dir: Path = Field(default=REPO_ROOT / "data", alias="DATA_DIR")

    fixture_relative_dates: bool = Field(default=True, alias="FIXTURE_RELATIVE_DATES")

    # -- tasks ----------------------------------------------------------
    tasks_source: str = Field(default="file", alias="TASKS_SOURCE")
    obsidian_vault_path: Path | None = Field(default=None, alias="OBSIDIAN_VAULT_PATH")
    obsidian_task_glob: str = Field(default="**/*.md", alias="OBSIDIAN_TASK_GLOB")

    # -- calendar -------------------------------------------------------
    calendar_source: str = Field(default="ics", alias="CALENDAR_SOURCE")
    calendar_ics_urls: str = Field(default="", alias="CALENDAR_ICS_URLS")
    calendar_names: str = Field(default="", alias="CALENDAR_NAMES")
    calendar_colors: str = Field(default="", alias="CALENDAR_COLORS")

    # -- weather --------------------------------------------------------
    weather_source: str = Field(default="open_meteo", alias="WEATHER_SOURCE")
    weather_latitude: float | None = Field(default=None, alias="WEATHER_LATITUDE")
    weather_longitude: float | None = Field(default=None, alias="WEATHER_LONGITUDE")
    weather_location_name: str = Field(default="", alias="WEATHER_LOCATION_NAME")

    # -- ai usage -------------------------------------------------------
    ai_usage_source: str = Field(default="file", alias="AI_USAGE_SOURCE")
    ai_usage_path: Path | None = Field(default=None, alias="AI_USAGE_PATH")

    # -- ai brief -------------------------------------------------------
    brief_source: str = Field(default="file", alias="BRIEF_SOURCE")
    brief_dir: Path | None = Field(default=None, alias="BRIEF_DIR")
    brief_evening_hour: int = Field(default=14, ge=0, le=23, alias="BRIEF_EVENING_HOUR")

    # -- home assistant -------------------------------------------------
    ha_source: str = Field(default="rest", alias="HA_SOURCE")
    ha_url: str = Field(default="", alias="HA_URL")
    ha_token: str = Field(default="", alias="HA_TOKEN")
    ha_entities_raw: str = Field(default="", alias="HA_ENTITIES")

    # -- device telemetry -----------------------------------------------
    device_source: str = Field(default="store", alias="DEVICE_SOURCE")
    telemetry_db_path: Path | None = Field(default=None, alias="TELEMETRY_DB_PATH")
    telemetry_retention_days: int = Field(
        default=30, ge=1, le=3650, alias="TELEMETRY_RETENTION_DAYS"
    )

    # -- adapter cache TTLs (seconds) -----------------------------------
    tasks_ttl_seconds: float = Field(default=300.0, alias="TASKS_TTL_SECONDS")
    calendar_ttl_seconds: float = Field(default=300.0, alias="CALENDAR_TTL_SECONDS")
    weather_ttl_seconds: float = Field(default=900.0, alias="WEATHER_TTL_SECONDS")
    ai_usage_ttl_seconds: float = Field(default=300.0, alias="AI_USAGE_TTL_SECONDS")
    brief_ttl_seconds: float = Field(default=60.0, alias="BRIEF_TTL_SECONDS")
    home_ttl_seconds: float = Field(default=120.0, alias="HOME_TTL_SECONDS")
    device_ttl_seconds: float = Field(default=60.0, alias="DEVICE_TTL_SECONDS")

    # -- staleness thresholds (seconds), pushed datasets only -----------
    ai_usage_stale_seconds: float = Field(default=21600.0, alias="AI_USAGE_STALE_SECONDS")
    brief_stale_seconds: float = Field(default=36000.0, alias="BRIEF_STALE_SECONDS")
    tasks_stale_seconds: float = Field(default=36000.0, alias="TASKS_STALE_SECONDS")

    # -- rendering ------------------------------------------------------
    render_timeout_ms: int = Field(default=15000, alias="RENDER_TIMEOUT_MS")
    http_timeout_seconds: float = Field(default=10.0, alias="HTTP_TIMEOUT_SECONDS")
    max_priority_tasks: int = Field(default=3, alias="MAX_PRIORITY_TASKS")
    agenda_days: int = Field(default=7, alias="AGENDA_DAYS")
    alert_default_duration_seconds: int = Field(
        default=90, alias="ALERT_DEFAULT_DURATION_SECONDS"
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:  # pragma: no cover
            # See app/modules/general/settings.py:_validate_timezone: ZoneInfo()
            # touches the filesystem, so an illegal filename raises OSError.
            raise ValueError(f"unknown TIMEZONE {value!r}") from exc
        return value


#: Which ``LegacyEnv`` fields belong to which settings section. A section is
#: written only when the environment explicitly set at least one of its
#: variables; the row then carries the whole section, defaults included, so
#: the settings page has something complete to render.
_SECTION_TRIGGERS: Final[dict[str, tuple[str, ...]]] = {
    "general": ("timezone", "units"),
    "tasks": (
        "tasks_source",
        "obsidian_vault_path",
        "obsidian_task_glob",
        "max_priority_tasks",
        "tasks_ttl_seconds",
        "tasks_stale_seconds",
    ),
    "calendar": (
        "calendar_source",
        "calendar_ics_urls",
        "calendar_names",
        "calendar_colors",
        "agenda_days",
        "calendar_ttl_seconds",
    ),
    "weather": (
        "weather_source",
        "weather_latitude",
        "weather_longitude",
        "weather_location_name",
        "weather_ttl_seconds",
    ),
    "ai_usage": ("ai_usage_source", "ai_usage_ttl_seconds", "ai_usage_stale_seconds"),
    "brief": (
        "brief_source",
        "brief_evening_hour",
        "brief_ttl_seconds",
        "brief_stale_seconds",
    ),
    "home": ("ha_source", "ha_url", "ha_token", "ha_entities_raw", "home_ttl_seconds"),
    "device": ("device_source", "telemetry_retention_days", "device_ttl_seconds"),
    "alert": ("alert_default_duration_seconds",),
}


# ---------------------------------------------------------------------------
# Pure translation: environment to section documents
# ---------------------------------------------------------------------------
def pushed_source(value: str) -> str:
    """``auto`` and ``file`` both become ``push``; anything else is itself."""
    return "push" if value in _PUSHED_SOURCES else value


def _tasks_source(value: str) -> str:
    """Like :func:`pushed_source`, plus the removed ``obsidian`` source: an
    old ``.env`` with ``TASKS_SOURCE=obsidian`` imports as ``push``, logged
    at WARNING since the hub no longer reads a vault directly and the local
    agent pushes tasks instead (docs/LOCAL-AGENT.md)."""
    if value == _REMOVED_TASKS_SOURCE:
        log(
            logger,
            logging.WARNING,
            "TASKS_SOURCE=obsidian was removed; imported as push - the local "
            "agent pushes tasks instead of the hub reading a vault directly",
        )
        return "push"
    return pushed_source(value)


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def feed_rows(urls: str, names: str, colors: str) -> list[dict[str, str]]:
    """The three comma lists folded into one feed row per URL.

    The pure core of :func:`calendar_feeds`, taking the three raw strings
    directly rather than a :class:`LegacyEnv`, so :func:`section_documents`
    stays the one place that maps the old environment onto a settings row.

    A feed nobody named falls back to the URL host, then to its number, the
    way the old ``config.py:Settings.ics_calendar_name`` did; a feed nobody
    coloured takes the next colour of the default cycle, the way ``view.py``
    did at render time.
    """
    url_list = _split(urls)
    name_list = _split(names)
    color_list = [item.lower() for item in _split(colors)]
    feeds: list[dict[str, str]] = []
    for index, url in enumerate(url_list):
        if index < len(name_list):
            name = name_list[index]
        else:
            name = urlparse(url).hostname or f"calendar {index + 1}"
        color = (
            color_list[index]
            if index < len(color_list)
            else DEFAULT_CALENDAR_COLORS[index % len(DEFAULT_CALENDAR_COLORS)]
        )
        feeds.append({"url": url, "name": name, "color": color})
    return feeds


def calendar_feeds(env: LegacyEnv) -> list[dict[str, str]]:
    """:func:`feed_rows` applied to one ``LegacyEnv``'s three comma lists."""
    return feed_rows(env.calendar_ics_urls, env.calendar_names, env.calendar_colors)


def entity_rows(raw: str) -> list[dict[str, str]]:
    """``HA_ENTITIES`` (a JSON object, as a raw string) as the ordered slot
    list the settings form edits. An unset or unparseable value falls back to
    the defaults the hub was actually rendering.

    The pure core of :func:`home_entities`, taking the raw string directly so
    :func:`section_documents` can call it with a :class:`LegacyEnv` field the
    same way :func:`calendar_feeds` does.
    """
    text = raw.strip()
    mapping: dict[str, str] = dict(DEFAULT_HA_ENTITIES)
    if text:
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            mapping = {str(key): str(value) for key, value in parsed.items()}
        else:
            log(logger, logging.WARNING, "HA_ENTITIES is not a JSON object, using defaults")
    return [{"slot": slot, "entity_id": entity_id} for slot, entity_id in mapping.items()]


def home_entities(env: LegacyEnv) -> list[dict[str, str]]:
    """:func:`entity_rows` applied to one ``LegacyEnv``'s ``HA_ENTITIES``."""
    return entity_rows(env.ha_entities_raw)


def section_documents(env: LegacyEnv) -> dict[str, dict[str, Any]]:
    """Every settings section this environment describes, keyed by section.

    Only sections with at least one explicitly set variable are present.
    """
    documents: dict[str, dict[str, Any]] = {
        "general": {"timezone": env.timezone, "units": env.units},
        "tasks": {
            "source": _tasks_source(env.tasks_source),
            "max_priority_tasks": env.max_priority_tasks,
            "ttl_seconds": env.tasks_ttl_seconds,
            "stale_seconds": env.tasks_stale_seconds,
        },
        "calendar": {
            "source": env.calendar_source,
            "feeds": calendar_feeds(env),
            "agenda_days": env.agenda_days,
            "ttl_seconds": env.calendar_ttl_seconds,
        },
        "weather": {
            "source": env.weather_source,
            "latitude": env.weather_latitude,
            "longitude": env.weather_longitude,
            "location_name": env.weather_location_name,
            "ttl_seconds": env.weather_ttl_seconds,
        },
        "ai_usage": {
            "source": pushed_source(env.ai_usage_source),
            "ttl_seconds": env.ai_usage_ttl_seconds,
            "stale_seconds": env.ai_usage_stale_seconds,
        },
        "brief": {
            "source": pushed_source(env.brief_source),
            "evening_hour": env.brief_evening_hour,
            "ttl_seconds": env.brief_ttl_seconds,
            "stale_seconds": env.brief_stale_seconds,
        },
        "home": {
            "source": env.ha_source,
            "url": env.ha_url,
            "token": env.ha_token,
            "entities": home_entities(env),
            "ttl_seconds": env.home_ttl_seconds,
        },
        "device": {
            "source": env.device_source,
            "retention_days": env.telemetry_retention_days,
            "ttl_seconds": env.device_ttl_seconds,
        },
        "alert": {"default_duration_seconds": env.alert_default_duration_seconds},
    }
    configured = env.model_fields_set
    return {
        section: document
        for section, document in documents.items()
        if any(name in configured for name in _SECTION_TRIGGERS[section])
    }


# ---------------------------------------------------------------------------
# The import itself
# ---------------------------------------------------------------------------
def import_legacy(db: Database, env: LegacyEnv, data_dir: Path) -> bool:
    """Run the one-time import. Returns True when it ran, False when it was
    already done.

    ``data_dir`` is the hub's real ``DATA_DIR``, not ``env.data_dir``: the
    environment supplies the values, the caller supplies the location, so a
    hub whose ``DATA_DIR`` comes from somewhere other than the environment
    still reads its own files.

    ``meta.legacy_imported_at`` - the gate - is only written when
    :func:`_import_telemetry` and :func:`_import_datasets` both report they
    fully succeeded (an absent file counts as success). A failed piece is
    logged at ERROR and leaves the gate open, so the next start retries
    whatever failed instead of leaving it lost. ``_import_hub`` is not part
    of that: an unreadable ``hub.json`` still raises and stops the start
    outright, as it always has.
    """
    if db.meta_get(LEGACY_IMPORTED_KEY) is not None:
        return False

    _import_hub(db, data_dir / "hub.json")
    telemetry_ok = _import_telemetry(db, env.telemetry_db_path or (data_dir / "telemetry.sqlite"))
    datasets_ok = _import_datasets(db, env, data_dir)
    _import_settings(db, env)

    if telemetry_ok and datasets_ok:
        db.meta_set(LEGACY_IMPORTED_KEY, utc_now_iso())
    else:
        log(
            logger,
            logging.ERROR,
            "legacy import incomplete; the failed piece will be retried on the next start",
            telemetry_imported=telemetry_ok,
            datasets_imported=datasets_ok,
        )
    return True


def _import_hub(db: Database, path: Path) -> None:
    """``hub.json`` into the ``hub`` row, only when that row is absent.

    A file that exists but cannot be read stops the hub with a clear line
    instead of being skipped: skipping it would leave the hub unclaimed, and
    the next ``POST /setup`` would mint a second token and device key beside
    an identity the operator still believes in.
    """
    with db.reading() as connection:
        existing = connection.execute("SELECT 1 FROM hub WHERE id = 1").fetchone()
    if existing is not None:
        return
    if not path.is_file():
        return
    try:
        config = _read_hub_json(path)
    except HubConfigUnreadable as exc:
        raise RuntimeError(
            f"cannot import {path}: {exc}. Fix or delete it, then start again"
        ) from exc
    with db.writing() as connection:
        connection.execute(
            "INSERT INTO hub (id, name, base_url, token_sha256, device_key_sha256,"
            " session_secret, created_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                config.name,
                config.base_url,
                config.token_sha256,
                config.device_key_sha256,
                config.session_secret,
                config.created_at.isoformat(),
            ),
        )
    log(logger, logging.INFO, "legacy hub identity imported", path=str(path), name=config.name)


def _read_hub_json(path: Path) -> HubConfig:
    """The old ``load_hub_config`` file reader, kept only for this import."""
    message = f"hub config unreadable: {path}, fix or delete it"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HubConfigUnreadable(message) from exc
    if not isinstance(payload, dict):
        raise HubConfigUnreadable(message)
    schema = payload.get("schema")
    if schema == 1:
        raise HubConfigUnreadable(
            "hub.json is schema 1, from before the read key; there is nothing to "
            "migrate, delete data/hub.json and run /setup again"
        )
    if schema != HUB_CONFIG_SCHEMA:
        raise HubConfigUnreadable(message)
    try:
        return HubConfig.from_json(payload)
    except (KeyError, ValueError) as exc:
        raise HubConfigUnreadable(message) from exc


def _import_telemetry(db: Database, path: Path) -> bool:
    """The old ``telemetry.sqlite`` rows, only when ``telemetry`` is empty.

    ``ATTACH`` and one ``INSERT ... SELECT`` over the columns both files have
    in common: an old file predates ``battery_mode`` and the rest, and those
    rows keep their NULLs rather than being dropped.

    Returns True when there was nothing to do (rows already present, or the
    file is absent) or the copy succeeded; False when a file that exists
    could not be attached or read, which is now an ERROR rather than a
    WARNING: :func:`import_legacy` reads this to decide whether the legacy
    gate may close, so a broken file is retried on the next start instead of
    being lost quietly for good the moment the gate closes behind it.
    """
    with db.reading() as connection:
        count = int(connection.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()["n"])
    if count:
        return True
    if not path.is_file():
        return True
    copied = 0
    with db.writing() as connection:
        target = {str(row["name"]) for row in connection.execute("PRAGMA table_info(telemetry)")}
        try:
            connection.execute("ATTACH DATABASE ? AS legacy_import", (str(path),))
        except sqlite3.Error as exc:
            log(
                logger,
                logging.ERROR,
                "cannot attach legacy telemetry",
                path=str(path),
                error=str(exc),
            )
            return False
        try:
            source = [
                str(row["name"])
                for row in connection.execute("PRAGMA legacy_import.table_info(telemetry)")
            ]
            shared = [name for name in source if name in target]
            if not shared:
                log(
                    logger,
                    logging.ERROR,
                    "legacy telemetry has no usable columns",
                    path=str(path),
                )
                return False
            columns = ", ".join(shared)
            copied = max(
                0,
                connection.execute(
                    f"INSERT INTO telemetry ({columns})"
                    f" SELECT {columns} FROM legacy_import.telemetry"
                ).rowcount,
            )
        except sqlite3.Error as exc:
            log(
                logger,
                logging.ERROR,
                "cannot import legacy telemetry",
                path=str(path),
                error=str(exc),
            )
            return False
        finally:
            # DETACH refuses to run inside a transaction, and the INSERT above
            # opened one, so the commit has to happen here rather than on the
            # way out of ``writing()``.
            connection.commit()
            connection.execute("DETACH DATABASE legacy_import")
    log(logger, logging.INFO, "legacy telemetry imported", path=str(path), rows=copied)
    return True


def _dataset_files(env: LegacyEnv, data_dir: Path) -> dict[str, Path]:
    """Dataset name to the file it used to live in, resolved against the
    hub's own ``DATA_DIR`` and the two environment overrides that could move
    one (``AI_USAGE_PATH``, ``BRIEF_DIR``)."""
    return {
        "tasks": data_dir / "tasks.json",
        "ai_usage": env.ai_usage_path or (data_dir / "ai-usage.json"),
        "brief": (env.brief_dir or (data_dir / "brief")) / "current.json",
        "alert": data_dir / "alert.json",
    }


def _import_datasets(db: Database, env: LegacyEnv, data_dir: Path) -> bool:
    """The four pushed files into ``datasets``, each only when its row is
    absent.

    Returns True when every existing file imported cleanly (an absent file
    or one already imported both count as success); False when at least one
    existing file could not be read. That failure is now an ERROR rather
    than a WARNING, and the loop still visits every other file - a broken
    ``tasks.json`` must not cost the hub its ``ai_usage`` or ``brief`` import
    too - but :func:`import_legacy` reads the returned False to keep the
    legacy gate open, so the broken file is retried (and only that one,
    since the rest already have their rows) on the next start.
    """
    ok = True
    for name, path in _dataset_files(env, data_dir).items():
        if not path.is_file():
            continue
        with db.reading() as connection:
            existing = connection.execute(
                "SELECT 1 FROM datasets WHERE name = ?", (name,)
            ).fetchone()
        if existing is not None:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(logger, logging.ERROR, "cannot import pushed file", path=str(path), error=str(exc))
            ok = False
            continue
        received_at = _received_at(payload, path)
        with db.writing() as connection:
            connection.execute(
                "INSERT INTO datasets (name, payload_json, received_at) VALUES (?, ?, ?)",
                (name, json.dumps(payload), received_at),
            )
        log(
            logger,
            logging.INFO,
            "legacy dataset imported",
            dataset=name,
            path=str(path),
            received_at=received_at,
        )
    return ok


def _received_at(payload: Any, path: Path) -> str:
    """The file's own ``received_at`` when it carries one, else its mtime."""
    if isinstance(payload, dict):
        raw = payload.get("received_at")
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt_timezone.utc)
                return parsed.astimezone(dt_timezone.utc).isoformat(timespec="milliseconds")
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=dt_timezone.utc)
    return mtime.isoformat(timespec="milliseconds")


def _import_settings(db: Database, env: LegacyEnv) -> None:
    """The explicitly set environment variables into ``settings`` rows."""
    stamp = utc_now_iso()
    for section, document in section_documents(env).items():
        with db.reading() as connection:
            existing = connection.execute(
                "SELECT 1 FROM settings WHERE section = ?", (section,)
            ).fetchone()
        if existing is not None:
            continue
        with db.writing() as connection:
            connection.execute(
                "INSERT INTO settings (section, value_json, updated_at) VALUES (?, ?, ?)",
                (section, json.dumps(document), stamp),
            )
        log(
            logger,
            logging.INFO,
            "legacy settings imported",
            section=section,
            fields=len(document),
        )


__all__ = [
    "LegacyEnv",
    "calendar_feeds",
    "entity_rows",
    "feed_rows",
    "home_entities",
    "import_legacy",
    "pushed_source",
    "section_documents",
]
