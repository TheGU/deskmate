"""Brief page tests: moved out of tests/test_view.py and tests/test_pages.py in
2.2 (docs/plan/2026-09-19-settings-modules-provisioning.md)."""

from __future__ import annotations

from datetime import datetime, timedelta


from app.settings import HubSettings
from app.models import (
    AdapterStatus,
    Brief,
    BriefBlock,
    BriefMode,
    BriefSection,
    DashboardState,
    Priority,
    Task,
    TasksBlock,
)
from app.timeutil import zone
from app.modules.brief.page import (
    BRIEF_CHIP_WIDTH_PX,
    BRIEF_TITLE_MAX_CHARS,
    brief_context,
    brief_due_label,
    brief_flag,
    brief_headline_fits_one_line,
    brief_is_risk,
    brief_lines,
    brief_task_rows,
    brief_title_budget,
    priority_tasks,
)
from tests.conftest import make_state
from tests.test_view import TODAY, NOW, _empty_state_with


from app.renderer.render import Renderer
from tests.conftest import run
from tests.test_pages import _VIEWPORT_OVERFLOW, _widest_header_state


def test_priority_tasks_skips_completed_and_respects_the_limit(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            items=[
                Task(id="1", title="One", due=TODAY, priority=Priority.HIGH),
                Task(id="2", title="Two", due=TODAY, priority=Priority.HIGH),
                Task(id="3", title="Three", due=TODAY, priority=Priority.HIGH),
                Task(id="4", title="Four", due=TODAY, priority=Priority.HIGH),
                Task(id="5", title="Done", due=TODAY, completed=True),
            ],
        ),
    )
    rows = priority_tasks(state, TODAY, hub_settings.tasks.max_priority_tasks)
    assert len(rows) == 3
    assert "Done" not in [row["title"] for row in rows]


def test_brief_due_label_drops_the_due_word() -> None:
    """Brief's due column is a fixed 84 px with no room for the "DUE " word;
    every other case matches due_label exactly."""
    assert brief_due_label(None, TODAY) == ""
    assert brief_due_label(TODAY, TODAY) == "TODAY"
    assert brief_due_label(TODAY - timedelta(days=1), TODAY) == "1D LATE"
    assert brief_due_label(TODAY + timedelta(days=1), TODAY) == "SAT"
    assert brief_due_label(TODAY + timedelta(days=5), TODAY) == "09 SEP"
    assert "DUE" not in brief_due_label(TODAY + timedelta(days=5), TODAY)


def _brief_state(brief: Brief | None, *, status: AdapterStatus = AdapterStatus.OK) -> DashboardState:
    tz = zone("Asia/Bangkok")
    return make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=tz),
        timezone="Asia/Bangkok",
        brief=BriefBlock(status=status, brief=brief),
    )


