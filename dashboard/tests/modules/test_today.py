"""Today page tests: moved out of tests/test_view.py and tests/test_pages.py in
2.2 (docs/plan/2026-09-19-settings-modules-provisioning.md)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.settings import HubSettings
from app.models import (
    AdapterStatus,
    AIUsage,
    AIUsageBlock,
    Brief,
    BriefBlock,
    CalendarBlock,
    DashboardState,
    Event,
    Priority,
    Task,
    TasksBlock,
)
from app.timeutil import zone
from app.view import (
    build_context,
)
from app.modules.today.page import (
    PRIORITY_TITLE_MAX_CHARS,
    TODAY_CHIP_WIDTH_PX,
    ai_capacity_rows,
    percent_accent,
    priority_title_budget,
    today_context,
    today_flag,
    today_next_rows,
    today_priority_tasks,
    upcoming_events,
)
from tests.conftest import make_state
from tests.test_view import TODAY, NOW, _empty_state_with


from app.renderer.palette import DISPLAY_SIZE, assert_palette, palette_violations
from app.renderer.render import Renderer
from tests.conftest import open_png, run, with_blocks
from tests.test_pages import _VIEWPORT_OVERFLOW, _widest_header_state


def test_today_priority_tasks_shows_weekday_for_tomorrow(hub_settings: HubSettings) -> None:
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[Task(id="1", title="Ship it", due=TODAY + timedelta(days=1), priority=Priority.HIGH)],
        ),
    )
    rows = today_priority_tasks(state, TODAY, hub_settings.tasks.max_priority_tasks)
    assert rows[0]["due_label"] == "SAT"
    assert "TOMORROW" not in rows[0]["due_label"]


def test_upcoming_events_labels_and_skips_the_past() -> None:
    tz = zone("Asia/Bangkok")
    now = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(
            status=AdapterStatus.OK,
            items=[
                Event(id="past", title="Standup", start=datetime(2026, 9, 4, 9, 30, tzinfo=tz)),
                Event(id="later", title="Demo", start=datetime(2026, 9, 4, 16, 30, tzinfo=tz)),
                Event(id="next", title="Drill", start=datetime(2026, 9, 5, 18, 0, tzinfo=tz)),
                Event(
                    id="allday",
                    title="Offsite",
                    start=datetime(2026, 9, 10, 0, 0, tzinfo=tz),
                    all_day=True,
                ),
            ],
        ),
    )
    rows = upcoming_events(state, now, 5)
    assert [row["title"] for row in rows] == ["Demo", "Drill", "Offsite"]
    assert rows[0]["when"] == "16:30"
    assert rows[1]["when"] == "SAT 18:00"
    assert "TOMORROW" not in rows[1]["when"]
    assert rows[2]["when"] == "THU"


def test_ai_capacity_rows_show_unavailable_when_collection_failed() -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(provider="Claude", short_window_percent_remaining=8, collection_status="ok"),
                AIUsage(
                    provider="Codex",
                    short_window_percent_remaining=99,
                    collection_status="error",
                ),
            ],
        ),
    )
    rows = ai_capacity_rows(state)
    claude, codex = rows[0]["windows"][0], rows[1]["windows"][0]
    # The numeral is bare (no percent sign baked in: the template draws that
    # separately, raised); the caption beneath names the window.
    assert claude["value"] == "8"
    assert claude["accent"] == "red"
    assert claude["available"] is True
    assert claude["label"] == "5H"
    # A failed collection is never displayed as a number; the hatch flag
    # takes its place instead, but the caption (and any reset time) stays.
    assert codex["value"] is None
    assert codex["available"] is False


def test_ai_capacity_rows_5h_label_carries_the_reset_time() -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="Claude",
                    short_window_percent_remaining=72,
                    short_window_reset_at=datetime(2026, 9, 4, 21, 30, tzinfo=tz),
                    weekly_percent_remaining=48,
                    collection_status="ok",
                )
            ],
        ),
    )
    windows = ai_capacity_rows(state)[0]["windows"]
    assert windows[0]["label"] == "5H RESET 21:30"
    # The 7D window never carries a reset time, only its own bare label.
    assert windows[1]["label"] == "7D"


def test_ai_capacity_rows_5h_label_is_bare_without_a_reset_time() -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="Claude",
                    short_window_percent_remaining=72,
                    short_window_reset_at=None,
                    collection_status="ok",
                )
            ],
        ),
    )
    windows = ai_capacity_rows(state)[0]["windows"]
    assert windows[0]["label"] == "5H"


def test_ai_capacity_rows_meter_fraction_is_the_used_share() -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="Claude",
                    short_window_percent_remaining=72,
                    weekly_percent_remaining=48,
                    collection_status="ok",
                )
            ],
        ),
    )
    windows = ai_capacity_rows(state)[0]["windows"]
    assert windows[0]["fraction"] == pytest.approx(0.28)
    assert windows[1]["fraction"] == pytest.approx(0.52)


def test_percent_accents_never_go_green() -> None:
    """Plenty of quota left is not news, and colouring the healthy case
    teaches the eye to ignore colour."""
    assert percent_accent(90, True) == "black"
    assert percent_accent(30, True) == "yellow"
    assert percent_accent(5, True) == "red"
    assert percent_accent(None, True) == "black"
    assert percent_accent(90, False) == "black"


def test_today_next_rows_orders_future_events_and_labels_by_day() -> None:
    """Today's own remaining events first with a bare time, then later days
    with their 3-letter weekday; the word TOMORROW never appears."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="past", title="Standup", start=datetime(2026, 9, 4, 9, 30, tzinfo=tz)),
            Event(id="later-today", title="Demo", start=datetime(2026, 9, 4, 16, 30, tzinfo=tz)),
            Event(id="tomorrow", title="Drill", start=datetime(2026, 9, 5, 18, 0, tzinfo=tz)),
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert [row["title"] for row in rows] == ["Demo", "Drill"]
    assert rows[0]["when"] == "16:30"
    assert rows[1]["when"] == "SAT 18:00"
    assert "TOMORROW" not in rows[1]["when"]


