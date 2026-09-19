"""Weather adapters: fixtures and Open-Meteo (no API key required).

Open-Meteo is used twice: the forecast API for temperature, humidity, UV and
hourly precipitation probability, and the air-quality API for PM2.5 and US AQI.
Both take latitude, longitude and a timezone, so nothing is hard coded: without
``WEATHER_LATITUDE`` / ``WEATHER_LONGITUDE`` the adapter reports
``unavailable`` rather than guessing a location.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Final

import httpx

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_tree
from app.config import Env
from app.logging_setup import log
from app.models import DailyForecast, HourlyRain, Weather
from app.modules.general.settings import GeneralSettings
from app.modules.weather.settings import WeatherSettings
from app.timeutil import now_local, to_local, today_local

logger = logging.getLogger("app.adapters.weather")

FORECAST_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL: Final[str] = "https://air-quality-api.open-meteo.com/v1/air-quality"

#: Hourly probability at or above this counts as "rain likely".
RAIN_THRESHOLD_PERCENT: Final[int] = 50

#: WMO weather interpretation codes, condensed to short e-paper labels.
WMO_CODES: Final[dict[int, str]] = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Showers",
    81: "Showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorms",
    96: "Storms with hail",
    99: "Storms with hail",
}

#: US AQI breakpoints, used only to label a value the API already computed.
AQI_LABELS: Final[tuple[tuple[int, str], ...]] = (
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for sensitive"),
    (200, "Unhealthy"),
    (300, "Very unhealthy"),
    (10_000, "Hazardous"),
)

DATE_KEYS: Final[frozenset[str]] = frozenset({"observed_at", "day", "at"})


def aqi_label(value: int | None) -> str | None:
    if value is None:
        return None
    for limit, label in AQI_LABELS:
        if value <= limit:
            return label
    return None


def condition_from_code(code: int | None) -> str:
    if code is None:
        return "unknown"
    return WMO_CODES.get(int(code), "unknown")


def rain_window(
    hourly: list[HourlyRain], reference: datetime, threshold: int = RAIN_THRESHOLD_PERCENT
) -> tuple[str | None, str | None]:
    """First and last "HH:MM" from ``reference`` where rain is likely today."""
    upcoming = [
        item
        for item in hourly
        if item.at >= reference.replace(minute=0, second=0, microsecond=0)
        and item.at.date() == reference.date()
        and item.probability_percent >= threshold
    ]
    if not upcoming:
        return None, None
    return upcoming[0].at.strftime("%H:%M"), upcoming[-1].at.strftime("%H:%M")


class FixtureWeatherAdapter:
    """Weather from ``fixtures/weather.json``."""

    name = "weather"
    source = "fixture"

    def __init__(self, weather: WeatherSettings, general: GeneralSettings, env: Env) -> None:
        self._weather = weather
        self._general = general
        self._env = env

    async def fetch(self) -> Weather:
        env = self._env
        timezone_name = self._general.timezone
        payload = load_fixture(env.fixtures_dir / "weather.json")
        delta = day_delta(
            payload, today_local(timezone_name), enabled=env.fixture_relative_dates
        )
        raw: Any = shift_tree(payload.get("weather", {}), delta, DATE_KEYS)
        weather = Weather.model_validate(raw)
        if weather.observed_at is not None:
            weather.observed_at = to_local(weather.observed_at, timezone_name)
        weather.hourly_rain = [
            HourlyRain(
                at=to_local(item.at, timezone_name),
                probability_percent=item.probability_percent,
                precipitation_mm=item.precipitation_mm,
            )
            for item in weather.hourly_rain
        ]
        if self._weather.location_name:
            weather.location_name = self._weather.location_name
        return weather


class OpenMeteoWeatherAdapter:
    """Live weather from Open-Meteo plus its air-quality endpoint."""

    name = "weather"
    source = "open_meteo"

    def __init__(self, weather: WeatherSettings, general: GeneralSettings, env: Env) -> None:
        self._weather = weather
        self._general = general
        self._env = env

    async def fetch(self) -> Weather:
        weather = self._weather
        if weather.latitude is None or weather.longitude is None:
            raise AdapterUnavailable("weather latitude / longitude are not set")

        common = {
            "latitude": weather.latitude,
            "longitude": weather.longitude,
            "timezone": self._general.timezone,
        }
        forecast_params = {
            **common,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,precipitation",
            "hourly": "precipitation_probability,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
            "forecast_days": 6,
            "temperature_unit": "celsius" if self._general.units == "metric" else "fahrenheit",
        }
        air_params = {**common, "current": "pm2_5,us_aqi"}

        async with httpx.AsyncClient(timeout=self._env.http_timeout_seconds) as client:
            forecast_response = await client.get(FORECAST_URL, params=forecast_params)
            forecast_response.raise_for_status()
            forecast: dict[str, Any] = forecast_response.json()
            air: dict[str, Any] = {}
            try:
                air_response = await client.get(AIR_QUALITY_URL, params=air_params)
                air_response.raise_for_status()
                air = air_response.json()
            except httpx.HTTPError as exc:
                # Air quality is optional: the rest of the page must still work.
                log(logger, logging.WARNING, "air quality unavailable", error=str(exc))

        return self._build(forecast, air)

    def _build(self, forecast: dict[str, Any], air: dict[str, Any]) -> Weather:
        weather = self._weather
        timezone_name = self._general.timezone
        current: dict[str, Any] = forecast.get("current", {}) or {}
        daily_raw: dict[str, Any] = forecast.get("daily", {}) or {}
        hourly_raw: dict[str, Any] = forecast.get("hourly", {}) or {}

        daily: list[DailyForecast] = []
        days: list[str] = daily_raw.get("time", []) or []
        for index, day_text in enumerate(days):
            daily.append(
                DailyForecast(
                    day=date.fromisoformat(day_text),
                    high_c=_at(daily_raw.get("temperature_2m_max"), index),
                    low_c=_at(daily_raw.get("temperature_2m_min"), index),
                    rain_probability_percent=_int_at(
                        daily_raw.get("precipitation_probability_max"), index
                    ),
                    condition=condition_from_code(_int_at(daily_raw.get("weather_code"), index)),
                )
            )

        reference = now_local(timezone_name)
        horizon = reference + timedelta(hours=24)
        hourly: list[HourlyRain] = []
        for index, stamp in enumerate(hourly_raw.get("time", []) or []):
            moment = to_local(datetime.fromisoformat(stamp), timezone_name)
            if moment < reference - timedelta(hours=1) or moment > horizon:
                continue
            probability = _int_at(hourly_raw.get("precipitation_probability"), index)
            hourly.append(
                HourlyRain(
                    at=moment,
                    probability_percent=probability if probability is not None else 0,
                    precipitation_mm=_at(hourly_raw.get("precipitation"), index),
                )
            )

        rain_from, rain_until = rain_window(hourly, reference)
        observed_at: datetime | None = None
        if current.get("time"):
            observed_at = to_local(datetime.fromisoformat(current["time"]), timezone_name)

        air_current: dict[str, Any] = air.get("current", {}) or {}
        aqi_value = air_current.get("us_aqi")
        aqi = int(round(aqi_value)) if isinstance(aqi_value, (int, float)) else None

        today = today_local(timezone_name)
        today_daily = next((item for item in daily if item.day == today), None)

        return Weather(
            location_name=weather.location_name
            or f"{weather.latitude:.2f},{weather.longitude:.2f}",
            observed_at=observed_at,
            temperature_c=current.get("temperature_2m"),
            feels_like_c=current.get("apparent_temperature"),
            humidity_percent=_as_int(current.get("relative_humidity_2m")),
            uv_index=_at(daily_raw.get("uv_index_max"), _index_of(days, today)),
            condition=condition_from_code(_as_int(current.get("weather_code"))),
            rain_probability_percent=(
                today_daily.rain_probability_percent if today_daily is not None else None
            ),
            rain_from=rain_from,
            rain_until=rain_until,
            pm2_5=air_current.get("pm2_5"),
            aqi=aqi,
            aqi_label=aqi_label(aqi),
            daily=daily,
            hourly_rain=hourly,
        )


def _index_of(days: list[str], today: date) -> int:
    text = today.isoformat()
    return days.index(text) if text in days else 0


def _at(values: Any, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    return float(value) if isinstance(value, (int, float)) else None


def _int_at(values: Any, index: int) -> int | None:
    value = _at(values, index)
    return int(round(value)) if value is not None else None


def _as_int(value: Any) -> int | None:
    return int(round(value)) if isinstance(value, (int, float)) else None


def build_weather_adapter(
    weather: WeatherSettings, general: GeneralSettings, env: Env
) -> FixtureWeatherAdapter | OpenMeteoWeatherAdapter:
    if weather.source == "open_meteo":
        return OpenMeteoWeatherAdapter(weather, general, env)
    return FixtureWeatherAdapter(weather, general, env)
