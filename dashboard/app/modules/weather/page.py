"""The Weather page's own context builder, its header widget, and the
helpers only they need.

Moved out of ``app/view.py`` in 2.2 (docs/plan/2026-09-19-settings-modules-
provisioning.md): everything here is read by no other page. Shared helpers
(formatting, header, footer, base context, ``is_heat``, which the widget
also uses) stay in ``app/view.py`` and are imported from there.

R.3 (docs/plan/2026-09-20-owner-feedback-round.md) moved the header's
weather reading here too, as :func:`weather_header`: the header's second
cell is a module's now, and weather is simply the module that fills it by
default.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, TYPE_CHECKING

from app import icons
from app.models import DashboardState, Weather, WeatherBlock
from app.view import base_context, block_note, fmt_number, is_heat

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.settings import HubSettings


def weather_header(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    """This module's header widget: a numeral, a glyph, and one state label.

    Was ``app/view.py:header_weather`` and is unchanged, markup included
    (``templates/weather_header.html``): the header slot became a module's
    in R.3 and weather is what fills it by default, so the pixels it draws
    had to stay exactly what they were.

    Heat outranks rain (a 40 degree feel is the thing to know first); a dry,
    cool reading gets the plain condition word with no dot and no colour.

    ``settings`` is unread: the reading is the block's, and the unit system
    is not yet a thing any page honours.
    """
    weather = state.block("weather", WeatherBlock).weather
    if not state.block("weather", WeatherBlock).usable or weather is None:
        return {"available": False}
    if is_heat(weather):
        color, label = "red", "HEAT"
    elif weather.rain_from:
        color, label = "blue", f"RAIN {weather.rain_from}"
    else:
        color, label = "", weather.condition.upper()
    return {
        "available": True,
        "temp": fmt_number(weather.temperature_c),
        "icon": icons.weather_icon(weather.condition),
        "color": color,
        "dot": color,
        "label": label,
    }


def weather_summary(weather: Weather | None) -> dict[str, Any]:
    """The hero reading: temperature, glyph, condition word, feels-like, and
    the one tell-tale the owner needs before reading anything else.

    Heat outranks rain, same ordering as :func:`weather_header`: a 40 degree
    feel is the thing to know first. A dry, cool day gets no dot at all.
    """
    if weather is None:
        return {"available": False}
    if is_heat(weather):
        dot = "red"
    elif (weather.rain_probability_percent or 0) >= 50 or weather.rain_from:
        dot = "blue"
    else:
        dot = ""
    return {
        "available": True,
        "temp": fmt_number(weather.temperature_c),
        "icon": icons.weather_icon(weather.condition),
        "condition": weather.condition.upper(),
        "feels": fmt_number(weather.feels_like_c),
        "dot": dot,
    }


def weather_readings(weather: Weather | None) -> list[dict[str, Any]]:
    """HUMIDITY, UV, AQI, RAIN: the four readings under the hero.

    Each carries its own tell-tale rule (UV only warns at yellow or red so a
    healthy reading stays quiet; AQI tells at every state including green
    because clean air is itself worth a glance at a glance). A missing value
    is ``None``, never a guessed zero: the template draws the hatch box for
    that in place of the numeral.
    """
    if weather is None:
        return [
            {"label": "HUMIDITY", "value": None, "dot": ""},
            {"label": "UV", "value": None, "dot": ""},
            {"label": "AQI", "value": None, "dot": ""},
            {"label": "RAIN", "value": None, "dot": ""},
        ]
    humidity = weather.humidity_percent
    uv = weather.uv_index
    aqi = weather.aqi
    rain = weather.rain_probability_percent
    uv_dot = uv_accent(uv)
    aqi_dot = aqi_accent(aqi)
    return [
        {"label": "HUMIDITY", "value": None if humidity is None else str(humidity), "dot": ""},
        {
            "label": "UV",
            "value": None if uv is None else f"{uv:.1f}",
            "dot": uv_dot if uv_dot in ("red", "yellow") else "",
        },
        {
            "label": "AQI",
            "value": None if aqi is None else str(aqi),
            "dot": aqi_dot if aqi_dot != "black" else "",
        },
        {
            "label": "RAIN",
            "value": None if rain is None else f"{rain}%",
            "dot": "blue" if (rain or 0) >= 50 else "",
        },
    ]


#: Matches weather.html's own ``.wx-plate { width: 64px; }``: the NEXT HOURS
#: baseline is sized in Python from this same figure so it always ends with
#: the last plate rather than running past whatever the block has.
WX_PLATE_WIDTH_PX: float = 64.0


def weather_hourly_plates(
    weather: Weather | None, reference: datetime, limit: int = 6
) -> list[dict[str, Any]]:
    """Up to ``limit`` hourly rain plates from the current hour onward."""
    if weather is None or not weather.hourly_rain:
        return []
    hour_start = reference.replace(minute=0, second=0, microsecond=0)
    upcoming = sorted(
        (item for item in weather.hourly_rain if item.at >= hour_start),
        key=lambda item: item.at,
    )
    return [
        {
            "hour": item.at.strftime("%H"),
            "percent": item.probability_percent,
            "mm": item.precipitation_mm,
            "fill": item.probability_percent >= 50,
        }
        for item in upcoming[:limit]
    ]


def weather_daily_rows(weather: Weather | None, today: date, limit: int = 7) -> list[dict[str, Any]]:
    """Up to ``limit`` days for the 7 DAYS pane, fixed-scale rain plates."""
    if weather is None:
        return []
    rows: list[dict[str, Any]] = []
    for item in weather.daily[:limit]:
        rain = item.rain_probability_percent
        rows.append(
            {
                "label": strip_day_label(item.day, today),
                "icon": icons.weather_icon(item.condition),
                "high": fmt_number(item.high_c) if item.high_c is not None else None,
                "low": fmt_number(item.low_c) if item.low_c is not None else None,
                "rain_percent": rain,
                "rain_fill": (rain or 0) >= 50,
            }
        )
    return rows


def weather_context(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    context = base_context(state, settings, "weather")
    weather = state.block("weather", WeatherBlock).weather if state.block("weather", WeatherBlock).usable else None
    reference: datetime = context["reference"]
    today: date = context["today"]

    context["weather_note"] = block_note(state.block("weather", WeatherBlock).status, "weather")
    context["hero"] = weather_summary(weather)
    context["readings"] = weather_readings(weather)
    context["hourly"] = weather_hourly_plates(weather, reference)
    context["hourly_span"] = (
        f"{context['hourly'][0]['hour']}:00 TO {context['hourly'][-1]['hour']}:00"
        if context["hourly"]
        else ""
    )
    context["wx_plates_width_px"] = len(context["hourly"]) * WX_PLATE_WIDTH_PX
    context["daily"] = weather_daily_rows(weather, today)
    context["location_name"] = settings.weather.location_name
    return context


def strip_day_label(day: date, today: date) -> str:
    """Day label for the five column forecast strip.

    Weekday names throughout, because a column is about 73 px wide inside its
    padding and TOMORROW needs 122 px at the 18 px legibility floor. Inventing
    a shorter word for it would put a second abbreviation in the world for no
    reason, so only TODAY, which fits, gets a relative name here. The word
    TOMORROW never appears anywhere in the product: every other day, near or
    far, prints its 3-letter weekday instead.
    """
    return "TODAY" if day == today else day.strftime("%a").upper()


def uv_accent(value: float | None) -> str:
    if value is None:
        return "black"
    if value >= 8:
        return "red"
    if value >= 6:
        return "yellow"
    return "green"


def pm25_accent(value: float | None) -> str:
    if value is None:
        return "black"
    if value >= 55:
        return "red"
    if value >= 25:
        return "yellow"
    return "green"


def aqi_accent(value: int | None) -> str:
    if value is None:
        return "black"
    if value > 150:
        return "red"
    if value > 50:
        return "yellow"
    return "green"


def weather_flag(state: DashboardState, settings: "HubSettings") -> bool:
    """Only air that is actually bad, never merely interesting weather."""
    block = state.block("weather", WeatherBlock)
    weather = block.weather
    if not block.usable or weather is None:
        return False
    air = (
        uv_accent(weather.uv_index),
        pm25_accent(weather.pm2_5),
        aqi_accent(weather.aqi),
    )
    return "red" in air


__all__ = [
    "WX_PLATE_WIDTH_PX",
    "aqi_accent",
    "pm25_accent",
    "strip_day_label",
    "uv_accent",
    "weather_context",
    "weather_daily_rows",
    "weather_flag",
    "weather_header",
    "weather_hourly_plates",
    "weather_readings",
    "weather_summary",
]
