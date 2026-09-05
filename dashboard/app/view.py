"""Turns :class:`DashboardState` into the flat context each template needs.

Templates stay dumb: no adapter knowledge, no arithmetic, no fallbacks. Every
"unknown" decision is made here, once.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from app import icons
from app.config import Settings
from app.models import (
    AdapterStatus,
    Alert,
    AlertPriority,
    DashboardState,
    DeviceState,
    DeviceStatus,
    Event,
    PRIORITY_RANK,
    Priority,
    Task,
    Weather,
)
from app.renderer.chart import build_chart
from app.timeutil import to_local, zone

UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"

PAGE_TITLES: dict[str, str] = {
    "today": "TODAY",
    "agenda": "NEXT 7 DAYS",
    "weather": "WEATHER",
    "brief": "AI BRIEF",
    "system": "HOME AND SYSTEM",
    "alert": "ALERT",
}

#: What the status bar and the window list call each page. Short enough to sit
#: in a segment; the window list is the device's navigation, so the order is
#: the button order in PRODUCT.md.
PAGE_NAMES: dict[str, str] = {
    "today": "TODAY",
    "agenda": "AGENDA",
    "weather": "WEATHER",
    "brief": "BRIEF",
    "system": "SYSTEM",
    "alert": "ALERT",
}

#: The five pages the left and right buttons walk through. Alert is not in the
#: list: it interrupts and then hands the previous page back.
WINDOW_PAGES: tuple[str, ...] = ("today", "agenda", "weather", "brief", "system")


# ---------------------------------------------------------------------------
# small formatters
# ---------------------------------------------------------------------------
def fmt_number(value: float | int | None, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "--"
    if digits == 0:
        return f"{round(value):d}{suffix}"
    return f"{value:.{digits}f}{suffix}"


def fmt_percent(value: int | None) -> str:
    return "--" if value is None else f"{value}%"


def fmt_time(value: datetime | None, timezone_name: str) -> str:
    return UNKNOWN if value is None else to_local(value, timezone_name).strftime("%H:%M")


def fmt_day_header(value: date) -> str:
    return value.strftime("%a %d %b").upper()


def fmt_long_date(value: datetime) -> str:
    return value.strftime("%a %d %b").upper()


def relative_day_label(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "TOMORROW"
    return day.strftime("%a").upper()


# ---------------------------------------------------------------------------
# task and event selection
# ---------------------------------------------------------------------------
def open_tasks(state: DashboardState) -> list[Task]:
    return [task for task in state.tasks.items if not task.completed]


def task_sort_key(task: Task, today: date) -> tuple[int, int, str]:
    """Overdue first, then priority, then due date, then title."""
    overdue = 0 if (task.due is not None and task.due < today) else 1
    due_rank = (task.due - today).days if task.due is not None else 3650
    return (overdue, PRIORITY_RANK[task.priority] * 1000 + min(due_rank, 999), task.title)


def priority_tasks(state: DashboardState, today: date, limit: int) -> list[dict[str, Any]]:
    tasks = sorted(open_tasks(state), key=lambda task: task_sort_key(task, today))[:limit]
    rows: list[dict[str, Any]] = []
    for task in tasks:
        rows.append(
            {
                "title": task.title,
                "due_label": due_label(task.due, today),
                "accent": task_accent(task, today),
                "priority": task.priority.value,
            }
        )
    return rows


def due_label(due: date | None, today: date) -> str:
    """Short enough to sit next to a task title on an 800px screen."""
    if due is None:
        return ""
    delta = (due - today).days
    if delta < 0:
        return f"{-delta}D LATE"
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "TOMORROW"
    return f"DUE {due.strftime('%d %b').upper()}"


def today_due_label(due: date | None, today: date) -> str:
    """Today's own due chip text: :func:`due_label`, but a task due tomorrow
    prints its 3-letter weekday instead of the word TOMORROW (too long for
    the priority row on the Today page). Brief still shows the word, since
    :func:`due_label` is shared with it and stays unchanged."""
    if due is not None and (due - today).days == 1:
        return due.strftime("%a").upper()
    return due_label(due, today)


def today_priority_tasks(state: DashboardState, today: date, limit: int) -> list[dict[str, Any]]:
    """The Today page's own priority rows: same selection as the shared
    :func:`priority_tasks` (which Brief also calls), with :func:`today_due_label`
    in place of :func:`due_label`."""
    tasks = sorted(open_tasks(state), key=lambda task: task_sort_key(task, today))[:limit]
    rows: list[dict[str, Any]] = []
    for task in tasks:
        rows.append(
            {
                "title": task.title,
                "due_label": today_due_label(task.due, today),
                "accent": task_accent(task, today),
                "priority": task.priority.value,
            }
        )
    return rows


def task_accent(task: Task, today: date) -> str:
    if task.due is not None and task.due < today:
        return "red"
    if task.due is not None and task.due == today:
        return "yellow"
    if task.priority is Priority.HIGH:
        return "black"
    return "black"


def upcoming_events(
    state: DashboardState,
    reference: datetime,
    limit: int,
    colors: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    today = reference.date()
    colors = colors or {}
    events = [event for event in state.calendar.items if _event_end(event) >= reference]
    events.sort(key=lambda event: event.start)
    rows: list[dict[str, Any]] = []
    for event in events[:limit]:
        day = event.start.date()
        if day == today:
            when = "ALL DAY" if event.all_day else event.start.strftime("%H:%M")
        elif (day - today).days == 1:
            when = "TOMORROW" if event.all_day else f"TOMORROW {event.start.strftime('%H:%M')}"
        else:
            prefix = day.strftime("%a").upper()
            when = prefix if event.all_day else f"{prefix} {event.start.strftime('%H:%M')}"
        rows.append(
            {
                "when": when,
                "title": event.title,
                "location": event.location or "",
                "color": event_color(event, colors),
            }
        )
    return rows


def _event_end(event: Event) -> datetime:
    return event.end if event.end is not None else event.start


#: The day scale runs 06:00 to 24:00, a span the fixed-size panel never
#: changes.
SCALE_START_HOUR: int = 6
SCALE_END_HOUR: int = 24
SCALE_SPAN_MINUTES: int = (SCALE_END_HOUR - SCALE_START_HOUR) * 60

def scale_position(minutes_since_midnight: float) -> float:
    """Percent along the 06:00-24:00 scale, clamped to [0, 100].

    Takes minutes since midnight rather than a datetime so the 24:00 end of
    the span (an hour a :class:`~datetime.datetime` cannot itself hold) is a
    plain number: 360 gives 0, 1440 gives 100, 15:06 (906) gives about 50.6.
    """
    minutes = minutes_since_midnight - SCALE_START_HOUR * 60
    return max(0.0, min(100.0, minutes / SCALE_SPAN_MINUTES * 100.0))


def _minutes_since_midnight(value: datetime) -> float:
    return value.hour * 60 + value.minute + value.second / 60.0


#: The Today left column is a fixed 372 px tall (the shared body height).
#: Below the priorities block the whole rest becomes the agenda list: two
#: 19.1875 px labels (measured: 16 px caps at line-height 1.2), the
#: priorities' own 120 px (three 40 px rows, the configured cap) and a 2 px
#: rule, with the column's own 8 px gap between every one of those five
#: children (4 gaps). That leaves 372 - (19.1875 * 2 + 120 + 2 + 4 * 8) =
#: 179.625 px, so four 36 px rows fit; a fifth would overrun by a third of a
#: pixel. Measured against the real rendered layout, not guessed.
TODAY_LEFT_HEIGHT_PX: float = 372.0
AGENDA_ROW_HEIGHT_PX: float = 36.0
AGENDA_ROW_LIMIT: int = 4


def today_next_rows(
    state: DashboardState, reference: datetime, colors: dict[str, str], limit: int = AGENDA_ROW_LIMIT
) -> list[dict[str, Any]]:
    """One line per upcoming event: today's events that have not ended yet
    first, then later days in start order, capped at the row count the left
    column actually has room for.

    The word TOMORROW never appears here (the owner asked for it gone): a
    later day always prints as its 3-letter weekday, timed or all-day.
    """
    if not state.calendar.usable:
        return []
    today = reference.date()
    events = sorted(
        (event for event in state.calendar.items if _event_end(event) >= reference),
        key=lambda event: event.start,
    )
    rows: list[dict[str, Any]] = []
    for event in events[:limit]:
        day = event.start.date()
        if day == today:
            when = "ALL DAY" if event.all_day else event.start.strftime("%H:%M")
        else:
            prefix = day.strftime("%a").upper()
            when = prefix if event.all_day else f"{prefix} {event.start.strftime('%H:%M')}"
        rows.append({"when": when, "title": event.title, "color": event_color(event, colors)})
    return rows


# ---------------------------------------------------------------------------
# page contexts
# ---------------------------------------------------------------------------
#: Colours handed out to calendars nobody named, in the order the calendars
#: first appear. Red and yellow are left out: they mean overdue and caution
#: everywhere else and a calendar is not a state.
DEFAULT_CALENDAR_COLORS: tuple[str, ...] = ("blue", "green", "yellow")


def calendar_colors(state: DashboardState, settings: Settings) -> dict[str, str]:
    """Colour per calendar name, configured first, then the default cycle."""
    mapping: dict[str, str] = {}
    configured = settings.calendar_color_list
    for index, name in enumerate(settings.calendar_name_list):
        mapping[name.lower()] = (
            configured[index]
            if index < len(configured)
            else DEFAULT_CALENDAR_COLORS[index % len(DEFAULT_CALENDAR_COLORS)]
        )
    unnamed = 0
    for event in state.calendar.items:
        key = (event.calendar or "").lower()
        if not key or key in mapping:
            continue
        mapping[key] = DEFAULT_CALENDAR_COLORS[unnamed % len(DEFAULT_CALENDAR_COLORS)]
        unnamed += 1
    return mapping


def event_color(event: Event, colors: dict[str, str]) -> str:
    """The colour an event's time prints in. Black when it has no calendar."""
    return colors.get((event.calendar or "").lower(), "black")