def test_today_next_rows_all_day_variants() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(
                id="today-allday",
                title="Offsite",
                start=datetime(2026, 9, 4, 0, 0, tzinfo=tz),
                end=datetime(2026, 9, 5, 0, 0, tzinfo=tz),
                all_day=True,
            ),
            Event(
                id="later-allday",
                title="Conference",
                start=datetime(2026, 9, 6, 0, 0, tzinfo=tz),
                all_day=True,
            ),
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert rows[0]["when"] == "ALL DAY"
    assert rows[1]["when"] == "SUN"


def test_today_next_rows_caps_at_the_row_limit() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 6, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id=str(n), title=f"Event {n}", start=datetime(2026, 9, 4, 7 + n, 0, tzinfo=tz))
            for n in range(8)
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {}, limit=3)
    assert len(rows) == 3
    assert [row["title"] for row in rows] == ["Event 0", "Event 1", "Event 2"]


def test_today_next_rows_empty_calendar_gives_no_rows() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 8, 0, tzinfo=tz)
    state = make_state(generated_at=reference, timezone="Asia/Bangkok")
    assert today_next_rows(state, reference, {}) == []


def test_today_next_rows_shows_placeholder_when_nothing_left_today() -> None:
    """When no remaining event falls today, the first row is a TODAY /
    Nothing left today placeholder, then future days follow."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 20, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id="past", title="Standup", start=datetime(2026, 9, 4, 9, 30, tzinfo=tz)),
            Event(id="tomorrow", title="Drill", start=datetime(2026, 9, 5, 18, 0, tzinfo=tz)),
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert rows[0] == {"when": "TODAY", "title": "Nothing left today", "color": "black"}
    assert rows[1]["title"] == "Drill"
    assert rows[1]["when"] == "SAT 18:00"


def test_today_next_rows_no_placeholder_when_today_has_events() -> None:
    """When today still has a remaining event, no placeholder row appears."""
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 12, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[Event(id="later-today", title="Demo", start=datetime(2026, 9, 4, 16, 30, tzinfo=tz))],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {})
    assert all(row["title"] != "Nothing left today" for row in rows)
    assert rows[0]["title"] == "Demo"


def test_today_next_rows_placeholder_counts_against_the_row_limit() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 20, 0, tzinfo=tz)
    events = CalendarBlock(
        status=AdapterStatus.OK,
        items=[
            Event(id=str(n), title=f"Event {n}", start=datetime(2026, 9, 5, 7 + n, 0, tzinfo=tz))
            for n in range(5)
        ],
    )
    state = make_state(generated_at=reference, timezone="Asia/Bangkok", calendar=events)
    rows = today_next_rows(state, reference, {}, limit=3)
    assert len(rows) == 3
    assert rows[0]["title"] == "Nothing left today"
    assert [row["title"] for row in rows[1:]] == ["Event 0", "Event 1"]


def test_ai_capacity_accent_never_goes_green(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="Claude",
                    short_window_percent_remaining=90,
                    weekly_percent_remaining=8,
                    collection_status="ok",
                )
            ],
        ),
    )
    context = build_context("today", state, hub_settings)
    windows = context["providers"][0]["windows"]
    assert windows[0]["accent"] == "black"
    assert windows[1]["accent"] == "red"


def test_priority_title_budget_ordered_by_chip_width() -> None:
    """A wider chip always leaves a title the same size or narrower budget
    than a lighter one; the widest chip's budget never dips below the old
    uniform floor."""
    kinds = sorted(TODAY_CHIP_WIDTH_PX, key=lambda kind: TODAY_CHIP_WIDTH_PX[kind], reverse=True)
    budgets = [priority_title_budget(kind) for kind in kinds]
    assert budgets == sorted(budgets)
    assert min(budgets) >= PRIORITY_TITLE_MAX_CHARS


def test_today_priorities_title_beside_a_weekday_chip_is_not_clipped(hub_settings: HubSettings) -> None:
    """The fixture's own regression: a title that stemmed beside a narrow
    "MON" chip under the old uniform budget now reads whole, because that
    row's own (lighter) chip hands its title the extra room."""
    today = date(2026, 9, 4)
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[
                Task(
                    id="1",
                    title="Review PR 482 auth refactor",
                    due=today + timedelta(days=1),
                    priority=Priority.HIGH,
                )
            ],
        ),
    )
    rows = today_priority_tasks(state, today, 5)
    assert rows[0]["title"] == "Review PR 482 auth refactor"


