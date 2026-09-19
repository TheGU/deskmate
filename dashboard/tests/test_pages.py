"""Every generated display image must be 800x480, valid PNG, palette-clean."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from PIL import Image

from app.adapters.device import build_device_state
from app.models import (
    AdapterStatus,
    AIUsage,
    AIUsageBlock,
    Alert,
    AlertPriority,
    Brief,
    BriefBlock,
    DashboardState,
    DeviceBlock,
    DeviceSample,
    HomeBlock,
    HomeState,
    HourlyRain,
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


def test_system_page_sensor_values_never_truncate(renderer: Renderer, state: DashboardState) -> None:
    """Neither HOME column truncates for the fixture's own rows (motion's
    "Clear 41M" included and FRONT DOOR's full name): the value column is
    sized to its own content (grid, not a fixed width), so the name column
    never has to give up space it needs."""
    truncated = run(
        renderer.probe(
            "system",
            state,
            """Array.from(document.querySelectorAll('.sensor-val, .sensor-row .label')).filter(
                 e => e.scrollWidth > e.clientWidth + 1
               ).map(e => [e.textContent, e.scrollWidth, e.clientWidth])""",
        )
    )
    assert truncated == [], truncated
    html = renderer.render_html("system", state, embed_fonts=False)
    assert "Clear 41M" in html
    assert "FRONT DOOR" in html


def test_system_page_draws_the_device_chart(renderer: Renderer, state: DashboardState) -> None:
    html = renderer.render_html("system", state, embed_fonts=False)
    assert "<polyline" in html
    # Black temperature, blue humidity: the panel has no gray, so the two
    # series are told apart by color alone, never red.
    assert 'stroke="#000000"' in html and 'stroke="#0000FF"' in html
    assert ">NOW<" in html
    assert "NO DEVICE DATA YET" not in html
    # The min and max temperature annotations, each with their own leader.
    assert "<line " in html


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
    assert 'class="sys-power-word">CHARGING<' in html

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
    assert 'class="sys-power-word">BATTERY<' in html

    image = open_png(run(renderer.render_png("system", on_battery)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)


def test_system_page_hides_power_row_when_unreported(
    renderer: Renderer, state: DashboardState
) -> None:
    unreported = state.model_copy(update={"device": _device_block_with_power(None, None)})
    html = renderer.render_html("system", unreported, embed_fonts=False)
    assert '<div class="sys-power-word">' not in html


#: Measured in the rendered document: does the header's right cluster (the
#: overdue tell-tale, Wi-Fi, battery and clock) ever run into the weather
#: reading beside it, and does the footer's window list fit beside its own
#: right edge.
_HEADER_GEOMETRY = """(() => {
  const round = value => Math.round(value * 10) / 10;
  const weather = document.querySelector('.hdr-weather').getBoundingClientRect();
  const right = document.querySelector('.hdr-right').getBoundingClientRect();
  return { weatherEnd: round(weather.right), rightStart: round(right.left) };
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
    return state.model_copy(update={"device": device, "tasks": tasks})


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
    return state.model_copy(update={"tasks": tasks, "weather": weather, "home": home})


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


def test_today_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("today", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def _today_state_with_stale_ai_usage_and_tasks(state: DashboardState) -> DashboardState:
    """AI CAPACITY and PRIORITIES both past their staleness threshold
    (AI_USAGE_STALE_SECONDS 21600 s / 6 h, TASKS_STALE_SECONDS 36000 s / 10 h),
    file-sourced so they are eligible to be marked at all (a fixture never
    is). The extra 10 minutes on each offset is a safety margin: the view
    layer's "now" is the newest block.updated_at, which lands a hair before
    ``state.generated_at`` (stamped after every adapter fetch completes), so
    an exact 8h/12h offset can float across the whole-hour floor and flip
    the asserted bucket by one.
    """
    old_usage = state.generated_at - timedelta(hours=8, minutes=10)
    old_tasks = state.generated_at - timedelta(hours=12, minutes=10)
    providers = [p.model_copy(update={"collected_at": old_usage}) for p in state.ai_usage.providers]
    return state.model_copy(
        update={
            "ai_usage": state.ai_usage.model_copy(update={"source": "file", "providers": providers}),
            "tasks": state.tasks.model_copy(update={"source": "file", "received_at": old_tasks}),
        }
    )


def test_today_page_fresh_has_no_stale_mark_and_stays_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    """Today, file-sourced but freshly received: no tell-tale, no age text,
    no DEMO (none of Today's three datasets, ai_usage/brief/tasks, is a
    fixture), palette clean, nothing overflowing."""
    # Every age source pinned to now (ai_usage: each provider's collected_at,
    # brief: generated_at, tasks: received_at, see view.py's *_stale). The
    # fixture's own timestamps sit in the morning, so without this the test
    # turned stale, and failed, every afternoon once the 6 h threshold passed.
    now = state.generated_at
    assert state.brief.brief is not None
    fresh = state.model_copy(
        update={
            "ai_usage": state.ai_usage.model_copy(
                update={
                    "source": "file",
                    "providers": [
                        p.model_copy(update={"collected_at": now}) for p in state.ai_usage.providers
                    ],
                }
            ),
            "brief": state.brief.model_copy(
                update={"source": "file", "brief": state.brief.brief.model_copy(update={"generated_at": now})}
            ),
            "tasks": state.tasks.model_copy(update={"source": "file", "received_at": now}),
        }
    )
    html = renderer.render_html("today", fresh, embed_fonts=False)
    assert 'class="today-stale-age"' not in html
    assert 'class="ftr-demo"' not in html

    image = open_png(run(renderer.render_png("today", fresh)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)
    overflow = run(renderer.probe("today", fresh, _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_today_page_stale_shows_tell_tale_and_age_and_stays_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    """AI CAPACITY and PRIORITIES both carry the yellow tell-tale plus a
    whole-hour age after the label, the footer flags "today", and the page
    is still palette-clean with nothing overflowing."""
    stale = _today_state_with_stale_ai_usage_and_tasks(state)
    html = renderer.render_html("today", stale, embed_fonts=False)
    assert html.count('<span class="today-stale-age">') == 2
    assert "8 H AGO" in html
    assert "12 H AGO" in html

    flagged_names = run(
        renderer.probe(
            "today",
            stale,
            """Array.from(document.querySelectorAll('.win'))
                 .filter(w => w.querySelector('.win-flag'))
                 .map(w => w.textContent.trim())""",
        )
    )
    assert any("TODAY" in name for name in flagged_names), flagged_names

    image = open_png(run(renderer.render_png("today", stale)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)
    overflow = run(renderer.probe("today", stale, _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_today_footer_shows_demo_for_the_default_fixture_state(
    renderer: Renderer, state: DashboardState
) -> None:
    """The shared fixture state's ai_usage/tasks are fixture-sourced (auto
    resolves to fixture with no DATA_DIR pushes), so a fresh install's
    footer must say DEMO rather than pass fixture numbers off as real."""
    assert state.ai_usage.source == "fixture"
    html = renderer.render_html("today", state, embed_fonts=False)
    assert '<span class="ftr-demo">DEMO</span>' in html

    image = open_png(run(renderer.render_png("today", state)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)
    overflow = run(renderer.probe("today", state, _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_today_footer_hides_demo_once_every_shown_dataset_is_file_sourced(
    renderer: Renderer, state: DashboardState
) -> None:
    """Today draws all three pushed datasets (ai_usage, brief via the NOTE
    field, and tasks), so DEMO only clears once none of the three is a
    fixture."""
    real = state.model_copy(
        update={
            "ai_usage": state.ai_usage.model_copy(update={"source": "file"}),
            "brief": state.brief.model_copy(update={"source": "file"}),
            "tasks": state.tasks.model_copy(update={"source": "file"}),
        }
    )
    html = renderer.render_html("today", real, embed_fonts=False)
    assert 'class="ftr-demo"' not in html


def test_today_footer_shows_demo_when_only_the_brief_is_still_a_fixture(
    renderer: Renderer, state: DashboardState
) -> None:
    """Regression: Today draws the brief note (the NOTE field), not just
    ai_usage and tasks, so a fixture-sourced brief alone must still show
    DEMO even when ai_usage and tasks are both file-sourced."""
    mixed = state.model_copy(
        update={
            "ai_usage": state.ai_usage.model_copy(update={"source": "file"}),
            "tasks": state.tasks.model_copy(update={"source": "file"}),
        }
    )
    assert mixed.brief.source == "fixture"
    html = renderer.render_html("today", mixed, embed_fonts=False)
    assert '<span class="ftr-demo">DEMO</span>' in html


def test_today_agenda_time_never_touches_the_title(renderer: Renderer, state: DashboardState) -> None:
    """The regression: a full "DAY HH:MM" time label ("MON 18:00") filled
    the fixed time column with no gap to the title beside it."""
    geometry = run(
        renderer.probe(
            "today",
            state,
            """Array.from(document.querySelectorAll('.agenda-row')).map(row => {
                 const t = row.querySelector('.agenda-time').getBoundingClientRect();
                 const title = row.querySelector('.agenda-title').getBoundingClientRect();
                 return {when: row.querySelector('.agenda-time').textContent, gap: title.left - t.right};
               })""",
        )
    )
    assert geometry, "no agenda rows in the fixture"
    for row in geometry:
        assert row["gap"] >= 12, row


def test_agenda_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("agenda", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_weather_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("weather", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def _weather_state_with_hourly_plates(count: int) -> DashboardState:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 9, 0, tzinfo=tz)
    hourly = [
        HourlyRain(at=reference + timedelta(hours=n), probability_percent=20, precipitation_mm=0.0)
        for n in range(count)
    ]
    weather = WeatherBlock(status=AdapterStatus.OK, weather=Weather(condition="Cloudy", hourly_rain=hourly))
    return DashboardState(generated_at=reference, timezone="Asia/Bangkok", weather=weather)


#: Whether the hero+readings group has grown to absorb the column's slack,
#: and how much gap (if any) is left below the plates, before the rule.
_WX_NEXT_HOURS_GEOMETRY = """(() => {
  const group = document.querySelector('.wx-hero-group');
  const plates = document.querySelector('.wx-plates');
  const left = document.querySelector('.wx-left');
  return {
    groupGrows: group.classList.contains('wx-hero-group-grow'),
    gapBelowPlates: Math.round((left.getBoundingClientRect().bottom - plates.getBoundingClientRect().bottom) * 10) / 10,
  };
})()"""


def test_weather_next_hours_collapses_with_fewer_than_four_plates(renderer: Renderer) -> None:
    """With fewer than four plates the block collapses to its own content
    height: no gap is left below the plates, the slack moves above the rule
    (more air around the hero and readings) instead."""
    geometry = run(renderer.probe("weather", _weather_state_with_hourly_plates(2), _WX_NEXT_HOURS_GEOMETRY))
    assert geometry["groupGrows"] is True
    assert geometry["gapBelowPlates"] <= 1, geometry


def test_weather_next_hours_keeps_its_shape_with_four_or_more_plates(renderer: Renderer) -> None:
    """With four or more plates the block is as it is: the hero and readings
    do not grow to absorb any slack."""
    geometry = run(renderer.probe("weather", _weather_state_with_hourly_plates(4), _WX_NEXT_HOURS_GEOMETRY))
    assert geometry["groupGrows"] is False

    geometry = run(renderer.probe("weather", _weather_state_with_hourly_plates(6), _WX_NEXT_HOURS_GEOMETRY))
    assert geometry["groupGrows"] is False


_WX_BASELINE_GEOMETRY = """(() => {
  const baseline = document.querySelector('.wx-baseline').getBoundingClientRect();
  const plates = Array.from(document.querySelectorAll('.wx-plate'));
  const last = plates[plates.length - 1].getBoundingClientRect();
  return {
    baselineRight: Math.round(baseline.right * 10) / 10,
    lastPlateRight: Math.round(last.right * 10) / 10,
  };
})()"""


def test_weather_next_hours_baseline_ends_with_the_last_plate(renderer: Renderer) -> None:
    """The baseline never advertises hours the block does not have: its
    right edge tracks the last plate's right edge, not the full block
    width, whether there are two plates or six."""
    two = run(renderer.probe("weather", _weather_state_with_hourly_plates(2), _WX_BASELINE_GEOMETRY))
    assert abs(two["baselineRight"] - two["lastPlateRight"]) <= 2, two

    six = run(renderer.probe("weather", _weather_state_with_hourly_plates(6), _WX_BASELINE_GEOMETRY))
    assert abs(six["baselineRight"] - six["lastPlateRight"]) <= 2, six


def test_brief_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("brief", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_brief_task_titles_never_truncate_mid_word(renderer: Renderer, state: DashboardState) -> None:
    """Every ``.brief-task-title`` either reads the fixture's real title in
    full or ends in the single ellipsis character :func:`clip_words` uses;
    the CSS ellipsis is a safety net only, so it should not be the one
    actually firing here."""
    fixture_titles = {task.title for task in state.tasks.items}
    rows = run(
        renderer.probe(
            "brief",
            state,
            """Array.from(document.querySelectorAll('.brief-task-title')).map(
                 e => [e.textContent, e.scrollWidth, e.clientWidth]
               )""",
        )
    )
    for text, scroll_width, client_width in rows:
        if text.endswith("…"):
            continue
        assert text in fixture_titles, f"unexpected title text: {text}"
        assert scroll_width <= client_width + 1, f"{text} truncated without an ellipsis"


def test_system_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    overflow = run(renderer.probe("system", _widest_header_state(state), _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


def test_system_without_device_data_has_no_element_overflowing_the_800x480_box(
    renderer: Renderer, state: DashboardState
) -> None:
    blank = _widest_header_state(state).model_copy(
        update={
            "device": DeviceBlock(
                status=AdapterStatus.UNAVAILABLE,
                source="store",
                error="the device has not posted any telemetry yet",
            )
        }
    )
    overflow = run(renderer.probe("system", blank, _VIEWPORT_OVERFLOW))
    assert overflow == [], overflow


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


def test_system_row_lists_do_not_overflow_their_box(
    renderer: Renderer, state: DashboardState
) -> None:
    """``.sys-row-list`` is not ``.pane-body``: the System page's HUB and
    merged HOME columns get their own probe, so a HUB or HOME list that no
    longer fits its box (a merged HOME+SERVICES list that clips, a HUB list
    taller than the shrunk sys-top leaves room for) fails loudly instead of
    silently scrolling under ``overflow: hidden``.
    """
    overflow = run(
        renderer.probe(
            "system",
            state,
            """Array.from(document.querySelectorAll('.sys-row-list')).filter(
                 e => e.scrollHeight > e.clientHeight + 1
               ).map(e => [e.parentElement.className, e.scrollHeight, e.clientHeight])""",
        )
    )
    assert overflow == [], f"system: {overflow}"


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
