"""Builds the frozen fixture state and its PNG hashes for the render gate.

Run from ``dashboard/``::

    uv run python tests/assets/freeze_state.py

This writes two files next to this script:

``frozen-state.json``
    ``DashboardState.model_dump_json`` of the same fixture state
    ``tests/conftest.py``'s session ``state`` fixture builds (every
    ``*_SOURCE=fixture``, a temp ``DATA_DIR``, ``TIMEZONE=Asia/Bangkok``), but
    with ``FIXTURE_RELATIVE_DATES=false`` (so the fixture dates are the
    literal dates in the fixture files, not shifted to "today") and with
    every block's ``updated_at`` and the state's own ``generated_at`` pinned
    to one fixed moment, so nothing in the frozen state depends on when this
    script happens to run.

``frozen-hashes.json`` (Windows) or ``frozen-hashes-linux.json`` (Linux)
    Maps each page in ``app.renderer.render.PAGES`` to the sha256 of its
    rendered PNG bytes, rendered from the frozen state above.

The PNG gate selects this platform's reviewed baseline: text rasterization
differs between Windows and Linux even with identical Chromium and fonts.
Pass ``--hashes-only`` to render the already committed shared frozen state
and update only this platform's hashes. Use it on both platforms after an
intentional render change; it never refreshes adapters or changes the state.

``dashboard/tests/test_render_gate.py`` re-renders every page from
``frozen-state.json`` and asserts its hash still matches ``frozen-hashes.json``.
Re-run this script with no flag only when a change to the rendered pixels is
intended; otherwise a mismatch there is a regression to fix, not a hash to
refresh.

The committed ``frozen-state.json`` is still written in the pre-2.1a shape
(the seven typed block fields at the top level, not the ``blocks`` mapping),
because rewriting it would mean rewriting the hashes beside it, and a gate
that refreshes its own expectation proves nothing.
``tests/test_render_gate.py:load_frozen_state`` reads either shape, and a
fresh run of this script writes the current one, so ``--check`` reports the
payload as differing for that reason as well as for the device block below.

Pass ``--check`` instead to prove the script still works (the point of
running it at all outside of an intentional pixel change) without touching
either committed file: it builds the state and renders every page the same
way, then compares against what is already on disk instead of writing.
Three pages are not compared byte-for-byte: ``system``, ``today`` and
``brief``, for the reasons in the note below.

Note: this script is not itself idempotent across runs. Two fixtures read
the wall clock, on purpose:

* the device fixture (app/adapters/device.py's ``load_device_fixture``)
  generates its 24 h history relative to "now" ("a fixed anchor date would
  make the demo device look permanently stale", per its own docstring), so
  the ``device`` block and the ``system`` page move between runs;
* the brief fixture picks the morning or the evening brief from the hour
  this script runs at (app/adapters/ai_brief.py's ``current_mode``), so the
  ``brief`` block, the ``brief`` page and the Today page's NOTE field flip
  across the section's ``evening_hour``.

Either way the content can differ between two invocations of this script
even though every timestamp on ``DashboardState`` itself is pinned above. That is fine for what this script is for: it freezes one
snapshot to disk once, and everything downstream (the render gate) only ever
renders that already-frozen, unchanging state, which is what makes the gate
itself byte-for-byte reproducible. It is also why ``--check`` cannot compare
those three pages' hashes against the committed ones: a fresh run's device
history, and a fresh run's brief mode, are not the ones already frozen on
disk, by design.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

#: dashboard/ - the directory ``app`` and ``tests`` import from. Needed
#: because running this file directly (not through pytest, which sets
#: pythonpath via pyproject.toml) only puts this script's own directory on
#: sys.path.
DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from app.alerts import AlertStore  # noqa: E402
from app.config import Env  # noqa: E402
from app.db import close_databases, get_database  # noqa: E402
from app.models import DashboardState  # noqa: E402
from app.modules.ai_usage.settings import AIUsageSettings  # noqa: E402
from app.modules.brief.settings import BriefSettings  # noqa: E402
from app.modules.calendar.settings import CalendarSettings  # noqa: E402
from app.modules.device.settings import DeviceSettings  # noqa: E402
from app.modules.general.settings import GeneralSettings  # noqa: E402
from app.modules.home.settings import HomeSettings  # noqa: E402
from app.modules.tasks.settings import TasksSettings  # noqa: E402
from app.modules.weather.settings import WeatherSettings  # noqa: E402
from app.renderer.render import PAGES, Renderer  # noqa: E402
from app.settings import HubSettings  # noqa: E402
from app.state import StateService  # noqa: E402
from tests.test_render_gate import HASHES_PATH, load_frozen_state  # noqa: E402

ASSETS_DIR = Path(__file__).resolve().parent
STATE_PATH = ASSETS_DIR / "frozen-state.json"

#: The one moment every block's ``updated_at`` and the state's ``generated_at``
#: are pinned to. Chosen as a plain, readable Asia/Bangkok morning; the exact
#: value carries no meaning beyond being fixed.
FROZEN_AT = datetime.fromisoformat("2026-09-19T09:00:00+07:00")

#: The pages whose rendered hashes are never compared in --check: they draw
#: a block whose fixture is wall-clock dependent (see module docstring).
#: ``today`` draws the brief's NOTE field, which is why it is here too.
_WALL_CLOCK_DEPENDENT_PAGES = ("system", "brief", "today")


def _env(data_dir: Path) -> Env:
    """Same environment knobs as ``tests/conftest.py``'s session fixture,
    except ``FIXTURE_RELATIVE_DATES=False``: the gate wants the literal
    fixture dates, not dates shifted to whatever day this script runs on.
    """
    return Env(
        _env_file=None,
        DATA_DIR=data_dir,
        FIXTURE_RELATIVE_DATES=False,
        LOG_LEVEL="WARNING",
    )


def _hub_settings() -> HubSettings:
    """Every source ``fixture``, the same story ``tests/conftest.py``'s
    session ``hub_settings`` fixture tells for the shared renderer/state."""
    return HubSettings(
        general=GeneralSettings(timezone="Asia/Bangkok"),
        tasks=TasksSettings(source="fixture"),
        calendar=CalendarSettings(source="fixture"),
        weather=WeatherSettings(source="fixture"),
        ai_usage=AIUsageSettings(source="fixture"),
        brief=BriefSettings(source="fixture"),
        home=HomeSettings(source="fixture"),
        device=DeviceSettings(source="fixture"),
    )


def _freeze_timestamps(state: DashboardState) -> DashboardState:
    """Pin every block's ``updated_at`` and the state's ``generated_at`` to
    :data:`FROZEN_AT`, so nothing in the frozen state depends on wall-clock
    time at freeze time."""
    frozen_blocks = {
        name: block.model_copy(update={"updated_at": FROZEN_AT})
        for name, block in state.blocks.items()
    }
    return state.model_copy(update={"generated_at": FROZEN_AT, "blocks": frozen_blocks})


async def _build_frozen_state(env: Env, hub_settings: HubSettings) -> DashboardState:
    database = get_database(env.hub_db_file)
    database.migrate()
    alerts = AlertStore(database, hub_settings.general.timezone)
    service = StateService(hub_settings, env, alerts)
    state = await service.build(force=True)
    return _freeze_timestamps(state)


def _dump_and_round_trip(state: DashboardState) -> str:
    payload = state.model_dump_json(indent=2) + "\n"
    # Confirm the JSON round-trips through the very loader the gate uses
    # (tests/test_render_gate.py:load_frozen_state): a change to models.py
    # that makes a Block subclass lose fields on the way back would
    # otherwise pass silently, since the state built above is never compared
    # against anything else in the write path.
    reloaded = load_frozen_state(payload)
    if reloaded != state:
        raise RuntimeError(
            "the freshly built state does not round-trip: "
            "load_frozen_state(...) != the model that produced it"
        )
    return payload


async def _render_hashes(
    env: Env, hub_settings: HubSettings, state: DashboardState
) -> dict[str, str]:
    renderer = Renderer(env, hub_settings)
    await renderer.start()
    try:
        hashes: dict[str, str] = {}
        for page in PAGES:
            png = await renderer.render_png(page, state)
            hashes[page] = hashlib.sha256(png).hexdigest()
        return hashes
    finally:
        await renderer.close()


async def _build(data_dir: Path) -> tuple[str, dict[str, str]]:
    env = _env(data_dir)
    hub_settings = _hub_settings()
    try:
        state = await _build_frozen_state(env, hub_settings)
        payload = _dump_and_round_trip(state)
        hashes = await _render_hashes(env, hub_settings, state)
    finally:
        # The database (and, through it, the fixture device adapter's
        # telemetry check) is process-wide and keyed by path; close it before
        # the temp directory is removed, or Windows refuses to delete a file
        # that is still open.
        close_databases()
    return payload, hashes


async def _check() -> int:
    """Build fresh and compare against what is already committed, changing
    nothing on disk. See the module docstring for why ``system`` is excluded
    from the hash comparison.
    """
    with tempfile.TemporaryDirectory() as tmp:
        payload, hashes = await _build(Path(tmp))

    ok = True

    expected_payload = STATE_PATH.read_text(encoding="utf-8")
    if payload != expected_payload:
        print(
            "frozen-state.json would differ from a fresh build "
            "(expected: the wall-clock-dependent device and brief blocks, and "
            "the committed file's pre-2.1a shape; unexpected for anything "
            "else - diff the two payloads by hand)",
            file=sys.stderr,
        )
        ok = False

    expected_hashes = json.loads(HASHES_PATH.read_text(encoding="utf-8"))
    for page in PAGES:
        if page in _WALL_CLOCK_DEPENDENT_PAGES:
            print(f"{page}: skipped (wall-clock dependent, see module docstring)")
            continue
        if hashes[page] != expected_hashes.get(page):
            print(
                f"{page}: hash mismatch (got {hashes[page]}, "
                f"expected {expected_hashes.get(page)})",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"{page}: matches {HASHES_PATH.name}")

    if ok:
        print("freeze_state.py --check: still builds a matching state and PNGs")
        return 0
    return 1


async def _write() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        payload, hashes = await _build(Path(tmp))

    STATE_PATH.write_text(payload, encoding="utf-8")
    HASHES_PATH.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"Updated shared state and {HASHES_PATH.name}; review and regenerate "
        "the other platform's hashes with --hashes-only too.", file=sys.stderr,
    )
    for page in PAGES:
        print(f"{page}: {hashes[page]}")
    return 0


async def _write_hashes_only() -> int:
    """Review another platform without replacing the shared frozen state."""
    state = load_frozen_state(STATE_PATH.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        hashes = await _render_hashes(_env(Path(tmp)), _hub_settings(), state)
    HASHES_PATH.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Updated {HASHES_PATH.name} from the committed frozen-state.json")
    for page, digest in hashes.items():
        print(f"{page}: {digest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="render and compare against the committed frozen assets instead of writing them",
    )
    mode.add_argument(
        "--hashes-only", action="store_true",
        help="update only this platform's hashes from the committed frozen state",
    )
    args = parser.parse_args()
    if args.check:
        return asyncio.run(_check())
    if args.hashes_only:
        return asyncio.run(_write_hashes_only())
    return asyncio.run(_write())


if __name__ == "__main__":
    raise SystemExit(main())
