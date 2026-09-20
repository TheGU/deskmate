"""Calendar adapters: fixtures and ICS (URL or local file).

The ICS reader expands simple recurrence rules with ``dateutil`` inside the
agenda window only, so a vault of ten-year-old repeating meetings does not turn
into a million events.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import httpx
from dateutil.rrule import rrulestr
from icalendar import Calendar as ICalendar

from app.adapters.base import AdapterError, AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_iso
from app.config import Env
from app.logging_setup import log
from app.models import Event
from app.modules.calendar.settings import CalendarSettings
from app.modules.general.settings import GeneralSettings
from app.timeutil import to_local, today_local, zone

logger = logging.getLogger("app.adapters.calendar")

#: How far back and forward the ICS reader looks.
WINDOW_BEFORE_DAYS = 1
WINDOW_AFTER_DAYS = 21
MAX_OCCURRENCES = 50


class FixtureCalendarAdapter:
    """Events from the agenda module's own ``fixtures/calendar.json``."""

    name = "calendar"
    source = "fixture"

    def __init__(
        self, calendar: CalendarSettings, general: GeneralSettings, env: Env, fixture: Path
    ) -> None:
        self._calendar = calendar
        self._general = general
        self._env = env
        self._fixture = fixture

    async def fetch(self) -> list[Event]:
        env = self._env
        timezone_name = self._general.timezone
        payload = load_fixture(self._fixture)
        delta = day_delta(
            payload, today_local(timezone_name), enabled=env.fixture_relative_dates
        )
        events: list[Event] = []
        raw_events: Any = payload.get("events", [])
        for raw in raw_events:
            item = dict(raw)
            item["start"] = shift_iso(item.get("start"), delta)
            item["end"] = shift_iso(item.get("end"), delta)
            item.setdefault("source", "fixture")
            event = Event.model_validate(item)
            event.start = to_local(event.start, timezone_name)
            if event.end is not None:
                event.end = to_local(event.end, timezone_name)
            events.append(event)
        events.sort(key=lambda item: (item.start, item.title))
        return events


class IcsCalendarAdapter:
    """Events from one or more ICS URLs or local ``.ics`` files."""

    name = "calendar"
    source = "ics"

    def __init__(self, calendar: CalendarSettings, general: GeneralSettings, env: Env) -> None:
        self._calendar = calendar
        self._general = general
        self._env = env

    async def fetch(self) -> list[Event]:
        sources = [feed.url for feed in self._calendar.feeds]
        if not sources:
            raise AdapterUnavailable("no calendar feeds are configured")
        # Fetched concurrently: sequentially, N feeds cost up to
        # N x HTTP_TIMEOUT_SECONDS, which is what made the container flap
        # during an outage against the compose healthcheck's 10s timeout.
        # return_exceptions=True plus the in-order scan below keeps the same
        # behaviour the old sequential loop had: the first feed *by
        # position* to fail aborts the whole fetch, exactly as it did when a
        # later feed was never even reached.
        async with httpx.AsyncClient(timeout=self._env.http_timeout_seconds) as client:
            results = await asyncio.gather(
                *(
                    _fetch_feed(client, index, reference, self._calendar.feed_name(index))
                    for index, reference in enumerate(sources)
                ),
                return_exceptions=True,
            )
        texts: list[tuple[str, str]] = []
        for reference, result in zip(sources, results):
            if isinstance(result, BaseException):
                raise result
            texts.append((reference, result))
        timezone_name = self._general.timezone
        today = today_local(timezone_name)
        window_start = datetime.combine(
            today - timedelta(days=WINDOW_BEFORE_DAYS), time.min, tzinfo=zone(timezone_name)
        )
        window_end = window_start + timedelta(days=WINDOW_BEFORE_DAYS + WINDOW_AFTER_DAYS)

        events: list[Event] = []
        for index, (reference, text) in enumerate(texts):
            events.extend(
                parse_ics(
                    text,
                    reference,
                    timezone_name,
                    window_start,
                    window_end,
                    calendar_name=self._calendar.feed_name(index),
                )
            )
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


async def _fetch_feed(client: httpx.AsyncClient, index: int, reference: str, name: str) -> str:
    """:func:`_read_ics` with the URL scrubbed out of whatever it raises.

    A Google or Outlook "secret address" ICS URL is a credential: httpx's own
    ``HTTPStatusError``/``RequestError`` carry the full request URL in their
    ``str()``, and that string would otherwise land in ``Outcome.error`` and
    so in ``/healthz``, ``/api/state`` (readable with the device key) and the
    WARNING log. The feed's index and configured name identify it instead.
    """
    try:
        return await _read_ics(client, reference)
    except httpx.HTTPStatusError as exc:
        raise AdapterError(
            f"calendar feed {index} ({name}): HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise AdapterError(f"calendar feed {index} ({name}): {type(exc).__name__}") from exc


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
    calendar_name: str | None = None,
) -> list[Event]:
    """Parse one ICS document into normalized events inside the window."""
    document = ICalendar.from_ical(text)
    label = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:8]
    events: list[Event] = []
    for component in document.walk("VEVENT"):
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
                    calendar=calendar_name,
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


def build_calendar_adapter(
    calendar: CalendarSettings, general: GeneralSettings, env: Env, fixture: Path
) -> FixtureCalendarAdapter | IcsCalendarAdapter:
    if calendar.source == "ics":
        return IcsCalendarAdapter(calendar, general, env)
    return FixtureCalendarAdapter(calendar, general, env, fixture)
