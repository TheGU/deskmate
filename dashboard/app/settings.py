"""The settings composite, the section registry, and the store that reads
and writes sections through the hub's database.

``app/modules/<id>/settings.py`` defines one pydantic model per section;
:data:`SECTIONS` is the registry of them, keyed by section name and in the
wizard's order (the plan's ``/setup/general``, ``/setup/weather``, ...
sequence extended to every section) - the settings page, the setup wizard
and :class:`HubSettings` itself all walk sections in this order, so there is
no separate ordering list to keep in sync. :class:`HubSettings` is the
composite every adapter, context builder and route reads: one attribute per
section, each defaulted, built by :meth:`SettingsStore.snapshot`.

:class:`SettingsStore` is the other half: it loads a section's row from
``settings`` (``app/db.py``), validates it through the section's model, and
saves one back. A missing row means defaults; a row that fails to parse or
fails validation is a WARNING and defaults, never a crash, so a hub with one
bad section keeps serving the others (and its own honest empty state) while
an admin fixes it on the settings page. ``app/main.py:Hub`` builds one
``SettingsStore`` over the hub's database at startup and reads its
:meth:`SettingsStore.snapshot` as ``HubSettings`` (and again on every
``Hub.reload``), so a section saved here - through the settings page, or
once through ``app/legacy.py:import_legacy`` on the old environment - is
what the running hub actually reads. ``app/legacy.py:LegacyEnv`` is read
only inside that one-time import; there is no other path that builds a
``HubSettings`` from the environment.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from app.db import Database, utc_now_iso
from app.logging_setup import log
from app.modules.ai_usage.settings import SECTION as _AI_USAGE_SECTION, AIUsageSettings
from app.modules.alert.settings import SECTION as _ALERT_SECTION, AlertSettings
from app.modules.brief.settings import SECTION as _BRIEF_SECTION, BriefSettings
from app.modules.calendar.settings import SECTION as _CALENDAR_SECTION, CalendarSettings
from app.modules.device.settings import SECTION as _DEVICE_SECTION, DeviceSettings
from app.modules.general.settings import SECTION as _GENERAL_SECTION, GeneralSettings
from app.modules.home.settings import SECTION as _HOME_SECTION, HomeSettings
from app.modules.registry import (
    SECTION as _MODULES_SECTION,
    ModulesSettings,
    builtin_registry,
)
from app.modules.tasks.settings import SECTION as _TASKS_SECTION, TasksSettings
from app.modules.weather.settings import SECTION as _WEATHER_SECTION, WeatherSettings

logger = logging.getLogger("app.settings")

#: The sections core owns. They are not modules and never will be:
#: ``general`` is the hub's own timezone and units, ``device`` belongs to the
#: telemetry routes and the retention sweep (the System page only draws it),
#: ``alert`` is the interrupt page core reserves, and ``modules`` is the
#: registry's own enable/order list.
CORE_SECTIONS: dict[str, type[BaseModel]] = {
    _GENERAL_SECTION: GeneralSettings,
    _DEVICE_SECTION: DeviceSettings,
    _ALERT_SECTION: AlertSettings,
    _MODULES_SECTION: ModulesSettings,
}

#: Wizard order. Insertion order of :data:`SECTIONS` is load-bearing - the
#: settings page and the setup wizard both walk it - but the registry orders
#: modules by the window list, which is not the order an owner fills the
#: wizard in. So membership comes from the registry and the order comes from
#: here; a section this list does not name (a third-party module's) sorts
#: after the ones it does, by name.
SECTION_ORDER: tuple[str, ...] = (
    _GENERAL_SECTION,
    _TASKS_SECTION,
    _CALENDAR_SECTION,
    _WEATHER_SECTION,
    _AI_USAGE_SECTION,
    _BRIEF_SECTION,
    _HOME_SECTION,
    _DEVICE_SECTION,
    _ALERT_SECTION,
    _MODULES_SECTION,
)


def order_sections(sections: dict[str, type[BaseModel]]) -> dict[str, type[BaseModel]]:
    """``sections`` in :data:`SECTION_ORDER`, unknown names last, by name."""
    rank = {name: index for index, name in enumerate(SECTION_ORDER)}
    return {
        name: sections[name]
        for name in sorted(sections, key=lambda name: (rank.get(name, len(rank)), name))
    }


#: Every settings section this hub knows at import time: each built-in
#: module's own section (``app/modules/registry.py:Registry.sections``) plus
#: the core ones above. A module installed into ``DATA_DIR/modules/`` brings
#: its section with it at runtime, through the hub's own registry; this
#: module-level map exists before any hub does, so it can only speak for the
#: modules that ship with the hub.
SECTIONS: dict[str, type[BaseModel]] = order_sections(
    {**builtin_registry().sections(), **CORE_SECTIONS}
)


class HubSettings(BaseModel):
    """One attribute per settings section, every one defaulted.

    Building one with no arguments is a hub that has never saved anything:
    every section is that model's own defaults, the same honest live
    selectors the hub has always shipped (never ``fixture``).
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
    modules: ModulesSettings = Field(default_factory=ModulesSettings)


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


__all__ = ["CORE_SECTIONS", "SECTIONS", "SECTION_ORDER", "HubSettings", "SettingsStore", "order_sections"]
