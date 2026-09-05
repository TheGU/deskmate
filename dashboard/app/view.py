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

#: Today's left column content width in px (its 456 px flex-basis minus its
#: own 16 px right padding). Event dots place by percent, same as everything
#: else on the scale, but a label is wide text, not a point, so keeping it
#: on the page at all needs a real pixel width.
SCALE_WIDTH_PX: float = 440.0
#: Rough per-glyph advance for 20 px Google Sans, bold for the time and
#: regular for the title, used only to keep a label's estimated box on the
#: scale and off its neighbours; it does not have to be exact.
LABEL_TIME_CHAR_PX: float = 12.5
LABEL_TITLE_CHAR_PX: float = 10.0
LABEL_GAP_PX: float = 10.0
#: The minimum clearance the "12 px apart" rule asks for between one label's
#: estimated box and the next.
LABEL_COLLIDE_PX: float = 12.0


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


def _on_scale(event: Event, today: date, reference: datetime) -> bool:
    """An event today, at or after 06:00, that has not already finished sits
    on the scale; anything earlier, already over, or all-day (which has no
    single time to place) does not. The scale looks forward, same as the
    rows beneath it."""
    return (
        event.start.date() == today
        and not event.all_day
        and event.start.hour >= SCALE_START_HOUR
        and _event_end(event) >= reference
    )


def _label_width(time_text: str, title: str) -> float:
    """Estimated pixel width of one scale label: bold time, regular title."""
    return len(time_text) * LABEL_TIME_CHAR_PX + LABEL_GAP_PX + len(title) * LABEL_TITLE_CHAR_PX


def today_scale(
    state: DashboardState, colors: dict[str, str], today: date, reference: datetime
) -> dict[str, Any]:
    """Geometry for the day scale: hour ticks, the NOW marker, event dots.

    Dot positions are percent of the 06:00-24:00 span, computed here so the
    template only pastes numbers into ``style="left:...%"``; the panel has no
    JavaScript to lay anything out itself. A label is wide text rather than a
    point, so its left edge is a clamped pixel offset instead, keeping it on
    the scale even when its dot sits right at either end, and a label only
    drops to the next line when its estimated box would otherwise run into
    the one before it.
    """
    hours = [
        {
            "pct": (hour - SCALE_START_HOUR) / (SCALE_END_HOUR - SCALE_START_HOUR) * 100.0,
            "tall": hour % 6 == 0,
            "label": f"{hour:02d}",
        }
        for hour in range(SCALE_START_HOUR, SCALE_END_HOUR + 1)
    ]

    markers: list[dict[str, Any]] = []
    if state.calendar.usable:
        events = sorted(
            (event for event in state.calendar.items if _on_scale(event, today, reference)),
            key=lambda event: event.start,
        )
        widths: list[float] = []
        for event in events:
            pct = scale_position(_minutes_since_midnight(event.start))
            dot_px = pct / 100.0 * SCALE_WIDTH_PX
            time_text = event.start.strftime("%H:%M")
            width = _label_width(time_text, event.title)
            left_px = max(0.0, min(SCALE_WIDTH_PX - width, dot_px - width / 2.0))
            widths.append(width)
            markers.append(
                {
                    "pct": pct,
                    "color": event_color(event, colors),
                    "time": time_text,
                    "title": event.title,
                    "label_left": round(left_px, 1),
                    "row": 0,
                }
            )
        # Greedy row assignment: a label goes on the lowest row whose last
        # occupant it clears by the collision distance: usually row 0, and
        # only the crowded case ever needs a second line.
        row_right_edge: list[float] = []
        for marker, width in zip(markers, widths):
            left = marker["label_left"]
            placed = False
            for row_index, right_edge in enumerate(row_right_edge):
                if left >= right_edge + LABEL_COLLIDE_PX:
                    marker["row"] = row_index
                    row_right_edge[row_index] = left + width
                    placed = True
                    break
            if not placed:
                marker["row"] = len(row_right_edge)
                row_right_edge.append(left + width)

    now_pct = scale_position(_minutes_since_midnight(reference))
    return {"now_pct": now_pct, "hours": hours, "markers": markers}


