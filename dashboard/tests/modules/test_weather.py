"""Weather page tests: moved out of tests/test_view.py and tests/test_pages.py in
2.2 (docs/plan/2026-09-19-settings-modules-provisioning.md)."""

from __future__ import annotations

from datetime import date, datetime, timedelta


from app.models import (
    AdapterStatus,
    DailyForecast,
    DashboardState,
    HourlyRain,
    Weather,
    WeatherBlock,
)
from app.timeutil import zone
from app.modules.weather.page import (
    weather_daily_rows,
    weather_hourly_plates,
    weather_readings,
    weather_summary,
)
from tests.conftest import make_state


from app.renderer.render import Renderer
from tests.conftest import run
from tests.test_pages import _VIEWPORT_OVERFLOW, _widest_header_state


def test_weather_summary_hero_states() -> None:
    heat = weather_summary(Weather(condition="Sunny", temperature_c=37.0, rain_from="15:00"))
    assert heat["dot"] == "red"
    rain = weather_summary(Weather(condition="Showers", temperature_c=29.0, rain_probability_percent=60))
    assert rain["dot"] == "blue"
    plain = weather_summary(Weather(condition="Cloudy", temperature_c=28.0, rain_probability_percent=10))
    assert plain["dot"] == ""
    assert weather_summary(None) == {"available": False}


def test_weather_readings_carry_unavailable_flags_and_selective_dots() -> None:
    empty = weather_readings(None)
    assert all(reading["value"] is None for reading in empty)

    full = weather_readings(
        Weather(humidity_percent=76, uv_index=8.4, aqi=71, rain_probability_percent=80)
    )
    by_label = {reading["label"]: reading for reading in full}
    assert by_label["HUMIDITY"]["value"] == "76"
    assert by_label["HUMIDITY"]["dot"] == ""
    assert by_label["UV"]["dot"] == "red"
    assert by_label["AQI"]["dot"] == "yellow"
    assert by_label["RAIN"]["value"] == "80%"
    assert by_label["RAIN"]["dot"] == "blue"

    # A healthy UV reading stays quiet; a healthy AQI still dots (green).
    calm = weather_readings(Weather(uv_index=2.0, aqi=10))
    calm_by_label = {reading["label"]: reading for reading in calm}
    assert calm_by_label["UV"]["dot"] == ""
    assert calm_by_label["AQI"]["dot"] == "green"


def test_weather_hourly_plates_start_at_current_hour_and_cap_at_six() -> None:
    tz = zone("Asia/Bangkok")
    reference = datetime(2026, 9, 4, 14, 30, tzinfo=tz)
    hourly = [
        HourlyRain(at=datetime(2026, 9, 4, hour, 0, tzinfo=tz), probability_percent=hour)
        for hour in range(9, 22)
    ]
    weather = Weather(hourly_rain=hourly)
    plates = weather_hourly_plates(weather, reference)
    assert len(plates) == 6
    assert plates[0]["hour"] == "14"
    assert plates[-1]["hour"] == "19"
    assert weather_hourly_plates(None, reference) == []


def test_weather_daily_rows_cap_at_seven() -> None:
    today = date(2026, 9, 4)
    daily = [
        DailyForecast(day=today + timedelta(days=offset), high_c=30.0, low_c=24.0, rain_probability_percent=10)
        for offset in range(10)
    ]
    weather = Weather(daily=daily)
    rows = weather_daily_rows(weather, today)
    assert len(rows) == 7
    assert rows[0]["label"] == "TODAY"
    assert weather_daily_rows(None, today) == []


# ---------------------------------------------------------------------------
# brief page
# ---------------------------------------------------------------------------


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
    return make_state(generated_at=reference, timezone="Asia/Bangkok", weather=weather)


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
