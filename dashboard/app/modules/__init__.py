"""What a module is, and what core promises it.

A *dataset* is a named block of normalized data with an adapter behind it
(``tasks``, ``weather``). A *page* is one 800x480 render. A *module* is a
Python package that provides zero or more datasets and at most one page,
plus its own settings section and its own push routes if it wants them.
Core keeps the frame (header, footer, the render engine, the alert page and
API, auth, the settings machinery, the device routes and telemetry); a
module never has to know any of it.

Everything here is data, not behaviour: a module is one frozen
:class:`Module` dataclass, exposed as ``MODULE`` at the top of its package.
``app/modules/registry.py`` is what finds those, validates them and puts
them in order. Phase 1 already parked each section's settings model at
``app/modules/<id>/settings.py``; 2.1a adds the ``MODULE`` beside it for the
built-ins, and 2.2 moves the templates, fixtures and context builders in
after it.

Two rules exist so a module cannot quietly break the panel:

* ``alert`` is reserved. The alert page interrupts every other page and then
  hands the previous one back, so it is core's, never a module's, and it
  never takes a slot in the window list.
* Push routes are returned as an ``APIRouter`` and mounted by core with the
  bearer-token dependency already applied, so a module cannot forget auth.

See docs/plan/2026-09-19-settings-modules-provisioning.md, phase 2.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    import logging

    from fastapi import APIRouter
    from PIL import Image
    from playwright.async_api import Browser

    from app.adapters.base import Adapter
    from app.config import Env
    from app.db import Database
    from app.models import Block, DashboardState
    from app.settings import HubSettings

#: A module id is one lowercase word: it is a package name, a URL segment
#: (``/display/<id>.png``), a settings key and a template stem all at once,
#: so anything else would have to be escaped somewhere.
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Longest a module id, settings section, dataset name or dataset section
#: may be. A page id is a module id verbatim, and it is what a device echoes
#: back in every ``POST /api/device/telemetry``
#: (``app/models.py:DeviceTelemetry.page``, ``max_length=32``): a longer id
#: would load here today, and then 400 every telemetry post from a device
#: sitting on that page, forever after.
MAX_ID_LENGTH = 32

#: Ids core owns and a module may never claim. ``alert`` is the interrupt
#: page (see the module docstring).
RESERVED_IDS: frozenset[str] = frozenset({"alert"})

#: Settings sections core owns outright and no module's own ``settings_model``
#: may claim, matching ``app/settings.py:CORE_SECTIONS``'s keys: ``general``
#: is the hub's own timezone and units, ``device`` belongs to the telemetry
#: routes and the retention sweep, ``alert`` is the interrupt page, and
#: ``modules`` is the registry's own enable/order list. A dataset is still
#: free to *read* one of these (the built-in ``system`` module's ``device``
#: dataset does exactly that); what is refused here is a module trying to
#: *own* the row, which would make core's own section a module's to edit or
#: to lose the moment that module is uninstalled.
RESERVED_SECTIONS: frozenset[str] = frozenset({"general", "device", "alert", "modules"})

#: The datasets an agent pushes to the hub, and therefore the only ones a
#: page may mark DEMO when they fall back to a fixture. A fetched dataset on
#: ``fixture`` never prints DEMO, and never has
#: (``app/view.py:page_shows_demo_data``).
PUSHED_DATASETS: frozenset[str] = frozenset({"tasks", "ai_usage", "brief"})

#: Renders one page as an 800x480 RGB image instead of through a template.
#: Core hands it the shared Chromium instance while holding the render lock,
#: so the whole call is serialized against every other render. No built-in
#: page uses this yet; the Home Assistant dashboard module of phase 3 is
#: what it exists for.
ScreenshotFn = Callable[
    ["Browser", "DashboardState", "HubSettings"], Awaitable["Image.Image"]
]

#: What a page turns state into for its template.
PageContextFn = Callable[["DashboardState", "HubSettings"], dict[str, Any]]

#: Whether a page wants the window list's "!" right now.
PageFlagFn = Callable[["DashboardState", "HubSettings"], bool]


@dataclass(frozen=True)
class ModuleContext:
    """What core hands a module when it builds an adapter or a router.

    Deliberately small: the environment knobs that survived the move into
    the database (``app/config.py:Env``), the hub's one database, the data
    directory, the shared HTTP timeout, and a logger already named after the
    module. A module reads its own settings from the section model it
    declared, never from here.
    """

    env: Env
    db: Database
    data_dir: Path
    http_timeout_seconds: float
    logger: logging.Logger


@dataclass(frozen=True)
class DatasetSpec:
    """One named block of data and the adapter that produces it.

    ``section`` is the settings section the adapter reads. It is usually the
    module's own id, but it does not have to be: the built-in ``agenda``
    module owns the ``calendar`` dataset and reads the ``calendar`` section,
    and ``system`` reads the core ``device`` section for its device dataset.
    Section names are what a deployed hub's rows are keyed by, so they stay
    what phase 1 named them.

    ``build_adapter`` gets the section's settings, the ``general`` section
    (which carries the timezone every adapter localizes with) and the
    context. ``ttl_seconds`` gets the section's settings alone.

    ``value_field`` is the one field of ``block_model`` the adapter's value
    lands in (``items`` for tasks, ``weather`` for weather). An outcome with
    no value leaves it at the block model's own default, which is how an
    unavailable dataset ends up as an empty list or ``None`` without a
    second place to keep the two in step (``app/state.py:build_block``).
    """

    name: str
    block_model: type[Block]
    value_field: str
    section: str
    build_adapter: Callable[[Any, Any, ModuleContext], Adapter[Any]]
    ttl_seconds: Callable[[Any], float]
    fixture: Path | None = None


@dataclass(frozen=True)
class PageSpec:
    """One 800x480 page.

    Either ``template`` (a file in ``templates_dir``) or ``screenshot`` is
    set, never both and never neither: a page is drawn by Jinja plus the
    headless browser, or by a renderer of its own.

    ``context`` is the module's own dict. Core computes ``header`` and
    ``footer`` itself and merges them over that dict *after* the module ran,
    so a module cannot overwrite the frame
    (``app/view.py:build_context``).

    ``flag`` is what puts the "!" beside this page in the window list. It
    takes its reference time from ``state.updated_at`` and its counts from
    the blocks it reads, so a page owns its own reason to be flagged instead
    of one shared function knowing about every page.

    ``title`` is the big title on the page itself ("NEXT 7 DAYS"), which is
    not always what the window list calls the page ("AGENDA"); that shorter
    name is the module's own ``title``.
    """

    title: str
    templates_dir: Path
    context: PageContextFn
    render_ttl_seconds: float
    template: str | None = None
    needs: tuple[str, ...] = ()
    demo_datasets: tuple[str, ...] = ()
    flag: PageFlagFn | None = None
    screenshot: ScreenshotFn | None = None


@dataclass(frozen=True)
class Module:
    """A module's whole manifest, exposed as ``MODULE`` by its package.

    ``settings_model`` is the module's *own* settings section, stored under
    ``settings_section`` (the module id by default). A module whose datasets
    read sections that predate it (the built-in ``today``, ``agenda`` and
    ``system``) leaves it ``None`` and names the sections on its datasets
    instead.

    ``routes`` returns an ``APIRouter``. Core mounts it under ``/api`` with
    ``Depends(require_token)`` applied, so every route on it needs the hub's
    bearer token whether the module remembered to ask for it or not.
    """

    id: str
    title: str
    version: str
    description: str
    settings_model: type[BaseModel] | None = None
    settings_section: str | None = None
    datasets: tuple[DatasetSpec, ...] = ()
    page: PageSpec | None = None
    routes: Callable[[ModuleContext], APIRouter] | None = None
    default_order: int = 100
    default_enabled: bool = True

    @property
    def section(self) -> str:
        """The settings section this module's own model is stored under."""
        return self.settings_section or self.id


