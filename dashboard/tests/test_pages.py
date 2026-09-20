"""Every generated display image must be 800x480, valid PNG, palette-clean.

Cross-page and core render-pipeline checks (the PNG gate's own sanity
checks, the header/footer/pane-overflow probes, the alert page). Each
built-in page's own render regression tests moved into
tests/modules/test_<id>.py in 2.2
(docs/plan/2026-09-19-settings-modules-provisioning.md).
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from PIL import Image

from app.adapters.device import build_device_state
from app.models import (
    AdapterStatus,
    Alert,
    AlertPriority,
    DashboardState,
    DeviceBlock,
    DeviceSample,
    HomeBlock,
    HomeState,
    ServiceHealth,
    ServiceStatus,
    Task,
    TasksBlock,
    Weather,
    WeatherBlock,
)
from app.renderer.palette import DISPLAY_SIZE, PALETTE_RGB, assert_palette, palette_violations
from app.renderer.render import PAGES, Renderer
from app.telemetry import TelemetrySummary
from app.timeutil import zone
from tests.conftest import open_png, run, with_blocks


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


_HEADER_GEOMETRY = """(() => {
  const round = value => Math.round(value * 10) / 10;
  const weather = document.querySelector('.hdr-weather').getBoundingClientRect();
  const right = document.querySelector('.hdr-right').getBoundingClientRect();
  return { weatherEnd: round(weather.right), rightStart: round(right.left) };
})()"""

#: The day stack and the two boxes the year line must not move: the header's
#: own 64 px and the top of the body under it.
_DAY_STACK_GEOMETRY = """(() => {
  const round = value => Math.round(value * 10) / 10;
  const header = document.querySelector('.hdr').getBoundingClientRect();
  const body = document.querySelector('.body').getBoundingClientRect();
  const lines = Array.from(document.querySelectorAll('.hdr-day-stack span'));
  return {
    headerHeight: round(header.height),
    headerBottom: round(header.bottom),
    bodyTop: round(body.top),
    lines: lines.map(line => ({
      text: line.textContent.trim(),
      size: getComputedStyle(line).fontSize,
      bottom: round(line.getBoundingClientRect().bottom),
    })),
  };
})()"""

_FOOTER_GEOMETRY = """(() => {
  const wins = Array.from(document.querySelectorAll('.win'));
  const list = document.querySelector('.win-list').getBoundingClientRect();
  return {
    count: wins.length,
    names: wins.map(w => w.textContent.trim()),
    listRight: Math.round(list.right * 10) / 10,
  };
})()"""


@pytest.mark.parametrize("page", PAGES)
def test_header_day_stack_is_three_16_px_lines_inside_the_64_px_header(
    renderer: Renderer, state: DashboardState, page: str
) -> None:
    """The year line (R.3, docs/plan/2026-09-20-owner-feedback-round.md,
    finding 10a) is a third 16 px line in the same stack, not a taller
    header: three lines at line-height 1.2 are 57.6 px, which still fits the
    64 px box, so the body below starts exactly where it always did and only
    the numeral beside the stack moves (down 1.6 px, as the stack recentres).
    """
    geometry = run(renderer.probe(page, _widest_header_state(state), _DAY_STACK_GEOMETRY))
    assert geometry["headerHeight"] == 64.0, geometry
    assert geometry["bodyTop"] == 66.0, geometry
    lines = geometry["lines"]
    assert len(lines) == 3, lines
    assert [line["size"] for line in lines] == ["16px"] * 3, lines
    assert lines[2]["text"].isdigit() and len(lines[2]["text"]) == 4, lines
    for line in lines:
        assert line["bottom"] <= geometry["headerBottom"] + 0.5, geometry


def _widest_header_state(state: DashboardState) -> DashboardState:
    """The state that makes the header's right cluster as wide as it can
    honestly get: a full battery, a strong Wi-Fi reading, and a fortnight of
    neglect for the overdue count."""
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
    return with_blocks(state, device=device, tasks=tasks)


def _all_flags_state(state: DashboardState) -> DashboardState:
    """The state that flags every flaggable window (agenda, weather, system),
    for the footer's widest honest width."""
    late = date(2026, 9, 5) - timedelta(days=1)
    tasks = TasksBlock(
        status=AdapterStatus.OK, items=[Task(id="late", title="Late", due=late)]
    )
    weather = WeatherBlock(
        status=AdapterStatus.OK, weather=Weather(condition="Hazy", uv_index=9.5)
    )
    home = HomeBlock(
        status=AdapterStatus.OK,
        home=HomeState(services=[ServiceStatus(key="nas", name="NAS", health=ServiceHealth.DOWN)]),
    )
    return with_blocks(state, tasks=tasks, weather=weather, home=home)


