"""Task adapters: fixtures and a read-only Obsidian Markdown reader.

The Obsidian reader understands two task dialects that appear in real vaults:

* Dataview inline fields::

      - [ ] Send the vendor quote (due:: 2026-09-05) [priority:: high]

* The Tasks plugin emoji dialect (the emoji live in the vault, never in our
  output)::

      - [ ] Send the vendor quote <due emoji> 2026-09-05 <high emoji>

Both dialects may appear in the same file. The vault is opened read only; the
adapter never writes to it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from app.adapters.base import AdapterUnavailable, received_at_or_mtime
from app.adapters.fixtures import day_delta, load_fixture, shift_iso
from app.config import Settings
from app.logging_setup import log
from app.models import Priority, Task
from app.timeutil import today_local

logger = logging.getLogger("app.adapters.tasks")

#: ``- [ ] title`` / ``* [x] title`` with any indentation.
TASK_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*[-*+]\s+\[(?P<mark>.)\]\s+(?P<body>.*\S)\s*$")

#: Dataview inline field, either ``(key:: value)`` or ``[key:: value]``.
INLINE_FIELD: Final[re.Pattern[str]] = re.compile(
    r"[\(\[](?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*::\s*(?P<value>[^\)\]]*)[\)\]]"
)

TAG: Final[re.Pattern[str]] = re.compile(r"(?<![\w/])#([A-Za-z0-9][\w/-]*)")

ISO_DATE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")

# Tasks-plugin emoji, referenced by codepoint so this file stays ASCII.
EMOJI_DUE: Final[str] = "\U0001F4C5"  # calendar, due date
EMOJI_SCHEDULED: Final[str] = "\u23F3"  # hourglass, scheduled date
EMOJI_START: Final[str] = "\U0001F6EB"  # departing plane, start date
EMOJI_DONE: Final[str] = "\u2705"  # check mark, done date
EMOJI_CREATED: Final[str] = "\u2795"  # heavy plus, created date
EMOJI_CANCELLED: Final[str] = "\u274C"  # cross mark, cancelled date
EMOJI_RECUR: Final[str] = "\U0001F501"  # repeat

EMOJI_PRIORITY: Final[dict[str, Priority]] = {
    "\U0001F53A": Priority.HIGH,  # red triangle up, highest
    "\u23EB": Priority.HIGH,  # double up, high
    "\U0001F53C": Priority.MEDIUM,  # triangle up, medium
    "\U0001F53D": Priority.LOW,  # triangle down, low
    "\u23EC": Priority.LOW,  # double down, lowest
}

DATE_EMOJI: Final[dict[str, str]] = {
    EMOJI_DUE: "due",
    EMOJI_SCHEDULED: "scheduled",
    EMOJI_START: "start",
    EMOJI_DONE: "done",
    EMOJI_CREATED: "created",
    EMOJI_CANCELLED: "cancelled",
}

PRIORITY_WORDS: Final[dict[str, Priority]] = {
    "highest": Priority.HIGH,
    "high": Priority.HIGH,
    "a": Priority.HIGH,
    "1": Priority.HIGH,
    "medium": Priority.MEDIUM,
    "med": Priority.MEDIUM,
    "normal": Priority.MEDIUM,
    "b": Priority.MEDIUM,
    "2": Priority.MEDIUM,
    "low": Priority.LOW,
    "lowest": Priority.LOW,
    "c": Priority.LOW,
    "3": Priority.LOW,
}

DONE_MARKS: Final[frozenset[str]] = frozenset({"x", "X"})
#: ``[-]`` is "cancelled" in the Tasks plugin; we drop those lines entirely.
CANCELLED_MARKS: Final[frozenset[str]] = frozenset({"-"})

#: Guard against pointing the adapter at an enormous directory by accident.
MAX_FILES: Final[int] = 5000


def _parse_priority(raw: str) -> Priority | None:
    return PRIORITY_WORDS.get(raw.strip().lower())


def parse_task_line(line: str, *, source_id: str) -> Task | None:
    """Parse one Markdown checkbox line into a :class:`Task`, or return None."""
    match = TASK_LINE.match(line)
    if match is None:
        return None
    mark = match.group("mark")
    if mark in CANCELLED_MARKS:
        return None
    body = match.group("body")

    fields: dict[str, str] = {}
    for field in INLINE_FIELD.finditer(body):
        fields[field.group("key").strip().lower()] = field.group("value").strip()
    body = INLINE_FIELD.sub(" ", body)

    priority = Priority.NONE
    raw_priority = fields.get("priority") or fields.get("prio")
    if raw_priority:
        priority = _parse_priority(raw_priority) or Priority.NONE

    due: date | None = None
    raw_due = fields.get("due") or fields.get("deadline")
    if raw_due:
        found = ISO_DATE.search(raw_due)
        if found:
            due = date.fromisoformat(found.group(0))

    # Emoji dialect: walk the remaining text and pull out marker + value pairs.
    pieces: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char in EMOJI_PRIORITY:
            if priority is Priority.NONE:
                priority = EMOJI_PRIORITY[char]
            index += 1
            continue
        if char in DATE_EMOJI:
            kind = DATE_EMOJI[char]
            rest = body[index + 1 : index + 20]
            found = ISO_DATE.search(rest)
            if found is not None:
                if kind == "due" and due is None:
                    due = date.fromisoformat(found.group(0))
                index += 1 + found.end()
                continue
            index += 1
            continue
        if char == EMOJI_RECUR:
            # Drop the recurrence rule text up to the next marker or end.
            index += 1
            while index < length and body[index] not in DATE_EMOJI and body[index] not in EMOJI_PRIORITY:
                index += 1
            continue
        pieces.append(char)
        index += 1

    title_raw = "".join(pieces)
    tags = sorted({tag.group(1) for tag in TAG.finditer(title_raw)})
    title = TAG.sub(" ", title_raw)
    title = re.sub(r"\s{2,}", " ", title).strip(" -")
    if not title:
        return None

    digest = hashlib.sha256(f"{source_id}|{title}".encode("utf-8")).hexdigest()[:12]
    return Task(
        id=f"obsidian-{digest}",
        title=title,
        due=due,
        priority=priority,
        completed=mark in DONE_MARKS,
        source="obsidian",
        tags=tags,
    )


class FixtureTasksAdapter:
    """Tasks from ``fixtures/tasks.json``."""

    name = "tasks"
    source = "fixture"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[Task]:
        settings = self._settings
        payload = load_fixture(settings.fixtures_dir / "tasks.json")
        delta = day_delta(
            payload, today_local(settings.timezone), enabled=settings.fixture_relative_dates
        )
        tasks: list[Task] = []
        raw_tasks: Any = payload.get("tasks", [])
        for raw in raw_tasks:
            item = dict(raw)
            item["due"] = shift_iso(item.get("due"), delta)
            item.setdefault("source", "fixture")
            tasks.append(Task.model_validate(item))
        return tasks


class FileTasksAdapter:
    """Tasks from ``DATA_DIR/tasks.json``, the shape ``POST /api/tasks``
    writes. No date shifting: a pushed due date is used exactly as sent."""

    name = "tasks"
    source = "file"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        #: Set on every successful fetch: the file's own ``received_at``, or
        #: its mtime. Read by ``CachedAdapter`` for ``TasksBlock.received_at``.
        self.last_received_at: datetime | None = None

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[Task]:
        path = self._settings.tasks_file
        if not path.is_file():
            raise AdapterUnavailable(f"tasks file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
        received_raw: Any = None
        if isinstance(payload, dict):
            raw_tasks: Any = payload.get("tasks", [])
            received_raw = payload.get("received_at")
        elif isinstance(payload, list):
            raw_tasks = payload
        else:
            raise ValueError(f"{path} must contain an object or a list")
        tasks: list[Task] = []
        for raw in raw_tasks:
            item = dict(raw)
            item.setdefault("source", "file")
            tasks.append(Task.model_validate(item))
        self.last_received_at = received_at_or_mtime(received_raw, path, self._settings.timezone)
        return tasks


class AutoTasksAdapter:
    """"auto": the file adapter when ``DATA_DIR/tasks.json`` exists, else
    fixture. Re-checked on every ``fetch()`` (see ``AutoAIUsageAdapter``);
    also falls back to fixture instead of an ``error`` block when the file
    delegate raises ``AdapterUnavailable`` or the file vanishes between the
    check here and the delegate's own read (TOCTOU)."""

    name = "tasks"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._file = FileTasksAdapter(settings)
        self._fixture = FixtureTasksAdapter(settings)
        self.last_received_at: datetime | None = None
        #: The delegate actually used on the last fetch; see
        #: ``AutoAIUsageAdapter._last_source``.
        self._last_source = "fixture"

    @property
    def source(self) -> str:
        return self._last_source

    def resolve(self) -> str:
        """A pure, live check of what the *next* ``fetch()`` would use; see
        ``AutoAIUsageAdapter.resolve``."""
        return "file" if self._settings.tasks_file.is_file() else "fixture"

    async def fetch(self) -> list[Task]:
        if self._settings.tasks_file.is_file():
            try:
                value = await self._file.fetch()
            except (AdapterUnavailable, OSError):
                pass
            else:
                self._last_source = "file"
                self.last_received_at = getattr(self._file, "last_received_at", None)
                return value
        value = await self._fixture.fetch()
        self._last_source = "fixture"
        self.last_received_at = getattr(self._fixture, "last_received_at", None)
        return value