#: Worst first. A pane title bar carries the most urgent thing under it, so a
#: single red row colours the whole title even when the rest is green.
ACCENT_ORDER: tuple[str, ...] = ("red", "yellow", "green")


def worst_accent(accents: Any) -> str:
    """The loudest accent in a group, or black when the group says nothing."""
    seen = set(accents)
    for accent in ACCENT_ORDER:
        if accent in seen:
            return accent
    return "black"


def meter_cells(value: float | None, filled: bool = True) -> list[bool]:
    """Ten block meter cells, one per ten percent.

    An unknown quantity is ten empty cells, never a guessed fill.
    """
    if value is None or not filled:
        return [False] * 10
    lit = int(round(max(0.0, min(100.0, float(value))) / 10.0))
    return [index < lit for index in range(10)]


def overdue_tasks(state: DashboardState, today: date) -> list[Task]:
    if not state.tasks.usable:
        return []
    return [
        task
        for task in sorted(open_tasks(state), key=lambda item: (item.due or today, item.title))
        if task.due is not None and task.due < today
    ]


def window_flags(state: DashboardState, overdue_count: int) -> set[str]:
    """Pages the window list marks with a "!".

    tmux flags a window that wants attention, and a flag on everything is a
    flag on nothing, so the bar is deliberately hard to set: something has to
    be late, broken or unhealthy, not merely worth reading. Rain is not a flag
    because the status bar already carries it on every page.
    """
    flagged: set[str] = set()
    if overdue_count > 0:
        flagged.add("agenda")

    home = state.home.home
    if state.home.usable and home is not None:
        # A degraded service is on the System page already; only a service
        # that is actually down is worth sending the owner there.
        if any(service.health.value == "down" for service in home.services):
            flagged.add("system")

    device = state.device.device
    if state.device.usable and device is not None and device.status is DeviceStatus.STALE:
        flagged.add("system")

    weather = state.weather.weather
    if state.weather.usable and weather is not None:
        air = (
            uv_accent(weather.uv_index),
            pm25_accent(weather.pm2_5),
            aqi_accent(weather.aqi),
        )
        if "red" in air:
            flagged.add("weather")
    return flagged


