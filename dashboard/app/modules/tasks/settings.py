"""The ``tasks`` settings section: where task items come from, and how the
priority list is built from them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The ``settings`` row name this model reads and writes.
SECTION = "tasks"

#: ``push`` reads the latest ``POST /api/tasks`` payload; ``obsidian`` scans a
#: mounted vault for open checkboxes; ``fixture`` is the explicit demo choice.
#: The old ``auto`` and ``file`` selectors are gone (see the plan's
#: Non-goals): both meant "the pushed file", which is now just ``push``.
TasksSource = Literal["push", "obsidian", "fixture"]


class TasksSettings(BaseModel):
    """Task source and the limits the priority list and its cache use."""

    model_config = ConfigDict(extra="ignore")

    source: TasksSource = Field(
        default="push",
        description="Where tasks come from: an agent pushing them, an Obsidian vault, or demo data.",
    )
    obsidian_vault_path: Path | None = Field(
        default=None,
        description="Path to the mounted Obsidian vault, used only when the source is obsidian.",
    )
    obsidian_task_glob: str = Field(
        default="**/*.md",
        description="Glob pattern selecting which files in the vault are scanned for open tasks.",
    )
    max_priority_tasks: int = Field(
        default=3,
        description="How many of the highest-priority open tasks the panel shows at once.",
    )
    ttl_seconds: float = Field(
        default=300.0,
        description="How long a fetched task list is cached before it is fetched again.",
    )
    stale_seconds: float = Field(
        default=36000.0,
        description="How old a pushed task list can get before the panel marks it stale.",
    )


__all__ = ["SECTION", "TasksSettings", "TasksSource"]
