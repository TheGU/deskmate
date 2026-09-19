"""The ``ai_usage`` settings section: the pushed AI usage dataset the brief
and the today page draw from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The ``settings`` row name this model reads and writes.
SECTION = "ai_usage"

AIUsageSource = Literal["push", "fixture"]


class AIUsageSettings(BaseModel):
    """AI usage source, and the cache TTL and staleness threshold for it."""

    model_config = ConfigDict(extra="ignore")

    source: AIUsageSource = Field(
        default="push",
        description="Where AI usage data comes from: an agent pushing it, or demo data.",
    )
    ttl_seconds: float = Field(
        default=300.0,
        ge=0,
        le=86400 * 7,
        allow_inf_nan=False,
        description="How long a pushed usage snapshot is cached before it is re-read.",
    )
    stale_seconds: float = Field(
        default=21600.0,
        ge=0,
        le=86400 * 7,
        allow_inf_nan=False,
        description="How old the newest pushed usage sample can get before the panel marks it stale.",
    )


__all__ = ["SECTION", "AIUsageSettings", "AIUsageSource"]