class ModuleError(ValueError):
    """A module core cannot serve. Raised at registry load, never later."""


def validate_module(module: Module) -> None:
    """Refuse a module core cannot serve, with the reason in one line.

    Every check here is about something that would otherwise fail much later
    and much less clearly: a bad id as a 404 on ``/display``, a page with
    neither a template nor a screenshot as a Jinja ``TemplateNotFound``
    mid-render, a ``value_field`` the block model does not have as a
    ``ValidationError`` on the first state build.
    """
    _check_id(module.id, "module id")
    if module.settings_section is not None:
        _check_id(module.settings_section, "settings section", reserved=False)
    if module.settings_model is not None and module.section in RESERVED_SECTIONS:
        raise ModuleError(
            f"module {module.id!r}: settings section {module.section!r} is reserved for core"
        )
    if module.settings_model is not None and not (
        isinstance(module.settings_model, type)
        and issubclass(module.settings_model, BaseModel)
    ):
        raise ModuleError(f"module {module.id!r}: settings_model must be a pydantic model")

    seen: set[str] = set()
    for dataset in module.datasets:
        _check_id(dataset.name, f"module {module.id!r}: dataset name", reserved=False)
        _check_id(dataset.section, f"module {module.id!r}: dataset section", reserved=False)
        if dataset.name in seen:
            raise ModuleError(f"module {module.id!r}: dataset {dataset.name!r} declared twice")
        seen.add(dataset.name)
        if dataset.value_field not in dataset.block_model.model_fields:
            raise ModuleError(
                f"module {module.id!r}: dataset {dataset.name!r} value_field "
                f"{dataset.value_field!r} is not a field of "
                f"{dataset.block_model.__name__}"
            )

    page = module.page
    if page is None:
        return
    if (page.template is None) == (page.screenshot is None):
        raise ModuleError(
            f"module {module.id!r}: a page is drawn by exactly one of template "
            "or screenshot, never both and never neither"
        )
    not_pushed = [name for name in page.demo_datasets if name not in PUSHED_DATASETS]
    if not_pushed:
        raise ModuleError(
            f"module {module.id!r}: demo_datasets may only name a pushed dataset "
            f"({', '.join(sorted(PUSHED_DATASETS))}); got {', '.join(not_pushed)}"
        )


