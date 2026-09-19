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

#: The ``settings`` row name this model reads and writes.
SECTION = "general"

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


__all__ = ["SECTION", "GeneralSettings", "Units"]