#: Header battery reading: yellow at or below 20 percent, red at or below 10.
#: Different (tighter) thresholds than :func:`battery_accent`, which colours
#: the System page's own battery reading; the header only has room to warn
#: right before the device actually goes flat.
def header_battery_chip(level: float | None) -> str:
    if level is None:
        return ""
    if level <= 10:
        return "red"
    if level <= 20:
        return "yellow"
    return ""


def wifi_level(rssi: float | None) -> str:
    """The word for a Wi-Fi reading, matching :func:`icons.wifi_icon`."""
    if rssi is None or rssi < -80:
        return "off"
    if rssi <= -68:
        return "low"
    return "strong"


def header_weather(state: DashboardState) -> dict[str, Any]:
    """The header's weather reading: a numeral, a glyph, and one state label.

    Heat outranks rain (a 40 degree feel is the thing to know first); a dry,
    cool reading gets the plain condition word with no dot and no colour.
    """
    weather = state.weather.weather
    if not state.weather.usable or weather is None:
        return {"available": False}
    if is_heat(weather):
        color, label = "red", "HEAT"
    elif weather.rain_from:
        color, label = "blue", f"RAIN {weather.rain_from}"
    else:
        color, label = "", weather.condition.upper()
    return {
        "available": True,
        "temp": fmt_number(weather.temperature_c),
        "icon": icons.weather_icon(weather.condition),
        "color": color,
        "dot": color,
        "label": label,
    }


def header_context(state: DashboardState, today: date, reference: datetime) -> dict[str, Any]:
    """Everything the shared header draws, on every page."""
    device: DeviceState | None = state.device.device
    has_device = state.device.usable and device is not None and device.has_reading
    level = device.battery_level if has_device else None
    rssi = device.wifi_rssi if has_device else None
    charging = has_device and device.charge_state == "charging"
    return {
        "day": reference.strftime("%d"),
        "weekday": reference.strftime("%a").upper(),
        "month": reference.strftime("%b").upper(),
        "weather": header_weather(state),
        "overdue_count": len(overdue_tasks(state, today)),
        "wifi_icon": icons.wifi_icon(rssi),
        "battery": {
            "icon": icons.battery_icon(level, charging),
            "percent": fmt_number(level, digits=0),
            "chip_accent": header_battery_chip(level),
        },
        "clock": reference.strftime("%H:%M"),
    }


def footer_context(state: DashboardState, today: date, page: str) -> dict[str, Any]:
    """The window list: the five pages the buttons walk through."""
    overdue_count = len(overdue_tasks(state, today))
    flagged = window_flags(state, overdue_count)
    return {
        "windows": [
            {
                "index": index,
                "name": PAGE_NAMES[name],
                "flag": name in flagged,
                "active": name == page,
            }
            for index, name in enumerate(WINDOW_PAGES, start=1)
        ],
    }


def base_context(state: DashboardState, settings: Settings, page: str) -> dict[str, Any]:
    reference = to_local(state.updated_at, state.timezone)
    today = reference.date()
    return {
        "page": page,
        "page_title": PAGE_TITLES.get(page, page.upper()),
        "state": state,
        "settings": settings,
        "today": today,
        "reference": reference,
        # Named constants for the fixed glyphs; the chosen ones are per row.
        "icons": icons,
        "header": header_context(state, today, reference),
        "footer": footer_context(state, today, page),
        # Each page builder fills this with one accent per pane title bar.
        # Kept for agenda, weather, brief and system until their own parts
        # rebuild them off the older pane-title chrome.
        "title_accents": {},
    }


def today_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "today")
    reference: datetime = context["reference"]
    today: date = context["today"]
    colors = calendar_colors(state, settings)

    context["priorities"] = (
        today_priority_tasks(state, today, settings.max_priority_tasks)
        if state.tasks.usable
        else []
    )
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    context["agenda_rows"] = today_next_rows(state, reference, colors)
    context["calendar_note"] = block_note(state.calendar.status, "calendar")
    context["providers"] = ai_capacity_rows(state)
    context["usage_note"] = block_note(state.ai_usage.status, "AI quota")
    context["ai_note"] = brief_note(state)
    return context


