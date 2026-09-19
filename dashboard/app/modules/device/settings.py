"""The ``device`` settings section: how long telemetry history is kept.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The ``settings`` row name this model reads and writes.
SECTION = "device"

DeviceSource = Literal["store", "fixture"]


class DeviceSettings(BaseModel):
    """Device telemetry source, its retention window, and the cache TTL."""

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


__all__ = ["SECTION", "DeviceSettings", "DeviceSource"]
