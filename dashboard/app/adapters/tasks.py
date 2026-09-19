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
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from starlette.concurrency import run_in_threadpool

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_iso
from app.config import Env
from app.datasets import read_dataset
from app.db import get_database
from app.logging_setup import log
from app.models import Priority, Task
from app.modules.general.settings import GeneralSettings
from app.modules.tasks.settings import TasksSettings
from app.timeutil import to_local, today_local

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

    def __init__(self, tasks: TasksSettings, general: GeneralSettings, env: Env) -> None:
        self._tasks = tasks
        self._general = general
        self._env = env

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[Task]:
        env = self._env
        payload = load_fixture(env.fixtures_dir / "tasks.json")
        delta = day_delta(
            payload, today_local(self._general.timezone), enabled=env.fixture_relative_dates
        )
        tasks: list[Task] = []
        raw_tasks: Any = payload.get("tasks", [])
        for raw in raw_tasks:
            item = dict(raw)
            item["due"] = shift_iso(item.get("due"), delta)
            item.setdefault("source", "fixture")
            tasks.append(Task.model_validate(item))
        return tasks


class PushTasksAdapter:
    """Tasks from the ``tasks`` row of the ``datasets`` table, the shape
    ``POST /api/tasks`` stores. No date shifting: a pushed due date is used
    exactly as sent."""

    name = "tasks"
    source = "push"

    def __init__(self, tasks: TasksSettings, general: GeneralSettings, env: Env) -> None:
        self._tasks = tasks
        self._general = general
        self._env = env
        #: Set on every successful fetch: the dataset row's own
        #: ``received_at``. Read by ``CachedAdapter`` for
        #: ``TasksBlock.received_at``.
        self.last_received_at: datetime | None = None

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[Task]:
        database = get_database(self._env.hub_db_file)
        database.migrate()
        found = read_dataset(database, "tasks")
        if found is None:
            raise AdapterUnavailable("nothing pushed yet for tasks")
        payload, received_at = found
        raw_tasks: Any
        if isinstance(payload, dict):
            raw_tasks = payload.get("tasks", [])
        elif isinstance(payload, list):
            raw_tasks = payload
        else:
            raise ValueError("the tasks dataset must hold an object or a list")
        tasks: list[Task] = []
        for raw in raw_tasks:
            item = dict(raw)
            item.setdefault("source", "push")
            tasks.append(Task.model_validate(item))
        self.last_received_at = to_local(received_at, self._general.timezone)
        return tasks


class ObsidianTasksAdapter:
    """Read-only Markdown task reader for an Obsidian vault."""

    name = "tasks"
    source = "obsidian"

    def __init__(self, tasks: TasksSettings, env: Env) -> None:
        self._tasks = tasks
        self._env = env

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[Task]:
        vault = self._tasks.obsidian_vault_path
        if vault is None:
            raise AdapterUnavailable("the obsidian vault path is not set")
        if not vault.is_dir():
            raise AdapterUnavailable(f"vault directory not found: {vault}")
        # read_vault globs and reads up to MAX_FILES files; off the event loop
        # so a large vault does not block every other request while it runs.
        return await run_in_threadpool(read_vault, vault, self._tasks.obsidian_task_glob)


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
    tasks: TasksSettings, general: GeneralSettings, env: Env
) -> FixtureTasksAdapter | ObsidianTasksAdapter | PushTasksAdapter:
    if tasks.source == "obsidian":
        return ObsidianTasksAdapter(tasks, env)
    if tasks.source == "push":
        return PushTasksAdapter(tasks, general, env)
    return FixtureTasksAdapter(tasks, general, env)
