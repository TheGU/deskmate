"""The Brief page's own context builder and the helpers only it needs.

Moved out of ``app/view.py`` in 2.2 (docs/plan/2026-09-19-settings-modules-
provisioning.md): everything here is read by no other page except
``priority_tasks``' shared building blocks (``open_tasks``, ``task_sort_key``,
``due_label``, ``due_chip_kind``, ``task_accent``), which Today also uses and
which stay in ``app/view.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, TYPE_CHECKING

from app import icons
from app.models import BriefBlock, DashboardState, TasksBlock
from app.view import (
    base_context,
    block_note,
    brief_stale,
    clip_words,
    due_chip_kind,
    due_label,
    fmt_time,
    open_tasks,
    page_reference,
    task_accent,
    task_sort_key,
    tasks_stale,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.settings import HubSettings


def priority_tasks(state: DashboardState, today: date, limit: int) -> list[dict[str, Any]]:
    """Brief's own task list rows: same selection as
    :func:`app.modules.today.page.today_priority_tasks`, with
    :func:`brief_due_label` (no "DUE " word) in place of :func:`app.view.
    due_label`. The title itself is clipped later, per row, in
    :func:`brief_task_rows`, which needs ``chip_kind`` to size that row's own
    budget."""
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


def brief_due_label(due: date | None, today: date) -> str:
    """Brief's own compact due text: :func:`app.view.due_label`, with the
    leading "DUE " word dropped ("DUE 08 SEP" alone runs past a legible width
    sooner than the bare date does)."""
    label = due_label(due, today)
    return label[len("DUE ") :] if label.startswith("DUE ") else label


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
#: :func:`app.view.due_chip_kind` can return, at Brief's own smaller 14 px
#: chip override (brief.html: no border, 1/4/2 px padding, 14 px caps at
#: weight 700, 2 px gap; the owner's floor is nothing under 14 px, so this is
#: the smallest this class may ever go). The overdue chip also carries the
#: FLAG glyph. Brief's own date shape has no "DUE " prefix
#: (:func:`brief_due_label` drops it), so unlike Today's table it is
#: genuinely one of the narrower chips.
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
    as the floor for the same reason as :func:`app.modules.today.page.
    priority_title_budget`.
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
    budget is clipped at a word boundary with :func:`app.view.clip_words`,
    using the per-line budget twice over: the icon and the chip are
    identical in both row heights, so the title's real available width does
    not change between them.

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


def brief_context(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    context = base_context(state, settings, "brief")
    today: date = context["today"]
    reference: datetime = context["reference"]
    brief = state.block("brief", BriefBlock).brief
    context["brief_stale"] = brief_stale(state, settings, reference)
    context["brief_note"] = block_note(state.block("brief", BriefBlock).status, "AI brief")
    context["brief_available"] = state.block("brief", BriefBlock).usable and brief is not None
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

    if state.block("tasks", TasksBlock).usable:
        open_count = len(open_tasks(state))
        candidates = priority_tasks(state, today, open_count)
        context["tasks"], context["tasks_more_count"] = brief_task_rows(candidates)
    else:
        context["tasks"] = []
        context["tasks_more_count"] = 0
    context["tasks_open_count"] = len(open_tasks(state)) if state.block("tasks", TasksBlock).usable else 0
    context["tasks_note"] = block_note(state.block("tasks", TasksBlock).status, "tasks")
    return context


def brief_flag(state: DashboardState, settings: "HubSettings") -> bool:
    """The brief page draws the brief and the task list, and nothing else."""
    reference = page_reference(state)
    return bool(
        brief_stale(state, settings, reference) or tasks_stale(state, settings, reference)
    )


__all__ = [
    "BRIEF_BODY_BUDGET_PX",
    "BRIEF_CHIP_WIDTH_PX",
    "BRIEF_HEADLINE_AVAILABLE_PX",
    "BRIEF_HEADLINE_CHAR_PX",
    "BRIEF_HEADLINE_MAX_CHARS_28",
    "BRIEF_LINE_PX",
    "BRIEF_MAX_LINES",
    "BRIEF_MORE_ROW_PX",
    "BRIEF_TASK_LIST_HEIGHT_PX",
    "BRIEF_TASK_ROW_1_PX",
    "BRIEF_TASK_ROW_2_PX",
    "BRIEF_TITLE_AND_CHIP_PX",
    "BRIEF_TITLE_AVAILABLE_PX",
    "BRIEF_TITLE_CHAR_PX",
    "BRIEF_TITLE_MAX_CHARS",
    "BRIEF_UNAVAILABLE_MESSAGE",
    "brief_context",
    "brief_due_label",
    "brief_flag",
    "brief_headline_fits_one_line",
    "brief_is_risk",
    "brief_lines",
    "brief_task_rows",
    "brief_title_budget",
    "priority_tasks",
]
