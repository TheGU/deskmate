"""Render every page to ``output/<page>.png`` without starting the server.

Usage (from the repository root)::

    cd dashboard && uv run python ../scripts/render-all.py

Prints size, byte count and the palette check for each page.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from PIL import Image  # noqa: E402

from app.alerts import AlertStore  # noqa: E402
from app.config import Env  # noqa: E402
from app.db import close_databases, get_database  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.modules.ai_usage.settings import AIUsageSettings  # noqa: E402
from app.modules.brief.settings import BriefSettings  # noqa: E402
from app.modules.calendar.settings import CalendarSettings  # noqa: E402
from app.modules.device.settings import DeviceSettings  # noqa: E402
from app.modules.general.settings import GeneralSettings  # noqa: E402
from app.modules.home.settings import HomeSettings  # noqa: E402
from app.modules.tasks.settings import TasksSettings  # noqa: E402
from app.modules.weather.settings import WeatherSettings  # noqa: E402
from app.renderer.palette import DISPLAY_SIZE, palette_violations  # noqa: E402
from app.renderer.render import PAGES, Renderer  # noqa: E402
from app.settings import HubSettings  # noqa: E402
from app.state import StateService  # noqa: E402


def _hub_settings() -> HubSettings:
    """Every source ``fixture``: this script's whole point is the demo pages,
    the same story ``tests/assets/freeze_state.py`` and the test suite's
    session ``hub_settings`` fixture tell the shared renderer and state
    service. There is no settings database involved here at all: this
    ``HubSettings`` is handed straight to ``StateService`` and ``Renderer``,
    never read from or written to ``DATA_DIR/deskmate.sqlite``.
    """
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


async def render_all(output_dir: Path) -> int:
    env = Env()
    configure_logging(env.log_level)
    output_dir.mkdir(parents=True, exist_ok=True)

    hub_settings = _hub_settings()
    # AlertStore still needs a real (migrated) database to read the alert row
    # from: it is the one section this script does not pin to fixture, since
    # there is no fixture alert to show instead of a real one.
    database = get_database(env.hub_db_file)
    database.migrate()
    alerts = AlertStore(database, hub_settings.general.timezone)
    service = StateService(hub_settings, env, alerts)
    renderer = Renderer(env, hub_settings)
    failures = 0
    try:
        await renderer.start()
        state = await service.build(force=True)
        for page in PAGES:
            png = await renderer.render_png(page, state)
            target = output_dir / f"{page}.png"
            target.write_bytes(png)
            image = Image.open(io.BytesIO(png))
            image.load()
            extra = palette_violations(image)
            size_ok = image.size == DISPLAY_SIZE
            status = "ok" if (size_ok and not extra) else "FAIL"
            if status == "FAIL":
                failures += 1
            print(
                f"{page:<8} {image.size[0]}x{image.size[1]} "
                f"{len(png):>7} bytes  colors={len(image.convert('RGB').getcolors(1 << 24) or [])}  "
                f"palette={'clean' if not extra else sorted(extra)[:4]}  {status}  -> {target}"
            )
    finally:
        await renderer.close()
        # Windows refuses to remove a file with an open sqlite connection, so
        # a caller that goes on to clean up a temp DATA_DIR needs this closed.
        close_databases()
    return failures


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "output"
    failures = asyncio.run(render_all(output_dir))
    if failures:
        print(f"{failures} page(s) failed the size or palette check")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
