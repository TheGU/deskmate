"""System page tests: moved out of tests/test_view.py and tests/test_pages.py in
2.2 (docs/plan/2026-09-19-settings-modules-provisioning.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any


from app.settings import HubSettings
from app.models import (
    AdapterStatus,
    DashboardState,
    DeviceBlock,
    DeviceSample,
    DeviceState,
    DeviceStatus,
    HomeBlock,
    HomeSensor,
    HomeState,
    ServiceHealth,
    ServiceStatus,
    TasksBlock,
)
from app.view import (
    wifi_level,
)
from app.modules.system.page import (
    HOME_ROW_BUDGET,
    battery_accent,
    cap_duration,
    dataset_age_label,
    desk_accent,
    device_panel,
    hub_rows,
    power_label,
    sensor_accent,
    sensor_value,
    service_mark,
    strip_scheme,
    system_context,
    wake_label,
    wifi_accent,
)
from tests.conftest import make_state


from app.adapters.device import build_device_state
from app.renderer.palette import DISPLAY_SIZE, assert_palette, palette_violations
from app.renderer.render import Renderer
from app.telemetry import TelemetrySummary
from tests.conftest import open_png, run, with_blocks
from tests.test_pages import _VIEWPORT_OVERFLOW, _widest_header_state


def test_sensor_value_formatting() -> None:
    assert sensor_value(None, None) == "unknown"
    assert sensor_value("Closed", None) == "Closed"
    assert sensor_value("64", "%") == "64%"
    assert sensor_value("27.8", "C") == "27.8 C"
    # A trailing duration inside free text is capped, matching the panel's
    # own all-caps numerals and units elsewhere ("41M", not "41m").
    assert sensor_value("Clear 41m", None) == "Clear 41M"


def test_cap_duration_only_touches_a_trailing_duration() -> None:
    assert cap_duration("Clear 41m") == "Clear 41M"
    assert cap_duration("Idle") == "Idle"
    assert cap_duration("Home") == "Home"
    assert cap_duration("Ready in 3h") == "Ready in 3H"


# ---------------------------------------------------------------------------
# agenda: the plain event list, the month grid, the next-seven-days strip
# ---------------------------------------------------------------------------
def test_battery_accent_thresholds() -> None:
    assert battery_accent(None) == "black"
    assert battery_accent(21) == "black"
    assert battery_accent(20) == "yellow"
    assert battery_accent(11) == "yellow"
    assert battery_accent(10) == "red"


def _device_state(**overrides: Any) -> DeviceState:
    base: dict[str, Any] = dict(
        status=DeviceStatus.OK,
        device="reterminal-e1002",
        received_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        age_seconds=30.0,
    )
    base.update(overrides)
    return DeviceState(**base)


def test_device_panel_hatches_the_battery_block_when_level_is_none(hub_settings: HubSettings) -> None:
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        device=DeviceBlock(status=AdapterStatus.OK, device=_device_state(battery_level=None)),
    )
    panel = device_panel(state, hub_settings)
    assert panel["available"] is True
    assert panel["battery_available"] is False


def test_device_panel_flags_stale_with_a_tell_tale_and_age(hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc)
    device = _device_state(
        status=DeviceStatus.STALE, received_at=now - timedelta(hours=2), age_seconds=7200.0
    )
    state = make_state(
        generated_at=now, timezone="Asia/Bangkok", device=DeviceBlock(status=AdapterStatus.OK, device=device)
    )
    panel = device_panel(state, hub_settings)
    assert panel["stale"] is True
    assert panel["age_text"] == "2 H AGO"
    assert desk_accent(state) == "yellow"


def test_device_panel_carries_the_plug_icon_and_usb_present(hub_settings: HubSettings) -> None:
    """device_panel's battery_icon and usb_present agree with icons.battery_icon
    and power_label: a plug glyph on usb_present true, battery glyphs on
    false, and None keeps the plain battery glyph."""
    from app import icons

    now = datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc)

    charging = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        device=DeviceBlock(
            status=AdapterStatus.OK,
            device=_device_state(battery_level=80.0, usb_present=True, charge_state="charging"),
        ),
    )
    panel = device_panel(charging, hub_settings)
    assert panel["usb_present"] is True
    assert panel["battery_icon"] == icons.POWER_PLUG

    on_battery = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        device=DeviceBlock(
            status=AdapterStatus.OK,
            device=_device_state(battery_level=80.0, usb_present=False, charge_state="not_charging"),
        ),
    )
    panel = device_panel(on_battery, hub_settings)
    assert panel["usb_present"] is False
    assert panel["battery_icon"] != icons.POWER_PLUG

    unreported = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        device=DeviceBlock(status=AdapterStatus.OK, device=_device_state(battery_level=80.0)),
    )
    panel = device_panel(unreported, hub_settings)
    assert panel["usb_present"] is None
    assert panel["battery_icon"] == icons.battery_icon(80.0)


def test_wifi_level_glyph_choice_matches_the_system_reading() -> None:
    assert wifi_level(-60) == "strong"
    assert wifi_level(-75) == "low"
    assert wifi_level(None) == "off"
    assert wifi_accent(-60) == "green"
    assert wifi_accent(-75) == "yellow"
    assert wifi_accent(None) == "black"


def test_sensor_accent_only_flags_warn_and_alert() -> None:
    assert sensor_accent("ok") == ""
    assert sensor_accent("unknown") == ""
    assert sensor_accent("warn") == "yellow"
    assert sensor_accent("alert") == "red"


def test_service_mark_per_health() -> None:
    assert service_mark("ok") == "black"
    assert service_mark("warn") == "yellow"
    assert service_mark("down") == "red"
    assert service_mark("unknown") == "hatch"


def test_system_context_sensor_and_service_rows(hub_settings: HubSettings) -> None:
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        home=HomeBlock(
            status=AdapterStatus.OK,
            home=HomeState(
                sensors=[
                    HomeSensor(key="front_door", name="Front door", value="Closed", severity="ok")
                ],
                services=[ServiceStatus(key="nas", name="NAS", health=ServiceHealth.DOWN)],
            ),
        ),
    )
    context = system_context(state, hub_settings)
    home_rows = context["home_rows"]
    sensor_row = next(row for row in home_rows if row["kind"] == "sensor")
    service_row = next(row for row in home_rows if row["kind"] == "service")
    assert sensor_row["accent"] == ""
    assert service_row["mark"] == "red"
    assert service_row["down"] is True
    # Sensors first: with one of each, the sensor row leads the merged list.
    assert home_rows[0]["kind"] == "sensor"
    assert home_rows[1]["kind"] == "service"


def test_home_rows_merge_sensors_and_services_under_one_budget(hub_settings: HubSettings) -> None:
    """A merged HOME column with more sensors and services than fit is
    capped to HOME_ROW_BUDGET total, sensors first, not per half."""
    sensors = [
        HomeSensor(key=f"s{i}", name=f"Sensor {i}", value="1", severity="ok")
        for i in range(HOME_ROW_BUDGET)
    ]
    services = [
        ServiceStatus(key=f"svc{i}", name=f"Service {i}", health=ServiceHealth.OK)
        for i in range(HOME_ROW_BUDGET)
    ]
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, 0, tzinfo=dt_timezone.utc),
        timezone="Asia/Bangkok",
        home=HomeBlock(status=AdapterStatus.OK, home=HomeState(sensors=sensors, services=services)),
    )
    context = system_context(state, hub_settings)
    home_rows = context["home_rows"]
    assert len(home_rows) == HOME_ROW_BUDGET
    assert all(row["kind"] == "sensor" for row in home_rows)


def test_dataset_age_label_buckets() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    assert dataset_age_label(None, now) == "NEVER"
    assert dataset_age_label(now, now) == "NOW"
    assert dataset_age_label(now - timedelta(minutes=5), now) == "5 MIN"
    assert dataset_age_label(now - timedelta(hours=2), now) == "2 H"
    assert dataset_age_label(now - timedelta(days=3), now) == "3 D"


def test_strip_scheme_drops_the_scheme_only() -> None:
    assert strip_scheme("https://192.0.2.10:8080") == "192.0.2.10:8080"
    assert strip_scheme("http://hub.local") == "hub.local"


def test_hub_rows_mark_a_stale_pushed_dataset_yellow(hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        tasks=TasksBlock(
            status=AdapterStatus.OK,
            source="agent",
            updated_at=now - timedelta(hours=20),
            received_at=now - timedelta(hours=20),
        ),
    )
    rows = hub_rows(state, hub_settings, now)
    tasks_row = next(row for row in rows if row["name"] == "TASKS")
    assert tasks_row["value"] == "20 H"
    assert tasks_row["accent"] == "yellow"


def test_hub_rows_device_origin_from_device_state(hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    device = DeviceState(
        status=DeviceStatus.OK,
        device="reterminal-e1002",
        received_at=now,
        age_seconds=0.0,
        newest_at=now - timedelta(minutes=5),
        remote_addr="192.0.2.10",
        hub_host="https://192.0.2.1:8080",
    )
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        device=DeviceBlock(status=AdapterStatus.OK, device=device),
    )
    rows = hub_rows(state, hub_settings, now)
    by_name = {row["name"]: row for row in rows}
    assert by_name["DEVICE SYNC"]["value"] == "5 MIN"
    assert by_name["DEVICE IP"]["value"] == "192.0.2.10"
    assert by_name["HUB URL"]["value"] == "192.0.2.1:8080"


def test_power_label_words_feed_the_battery_meter_caption() -> None:
    assert power_label(False, None) == "BATTERY"
    assert power_label(True, "charging") == "CHARGING"
    assert power_label(True, "pre_charge") == "CHARGING"
    assert power_label(True, "charged") == "USB"
    # Any other usb_present-true charge state still gets a word: the plug
    # glyph and a blank caption underneath it would be a lie by omission.
    assert power_label(True, "unknown") == "USB"
    assert power_label(True, "not_charging") == "USB"
    assert power_label(True, None) == "USB"
    assert power_label(None, None) is None


def test_wake_label_spaces_out_the_firmware_word() -> None:
    assert wake_label(None) == "UNKNOWN"
    assert wake_label("button_left") == "BUTTON LEFT"


# ---------------------------------------------------------------------------
# alert page
# ---------------------------------------------------------------------------


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
    blank = with_blocks(
        state,
        device=DeviceBlock(
            status=AdapterStatus.UNAVAILABLE,
            source="store",
            error="the device has not posted any telemetry yet",
        ),
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
    charging = with_blocks(state, device=_device_block_with_power(True, "charging"))
    html = renderer.render_html("system", charging, embed_fonts=False)
    assert 'class="sys-power-word">CHARGING<' in html

    image = open_png(run(renderer.render_png("system", charging)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)


def test_system_page_shows_power_on_battery_and_stays_palette_clean(
    renderer: Renderer, state: DashboardState
) -> None:
    on_battery = with_blocks(state, device=_device_block_with_power(False, "not_charging"))
    html = renderer.render_html("system", on_battery, embed_fonts=False)
    assert 'class="sys-power-word">BATTERY<' in html

    image = open_png(run(renderer.render_png("system", on_battery)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)


def test_system_page_hides_power_row_when_unreported(
    renderer: Renderer, state: DashboardState
) -> None:
    unreported = with_blocks(state, device=_device_block_with_power(None, None))
    html = renderer.render_html("system", unreported, embed_fonts=False)
    assert '<div class="sys-power-word">' not in html


#: Measured in the rendered document: does the header's right cluster (the
#: overdue tell-tale, Wi-Fi, battery and clock) ever run into the weather
#: reading beside it, and does the footer's window list fit beside its own
#: right edge.
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