#: The panel is read in Bangkok, where 36 C in the shade or a 40 C feel is the
#: difference between walking and taking a taxi.
HEAT_TEMPERATURE_C: float = 36.0
HEAT_FEELS_LIKE_C: float = 40.0


def is_heat(weather: Weather | None) -> bool:
    if weather is None:
        return False
    return (weather.temperature_c or 0.0) >= HEAT_TEMPERATURE_C or (
        weather.feels_like_c or 0.0
    ) >= HEAT_FEELS_LIKE_C


def _capacity_window(
    label: str,
    value: int | None,
    healthy: bool,
    reset_at: datetime | None = None,
    timezone_name: str = "",
) -> dict[str, Any]:
    """One 5H or 7D reading: the percent numeral, or a hatch flag, and a
    caption beneath naming the window and (5H only) its reset time.

    The reset time is part of the label, not the numeral, so it survives an
    unavailable reading: a stale window still tells the owner when it clears.
    """
    available = healthy and value is not None
    window_label = f"{label} RESET {fmt_time(reset_at, timezone_name)}" if reset_at else label
    return {
        "label": window_label,
        "value": fmt_number(value, digits=0) if available else None,
        "accent": percent_accent(value, healthy),
        "available": available,
        # The used fraction the meter fills, not the remaining one.
        "fraction": max(0.0, min(1.0, (100 - value) / 100.0)) if available else 0.0,
    }


def ai_capacity_rows(state: DashboardState) -> list[dict[str, Any]]:
    """CLAUDE and CODEX, each with a 5H and a 7D reading, for the Today page."""
    if not state.ai_usage.usable:
        return []
    rows: list[dict[str, Any]] = []
    for provider in state.ai_usage.providers[:2]:
        healthy = provider.collection_status == "ok"
        rows.append(
            {
                "name": provider.provider.upper(),
                "windows": [
                    _capacity_window(
                        "5H",
                        provider.short_window_percent_remaining,
                        healthy,
                        provider.short_window_reset_at,
                        state.timezone,
                    ),
                    _capacity_window("7D", provider.weekly_percent_remaining, healthy),
                ],
            }
        )
    return rows


def percent_accent(value: int | None, healthy: bool) -> str:
    """Colour for a percentage that is running out.

    Never green: plenty of quota left is not news, and a page where the
    healthy case is coloured teaches the eye to ignore colour.
    """
    if not healthy or value is None:
        return "black"
    if value <= 15:
        return "red"
    if value <= 35:
        return "yellow"
    return "black"


def brief_note(state: DashboardState) -> dict[str, str]:
    brief = state.brief.brief
    if not state.brief.usable or brief is None:
        return {"text": f"AI brief {UNAVAILABLE}", "accent": "black"}
    text = brief.note or brief.headline
    if not text:
        return {"text": "AI brief has no note", "accent": "black"}
    return {"text": text, "accent": "black"}


def block_note(status: AdapterStatus, label: str) -> str:
    if status is AdapterStatus.OK:
        return ""
    if status is AdapterStatus.STALE:
        return f"{label}: showing last known data"
    if status is AdapterStatus.UNAVAILABLE:
        return f"{label} {UNAVAILABLE}"
    return f"{label} error"


#: The agenda's left column body height, matching the shared 372 px body.
AGENDA_LEFT_BODY_HEIGHT_PX: float = 372.0
#: "TODAY" plus the long date, one baseline-aligned row.
AGENDA_HEAD_HEIGHT_PX: float = 24.0
AGENDA_HEAD_GAP_PX: float = 8.0
#: One all-day row (hollow dot plus title) and the gap before the timed route.
AGENDA_ALLDAY_ROW_HEIGHT_PX: float = 22.0
AGENDA_ALLDAY_GAP_PX: float = 8.0
#: The minimum vertical clearance a label needs from the one stacked above it.
AGENDA_LABEL_ROW_HEIGHT_PX: float = 22.0
#: Calendar lines on the route sit this many px apart, left to right.
AGENDA_LINE_GAP_PX: float = 16.0

#: The "rough answer" strip's own span: 06:00 to 22:00, not the full day.
NEXT7_START_MIN: int = 6 * 60
NEXT7_END_MIN: int = 22 * 60
NEXT7_SPAN_MIN: int = NEXT7_END_MIN - NEXT7_START_MIN


def _event_end_minutes_same_day(event: Event) -> float:
    """Minutes-since-midnight of an event's end, clamped to 24:00 once the
    event's end falls on a later date than its start (it runs past midnight,
    an hour a plain "minutes since midnight" cannot itself represent)."""
    end = event.end
    if end is None:
        return _minutes_since_midnight(event.start)
    if end.date() > event.start.date():
        return float(SCALE_END_HOUR * 60)
    return _minutes_since_midnight(end)


