"""Assembles the normalized :class:`DashboardState` from every adapter.

One failing adapter never breaks a page: each block carries its own status and
the templates print "unknown" / "unavailable" instead of a made up value.

Since 2.1a the set of adapters is not written down here. It is whatever the
enabled modules declare (``app/modules/registry.py:Registry.datasets``), so
disabling a module stops its fetches and leaves the pages that draw it
showing an unavailable block, and installing one starts its fetches with no
change to this file.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, TypeVar

from app.adapters.base import Adapter, CachedAdapter, Outcome
from app.alerts import AlertStore
from app.config import Env
from app.db import Database, get_database
from app.logging_setup import log
from app.models import Block, DashboardState
from app.modules import DatasetSpec, ModuleContext
from app.modules.registry import Registry, builtin_registry
from app.settings import HubSettings
from app.timeutil import now_local

logger = logging.getLogger("app.state")

BlockT = TypeVar("BlockT", bound=Block)


class StateService:
    """Owns the cached adapters and produces state snapshots."""

    def __init__(
        self,
        hub_settings: HubSettings,
        env: Env,
        alerts: AlertStore,
        registry: Registry | None = None,
        db: Database | None = None,
    ) -> None:
        self._hub_settings = hub_settings
        self._env = env
        self._alerts = alerts
        # A caller with no registry gets the built-ins with this snapshot's
        # own module toggles applied, which is what every test that builds a
        # StateService directly wants. ``Hub`` always passes its own.
        self._registry = builtin_registry(hub_settings.modules) if registry is None else registry
        self._db = get_database(env.hub_db_file) if db is None else db
        self._specs: dict[str, DatasetSpec] = self._registry.datasets()
        self._adapters: dict[str, CachedAdapter[Any]] = {
            name: CachedAdapter(self._build(name, spec), spec.ttl_seconds(self._section(spec)))
            for name, spec in self._specs.items()
        }

    def _section(self, spec: DatasetSpec) -> Any:
        """The settings the dataset's adapter reads.

        A built-in or core section is an attribute of the snapshot. A section
        a module brought with it is not (``HubSettings`` is a fixed model),
        so it falls back to that module's own defaults until 2.1b gives a
        module's section a row of its own.
        """
        stored = getattr(self._hub_settings, spec.section, None)
        if stored is not None:
            return stored
        model = self._registry.sections().get(spec.section)
        if model is None:
            raise KeyError(
                f"dataset {spec.name!r} reads the settings section {spec.section!r}, "
                "which no module and no core section defines"
            )
        return model()

    def _build(self, name: str, spec: DatasetSpec) -> Adapter[Any]:
        owner = self._registry.dataset_owner(name)
        context = ModuleContext(
            env=self._env,
            db=self._db,
            data_dir=self._env.data_dir,
            http_timeout_seconds=self._env.http_timeout_seconds,
            logger=logging.getLogger(f"app.modules.{owner.id if owner else name}"),
        )
        return spec.build_adapter(self._section(spec), self._hub_settings.general, context)

    @property
    def adapters(self) -> dict[str, CachedAdapter[Any]]:
        """Every cached adapter, keyed and ordered like DashboardState.blocks.

        Each adapter's own ``name`` (adapters/*.py) already matches its key
        here, so this is just the explicit registry /healthz walks without
        forcing a fetch. It is also how a push route invalidates the adapter
        whose row it just wrote.
        """
        return dict(self._adapters)

    async def build(self, *, force: bool = False) -> DashboardState:
        """Fetch every adapter (concurrently) and fold the results into state."""
        names = list(self._adapters)
        outcomes = await asyncio.gather(
            *(self._adapters[name].get(force=force) for name in names)
        )
        blocks: dict[str, Block] = {
            name: build_block(
                self._specs[name].block_model, outcome, self._specs[name].value_field
            )
            for name, outcome in zip(names, outcomes)
        }

        state = DashboardState(
            generated_at=now_local(self._hub_settings.general.timezone),
            timezone=self._hub_settings.general.timezone,
            blocks=blocks,
            alert=self._alerts.current,
        )
        log(
            logger,
            logging.INFO,
            "state built",
            forced=force,
            **{name: block.status.value for name, block in state.blocks.items()},
        )
        return state


def build_block(model: type[BlockT], outcome: Outcome[Any], value_field: str) -> BlockT:
    """One adapter outcome as its block: the envelope plus the one field the
    block type carries the value in.

    An outcome with no value leaves ``value_field`` off entirely rather than
    passing an empty sentinel, so the block type's own default (an empty
    list, or ``None``) is what an unavailable dataset shows. That is the
    same result the explicit ``empty`` argument produced before 2.1a, minus
    a second place to keep the two in step.
    """
    extra: dict[str, Any] = {} if outcome.value is None else {value_field: outcome.value}
    return model(
        status=outcome.status,
        source=outcome.source,
        updated_at=outcome.updated_at,
        error=outcome.error,
        # Only AIUsageBlock, BriefBlock and TasksBlock declare this field;
        # pydantic's default extra="ignore" drops it for the other blocks.
        received_at=outcome.received_at,
        **extra,
    )


def state_fingerprint(state: DashboardState) -> str:
    """Stable hash of everything a page can render.

    ``generated_at`` is excluded on purpose: it moves on every request, while
    the rendered pixels only change when an adapter produced something new.
    """
    payload = state.model_dump_json(exclude={"generated_at"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["StateService", "build_block", "state_fingerprint"]
