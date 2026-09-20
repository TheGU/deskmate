"""Agenda page tests: moved out of tests/test_view.py and tests/test_pages.py in
2.2 (docs/plan/2026-09-19-settings-modules-provisioning.md)."""

from __future__ import annotations

from datetime import date, datetime, timedelta


from app.settings import HubSettings
from app.models import (
    AdapterStatus,
    CalendarBlock,
    DashboardState,
    Event,
)
from app.timeutil import zone
from app.view import (
    block_note,
)
from app.modules.agenda.page import (
    AGENDA_LIST_ROW_LIMIT,
    agenda_context,
    agenda_list_rows,
    month_grid,
    next_seven_days,
)
from tests.conftest import make_state


from app.renderer.render import Renderer
from tests.conftest import run
from tests.test_pages import _VIEWPORT_OVERFLOW, _widest_header_state


def test_agenda_list_rows_today_all_day_first_then_time_including_past() -> None:
    """Today's own block: all-day events first, then timed events in start
    order, including ones that have already ended (unlike Today's own
    agenda list, which drops them)."""
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(
                id="a",
                title="Morning standup",
                calendar="work",
                start=datetime(2026, 9, 4, 9, 0, tzinfo=tz),
                end=datetime(2026, 9, 4, 9, 15, tzinfo=tz),
            ),
            Event(
                id="b",
                title="Offsite",
                calendar="personal",
                start=datetime(2026, 9, 4, 0, 0, tzinfo=tz),
                end=datetime(2026, 9, 5, 0, 0, tzinfo=tz),
                all_day=True,
            ),
            Event(
                id="c",
                title="Evening class",
                calendar="personal",
                start=datetime(2026, 9, 4, 19, 0, tzinfo=tz),
            ),
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {"work": "blue", "personal": "green"}, today)
    assert [row["kind"] for row in rows] == ["event", "event", "event"]
    assert rows[0]["when"] == "ALL DAY"
    assert rows[1]["when"] == "09:00"  # already ended by noon, still shown
    assert rows[2]["when"] == "19:00"


def test_agenda_list_rows_inserts_a_divider_once_today_runs_out() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="a", title="Standup", start=datetime(2026, 9, 4, 9, 0, tzinfo=tz)),
            Event(id="b", title="Sprint planning", start=datetime(2026, 9, 5, 9, 30, tzinfo=tz)),
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {}, today)
    assert rows[0]["kind"] == "event"
    # The next day is announced by name, never by its weekday.
    assert rows[1] == {"kind": "divider", "label": "TOMORROW"}
    assert rows[2]["kind"] == "event"
    assert rows[2]["when"] == "09:30"


def test_agenda_list_rows_divider_names_weekday_and_date_for_a_later_day() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)  # a Friday
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[Event(id="a", title="QBR", start=datetime(2026, 9, 7, 13, 0, tzinfo=tz))],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {}, today)
    assert rows[0] == {"kind": "divider", "label": "MON 07 SEP"}
    assert rows[1]["when"] == "13:00"


def test_agenda_list_rows_shows_more_when_the_budget_is_exceeded() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 6, 0, tzinfo=tz)
    total_events = AGENDA_LIST_ROW_LIMIT + 4
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(
                id=f"e{i}",
                title=f"Event {i}",
                start=datetime(2026, 9, 4, 7, 0, tzinfo=tz) + timedelta(minutes=i),
            )
            for i in range(total_events)
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = agenda_list_rows(state, {}, today)
    assert len(rows) == AGENDA_LIST_ROW_LIMIT
    assert rows[-1]["kind"] == "more"
    # N counts only the events cut, not the row it replaces.
    assert rows[-1]["count"] == total_events - AGENDA_LIST_ROW_LIMIT + 1


def test_agenda_list_rows_never_exceeds_the_row_budget() -> None:
    """Many events across many days, so dividers and events both compete for
    the same fixed row budget: the packed list never runs past it."""
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 6, 0, tzinfo=tz)
    items = [
        Event(
            id=f"e{day_offset}-{hour}",
            title=f"Event {day_offset}-{hour}",
            start=datetime(2026, 9, 4, tzinfo=tz) + timedelta(days=day_offset, hours=8 + hour),
        )
        for day_offset in range(10)
        for hour in range(5)
    ]
    state = make_state(
        generated_at=reference,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.OK, items=items),
    )
    rows = agenda_list_rows(state, {}, today)
    assert len(rows) <= AGENDA_LIST_ROW_LIMIT
    assert rows[-1]["kind"] == "more"


