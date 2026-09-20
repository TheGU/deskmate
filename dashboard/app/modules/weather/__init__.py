"""The ``weather`` section (``settings.py``), its dataset and its page.

Phase 1 parked the section model here; 2.1a adds the ``MODULE`` beside it.
Weather is fetched, never pushed, so it is never a DEMO dataset: a
fixture-sourced forecast has always been the hub's honest empty state, not
something an agent forgot to send.
"""

from __future__ import annotations

from app import __version__
from app.adapters.weather import build_weather_adapter
from app.config import APP_DIR
from app.models import WeatherBlock
from app.modules import DatasetSpec, Module, PageSpec
from app.modules.weather.settings import SECTION, WeatherSettings
from app.view import PAGE_NAMES, PAGE_PUSH_DATASETS, PAGE_TITLES, weather_context, weather_flag

MODULE = Module(
    id="weather",
    title=PAGE_NAMES["weather"],
    version=__version__,
    description="Current conditions, the rain and air readings, and the week ahead.",
    settings_model=WeatherSettings,
    settings_section=SECTION,
    datasets=(
        DatasetSpec(
            name="weather",
            block_model=WeatherBlock,
            value_field="weather",
            section=SECTION,
            build_adapter=lambda weather, general, context: build_weather_adapter(
                weather, general, context.env
            ),
            ttl_seconds=lambda weather: weather.ttl_seconds,
        ),
    ),
    page=PageSpec(
        title=PAGE_TITLES["weather"],
        templates_dir=APP_DIR / "templates",
        template="weather.html",
        context=weather_context,
        render_ttl_seconds=3600.0,
        needs=("weather",),
        demo_datasets=PAGE_PUSH_DATASETS["weather"],
        flag=weather_flag,
    ),
    default_order=30,
)

__all__ = ["MODULE"]
