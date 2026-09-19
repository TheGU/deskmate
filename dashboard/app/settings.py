"""The settings composite, the section registry, and the store that reads
and writes sections through the hub's database.

``app/modules/<id>/settings.py`` defines one pydantic model per section;
:data:`SECTIONS` is the registry of them, in the wizard's order (the plan's
``/setup/general``, ``/setup/weather``, ... sequence extended to every
section). :class:`HubSettings` is the composite every adapter, context
builder and route will read once 1.2b switches call sites to it. Until then,
this module has no effect on the running hub: ``app/config.py:Settings``
stays what everything actually reads.

:class:`SettingsStore` is the other half: it loads a section's row from
``settings`` (``app/db.py``), validates it through the section's model, and
saves one back. A missing row means defaults; a row that fails to parse or
fails validation is a WARNING and defaults, never a crash, so a hub with one
bad section keeps serving the others (and its own honest empty state) while
an admin fixes it on the settings page (1.4).

:meth:`HubSettings.from_env` builds a snapshot directly from
``config.Settings``, using the same mapping ``app/legacy.py:import_legacy``
uses to write ``settings`` rows from the old environment
(:func:`app.legacy.pushed_source`, :func:`app.legacy.feed_rows`,
:func:`app.legacy.entity_rows`) so the two never drift apart. 1.2b builds a
``HubSettings`` this way while ``SettingsStore`` is still unused at runtime;
1.2c makes the store the real source and this method goes away with
``Settings`` itself.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError

from app.db import Database, utc_now_iso
from app.logging_setup import log
from app.modules.ai_usage.settings import SECTION as _AI_USAGE_SECTION, AIUsageSettings
from app.modules.alert.settings import SECTION as _ALERT_SECTION, AlertSettings
from app.modules.brief.settings import SECTION as _BRIEF_SECTION, BriefSettings
from app.modules.calendar.settings import SECTION as _CALENDAR_SECTION, CalendarSettings, Feed
from app.modules.device.settings import SECTION as _DEVICE_SECTION, DeviceSettings
from app.modules.general.settings import SECTION as _GENERAL_SECTION, GeneralSettings
from app.modules.home.settings import SECTION as _HOME_SECTION, EntitySlot, HomeSettings
from app.modules.tasks.settings import SECTION as _TASKS_SECTION, TasksSettings
from app.modules.weather.settings import SECTION as _WEATHER_SECTION, WeatherSettings

if TYPE_CHECKING:
    from app import config

logger = logging.getLogger("app.settings")

#: Every settings section, in wizard order. Insertion order is load-bearing:
#: ``HubSettings`` fields, the settings page and the setup wizard all walk
#: sections in this order.
SECTIONS: dict[str, type[BaseModel]] = {
    _GENERAL_SECTION: GeneralSettings,
    _TASKS_SECTION: TasksSettings,
    _CALENDAR_SECTION: CalendarSettings,
    _WEATHER_SECTION: WeatherSettings,
    _AI_USAGE_SECTION: AIUsageSettings,
    _BRIEF_SECTION: BriefSettings,
    _HOME_SECTION: HomeSettings,
    _DEVICE_SECTION: DeviceSettings,
    _ALERT_SECTION: AlertSettings,
}


class HubSettings(BaseModel):
    """One attribute per settings section, every one defaulted.

    Building one with no arguments is a hub that has never saved anything:
    every section is that model's own defaults, the same honest live
    selectors ``config.py:Settings`` ships with (never ``fixture``).
    """

    general: GeneralSettings = Field(default_factory=GeneralSettings)
    tasks: TasksSettings = Field(default_factory=TasksSettings)
    calendar: CalendarSettings = Field(default_factory=CalendarSettings)
    weather: WeatherSettings = Field(default_factory=WeatherSettings)
    ai_usage: AIUsageSettings = Field(default_factory=AIUsageSettings)
    brief: BriefSettings = Field(default_factory=BriefSettings)
    home: HomeSettings = Field(default_factory=HomeSettings)
    device: DeviceSettings = Field(default_factory=DeviceSettings)
    alert: AlertSettings = Field(default_factory=AlertSettings)

    @classmethod
    def from_env(cls, settings: "config.Settings") -> "HubSettings":
        """Build a snapshot from ``config.Settings``, mapping the old
        environment fields onto sections the same way
        ``app/legacy.py:import_legacy`` maps them onto ``settings`` rows."""
        from app.legacy import entity_rows, feed_rows, pushed_source

        return cls(
            general=GeneralSettings(
                timezone=settings.timezone,
                units=settings.units,
            ),
            tasks=TasksSettings(
                source=pushed_source(settings.tasks_source),
                obsidian_vault_path=settings.obsidian_vault_path,
                obsidian_task_glob=settings.obsidian_task_glob,
                max_priority_tasks=settings.max_priority_tasks,
                ttl_seconds=settings.tasks_ttl_seconds,
                stale_seconds=settings.tasks_stale_seconds,
            ),
            calendar=CalendarSettings(
                source=settings.calendar_source,
                feeds=[
                    Feed(**row)
                    for row in feed_rows(
                        settings.calendar_ics_urls,
                        settings.calendar_names,
                        settings.calendar_colors,
                    )
                ],
                agenda_days=settings.agenda_days,
                ttl_seconds=settings.calendar_ttl_seconds,
            ),
            weather=WeatherSettings(
                source=settings.weather_source,
                latitude=settings.weather_latitude,
                longitude=settings.weather_longitude,
                location_name=settings.weather_location_name,
                ttl_seconds=settings.weather_ttl_seconds,
            ),
            ai_usage=AIUsageSettings(
                source=pushed_source(settings.ai_usage_source),
                ttl_seconds=settings.ai_usage_ttl_seconds,
                stale_seconds=settings.ai_usage_stale_seconds,
            ),
            brief=BriefSettings(
                source=pushed_source(settings.brief_source),
                evening_hour=settings.brief_evening_hour,
                ttl_seconds=settings.brief_ttl_seconds,
                stale_seconds=settings.brief_stale_seconds,
            ),
            home=HomeSettings(
                source=settings.ha_source,
                url=settings.ha_url,
                token=settings.ha_token,
                entities=[EntitySlot(**row) for row in entity_rows(settings.ha_entities_raw)],
                ttl_seconds=settings.home_ttl_seconds,
            ),
            device=DeviceSettings(
                source=settings.device_source,
                retention_days=settings.telemetry_retention_days,
                ttl_seconds=settings.device_ttl_seconds,
            ),
            alert=AlertSettings(
                default_duration_seconds=settings.alert_default_duration_seconds,
            ),
        )


class SettingsStore:
    """Loads and saves settings sections through the hub's database.

    Call it with a migrated :class:`app.db.Database`. Every method is plain
    synchronous I/O, like ``Database.reading``/``writing`` themselves; an
    async route calls in through ``run_in_threadpool``.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def load(self, section: str) -> BaseModel:
        """The section's model, built from its row, or defaults.

        A missing row is silent (a hub that never saved this section). A row
        that is not valid JSON, or that fails the model's validation, is
        logged at WARNING and defaults are returned instead of raising: one
        damaged section must not stop the hub from serving every other page.
        """
        model = SECTIONS[section]
        with self._db.reading() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE section = ?", (section,)
            ).fetchone()
        if row is None:
            return model()
        try:
            document = json.loads(row["value_json"])
        except json.JSONDecodeError as exc:
            log(
                logger,
                logging.WARNING,
                "settings row is not valid JSON, using defaults",
                section=section,
                error=str(exc),
            )
            return model()
        try:
            return model.model_validate(document)
        except ValidationError as exc:
            log(
                logger,
                logging.WARNING,
                "settings row failed validation, using defaults",
                section=section,
                error=str(exc),
            )
            return model()

    def save(self, section: str, value: BaseModel) -> None:
        """Upsert ``value`` as ``section``'s row, stamping ``updated_at``.

        ``model_dump(mode="json")`` is what unwraps a ``SecretStr`` field
        into its plain value (through that field's own ``field_serializer``,
        see ``app/modules/home/settings.py``): this database is the hub's
        own secret store, so the stored JSON carries the real value, never
        the masked display string.
        """
        model = SECTIONS[section]
        if not isinstance(value, model):
            raise TypeError(f"settings section {section!r} expects a {model.__name__}")
        payload = json.dumps(value.model_dump(mode="json"))
        stamp = utc_now_iso()
        with self._db.writing() as connection:
            connection.execute(
                "INSERT INTO settings (section, value_json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(section) DO UPDATE SET"
                " value_json = excluded.value_json, updated_at = excluded.updated_at",
                (section, payload, stamp),
            )

    def snapshot(self) -> HubSettings:
        """Every section, loaded, as one :class:`HubSettings`."""
        return HubSettings(**{section: self.load(section) for section in SECTIONS})

    def updated_at(self, section: str) -> datetime | None:
        """When ``section`` was last saved, or ``None`` before it ever was."""
        with self._db.reading() as connection:
            row = connection.execute(
                "SELECT updated_at FROM settings WHERE section = ?", (section,)
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(str(row["updated_at"]))


__all__ = ["SECTIONS", "HubSettings", "SettingsStore"]
