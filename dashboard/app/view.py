"""Turns :class:`DashboardState` into the flat context each template needs.

Templates stay dumb: no adapter knowledge, no arithmetic, no fallbacks. Every
"unknown" decision is made here, once.
"""

from __future__ import annotations

import re
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


#: The ellipsis :func:`clip_words` appends: one real character, never the
#: three ASCII dots CSS ``text-overflow: ellipsis`` draws.
ELLIPSIS = "…"


def clip_words(text: str, budget_chars: int) -> str:
    """Clip ``text`` to ``budget_chars``, never cutting inside a word.

    Returns ``text`` unchanged when it already fits. Otherwise returns the
    longest whole-word prefix that still fits the budget with a trailing
    :data:`ELLIPSIS`, so a title reads "Send vendor quote..." rather than
    stemming mid-word ("Send vendor quote answ...").

    A single word (or a Thai phrase, which carries no spaces to break a line
    on) longer than the whole budget is the one exception: there is no word
    boundary to honour, so it is cut at the budget instead, exactly where
    ``text-overflow: ellipsis`` would have cut it.
    """
    if len(text) <= budget_chars:
        return text
    words = text.split(" ")
    kept: list[str] = []
    length = 0
    for word in words:
        addition = len(word) + (1 if kept else 0)
        if length + addition + len(ELLIPSIS) > budget_chars:
            break
        kept.append(word)
        length += addition
    if not kept:
        return text[: max(budget_chars - len(ELLIPSIS), 0)] + ELLIPSIS
    return " ".join(kept) + ELLIPSIS


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
    """Brief's own task list rows: same selection as :func:`today_priority_tasks`,
    with :func:`brief_due_label` (no "DUE " word) in place of :func:`due_label`.
    The title itself is clipped later, per row, in :func:`brief_task_rows`,
    which needs ``chip_kind`` to size that row's own budget."""
    tasks = sorted(open_tasks(state), key=lambda task: task_sort_key(task, today))[:limit]
    rows: list[dict[str, Any]] = []
    for task in tasks:
        rows.append(
            {
                "title": task.title,
                "due_label": brief_due_label(task.due, today),
                "accent": task_accent(task, today),
                "priority": task.priority.value,
                "chip_kind": due_chip_kind(task.due, today),
            }
        )
    return rows


def due_label(due: date | None, today: date) -> str:
    """Short enough to sit next to a task title on an 800px screen.

    The word TOMORROW never appears here (the owner asked for it gone
    everywhere, not just on Today): a task due tomorrow prints its 3-letter
    weekday instead.
    """
    if due is None:
        return ""
    delta = (due - today).days
    if delta < 0:
        return f"{-delta}D LATE"
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return due.strftime("%a").upper()
    return f"DUE {due.strftime('%d %b').upper()}"


def brief_due_label(due: date | None, today: date) -> str:
    """Brief's own compact due text: :func:`due_label`, with the leading
    "DUE " word dropped ("DUE 08 SEP" alone runs past a legible width sooner
    than the bare date does)."""
    label = due_label(due, today)
    return label[len("DUE ") :] if label.startswith("DUE ") else label


def due_chip_kind(due: date | None, today: date) -> str:
    """Which of the five due-chip shapes ``due`` renders as.

    Matches :func:`due_label`/:func:`brief_due_label`'s own branching
    exactly, so a row's title budget can be sized to that row's own chip
    instead of assuming every row carries the widest one.
    """
    if due is None:
        return "none"
    delta = (due - today).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta == 1:
        return "day"
    return "date"


