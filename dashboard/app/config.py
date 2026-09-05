"""Configuration for dashboard-hub.

All settings come from environment variables (or a local ``.env``). Names match
docs/ARCHITECTURE.md. Every adapter has a ``*_SOURCE`` selector and ``fixture``
is always available, so the server runs with no configuration at all.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TasksSource = Literal["fixture", "obsidian"]
CalendarSource = Literal["fixture", "ics"]
WeatherSource = Literal["fixture", "open_meteo"]
AIUsageSource = Literal["fixture", "file"]
BriefSource = Literal["fixture", "file"]
HomeAssistantSource = Literal["fixture", "rest"]
#: ``store`` reads only what the device posted; ``fixture`` falls back to
#: ``fixtures/device.json`` while the store is still empty (dev and preview).
DeviceSource = Literal["fixture", "store"]

#: Repository root, i.e. the directory that holds ``fixtures/`` and ``data/``.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
APP_DIR: Path = Path(__file__).resolve().parent

#: Default Home Assistant entity map. Keys are dashboard slots, values are
#: entity ids. Override with ``HA_ENTITIES`` (JSON object).
DEFAULT_HA_ENTITIES: dict[str, str] = {
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


class Settings(BaseSettings):
    """Environment-driven settings. See ``.env.example`` for every variable."""

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

    fixtures_dir: Path = Field(default=REPO_ROOT / "fixtures", alias="FIXTURES_DIR")
    data_dir: Path = Field(default=REPO_ROOT / "data", alias="DATA_DIR")

    #: Shift fixture dates so the demo always looks like "today".
    fixture_relative_dates: bool = Field(default=True, alias="FIXTURE_RELATIVE_DATES")

    # -- tasks ----------------------------------------------------------
    tasks_source: TasksSource = Field(default="fixture", alias="TASKS_SOURCE")
    obsidian_vault_path: Path | None = Field(default=None, alias="OBSIDIAN_VAULT_PATH")
    obsidian_task_glob: str = Field(default="**/*.md", alias="OBSIDIAN_TASK_GLOB")

    # -- calendar -------------------------------------------------------
    calendar_source: CalendarSource = Field(default="fixture", alias="CALENDAR_SOURCE")
    #: Comma separated list of ICS URLs or local file paths.
    calendar_ics_urls: str = Field(default="", alias="CALENDAR_ICS_URLS")

    # -- weather --------------------------------------------------------
    weather_source: WeatherSource = Field(default="fixture", alias="WEATHER_SOURCE")
    weather_latitude: float | None = Field(default=None, alias="WEATHER_LATITUDE")
    weather_longitude: float | None = Field(default=None, alias="WEATHER_LONGITUDE")
    weather_location_name: str = Field(default="", alias="WEATHER_LOCATION_NAME")

    # -- ai usage -------------------------------------------------------
    ai_usage_source: AIUsageSource = Field(default="fixture", alias="AI_USAGE_SOURCE")
    ai_usage_path: Path | None = Field(default=None, alias="AI_USAGE_PATH")

    # -- ai brief -------------------------------------------------------
    brief_source: BriefSource = Field(default="fixture", alias="BRIEF_SOURCE")
    brief_dir: Path | None = Field(default=None, alias="BRIEF_DIR")
    #: Local hour at which the brief switches from morning to evening mode.
    brief_evening_hour: int = Field(default=14, ge=0, le=23, alias="BRIEF_EVENING_HOUR")

    # -- home assistant -------------------------------------------------
    ha_source: HomeAssistantSource = Field(default="fixture", alias="HA_SOURCE")
    ha_url: str = Field(default="", alias="HA_URL")
    ha_token: str = Field(default="", alias="HA_TOKEN")
    ha_entities_raw: str = Field(default="", alias="HA_ENTITIES")

    # -- device telemetry -----------------------------------------------
    device_source: DeviceSource = Field(default="fixture", alias="DEVICE_SOURCE")
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
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover
            raise ValueError(f"unknown TIMEZONE {value!r}") from exc
        return value

    # -- derived helpers -------------------------------------------------
    @property
    def ai_usage_file(self) -> Path:
        return self.ai_usage_path or (self.data_dir / "ai-usage.json")

    @property
    def brief_directory(self) -> Path:
        return self.brief_dir or (self.data_dir / "brief")

    @property
    def alert_file(self) -> Path:
        return self.data_dir / "alert.json"

    @property
    def telemetry_db_file(self) -> Path:
        return self.telemetry_db_path or (self.data_dir / "telemetry.sqlite")

    @property
    def ics_sources(self) -> list[str]:
        return [item.strip() for item in self.calendar_ics_urls.split(",") if item.strip()]

    @property
    def ha_entities(self) -> dict[str, str]:
        if not self.ha_entities_raw.strip():
            return dict(DEFAULT_HA_ENTITIES)
        parsed: Any = json.loads(self.ha_entities_raw)
        if not isinstance(parsed, dict):
            raise ValueError("HA_ENTITIES must be a JSON object of slot -> entity_id")
        return {str(key): str(value) for key, value in parsed.items()}

    @property
    def templates_dir(self) -> Path:
        return APP_DIR / "templates"

    @property
    def static_dir(self) -> Path:
        return APP_DIR / "static"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (used by tests that patch the environment)."""
    get_settings.cache_clear()