def today_next_rows(
    state: DashboardState, reference: datetime, colors: dict[str, str], limit: int = 2
) -> list[dict[str, Any]]:
    """Up to two events after the ones already placed on the day scale.

    An event today before 06:00 never gets a marker, so it still has to be
    named here rather than disappearing from the page entirely.
    """
    if not state.calendar.usable:
        return []
    today = reference.date()
    events = sorted(
        (event for event in state.calendar.items if _event_end(event) >= reference),
        key=lambda event: event.start,
    )
    rows: list[dict[str, Any]] = []
    for event in events:
        if _on_scale(event, today, reference):
            continue
        day = event.start.date()
        if day == today:
            when = "ALL DAY" if event.all_day else event.start.strftime("%H:%M")
        elif (day - today).days == 1:
            when = "TOMORROW" if event.all_day else f"TOMORROW {event.start.strftime('%H:%M')}"
        else:
            prefix = day.strftime("%a").upper()
            when = prefix if event.all_day else f"{prefix} {event.start.strftime('%H:%M')}"
        rows.append({"when": when, "title": event.title, "color": event_color(event, colors)})
        if len(rows) >= limit:
            break
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


def device_badge(state: DashboardState) -> dict[str, Any]:
    """Battery and Wi-Fi as the top bar and the window list bar need them.

    Every page carries these, not just System: the paper can be on battery
    anywhere, and a flat device is the one thing that stops all six pages.
    """
    device: DeviceState | None = state.device.device
    if not state.device.usable or device is None or not device.has_reading:
        return {
            "available": False,
            "battery_percent": "",
            "battery_accent": "black",
            "battery_icon": icons.BATTERY,
            "battery_level": None,
            "battery_cells": meter_cells(None),
            "charging": False,
            "wifi": "",
            "wifi_short": "",
            "wifi_accent": "black",
        }
    level = device.battery_level
    charging = device.charge_state == "charging"
    return {
        "available": True,
        "battery_percent": fmt_number(level, digits=0, suffix="%"),
        "battery_accent": battery_accent(level),
        "battery_icon": icons.battery_icon(level, charging),
        "battery_level": level,
        "battery_cells": meter_cells(level),
        "charging": charging,
        "wifi": fmt_number(device.wifi_rssi, digits=0, suffix=" dBm"),
        # The window list bar has no room for the unit and the glyph beside it
        # already says what the number is.
        "wifi_short": fmt_number(device.wifi_rssi, digits=0),
        "wifi_accent": wifi_accent(device.wifi_rssi),
    }


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
        priority_tasks(state, today, settings.max_priority_tasks)
        if state.tasks.usable
        else []
    )
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    context["scale"] = today_scale(state, colors, today, reference)
    context["next_events"] = today_next_rows(state, reference, colors, limit=2)
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


