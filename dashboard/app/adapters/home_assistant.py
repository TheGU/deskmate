"""Home / system adapters: fixtures and the Home Assistant REST API.

The REST reader asks ``GET {HA_URL}/api/states`` once with a long-lived access
token and picks out the entities named in ``HA_ENTITIES`` (a JSON object of
dashboard slot -> entity id). Slots the map does not cover are simply absent;
entities the map names but Home Assistant does not know show as "unknown".
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import load_fixture
from app.config import Env
from app.logging_setup import log
from app.models import HomeSensor, HomeState, ServiceHealth, ServiceStatus
from app.modules.home.settings import HomeSettings

logger = logging.getLogger("app.adapters.home")

#: Which slots are drawn as HOME sensors and which as SYSTEM services.
SENSOR_SLOTS: Final[tuple[str, ...]] = (
    "front_door",
    "doorbell",
    "motion",
    "room_temperature",
    "room_humidity",
)
SERVICE_SLOTS: Final[tuple[str, ...]] = (
    "internet",
    "home_assistant",
    "nas",
    "proxmox",
    "backup",
    "assistant",
    "assistant_last_run",
)

SLOT_LABELS: Final[dict[str, str]] = {
    "front_door": "Front door",
    "doorbell": "Doorbell",
    "motion": "Motion",
    "room_temperature": "Room temp",
    "room_humidity": "Room humidity",
    "internet": "Internet",
    "home_assistant": "Home Assistant",
    "nas": "NAS",
    "proxmox": "Proxmox",
    "backup": "Backup",
    "assistant": "Local assistant",
    "assistant_last_run": "Last assistant run",
}

#: Home Assistant states that mean "this thing is fine".
HEALTHY_STATES: Final[frozenset[str]] = frozenset(
    {"on", "home", "connected", "ok", "online", "up", "success", "idle", "clear"}
)
UNHEALTHY_STATES: Final[frozenset[str]] = frozenset(
    {"off", "unavailable", "disconnected", "down", "offline", "error", "failed"}
)
OPEN_STATES: Final[frozenset[str]] = frozenset({"on", "open", "detected", "unlocked"})
MISSING_STATES: Final[frozenset[str]] = frozenset({"unknown", "unavailable", "none", ""})


class FixtureHomeAdapter:
    """Home and system status from ``fixtures/home.json``."""

    name = "home"
    source = "fixture"

    def __init__(self, home: HomeSettings, env: Env) -> None:
        self._home = home
        self._env = env

    async def fetch(self) -> HomeState:
        payload = load_fixture(self._env.fixtures_dir / "home.json")
        raw: Any = payload.get("home", {})
        return HomeState.model_validate(raw)


class RestHomeAdapter:
    """Home and system status from the Home Assistant REST API."""

    name = "home"
    source = "rest"

    def __init__(self, home: HomeSettings, env: Env) -> None:
        self._home = home
        self._env = env

    async def fetch(self) -> HomeState:
        home = self._home
        token = home.token.get_secret_value()
        if not home.url or not token:
            raise AdapterUnavailable("the home assistant url / token are not set")
        url = home.url.rstrip("/") + "/api/states"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._env.http_timeout_seconds) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload: Any = response.json()
        if not isinstance(payload, list):
            raise ValueError("unexpected /api/states payload")
        states = {
            str(item.get("entity_id")): item for item in payload if isinstance(item, dict)
        }
        log(logger, logging.INFO, "home assistant states read", entities=len(states))
        return build_home_state(states, home.entity_map())


def build_home_state(
    states: dict[str, dict[str, Any]], entity_map: dict[str, str]
) -> HomeState:
    """Map raw Home Assistant states onto the normalized HomeState."""
    sensors: list[HomeSensor] = []
    services: list[ServiceStatus] = []

    for slot in SENSOR_SLOTS:
        entity_id = entity_map.get(slot)
        if entity_id is None:
            continue
        entity = states.get(entity_id)
        label = SLOT_LABELS.get(slot, slot)
        if entity is None:
            sensors.append(HomeSensor(key=slot, name=label, value=None, severity="unknown"))
            continue
        raw_state = str(entity.get("state", "")).strip()
        attributes: dict[str, Any] = entity.get("attributes", {}) or {}
        unit = attributes.get("unit_of_measurement")
        if raw_state.lower() in MISSING_STATES:
            sensors.append(HomeSensor(key=slot, name=label, value=None, severity="unknown"))
            continue
        severity = "ok"
        value = raw_state
        if entity_id.startswith("binary_sensor."):
            is_open = raw_state.lower() in OPEN_STATES
            device_class = str(attributes.get("device_class", "")).lower()
            if device_class in ("door", "window", "opening", "lock"):
                value = "Open" if is_open else "Closed"
                severity = "alert" if is_open else "ok"
            elif device_class in ("motion", "occupancy", "presence"):
                value = "Detected" if is_open else "Clear"
                severity = "warn" if is_open else "ok"
            else:
                value = "On" if is_open else "Off"
                severity = "warn" if is_open else "ok"
        sensors.append(
            HomeSensor(
                key=slot,
                name=label,
                value=value,
                unit=str(unit) if unit else None,
                severity=severity,
            )
        )

    for slot in SERVICE_SLOTS:
        entity_id = entity_map.get(slot)
        if entity_id is None:
            continue
        entity = states.get(entity_id)
        label = SLOT_LABELS.get(slot, slot)
        if entity is None:
            services.append(ServiceStatus(key=slot, name=label, health=ServiceHealth.UNKNOWN))
            continue
        raw_state = str(entity.get("state", "")).strip()
        lowered = raw_state.lower()
        if lowered in MISSING_STATES:
            health = ServiceHealth.UNKNOWN
        elif lowered in UNHEALTHY_STATES:
            health = ServiceHealth.DOWN
        elif lowered in HEALTHY_STATES:
            health = ServiceHealth.OK
        else:
            health = ServiceHealth.OK
        detail = raw_state if lowered not in MISSING_STATES else None
        services.append(ServiceStatus(key=slot, name=label, health=health, detail=detail))

    return HomeState(sensors=sensors, services=services)


def build_home_adapter(home: HomeSettings, env: Env) -> FixtureHomeAdapter | RestHomeAdapter:
    if home.source == "rest":
        return RestHomeAdapter(home, env)
    return FixtureHomeAdapter(home, env)
