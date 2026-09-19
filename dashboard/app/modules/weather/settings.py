"""The ``weather`` settings section: where the forecast comes from and for
which place.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The ``settings`` row name this model reads and writes.
SECTION = "weather"

WeatherSource = Literal["open_meteo", "fixture"]


class WeatherSettings(BaseModel):
    """Weather source, the place it is fetched for, and the cache TTL."""

    model_config = ConfigDict(extra="ignore")

    source: WeatherSource = Field(
        default="open_meteo",
        description="Where the forecast comes from: the Open-Meteo API, or demo data.",
    )
    latitude: float | None = Field(
        default=None,
        description="Latitude of the location to fetch weather for.",
    )
    longitude: float | None = Field(
        default=None,
        description="Longitude of the location to fetch weather for.",
    )
    location_name: str = Field(
        default="",
        description="Label shown on the weather page for this location.",
    )
    ttl_seconds: float = Field(
        default=900.0,
        description="How long a fetched forecast is cached before it is fetched again.",
    )


__all__ = ["SECTION", "WeatherSettings", "WeatherSource"]
