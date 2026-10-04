"""Agenda page tests: moved out of tests/test_view.py and tests/test_pages.py in
2.2 (docs/plan/2026-09-19-settings-modules-provisioning.md)."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta

import pytest

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
    assert first_week[0]["date"] == date(2026, 8, 24)
    assert first_week[0]["in_month"] is False
    assert grid["weeks"][1][1]["number"] == 1
    flat = [cell for week in grid["weeks"] for cell in week if cell]
    today_cells = [cell for cell in flat if cell["is_today"]]
    assert [cell["number"] for cell in today_cells] == [5]


def test_month_grid_borders_use_calendar_color_and_black_for_multiple() -> None:
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
    flat = {cell["number"]: cell for week in grid["weeks"] for cell in week if cell["in_month"]}
    assert flat[10]["color"] == "blue"
    assert flat[12]["color"] == "black"
    assert flat[11]["color"] is None


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
    assert tomorrow["segments"] == [{"left": 0.0, "width": 100.0, "color": "black"}]
    assert tomorrow["count"] == 1
    # A day with nothing on it prints no count at all, not a zero.
    empty_day = rows[1]
    assert empty_day["segments"] == []
    assert empty_day["count"] is None


@pytest.mark.parametrize("today", [
    date(2026, 8, 1), date(2026, 5, 31), date(2027, 1, 1),
    date(2026, 12, 31), date(2028, 2, 29), date(2026, 10, 3),
])
def test_month_grid_contains_whole_month_and_three_weeks_of_context(today: date) -> None:
    state = make_state(generated_at=datetime.combine(today, datetime.min.time(), zone("Asia/Bangkok")))
    grid = month_grid(state, {}, today)
    days = [cell["date"] for week in grid["weeks"] for cell in week]
    monday = today - timedelta(days=today.weekday())
    assert days[0].weekday() == 0
    assert days[-1].weekday() == 6
    assert days[0] <= monday - timedelta(days=7)
    assert days[-1] >= monday + timedelta(days=13)
    assert all(right - left == timedelta(days=1) for left, right in zip(days, days[1:]))
    assert [cell["number"] for week in grid["weeks"] for cell in week if cell["is_today"]] == [today.day]
    assert {cell["date"].month for week in grid["weeks"] for cell in week if cell["in_month"]} == {today.month}
    assert [day.day for day in days if day.year == today.year and day.month == today.month] == list(
        range(1, monthrange(today.year, today.month)[1] + 1)
    )
    assert 4 <= len(grid["weeks"]) <= 7


def test_adjacent_month_events_keep_their_color() -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2027, 1, 1, 8, tzinfo=tz),
        calendar=CalendarBlock(status=AdapterStatus.OK, items=[
            Event(id="previous", title="Review", calendar="work", start=datetime(2026, 12, 31, 9, tzinfo=tz)),
        ]),
    )
    cells = [cell for week in month_grid(state, {"work": "red"}, date(2027, 1, 1))["weeks"] for cell in week]
    previous = next(cell for cell in cells if cell["date"] == date(2026, 12, 31))
    assert previous["in_month"] is False
    assert previous["color"] == "red"


def test_next_seven_days_separates_all_day_marks_from_colored_timed_slots() -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 10, 3, 8, tzinfo=tz),
        calendar=CalendarBlock(status=AdapterStatus.OK, items=[
            Event(id="all-day", title="Offsite", calendar="work", all_day=True,
                  start=datetime(2026, 10, 4, tzinfo=tz), end=datetime(2026, 10, 5, tzinfo=tz)),
            Event(id="timed", title="Review", calendar="personal",
                  start=datetime(2026, 10, 4, 8, tzinfo=tz), end=datetime(2026, 10, 4, 12, tzinfo=tz)),
        ]),
    )
    rows = next_seven_days(state, date(2026, 10, 3), {"work": "blue", "personal": "red"})
    assert rows[0] == {
        "label": "S 04", "count": 2, "all_day_colors": ["blue"],
        "segments": [{"left": 12.5, "width": 25.0, "color": "red"}],
    }
    assert rows[1]["all_day_colors"] == []


# ---------------------------------------------------------------------------
# weather: the hero, the four readings, the hourly plates, the daily rows
# ---------------------------------------------------------------------------


def test_agenda_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("agenda", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def _agenda_preview_state(state: DashboardState, today: date) -> DashboardState:
    """Dense bilingual events exercise both month-edge layout and slot colors."""
    reference = datetime.combine(today, datetime.min.time(), zone("Asia/Bangkok")) + timedelta(hours=8)
    blocks = {name: block.model_copy(update={"updated_at": reference}) for name, block in state.blocks.items()}
    events = []
    for offset in range(-7, 22):
        day = reference.replace(hour=0) + timedelta(days=offset)
        events.append(Event(
            id=f"timed-{offset}", title="Project review and weekly planning", calendar="work",
            start=day + timedelta(hours=9), end=day + timedelta(hours=10),
        ))
        if offset % 2 == 0:
            events.append(Event(
                id=f"evening-{offset}", title="ประชุมทีมและติดตามงานประจำสัปดาห์", calendar="personal",
                start=day + timedelta(hours=18), end=day + timedelta(hours=20),
            ))
        if offset % 3 == 1:
            events.append(Event(
                id=f"all-day-{offset}", title="นัดหมายแพทย์และตรวจสุขภาพ", calendar="personal",
                start=day, end=day + timedelta(days=1), all_day=True,
            ))
    blocks["calendar"] = CalendarBlock(status=AdapterStatus.OK, updated_at=reference, items=events)
    return state.model_copy(update={"generated_at": reference, "blocks": blocks})


@pytest.mark.parametrize("today", [date(2026, 8, 1), date(2026, 5, 31), date(2026, 10, 3)])
def test_agenda_month_edges_and_all_day_labels_fit(
    renderer: Renderer, state: DashboardState, today: date,
) -> None:
    dense = _agenda_preview_state(_widest_header_state(state), today)
    assert run(renderer.probe("agenda", dense, _VIEWPORT_OVERFLOW)) == []
    measurements = run(renderer.probe("agenda", dense, """() => {
      const body = document.querySelector('.body').getBoundingClientRect();
      const right = document.querySelector('.ag-right');
      const times = [...document.querySelectorAll('.ag-time')].filter(e => e.textContent.trim() === 'ALL DAY');
      return {
        timeBoxes: times.map(e => ({width:e.clientWidth, contentWidth:e.scrollWidth,
          height:e.clientHeight, lineCount:(() => {const r=document.createRange();r.selectNodeContents(e);return r.getClientRects().length;})()})),
        rightBottom: Math.max(...[...right.children].map(e => e.getBoundingClientRect().bottom)),
        bodyBottom: body.bottom,
        headMargin:getComputedStyle(document.querySelector('.ag-head')).marginBottom,
        dividerHeight:document.querySelector('.ag-divider').getBoundingClientRect().height,
        tickCount:document.querySelectorAll('.next7-tick').length,
        tickLabels:[...document.querySelectorAll('.next7-axis-label')].map(e=>e.textContent.trim()),
        allDayMarks:document.querySelectorAll('.next7-all-day-mark').length,
        segmentColors:[...document.querySelectorAll('.next7-seg')].map(e=>getComputedStyle(e).backgroundColor)
      };
    }"""))
    assert measurements["timeBoxes"]
    assert all(box["contentWidth"] <= box["width"] and box["lineCount"] == 1 for box in measurements["timeBoxes"])
    assert measurements["rightBottom"] <= measurements["bodyBottom"]
    assert measurements["headMargin"] == "0px"
    assert measurements["dividerHeight"] == 20
    assert measurements["tickCount"] == 21
    assert measurements["tickLabels"] == ["08", "12", "18"]
    assert measurements["allDayMarks"] > 0
    assert "rgb(0, 0, 255)" in measurements["segmentColors"]
    assert "rgb(0, 255, 0)" in measurements["segmentColors"]
