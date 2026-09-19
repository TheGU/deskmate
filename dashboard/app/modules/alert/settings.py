"""The ``alert`` settings section: the one knob the alert page's default
duration has.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: The ``settings`` row name this model reads and writes.
SECTION = "alert"


class AlertSettings(BaseModel):
    """Default duration for an alert that does not specify its own."""

    model_config = ConfigDict(extra="ignore")

    default_duration_seconds: int = Field(
        default=90,
        description="How long an alert stays on screen when it does not specify its own duration.",
    )


__all__ = ["SECTION", "AlertSettings"]