def agenda_route(
    state: DashboardState, colors: dict[str, str], today: date, reference: datetime
) -> dict[str, Any]:
    """Geometry for the agenda's vertical route strip.

    One line per calendar with a timed event today, positioned left to right
    in the order its first event appears; a dot and a busy bar per event on
    its own line; labels stacked top to bottom, each pushed down only as far
    as it needs to clear :data:`AGENDA_LABEL_ROW_HEIGHT_PX` from the one
    above (two events on different calendars at the same time land on the
    same dot position and their labels stack automatically, which is the
    interchange the spec calls for, with no special case needed); and NOW.
    """
    events_today = (
        [event for event in state.calendar.items if event.start.date() == today]
        if state.calendar.usable
        else []
    )
    allday = [
        {"title": event.title, "color": event_color(event, colors)}
        for event in sorted((e for e in events_today if e.all_day), key=lambda e: e.title)
    ]
    timed = sorted((e for e in events_today if not e.all_day), key=lambda e: e.start)

    route_height = AGENDA_LEFT_BODY_HEIGHT_PX - AGENDA_HEAD_HEIGHT_PX - AGENDA_HEAD_GAP_PX
    if allday:
        route_height -= len(allday) * AGENDA_ALLDAY_ROW_HEIGHT_PX + AGENDA_ALLDAY_GAP_PX
    route_height = max(0.0, route_height)

    hours = [
        {
            "pct": scale_position(hour * 60),
            "label": f"{hour:02d}",
            # 24:00 keeps its tick but not its numeral: centered on the route
            # body's very last pixel, a printed "24" would drop its descent
            # past the body and into the footer rule below it.
            "labeled": hour in (6, 9, 12, 15, 18, 21),
        }
        for hour in range(SCALE_START_HOUR, SCALE_END_HOUR + 1)
    ]

    line_order: list[str] = []
    line_color: dict[str, str] = {}
    for event in timed:
        key = (event.calendar or "").lower()
        if key not in line_color:
            line_order.append(key)
            line_color[key] = event_color(event, colors)
    lines = [
        {"color": line_color[key], "x": index * AGENDA_LINE_GAP_PX}
        for index, key in enumerate(line_order)
    ]
    line_x = {key: index * AGENDA_LINE_GAP_PX for index, key in enumerate(line_order)}

    markers: list[dict[str, Any]] = []
    next_min_top = 0.0
    for event in timed:
        start_pct = scale_position(_minutes_since_midnight(event.start))
        end_pct = scale_position(_event_end_minutes_same_day(event))
        dot_top = start_pct / 100.0 * route_height
        end_top = end_pct / 100.0 * route_height
        label_top = max(dot_top, next_min_top)
        next_min_top = label_top + AGENDA_LABEL_ROW_HEIGHT_PX
        key = (event.calendar or "").lower()
        markers.append(
            {
                "x": line_x.get(key, 0.0),
                "color": event_color(event, colors),
                "dot_top": round(dot_top, 1),
                "bar_top": round(min(dot_top, end_top), 1),
                "bar_height": round(abs(end_top - dot_top), 1),
                "label_top": round(label_top, 1),
                "time": event.start.strftime("%H:%M"),
                "title": event.title,
            }
        )

    now_pct = scale_position(_minutes_since_midnight(reference))
    return {
        "allday": allday,
        "height_px": round(route_height, 1),
        "hours": hours,
        "lines": lines,
        "markers": markers,
        "now_pct": now_pct,
        "empty": not allday and not timed,
        "empty_top_px": round(scale_position(12 * 60) / 100.0 * route_height, 1),
    }


def month_grid(state: DashboardState, colors: dict[str, str], today: date) -> dict[str, Any]:
    """The current month as a Monday-first grid: blanks outside the month,
    today inverted, a dot under any day with at least one event (black when
    more than one calendar has an event that day)."""
    first = today.replace(day=1)
    next_first = (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )
    days_in_month = (next_first - first).days

    day_colors: dict[int, set[str]] = {}
    if state.calendar.usable:
        for event in state.calendar.items:
            day = event.start.date()
            if day.year == today.year and day.month == today.month:
                day_colors.setdefault(day.day, set()).add(event_color(event, colors))

    cells: list[dict[str, Any] | None] = [None] * first.weekday()  # Monday first
    for day_num in range(1, days_in_month + 1):
        present = day_colors.get(day_num, set())
        dot = "black" if len(present) > 1 else (next(iter(present)) if present else None)
        cells.append({"number": day_num, "is_today": day_num == today.day, "dot": dot})
    while len(cells) % 7 != 0:
        cells.append(None)

    return {
        "name": today.strftime("%B").upper(),
        "weeks": [cells[index : index + 7] for index in range(0, len(cells), 7)],
    }


def _next7_position(minutes: float) -> float:
    return max(0.0, min(100.0, (minutes - NEXT7_START_MIN) / NEXT7_SPAN_MIN * 100.0))


def next_seven_days(state: DashboardState, today: date) -> list[dict[str, Any]]:
    """The "rough answer" strip: one busy bar per day, 06:00-22:00, all-day
    events filling the whole bar; the event count blank at zero."""
    events = state.calendar.items if state.calendar.usable else []
    rows: list[dict[str, Any]] = []
    for offset in range(1, 8):
        day = today + timedelta(days=offset)
        day_events = [event for event in events if event.start.date() == day]
        segments: list[dict[str, float]] = []
        for event in day_events:
            if event.all_day:
                segments.append({"left": 0.0, "width": 100.0})
                continue
            start_pct = _next7_position(_minutes_since_midnight(event.start))
            end_pct = _next7_position(_event_end_minutes_same_day(event))
            if end_pct > start_pct:
                segments.append({"left": round(start_pct, 2), "width": round(end_pct - start_pct, 2)})
        rows.append(
            {
                "label": f"{day.strftime('%a').upper()} {day.strftime('%d')}",
                "segments": segments,
                "count": len(day_events) or None,
            }
        )
    return rows


def agenda_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "agenda")
    today: date = context["today"]
    reference: datetime = context["reference"]
    colors = calendar_colors(state, settings)

    context["long_date"] = reference.strftime("%A %d %B").upper()
    context["route"] = agenda_route(state, colors, today, reference)
    context["month"] = month_grid(state, colors, today)
    context["next7"] = next_seven_days(state, today)
    context["calendar_note"] = block_note(state.calendar.status, "calendar")
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    return context