def test_brief_mode_label_and_generated_time(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    brief = Brief(
        mode=BriefMode.MORNING,
        generated_at=datetime(2026, 9, 4, 7, 40, tzinfo=tz),
        headline="Two hard deadlines today",
    )
    context = brief_context(_brief_state(brief), hub_settings)
    assert context["mode_label"] == "MORNING BRIEF"
    assert context["generated_label"] == "GENERATED 07:40"


def test_brief_generated_label_is_not_generated_when_missing(hub_settings: HubSettings) -> None:
    brief = Brief(mode=BriefMode.EVENING, generated_at=None, headline="x")
    context = brief_context(_brief_state(brief), hub_settings)
    assert context["generated_label"] == "NOT GENERATED"


def test_brief_risk_sections_get_flagged(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    brief = Brief(
        mode=BriefMode.MORNING,
        generated_at=datetime(2026, 9, 4, 7, 0, tzinfo=tz),
        headline="x",
        sections=[
            BriefSection(title="At risk", items=["Vendor quote overdue"]),
            BriefSection(title="Key tasks", items=["Finish deck"]),
        ],
    )
    context = brief_context(_brief_state(brief), hub_settings)
    headers = [line for line in context["lines"] if line["kind"] == "header"]
    assert headers[0]["risk"] is True
    assert headers[1]["risk"] is False
    assert brief_is_risk("At risk") is True
    assert brief_is_risk("Key tasks") is False


def test_brief_clipping_produces_an_ellipsis_line() -> None:
    sections = [{"title": "SECTION", "risk": False, "items": [f"Item {n}" for n in range(20)]}]
    lines, truncated = brief_lines(sections, max_lines=5)
    assert truncated is True
    assert len(lines) == 5
    assert lines[-1]["kind"] == "item"
    assert lines[-1]["text"].endswith("...")


def test_brief_lines_never_cuts_leaving_a_bare_header() -> None:
    sections = [
        {"title": "FIRST", "risk": False, "items": ["one", "two"]},
        {"title": "SECOND", "risk": False, "items": ["three"]},
    ]
    # Exactly enough room for the first section's header and items, nothing
    # from the second: the cut must drop the bare "SECOND" header too.
    lines, truncated = brief_lines(sections, max_lines=3)
    assert truncated is True
    assert [line["kind"] for line in lines] == ["header", "item", "item"]


def test_brief_unavailable_message_when_brief_is_missing(hub_settings: HubSettings) -> None:
    context = brief_context(_brief_state(None, status=AdapterStatus.UNAVAILABLE), hub_settings)
    assert context["brief_available"] is False
    assert context["unavailable_message"] == "No brief from the PC yet"


def test_brief_headline_fits_one_line_thresholds() -> None:
    assert brief_headline_fits_one_line("Rent due Friday") is True
    assert brief_headline_fits_one_line("Two hard deadlines today, storms from 15:00") is False


def test_brief_context_headline_drops_to_24px_when_it_does_not_fit_one_line(
    hub_settings: HubSettings,
) -> None:
    tz = zone("Asia/Bangkok")
    long_headline = "Two hard deadlines today, storms from 15:00"
    brief = Brief(mode=BriefMode.MORNING, generated_at=datetime(2026, 9, 4, 7, 0, tzinfo=tz), headline=long_headline)
    context = brief_context(_brief_state(brief), hub_settings)
    assert context["headline_large"] is False

    short_headline = "Rent due Friday"
    brief = Brief(mode=BriefMode.MORNING, generated_at=datetime(2026, 9, 4, 7, 0, tzinfo=tz), headline=short_headline)
    context = brief_context(_brief_state(brief), hub_settings)
    assert context["headline_large"] is True


def test_brief_task_rows_single_line_for_short_titles() -> None:
    tasks = [{"title": "Ship it"}, {"title": "Call mom"}, {"title": "Water plants"}]
    rows, more = brief_task_rows(tasks)
    assert more == 0
    assert len(rows) == 3
    assert all(row["two_line"] is False for row in rows)


def test_brief_task_rows_wraps_only_the_titles_that_need_it() -> None:
    """Each row decides for itself: a short title stays single-line even
    beside a long one that needs two, unlike the old all-or-nothing switch."""
    tasks = [
        {"title": "Ship it", "chip_kind": "day"},
        {"title": "Submit August expense claim (BTS and Grab)", "chip_kind": "overdue"},
    ]
    rows, more = brief_task_rows(tasks)
    assert more == 0
    assert rows[0]["two_line"] is False
    assert rows[0]["title"] == "Ship it"
    assert rows[1]["two_line"] is True
    assert rows[1]["title"] != tasks[1]["title"]


def test_brief_task_rows_reports_the_hidden_count_when_the_column_is_full() -> None:
    """Seven real, long task titles (the fixture's own) do not all fit two
    lines each in the column: the ones that do not fit are folded into a
    "+N MORE" count instead of overflowing or shrinking rows further."""
    long_titles = [
        "Finish Q3 OKR review deck for Monday",
        "Send vendor quote answer to K. Somchai",
        "Review PR 482 auth refactor",
        "Submit August expense claim (BTS and Grab)",
        "Renew work permit documents at HR",
        "Back up NAS photo library to cold storage",
        "Book dentist appointment near Asok",
    ]
    tasks = [{"title": title} for title in long_titles]
    rows, more = brief_task_rows(tasks)
    assert len(rows) + more == len(tasks)
    assert more > 0
    for row, title in zip(rows, long_titles):
        if row["title"] != title:
            # Never a mid-word stem: the kept text plus ellipsis is a clean
            # prefix of the real title.
            body = row["title"][:-1]
            assert title.startswith(body)


def test_brief_title_budget_ordered_by_chip_width() -> None:
    kinds = sorted(BRIEF_CHIP_WIDTH_PX, key=lambda kind: BRIEF_CHIP_WIDTH_PX[kind], reverse=True)
    budgets = [brief_title_budget(kind) for kind in kinds]
    assert budgets == sorted(budgets)
    assert min(budgets) >= BRIEF_TITLE_MAX_CHARS


def test_brief_flag_is_set_when_tasks_is_stale(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(tasks=TasksBlock(status=AdapterStatus.OK, source="file", received_at=old))
    assert brief_flag(state, hub_settings) is True


def test_brief_context_carries_the_stale_label(hub_settings: HubSettings) -> None:
    old = NOW - timedelta(hours=11)
    state = _empty_state_with(
        brief=BriefBlock(
            status=AdapterStatus.OK,
            source="file",
            brief=Brief(headline="x", generated_at=old, source="file"),
        )
    )
    context = brief_context(state, hub_settings)
    assert context["brief_stale"] == "11 H AGO"


def test_brief_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("brief", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_brief_task_titles_never_truncate_mid_word(renderer: Renderer, state: DashboardState) -> None:
    """Every ``.brief-task-title`` either reads the fixture's real title in
    full or ends in the single ellipsis character :func:`clip_words` uses;
    the CSS ellipsis is a safety net only, so it should not be the one
    actually firing here."""
    fixture_titles = {task.title for task in state.block("tasks", TasksBlock).items}
    rows = run(
        renderer.probe(
            "brief",
            state,
            """Array.from(document.querySelectorAll('.brief-task-title')).map(
                 e => [e.textContent, e.scrollWidth, e.clientWidth]
               )""",
        )
    )
    for text, scroll_width, client_width in rows:
        if text.endswith("…"):
            continue
        assert text in fixture_titles, f"unexpected title text: {text}"
        assert scroll_width <= client_width + 1, f"{text} truncated without an ellipsis"
