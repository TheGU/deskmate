"""Finds the modules, validates them, and puts them in order.

Three places a module can come from, in this order:

1. the built-in packages under ``app/modules/<id>/``, listed explicitly in
   :data:`BUILTIN_MODULE_PACKAGES` so a built-in is one line rather than a
   directory scan that could pick up a half-written package;
2. the ``deskmate.modules`` entry point group, which is how a
   ``pip install``ed module announces itself;
3. the packages under ``DATA_DIR/modules/``, which is how a module is
   installed without packaging anything: drop the directory in, restart.

A duplicate id, an invalid id, or two modules claiming the same dataset
name refuses to load, loudly, at startup. The alternative - dropping one
silently - would mean a panel that draws something the settings page cannot
explain.

Order and enablement come from the ``modules`` settings section
(:class:`ModulesSettings`): one row per module with an ``enabled`` box and
an optional ``order``. A module with no row keeps its own
``default_enabled`` / ``default_order``, so installing one is enough to see
it. A row naming a module that is not installed is kept and reported by
:meth:`Registry.missing_ids`, never dropped: an id in the database is the
owner's intent, and a module can come back after an upgrade.

Enabling or disabling a module takes effect through ``Hub.reload()``, which
rebuilds the registry from the fresh settings snapshot.

The registry is deliberately not a process-wide singleton: it belongs to a
``Hub``, because two ``create_app`` calls over two data directories have two
different sets of installed modules. :func:`builtin_registry` is the
built-ins-only default the module-level constants in
``app/renderer/render.py`` and ``app/settings.py`` are derived from.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
from collections.abc import Iterable
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.logging_setup import log
from app.modules import DatasetSpec, Module, ModuleError, validate_module

logger = logging.getLogger("app.modules")

#: The built-in modules, as import paths of packages exposing ``MODULE``.
#: The order here is irrelevant (settings order, then ``default_order``,
#: then id is what orders them); it is a list, not a scan of
#: ``app/modules/``, because several packages in that tree are settings
#: sections only (``general``, ``alert``, ``device``, ``home``,
#: ``calendar``) and are not modules at all.
BUILTIN_MODULE_PACKAGES: tuple[str, ...] = (
    "app.modules.today",
    "app.modules.agenda",
    "app.modules.weather",
    "app.modules.brief",
    "app.modules.system",
    "app.modules.tasks",
    "app.modules.ai_usage",
    "app.modules.ha_dashboard",
)

#: The entry point group a packaged module announces itself in.
ENTRY_POINT_GROUP = "deskmate.modules"

#: Where a module installed by hand lives, under ``DATA_DIR``.
MODULES_DIRNAME = "modules"

#: The settings section this file owns.
SECTION = "modules"

#: Every ``DATA_DIR/modules`` root :func:`directory_modules` has ever put on
#: ``sys.path``, across every ``Hub`` built in this process. What tells the
#: shadow check in :func:`directory_modules` apart from an installed
#: package: a name that resolves into one of these roots is some other
#: hub's own directory module of the same name (allowed, and how the tests
#: run two hubs in one process), never the standard library or a real
#: dependency (refused).
_KNOWN_MODULE_ROOTS: set[Path] = set()

#: For each name ever imported by :func:`directory_modules`, the root it was
#: last imported from. What :func:`_forget_foreign` uses to tell "this name
#: belongs to a *different* data directory's module of the same id, safe to
#: evict and reimport" from "this name is something else already in
#: ``sys.modules`` - the standard library, an installed package, anything
#: this process imported for its own reasons - which must never be touched.
_DIRECTORY_MODULE_ROOTS: dict[str, Path] = {}


class ModuleToggle(BaseModel):
    """One module's row in the ``modules`` settings section."""

    id: str = Field(description="The module's id, as its package reports it.")
    enabled: bool = Field(default=True, description="Draw this module's page and fetch its data.")
    order: int | None = Field(
        default=None,
        description="Where it sits in the window list. Blank keeps the module's own default.",
    )


class ModulesSettings(BaseModel):
    """Which modules are on, and in what order.

    A list rather than a mapping because that is what the settings form
    generator renders (``app/forms.py``: indexed rows with a delete box),
    and because a list keeps a row for a module that is not installed right
    now instead of needing a key that resolves to nothing.
    """

    items: list[ModuleToggle] = Field(
        default_factory=list,
        description="One row per module. A module with no row uses its own defaults.",
    )


