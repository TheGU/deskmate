"""The ``tasks`` settings section: where task items come from, and how the
priority list is built from them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The ``settings`` row name this model reads and writes.
SECTION = "tasks"

#: ``push`` reads the latest ``POST /api/tasks`` payload; ``fixture`` is the
#: explicit demo choice. The old ``auto`` and ``file`` selectors are gone
#: (see the plan's Non-goals): both meant "the pushed file", which is now
#: just ``push``. The ``obsidian`` selector (the hub reading a mounted vault
#: directly) is also gone: tasks reach the hub only through the push API, a
#: local agent reads the owner's own vault and pushes (docs/LOCAL-AGENT.md).
TasksSource = Literal["push", "fixture"]


class TasksSettings(BaseModel):
    """Task source and the limits the priority list and its cache use."""

    model_config = ConfigDict(extra="ignore")

    source: TasksSource = Field(
        default="push",
        description="Where tasks come from: an agent pushing them, or demo data.",
    )
    max_priority_tasks: int = Field(
        default=3,
        ge=1,
        le=10,
        description="How many of the highest-priority open tasks the panel shows at once.",
    )
    ttl_seconds: float = Field(
        default=300.0,
        ge=0,
        le=86400 * 7,
        allow_inf_nan=False,
        description="How long a fetched task list is cached before it is fetched again.",
    )
    stale_seconds: float = Field(
        default=36000.0,
        ge=0,
        le=86400 * 7,
        allow_inf_nan=False,
        description="How old a pushed task list can get before the panel marks it stale.",
    )

    @model_validator(mode="before")
    @classmethod
    def _removed_obsidian_source(cls, data: Any) -> Any:
        """A hub upgraded from before the ``obsidian`` source was removed can
        still have a ``tasks`` row with ``source: "obsidian"`` and an
        ``obsidian_vault_path``, neither of which this model accepts any
        more. Map the source to ``push`` and drop the stale fields here,
        before validation, so ``SettingsStore.load`` does not fall back to
        defaults for the *whole* section (losing ``max_priority_tasks`` and
        the TTLs too) just because one field no longer parses."""
        if isinstance(data, dict) and data.get("source") == "obsidian":
            data = dict(data)
            data["source"] = "push"
            data.pop("obsidian_vault_path", None)
            data.pop("obsidian_task_glob", None)
        return data


__all__ = ["SECTION", "TasksSettings", "TasksSource"]