def weather_summary(weather: Weather | None) -> dict[str, Any]:
    """The hero reading: temperature, glyph, condition word, feels-like, and
    the one tell-tale the owner needs before reading anything else.

    Heat outranks rain, same ordering as :func:`header_weather`: a 40 degree
    feel is the thing to know first. A dry, cool day gets no dot at all.
    """
    if weather is None:
        return {"available": False}
    if is_heat(weather):
        dot = "red"
    elif (weather.rain_probability_percent or 0) >= 50 or weather.rain_from:
        dot = "blue"
    else:
        dot = ""
    return {
        "available": True,
        "temp": fmt_number(weather.temperature_c),
        "icon": icons.weather_icon(weather.condition),
        "condition": weather.condition.upper(),
        "feels": fmt_number(weather.feels_like_c),
        "dot": dot,
    }


def weather_readings(weather: Weather | None) -> list[dict[str, Any]]:
    """HUMIDITY, UV, AQI, RAIN: the four readings under the hero.

    Each carries its own tell-tale rule (UV only warns at yellow or red so a
    healthy reading stays quiet; AQI tells at every state including green
    because clean air is itself worth a glance at a glance). A missing value
    is ``None``, never a guessed zero: the template draws the hatch box for
    that in place of the numeral.
    """
    if weather is None:
        return [
            {"label": "HUMIDITY", "value": None, "dot": ""},
            {"label": "UV", "value": None, "dot": ""},
            {"label": "AQI", "value": None, "dot": ""},
            {"label": "RAIN", "value": None, "dot": ""},
        ]
    humidity = weather.humidity_percent
    uv = weather.uv_index
    aqi = weather.aqi
    rain = weather.rain_probability_percent
    uv_dot = uv_accent(uv)
    aqi_dot = aqi_accent(aqi)
    return [
        {"label": "HUMIDITY", "value": None if humidity is None else str(humidity), "dot": ""},
        {
            "label": "UV",
            "value": None if uv is None else f"{uv:.1f}",
            "dot": uv_dot if uv_dot in ("red", "yellow") else "",
        },
        {
            "label": "AQI",
            "value": None if aqi is None else str(aqi),
            "dot": aqi_dot if aqi_dot != "black" else "",
        },
        {
            "label": "RAIN",
            "value": None if rain is None else f"{rain}%",
            "dot": "blue" if (rain or 0) >= 50 else "",
        },
    ]


def weather_hourly_plates(
    weather: Weather | None, reference: datetime, limit: int = 6
) -> list[dict[str, Any]]:
    """Up to ``limit`` hourly rain plates from the current hour onward."""
    if weather is None or not weather.hourly_rain:
        return []
    hour_start = reference.replace(minute=0, second=0, microsecond=0)
    upcoming = sorted(
        (item for item in weather.hourly_rain if item.at >= hour_start),
        key=lambda item: item.at,
    )
    return [
        {
            "hour": item.at.strftime("%H"),
            "percent": item.probability_percent,
            "mm": item.precipitation_mm,
            "fill": item.probability_percent >= 50,
        }
        for item in upcoming[:limit]
    ]


def weather_daily_rows(weather: Weather | None, today: date, limit: int = 7) -> list[dict[str, Any]]:
    """Up to ``limit`` days for the 7 DAYS pane, fixed-scale rain plates."""
    if weather is None:
        return []
    rows: list[dict[str, Any]] = []
    for item in weather.daily[:limit]:
        rain = item.rain_probability_percent
        rows.append(
            {
                "label": strip_day_label(item.day, today),
                "icon": icons.weather_icon(item.condition),
                "high": fmt_number(item.high_c) if item.high_c is not None else None,
                "low": fmt_number(item.low_c) if item.low_c is not None else None,
                "rain_percent": rain,
                "rain_fill": (rain or 0) >= 50,
            }
        )
    return rows


def weather_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "weather")
    weather = state.weather.weather if state.weather.usable else None
    reference: datetime = context["reference"]
    today: date = context["today"]

    context["weather_note"] = block_note(state.weather.status, "weather")
    context["hero"] = weather_summary(weather)
    context["readings"] = weather_readings(weather)
    context["hourly"] = weather_hourly_plates(weather, reference)
    context["hourly_span"] = (
        f"{context['hourly'][0]['hour']}:00 TO {context['hourly'][-1]['hour']}:00"
        if context["hourly"]
        else ""
    )
    context["daily"] = weather_daily_rows(weather, today)
    return context


def strip_day_label(day: date, today: date) -> str:
    """Day label for the five column forecast strip.

    Weekday names throughout, because a column is about 73 px wide inside its
    padding and TOMORROW needs 122 px at the 18 px legibility floor. Inventing
    a shorter word for it would put a second abbreviation in the world for no
    reason, so only TODAY, which fits, gets a relative name here. The agenda
    has the room and keeps TODAY and TOMORROW.
    """
    return "TODAY" if day == today else day.strftime("%a").upper()


def uv_accent(value: float | None) -> str:
    if value is None:
        return "black"
    if value >= 8:
        return "red"
    if value >= 6:
        return "yellow"
    return "green"


def pm25_accent(value: float | None) -> str:
    if value is None:
        return "black"
    if value >= 55:
        return "red"
    if value >= 25:
        return "yellow"
    return "green"


def aqi_accent(value: int | None) -> str:
    if value is None:
        return "black"
    if value > 150:
        return "red"
    if value > 50:
        return "yellow"
    return "green"


