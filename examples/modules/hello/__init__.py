"""A complete, minimal third-party page module: copy this directory.

This is the running example for docs/MODULES.md. It shows the smallest
module that draws a real page: no dataset, no push route, one settings
field (its greeting, ``settings.py:HelloSettings``), a page whose context
comes straight off ``DashboardState`` plus that one section.

To install it on a real hub without packaging anything, copy this whole
``hello/`` directory to ``DATA_DIR/modules/hello/`` (see
docs/MODULES.md, "How to install a module") and restart the hub; then
enable it and give it a spot in the window order on ``/settings``
(the Modules section). ``dashboard/tests/modules/test_example_hello.py``
does exactly that copy, into a temp data directory, and asserts the page
shows up and renders.

``app/modules/__init__.py`` is the authority for every field used below;
this file only fills them in.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.modules import Module, PageSpec

from .settings import HelloSettings

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.models import DashboardState
    from app.settings import HubSettings

#: The module's own directory, so ``templates_dir`` below works no matter
#: where this package ends up copied to (``DATA_DIR/modules/hello`` on a
#: real hub, a pytest ``tmp_path`` in the test).
HERE = Path(__file__).resolve().parent


def hello_context(state: DashboardState, settings: HubSettings) -> dict[str, Any]:
    """The module's own context dict.

    Core computes ``header`` and ``footer`` itself and merges them over
    whatever this function returns, *after* it runs
    (``app/view.py:build_context``), so there is no need to build them here
    and no way for this function to overwrite them.

    Reads two things straight off the state every module can see: the
    hub's configured timezone (``state.timezone``) and how many datasets
    have landed in it so far (``len(state.blocks)``), which grows as more
    modules are enabled. A real module would instead read one of its own
    or another module's blocks through ``state.block(name, Model)``; see
    ``app/modules/today/__init__.py`` (``app/view.py:today_context``) for
    that pattern with a dataset that might be missing.

    ``settings.section("hello", HelloSettings)`` is the accessor a
    third-party module uses to read its own settings section whether or
    not core's ``HubSettings`` has ever heard of it
    (``app/settings.py:HubSettings.section``): the value lands in
    ``HubSettings.extra["hello"]`` once it is saved, and a hub that has
    never saved this section gets ``HelloSettings()``'s own default,
    never ``None``.
    """
    own = settings.section("hello", HelloSettings)
    return {
        "timezone": state.timezone,
        "block_count": len(state.blocks),
        "greeting": own.greeting,
    }


def hello_flag(state: DashboardState, settings: HubSettings) -> bool:
    """Whether the footer should mark this page with "!" right now.

    This example never does. A real flag derives its reference time from
    ``state.updated_at`` and its condition from a block it reads, the way
    ``app/view.py:today_flag`` flags Today when a task is overdue.
    """
    return False


#: settings_model is HelloSettings: this example has one settings field, its
#: greeting, stored under the "hello" section (Module.section defaults to
#: the module id). See docs/MODULES.md, "Settings for a module".
MODULE = Module(
    id="hello",
    title="HELLO",
    version="1.0.0",
    description="Minimal example module: one page, no dataset, one settings field.",
    settings_model=HelloSettings,
    page=PageSpec(
        title="HELLO",
        templates_dir=HERE / "templates",
        template="hello.html",
        context=hello_context,
        render_ttl_seconds=300.0,
        needs=(),
        demo_datasets=(),
        flag=hello_flag,
    ),
    default_order=100,
)

__all__ = ["MODULE"]