#: PRIORITIES title budget floor: the narrowest a row's title column ever
#: gets is beside a red overdue chip (icon included), measured at 302.9 px
#: against the fixture in Chromium ("Google Sans" 500 at 24px). Every row
#: now gets its own budget from its own chip (see :data:`TODAY_CHIP_WIDTH_PX`
#: and :func:`priority_title_budget`); this stays the minimum any row can
#: fall to, so the ragged right edge never grows past what one more word
#: beyond it would need.
PRIORITY_TITLE_AVAILABLE_PX: float = 300.0
#: Measured width per character at that size: "Send vendor..." and "Finish
#: Q3..." both landed near 11.7 px/char; the higher, denser figure is used so
#: a borderline title clips a touch early rather than ever overflowing.
PRIORITY_TITLE_CHAR_PX: float = 12.7
PRIORITY_TITLE_MAX_CHARS: int = int(PRIORITY_TITLE_AVAILABLE_PX // PRIORITY_TITLE_CHAR_PX)
#: Chip widths measured directly against the real render (Chromium), one per
#: shape :func:`due_chip_kind` can return, at the PRIORITIES row's own 16 px
#: ``.chip`` / ``.chip-plain`` (base.html: no border, 2/6/3 px padding, 16 px
#: caps at weight 700, 0.02em tracking; the overdue chip also carries the
#: FLAG glyph ahead of its text). The "date" shape ("DUE 08 SEP") is, despite
#: looking plain, not the narrowest: its "DUE " prefix makes it about as wide
#: as the overdue chip.
TODAY_CHIP_WIDTH_PX: dict[str, float] = {
    "overdue": 94.0,
    "today": 68.0,
    "day": 41.0,
    "date": 96.0,
    "none": 0.0,
}
#: PRIORITIES row content width (456 px column minus its own 16 px padding)
#: minus the checkbox glyph and the row's own two 10 px gaps: 440 - 24 - 20.
#: A row with no due chip at all only pays one gap, not two (no third
#: element to space from), so this slightly under-counts that one case;
#: safe, since it only makes that row's budget a touch smaller than the true
#: maximum, never larger.
TODAY_TITLE_AND_CHIP_PX: float = 396.0


def priority_title_budget(kind: str) -> int:
    """PRIORITIES per-row title character budget for a chip of ``kind``.

    Never below :data:`PRIORITY_TITLE_MAX_CHARS`: that old uniform figure is
    the floor every row keeps even beside its own widest possible chip.
    """
    title_px = TODAY_TITLE_AND_CHIP_PX - TODAY_CHIP_WIDTH_PX[kind]
    return max(PRIORITY_TITLE_MAX_CHARS, int(title_px // PRIORITY_TITLE_CHAR_PX))


def today_priority_tasks(state: DashboardState, today: date, limit: int) -> list[dict[str, Any]]:
    """The Today page's own priority rows: same selection and due text as the
    shared :func:`priority_tasks` (which Brief also calls, through its own
    :func:`brief_due_label`)."""
    tasks = sorted(open_tasks(state), key=lambda task: task_sort_key(task, today))[:limit]
    rows: list[dict[str, Any]] = []
    for task in tasks:
        budget = priority_title_budget(due_chip_kind(task.due, today))
        rows.append(
            {
                "title": clip_words(task.title, budget),
                "due_label": due_label(task.due, today),
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
        else:
            # A later day always prints its 3-letter weekday, including
            # tomorrow: the word TOMORROW never appears here.
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


#: The day clamps a same-day event end to 24:00, an hour a plain "minutes
#: since midnight" cannot itself represent (see
#: :func:`_event_end_minutes_same_day`, which :func:`next_seven_days` uses).
SCALE_END_HOUR: int = 24


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
#: The AGENDA title's own available width: the 440 px row minus the fixed
#: 132 px time column and its own 12 px gap leaves 296 px (same font as
#: PRIORITIES: "Google Sans" 500 at 24px), so it reuses that size's per-
#: character figure.
AGENDA_TITLE_AVAILABLE_PX: float = 296.0
AGENDA_TITLE_MAX_CHARS: int = int(AGENDA_TITLE_AVAILABLE_PX // PRIORITY_TITLE_CHAR_PX)


def today_next_rows(
    state: DashboardState, reference: datetime, colors: dict[str, str], limit: int = AGENDA_ROW_LIMIT
) -> list[dict[str, Any]]:
    """One line per upcoming event: today's events that have not ended yet
    first, then later days in start order, capped at the row count the left
    column actually has room for.

    When no event remains today, the first row is a placeholder ("TODAY" /
    "Nothing left today") so the list never opens straight on a future day
    with no sign that today itself is clear; it counts against the row
    budget like any other row. When today still has a remaining event, no
    placeholder is added.

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
    if not any(event.start.date() == today for event in events):
        rows.append({"when": "TODAY", "title": "Nothing left today", "color": "black"})
    for event in events[: limit - len(rows)]:
        day = event.start.date()
        if day == today:
            when = "ALL DAY" if event.all_day else event.start.strftime("%H:%M")
        else:
            prefix = day.strftime("%a").upper()
            when = prefix if event.all_day else f"{prefix} {event.start.strftime('%H:%M')}"
        rows.append(
            {
                "when": when,
                "title": clip_words(event.title, AGENDA_TITLE_MAX_CHARS),
                "color": event_color(event, colors),
            }
        )
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


# ---------------------------------------------------------------------------
# Staleness (Part 2, DESIGN.md): view-layer only. A dataset a remote agent
# pushes (ai_usage, brief, tasks) carries its own age, and past a threshold
# the panel marks it, exactly the System page's stale BATTERY pattern: a
# yellow tell-tale before the section label, the age in 16 px caps after it.
# Never serialized onto a model: the state fingerprint (state.py) and the
# device's PNG 304 path stay untouched. Because pages are cached (Today's
# TTL is 1800 s, render.py:PAGE_TTL_SECONDS), the mark itself can lag a push
# by up to one page TTL.
# ---------------------------------------------------------------------------
def stale_info(
    reference: datetime | None, source: str, threshold_seconds: float, now: datetime
) -> str | None:
    """None when the age is unknown, the source is a fixture (demo data is
    never marked stale), or the age is under an hour or under the
    threshold; otherwise an age label bucketed to whole hours ("6 H AGO").
    """
    if source == "fixture" or reference is None:
        return None
    age_seconds = (now - reference).total_seconds()
    if age_seconds < threshold_seconds:
        return None
    hours = int(age_seconds // 3600)
    if hours < 1:
        return None
    return f"{hours} H AGO"


def ai_usage_stale(state: DashboardState, settings: Settings, now: datetime) -> str | None:
    """Age source: the oldest provider's ``collected_at``."""
    block = state.ai_usage
    collected = [p.collected_at for p in block.providers if p.collected_at is not None]
    reference = min(collected) if collected else None
    return stale_info(reference, block.source, settings.ai_usage_stale_seconds, now)


def brief_stale(state: DashboardState, settings: Settings, now: datetime) -> str | None:
    """Age source: ``Brief.generated_at``."""
    block = state.brief
    reference = block.brief.generated_at if block.brief is not None else None
    return stale_info(reference, block.source, settings.brief_stale_seconds, now)


def tasks_stale(state: DashboardState, settings: Settings, now: datetime) -> str | None:
    """Age source: ``TasksBlock.received_at``."""
    block = state.tasks
    return stale_info(block.received_at, block.source, settings.tasks_stale_seconds, now)


#: Which pushed datasets each page actually shows: what the footer's DEMO
#: mark (any of them sourced "fixture") and the "!" flag (any of them
#: stale) are about. Weather/calendar/home/device keep their own
#: established fixture-fallback story from earlier phases (fetched, or
#: pushed by the device itself, not by an agent), so they are never listed
#: here and never print DEMO. A page missing from this map, or missing one
#: of its own datasets, silently gets no DEMO mark and no stale flag for
#: that dataset: keep it in step with what each page's template actually
#: draws (see "Adding a new pushed dataset" in docs/DATA-SOURCES.md). Today
#: also draws the brief note (view.py:brief_note, the Today page's NOTE
#: field), so "brief" belongs here too, not just on the brief page.
PAGE_PUSH_DATASETS: dict[str, tuple[str, ...]] = {
    "today": ("ai_usage", "brief", "tasks"),
    "agenda": (),
    "weather": (),
    "brief": ("brief", "tasks"),
    "system": (),
    "alert": (),
}


def page_shows_demo_data(state: DashboardState, page: str) -> bool:
    """True when a pushed dataset actually shown on this page is a fixture,
    so a fresh install (nothing pushed yet) never passes demo numbers off
    as real."""
    blocks = {"ai_usage": state.ai_usage, "brief": state.brief, "tasks": state.tasks}
    return any(blocks[name].source == "fixture" for name in PAGE_PUSH_DATASETS.get(page, ()))


def window_flags(
    state: DashboardState, settings: Settings, reference: datetime, overdue_count: int
) -> set[str]:
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

    if (
        ai_usage_stale(state, settings, reference)
        or brief_stale(state, settings, reference)
        or tasks_stale(state, settings, reference)
    ):
        flagged.add("today")
    if brief_stale(state, settings, reference) or tasks_stale(state, settings, reference):
        flagged.add("brief")
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


#: Pages that already show the overdue task themselves, in the red 1D LATE
#: chip on their own priority row: the header's own overdue chip would only
#: repeat it, so those pages hide it there.
PAGES_WITH_OWN_OVERDUE_CHIP: frozenset[str] = frozenset({"today", "brief"})


def header_context(state: DashboardState, today: date, reference: datetime, page: str) -> dict[str, Any]:
    """Everything the shared header draws, on every page."""
    device: DeviceState | None = state.device.device
    has_device = state.device.usable and device is not None and device.has_reading
    level = device.battery_level if has_device else None
    rssi = device.wifi_rssi if has_device else None
    charging = has_device and device.charge_state == "charging"
    overdue_count = len(overdue_tasks(state, today))
    return {
        "day": reference.strftime("%d"),
        "weekday": reference.strftime("%a").upper(),
        "month": reference.strftime("%b").upper(),
        "weather": header_weather(state),
        "overdue_count": overdue_count,
        "show_overdue_chip": overdue_count > 0 and page not in PAGES_WITH_OWN_OVERDUE_CHIP,
        "wifi_icon": icons.wifi_icon(rssi),
        "battery": {
            "icon": icons.battery_icon(level, charging),
            "percent": fmt_number(level, digits=0),
            "chip_accent": header_battery_chip(level),
        },
        "clock": reference.strftime("%H:%M"),
    }


def footer_context(
    state: DashboardState, settings: Settings, today: date, reference: datetime, page: str
) -> dict[str, Any]:
    """The window list: the five pages the buttons walk through, plus the
    DEMO mark at the footer's right end for this page."""
    overdue_count = len(overdue_tasks(state, today))
    flagged = window_flags(state, settings, reference, overdue_count)
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
        "demo": page_shows_demo_data(state, page),
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
        "header": header_context(state, today, reference, page),
        "footer": footer_context(state, settings, today, reference, page),
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
    context["priorities_stale"] = tasks_stale(state, settings, reference)
    context["capacity_stale"] = ai_usage_stale(state, settings, reference)
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


#: One list row, event or divider alike: the body below "TODAY" and the long
#: date is a plain list, so every row it holds - an event, a day divider, or
#: the trailing "+N more" - is quantized to this one fixed height. Picked at
#: the dense-list-row floor (DESIGN.md: "22 to 28 px for dense list rows"),
#: which packs the most rows into the fixed 372 px body, matching the owner's
#: "until it fills the screen".
AGENDA_LIST_ROW_HEIGHT_PX: float = 28.0
#: How many list rows the body has room for below the head: 372 - (24 + 8)
#: leaves 340 px, and 340 // 28 is 12 whole rows (336 px used, 4 px spare;
#: never a thirteenth partial row).
AGENDA_LIST_ROW_LIMIT: int = int(
    (AGENDA_LEFT_BODY_HEIGHT_PX - AGENDA_HEAD_HEIGHT_PX - AGENDA_HEAD_GAP_PX)
    // AGENDA_LIST_ROW_HEIGHT_PX
)
#: The time column's own fixed width: wide enough for "ALL DAY" (the widest
#: label this column ever prints, measured at 65.9 px against the real
#: render, this row's own weight 700 20 px Google Sans; a plain "HH:MM" is
#: narrower at 43.2 px), with a couple of pixels of margin.
AGENDA_LIST_TIME_PX: float = 68.0
#: Gap between the time column and the title, matching the agenda row's own
#: convention on Today's left column (`.agenda-row` in today.html).
AGENDA_LIST_GAP_PX: float = 12.0
#: The title's own available width: the 440 px row (the 456 px column minus
#: its own 16 px padding) minus the time column and its gap.
AGENDA_LIST_TITLE_AVAILABLE_PX: float = 440.0 - AGENDA_LIST_TIME_PX - AGENDA_LIST_GAP_PX
#: Per-character width at this row's 20 px/500 weight: the same measured
#: figure Brief's own 20 px/500 task titles use (see :data:`BRIEF_TITLE_CHAR_PX`),
#: since it is the identical font, weight and size.
AGENDA_LIST_TITLE_CHAR_PX: float = 10.5
AGENDA_LIST_TITLE_MAX_CHARS: int = int(AGENDA_LIST_TITLE_AVAILABLE_PX // AGENDA_LIST_TITLE_CHAR_PX)


def _agenda_day_label(day: date, today: date) -> str:
    """Divider text introducing a later day: TOMORROW for the next day (the
    one place the word is still used, naming the very next day rather than a
    weekday that could be mistaken for this week), else the weekday and date
    (:func:`fmt_day_header`, e.g. "SAT 21 SEP")."""
    if day == today + timedelta(days=1):
        return "TOMORROW"
    return fmt_day_header(day)


def _agenda_day_event_rows(events: list[Event], colors: dict[str, str]) -> list[dict[str, Any]]:
    """One row per event on a single day: all-day events first, then timed
    events in start order. The day itself is never repeated here (the head
    or a divider row already named it), so the time slot is bare "HH:MM" or
    "ALL DAY"."""
    ordered = sorted(events, key=lambda event: (not event.all_day, event.start))
    return [
        {
            "kind": "event",
            "when": "ALL DAY" if event.all_day else event.start.strftime("%H:%M"),
            "title": clip_words(event.title, AGENDA_LIST_TITLE_MAX_CHARS),
            "color": event_color(event, colors),
        }
        for event in ordered
    ]


def agenda_list_rows(
    state: DashboardState, colors: dict[str, str], today: date
) -> list[dict[str, Any]]:
    """The agenda's left column: today's events (including ones already
    past), then as many later days as the column has room for, each
    introduced by its own divider row, packed into
    :data:`AGENDA_LIST_ROW_LIMIT` fixed-height rows.

    Returns an empty list when there is nothing to show at all (the template
    prints "Nothing scheduled" once); otherwise the last row reads "+N more"
    in place of a divider or event once the remaining rows would not fit,
    ``N`` counting only events, never the dividers that introduced them.
    """
    if not state.calendar.usable:
        return []
    events_by_day: dict[date, list[Event]] = {}
    for event in state.calendar.items:
        day = event.start.date()
        if day < today:
            continue
        events_by_day.setdefault(day, []).append(event)
    if not events_by_day:
        return []

    entries: list[dict[str, Any]] = []
    for day in sorted(events_by_day):
        if day != today:
            entries.append({"kind": "divider", "label": _agenda_day_label(day, today)})
        entries.extend(_agenda_day_event_rows(events_by_day[day], colors))

    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        more_after = index < len(entries) - 1
        reserve = 1 if more_after else 0
        if len(rows) + 1 + reserve > AGENDA_LIST_ROW_LIMIT:
            remaining = sum(1 for later in entries[index:] if later["kind"] == "event")
            if remaining:
                rows.append({"kind": "more", "count": remaining})
            break
        rows.append(entry)
    return rows


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
    context["agenda_list"] = agenda_list_rows(state, colors, today)
    context["month"] = month_grid(state, colors, today)
    context["next7"] = next_seven_days(state, today)
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


#: Matches weather.html's own ``.wx-plate { width: 64px; }``: the NEXT HOURS
#: baseline is sized in Python from this same figure so it always ends with
#: the last plate rather than running past whatever the block has.
WX_PLATE_WIDTH_PX: float = 64.0


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
    context["wx_plates_width_px"] = len(context["hourly"]) * WX_PLATE_WIDTH_PX
    context["daily"] = weather_daily_rows(weather, today)
    return context


def strip_day_label(day: date, today: date) -> str:
    """Day label for the five column forecast strip.

    Weekday names throughout, because a column is about 73 px wide inside its
    padding and TOMORROW needs 122 px at the 18 px legibility floor. Inventing
    a shorter word for it would put a second abbreviation in the world for no
    reason, so only TODAY, which fits, gets a relative name here. The word
    TOMORROW never appears anywhere in the product: every other day, near or
    far, prints its 3-letter weekday instead.
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
#: The task column's own body height: the 372 px shared right-column height
#: minus the 24 px head and its 4 px margin, measured against the real
#: rendered layout.
BRIEF_TASK_LIST_HEIGHT_PX: float = 344.0
#: A title that fits one line renders as a plain 32 px row.
BRIEF_TASK_ROW_1_PX: float = 32.0
#: A title that needs two lines: measured against the real clamp-2 box in
#: Chromium at this font (20px/500, line-height 1.3), a two-line title is
#: 52 px tall.
BRIEF_TASK_ROW_2_PX: float = 52.0
#: The "+N MORE" row, when the list does not have room for every open task:
#: same height as a plain single-line row.
BRIEF_MORE_ROW_PX: float = 32.0
#: The single-line title's own available width floor, measured against the
#: real 296 px right column (Chromium, "Google Sans" 500 at 20px) beside the
#: overdue chip, its narrowest case: the checkbox glyph, two 10 px row gaps
#: and that chip leave 156 px. Every row now gets its own budget from its
#: own chip (see :data:`BRIEF_CHIP_WIDTH_PX` and :func:`brief_title_budget`);
#: this stays the minimum, and a two-line title's per-line budget is that
#: row's own single-line figure used twice (the icon and due chip are the
#: same in both row heights).
BRIEF_TITLE_AVAILABLE_PX: float = 156.0
#: Worst-case measured width per character at that size (real task titles
#: ran 9.4-10.4 px/char); the higher bound is used so a borderline title
#: wraps rather than stemming.
BRIEF_TITLE_CHAR_PX: float = 10.5
BRIEF_TITLE_MAX_CHARS: int = int(BRIEF_TITLE_AVAILABLE_PX // BRIEF_TITLE_CHAR_PX)
#: Chip widths measured directly against the real render, one per shape
#: :func:`due_chip_kind` can return, at Brief's own smaller 14 px chip
#: override (brief.html: no border, 1/4/2 px padding, 14 px caps at weight
#: 700, 2 px gap; the owner's floor is nothing under 14 px, so this is the
#: smallest this class may ever go). The overdue chip also carries the FLAG
#: glyph. Brief's own date shape has no "DUE " prefix (:func:`brief_due_label`
#: drops it), so unlike Today's table it is genuinely one of the narrower
#: chips.
BRIEF_CHIP_WIDTH_PX: dict[str, float] = {
    "overdue": 82.0,
    "today": 58.0,
    "day": 37.0,
    "date": 51.0,
    "none": 0.0,
}
#: Task row content width (296 px column minus its own 16 px padding) minus
#: the checkbox glyph and the row's own two 10 px gaps: 280 - 20 - 20.
BRIEF_TITLE_AND_CHIP_PX: float = 240.0


def brief_title_budget(kind: str) -> int:
    """Brief per-row title character budget for a chip of ``kind``.

    Never below :data:`BRIEF_TITLE_MAX_CHARS`, the old uniform figure, kept
    as the floor for the same reason as :func:`priority_title_budget`.
    """
    title_px = BRIEF_TITLE_AND_CHIP_PX - BRIEF_CHIP_WIDTH_PX[kind]
    return max(BRIEF_TITLE_MAX_CHARS, int(title_px // BRIEF_TITLE_CHAR_PX))
#: Shown in place of the running text when the PC has never sent a brief.
BRIEF_UNAVAILABLE_MESSAGE: str = "No brief from the PC yet"
#: The headline's own available width, measured the same way against the
#: 456 px left column minus its 16 px padding-right: 440 px at 28px/600.
BRIEF_HEADLINE_AVAILABLE_PX: float = 440.0
#: Worst-case measured width per character at that size (9.4-10.4 px/char
#: measured lower; the sample topped out at ~14.3 px/char), so a headline
#: this long or longer never reliably fits one line at 28 px and drops to
#: 24 px instead.
BRIEF_HEADLINE_CHAR_PX: float = 14.3
BRIEF_HEADLINE_MAX_CHARS_28: int = int(BRIEF_HEADLINE_AVAILABLE_PX // BRIEF_HEADLINE_CHAR_PX)


def brief_is_risk(title: str) -> bool:
    """A section heading belongs to the existing risk keyword group."""
    return icons.brief_accent(title) == "red"


def brief_headline_fits_one_line(headline: str) -> bool:
    """Whether the headline fits one line at 28 px; if not it renders at
    24 px instead (see :data:`BRIEF_HEADLINE_MAX_CHARS_28`)."""
    return len(headline) <= BRIEF_HEADLINE_MAX_CHARS_28


def brief_task_rows(
    tasks: list[dict[str, Any]], list_height_px: float = BRIEF_TASK_LIST_HEIGHT_PX
) -> tuple[list[dict[str, Any]], int]:
    """Pack ``tasks`` (already sorted, priority order) into the column's
    fixed height, one row per task, single line where the title fits and
    two lines where it does not, rather than the old all-or-nothing switch
    that dropped every row to two lines and five slots the moment one title
    ran long.

    The character budget comes from that row's own chip, not a uniform
    figure: a narrow chip ("MON") hands its title real extra room, per
    :func:`brief_title_budget`. A title too long even for two lines at that
    budget is clipped at a word boundary with :func:`clip_words`, using the
    per-line budget twice over: the icon and the chip are identical in both
    row heights, so the title's real available width does not change
    between them.

    When every task still does not fit the column, the row that would have
    overrun is dropped and everything from it on is folded into the hidden
    count the template prints as "+N MORE"; that row's own 32 px is reserved
    ahead of time so the more-line itself never has to bump something else.
    """
    rows: list[dict[str, Any]] = []
    used_px = 0.0
    for index, task in enumerate(tasks):
        title = task["title"]
        budget = brief_title_budget(task.get("chip_kind", "none"))
        if len(title) <= budget:
            row = {**task, "two_line": False}
            row_px = BRIEF_TASK_ROW_1_PX
        else:
            row = {**task, "title": clip_words(title, budget * 2), "two_line": True}
            row_px = BRIEF_TASK_ROW_2_PX
        more_after = index < len(tasks) - 1
        reserve = BRIEF_MORE_ROW_PX if more_after else 0.0
        if used_px + row_px + reserve > list_height_px + 0.5:
            return rows, len(tasks) - index
        rows.append(row)
        used_px += row_px
    return rows, 0


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
    reference: datetime = context["reference"]
    brief = state.brief.brief
    context["brief_stale"] = brief_stale(state, settings, reference)
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
        context["headline_large"] = brief_headline_fits_one_line(context["headline"])
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
        context["headline_large"] = True
        context["lines"] = []
        context["truncated"] = False

    if state.tasks.usable:
        open_count = len(open_tasks(state))
        candidates = priority_tasks(state, today, open_count)
        context["tasks"], context["tasks_more_count"] = brief_task_rows(candidates)
    else:
        context["tasks"] = []
        context["tasks_more_count"] = 0
    context["tasks_open_count"] = len(open_tasks(state)) if state.tasks.usable else 0
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    return context


#: A trailing duration inside a free-text sensor value ("Clear 41m"): the
#: panel's numerals and units are otherwise always caps ("41M", "3H"), so the
#: unit letter is capped here rather than left lower-case mid-sentence.
_DURATION_SUFFIX_RE = re.compile(r"(?<=\d)([hmsd])\b")


def cap_duration(value: str) -> str:
    """Upper-case a trailing "<number><unit>" duration inside ``value``."""
    return _DURATION_SUFFIX_RE.sub(lambda m: m.group(1).upper(), value)


def sensor_value(value: str | None, unit: str | None) -> str:
    """Join a sensor reading and its unit without inventing a value."""
    if value is None:
        return UNKNOWN
    if not unit:
        return cap_duration(value)
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