#: Running-text lines in the brief's left column are a flat list: a section
#: header and each of its bullet items are the same height, so "how much
#: fits" is just how many of these lines the column can hold.
BRIEF_LINE_PX: float = 26.0
#: The 372 px body minus the mode/generated line, the two-line headline and
#: the rule between them, measured against brief.html's own row heights.
BRIEF_BODY_BUDGET_PX: float = 242.0
BRIEF_MAX_LINES: int = int(BRIEF_BODY_BUDGET_PX // BRIEF_LINE_PX)
#: Task rows are 32 px each; "about 9" is what the spec asks for and what the
#: right column actually holds under its own label.
BRIEF_TASK_ROWS: int = 9
#: Shown in place of the running text when the PC has never sent a brief.
BRIEF_UNAVAILABLE_MESSAGE: str = "No brief from the PC yet"


def brief_is_risk(title: str) -> bool:
    """A section heading belongs to the existing risk keyword group."""
    return icons.brief_accent(title) == "red"


def brief_lines(
    sections: list[dict[str, Any]], max_lines: int = BRIEF_MAX_LINES
) -> tuple[list[dict[str, Any]], bool]:
    """Flatten the brief's sections into running-text lines, clipped to fit.

    A section header and each bullet item are one line each, so the column is
    just a list of lines to slice. The cut never lands on a bare header (there
    is nothing to read under it), and the last item actually shown is marked
    so the template can end the column in an ellipsis rather than stopping
    mid-thought with no sign that more was cut.
    """
    lines: list[dict[str, Any]] = []
    for section in sections:
        lines.append({"kind": "header", "title": section["title"], "risk": section["risk"]})
        for item in section["items"]:
            lines.append({"kind": "item", "text": item})
    if len(lines) <= max_lines:
        return lines, False
    visible = lines[:max_lines]
    while visible and visible[-1]["kind"] == "header":
        visible.pop()
    if visible and visible[-1]["kind"] == "item":
        visible[-1] = {"kind": "item", "text": f"{visible[-1]['text'].rstrip()} ..."}
    return visible, True


def brief_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "brief")
    today: date = context["today"]
    brief = state.brief.brief
    context["brief_note"] = block_note(state.brief.status, "AI brief")
    context["brief_available"] = state.brief.usable and brief is not None
    context["unavailable_message"] = BRIEF_UNAVAILABLE_MESSAGE
    if context["brief_available"]:
        context["mode_label"] = f"{brief.mode.value.upper()} BRIEF"
        context["generated_label"] = (
            f"GENERATED {fmt_time(brief.generated_at, state.timezone)}"
            if brief.generated_at is not None
            else "NOT GENERATED"
        )
        context["headline"] = brief.headline or "No headline"
        sections = [
            {
                "title": section.title.upper(),
                "risk": brief_is_risk(section.title),
                "items": section.items,
            }
            for section in brief.sections
        ]
        context["lines"], context["truncated"] = brief_lines(sections)
    else:
        context["mode_label"] = "BRIEF"
        context["generated_label"] = ""
        context["headline"] = ""
        context["lines"] = []
        context["truncated"] = False

    context["tasks"] = priority_tasks(state, today, BRIEF_TASK_ROWS) if state.tasks.usable else []
    context["tasks_open_count"] = len(open_tasks(state)) if state.tasks.usable else 0
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    return context


def sensor_value(value: str | None, unit: str | None) -> str:
    """Join a sensor reading and its unit without inventing a value."""
    if value is None:
        return UNKNOWN
    if not unit:
        return value
    if unit in ("%",):
        return f"{value}{unit}"
    return f"{value} {unit}"


#: The DESK panel owns temperature and humidity now, so the Home Assistant
#: room rows would only repeat it (usually from a sensor in another room).
DESK_OWNED_SLOTS: frozenset[str] = frozenset({"room_temperature", "room_humidity"})

#: Sensor and service rows are 28 px each under their own label, in a column
#: about 170 px tall; this is roughly what fits without a scrollbar the panel
#: does not have.
SYSTEM_ROW_LIMIT: int = 5

NO_DEVICE_DATA = "NO DEVICE DATA YET"
#: The device is reporting, it just has not reported long enough to plot.
NO_DEVICE_HISTORY = "NOT ENOUGH HISTORY YET"


def battery_accent(level: float | None) -> str:
    """Colour for the BATTERY meter fill: plain ink until it is nearly flat.

    Same 10/20 thresholds as the header's chip: a mid-range charge is not
    news, so only the stretch right before the device actually goes flat
    earns a colour. The meter never turns green; a level is not itself a
    "healthy" state the way a service or a sensor is.
    """
    if level is None:
        return "black"
    if level <= 10:
        return "red"
    if level <= 20:
        return "yellow"
    return "black"


def wifi_accent(rssi: float | None) -> str:
    if rssi is None:
        return "black"
    if rssi >= -67:
        return "green"
    if rssi >= -80:
        return "yellow"
    return "red"


def power_label(usb_present: bool | None, charge_state: str | None) -> str | None:
    """The single caps word printed under the BATTERY meter.

    Only the three states the device firmware can actually distinguish are
    named; anything else (older firmware that never sends the fields, or a
    charge state the gauge itself calls "unknown") prints nothing rather than
    guessing.
    """
    if usb_present is False:
        return "BATTERY"
    if usb_present is True and charge_state == "charging":
        return "CHARGING"
    if usb_present is True and charge_state == "charged":
        return "USB"
    return None


