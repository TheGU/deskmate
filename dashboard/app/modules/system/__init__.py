"""The System page and the two datasets it draws: ``home`` and ``device``.

``home`` is this module's own section. ``device`` is not: it is a core
section, because the device telemetry routes, the retention sweep and the
backup all read it whether or not this page is enabled. A module declaring
a dataset whose settings live in a core section is allowed and is what
``DatasetSpec.section`` is for; it just means the settings page keeps
showing that section when this module is turned off.
"""

from __future__ import annotations

from pathlib import Path

from app import __version__
from app.adapters.device import build_device_adapter
from app.adapters.home_assistant import build_home_adapter
from app.models import DeviceBlock, HomeBlock
from app.modules import DatasetSpec, Module, PageSpec
from app.modules.device.settings import SECTION as DEVICE_SECTION
from app.modules.home.settings import SECTION as HOME_SECTION, HomeSettings
from app.view import PAGE_NAMES, PAGE_PUSH_DATASETS, PAGE_TITLES, system_context, system_flag

#: The demo data ``home`` and ``device`` fall back to on ``source: fixture``.
HOME_FIXTURE: Path = Path(__file__).parent / "fixtures" / "home.json"
DEVICE_FIXTURE: Path = Path(__file__).parent / "fixtures" / "device.json"

MODULE = Module(
    id="system",
    title=PAGE_NAMES["system"],
    version=__version__,
    description="The house sensors, the service health rows and the panel's own device.",
    settings_model=HomeSettings,
    settings_section=HOME_SECTION,
    datasets=(
        DatasetSpec(
            name="home",
            block_model=HomeBlock,
            value_field="home",
            section=HOME_SECTION,
            build_adapter=lambda home, general, context: build_home_adapter(
                home, context.env, HOME_FIXTURE
            ),
            ttl_seconds=lambda home: home.ttl_seconds,
            fixture=HOME_FIXTURE,
        ),
        DatasetSpec(
            name="device",
            block_model=DeviceBlock,
            value_field="device",
            section=DEVICE_SECTION,
            build_adapter=lambda device, general, context: build_device_adapter(
                device, context.env, DEVICE_FIXTURE
            ),
            ttl_seconds=lambda device: device.ttl_seconds,
            fixture=DEVICE_FIXTURE,
        ),
    ),
    page=PageSpec(
        title=PAGE_TITLES["system"],
        templates_dir=Path(__file__).parent / "templates",
        template="system.html",
        context=system_context,
        render_ttl_seconds=900.0,
        needs=("home", "device"),
        demo_datasets=PAGE_PUSH_DATASETS["system"],
        flag=system_flag,
    ),
    default_order=50,
)

__all__ = ["DEVICE_FIXTURE", "HOME_FIXTURE", "MODULE"]
