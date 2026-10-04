"""The Agenda page's own context builder, its header widget, and the
helpers only they need.

Moved out of ``app/view.py`` in 2.2 (docs/plan/2026-09-19-settings-modules-
provisioning.md): everything here is read by no other page. Shared helpers
(formatting, header, footer, base context, ``overdue_tasks``,
``calendar_colors``/``event_color``) stay in ``app/view.py`` and are
imported from there.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, TYPE_CHECKING

from app.models import CalendarBlock, DashboardState, Event
from app.modules import HEADER_WIDGET_WIDTH_PX
from app.view import (
    base_context,
    block_note,
    calendar_colors,
    clip_words,
    event_color,
    fmt_day_header,
    overdue_tasks,
    page_reference,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.settings import HubSettings


def agenda_flag(state: DashboardState, settings: "HubSettings") -> bool:
    """An overdue task is what sends the owner to the task list."""
    return len(overdue_tasks(state, page_reference(state).date())) > 0


#: The agenda's left column body height, matching the shared 372 px body.
AGENDA_LEFT_BODY_HEIGHT_PX: float = 372.0
#: "TODAY" plus the long date, one baseline-aligned row.
AGENDA_HEAD_HEIGHT_PX: float = 24.0
AGENDA_HEAD_GAP_PX: float = 0.0

#: The "rough answer" strip's own span: 06:00 to 22:00, not the full day.
NEXT7_START_MIN: int = 6 * 60
NEXT7_END_MIN: int = 22 * 60
NEXT7_SPAN_MIN: int = NEXT7_END_MIN - NEXT7_START_MIN

#: The day clamps a same-day event end to 24:00, an hour a plain "minutes
#: since midnight" cannot itself represent (see
#: :func:`_event_end_minutes_same_day`, which :func:`next_seven_days` uses).
SCALE_END_HOUR: int = 24


def _minutes_since_midnight(value: datetime) -> float:
    return value.hour * 60 + value.minute + value.second / 60.0


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
#: Keep a conservative row budget even though the shorter day dividers
#: leave some spare room: a list of events alone must also fit.
AGENDA_LIST_ROW_LIMIT: int = int(
    (AGENDA_LEFT_BODY_HEIGHT_PX - AGENDA_HEAD_HEIGHT_PX - AGENDA_HEAD_GAP_PX)
    // AGENDA_LIST_ROW_HEIGHT_PX
)
#: The time column's own fixed width: wide enough for "ALL DAY" (the widest
#: label this column ever prints, measured at 80.4 px against the real
#: render, this row's own weight 700 20 px Google Sans; a plain "HH:MM" is
#: narrower at 43.2 px), with a couple of pixels of margin.
AGENDA_LIST_TIME_PX: float = 84.0
#: Gap between the time column and the title, matching the agenda row's own
#: convention on Today's left column (`.agenda-row` in today.html).
AGENDA_LIST_GAP_PX: float = 12.0
#: The title's own available width: the 440 px row (the 456 px column minus
#: its own 16 px padding) minus the time column and its gap.
AGENDA_LIST_TITLE_AVAILABLE_PX: float = 440.0 - AGENDA_LIST_TIME_PX - AGENDA_LIST_GAP_PX
#: Per-character width at this row's 20 px/500 weight: the same measured
#: figure Brief's own 20 px/500 task titles use (see :data:`app.modules.brief.
#: page.BRIEF_TITLE_CHAR_PX`), since it is the identical font, weight and size.
AGENDA_LIST_TITLE_CHAR_PX: float = 10.5
AGENDA_LIST_TITLE_MAX_CHARS: int = int(AGENDA_LIST_TITLE_AVAILABLE_PX // AGENDA_LIST_TITLE_CHAR_PX)


def _agenda_day_label(day: date, today: date) -> str:
    """Divider text introducing a later day: TOMORROW for the next day (the
    one place the word is still used, naming the very next day rather than a
    weekday that could be mistaken for this week), else the weekday and date
    (:func:`app.view.fmt_day_header`, e.g. "SAT 21 SEP")."""
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
    if not state.block("calendar", CalendarBlock).usable:
        return []
    events_by_day: dict[date, list[Event]] = {}
    for event in state.block("calendar", CalendarBlock).items:
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
    """Show the whole month and at least the weeks before and after today.

    Real neighboring dates keep week context at month boundaries. A date's
    event border uses its calendar color, or black for multiple calendars.
    """
    first = today.replace(day=1)
    next_first = (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )
    last = next_first - timedelta(days=1)
    current_monday = today - timedelta(days=today.weekday())
    grid_start = min(first - timedelta(days=first.weekday()), current_monday - timedelta(days=7))
    grid_end = max(last + timedelta(days=6 - last.weekday()), current_monday + timedelta(days=13))

    day_colors: dict[date, set[str]] = {}
    if state.block("calendar", CalendarBlock).usable:
        for event in state.block("calendar", CalendarBlock).items:
            day = event.start.date()
            day_colors.setdefault(day, set()).add(event_color(event, colors))

    cells: list[dict[str, Any]] = []
    for offset in range((grid_end - grid_start).days + 1):
        day = grid_start + timedelta(days=offset)
        present = day_colors.get(day, set())
        color = "black" if len(present) > 1 else (next(iter(present)) if present else None)
        cells.append({
            "date": day,
            "number": day.day,
            "is_today": day == today,
            "in_month": day.month == today.month and day.year == today.year,
            "color": color,
        })

    return {
        "name": today.strftime("%B").upper(),
        "weeks": [cells[index : index + 7] for index in range(0, len(cells), 7)],
    }


def _next7_position(minutes: float) -> float:
    return max(0.0, min(100.0, (minutes - NEXT7_START_MIN) / NEXT7_SPAN_MIN * 100.0))


def next_seven_days(
    state: DashboardState, today: date, colors: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Keep all-day calendar marks beside the timed 06:00-22:00 tracks.

    Separating them avoids an all-day reminder hiding the day's timed slots.
    """
    colors = colors or {}
    events = state.block("calendar", CalendarBlock).items if state.block("calendar", CalendarBlock).usable else []
    rows: list[dict[str, Any]] = []
    for offset in range(1, 8):
        day = today + timedelta(days=offset)
        day_events = [event for event in events if event.start.date() == day]
        segments: list[dict[str, Any]] = []
        all_day_colors: set[str] = set()
        for event in day_events:
            if event.all_day:
                all_day_colors.add(event_color(event, colors))
                continue
            start_pct = _next7_position(_minutes_since_midnight(event.start))
            end_pct = _next7_position(_event_end_minutes_same_day(event))
            if end_pct > start_pct:
                segments.append({
                    "left": round(start_pct, 2), "width": round(end_pct - start_pct, 2),
                    "color": event_color(event, colors),
                })
        rows.append(
            {
                "label": f"{day.strftime('%a')[0].upper()} {day.strftime('%d')}",
                "segments": segments,
                "all_day_colors": sorted(all_day_colors),
                "count": len(day_events) or None,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# the header widget: the next event, in the 380 x 40 px slot
# (docs/MODULES.md, "Header widget")
# ---------------------------------------------------------------------------
#: The widget's own time column, wide enough for its widest reading, a later
#: day's "SAT 18:00" at this row's 20 px weight 700 (measured against the
#: agenda list's own 20 px figures: 65.9 px for "ALL DAY", so a weekday plus
#: a clock lands just under 100).
AGENDA_HEADER_WHEN_PX: float = 100.0
#: Gap between the time and the title, the agenda list's own.
AGENDA_HEADER_GAP_PX: float = 12.0
#: What is left of the 380 px slot for the title itself.
AGENDA_HEADER_TITLE_AVAILABLE_PX: float = (
    HEADER_WIDGET_WIDTH_PX - AGENDA_HEADER_WHEN_PX - AGENDA_HEADER_GAP_PX
)
#: Per-character width at 20 px weight 500, the agenda list's own measured
#: figure (:data:`AGENDA_LIST_TITLE_CHAR_PX`).
AGENDA_HEADER_TITLE_MAX_CHARS: int = int(
    AGENDA_HEADER_TITLE_AVAILABLE_PX // AGENDA_LIST_TITLE_CHAR_PX
)


def _agenda_header_when(event: Event, today: date) -> str:
    """The event's slot, the same grammar Today's upcoming list uses: a bare
    clock for something today, the 3-letter weekday in front of it for a
    later day, and the weekday alone for an all-day event."""
    day = event.start.date()
    if day == today:
        return "ALL DAY" if event.all_day else event.start.strftime("%H:%M")
    prefix = day.strftime("%a").upper()
    return prefix if event.all_day else f"{prefix} {event.start.strftime('%H:%M')}"


def agenda_header(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    """This module's header widget: when the next event starts, and what it
    is.

    "Next" is measured from the same reference every page reasons from
    (:func:`app.view.page_reference`) and an event counts until it ends, so
    the meeting the owner is sitting in is still the one on the header
    rather than the one after it. Nothing left today or later is an honest
    empty state, not a blank cell.
    """
    block = state.block("calendar", CalendarBlock)
    if not block.usable:
        return {"available": False}
    reference = page_reference(state)
    upcoming = sorted(
        (event for event in block.items if (event.end or event.start) >= reference),
        key=lambda event: event.start,
    )
    if not upcoming:
        return {"available": False}
    event = upcoming[0]
    # No calendar colour here, unlike the agenda list's own rows: the
    # header carries no legend, so a green time would be colour without a
    # state behind it, which is exactly what the panel's grammar refuses
    # (DESIGN.md, "colour is a state").
    return {
        "available": True,
        "when": _agenda_header_when(event, reference.date()),
        "title": clip_words(event.title, AGENDA_HEADER_TITLE_MAX_CHARS),
    }


def agenda_context(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    context = base_context(state, settings, "agenda")
    today: date = context["today"]
    reference: datetime = context["reference"]
    colors = calendar_colors(state, settings)

    context["long_date"] = reference.strftime("%A %d %B").upper()
    context["agenda_list"] = agenda_list_rows(state, colors, today)
    context["calendar_note"] = block_note(state.block("calendar", CalendarBlock).status, "calendar")
    context["month"] = month_grid(state, colors, today)
    context["next7"] = next_seven_days(state, today, colors)
    context["next7_ticks"] = [
        {"label": f"{hour:02d}", "left": _next7_position(hour * 60)}
        for hour in (8, 12, 18)
    ]
    return context


__all__ = [
    "AGENDA_HEADER_GAP_PX",
    "AGENDA_HEADER_TITLE_AVAILABLE_PX",
    "AGENDA_HEADER_TITLE_MAX_CHARS",
    "AGENDA_HEADER_WHEN_PX",
    "AGENDA_HEAD_GAP_PX",
    "AGENDA_HEAD_HEIGHT_PX",
    "AGENDA_LEFT_BODY_HEIGHT_PX",
    "AGENDA_LIST_GAP_PX",
    "AGENDA_LIST_ROW_HEIGHT_PX",
    "AGENDA_LIST_ROW_LIMIT",
    "AGENDA_LIST_TIME_PX",
    "AGENDA_LIST_TITLE_AVAILABLE_PX",
    "AGENDA_LIST_TITLE_CHAR_PX",
    "AGENDA_LIST_TITLE_MAX_CHARS",
    "NEXT7_END_MIN",
    "NEXT7_SPAN_MIN",
    "NEXT7_START_MIN",
    "SCALE_END_HOUR",
    "agenda_context",
    "agenda_flag",
    "agenda_header",
    "agenda_list_rows",
    "month_grid",
    "next_seven_days",
]
