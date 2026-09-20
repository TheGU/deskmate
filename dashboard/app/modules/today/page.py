"""The Today page's own context builder and the helpers only it needs.

Moved out of ``app/view.py`` in 2.2 (docs/plan/2026-09-19-settings-modules-
provisioning.md): everything here is read by no other page, so it lives next
to the module that owns it. Shared helpers (formatting, header, footer, base
context, the stale rules, ``open_tasks``/``task_sort_key``/``due_label``/
``due_chip_kind``, which Brief also uses) stay in ``app/view.py`` and are
imported from there.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, TYPE_CHECKING

from app.models import (
    AIUsageBlock,
    BriefBlock,
    CalendarBlock,
    DashboardState,
    Event,
    TasksBlock,
)
from app.view import (
    UNAVAILABLE,
    ai_usage_stale,
    base_context,
    block_note,
    brief_stale,
    calendar_colors,
    clip_words,
    due_chip_kind,
    due_label,
    event_color,
    fmt_number,
    fmt_time,
    open_tasks,
    page_reference,
    task_accent,
    task_sort_key,
    tasks_stale,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.settings import HubSettings

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
    shared :func:`app.view.priority_tasks` (which Brief also calls, through
    its own :func:`app.view.brief_due_label`)."""
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


def upcoming_events(
    state: DashboardState,
    reference: datetime,
    limit: int,
    colors: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    today = reference.date()
    colors = colors or {}
    events = [event for event in state.block("calendar", CalendarBlock).items if _event_end(event) >= reference]
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
    if not state.block("calendar", CalendarBlock).usable:
        return []
    today = reference.date()
    events = sorted(
        (event for event in state.block("calendar", CalendarBlock).items if _event_end(event) >= reference),
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


def today_context(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    context = base_context(state, settings, "today")
    reference: datetime = context["reference"]
    today: date = context["today"]
    colors = calendar_colors(state, settings)

    context["priorities"] = (
        today_priority_tasks(state, today, settings.tasks.max_priority_tasks)
        if state.block("tasks", TasksBlock).usable
        else []
    )
    context["tasks_note"] = block_note(state.block("tasks", TasksBlock).status, "tasks")
    context["agenda_rows"] = today_next_rows(state, reference, colors)
    context["calendar_note"] = block_note(state.block("calendar", CalendarBlock).status, "calendar")
    context["providers"] = ai_capacity_rows(state)
    context["usage_note"] = block_note(state.block("ai_usage", AIUsageBlock).status, "AI quota")
    context["ai_note"] = brief_note(state)
    context["priorities_stale"] = tasks_stale(state, settings, reference)
    context["capacity_stale"] = ai_usage_stale(state, settings, reference)
    return context


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
    if not state.block("ai_usage", AIUsageBlock).usable:
        return []
    rows: list[dict[str, Any]] = []
    for provider in state.block("ai_usage", AIUsageBlock).providers[:2]:
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
    brief = state.block("brief", BriefBlock).brief
    if not state.block("brief", BriefBlock).usable or brief is None:
        return {"text": f"AI brief {UNAVAILABLE}", "accent": "black"}
    text = brief.note or brief.headline
    if not text:
        return {"text": "AI brief has no note", "accent": "black"}
    return {"text": text, "accent": "black"}


def today_flag(state: DashboardState, settings: "HubSettings") -> bool:
    """Today draws AI capacity, the brief note and the priorities, so any of
    the three going stale is Today's problem."""
    reference = page_reference(state)
    return bool(
        ai_usage_stale(state, settings, reference)
        or brief_stale(state, settings, reference)
        or tasks_stale(state, settings, reference)
    )


__all__ = [
    "AGENDA_ROW_HEIGHT_PX",
    "AGENDA_ROW_LIMIT",
    "AGENDA_TITLE_AVAILABLE_PX",
    "AGENDA_TITLE_MAX_CHARS",
    "PRIORITY_TITLE_AVAILABLE_PX",
    "PRIORITY_TITLE_CHAR_PX",
    "PRIORITY_TITLE_MAX_CHARS",
    "TODAY_CHIP_WIDTH_PX",
    "TODAY_LEFT_HEIGHT_PX",
    "TODAY_TITLE_AND_CHIP_PX",
    "ai_capacity_rows",
    "brief_note",
    "percent_accent",
    "priority_title_budget",
    "task_accent",
    "today_context",
    "today_flag",
    "today_next_rows",
    "today_priority_tasks",
    "upcoming_events",
]
