"""The System page's own context builder and the helpers only it needs.

Moved out of ``app/view.py`` in 2.2 (docs/plan/2026-09-19-settings-modules-
provisioning.md): everything here is read by no other page. Shared helpers
(formatting, header, footer, base context, the stale rules) stay in
``app/view.py`` and are imported from there.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, TYPE_CHECKING

from app import icons
from app.models import (
    AIUsageBlock,
    BriefBlock,
    Block,
    CalendarBlock,
    DashboardState,
    DeviceBlock,
    DeviceState,
    DeviceStatus,
    HomeBlock,
    TasksBlock,
    WeatherBlock,
)
from app.renderer.chart import build_chart
from app.view import (
    UNKNOWN,
    ai_usage_stale,
    base_context,
    block_note,
    brief_stale,
    fmt_number,
    tasks_stale,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.settings import HubSettings


#: A trailing duration inside a free-text sensor value ("Clear 41m"): the
#: panel's numerals and units are otherwise always caps ("41M", "3H"), so the
#: unit letter is capped here rather than left lower-case mid-sentence.
_DURATION_SUFFIX_RE = re.compile(r"(?<=\d)([hmsd])\b")


def cap_duration(value: str) -> str:
    """Upper-case a trailing "<number><unit>" duration inside ``value``."""
    return _DURATION_SUFFIX_RE.sub(lambda m: m.group(1).upper(), value)


def sensor_value(value: str | None, unit: str | None) -> str:
    """Join a sensor reading and its unit without inventing a value."""
    if value is None:
        return UNKNOWN
    if not unit:
        return cap_duration(value)
    if unit in ("%",):
        return f"{value}{unit}"
    return f"{value} {unit}"


#: The DESK panel owns temperature and humidity now, so the Home Assistant
#: room rows would only repeat it (usually from a sensor in another room).
DESK_OWNED_SLOTS: frozenset[str] = frozenset({"room_temperature", "room_humidity"})

#: HOME and SERVICES merged into one column and one budget (R3 "hub column"
#: rework), sensors first. Derived from the measured layout, not guessed:
#: the page body is 372 px (480 - 64 px header - 40 px footer - two 2 px
#: rules, DESIGN.md "Layout"); the DESK instrument cluster (sys-top) was cut
#: from 200 to 150 px to leave room for the new HUB column's fixed 9-row
#: list (see hub_rows below), so the bottom row is 372 - 150 - 2 = 220 px
#: tall. Inside a bottom-row column that leaves 220 - 20 (the "HOME"/"HUB"
#: label) - 2 (the row list's own top margin) = 198 px for 28 px rows:
#: floor(198 / 28) = 7.
HOME_ROW_BUDGET: int = 7

NO_DEVICE_DATA = "NO DEVICE DATA YET"
#: The device is reporting, it just has not reported long enough to plot.
NO_DEVICE_HISTORY = "NOT ENOUGH HISTORY YET"


def battery_accent(level: float | None) -> str:
    """Colour for the BATTERY meter fill: plain ink until it is nearly flat.

    Same 10/20 thresholds as the header's chip: a mid-range charge is not
    news, so only the stretch right before the device actually goes flat
    earns a colour. The meter never turns green; a level is not itself a
    "healthy" state the way a service or a sensor is.
    """
    if level is None:
        return "black"
    if level <= 10:
        return "red"
    if level <= 20:
        return "yellow"
    return "black"


def wifi_accent(rssi: float | None) -> str:
    if rssi is None:
        return "black"
    if rssi >= -67:
        return "green"
    if rssi >= -80:
        return "yellow"
    return "red"


def power_label(usb_present: bool | None, charge_state: str | None) -> str | None:
    """The single caps word printed under the BATTERY meter.

    Kept in step with the plug glyph in :func:`icons.battery_icon`: any
    ``usb_present is True`` reading gets a word here (CHARGING while the
    charger reports actually pushing current, USB otherwise, including an
    unknown or missing charge state), never a plug icon with a blank caption
    under it. ``usb_present is False`` is BATTERY, and ``usb_present is None``
    (older firmware that never sends the field) prints nothing, matching the
    plain battery glyph it gets instead of the plug.
    """
    if usb_present is False:
        return "BATTERY"
    if usb_present is True:
        return "CHARGING" if charge_state in ("charging", "pre_charge") else "USB"
    return None


def age_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return UNKNOWN.upper()
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "NOW"
    if minutes < 60:
        return f"{minutes} MIN AGO"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} H AGO"
    return f"{hours // 24} D AGO"


def wake_label(wake_cause: str | None) -> str:
    """The LAST WAKE reading: the firmware's own word, spaced out and capped."""
    if not wake_cause:
        return UNKNOWN.upper()
    return wake_cause.replace("_", " ").upper()


