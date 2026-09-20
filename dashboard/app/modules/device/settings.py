"""The ``device`` settings section: how long telemetry history is kept, and
the refresh schedule the device itself follows.

``refresh_minutes``, ``telemetry_minutes`` and ``wake_hours`` used to be
ESPHome substitutions compiled into ``firmware/e1002.yaml``; changing any of
them meant a reflash. They now live here instead: the telemetry POST
response (``app/main.py:post_device_telemetry``) carries the three values
next to ``page_count``/``pages``, and the firmware stores them in
restorable globals and applies them on its own two interval components and
its battery wake slot, with no reflash. The compiled substitutions remain
as the first-boot defaults for a device that has never talked to a hub yet
(see docs/FLASHING.md).

``app/forms.py`` only renders a plain list of rows for ``list[Model]``
fields, not a bare ``list[int]``, so ``wake_hours`` is a comma separated
string field here, normalized and bounds-checked by its validator; other
code reads :attr:`DeviceSettings.wake_hours_list` for the parsed value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The ``settings`` row name this model reads and writes.
SECTION = "device"

DeviceSource = Literal["store", "fixture"]


class DeviceSettings(BaseModel):
    """Device telemetry source, its retention window, the cache TTL, and the
    refresh schedule the device follows."""

    model_config = ConfigDict(extra="ignore")

    source: DeviceSource = Field(
        default="store",
        description="Where device status comes from: what the device itself posted, or demo data.",
    )
    retention_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        description="How many days of posted telemetry are kept before older rows are pruned.",
    )
    ttl_seconds: float = Field(
        default=60.0,
        ge=0,
        le=86400 * 7,
        allow_inf_nan=False,
        description="How long the latest telemetry summary is cached before it is re-read.",
    )
    refresh_minutes: int = Field(
        default=30,
        ge=5,
        le=240,
        description=(
            "How often the device asks the hub for a fresh panel image, in minutes. "
            "Sent to the device on its next telemetry post and applied to its "
            "polling interval right away, no reflash."
        ),
    )
    telemetry_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        description=(
            "How often the device posts a telemetry sample (battery, temperature, "
            "humidity, page) to the hub, in minutes. Applied the same way as "
            "Refresh minutes, no reflash."
        ),
    )
    wake_hours: str = Field(
        default="8, 12, 17",
        max_length=96,
        description=(
            "Local hours the device wakes at while running on battery, comma "
            "separated (0 to 23, any of the 24 hours, for example \"8, 12, 17\"). "
            "Applied on the device's next telemetry post, no reflash."
        ),
    )

    @field_validator("wake_hours")
    @classmethod
    def _validate_wake_hours(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",") if part.strip() != ""]
        if not parts:
            raise ValueError("wake_hours must list at least one hour")
        try:
            hours = [int(part) for part in parts]
        except ValueError as exc:
            raise ValueError("wake_hours must be a comma separated list of whole hours") from exc
        for hour in hours:
            if hour < 0 or hour > 23:
                raise ValueError("wake_hours must list hours between 0 and 23")
        hours = sorted(set(hours))
        if not (1 <= len(hours) <= 24):
            raise ValueError("wake_hours must list 1 to 24 distinct hours")
        return ", ".join(str(hour) for hour in hours)

    @property
    def wake_hours_list(self) -> list[int]:
        """The parsed, sorted, de-duplicated hours: what the telemetry
        response and any other code should read, rather than re-parsing the
        stored string."""
        return [int(part.strip()) for part in self.wake_hours.split(",") if part.strip() != ""]


__all__ = ["SECTION", "DeviceSettings", "DeviceSource"]
