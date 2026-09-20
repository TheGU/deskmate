"""Task adapters: fixtures and the pushed task list.

Tasks reach the hub only through the push API (``POST /api/tasks``, see
docs/DATA-SOURCES.md): a local agent that reads the owner's own task
manager pushes the list over HTTP (docs/LOCAL-AGENT.md). The hub itself
never reads a task source directly; the only other selector is
``fixture``, the explicit demo choice.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_iso
from app.config import Env
from app.datasets import read_dataset
from app.db import get_database
from app.models import Task
from app.modules.general.settings import GeneralSettings
from app.modules.tasks.settings import TasksSettings
from app.timeutil import to_local, today_local


class FixtureTasksAdapter:
    """Tasks from the module's own ``fixtures/tasks.json``."""

    name = "tasks"
    source = "fixture"

    def __init__(self, tasks: TasksSettings, general: GeneralSettings, env: Env, fixture: Path) -> None:
        self._tasks = tasks
        self._general = general
        self._env = env
        self._fixture = fixture

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> list[Task]:
        env = self._env
        payload = load_fixture(self._fixture)
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


def build_tasks_adapter(
    tasks: TasksSettings, general: GeneralSettings, env: Env, fixture: Path
) -> FixtureTasksAdapter | PushTasksAdapter:
    if tasks.source == "push":
        return PushTasksAdapter(tasks, general, env)
    return FixtureTasksAdapter(tasks, general, env, fixture)
