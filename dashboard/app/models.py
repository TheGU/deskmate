"""Normalized models. Adapters produce these; templates consume only these.

The renderer never learns where a value came from, and no value is ever
invented: when an adapter cannot produce data the surrounding block carries
``status="unavailable"`` or ``status="error"`` and the page prints "unknown".
"""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from enum import Enum
from typing import Annotated, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
)


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
    #: Which feed the event came from ("work", "personal", an ICS feed name).
    #: The pages colour the event's time by this, so two calendars are told
    #: apart without a label. ``None`` means one unnamed calendar.
    calendar: str | None = Field(default=None, max_length=32)


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
    #: Which slot of the window list the device is on, 0-based. The device
    #: navigates by index and only learns the ids from a telemetry response,
    #: so a session that has not had one yet can name the slot but not the
    #: page: ``page`` is authoritative and this is the fallback the hub
    #: resolves through the registry (``app/main.py:resolve_telemetry_page``).
    #: Never stored: what lands in the ``page`` column is always an id.
    #: ``None`` on older firmware.
    page_index: int | None = None
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

    #: Origin of the newest POST (telemetry.py's ``latest_origin``): who sent
    #: it and which hub URL they used. ``None`` for the fixture device (it
    #: never posted) and for a fresh store. Distinct from every other field
    #: above, which describes the device's own reading.
    remote_addr: str | None = None
    hub_host: str | None = None

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
    #: What raised the alert, in the sender's own words ("Front door"). The
    #: alert page puts it in the title bar. Optional: a caller that does not
    #: send one gets the priority class there instead.
    source: str | None = Field(default=None, max_length=48)

    @property
    def rank(self) -> int:
        return ALERT_PRIORITY_RANK[self.priority]


class AlertRequest(BaseModel):
    """Body of ``POST /api/alert``."""

    title: str = Field(min_length=1, max_length=60)
    message: str = Field(default="", max_length=240)
    priority: AlertPriority = AlertPriority.NORMAL
    #: ``None`` means "use the alert section's configured default"
    #: (app/modules/alert/settings.py's ``default_duration_seconds``),
    #: resolved by the ``POST /api/alert`` route, not by this model.
    duration_seconds: int | None = Field(default=None, ge=5, le=600)
    beep: bool = True
    source: str | None = Field(default=None, max_length=48)


# ---------------------------------------------------------------------------
# Push endpoints (docs/DATA-SOURCES.md): a remote agent's own data, not
# fetched by the hub. Every push body forbids unknown fields and pins its
# shape to ``schema_version`` 1, same discipline as :class:`AlertRequest`.
# ---------------------------------------------------------------------------
def _utc_now() -> datetime:
    return datetime.now(tz=dt_timezone.utc)