def test_agenda_list_rows_nothing_scheduled_when_the_window_is_empty() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 4)
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    unusable = make_state(generated_at=reference, timezone="Asia/Bangkok")
    assert agenda_list_rows(unusable, {}, today) == []
    usable_but_empty = make_state(
        generated_at=reference,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.OK, items=[]),
    )
    assert agenda_list_rows(usable_but_empty, {}, today) == []


def test_agenda_context_calendar_note_is_blank_when_the_calendar_is_just_empty(
    hub_settings: HubSettings,
) -> None:
    """An empty but usable calendar has nothing wrong with it: the note must
    be blank so the template's default "Nothing scheduled" prints."""
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.OK, items=[]),
    )
    context = agenda_context(state, hub_settings)
    assert context["agenda_list"] == []
    assert context["calendar_note"] == ""


def test_agenda_context_calendar_note_explains_an_unusable_calendar(
    hub_settings: HubSettings,
) -> None:
    """When the calendar block itself is not usable (unset, or erroring),
    the empty agenda list must say why instead of the generic "Nothing
    scheduled", which would read as "you have no events" rather than "the
    calendar is not configured"."""
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(status=AdapterStatus.UNAVAILABLE),
    )
    context = agenda_context(state, hub_settings)
    assert context["agenda_list"] == []
    assert context["calendar_note"] == block_note(AdapterStatus.UNAVAILABLE, "calendar")
    assert context["calendar_note"] == "calendar unavailable"


def test_month_grid_starts_monday_and_marks_today() -> None:
    today = date(2026, 9, 5)  # a Saturday; 1 Sep 2026 is a Tuesday
    state = make_state(
        generated_at=datetime(2026, 9, 5, 8, 0, tzinfo=zone("Asia/Bangkok")), timezone="Asia/Bangkok"
    )
    grid = month_grid(state, {}, today)
    assert grid["name"] == "SEPTEMBER"
    first_week = grid["weeks"][0]
    assert first_week[0] is None
    assert first_week[1]["number"] == 1
    flat = [cell for week in grid["weeks"] for cell in week if cell]
    today_cells = [cell for cell in flat if cell["is_today"]]
    assert [cell["number"] for cell in today_cells] == [5]


def test_month_grid_dots_use_calendar_color_and_black_for_multiple() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 5)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="a", title="Solo", calendar="work", start=datetime(2026, 9, 10, 9, 0, tzinfo=tz)),
            Event(id="b", title="One", calendar="work", start=datetime(2026, 9, 12, 9, 0, tzinfo=tz)),
            Event(id="c", title="Two", calendar="personal", start=datetime(2026, 9, 12, 14, 0, tzinfo=tz)),
        ],
    )
    state = make_state(
        generated_at=datetime(2026, 9, 5, 8, 0, tzinfo=tz), timezone="Asia/Bangkok", calendar=events
    )
    grid = month_grid(state, {"work": "blue", "personal": "green"}, today)
    flat = {cell["number"]: cell for week in grid["weeks"] for cell in week if cell}
    assert flat[10]["dot"] == "blue"
    assert flat[12]["dot"] == "black"
    assert flat[11]["dot"] is None


def test_next_seven_days_clamp_busy_bars_and_blank_count() -> None:
    tz = zone("Asia/Bangkok")
    today = date(2026, 9, 5)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            # Starts before 06:00 and runs past 22:00: clamps to a full bar.
            Event(
                id="a",
                title="Overnight",
                start=datetime(2026, 9, 6, 4, 0, tzinfo=tz),
                end=datetime(2026, 9, 6, 23, 0, tzinfo=tz),
            ),
        ],
    )
    state = make_state(
        generated_at=datetime(2026, 9, 5, 8, 0, tzinfo=tz), timezone="Asia/Bangkok", calendar=events
    )
    rows = next_seven_days(state, today)
    assert len(rows) == 7
    tomorrow = rows[0]
    assert tomorrow["segments"] == [{"left": 0.0, "width": 100.0}]
    assert tomorrow["count"] == 1
    # A day with nothing on it prints no count at all, not a zero.
    empty_day = rows[1]
    assert empty_day["segments"] == []
    assert empty_day["count"] is None


# ---------------------------------------------------------------------------
# weather: the hero, the four readings, the hourly plates, the daily rows
# ---------------------------------------------------------------------------


def test_agenda_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("agenda", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow
