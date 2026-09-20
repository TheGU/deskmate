"""What stays in the environment once settings live in the database.

Everything a settings-page section can hold (timezone, sources, TTLs, feeds,
tokens, ...) is a pydantic model under ``app/modules/<id>/settings.py``,
composed as ``app/settings.py:HubSettings`` and read from and written to the
hub's own database through ``app/settings.py:SettingsStore``. What is left
here is process and container configuration that makes no sense as a
settings-page field, plus the demo knobs used only in development
(plan section "What stays in the environment"). Compose keeps ``HUB_PORT``,
``PUID``, ``PGID`` and the Obsidian bind mount path, which are never read
here. ``app/legacy.py:LegacyEnv`` is the only place the old, now-removed
environment variable names survive, for the one-time import of a
pre-database install.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root, i.e. the directory that holds ``data/``.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
APP_DIR: Path = Path(__file__).resolve().parent

#: Default Home Assistant entity map. Keys are dashboard slots, values are
#: entity ids. This is the ``home`` settings section's own default
#: (``app/modules/home/settings.py``); a saved section overrides it entirely.
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


class Env(BaseSettings):
    """What stays in the environment once settings move into the database
    (plan section "What stays in the environment"): process and container
    knobs that make no sense as a settings-page field, plus the demo knobs
    used only in development. Everything else that used to live in
    ``.env.example`` is now a ``app/settings.py`` section.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    data_dir: Path = Field(default=REPO_ROOT / "data", alias="DATA_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    render_timeout_ms: int = Field(default=15000, alias="RENDER_TIMEOUT_MS")
    http_timeout_seconds: float = Field(default=10.0, alias="HTTP_TIMEOUT_SECONDS")
    #: Shift fixture dates so the demo always looks like "today".
    fixture_relative_dates: bool = Field(default=True, alias="FIXTURE_RELATIVE_DATES")

    @property
    def templates_dir(self) -> Path:
        return APP_DIR / "templates"

    @property
    def static_dir(self) -> Path:
        return APP_DIR / "static"

    @property
    def hub_db_file(self) -> Path:
        """The one SQLite file the hub owns: identity, settings, pushed
        datasets and telemetry (``app/db.py``). It replaces ``hub.json``,
        ``alert.json`` and ``telemetry.sqlite``; those names now live only in
        ``app/legacy.py``, which imports them once and leaves them on disk.
        """
        return self.data_dir / "deskmate.sqlite"


__all__ = ["APP_DIR", "DEFAULT_HA_ENTITIES", "Env", "REPO_ROOT"]