def age_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return UNKNOWN.upper()
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "NOW"
    if minutes < 60:
        return f"{minutes} MIN AGO"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} H AGO"
    return f"{hours // 24} D AGO"


def wake_label(wake_cause: str | None) -> str:
    """The LAST WAKE reading: the firmware's own word, spaced out and capped."""
    if not wake_cause:
        return UNKNOWN.upper()
    return wake_cause.replace("_", " ").upper()


def device_panel(state: DashboardState, settings: Settings) -> dict[str, Any]:
    """Everything the DESK instrument cluster and its chart draw."""
    device: DeviceState | None = state.device.device
    if not state.device.usable or device is None or not device.has_reading:
        return {
            "available": False,
            "chart": build_chart([], state.timezone, note=NO_DEVICE_DATA),
        }
    level = device.battery_level
    charging = device.charge_state == "charging"
    hours = None if device.uptime_s is None else device.uptime_s / 3600.0
    return {
        "available": True,
        "battery_level": level,
        "battery_text": fmt_number(level, digits=0),
        "battery_available": level is not None,
        "battery_fraction": 0.0 if level is None else max(0.0, min(1.0, level / 100.0)),
        "battery_accent": battery_accent(level),
        "battery_icon": icons.battery_icon(level, charging),
        "power_word": power_label(device.usb_present, device.charge_state),
        "stale": device.status is not DeviceStatus.OK,
        "age_text": age_label(device.age_seconds),
        "temperature_text": fmt_number(device.temperature, digits=1),
        "humidity_text": fmt_number(device.humidity, digits=0),
        "wifi_icon": icons.wifi_icon(device.wifi_rssi),
        "wifi_text": fmt_number(device.wifi_rssi, digits=0),
        "uptime_text": fmt_number(hours, digits=0),
        "wake_text": wake_label(device.wake_cause),
        "chart": build_chart(device.history_24h, state.timezone, note=NO_DEVICE_HISTORY),
    }


def desk_accent(state: DashboardState) -> str:
    """Whether the DESK row's stale tell-tale should show at all."""
    device: DeviceState | None = state.device.device
    if not state.device.usable or device is None or not device.has_reading:
        return "black"
    return "black" if device.status is DeviceStatus.OK else "yellow"


def sensor_accent(severity: str) -> str:
    """Tell-tale colour before a HOME sensor's name.

    Only a state worth a second look earns a dot: "ok" and "unknown" print
    the name in plain black, same as every other quiet row on the panel.
    """
    return {"warn": "yellow", "alert": "red"}.get(severity, "")


def service_mark(health: str) -> str:
    """Colour of the 12 px square before a SERVICES row's name.

    An unrecognised health string draws a hatch square rather than guessing a
    colour for a state nobody named.
    """
    return {"ok": "black", "warn": "yellow", "down": "red"}.get(health, "hatch")


def system_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "system")
    context["home_note"] = block_note(state.home.status, "home")
    context["device"] = device_panel(state, settings)

    home = state.home.home
    if not state.home.usable or home is None:
        context["sensors"] = []
        context["services"] = []
        return context

    context["sensors"] = [
        {
            "name": sensor.name.upper(),
            "value": sensor_value(sensor.value, sensor.unit),
            "available": sensor.value is not None,
            "accent": sensor_accent(sensor.severity),
        }
        for sensor in home.sensors
        if sensor.key not in DESK_OWNED_SLOTS
    ][:SYSTEM_ROW_LIMIT]
    context["services"] = [
        {
            "name": service.name.upper(),
            "mark": service_mark(service.health.value),
            "down": service.health.value == "down",
        }
        for service in home.services
    ][:SYSTEM_ROW_LIMIT]
    return context


def alert_priority_word(priority: AlertPriority) -> str:
    return priority.value.upper()


#: The alert page is the one page a colour may own a whole region, because
#: the page itself is a state. Only the three colours this world allows for a
#: state band: red for the two priorities that should interrupt whatever the
#: owner is doing, yellow for one that is merely worth noticing, black for
#: the default. Blue is a calendar's own colour and rain's; it never means an
#: alert.
ALERT_BAND_ACCENT: dict[AlertPriority, str] = {
    AlertPriority.CRITICAL: "red",
    AlertPriority.DOORBELL: "red",
    AlertPriority.IMPORTANT: "yellow",
    AlertPriority.NORMAL: "black",
}


def alert_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "alert")
    alert: Alert | None = state.alert
    context["alert"] = alert
    if alert is None:
        context["band_label"] = "ALERT"
        context["band_right"] = context["header"]["clock"]
        context["band_accent"] = "black"
        context["title"] = "NO ACTIVE ALERT"
        context["message"] = "The hub has nothing to show right now."
        return context
    context["band_label"] = alert.source.upper() if alert.source else "ALERT"
    time_text = to_local(alert.created_at, state.timezone).strftime("%H:%M")
    context["band_right"] = f"{alert_priority_word(alert.priority)} {time_text}"
    context["band_accent"] = ALERT_BAND_ACCENT[alert.priority]
    context["title"] = alert.title.upper()
    context["message"] = alert.message
    return context


CONTEXT_BUILDERS = {
    "today": today_context,
    "agenda": agenda_context,
    "weather": weather_context,
    "brief": brief_context,
    "system": system_context,
    "alert": alert_context,
}


def build_context(page: str, state: DashboardState, settings: Settings) -> dict[str, Any]:
    builder = CONTEXT_BUILDERS.get(page)
    if builder is None:
        raise KeyError(f"unknown page {page!r}")
    return builder(state, settings)
