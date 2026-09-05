"""Turns :class:`DashboardState` into the flat context each template needs.

Templates stay dumb: no adapter knowledge, no arithmetic, no fallbacks. Every
"unknown" decision is made here, once.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from app.config import Settings
from app.models import (
    AdapterStatus,
    Alert,
    AlertPriority,
    DashboardState,
    DeviceState,
    DeviceStatus,
    Event,
    PRIORITY_RANK,
    Priority,
    Task,
)
from app.renderer.chart import build_chart
from app.timeutil import to_local, zone

UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"

PAGE_TITLES: dict[str, str] = {
    "today": "TODAY",
    "agenda": "NEXT 7 DAYS",
    "weather": "WEATHER",
    "brief": "AI BRIEF",
    "system": "HOME AND SYSTEM",
    "alert": "ALERT",
}


# ---------------------------------------------------------------------------
# small formatters
# ---------------------------------------------------------------------------
def fmt_number(value: float | int | None, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "--"
    if digits == 0:
        return f"{round(value):d}{suffix}"
    return f"{value:.{digits}f}{suffix}"


def fmt_percent(value: int | None) -> str:
    return "--" if value is None else f"{value}%"


def fmt_time(value: datetime | None, timezone_name: str) -> str:
    return UNKNOWN if value is None else to_local(value, timezone_name).strftime("%H:%M")


def fmt_day_header(value: date) -> str:
    return value.strftime("%a %d %b").upper()


def fmt_long_date(value: datetime) -> str:
    return value.strftime("%a %d %b").upper()


def relative_day_label(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "TOMORROW"
    return day.strftime("%a").upper()


# ---------------------------------------------------------------------------
# task and event selection
# ---------------------------------------------------------------------------
def open_tasks(state: DashboardState) -> list[Task]:
    return [task for task in state.tasks.items if not task.completed]


def task_sort_key(task: Task, today: date) -> tuple[int, int, str]:
    """Overdue first, then priority, then due date, then title."""
    overdue = 0 if (task.due is not None and task.due < today) else 1
    due_rank = (task.due - today).days if task.due is not None else 3650
    return (overdue, PRIORITY_RANK[task.priority] * 1000 + min(due_rank, 999), task.title)


def priority_tasks(state: DashboardState, today: date, limit: int) -> list[dict[str, Any]]:
    tasks = sorted(open_tasks(state), key=lambda task: task_sort_key(task, today))[:limit]
    rows: list[dict[str, Any]] = []
    for task in tasks:
        rows.append(
            {
                "title": task.title,
                "due_label": due_label(task.due, today),
                "accent": task_accent(task, today),
                "priority": task.priority.value,
            }
        )
    return rows


def due_label(due: date | None, today: date) -> str:
    """Short enough to sit next to a task title on an 800px screen."""
    if due is None:
        return ""
    delta = (due - today).days
    if delta < 0:
        return f"{-delta}D LATE"
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return "TOMORROW"
    return f"DUE {due.strftime('%d %b').upper()}"


def task_accent(task: Task, today: date) -> str:
    if task.due is not None and task.due < today:
        return "red"
    if task.due is not None and task.due == today:
        return "yellow"
    if task.priority is Priority.HIGH:
        return "black"
    return "black"


def upcoming_events(
    state: DashboardState, reference: datetime, limit: int
) -> list[dict[str, Any]]:
    today = reference.date()
    events = [event for event in state.calendar.items if _event_end(event) >= reference]
    events.sort(key=lambda event: event.start)
    rows: list[dict[str, Any]] = []
    for event in events[:limit]:
        day = event.start.date()
        if day == today:
            when = "ALL DAY" if event.all_day else event.start.strftime("%H:%M")
        elif (day - today).days == 1:
            when = "TOMORROW" if event.all_day else f"TOMORROW {event.start.strftime('%H:%M')}"
        else:
            prefix = day.strftime("%a").upper()
            when = prefix if event.all_day else f"{prefix} {event.start.strftime('%H:%M')}"
        rows.append({"when": when, "title": event.title, "location": event.location or ""})
    return rows


def _event_end(event: Event) -> datetime:
    return event.end if event.end is not None else event.start


# ---------------------------------------------------------------------------
# page contexts
# ---------------------------------------------------------------------------
def base_context(state: DashboardState, settings: Settings, page: str) -> dict[str, Any]:
    reference = to_local(state.updated_at, state.timezone)
    return {
        "page": page,
        "page_title": PAGE_TITLES.get(page, page.upper()),
        "state": state,
        "settings": settings,
        "today": reference.date(),
        "reference": reference,
        "date_label": fmt_long_date(reference),
        "updated_label": reference.strftime("%H:%M"),
    }


def today_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "today")
    reference: datetime = context["reference"]
    today: date = context["today"]

    context["priorities"] = (
        priority_tasks(state, today, settings.max_priority_tasks)
        if state.tasks.usable
        else []
    )
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    context["next_events"] = (
        upcoming_events(state, reference, 4) if state.calendar.usable else []
    )
    context["calendar_note"] = block_note(state.calendar.status, "calendar")
    context["weather_summary"] = weather_summary(state)
    context["providers"] = usage_rows(state)
    context["usage_note"] = block_note(state.ai_usage.status, "AI quota")
    context["ai_note"] = brief_note(state)
    return context


def weather_summary(state: DashboardState) -> dict[str, str]:
    weather = state.weather.weather
    if not state.weather.usable or weather is None:
        return {"temp": "--", "detail": UNAVAILABLE, "accent": "black"}
    temp = fmt_number(weather.temperature_c, suffix="C")
    if weather.rain_from:
        detail = f"RAIN FROM {weather.rain_from}"
        accent = "blue"
    elif weather.rain_probability_percent is not None:
        detail = f"RAIN {weather.rain_probability_percent}%"
        accent = "black"
    else:
        detail = weather.condition.upper()
        accent = "black"
    return {"temp": temp, "detail": detail, "accent": accent}


def usage_rows(state: DashboardState) -> list[dict[str, Any]]:
    if not state.ai_usage.usable:
        return []
    rows: list[dict[str, Any]] = []
    for provider in state.ai_usage.providers:
        healthy = provider.collection_status == "ok"
        rows.append(
            {
                "provider": provider.provider.upper(),
                "short": fmt_percent(provider.short_window_percent_remaining)
                if healthy
                else UNKNOWN,
                "weekly": fmt_percent(provider.weekly_percent_remaining) if healthy else UNKNOWN,
                "short_accent": percent_accent(provider.short_window_percent_remaining, healthy),
                "weekly_accent": percent_accent(provider.weekly_percent_remaining, healthy),
                "status": provider.collection_status,
            }
        )
    return rows


def percent_accent(value: int | None, healthy: bool) -> str:
    if not healthy or value is None:
        return "black"
    if value <= 15:
        return "red"
    if value <= 35:
        return "yellow"
    return "green"


def brief_note(state: DashboardState) -> dict[str, str]:
    brief = state.brief.brief
    if not state.brief.usable or brief is None:
        return {"text": f"AI brief {UNAVAILABLE}", "accent": "black"}
    text = brief.note or brief.headline
    if not text:
        return {"text": "AI brief has no note", "accent": "black"}
    return {"text": text, "accent": "black"}


def block_note(status: AdapterStatus, label: str) -> str:
    if status is AdapterStatus.OK:
        return ""
    if status is AdapterStatus.STALE:
        return f"{label}: showing last known data"
    if status is AdapterStatus.UNAVAILABLE:
        return f"{label} {UNAVAILABLE}"
    return f"{label} error"


def agenda_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "agenda")
    today: date = context["today"]
    tz = zone(state.timezone)

    days: list[dict[str, Any]] = []
    for offset in range(settings.agenda_days):
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        events = [
            {
                "when": "ALL DAY" if event.all_day else event.start.strftime("%H:%M"),
                "title": event.title,
            }
            for event in sorted(state.calendar.items, key=lambda item: item.start)
            if start <= event.start < end
        ]
        due = [
            {"title": task.title, "priority": task.priority.value}
            for task in sorted(open_tasks(state), key=lambda item: item.title)
            if task.due == day
        ]
        days.append(
            {
                "day": day,
                "label": relative_day_label(day, today),
                "date_label": day.strftime("%d %b").upper(),
                "events": events,
                "due": due,
                "empty": not events and not due,
            }
        )

    overdue = [
        {"title": task.title, "label": due_label(task.due, today)}
        for task in sorted(open_tasks(state), key=lambda item: (item.due or today, item.title))
        if task.due is not None and task.due < today
    ]
    context["days"] = days
    context["overdue"] = overdue
    context["calendar_note"] = block_note(state.calendar.status, "calendar")
    context["tasks_note"] = block_note(state.tasks.status, "tasks")
    return context


def weather_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "weather")
    weather = state.weather.weather
    context["weather_note"] = block_note(state.weather.status, "weather")
    if not state.weather.usable or weather is None:
        context["weather"] = None
        context["daily"] = []
        context["metrics"] = []
        context["location"] = UNKNOWN
        return context

    context["weather"] = weather
    context["location"] = weather.location_name or UNKNOWN
    context["temp"] = fmt_number(weather.temperature_c)
    context["feels"] = fmt_number(weather.feels_like_c)
    context["condition"] = weather.condition.upper()

    today = context["today"]
    today_row = next((item for item in weather.daily if item.day == today), None)
    context["high"] = fmt_number(today_row.high_c) if today_row else "--"
    context["low"] = fmt_number(today_row.low_c) if today_row else "--"

    if weather.rain_from and weather.rain_until and weather.rain_from != weather.rain_until:
        rain_timing = f"{weather.rain_from} TO {weather.rain_until}"
    elif weather.rain_from:
        rain_timing = f"FROM {weather.rain_from}"
    else:
        rain_timing = "NONE EXPECTED"
    context["rain_timing"] = rain_timing
    context["rain_probability"] = fmt_percent(weather.rain_probability_percent)
    context["rain_accent"] = (
        "blue"
        if (weather.rain_probability_percent or 0) >= 50
        else "black"
    )

    context["metrics"] = [
        {
            "label": "HUMIDITY",
            "value": fmt_percent(weather.humidity_percent),
            "accent": "black",
        },
        {
            "label": "UV INDEX",
            "value": fmt_number(weather.uv_index, digits=1),
            "accent": uv_accent(weather.uv_index),
        },
        {
            "label": "PM2.5",
            "value": fmt_number(weather.pm2_5, digits=0),
            "accent": pm25_accent(weather.pm2_5),
        },
        {
            "label": "AQI " + (weather.aqi_label.upper() if weather.aqi_label else UNKNOWN.upper()),
            "value": fmt_number(weather.aqi),
            "accent": aqi_accent(weather.aqi),
        },
    ]

    context["daily"] = [
        {
            "label": relative_day_label(item.day, today),
            "date_label": item.day.strftime("%d %b").upper(),
            "high": fmt_number(item.high_c),
            "low": fmt_number(item.low_c),
            "rain": fmt_percent(item.rain_probability_percent),
            "accent": "blue" if (item.rain_probability_percent or 0) >= 50 else "black",
        }
        for item in weather.daily[:5]
    ]
    return context


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


def brief_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "brief")
    brief = state.brief.brief
    context["brief_note"] = block_note(state.brief.status, "AI brief")
    if not state.brief.usable or brief is None:
        context["brief"] = None
        context["sections"] = []
        context["mode_label"] = UNKNOWN.upper()
        context["headline"] = f"AI brief {UNAVAILABLE}"
        context["generated_label"] = UNKNOWN
        return context
    context["brief"] = brief
    context["mode_label"] = brief.mode.value.upper()
    context["headline"] = brief.headline or "No headline"
    context["generated_label"] = fmt_time(brief.generated_at, state.timezone)
    # "lines", not "items": a Jinja dict lookup would hit dict.items instead.
    context["sections"] = [
        {"title": section.title.upper(), "lines": section.items[:4]}
        for section in brief.sections[:4]
    ]
    return context


def sensor_value(value: str | None, unit: str | None) -> str:
    """Join a sensor reading and its unit without inventing a value."""
    if value is None:
        return UNKNOWN
    if not unit:
        return value
    if unit in ("%",):
        return f"{value}{unit}"
    return f"{value} {unit}"


#: The DESK panel owns temperature and humidity now, so the Home Assistant
#: room rows would only repeat it (usually from a sensor in another room).
DESK_OWNED_SLOTS: frozenset[str] = frozenset({"room_temperature", "room_humidity"})

NO_DEVICE_DATA = "NO DEVICE DATA YET"
#: The device is reporting, it just has not reported long enough to plot.
NO_DEVICE_HISTORY = "NOT ENOUGH HISTORY YET"


def battery_accent(level: float | None) -> str:
    if level is None:
        return "black"
    if level <= 15:
        return "red"
    if level <= 35:
        return "yellow"
    return "green"


def wifi_accent(rssi: float | None) -> str:
    if rssi is None:
        return "black"
    if rssi >= -67:
        return "green"
    if rssi >= -80:
        return "yellow"
    return "red"


def power_label(usb_present: bool | None, charge_state: str | None) -> tuple[str | None, str | None]:
    """Label and chip accent for the DESK power row.

    Only the three states the device firmware can actually distinguish are
    named; anything else (older firmware that never sends the fields, or a
    charge state the gauge itself calls "unknown") prints nothing rather than
    guessing.
    """
    if usb_present is False:
        return "ON BATTERY", "yellow"
    if usb_present is True and charge_state == "charging":
        return "ON USB, CHARGING", "green"
    if usb_present is True and charge_state == "charged":
        return "ON USB, CHARGED", "green"
    return None, None


def age_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return UNKNOWN.upper()
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "NOW"
    if minutes < 60:
        return f"{minutes}M AGO"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}H AGO"
    return f"{hours // 24}D AGO"


def device_panel(state: DashboardState, settings: Settings) -> dict[str, Any]:
    """Everything the DESK panel draws, including the chart geometry."""
    device: DeviceState | None = state.device.device
    if not state.device.usable or device is None or not device.has_reading:
        return {
            "available": False,
            "note": block_note(state.device.status, "device") or NO_DEVICE_DATA,
            "empty_label": NO_DEVICE_DATA,
            "chart": build_chart([], state.timezone, note=NO_DEVICE_DATA),
        }
    level = device.battery_level
    power_text, power_accent = power_label(device.usb_present, device.charge_state)
    return {
        "available": True,
        "name": (device.device or UNKNOWN).upper(),
        "temperature": fmt_number(device.temperature, digits=1),
        "humidity": fmt_number(device.humidity, digits=0, suffix="%"),
        "power_label": power_text,
        "power_accent": power_accent,
        "battery_percent": fmt_number(level, digits=0, suffix="%"),
        # Clamped only for the bar width; the printed number stays as reported.
        "battery_fill": 0 if level is None else int(max(0.0, min(100.0, level))),
        "battery_accent": battery_accent(level),
        "wifi": fmt_number(device.wifi_rssi, digits=0, suffix=" dBm"),
        "wifi_accent": wifi_accent(device.wifi_rssi),
        "status_label": (
            age_label(device.age_seconds)
            if device.status is DeviceStatus.OK
            else f"STALE {age_label(device.age_seconds)}"
        ),
        "status_accent": "black" if device.status is DeviceStatus.OK else "yellow",
        "empty_label": NO_DEVICE_DATA,
        "note": block_note(state.device.status, "device"),
        "chart": build_chart(device.history_24h, state.timezone, note=NO_DEVICE_HISTORY),
    }


def system_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "system")
    home = state.home.home
    context["home_note"] = block_note(state.home.status, "home")
    context["device"] = device_panel(state, settings)
    if not state.home.usable or home is None:
        context["sensors"] = []
        context["services"] = []
        return context
    context["sensors"] = [
        {
            "name": sensor.name.upper(),
            "value": sensor_value(sensor.value, sensor.unit),
            "accent": {"ok": "green", "warn": "yellow", "alert": "red"}.get(
                sensor.severity, "black"
            ),
        }
        for sensor in home.sensors
        if sensor.key not in DESK_OWNED_SLOTS
    ]
    context["services"] = [
        {
            "name": service.name.upper(),
            "detail": service.detail or UNKNOWN,
            "accent": {"ok": "green", "warn": "yellow", "down": "red"}.get(
                service.health.value, "black"
            ),
            "mark": {"ok": "OK", "warn": "WARN", "down": "DOWN"}.get(
                service.health.value, "UNKNOWN"
            ),
        }
        for service in home.services
    ]
    return context


def alert_context(state: DashboardState, settings: Settings) -> dict[str, Any]:
    context = base_context(state, settings, "alert")
    alert: Alert | None = state.alert
    context["alert"] = alert
    if alert is None:
        context["alert_kicker"] = "DESKMATE"
        context["alert_title"] = "NO ACTIVE ALERT"
        context["alert_message"] = "The hub has nothing to show right now."
        context["alert_time"] = context["updated_label"]
        context["alert_accent"] = "black"
        return context
    context["alert_title"] = alert.title.upper()
    # Do not repeat the title as its own kicker ("DOORBELL" over "DOORBELL").
    priority_label = alert.priority.value.upper()
    context["alert_kicker"] = (
        "DESKMATE ALERT" if priority_label == context["alert_title"] else priority_label
    )
    context["alert_message"] = alert.message
    context["alert_time"] = to_local(alert.created_at, state.timezone).strftime("%H:%M")
    context["alert_accent"] = {
        AlertPriority.CRITICAL: "red",
        AlertPriority.DOORBELL: "red",
        AlertPriority.IMPORTANT: "yellow",
        AlertPriority.NORMAL: "blue",
    }[alert.priority]
    return context


CONTEXT_BUILDERS = {
    "today": today_context,
    "agenda": agenda_context,
    "weather": weather_context,
    "brief": brief_context,
    "system": system_context,
    "alert": alert_context,
}


def build_context(page: str, state: DashboardState, settings: Settings) -> dict[str, Any]:
    builder = CONTEXT_BUILDERS.get(page)
    if builder is None:
        raise KeyError(f"unknown page {page!r}")
    return builder(state, settings)
