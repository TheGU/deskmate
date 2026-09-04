"""Calendar adapters: fixtures and ICS (URL or local file).

The ICS reader expands simple recurrence rules with ``dateutil`` inside the
agenda window only, so a vault of ten-year-old repeating meetings does not turn
into a million events.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import httpx
from dateutil.rrule import rrulestr
from icalendar import Calendar as ICalendar

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_iso
from app.config import Settings
from app.logging_setup import log
from app.models import Event
from app.timeutil import to_local, today_local, zone

logger = logging.getLogger("app.adapters.calendar")

#: How far back and forward the ICS reader looks.
WINDOW_BEFORE_DAYS = 1
WINDOW_AFTER_DAYS = 21
MAX_OCCURRENCES = 50


class FixtureCalendarAdapter:
    """Events from ``fixtures/calendar.json``."""

    name = "calendar"
    source = "fixture"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self) -> list[Event]:
        settings = self._settings
        payload = load_fixture(settings.fixtures_dir / "calendar.json")
        delta = day_delta(
            payload, today_local(settings.timezone), enabled=settings.fixture_relative_dates
        )
        events: list[Event] = []
        raw_events: Any = payload.get("events", [])
        for raw in raw_events:
            item = dict(raw)
            item["start"] = shift_iso(item.get("start"), delta)
            item["end"] = shift_iso(item.get("end"), delta)
            item.setdefault("source", "fixture")
            event = Event.model_validate(item)
            event.start = to_local(event.start, settings.timezone)
            if event.end is not None:
                event.end = to_local(event.end, settings.timezone)
            events.append(event)
        events.sort(key=lambda item: (item.start, item.title))
        return events


class IcsCalendarAdapter:
    """Events from one or more ICS URLs or local ``.ics`` files."""

    name = "calendar"
    source = "ics"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self) -> list[Event]:
        sources = self._settings.ics_sources
        if not sources:
            raise AdapterUnavailable("CALENDAR_ICS_URLS is not set")
        texts: list[tuple[str, str]] = []
        async with httpx.AsyncClient(timeout=self._settings.http_timeout_seconds) as client:
            for reference in sources:
                texts.append((reference, await _read_ics(client, reference)))
        timezone_name = self._settings.timezone
        today = today_local(timezone_name)
        window_start = datetime.combine(
            today - timedelta(days=WINDOW_BEFORE_DAYS), time.min, tzinfo=zone(timezone_name)
        )
        window_end = window_start + timedelta(days=WINDOW_BEFORE_DAYS + WINDOW_AFTER_DAYS)

        events: list[Event] = []
        for reference, text in texts:
            events.extend(parse_ics(text, reference, timezone_name, window_start, window_end))
        events.sort(key=lambda item: (item.start, item.title))
        log(logger, logging.INFO, "ics parsed", sources=len(sources), events=len(events))
        return events


async def _read_ics(client: httpx.AsyncClient, reference: str) -> str:
    if reference.startswith(("http://", "https://")):
        response = await client.get(reference, follow_redirects=True)
        response.raise_for_status()
        return response.text
    path = Path(reference)
    if not path.is_file():
        raise AdapterUnavailable(f"ICS file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _as_datetime(value: Any, timezone_name: str) -> tuple[datetime, bool]:
    """Normalize an icalendar date/datetime into local time plus all-day flag."""
    if isinstance(value, datetime):
        return to_local(value, timezone_name), False
    if isinstance(value, date):
        return to_local(datetime.combine(value, time.min), timezone_name), True
    raise ValueError(f"unsupported ICS date value: {value!r}")


def parse_ics(
    text: str,
    reference: str,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
) -> list[Event]:
    """Parse one ICS document into normalized events inside the window."""
    calendar = ICalendar.from_ical(text)
    label = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:8]
    events: list[Event] = []
    for component in calendar.walk("VEVENT"):
        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue
        start, all_day = _as_datetime(dtstart.dt, timezone_name)
        end: datetime | None = None
        dtend = component.get("DTEND")
        if dtend is not None:
            end, _ = _as_datetime(dtend.dt, timezone_name)
        duration = (end - start) if end is not None else timedelta(0)

        uid = str(component.get("UID", "")) or f"{label}-{start.isoformat()}"
        title = str(component.get("SUMMARY", "(no title)"))
        location_raw = component.get("LOCATION")
        location = str(location_raw) if location_raw else None

        starts: Iterable[datetime]
        rrule = component.get("RRULE")
        if rrule is None:
            starts = [start]
        else:
            starts = _expand(component, start, window_start, window_end, timezone_name)

        for index, occurrence in enumerate(starts):
            if occurrence > window_end or (occurrence + duration) < window_start:
                continue
            suffix = "" if index == 0 and rrule is None else f"-{occurrence.date().isoformat()}"
            events.append(
                Event(
                    id=f"ics-{label}-{hashlib.sha256(uid.encode()).hexdigest()[:8]}{suffix}",
                    title=title,
                    start=occurrence,
                    end=(occurrence + duration) if end is not None else None,
                    all_day=all_day,
                    location=location,
                    source="ics",
                )
            )
    return events


def _expand(
    component: Any,
    start: datetime,
    window_start: datetime,
    window_end: datetime,
    timezone_name: str,
) -> list[datetime]:
    rule_text = component.get("RRULE").to_ical().decode("utf-8")
    try:
        rule = rrulestr(f"RRULE:{rule_text}", dtstart=start)
    except (ValueError, TypeError) as exc:
        log(logger, logging.WARNING, "unparsable RRULE", rule=rule_text, error=str(exc))
        return [start]
    excluded: set[datetime] = set()
    exdate = component.get("EXDATE")
    for entry in exdate if isinstance(exdate, list) else ([exdate] if exdate else []):
        for item in getattr(entry, "dts", []):
            moment, _ = _as_datetime(item.dt, timezone_name)
            excluded.add(moment)
    occurrences: list[datetime] = []
    for occurrence in rule.between(window_start, window_end, inc=True):
        moment = to_local(occurrence, timezone_name)
        if moment in excluded:
            continue
        occurrences.append(moment)
        if len(occurrences) >= MAX_OCCURRENCES:
            break
    return occurrences


def build_calendar_adapter(settings: Settings) -> FixtureCalendarAdapter | IcsCalendarAdapter:
    if settings.calendar_source == "ics":
        return IcsCalendarAdapter(settings)
    return FixtureCalendarAdapter(settings)