class Registry:
    """The installed modules, ordered and answering for the enabled ones."""

    def __init__(
        self, modules: Iterable[Module], settings: ModulesSettings | None = None
    ) -> None:
        toggles = {toggle.id: toggle for toggle in (settings or ModulesSettings()).items}
        by_id: dict[str, Module] = {}
        dataset_owner: dict[str, str] = {}
        for module in modules:
            validate_module(module)
            if module.id in by_id:
                raise ModuleError(f"two modules claim the id {module.id!r}")
            by_id[module.id] = module
            for dataset in module.datasets:
                owner = dataset_owner.get(dataset.name)
                if owner is not None:
                    raise ModuleError(
                        f"modules {owner!r} and {module.id!r} both provide the "
                        f"dataset {dataset.name!r}"
                    )
                dataset_owner[dataset.name] = module.id

        self._toggles = toggles
        self._modules = tuple(
            sorted(by_id.values(), key=lambda module: (self.order_of(module), module.id))
        )
        self._missing = tuple(sorted(name for name in toggles if name not in by_id))

    # -- enablement and order --------------------------------------------
    def order_of(self, module: Module) -> int:
        toggle = self._toggles.get(module.id)
        if toggle is None or toggle.order is None:
            return module.default_order
        return toggle.order

    def is_enabled(self, module: Module) -> bool:
        toggle = self._toggles.get(module.id)
        return module.default_enabled if toggle is None else toggle.enabled

    # -- what is installed -------------------------------------------------
    @property
    def modules(self) -> tuple[Module, ...]:
        """Every installed module, enabled or not, in order."""
        return self._modules

    def missing_ids(self) -> tuple[str, ...]:
        """Ids the ``modules`` section names that are not installed here."""
        return self._missing

    def toggle_rows(self) -> tuple[ModuleToggle, ...]:
        """The ``modules`` section as the settings page shows it.

        One row per installed module, in order, carrying what is actually in
        force: the stored toggle where there is one, the module's own
        manifest defaults where there is not, so installing a module is
        enough to see it on the page with its real state. Then the stored
        rows for ids that are not installed, kept exactly as they are, so
        saving the form cannot quietly drop a module that is only missing
        because an upgrade has not been done yet (:meth:`missing_ids` is
        what puts the warning next to them).
        """
        rows = [
            ModuleToggle(id=module.id, enabled=self.is_enabled(module), order=self.order_of(module))
            for module in self._modules
        ]
        rows.extend(self._toggles[name] for name in self._missing)
        return tuple(rows)

    def with_settings(self, settings: ModulesSettings) -> Registry:
        """The same installed modules under a different ``modules`` section.

        What a settings save is checked against before it is written
        (``app/settings_pages.py``): a submission that would leave the panel
        with no page at all has to be refused while it is still a form, and
        the only honest way to know is to apply it.
        """
        return Registry(self._modules, settings)

    def enabled_modules(self) -> tuple[Module, ...]:
        return tuple(module for module in self._modules if self.is_enabled(module))

    # -- pages -------------------------------------------------------------
    def pages(self) -> tuple[Module, ...]:
        """The enabled modules that draw a page, in window-list order."""
        return tuple(module for module in self.enabled_modules() if module.page is not None)

    def page_ids(self) -> tuple[str, ...]:
        return tuple(module.id for module in self.pages())

    def page(self, page_id: str) -> Module | None:
        """The enabled module that draws ``page_id``, or ``None``."""
        for module in self.pages():
            if module.id == page_id:
                return module
        return None

    def page_by_index(self, index: int) -> Module | None:
        """The n-th enabled page, 0-based, or ``None`` past the end.

        The device walks the pages by number (2.1b's ``/display/{n}.png``),
        which is why this is the registry's job and not the route's: the
        index has to resolve to the same id the render cache and the
        ``X-Deskmate-Page`` header use.
        """
        pages = self.pages()
        if index < 0 or index >= len(pages):
            return None
        return pages[index]

    def page_ttl_seconds(self, page_id: str) -> float:
        module = self.page(page_id)
        return 0.0 if module is None or module.page is None else module.page.render_ttl_seconds

    def templates_dirs(self) -> tuple[Path, ...]:
        """Every enabled page's template directory, without repeats.

        Order matters: it is the Jinja search order after core's own
        ``app/templates``.
        """
        seen: list[Path] = []
        for module in self.pages():
            assert module.page is not None
            directory = module.page.templates_dir
            if directory not in seen:
                seen.append(directory)
        return tuple(seen)

    # -- datasets ----------------------------------------------------------
    def datasets(self) -> dict[str, DatasetSpec]:
        """Every enabled module's datasets, keyed by name, in module order."""
        return {
            dataset.name: dataset
            for module in self.enabled_modules()
            for dataset in module.datasets
        }

    def dataset_owner(self, name: str) -> Module | None:
        for module in self.enabled_modules():
            for dataset in module.datasets:
                if dataset.name == name:
                    return module
        return None

    # -- settings ----------------------------------------------------------
    def sections(self) -> dict[str, type[BaseModel]]:
        """Each module's own settings section, keyed by section name.

        Every installed module, not only the enabled ones: a disabled
        module's section still has a row in the database and still has to
        load, save and render, or turning a module off would lose its
        settings.

        This is not the whole of ``app/settings.py:SECTIONS``. The core
        sections (``general``, ``device``, ``alert``, ``modules``) are not
        modules and never will be, so that file adds them.
        """
        return {
            module.section: module.settings_model
            for module in self._modules
            if module.settings_model is not None
        }


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def builtin_modules() -> tuple[Module, ...]:
    return tuple(_module_of(importlib.import_module(name)) for name in BUILTIN_MODULE_PACKAGES)


