"""Render every page to ``output/<page>.png`` without starting the server.

Usage (from the repository root)::

    cd dashboard && uv run python ../scripts/render-all.py

Prints size, byte count and the palette check for each page.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

# Every *_SOURCE now defaults to a live selector, so a plain run of this
# script would render six honest "unavailable" blocks instead of the demo
# pages a developer expects. Pin all seven to fixture, same as the tests.
# setdefault so a developer's own env/`.env` (or an explicit empty-state run)
# can still override any of them.
os.environ.setdefault("TASKS_SOURCE", "fixture")
os.environ.setdefault("CALENDAR_SOURCE", "fixture")
os.environ.setdefault("WEATHER_SOURCE", "fixture")
os.environ.setdefault("AI_USAGE_SOURCE", "fixture")
os.environ.setdefault("BRIEF_SOURCE", "fixture")
os.environ.setdefault("HA_SOURCE", "fixture")
os.environ.setdefault("DEVICE_SOURCE", "fixture")

from PIL import Image  # noqa: E402

from app.alerts import AlertStore  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.renderer.palette import DISPLAY_SIZE, palette_violations  # noqa: E402
from app.renderer.render import PAGES, Renderer  # noqa: E402
from app.state import StateService  # noqa: E402


async def render_all(output_dir: Path) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    output_dir.mkdir(parents=True, exist_ok=True)

    alerts = AlertStore(settings.alert_file, settings.timezone)
    service = StateService(settings, alerts)
    renderer = Renderer(settings)
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
    return failures


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "output"
    failures = asyncio.run(render_all(output_dir))
    if failures:
        print(f"{failures} page(s) failed the size or palette check")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