@pytest.mark.parametrize("page", PAGES)
def test_header_overdue_chip_hidden_only_on_pages_with_their_own(
    renderer: Renderer, state: DashboardState, page: str
) -> None:
    """Today and Brief already show the overdue task in their own red 1D
    LATE chip; the header's own chip renders everywhere else."""
    html = renderer.render_html(page, _widest_header_state(state), embed_fonts=False)
    has_chip = 'class="chip chip-red hdr-pill"' in html
    assert has_chip == (page not in ("today", "brief")), (page, has_chip)


@pytest.mark.parametrize("page", PAGES)
def test_header_right_cluster_never_overlaps_the_weather_reading(
    renderer: Renderer, state: DashboardState, page: str
) -> None:
    """The header's two halves keep white paper between them."""
    geometry = run(renderer.probe(page, _widest_header_state(state), _HEADER_GEOMETRY))
    gap = geometry["rightStart"] - geometry["weatherEnd"]
    assert gap >= 8, f"{page}: header halves {gap} px apart, {geometry}"


@pytest.mark.parametrize("page", PAGES)
def test_footer_window_list_fits_with_every_name_and_flag(
    renderer: Renderer, state: DashboardState, page: str
) -> None:
    geometry = run(renderer.probe(page, _all_flags_state(state), _FOOTER_GEOMETRY))
    assert geometry["count"] == 5, geometry
    assert geometry["listRight"] <= 800, geometry


#: Every visible element's bounding rect, so nothing on the fully rebuilt
#: Today page can silently spill outside the 800x480 panel.
_VIEWPORT_OVERFLOW = """(() => {
  const bad = [];
  document.querySelectorAll('body *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    if (r.left < -0.5 || r.top < -0.5 || r.right > 800.5 || r.bottom > 480.5) {
      bad.push([el.className, Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)]);
    }
  });
  return bad;
})()"""


def test_alert_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("alert", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def _doorbell_alert_state(state: DashboardState) -> DashboardState:
    tz = zone("Asia/Bangkok")
    alert = Alert(
        title="Someone at the door",
        message="Front door camera saw motion.",
        priority=AlertPriority.DOORBELL,
        created_at=datetime(2026, 9, 5, 15, 6, tzinfo=tz),
        source="Front door",
    )
    return state.model_copy(update={"alert": alert})


@pytest.mark.parametrize(
    "priority,expected_class",
    [
        (AlertPriority.CRITICAL, "alert-band-red"),
        (AlertPriority.DOORBELL, "alert-band-red"),
        (AlertPriority.IMPORTANT, "alert-band-yellow"),
        (AlertPriority.NORMAL, "alert-band-black"),
    ],
)
def test_alert_band_class_matches_priority(
    renderer: Renderer, state: DashboardState, priority: AlertPriority, expected_class: str
) -> None:
    tz = zone("Asia/Bangkok")
    alert = Alert(title="Test", priority=priority, created_at=datetime(2026, 9, 5, 15, 6, tzinfo=tz))
    html = renderer.render_html("alert", state.model_copy(update={"alert": alert}), embed_fonts=False)
    assert f'class="alert-band {expected_class}"' in html


def test_alert_band_paints_red_with_white_text_for_a_doorbell_alert(
    renderer: Renderer, state: DashboardState
) -> None:
    """The regression this guards: batch 1 deleted every ``.f-*`` rule from
    base.html, so a doorbell alert's band rendered black text on white
    (priority invisible) instead of the red band with white text the
    priority demands."""
    png = run(renderer.render_png("alert", _doorbell_alert_state(state)))
    image = open_png(png).convert("RGB")
    band = image.crop((0, 66, 800, 122))
    colors = {color for _count, color in (band.getcolors(maxcolors=1 << 24) or [])}
    assert (255, 0, 0) in colors, f"no pure red in the band rows: {colors}"
    assert (255, 255, 255) in colors, f"no white text in the band rows: {colors}"
    assert (0, 0, 0) not in colors, f"black leaked into the band rows: {colors}"


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