def device_panel(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    """Everything the DESK instrument cluster and its chart draw."""
    device: DeviceState | None = state.block("device", DeviceBlock).device
    if not state.block("device", DeviceBlock).usable or device is None or not device.has_reading:
        return {
            "available": False,
            "chart": build_chart([], state.timezone, note=NO_DEVICE_DATA),
        }
    level = device.battery_level
    hours = None if device.uptime_s is None else device.uptime_s / 3600.0
    return {
        "available": True,
        "battery_level": level,
        "battery_text": fmt_number(level, digits=0),
        "battery_available": level is not None,
        "battery_fraction": 0.0 if level is None else max(0.0, min(1.0, level / 100.0)),
        "battery_accent": battery_accent(level),
        "battery_icon": icons.battery_icon(level, device.usb_present),
        "usb_present": device.usb_present,
        "power_word": power_label(device.usb_present, device.charge_state),
        "stale": device.status is not DeviceStatus.OK,
        "age_text": age_label(device.age_seconds),
        "temperature_text": fmt_number(device.temperature, digits=1),
        "humidity_text": fmt_number(device.humidity, digits=0),
        "wifi_icon": icons.wifi_icon(device.wifi_rssi),
        "wifi_text": fmt_number(device.wifi_rssi, digits=0),
        "uptime_text": fmt_number(hours, digits=0),
        "wake_text": wake_label(device.wake_cause),
        "chart": build_chart(device.history_24h, state.timezone, note=NO_DEVICE_HISTORY),
    }


def desk_accent(state: DashboardState) -> str:
    """Whether the DESK row's stale tell-tale should show at all."""
    device: DeviceState | None = state.block("device", DeviceBlock).device
    if not state.block("device", DeviceBlock).usable or device is None or not device.has_reading:
        return "black"
    return "black" if device.status is DeviceStatus.OK else "yellow"


def sensor_accent(severity: str) -> str:
    """Tell-tale colour before a HOME sensor's name.

    Only a state worth a second look earns a dot: "ok" and "unknown" print
    the name in plain black, same as every other quiet row on the panel.
    """
    return {"warn": "yellow", "alert": "red"}.get(severity, "")


def service_mark(health: str) -> str:
    """Colour of the 12 px square before a SERVICES row's name.

    An unrecognised health string draws a hatch square rather than guessing a
    colour for a state nobody named.
    """
    return {"ok": "black", "warn": "yellow", "down": "red"}.get(health, "hatch")


def dataset_age_label(updated_at: datetime | None, now: datetime) -> str:
    """HUB column age text for one dataset: "NOW" under a minute, then
    minutes, hours, days - "NEVER" when the block has not updated at all
    (including a source that is simply unconfigured: this column cannot
    tell "off" apart from "never pushed", so it never claims to).

    Same bucketing as :func:`age_label`, without the " AGO" suffix: the HUB
    column is nine rows deep, and one word reads faster than three.
    """
    if updated_at is None:
        return "NEVER"
    age_seconds = max(0.0, (now - updated_at).total_seconds())
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "NOW"
    if minutes < 60:
        return f"{minutes} MIN"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} H"
    return f"{hours // 24} D"


def strip_scheme(url: str) -> str:
    """Strip the scheme, "https://host:port" becomes "host:port": the HUB URL row has no room for a
    scheme nobody needs to type, and the device always speaks the hub's own
    scheme anyway."""
    return url.split("://", 1)[-1]


#: The six pushed/fetched datasets the HUB column reports an age for, in
#: display order, and the block each reads. calendar, weather and home have
#: no existing staleness threshold (only ai_usage/brief/tasks do), so they
#: never draw the yellow telltale here; they still get an age like
#: everything else.
def _hub_dataset_rows(
    state: DashboardState, settings: "HubSettings", now: datetime
) -> list[dict[str, Any]]:
    datasets: tuple[tuple[str, Block, str | None], ...] = (
        ("TASKS", state.block("tasks", TasksBlock), tasks_stale(state, settings, now)),
        ("CALENDAR", state.block("calendar", CalendarBlock), None),
        ("WEATHER", state.block("weather", WeatherBlock), None),
        ("AI USAGE", state.block("ai_usage", AIUsageBlock), ai_usage_stale(state, settings, now)),
        ("BRIEF", state.block("brief", BriefBlock), brief_stale(state, settings, now)),
        ("HOME", state.block("home", HomeBlock), None),
    )
    return [
        {
            "name": name,
            "value": dataset_age_label(block.updated_at, now),
            "available": True,
            "accent": "yellow" if stale else "",
            "clip": False,
        }
        for name, block, stale in datasets
    ]


