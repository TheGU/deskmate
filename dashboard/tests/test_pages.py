"""Every generated display image must be 800x480, valid PNG, palette-clean."""

from __future__ import annotations

import io
from datetime import datetime, timezone as dt_timezone

import pytest
from PIL import Image

from app.adapters.device import build_device_state
from app.models import AdapterStatus, DashboardState, DeviceBlock, DeviceSample
from app.renderer.palette import DISPLAY_SIZE, PALETTE_RGB, assert_palette, palette_violations
from app.renderer.render import PAGES, Renderer
from app.telemetry import TelemetrySummary
from tests.conftest import open_png, run


@pytest.fixture(scope="module")
def rendered(renderer: Renderer, state: DashboardState) -> dict[str, bytes]:
    return {page: run(renderer.render_png(page, state)) for page in PAGES}


@pytest.mark.parametrize("page", PAGES)
def test_page_is_valid_png(rendered: dict[str, bytes], page: str) -> None:
    Image.open(io.BytesIO(rendered[page])).verify()


@pytest.mark.parametrize("page", PAGES)
def test_page_is_exactly_800x480(rendered: dict[str, bytes], page: str) -> None:
    assert open_png(rendered[page]).size == DISPLAY_SIZE


@pytest.mark.parametrize("page", PAGES)
def test_page_is_palette_constrained(rendered: dict[str, bytes], page: str) -> None:
    image = open_png(rendered[page])
    assert palette_violations(image) == set()
    assert_palette(image)


@pytest.mark.parametrize("page", PAGES)
def test_page_is_not_interlaced(rendered: dict[str, bytes], page: str) -> None:
    # ESPHome's PNG decoder cannot read interlaced files.
    image = Image.open(io.BytesIO(rendered[page]))
    assert image.info.get("interlace", 0) in (0, None)


@pytest.mark.parametrize("page", PAGES)
def test_rendering_is_deterministic(
    renderer: Renderer, state: DashboardState, rendered: dict[str, bytes], page: str
) -> None:
    again = run(renderer.render_png(page, state))
    assert again == rendered[page], f"{page} is not byte-identical on a second render"


def test_system_page_draws_the_device_chart(renderer: Renderer, state: DashboardState) -> None:
    html = renderer.render_html("system", state, embed_fonts=False)
    assert "<polyline" in html
    assert 'stroke="#FF0000"' in html and 'stroke="#0000FF"' in html
    assert ">NOW<" in html
    assert "NO DEVICE DATA YET" not in html


def test_system_page_without_device_data_is_still_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    """No device, no chart: the panel says so rather than drawing a flat line."""
    blank = state.model_copy(
        update={
            "device": DeviceBlock(
                status=AdapterStatus.UNAVAILABLE,
                source="store",
                error="the device has not posted any telemetry yet",
            )
        }
    )
    assert "NO DEVICE DATA YET" in renderer.render_html("system", blank, embed_fonts=False)

    image = open_png(run(renderer.render_png("system", blank)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)


def _device_block_with_power(usb_present: bool | None, charge_state: str | None) -> DeviceBlock:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = DeviceSample(
        received_at=now,
        device="reterminal-e1002",
        temperature=30.0,
        humidity=50.0,
        usb_present=usb_present,
        charge_state=charge_state,
    )
    return DeviceBlock(
        status=AdapterStatus.OK,
        source="store",
        device=build_device_state(
            latest=latest,
            history=[latest],
            summary=TelemetrySummary(sample_count=1, oldest=now, newest=now),
            now=now,
        ),
    )


def test_system_page_shows_power_on_usb_and_stays_palette_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    charging = state.model_copy(
        update={"device": _device_block_with_power(True, "charging")}
    )
    html = renderer.render_html("system", charging, embed_fonts=False)
    assert "ON USB, CHARGING" in html

    image = open_png(run(renderer.render_png("system", charging)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)


def test_system_page_shows_power_on_battery_and_stays_palette_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    on_battery = state.model_copy(
        update={"device": _device_block_with_power(False, "not_charging")}
    )
    html = renderer.render_html("system", on_battery, embed_fonts=False)
    assert "ON BATTERY" in html

    image = open_png(run(renderer.render_png("system", on_battery)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)


def test_system_page_hides_power_row_when_unreported(
    renderer: Renderer, state: DashboardState
) -> None:
    unreported = state.model_copy(update={"device": _device_block_with_power(None, None)})
    html = renderer.render_html("system", unreported, embed_fonts=False)
    assert "ON USB" not in html
    assert "ON BATTERY" not in html


def test_pages_cover_the_documented_set() -> None:
    assert PAGES == ("today", "agenda", "weather", "brief", "system", "alert")


def test_palette_has_the_six_panel_colors() -> None:
    assert PALETTE_RGB == (
        (255, 255, 255),
        (0, 0, 0),
        (255, 0, 0),
        (255, 255, 0),
        (0, 255, 0),
        (0, 0, 255),
    )