def _capacity_window(label: str, value: int | None, healthy: bool) -> dict[str, Any]:
    """One 5H or 7D reading: a bare numeral (no percent sign) or a hatch flag."""
    available = healthy and value is not None
    return {
        "label": label,
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
                    _capacity_window("5H", provider.short_window_percent_remaining, healthy),
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


def agenda_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "agenda")
    today: date = context["today"]
    tz = zone(state.timezone)

    colors = calendar_colors(state, settings)
    days: list[dict[str, Any]] = []
    for offset in range(settings.agenda_days):
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        events = [
            {
                "when": "ALL DAY" if event.all_day else event.start.strftime("%H:%M"),
                "title": event.title,
                "color": event_color(event, colors),
            }
            for event in sorted(state.calendar.items, key=lambda item: item.start)
            if start <= event.start < end
        ]
        due = [
            {"title": task.title, "priority": task.priority.value}
            for task in sorted(open_tasks(state), key=lambda item: item.title)
            if task.due == day
        ]
        label = relative_day_label(day, today)
        days.append(
            {
                "day": day,
                "label": label,
                "date_label": day.strftime("%d %b").upper(),
                # The chip already spells the weekday, so the label beside it
                # only earns its space while it says something else.
                "chip_label": day.strftime("%a %d").upper(),
                "head_label": label if label in ("TODAY", "TOMORROW") else "",
                "events": events,
                "due": due,
                "empty": not events and not due,
            }
        )

    context["days"] = days
    context["overdue"] = [
        {"title": task.title, "label": due_label(task.due, today)}
        for task in overdue_tasks(state, today)
    ]
    context["calendar_note"] = block_note(state.calendar.status, "calendar")
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    context["title_accents"] = {"days": "black", "then": "black"}
    return context


def weather_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "weather")
    weather = state.weather.weather
    context["weather_note"] = block_note(state.weather.status, "weather")
    if not state.weather.usable or weather is None:
        context["weather"] = None
        context["daily"] = []
        context["metrics"] = []
        context["location"] = UNKNOWN
        context["condition_icon"] = icons.WEATHER_PARTLY_CLOUDY
        context["title_accents"] = {"weather": "black"}
        return context

    context["weather"] = weather
    context["location"] = weather.location_name or UNKNOWN
    # The page's own glyph is the sky it is describing.
    context["condition_icon"] = icons.weather_icon(weather.condition)
    context["temp"] = fmt_number(weather.temperature_c)
    context["feels"] = fmt_number(weather.feels_like_c)
    context["condition"] = weather.condition.upper()

    today = context["today"]
    today_row = next((item for item in weather.daily if item.day == today), None)
    context["high"] = fmt_number(today_row.high_c) if today_row else "--"
    context["low"] = fmt_number(today_row.low_c) if today_row else "--"

    if weather.rain_from and weather.rain_until and weather.rain_from != weather.rain_until:
        rain_timing = f"{weather.rain_from} TO {weather.rain_until}"
    elif weather.rain_from:
        rain_timing = f"FROM {weather.rain_from}"
    else:
        rain_timing = "NONE EXPECTED"
    context["rain_timing"] = rain_timing
    context["rain_probability"] = fmt_percent(weather.rain_probability_percent)
    context["rain_accent"] = (
        "blue"
        if (weather.rain_probability_percent or 0) >= 50
        else "black"
    )

    # Labels are one short word: the cells are about 96 px wide and the type
    # floor is 20 px, so HUMIDITY does not fit and the glyph carries the rest.
    context["metrics"] = [
        {
            "label": "HUMID",
            "icon": icons.WATER_PERCENT,
            "value": fmt_percent(weather.humidity_percent),
            "accent": "black",
        },
        {
            "label": "UV",
            "icon": icons.WHITE_BALANCE_SUNNY,
            "value": fmt_number(weather.uv_index, digits=1),
            "accent": uv_accent(weather.uv_index),
        },
        {
            "label": "PM2.5",
            "icon": icons.SMOG,
            "value": fmt_number(weather.pm2_5, digits=0),
            "accent": pm25_accent(weather.pm2_5),
        },
        {
            # The qualitative word would not fit the cell and the accent bar
            # under the number already carries it.
            "label": "AQI",
            "icon": icons.GAUGE,
            "value": fmt_number(weather.aqi),
            "accent": aqi_accent(weather.aqi),
        },
    ]

    context["daily"] = [
        {
            "label": strip_day_label(item.day, today),
            "date_label": item.day.strftime("%d %b").upper(),
            "high": fmt_number(item.high_c),
            "low": fmt_number(item.low_c),
            "rain": fmt_percent(item.rain_probability_percent),
            "accent": "blue" if (item.rain_probability_percent or 0) >= 50 else "black",
        }
        for item in weather.daily[:5]
    ]
    air = worst_accent(metric["accent"] for metric in context["metrics"])
    context["title_accents"] = {
        # Heat is the state the NOW pane can carry; the rest is just weather.
        "now": "red" if is_heat(weather) else "black",
        "rain": context["rain_accent"],
        # Green air is not news, so a clean day leaves the bar black.
        "air": air if air in ("red", "yellow") else "black",
        "days": "black",
    }
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