@lru_cache(maxsize=1)
def entry_point_modules() -> tuple[Module, ...]:
    """Modules announced through the ``deskmate.modules`` entry point group.

    A broken entry point is a WARNING and is skipped, not a dead hub: one
    third-party package that fails to import must not take the panel down
    with it. A module that loads but is invalid still refuses, in
    :class:`Registry`, because by then it is something the settings page
    would have to explain.

    Cached for the process: scanning every installed distribution is not
    free and a ``Hub`` is built once per process in production and many
    times over in the tests. Installing a package means restarting the
    container anyway (plan, Non-goals).
    """
    found: list[Module] = []
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            found.append(_module_of(entry.load()))
        except Exception as exc:  # noqa: BLE001 - one bad package, not a dead hub
            log(
                logger,
                logging.WARNING,
                "module entry point failed to load",
                entry_point=entry.name,
                error=str(exc),
            )
    return tuple(found)


def directory_modules(data_dir: Path) -> tuple[Module, ...]:
    """Modules dropped into ``DATA_DIR/modules/`` as plain packages.

    The directory goes on ``sys.path`` and each subdirectory that holds an
    ``__init__.py`` is imported by its own name. It is appended, not put at
    the front: a name that also names something already installed - the
    standard library's own ``calendar``, or ``json``, or ``app`` itself -
    must resolve to that real module first, which is what lets the check
    below tell a genuine shadow apart from a harmless name (see
    :func:`_is_a_directory_module`). A name already imported from a
    *different* data directory's own module of the same id is dropped from
    ``sys.modules`` first (:func:`_forget_foreign`), so two hubs in one
    process (the tests) cannot serve each other's ``hello``; the actual
    import then always loads from this ``root``, never from sys.path order,
    so the other hub's copy cannot win just for having been imported first
    (:func:`_import_from_root`).
    """
    root = data_dir / MODULES_DIRNAME
    if not root.is_dir():
        return ()
    if str(root) not in sys.path:
        sys.path.append(str(root))
    _KNOWN_MODULE_ROOTS.add(root)
    importlib.invalidate_caches()

    found: list[Module] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not (entry / "__init__.py").is_file():
            continue
        _forget_foreign(entry.name, root)
        collision = importlib.util.find_spec(entry.name)
        if collision is not None and not _is_a_directory_module(collision, entry):
            raise ModuleError(
                f"module directory {entry.name!r} shadows an installed package; rename it"
            )
        try:
            module = _import_from_root(entry.name, root)
            found.append(_module_of(module))
        except ModuleError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad package, not a dead hub
            log(
                logger,
                logging.WARNING,
                "module directory package failed to import",
                package=entry.name,
                path=str(entry),
                error=str(exc),
            )
            continue
        _DIRECTORY_MODULE_ROOTS[entry.name] = root
    return tuple(found)


def load_modules(data_dir: Path | None = None) -> tuple[Module, ...]:
    """Every module this hub can see, from all three sources."""
    modules = [*builtin_modules(), *entry_point_modules()]
    if data_dir is not None:
        modules.extend(directory_modules(data_dir))
    return tuple(modules)