#: DEVICE SYNC/IP/URL: the device's own telemetry origin, not a pushed
#: dataset. All three come from the same DeviceState (adapters/device.py
#: threads TelemetryStore.latest_origin through it), and all three fall
#: back together when the device has never reported: NEVER, a hatch, a
#: hatch, never a guess.
def _hub_device_rows(state: DashboardState, now: datetime) -> list[dict[str, Any]]:
    device: DeviceState | None = state.block("device", DeviceBlock).device
    has_reading = state.block("device", DeviceBlock).usable and device is not None and device.has_reading
    newest_at = device.newest_at if has_reading and device is not None else None
    remote_addr = device.remote_addr if has_reading and device is not None else None
    hub_host = device.hub_host if has_reading and device is not None else None
    return [
        {
            "name": "DEVICE SYNC",
            "value": dataset_age_label(newest_at, now),
            "available": True,
            "accent": "",
            "clip": False,
        },
        {
            # Shows whoever last spoke to /api/device/telemetry. Behind a
            # reverse proxy that is the proxy's own address, not the
            # device's real LAN address.
            "name": "DEVICE IP",
            "value": remote_addr,
            "available": remote_addr is not None,
            "accent": "",
            "clip": False,
        },
        {
            "name": "HUB URL",
            "value": None if hub_host is None else strip_scheme(hub_host),
            "available": hub_host is not None,
            "accent": "",
            "clip": True,
        },
    ]


def hub_rows(state: DashboardState, settings: "HubSettings", now: datetime) -> list[dict[str, Any]]:
    """The HUB column's fixed nine rows, in order: six dataset ages, then
    the device's own sync age, IP and bound hub URL."""
    return _hub_dataset_rows(state, settings, now) + _hub_device_rows(state, now)


def system_context(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    context = base_context(state, settings, "system")
    reference: datetime = context["reference"]
    context["home_note"] = block_note(state.block("home", HomeBlock).status, "home")
    context["device"] = device_panel(state, settings)
    context["hub"] = hub_rows(state, settings, reference)

    home = state.block("home", HomeBlock).home
    if not state.block("home", HomeBlock).usable or home is None:
        context["home_rows"] = []
        return context

    sensor_rows = [
        {
            "kind": "sensor",
            "name": sensor.name.upper(),
            "value": sensor_value(sensor.value, sensor.unit),
            "available": sensor.value is not None,
            "accent": sensor_accent(sensor.severity),
        }
        for sensor in home.sensors
        if sensor.key not in DESK_OWNED_SLOTS
    ]
    service_rows = [
        {
            "kind": "service",
            "name": service.name.upper(),
            "mark": service_mark(service.health.value),
            "down": service.health.value == "down",
        }
        for service in home.services
    ]
    # Sensors first, one combined budget: HOME_ROW_BUDGET caps the merged
    # list, not each half.
    context["home_rows"] = (sensor_rows + service_rows)[:HOME_ROW_BUDGET]
    return context


def system_flag(state: DashboardState, settings: "HubSettings") -> bool:
    """A service that is down, or a device that stopped reporting."""
    home_block = state.block("home", HomeBlock)
    home = home_block.home
    if home_block.usable and home is not None:
        # A degraded service is on the System page already; only a service
        # that is actually down is worth sending the owner there.
        if any(service.health.value == "down" for service in home.services):
            return True
    device_block = state.block("device", DeviceBlock)
    device = device_block.device
    return device_block.usable and device is not None and device.status is DeviceStatus.STALE


__all__ = [
    "DESK_OWNED_SLOTS",
    "HOME_ROW_BUDGET",
    "NO_DEVICE_DATA",
    "NO_DEVICE_HISTORY",
    "age_label",
    "battery_accent",
    "cap_duration",
    "dataset_age_label",
    "desk_accent",
    "device_panel",
    "hub_rows",
    "power_label",
    "sensor_accent",
    "sensor_value",
    "service_mark",
    "strip_scheme",
    "system_context",
    "system_flag",
    "wake_label",
    "wifi_accent",
]
