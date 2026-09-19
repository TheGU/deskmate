"""The ``calendar`` settings section: one or more ICS feeds and how far
ahead the agenda page looks.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

#: The ``settings`` row name this model reads and writes.
SECTION = "calendar"

CalendarSource = Literal["ics", "fixture"]

#: The panel's six-ink palette, minus white: what a feed may be coloured.
FeedColor = Literal["blue", "green", "yellow", "red", "black"]


class Feed(BaseModel):
    """One ICS feed: where to fetch it, what to call it, what colour it gets."""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="The ICS feed's URL.")
    name: str = Field(
        default="",
        description="Label shown on the agenda page. Left blank, the feed's URL host is used.",
    )
    color: FeedColor = Field(
        default="blue",
        description="Panel colour for this feed's events: blue, green, yellow, red or black.",
    )


class CalendarSettings(BaseModel):
    """Calendar source, its feeds, and the agenda window and cache TTL."""

    model_config = ConfigDict(extra="ignore")

    source: CalendarSource = Field(
        default="ics",
        description="Where calendar events come from: the configured ICS feeds, or demo data.",
    )
    feeds: list[Feed] = Field(
        default_factory=list,
        description="The ICS feeds to fetch and merge onto the agenda page.",
    )
    agenda_days: int = Field(
        default=7,
        description="How many days ahead the agenda page shows.",
    )
    ttl_seconds: float = Field(
        default=300.0,
        description="How long a fetched calendar is cached before it is fetched again.",
    )

    def feed_name(self, index: int) -> str:
        """The label for feed ``index``: its configured name, else its URL's
        host, else ``"calendar N"``, the same fallback ``config.py:
        ics_calendar_name`` used before feeds moved into the database."""
        try:
            feed = self.feeds[index]
        except IndexError:
            return f"calendar {index + 1}"
        if feed.name:
            return feed.name
        host = urlparse(feed.url).hostname
        return host or f"calendar {index + 1}"


__all__ = ["SECTION", "CalendarSettings", "CalendarSource", "Feed", "FeedColor"]