def load_registry(
    settings: ModulesSettings | None = None, data_dir: Path | None = None
) -> Registry:
    """The registry a running hub uses."""
    registry = Registry(load_modules(data_dir), settings)
    missing = registry.missing_ids()
    if missing:
        log(
            logger,
            logging.WARNING,
            "modules settings name modules that are not installed",
            ids=",".join(missing),
        )
    return registry


def builtin_registry(settings: ModulesSettings | None = None) -> Registry:
    """The built-ins only, with no settings applied unless asked.

    This is what ``app/settings.py:SECTIONS`` and
    ``app/renderer/render.py:PAGES`` are derived from: both are module-level
    constants that exist before any hub does, so they can only speak for the
    modules that ship with the hub.
    """
    return Registry(_builtin_modules_cached(), settings)


@lru_cache(maxsize=1)
def _builtin_modules_cached() -> tuple[Module, ...]:
    return builtin_modules()


def _module_of(obj: Any) -> Module:
    """A :class:`Module` from a package, an entry point target, or itself."""
    if isinstance(obj, Module):
        return obj
    module = getattr(obj, "MODULE", None)
    if isinstance(module, Module):
        return module
    raise ModuleError(f"{obj!r} does not expose a Module as MODULE")


def _forget_foreign(name: str, root: Path) -> None:
    """Evict ``name`` from ``sys.modules`` only when that is known safe.

    Safe means: :func:`directory_modules` itself loaded ``name`` before, for
    a *different* ``root``, so what is cached is another data directory's
    own module of this id and reimporting for this ``root`` is exactly what
    is wanted. Anything else - ``name`` never seen by this function, or
    already loaded for this same ``root`` - is left untouched. The old rule
    ("evict unless the cached module's file lives under this package
    directory") deleted the standard library's own ``calendar`` from
    ``sys.modules`` the moment a ``DATA_DIR/modules/calendar`` package
    existed, corrupting ``import calendar`` for the rest of the process; this
    rule can only ever evict something *this function* put there.
    """
    previous_root = _DIRECTORY_MODULE_ROOTS.get(name)
    if previous_root is None or previous_root == root:
        return
    for key in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
        del sys.modules[key]


def _is_a_directory_module(spec: importlib.machinery.ModuleSpec, entry: Path) -> bool:
    """Whether ``spec`` - what ``importlib.util.find_spec`` already resolved
    ``entry.name`` to, from the *whole* of ``sys.path`` - is ``entry`` itself
    or some other data directory's own module of the same id, rather than an
    installed package or the standard library.

    This is the check :func:`directory_modules` refuses on when it fails:
    ``sys.path.append`` (not ``insert(0)``) means a real installed package
    always wins a plain name search over a data directory's own package, so
    a spec that resolves anywhere else has to be a genuine collision - a
    ``DATA_DIR/modules/calendar`` shadowing the standard library, say -
    unless it lands inside a root some *other* call to
    :func:`directory_modules` already put on ``sys.path``, which is simply
    two hubs' own modules sharing an id (the multi-hub tests).
    """
    origin = spec.origin
    if origin is None:
        return False
    path = Path(origin).resolve()
    if path.parent == entry.resolve():
        return True
    return any(_is_relative_to(path, known.resolve()) for known in _KNOWN_MODULE_ROOTS)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _import_from_root(name: str, root: Path) -> Any:
    """Import ``name`` strictly from ``root``, never from the rest of
    ``sys.path``.

    Needed because ``root`` is appended, not inserted at the front: a plain
    ``importlib.import_module(name)`` would follow ``sys.path`` order and
    could resolve to an *earlier* data directory's own same-named module
    instead of this one's, which is exactly the case
    :func:`_is_a_directory_module` allows through as "not a real shadow".
    Scoping the actual load to ``root`` is what makes this hub's own module
    win regardless of import order.
    """
    spec = importlib.machinery.PathFinder.find_spec(name, path=[str(root)])
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"no module named {name!r} under {root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


__all__ = [
    "BUILTIN_MODULE_PACKAGES",
    "ENTRY_POINT_GROUP",
    "MODULES_DIRNAME",
    "SECTION",
    "ModuleToggle",
    "ModulesSettings",
    "Registry",
    "builtin_modules",
    "builtin_registry",
    "directory_modules",
    "entry_point_modules",
    "load_modules",
    "load_registry",
]