class SchemaVersioned(BaseModel):
    """Shared envelope: unknown fields and any schema but 1 are a 422."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=1,
        ge=1,
        le=1,
        description="Payload shape version. Only 1 is understood today.",
    )


class AIUsageProviderPush(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=32)
    #: Percent of quota REMAINING (not used), 0 to 100, or null when unknown.
    short_window_percent_remaining: int | None = Field(default=None, ge=0, le=100)
    short_window_reset_at: AwareDatetime | None = Field(
        default=None,
        description="Must carry a UTC offset (e.g. a trailing Z or +07:00). "
        "A naive value is rejected with 422 rather than assumed to be the "
        "hub's own timezone.",
    )
    #: Percent of quota REMAINING (not used), 0 to 100, or null when unknown.
    weekly_percent_remaining: int | None = Field(default=None, ge=0, le=100)
    weekly_reset_at: AwareDatetime | None = Field(
        default=None,
        description="Must carry a UTC offset; a naive value is rejected (see short_window_reset_at).",
    )
    collected_at: AwareDatetime = Field(
        default_factory=_utc_now,
        description="Must carry a UTC offset; a naive value is rejected (see short_window_reset_at).",
    )


class AIUsagePush(SchemaVersioned):
    """Body of ``POST /api/ai-usage``."""

    providers: list[AIUsageProviderPush] = Field(min_length=1, max_length=8)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "schema_version": 1,
                "providers": [
                    {
                        "provider": "claude",
                        "short_window_percent_remaining": 62,
                        "weekly_percent_remaining": 40,
                    }
                ],
            }
        }
    )


class BriefSectionPush(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=40)
    items: list[Annotated[str, Field(max_length=160)]] = Field(
        default_factory=list, max_length=12
    )


class BriefPush(SchemaVersioned):
    """Body of ``POST /api/brief``."""

    #: Defaults to the mode the clock would pick (adapters/ai_brief.py's
    #: ``current_mode``) when the caller does not name one.
    mode: BriefMode | None = None
    headline: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=280)
    sections: list[BriefSectionPush] = Field(default_factory=list, max_length=6)
    generated_at: AwareDatetime = Field(
        default_factory=_utc_now,
        description="Must carry a UTC offset; a naive value is rejected rather than assumed "
        "to be the hub's own timezone.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "schema_version": 1,
                "headline": "Two deadlines today",
                "note": "Answer the vendor quote before standup.",
                "sections": [{"title": "Key tasks", "items": ["Ship it"]}],
            }
        }
    )


class TaskPush(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The agent's own stable id for this task; used to update it on a later
    #: push and to catch a duplicate within the same push (422).
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    due: date | None = None
    priority: Priority = Priority.NONE
    completed: bool = False
    tags: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(
        default_factory=list, max_length=8
    )


class TasksPush(SchemaVersioned):
    """Body of ``POST /api/tasks``."""

    tasks: list[TaskPush] = Field(default_factory=list, max_length=60)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "schema_version": 1,
                "tasks": [
                    {"id": "agent-1", "title": "Ship the release notes", "priority": "high"}
                ],
            }
        }
    )

    @field_validator("tasks")
    @classmethod
    def _no_duplicate_ids(cls, value: list[TaskPush]) -> list[TaskPush]:
        seen: set[str] = set()
        for item in value:
            if item.id in seen:
                raise ValueError(f"duplicate task id: {item.id}")
            seen.add(item.id)
        return value


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


#: Set by the file adapters (ai_usage, brief, tasks) from the pushed file's
#: own ``received_at`` key, or its mtime when that key is absent. ``None``
#: for a fixture block. Plain field, not a computed one: view.py (Part 2)
#: reads it to decide staleness, and it is not used for anything today.
class TasksBlock(Block):
    items: list[Task] = Field(default_factory=list)
    received_at: datetime | None = None


class CalendarBlock(Block):
    items: list[Event] = Field(default_factory=list)


class WeatherBlock(Block):
    weather: Weather | None = None


class AIUsageBlock(Block):
    providers: list[AIUsage] = Field(default_factory=list)
    received_at: datetime | None = None


class BriefBlock(Block):
    brief: Brief | None = None
    received_at: datetime | None = None


class HomeBlock(Block):
    home: HomeState | None = None


class DeviceBlock(Block):
    device: DeviceState | None = None


BlockT = TypeVar("BlockT", bound=Block)

#: ``/api/state``'s shape number. 1 was the seven typed top-level block
#: fields this model carried until 2.1a; 2 is the ``blocks`` mapping, whose
#: keys are whatever datasets the enabled modules declare
#: (docs/plan/2026-09-19-settings-modules-provisioning.md, phase 2 "State").
STATE_SCHEMA_VERSION: int = 2


class DashboardState(BaseModel):
    """Everything the templates are allowed to see.

    ``blocks`` is keyed by dataset name, not by a fixed set of fields: a
    module brings its own datasets, so the hub cannot know the names at
    class-definition time. ``SerializeAsAny`` is what makes that mapping
    carry real data over the wire: without it pydantic would serialize
    every value as the declared base :class:`Block` and ``/api/state``
    would be seven envelopes with no items, no weather and no brief in
    them.

    Nothing reads ``state.blocks[name]`` directly. :meth:`block` is the
    accessor, and it always answers with the asked-for block type, so a
    page never has to None-check a dataset whose module is disabled or
    whose adapter has not run.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    schema_version: int = Field(default=STATE_SCHEMA_VERSION, alias="schema")
    generated_at: datetime
    timezone: str
    blocks: dict[str, SerializeAsAny[Block]] = Field(default_factory=dict)
    alert: Alert | None = None

    def block(self, name: str, model: type[BlockT]) -> BlockT:
        """``name``'s block as a ``model``, or an unavailable placeholder.

        A missing dataset (no such module, the module disabled, or the
        adapter never ran) is an unavailable block of the asked-for type,
        which is exactly what every page already draws for an adapter that
        could not produce anything. A block stored under a different type -
        a state validated from JSON that predates the module that owns it -
        keeps its envelope (status, source, ``updated_at``, error) and
        loses only the value fields the asked-for type does not declare.
        """
        value = self.blocks.get(name)
        if isinstance(value, model):
            return value
        if value is None:
            return model()
        return model.model_validate(value.model_dump())

    @property
    def updated_at(self) -> datetime:
        """Newest adapter timestamp, falling back to ``generated_at``."""
        stamps = [
            block.updated_at for block in self.blocks.values() if block.updated_at is not None
        ]
        return max(stamps) if stamps else self.generated_at