def _check_id(value: Any, what: str, *, reserved: bool = True) -> None:
    if not isinstance(value, str) or not MODULE_ID_RE.match(value):
        raise ModuleError(f"{what} {value!r} must match {MODULE_ID_RE.pattern}")
    if len(value) > MAX_ID_LENGTH:
        raise ModuleError(
            f"{what} {value!r} is {len(value)} characters; the longest allowed is "
            f"{MAX_ID_LENGTH} (a page id is a module id, and that is what a device "
            "echoes back on every telemetry post)"
        )
    if value.isdigit():
        # Unreachable through the pattern above (it demands a leading
        # letter), and checked anyway: /display/{n}.png reads an all-digit
        # segment as a page index in 2.1b, so an all-digit id would be
        # ambiguous the moment that pattern is ever loosened.
        raise ModuleError(f"{what} {value!r} must not be all digits")
    if reserved and value in RESERVED_IDS:
        raise ModuleError(f"{what} {value!r} is reserved for core")


__all__ = [
    "MAX_ID_LENGTH",
    "MODULE_ID_RE",
    "PUSHED_DATASETS",
    "RESERVED_IDS",
    "RESERVED_SECTIONS",
    "DatasetSpec",
    "Module",
    "ModuleContext",
    "ModuleError",
    "PageContextFn",
    "PageFlagFn",
    "PageSpec",
    "ScreenshotFn",
    "validate_module",
]
