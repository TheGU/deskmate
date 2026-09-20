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
saves one back. It is built over a section *map*, not over :data:`SECTIONS`
directly: a module installed into ``DATA_DIR/modules/`` exists only at
runtime, so the running hub passes :func:`sections_for` over its own
registry and that module's section is loaded, saved and rendered like any
built-in (its value lands in :attr:`HubSettings.extra`, read back through
:meth:`HubSettings.section`). A missing row means defaults; a row that fails to parse or
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
from typing import TypeVar

from pydantic import BaseModel, Field, SerializeAsAny, ValidationError

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
    Registry,
    builtin_registry,
)
from app.modules.tasks.settings import SECTION as _TASKS_SECTION, TasksSettings
from app.modules.weather.settings import SECTION as _WEATHER_SECTION, WeatherSettings

logger = logging.getLogger("app.settings")

SectionT = TypeVar("SectionT", bound=BaseModel)

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


def sections_for(registry: Registry) -> dict[str, type[BaseModel]]:
    """Every settings section a hub whose modules are ``registry`` has.

    :data:`SECTIONS` is this same thing for the built-ins alone. A running
    hub uses this instead, because a module installed into
    ``DATA_DIR/modules/`` or through an entry point brings a section with
    it that no module-level constant could know about
    (``app/main.py:Hub``). Disabled modules are included: their sections
    still have to load, save and render, or turning a module off would
    throw away what its section holds.
    """
    return order_sections({**registry.sections(), **CORE_SECTIONS})


#: The sections :class:`HubSettings` carries as fixed attributes. Every
#: other installed module's section lands in :attr:`HubSettings.extra`, and
#: :meth:`HubSettings.section` is what reads either kind without the caller
#: having to know which is which.
BUILTIN_SECTIONS: frozenset[str] = frozenset(SECTIONS)


class HubSettings(BaseModel):
    """One attribute per built-in settings section, every one defaulted,
    plus :attr:`extra` for the sections installed modules bring.

    Building one with no arguments is a hub that has never saved anything:
    every section is that model's own defaults, the same honest live
    selectors the hub has always shipped (never ``fixture``).

    The built-in sections stay fixed attributes rather than collapsing into
    ``extra`` because every adapter, page and route in the tree reads them
    by name and type (``settings.weather.source``), and a mapping of
    ``BaseModel`` would cost all of that its typing. A module core has
    never heard of cannot have an attribute here at all, so it gets a slot
    in ``extra`` and reads it back through :meth:`section`, which answers
    for both kinds.
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

    #: Every installed module's own section that is not one of the fixed
    #: attributes above, keyed by section name. ``SerializeAsAny`` for the
    #: same reason ``DashboardState.blocks`` needs it: without it pydantic
    #: would serialize each value as the bare ``BaseModel`` and a backup or
    #: an ``/api`` response would carry empty objects.
    extra: dict[str, SerializeAsAny[BaseModel]] = Field(default_factory=dict)

    def section(self, name: str, model: type[SectionT]) -> SectionT:
        """``name``'s settings as a ``model``, or that model's defaults.

        The one accessor a module's context builder, adapter or route needs:
        it does not have to know whether its section is a fixed attribute
        here (a built-in) or a row in :attr:`extra` (anything else), and a
        section with no stored value at all answers with defaults rather
        than ``None``, so nobody None-checks their own settings.

        A value stored under a different model - a snapshot taken while the
        module was not installed, or a backup from before it was - is
        re-validated through ``model``, keeping the fields it declares and
        dropping the rest.
        """
        value = getattr(self, name) if name in BUILTIN_SECTIONS else self.extra.get(name)
        if isinstance(value, model):
            return value
        if value is None:
            return model()
        return model.model_validate(value.model_dump())


class SettingsStore:
    """Loads and saves settings sections through the hub's database.

    Call it with a migrated :class:`app.db.Database` and the section map it
    is to speak for. ``sections`` defaults to :data:`SECTIONS`, the
    built-ins, which is what a caller with no hub to hand (a test, a
    script) wants; ``app/main.py:Hub`` passes :func:`sections_for` over its
    own registry, so a module installed into ``DATA_DIR/modules/`` gets its
    section loaded, saved and snapshotted like any built-in.

    Every method is plain synchronous I/O, like ``Database.reading``/
    ``writing`` themselves; an async route calls in through
    ``run_in_threadpool``.
    """

    def __init__(self, db: Database, sections: dict[str, type[BaseModel]] | None = None) -> None:
        self._db = db
        self._sections = dict(SECTIONS if sections is None else sections)

    @property
    def sections(self) -> dict[str, type[BaseModel]]:
        """The section map this store reads and writes, in page order."""
        return dict(self._sections)

    def modules(self) -> ModulesSettings:
        """The ``modules`` section, typed.

        Its own method because it is the one section that has to be read
        before the section map is even known: the registry is built from it
        and the registry is what says which other sections exist, so
        ``Hub`` reads it through a store over the built-ins first and
        builds the real store afterwards (``app/main.py:Hub._rebuild``).
        """
        value = self.load(_MODULES_SECTION)
        return value if isinstance(value, ModulesSettings) else ModulesSettings()

    def load(self, section: str) -> BaseModel:
        """The section's model, built from its row, or defaults.

        A missing row is silent (a hub that never saved this section). A row
        that is not valid JSON, or that fails the model's validation, is
        logged at WARNING and defaults are returned instead of raising: one
        damaged section must not stop the hub from serving every other page.
        """
        model = self._sections[section]
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
        model = self._sections[section]
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
        """Every section this store knows, loaded, as one :class:`HubSettings`.

        A built-in section lands on its own attribute; anything else - a
        module installed into ``DATA_DIR/modules/`` or through an entry
        point - lands in :attr:`HubSettings.extra`, where
        :meth:`HubSettings.section` reads it back.
        """
        fixed: dict[str, BaseModel] = {}
        extra: dict[str, BaseModel] = {}
        for section in self._sections:
            target = fixed if section in BUILTIN_SECTIONS else extra
            target[section] = self.load(section)
        return HubSettings(**fixed, extra=extra)

    def updated_at(self, section: str) -> datetime | None:
        """When ``section`` was last saved, or ``None`` before it ever was."""
        with self._db.reading() as connection:
            row = connection.execute(
                "SELECT updated_at FROM settings WHERE section = ?", (section,)
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(str(row["updated_at"]))


__all__ = [
    "BUILTIN_SECTIONS",
    "CORE_SECTIONS",
    "SECTIONS",
    "SECTION_ORDER",
    "HubSettings",
    "SettingsStore",
    "order_sections",
    "sections_for",
]