def brief_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "brief")
    brief = state.brief.brief
    context["brief_note"] = block_note(state.brief.status, "AI brief")
    if not state.brief.usable or brief is None:
        context["brief"] = None
        context["sections"] = []
        context["mode_label"] = UNKNOWN.upper()
        context["headline"] = f"AI brief {UNAVAILABLE}"
        context["generated_label"] = UNKNOWN
        context["title_accents"] = {"brief": "black"}
        return context
    context["brief"] = brief
    context["mode_label"] = brief.mode.value.upper()
    context["headline"] = brief.headline or "No headline"
    context["generated_label"] = fmt_time(brief.generated_at, state.timezone)
    # "lines", not "items": a Jinja dict lookup would hit dict.items instead.
    # Only a section that names a risk earns a colour, and only when it has
    # something in it. Everything else is a heading, not a state.
    context["sections"] = [
        {
            "title": section.title.upper(),
            "icon": icons.brief_icon(section.title),
            "accent": (
                "red"
                if icons.brief_accent(section.title) == "red" and section.items
                else "black"
            ),
            "lines": section.items[:4],
        }
        for section in brief.sections[:4]
    ]
    context["title_accents"] = {"brief": "black"}
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

NO_DEVICE_DATA = "NO DEVICE DATA YET"
#: The device is reporting, it just has not reported long enough to plot.
NO_DEVICE_HISTORY = "NOT ENOUGH HISTORY YET"


def battery_accent(level: float | None) -> str:
    if level is None:
        return "black"
    if level <= 15:
        return "red"
    if level <= 35:
        return "yellow"
    return "green"


def wifi_accent(rssi: float | None) -> str:
    if rssi is None:
        return "black"
    if rssi >= -67:
        return "green"
    if rssi >= -80:
        return "yellow"
    return "red"


def power_label(usb_present: bool | None, charge_state: str | None) -> tuple[str | None, str | None]:
    """Label and chip accent for the DESK power row.

    Only the three states the device firmware can actually distinguish are
    named; anything else (older firmware that never sends the fields, or a
    charge state the gauge itself calls "unknown") prints nothing rather than
    guessing.
    """
    if usb_present is False:
        return "ON BATTERY", "yellow"
    if usb_present is True and charge_state == "charging":
        return "ON USB, CHARGING", "green"
    if usb_present is True and charge_state == "charged":
        return "ON USB, CHARGED", "green"
    return None, None


def age_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return UNKNOWN.upper()
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "NOW"
    if minutes < 60:
        return f"{minutes}M AGO"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}H AGO"
    return f"{hours // 24}D AGO"


def device_panel(state: DashboardState, settings: Settings) -> dict[str, Any]:
    """Everything the DESK panel draws, including the chart geometry."""
    device: DeviceState | None = state.device.device
    badge = device_badge(state)
    if not state.device.usable or device is None or not device.has_reading:
        return {
            **badge,
            "note": block_note(state.device.status, "device") or NO_DEVICE_DATA,
            "empty_label": NO_DEVICE_DATA,
            "chart": build_chart([], state.timezone, note=NO_DEVICE_DATA),
        }
    level = device.battery_level
    power_text, power_accent = power_label(device.usb_present, device.charge_state)
    return {
        **badge,
        "name": (device.device or UNKNOWN).upper(),
        "temperature": fmt_number(device.temperature, digits=1),
        "humidity": fmt_number(device.humidity, digits=0, suffix="%"),
        "power_label": power_text,
        "power_accent": power_accent,
        "power_icon": (
            icons.POWER_PLUG_OFF if device.usb_present is False else icons.USB
        ),
        # Clamped only for the bar width; the printed number stays as reported.
        "battery_fill": 0 if level is None else int(max(0.0, min(100.0, level))),
        "status_label": (
            age_label(device.age_seconds)
            if device.status is DeviceStatus.OK
            else f"STALE {age_label(device.age_seconds)}"
        ),
        "status_accent": "black" if device.status is DeviceStatus.OK else "yellow",
        "empty_label": NO_DEVICE_DATA,
        "note": block_note(state.device.status, "device"),
        "chart": build_chart(device.history_24h, state.timezone, note=NO_DEVICE_HISTORY),
    }


