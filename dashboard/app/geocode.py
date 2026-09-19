"""Place search for the weather section: Open-Meteo's geocoding API.

The weather section needs a latitude and a longitude, and nobody knows
theirs. This is the "Find" box next to those two fields: a plain GET form
submits a place name, this module asks Open-Meteo what it matches, and the
settings page lists the answers as radio buttons that fill the three weather
fields when the section is saved.

Two rules this module exists to keep in one place:

* The query is never logged. Not at INFO, not in an error line, not through
  ``str(exc)``: an httpx error carries the request URL, and the URL carries
  the place the owner searched for. Failures are logged with the exception
  type only.
* An upstream failure is one line on the page, never a 500. The search is a
  convenience; the coordinates can always be typed in by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.logging_setup import log

logger = logging.getLogger("app.geocode")

#: The upstream host, as the plan requires: a constant, never built from
#: anything a request carries.
GEOCODING_HOST = "https://geocoding-api.open-meteo.com"
GEOCODING_URL = f"{GEOCODING_HOST}/v1/search"

#: Longest query sent upstream. A place name is short; anything longer is a
#: mistake or an attempt to make the hub fetch something odd.
MAX_QUERY_LENGTH = 80

#: How many matches to ask for. Enough to disambiguate a common city name,
#: few enough to read as a list of radio buttons.
RESULT_COUNT = 8

#: What the user is told when the search does not work. Deliberately free of
#: any detail that could echo the query back onto the page or into a log.
SEARCH_FAILED = "Location search is not reachable right now. Type the coordinates instead."


class GeocodeFailed(RuntimeError):
    """The upstream search failed. Carries no query and no upstream text."""


@dataclass(frozen=True, slots=True)
class Place:
    """One match: what to show, and what saving it stores."""

    name: str
    admin1: str
    country: str
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        """The radio button's text: the place, then its coordinates."""
        where = ", ".join(part for part in (self.name, self.admin1, self.country) if part)
        return f"{where} ({self.latitude:.4f}, {self.longitude:.4f})"

    @property
    def value(self) -> str:
        """The radio button's value: latitude, longitude and the name that
        the weather section stores. Parsed back with ``split(",", 2)``, so a
        name containing a comma survives the round trip."""
        return f"{self.latitude},{self.longitude},{self.name}"


@dataclass(frozen=True, slots=True)
class PlaceSearch:
    """What the weather section's Find box renders: where it submits, what
    was searched for, what came back, and the one error line if it did not."""

    url: str
    query: str = ""
    error: str = ""
    places: tuple[Place, ...] = ()


def clean_query(raw: str) -> str:
    """The submitted query, trimmed and capped, ready to send upstream."""
    return raw.strip()[:MAX_QUERY_LENGTH]


def _place(entry: dict[str, Any]) -> Place | None:
    """One upstream result as a :class:`Place`, or ``None`` if it has no
    usable coordinates: upstream shape is not this hub's to guarantee."""
    latitude = entry.get("latitude")
    longitude = entry.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return Place(
        name=str(entry.get("name") or ""),
        admin1=str(entry.get("admin1") or ""),
        country=str(entry.get("country") or ""),
        latitude=float(latitude),
        longitude=float(longitude),
    )


async def search(query: str, timeout_seconds: float) -> tuple[Place, ...]:
    """Ask Open-Meteo what ``query`` matches.

    Raises :class:`GeocodeFailed` on any upstream trouble, with a message
    that holds neither the query nor the upstream error text.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                GEOCODING_URL,
                params={
                    "name": query,
                    "count": RESULT_COUNT,
                    "language": "en",
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # type(exc).__name__ only: str(exc) on an httpx error prints the
        # request URL, and the URL holds the query.
        log(logger, logging.WARNING, "location search failed", error=type(exc).__name__)
        raise GeocodeFailed(SEARCH_FAILED) from exc

    results = payload.get("results")
    if not isinstance(results, list):
        return ()
    places = [_place(entry) for entry in results if isinstance(entry, dict)]
    return tuple(place for place in places if place is not None)


__all__ = [
    "GEOCODING_HOST",
    "GEOCODING_URL",
    "MAX_QUERY_LENGTH",
    "RESULT_COUNT",
    "SEARCH_FAILED",
    "GeocodeFailed",
    "Place",
    "PlaceSearch",
    "clean_query",
    "search",
]