class ObsidianTasksAdapter:
    """Read-only Markdown task reader for an Obsidian vault."""

    name = "tasks"
    source = "obsidian"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[Task]:
        vault = self._settings.obsidian_vault_path
        if vault is None:
            raise AdapterUnavailable("OBSIDIAN_VAULT_PATH is not set")
        if not vault.is_dir():
            raise AdapterUnavailable(f"vault directory not found: {vault}")
        return read_vault(vault, self._settings.obsidian_task_glob)


def read_vault(vault: Path, pattern: str) -> list[Task]:
    """Collect tasks from every Markdown file matching ``pattern``."""
    tasks: list[Task] = []
    seen: set[str] = set()
    files = 0
    for path in sorted(vault.glob(pattern)):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        if any(part.startswith(".") for part in path.relative_to(vault).parts):
            continue
        files += 1
        if files > MAX_FILES:
            log(logger, logging.WARNING, "vault file cap reached", cap=MAX_FILES, vault=str(vault))
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log(logger, logging.WARNING, "unreadable vault file", path=str(path), error=str(exc))
            continue
        relative = path.relative_to(vault).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            task = parse_task_line(line, source_id=f"{relative}:{number}")
            if task is None or task.id in seen:
                continue
            seen.add(task.id)
            tasks.append(task)
    log(logger, logging.INFO, "vault scanned", vault=str(vault), files=files, tasks=len(tasks))
    return tasks


def build_tasks_adapter(
    settings: Settings,
) -> FixtureTasksAdapter | ObsidianTasksAdapter | FileTasksAdapter | AutoTasksAdapter:
    if settings.tasks_source == "obsidian":
        return ObsidianTasksAdapter(settings)
    if settings.tasks_source == "file":
        return FileTasksAdapter(settings)
    if settings.tasks_source == "auto":
        return AutoTasksAdapter(settings)
    return FixtureTasksAdapter(settings)