def desk_accent(state: DashboardState) -> str:
    """DESK title bar: yellow once the paper stops reporting, else neutral."""
    device: DeviceState | None = state.device.device
    if not state.device.usable or device is None or not device.has_reading:
        return "black"
    return "black" if device.status is DeviceStatus.OK else "yellow"


def system_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "system")
    home = state.home.home
    context["home_note"] = block_note(state.home.status, "home")
    context["device"] = device_panel(state, settings)
    context["title_accents"] = {"desk": desk_accent(state), "home": "black", "services": "black"}
    if not state.home.usable or home is None:
        context["sensors"] = []
        context["services"] = []
        return context
    context["sensors"] = [
        {
            "name": sensor.name.upper(),
            "icon": icons.sensor_icon(sensor.key),
            "value": sensor_value(sensor.value, sensor.unit),
            "accent": {"ok": "green", "warn": "yellow", "alert": "red"}.get(
                sensor.severity, "black"
            ),
        }
        for sensor in home.sensors
        if sensor.key not in DESK_OWNED_SLOTS
    ]
    context["services"] = [
        {
            "name": service.name.upper(),
            "detail": service.detail or UNKNOWN,
            "accent": {"ok": "green", "warn": "yellow", "down": "red"}.get(
                service.health.value, "black"
            ),
            "icon": icons.health_icon(service.health.value),
            "mark": {"ok": "OK", "warn": "WARN", "down": "DOWN"}.get(
                service.health.value, "UNKNOWN"
            ),
        }
        for service in home.services
    ]
    # Green is the quiet default up here: a bar only lights for a warning.
    home = worst_accent(sensor["accent"] for sensor in context["sensors"])
    services = worst_accent(service["accent"] for service in context["services"])
    context["title_accents"]["home"] = home if home in ("red", "yellow") else "black"
    context["title_accents"]["services"] = (
        services if services in ("red", "yellow") else "black"
    )
    return context


def alert_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "alert")
    alert: Alert | None = state.alert
    context["alert"] = alert
    if alert is None:
        context["alert_bar_label"] = ""
        context["alert_title"] = "NO ACTIVE ALERT"
        context["alert_message"] = "The hub has nothing to show right now."
        context["alert_time"] = context["header"]["clock"]
        context["alert_accent"] = "black"
        return context
    context["alert_title"] = alert.title.upper()
    # What the bar says, best first: where the alert came from, else the
    # priority class, and nothing at all when that would only repeat the
    # title. It is never the product name over its own heading.
    priority_label = alert.priority.value.upper()
    if alert.source:
        bar_label = alert.source.upper()
    elif priority_label != context["alert_title"]:
        bar_label = priority_label
    else:
        bar_label = ""
    context["alert_bar_label"] = bar_label
    context["alert_message"] = alert.message
    context["alert_time"] = to_local(alert.created_at, state.timezone).strftime("%H:%M")
    accent = {
        AlertPriority.CRITICAL: "red",
        AlertPriority.DOORBELL: "red",
        AlertPriority.IMPORTANT: "yellow",
        AlertPriority.NORMAL: "blue",
    }[alert.priority]
    context["alert_accent"] = accent
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
