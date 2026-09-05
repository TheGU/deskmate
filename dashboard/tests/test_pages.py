"""Every generated display image must be 800x480, valid PNG, palette-clean."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from PIL import Image

from app.adapters.device import build_device_state
from app.models import AdapterStatus, DashboardState, DeviceBlock, DeviceSample, Task, TasksBlock
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


def test_render_rgb_is_800x480_and_pre_quantization(renderer: Renderer, state: DashboardState) -> None:
    """The RGB stage is downsampled to panel size but not yet snapped to six inks."""
    image = run(renderer.render_rgb("today", state))
    assert image.mode == "RGB"
    assert image.size == DISPLAY_SIZE
    assert len(image.getcolors(maxcolors=1 << 24) or []) > len(PALETTE_RGB)


@pytest.mark.parametrize("page", PAGES)
def test_page_uses_at_least_one_panel_color(rendered: dict[str, bytes], page: str) -> None:
    """The world commits to color: a page that is only black on white is a bug.

    Every page carries at least a colored status bar segment, a chip, a meter
    or a title bar, so a page rendering in pure monochrome means an accent was
    dropped somewhere between ``view.py`` and the template.
    """
    image = open_png(rendered[page]).convert("RGB")
    monochrome = {(255, 255, 255), (0, 0, 0)}
    colors = {color for _count, color in (image.getcolors(maxcolors=1 << 24) or [])}
    assert colors - monochrome, f"{page} renders without a single colored pixel"


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


#: Measured in the rendered document: where the two halves of the status band
#: start and end, and whether the window list still fits beside the Wi-Fi
#: reading at the foot.
_BAND_GEOMETRY = """(() => {
  const round = value => Math.round(value * 10) / 10;
  const left = document.querySelector('.cluster-left').getBoundingClientRect();
  const right = document.querySelector('.cluster-right').getBoundingClientRect();
  const wins = document.querySelector('.wins').getBoundingClientRect();
  const foot = document.querySelector('.winbar-right');
  return {
    leftEnd: round(left.right),
    rightStart: round(right.left),
    winsEnd: round(wins.right),
    footStart: foot ? round(foot.getBoundingClientRect().left) : 800,
    footHeight: foot ? Math.round(foot.getBoundingClientRect().height) : 0,
  };
})()"""


def _widest_status_bar(state: DashboardState) -> DashboardState:
    """The state that makes the top band as wide as it can honestly get.

    A full battery is the widest percentage, a three-digit RSSI the widest
    Wi-Fi reading, and a fortnight of neglect the widest overdue count.
    """
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = DeviceSample(
        received_at=now,
        device="reterminal-e1002",
        temperature=32.0,
        humidity=55.0,
        battery_level=100.0,
        wifi_rssi=-100.0,
        usb_present=True,
        charge_state="charged",
    )
    device = DeviceBlock(
        status=AdapterStatus.OK,
        source="store",
        device=build_device_state(
            latest=latest,
            history=[latest],
            summary=TelemetrySummary(sample_count=1, oldest=now, newest=now),
            now=now,
        ),
    )
    late = date(2026, 9, 5) - timedelta(days=3)
    tasks = TasksBlock(
        status=AdapterStatus.OK,
        items=[Task(id=str(n), title=f"Overdue {n}", due=late) for n in range(12)],
    )
    return state.model_copy(update={"device": device, "tasks": tasks})


@pytest.mark.parametrize("page", PAGES)
def test_status_band_halves_never_collide(
    renderer: Renderer, state: DashboardState, page: str
) -> None:
    """The two halves of the band keep white paper between them."""
    geometry = run(renderer.probe(page, _widest_status_bar(state), _BAND_GEOMETRY))
    gap = geometry["rightStart"] - geometry["leftEnd"]
    assert gap >= 8, f"{page}: band halves {gap} px apart, {geometry}"


@pytest.mark.parametrize("page", PAGES)
def test_window_list_fits_beside_the_wifi_reading(
    renderer: Renderer, state: DashboardState, page: str
) -> None:
    geometry = run(renderer.probe(page, _widest_status_bar(state), _BAND_GEOMETRY))
    assert geometry["winsEnd"] <= geometry["footStart"], geometry
    # A taller box would mean the bar overflowed and wrapped onto two lines.
    assert geometry["footHeight"] in (0, 40), geometry


@pytest.mark.parametrize("page", PAGES)
def test_no_pane_overflows_its_own_box(
    renderer: Renderer, state: DashboardState, page: str
) -> None:
    """Nothing is clipped by a pane that was not sized for it.

    Rows that truncate opt in with ``clip`` or ``clamp2``; anything else whose
    content scrolls is a layout that no longer fits.
    """
    overflow = run(
        renderer.probe(
            page,
            state,
            """Array.from(document.querySelectorAll('.pane-body')).filter(
                 e => e.scrollHeight > e.clientHeight + 1
               ).map(e => [e.parentElement.className, e.scrollHeight, e.clientHeight])""",
        )
    )
    assert overflow == [], f"{page}: {overflow}"


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