# ---------------------------------------------------------------------------
# system page
# ---------------------------------------------------------------------------
def test_today_flag_is_set_when_ai_usage_is_stale(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(hours=7)
    state = _empty_state_with(
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            source="file",
            providers=[AIUsage(provider="claude", collected_at=old)],
        )
    )
    assert today_flag(state, hub_settings) is True


def test_today_flag_is_set_when_the_brief_is_stale(hub_settings: HubSettings) -> None:
    """Today draws the brief note too (view.py:brief_note), so a stale brief
    must flag Today's own footer entry, not only Brief's."""
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(
        brief=BriefBlock(
            status=AdapterStatus.OK,
            source="file",
            brief=Brief(headline="x", generated_at=old, source="file"),
        )
    )
    assert today_flag(state, hub_settings) is True


def test_today_context_carries_the_stale_labels(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(hours=7)
    state = _empty_state_with(
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            source="file",
            providers=[AIUsage(provider="claude", collected_at=old)],
        )
    )
    context = today_context(state, hub_settings)
    assert context["capacity_stale"] == "7 H AGO"
    assert context["priorities_stale"] is None


def test_today_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("today", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def _today_state_with_stale_ai_usage_and_tasks(state: DashboardState) -> DashboardState:
    """AI CAPACITY and PRIORITIES both past their staleness threshold
    (AI_USAGE_STALE_SECONDS 21600 s / 6 h, TASKS_STALE_SECONDS 36000 s / 10 h),
    file-sourced so they are eligible to be marked at all (a fixture never
    is). The extra 10 minutes on each offset is a safety margin: the view
    layer's "now" is the newest block.updated_at, which lands a hair before
    ``state.generated_at`` (stamped after every adapter fetch completes), so
    an exact 8h/12h offset can float across the whole-hour floor and flip
    the asserted bucket by one.
    """
    old_usage = state.generated_at - timedelta(hours=8, minutes=10)
    old_tasks = state.generated_at - timedelta(hours=12, minutes=10)
    providers = [p.model_copy(update={"collected_at": old_usage}) for p in state.block("ai_usage", AIUsageBlock).providers]
    return with_blocks(
        state,
        ai_usage=state.block("ai_usage", AIUsageBlock).model_copy(
            update={"source": "file", "providers": providers}
        ),
        tasks=state.block("tasks", TasksBlock).model_copy(
            update={"source": "file", "received_at": old_tasks}
        ),
    )


def test_today_page_fresh_has_no_stale_mark_and_stays_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    """Today, file-sourced but freshly received: no tell-tale, no age text,
    no DEMO (none of Today's three datasets, ai_usage/brief/tasks, is a
    fixture), palette clean, nothing overflowing."""
    # Every age source pinned to now (ai_usage: each provider's collected_at,
    # brief: generated_at, tasks: received_at, see view.py's *_stale). The
    # fixture's own timestamps sit in the morning, so without this the test
    # turned stale, and failed, every afternoon once the 6 h threshold passed.
    now = state.generated_at
    assert state.block("brief", BriefBlock).brief is not None
    usage = state.block("ai_usage", AIUsageBlock)
    brief = state.block("brief", BriefBlock)
    assert brief.brief is not None
    fresh = with_blocks(
        state,
        ai_usage=usage.model_copy(
            update={
                "source": "file",
                "providers": [
                    p.model_copy(update={"collected_at": now}) for p in usage.providers
                ],
            }
        ),
        brief=brief.model_copy(
            update={
                "source": "file",
                "brief": brief.brief.model_copy(update={"generated_at": now}),
            }
        ),
        tasks=state.block("tasks", TasksBlock).model_copy(
            update={"source": "file", "received_at": now}
        ),
    )
    html = renderer.render_html("today", fresh, embed_fonts=False)
    assert 'class="today-stale-age"' not in html
    assert 'class="ftr-demo"' not in html

    image = open_png(run(renderer.render_png("today", fresh)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)
    overflow = run(renderer.probe("today", fresh, _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_today_page_stale_shows_tell_tale_and_age_and_stays_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    """AI CAPACITY and PRIORITIES both carry the yellow tell-tale plus a
    whole-hour age after the label, the footer flags "today", and the page
    is still palette-clean with nothing overflowing."""
    stale = _today_state_with_stale_ai_usage_and_tasks(state)
    html = renderer.render_html("today", stale, embed_fonts=False)
    assert html.count('<span class="today-stale-age">') == 2
    assert "8 H AGO" in html
    assert "12 H AGO" in html

    flagged_names = run(
        renderer.probe(
            "today",
            stale,
            """Array.from(document.querySelectorAll('.win'))
                 .filter(w => w.querySelector('.win-flag'))
                 .map(w => w.textContent.trim())""",
        )
    )
    assert any("TODAY" in name for name in flagged_names), flagged_names

    image = open_png(run(renderer.render_png("today", stale)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)
    overflow = run(renderer.probe("today", stale, _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_today_footer_shows_demo_for_the_default_fixture_state(
    renderer: Renderer, state: DashboardState
) -> None:
    """The shared fixture state's ai_usage/tasks are fixture-sourced (auto
    resolves to fixture with no DATA_DIR pushes), so a fresh install's
    footer must say DEMO rather than pass fixture numbers off as real."""
    assert state.block("ai_usage", AIUsageBlock).source == "fixture"
    html = renderer.render_html("today", state, embed_fonts=False)
    assert '<span class="ftr-demo">DEMO</span>' in html

    image = open_png(run(renderer.render_png("today", state)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)
    overflow = run(renderer.probe("today", state, _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_today_footer_hides_demo_once_every_shown_dataset_is_file_sourced(
    renderer: Renderer, state: DashboardState
) -> None:
    """Today draws all three pushed datasets (ai_usage, brief via the NOTE
    field, and tasks), so DEMO only clears once none of the three is a
    fixture."""
    real = with_blocks(
        state,
        ai_usage=state.block("ai_usage", AIUsageBlock).model_copy(update={"source": "file"}),
        brief=state.block("brief", BriefBlock).model_copy(update={"source": "file"}),
        tasks=state.block("tasks", TasksBlock).model_copy(update={"source": "file"}),
    )
    html = renderer.render_html("today", real, embed_fonts=False)
    assert 'class="ftr-demo"' not in html


def test_today_footer_shows_demo_when_only_the_brief_is_still_a_fixture(
    renderer: Renderer, state: DashboardState
) -> None:
    """Regression: Today draws the brief note (the NOTE field), not just
    ai_usage and tasks, so a fixture-sourced brief alone must still show
    DEMO even when ai_usage and tasks are both file-sourced."""
    mixed = with_blocks(
        state,
        ai_usage=state.block("ai_usage", AIUsageBlock).model_copy(update={"source": "file"}),
        tasks=state.block("tasks", TasksBlock).model_copy(update={"source": "file"}),
    )
    assert mixed.block("brief", BriefBlock).source == "fixture"
    html = renderer.render_html("today", mixed, embed_fonts=False)
    assert '<span class="ftr-demo">DEMO</span>' in html


def test_today_agenda_time_never_touches_the_title(renderer: Renderer, state: DashboardState) -> None:
    """The regression: a full "DAY HH:MM" time label ("MON 18:00") filled
    the fixed time column with no gap to the title beside it."""
    geometry = run(
        renderer.probe(
            "today",
            state,
            """Array.from(document.querySelectorAll('.agenda-row')).map(row => {
                 const t = row.querySelector('.agenda-time').getBoundingClientRect();
                 const title = row.querySelector('.agenda-title').getBoundingClientRect();
                 return {when: row.querySelector('.agenda-time').textContent, gap: title.left - t.right};
               })""",
        )
    )
    assert geometry, "no agenda rows in the fixture"
    for row in geometry:
        assert row["gap"] >= 12, row
