"""Normalized models. Adapters produce these; templates consume only these.

The renderer never learns where a value came from, and no value is ever
invented: when an adapter cannot produce data the surrounding block carries
``status="unavailable"`` or ``status="error"`` and the page prints "unknown".
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class AdapterStatus(str, Enum):
    """Health of one adapter for the current state snapshot."""

    OK = "ok"
    #: Source is not configured, or the underlying file/entity does not exist.
    UNAVAILABLE = "unavailable"
    #: The fetch raised. The block may still carry the last good value.
    ERROR = "error"
    #: Last fetch failed but a previous value is being shown.
    STALE = "stale"


class Priority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


PRIORITY_RANK: dict[Priority, int] = {
    Priority.HIGH: 0,
    Priority.MEDIUM: 1,
    Priority.LOW: 2,
    Priority.NONE: 3,
}


class AlertPriority(str, Enum):
    """critical > doorbell > important > normal."""

    CRITICAL = "critical"
    DOORBELL = "doorbell"
    IMPORTANT = "important"
    NORMAL = "normal"


ALERT_PRIORITY_RANK: dict[AlertPriority, int] = {
    AlertPriority.CRITICAL: 3,
    AlertPriority.DOORBELL: 2,
    AlertPriority.IMPORTANT: 1,
    AlertPriority.NORMAL: 0,
}


class ServiceHealth(str, Enum):
    OK = "ok"
    WARN = "warn"
    DOWN = "down"
    UNKNOWN = "unknown"


class DeviceStatus(str, Enum):
    """Freshness of the newest telemetry sample the device posted."""

    #: Younger than :data:`DEVICE_STALE_AFTER_SECONDS`.
    OK = "ok"
    #: The device has reported before, but not recently enough.
    STALE = "stale"
    #: Nothing has ever been stored.
    UNAVAILABLE = "unavailable"


#: The device POSTs telemetry on this cadence (firmware/e1002.yaml).
DEVICE_INTERVAL_SECONDS: int = 300
#: Three missed reports in a row mean the sample on screen is stale.
DEVICE_STALE_AFTER_SECONDS: int = 3 * DEVICE_INTERVAL_SECONDS

#: Charge state as reported by the battery gauge. Unrecognized strings are
#: rejected rather than stored, same as every other telemetry field.
ChargeState = Literal["charging", "charged", "pre_charge", "not_charging", "unknown"]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
class Task(BaseModel):
    id: str
    title: str
    due: date | None = None
    priority: Priority = Priority.NONE
    completed: bool = False
    source: str = "fixture"
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
class Event(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime | None = None
    all_day: bool = False
    location: str | None = None
    source: str = "fixture"


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
class DailyForecast(BaseModel):
    day: date
    high_c: float | None = None
    low_c: float | None = None
    rain_probability_percent: int | None = None
    condition: str = "unknown"


class HourlyRain(BaseModel):
    """One hourly bucket used to derive "rain likely from HH:MM"."""

    at: datetime
    probability_percent: int
    precipitation_mm: float | None = None


class Weather(BaseModel):
    location_name: str = "unknown"
    observed_at: datetime | None = None
    temperature_c: float | None = None
    feels_like_c: float | None = None
    humidity_percent: int | None = None
    uv_index: float | None = None
    condition: str = "unknown"
    rain_probability_percent: int | None = None
    #: Local "HH:MM" of the first hour today whose rain probability crosses the
    #: threshold, or None when no rain is expected / data is missing.
    rain_from: str | None = None
    rain_until: str | None = None
    pm2_5: float | None = None
    aqi: int | None = None
    aqi_label: str | None = None
    daily: list[DailyForecast] = Field(default_factory=list)
    hourly_rain: list[HourlyRain] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AI usage / quota
# ---------------------------------------------------------------------------
class AIUsage(BaseModel):
    provider: str
    short_window_percent_remaining: int | None = None
    short_window_reset_at: datetime | None = None
    weekly_percent_remaining: int | None = None
    weekly_reset_at: datetime | None = None
    collected_at: datetime | None = None
    #: "ok" | "unknown" | "error" - reported per provider, never guessed.
    collection_status: str = "unknown"


# ---------------------------------------------------------------------------
# AI brief
# ---------------------------------------------------------------------------
class BriefMode(str, Enum):
    MORNING = "morning"
    EVENING = "evening"


class BriefSection(BaseModel):
    title: str
    items: list[str] = Field(default_factory=list)


class Brief(BaseModel):
    mode: BriefMode = BriefMode.MORNING
    generated_at: datetime | None = None
    headline: str = ""
    #: One short line reused on the Today page ("AI NOTE:").
    note: str = ""
    sections: list[BriefSection] = Field(default_factory=list)
    source: str = "fixture"


# ---------------------------------------------------------------------------
# Home / system
# ---------------------------------------------------------------------------
class HomeSensor(BaseModel):
    key: str
    name: str
    value: str | None = None
    unit: str | None = None
    #: "ok" | "warn" | "alert" | "unknown" - drives the accent color.
    severity: str = "unknown"


class ServiceStatus(BaseModel):
    key: str
    name: str
    health: ServiceHealth = ServiceHealth.UNKNOWN
    detail: str | None = None


class HomeState(BaseModel):
    sensors: list[HomeSensor] = Field(default_factory=list)
    services: list[ServiceStatus] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Device telemetry
# ---------------------------------------------------------------------------
class DeviceTelemetry(BaseModel):
    """Body of ``POST /api/device/telemetry``, exactly as ESPHome sends it.

    Every numeric field may be ``null``: on a cold boot the SHT4x and the
    battery gauge are not ready yet, and the firmware would rather report a
    hole than a made up reading.
    """

    device: str = Field(min_length=1, max_length=64)
    battery_voltage: float | None = None
    battery_level: float | None = None
    temperature: float | None = None
    humidity: float | None = None
    wifi_rssi: float | None = None
    uptime_s: float | None = None
    page: str | None = Field(default=None, max_length=32)
    #: Whether the gauge is in "on battery" mode. ``None`` on older firmware.
    battery_mode: bool | None = None
    #: Whether USB power is plugged in. ``None`` on older firmware.
    usb_present: bool | None = None
    #: Charger state, one of :data:`ChargeState`. ``None`` on older firmware.
    charge_state: ChargeState | None = None
    #: Why the device woke up, e.g. "power_on", "timer", "button_left". Free
    #: string (no enum), ``None`` on older firmware.
    wake_cause: str | None = Field(default=None, max_length=32)


class DeviceSample(DeviceTelemetry):
    """One stored telemetry row. ``received_at`` is when the hub accepted it."""

    device: str = Field(default="unknown", max_length=64)
    received_at: datetime


class DevicePoint(BaseModel):
    """One downsampled history point. ``None`` means "no reading in bucket"."""

    at: datetime
    temperature: float | None = None
    humidity: float | None = None


class DeviceState(BaseModel):
    """What the pages know about the desk device."""

    status: DeviceStatus = DeviceStatus.UNAVAILABLE
    device: str | None = None
    received_at: datetime | None = None
    #: Seconds between ``received_at`` and the moment the state was built.
    age_seconds: float | None = None

    battery_voltage: float | None = None
    battery_level: float | None = None
    temperature: float | None = None
    humidity: float | None = None
    wifi_rssi: float | None = None
    uptime_s: float | None = None
    page: str | None = None
    battery_mode: bool | None = None
    usb_present: bool | None = None
    charge_state: ChargeState | None = None
    wake_cause: str | None = None

    sample_count: int = 0
    oldest_at: datetime | None = None
    newest_at: datetime | None = None
    #: 24 hours of history, meaned into 15 minute buckets (at most 96 points).
    history_24h: list[DevicePoint] = Field(default_factory=list)

    @property
    def has_reading(self) -> bool:
        return self.status is not DeviceStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
class Alert(BaseModel):
    title: str
    message: str = ""
    priority: AlertPriority = AlertPriority.NORMAL
    created_at: datetime
    #: Seconds the device should keep the alert on screen.
    duration_seconds: int = 90
    beep: bool = True
    source: str = "api"

    @property
    def rank(self) -> int:
        return ALERT_PRIORITY_RANK[self.priority]


class AlertRequest(BaseModel):
    """Body of ``POST /api/alert``."""

    title: str = Field(min_length=1, max_length=60)
    message: str = Field(default="", max_length=240)
    priority: AlertPriority = AlertPriority.NORMAL
    duration_seconds: int = Field(default=90, ge=5, le=600)
    beep: bool = True


# ---------------------------------------------------------------------------
# State blocks
# ---------------------------------------------------------------------------
class Block(BaseModel):
    """Per-adapter envelope: status, when it was fetched, why it failed."""

    status: AdapterStatus = AdapterStatus.UNAVAILABLE
    source: str = "fixture"
    updated_at: datetime | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is AdapterStatus.OK

    @property
    def usable(self) -> bool:
        """True when the block carries real values (fresh or stale)."""
        return self.status in (AdapterStatus.OK, AdapterStatus.STALE)


class TasksBlock(Block):
    items: list[Task] = Field(default_factory=list)


class CalendarBlock(Block):
    items: list[Event] = Field(default_factory=list)


class WeatherBlock(Block):
    weather: Weather | None = None


class AIUsageBlock(Block):
    providers: list[AIUsage] = Field(default_factory=list)


class BriefBlock(Block):
    brief: Brief | None = None


class HomeBlock(Block):
    home: HomeState | None = None


class DeviceBlock(Block):
    device: DeviceState | None = None


class DashboardState(BaseModel):
    """Everything the templates are allowed to see."""

    generated_at: datetime
    timezone: str
    tasks: TasksBlock = Field(default_factory=TasksBlock)
    calendar: CalendarBlock = Field(default_factory=CalendarBlock)
    weather: WeatherBlock = Field(default_factory=WeatherBlock)
    ai_usage: AIUsageBlock = Field(default_factory=AIUsageBlock)
    brief: BriefBlock = Field(default_factory=BriefBlock)
    home: HomeBlock = Field(default_factory=HomeBlock)
    device: DeviceBlock = Field(default_factory=DeviceBlock)
    alert: Alert | None = None

    @property
    def blocks(self) -> dict[str, Block]:
        return {
            "tasks": self.tasks,
            "calendar": self.calendar,
            "weather": self.weather,
            "ai_usage": self.ai_usage,
            "brief": self.brief,
            "home": self.home,
            "device": self.device,
        }

    @property
    def updated_at(self) -> datetime:
        """Newest adapter timestamp, falling back to ``generated_at``."""
        stamps = [
            block.updated_at for block in self.blocks.values() if block.updated_at is not None
        ]
        return max(stamps) if stamps else self.generated_at
