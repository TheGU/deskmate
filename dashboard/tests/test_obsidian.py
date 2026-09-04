"""The Obsidian reader: both task dialects, read-only, on a temporary vault.

The Tasks-plugin dialect uses emoji markers. They are imported from the parser
rather than typed here, so this file stays plain ASCII while still exercising
the real characters that appear in a vault.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.adapters.tasks import (
    EMOJI_DONE,
    EMOJI_DUE,
    EMOJI_PRIORITY,
    EMOJI_RECUR,
    ObsidianTasksAdapter,
    parse_task_line,
    read_vault,
)
from app.config import Settings
from app.models import Priority
from tests.conftest import run

HIGH = next(char for char, value in EMOJI_PRIORITY.items() if value is Priority.HIGH)
MEDIUM = next(char for char, value in EMOJI_PRIORITY.items() if value is Priority.MEDIUM)
LOW = next(char for char, value in EMOJI_PRIORITY.items() if value is Priority.LOW)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "projects").mkdir(parents=True)
    (root / ".obsidian").mkdir()

    (root / "inbox.md").write_text(
        "\n".join(
            [
                "# Inbox",
                "",
                "- [ ] Send the vendor quote (due:: 2026-09-05) [priority:: high]",
                "- [ ] Book the dentist [priority:: low]",
                "- [x] Draft retro notes (due:: 2026-09-03)",
                "- [-] Cancelled idea (due:: 2026-09-03)",
                "Some prose that is not a task.",
            ]
        ),
        encoding="utf-8",
    )
    (root / "projects" / "deskmate.md").write_text(
        "\n".join(
            [
                "## Deskmate",
                "",
                f"- [ ] Review PR 482 auth refactor {EMOJI_DUE} 2026-09-06 {HIGH} #code",
                f"* [ ] Water the monstera {EMOJI_RECUR} every week {EMOJI_DUE} 2026-09-07 {LOW}",
                f"- [x] Order the panel {EMOJI_DONE} 2026-09-01 {MEDIUM}",
            ]
        ),
        encoding="utf-8",
    )
    (root / ".obsidian" / "workspace.md").write_text("- [ ] Ignore me", encoding="utf-8")
    return root


def test_dataview_dialect(vault: Path) -> None:
    tasks = read_vault(vault, "**/*.md")
    quote = next(task for task in tasks if task.title.startswith("Send the vendor quote"))
    assert quote.due == date(2026, 9, 5)
    assert quote.priority is Priority.HIGH
    assert quote.completed is False
    assert quote.source == "obsidian"
    assert quote.title == "Send the vendor quote"


def test_tasks_plugin_emoji_dialect(vault: Path) -> None:
    tasks = read_vault(vault, "**/*.md")
    review = next(task for task in tasks if task.title.startswith("Review PR 482"))
    assert review.due == date(2026, 9, 6)
    assert review.priority is Priority.HIGH
    assert review.tags == ["code"]
    # The emoji never survive into the model.
    assert review.title == "Review PR 482 auth refactor"
    assert all(ord(char) < 0x2000 for char in review.title)


def test_recurrence_text_is_dropped(vault: Path) -> None:
    tasks = read_vault(vault, "**/*.md")
    monstera = next(task for task in tasks if task.title.startswith("Water the monstera"))
    assert monstera.title == "Water the monstera"
    assert monstera.due == date(2026, 9, 7)
    assert monstera.priority is Priority.LOW


def test_completed_and_cancelled_marks(vault: Path) -> None:
    tasks = read_vault(vault, "**/*.md")
    titles = {task.title for task in tasks}
    assert "Draft retro notes" in titles
    assert next(task for task in tasks if task.title == "Draft retro notes").completed is True
    assert "Cancelled idea" not in titles


def test_dot_directories_are_skipped(vault: Path) -> None:
    tasks = read_vault(vault, "**/*.md")
    assert "Ignore me" not in {task.title for task in tasks}


def test_ids_are_stable_across_reads(vault: Path) -> None:
    first = {task.id for task in read_vault(vault, "**/*.md")}
    second = {task.id for task in read_vault(vault, "**/*.md")}
    assert first == second
    # Five open plus one completed; the cancelled line is dropped.
    assert len(first) == 6


def test_reading_the_vault_does_not_modify_it(vault: Path) -> None:
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(vault.rglob("*.md"))
    }
    read_vault(vault, "**/*.md")
    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(vault.rglob("*.md"))
    }
    assert before == after


def test_adapter_uses_the_configured_vault(settings: Settings, vault: Path) -> None:
    adapter = ObsidianTasksAdapter(
        settings.model_copy(update={"obsidian_vault_path": vault, "tasks_source": "obsidian"})
    )
    tasks = run(adapter.fetch())
    assert tasks
    assert all(task.source == "obsidian" for task in tasks)


def test_glob_can_narrow_the_scan(settings: Settings, vault: Path) -> None:
    tasks = read_vault(vault, "projects/*.md")
    assert {task.title for task in tasks} == {
        "Review PR 482 auth refactor",
        "Water the monstera",
        "Order the panel",
    }


@pytest.mark.parametrize(
    "line",
    [
        "not a task",
        "- [ ]",
        "  - [ ]    ",
        "# heading",
        "- just a bullet",
    ],
)
def test_non_task_lines_are_ignored(line: str) -> None:
    assert parse_task_line(line, source_id="x:1") is None


def test_priority_words_are_case_insensitive() -> None:
    task = parse_task_line("- [ ] Thing [priority:: HIGH]", source_id="x:1")
    assert task is not None
    assert task.priority is Priority.HIGH
