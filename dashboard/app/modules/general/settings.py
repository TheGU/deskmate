"""The ``general`` settings section: timezone and units, read by every page.

Loaded and saved through ``app/settings.py:SettingsStore`` under the section
name :data:`SECTION`. ``extra="ignore"`` on every model in this tree means a
row written by an older build that dropped a field still loads instead of
refusing to start.
"""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules import MODULE_ID_RE

#: The ``settings`` row name this model reads and writes.
SECTION = "general"

#: What :attr:`GeneralSettings.header_widget` means when it asks for an
#: empty slot, and what a page's own override means when it defers to this
#: section (``app/modules/registry.py:ModuleToggle.header_widget``).
HEADER_WIDGET_NONE = "none"

Units = Literal["metric", "imperial"]


class GeneralSettings(BaseModel):
    """Timezone and unit system every page renders with."""

    model_config = ConfigDict(extra="ignore")

    timezone: str = Field(
        default="Asia/Bangkok",
        max_length=64,
        description=(
            "IANA timezone name the hub renders every date and time in, "
            "for example Asia/Bangkok or Europe/Berlin."
        ),
    )
    units: Units = Field(
        default="metric",
        description="Measurement system for temperature and other units: metric or imperial.",
    )
    header_widget: str = Field(
        default="weather",
        max_length=64,
        description=(
            "Which module fills the header's widget slot, on every page that "
            "does not override it in the Modules section. Built-in widgets: "
            "weather, agenda, system, ai_usage. Use none for an empty slot."
        ),
    )

    @field_validator("header_widget")
    @classmethod
    def _validate_header_widget(cls, value: str) -> str:
        """Shape only: a module id, or ``none``.

        Deliberately not "a module that has a widget on this hub". This
        model is validated with no registry in reach on two paths that must
        keep working - restoring a backup taken on a hub with other modules
        installed (``app/backup.py``) and the one-time legacy import - and
        refusing there would turn a foreign choice into a failed restore.
        The settings page checks the live registry before it writes
        (``app/settings_pages.py:_unknown_header_widgets``) and the render
        falls back to the first installed widget
        (``app/view.py:resolve_header_widget``).
        """
        if value != HEADER_WIDGET_NONE and not MODULE_ID_RE.match(value):
            raise ValueError(
                f"{value!r} is not a module id or {HEADER_WIDGET_NONE!r}; "
                f"it must match {MODULE_ID_RE.pattern}"
            )
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            # ZoneInfo() looks the name up on the filesystem, so a name that
            # is not a legal filename (too long, or carrying a character such
            # as "<" on Windows) raises OSError rather than either of the
            # "not a timezone" exceptions above - and would otherwise escape
            # as an unhandled 500 from POST /settings/general.
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


__all__ = ["HEADER_WIDGET_NONE", "SECTION", "GeneralSettings", "Units"]
