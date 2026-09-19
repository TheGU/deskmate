"""The ``brief`` settings section: the pushed morning/evening brief and when
it switches from one mode to the other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The ``settings`` row name this model reads and writes.
SECTION = "brief"

BriefSource = Literal["push", "fixture"]


class BriefSettings(BaseModel):
    """Brief source, the morning/evening switch hour, and the TTLs for it."""

    model_config = ConfigDict(extra="ignore")

    source: BriefSource = Field(
        default="push",
        description="Where the brief comes from: an agent pushing it, or demo data.",
    )
    evening_hour: int = Field(
        default=14,
        ge=0,
        le=23,
        description="Local hour (0-23) at which the brief switches from morning to evening mode.",
    )
    ttl_seconds: float = Field(
        default=60.0,
        description="How long a pushed brief is cached before it is re-read.",
    )
    stale_seconds: float = Field(
        default=36000.0,
        description="How old the pushed brief can get before the panel marks it stale.",
    )


__all__ = ["SECTION", "BriefSettings", "BriefSource"]
