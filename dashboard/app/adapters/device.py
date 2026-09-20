"""Device telemetry adapter.

The only real source is the local SQLite store the device writes into through
``POST /api/device/telemetry``; there is nothing to configure but the paths.
``DEVICE_SOURCE=fixture`` adds one fallback for development and for the render
tests: while the store is still empty, the system module's own
``fixtures/device.json`` supplies a day of plausible samples so the page can
be laid out before the device is flashed. As soon as one real sample exists
the fixture is ignored, and ``DEVICE_SOURCE=store`` never looks at it at all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Final

from app.adapters.base import AdapterUnavailable
from app.config import Env
from app.logging_setup import log
from app.models import (
    DEVICE_STALE_AFTER_SECONDS,
    DevicePoint,
    DeviceSample,
    DeviceState,
    DeviceStatus,
)
from app.modules.device.settings import DeviceSettings
from app.telemetry import TelemetrySummary, get_telemetry_store, utc_now

logger = logging.getLogger("app.adapters.device")

#: How much history the pages get.
HISTORY_HOURS: Final[float] = 24.0
#: 24 h / 15 min = 96 points, which is what an 800 px wide panel can resolve.
HISTORY_BUCKET_MINUTES: Final[int] = 15
#: Hard ceiling for the chart. A window that straddles a bucket boundary would
#: otherwise produce 97 buckets, the oldest of which is a fragment.
HISTORY_POINTS: Final[int] = 96
#: Hard ceiling for ``GET /api/device/history``.
HISTORY_MAX_POINTS: Final[int] = 300


# ---------------------------------------------------------------------------
# downsampling
# ---------------------------------------------------------------------------
def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _mean_time(values: Sequence[datetime]) -> datetime:
    epoch = sum(value.timestamp() for value in values) / len(values)
    return datetime.fromtimestamp(epoch, tz=dt_timezone.utc)


def bucket_points(
    samples: Sequence[DeviceSample],
    *,
    bucket_minutes: int = HISTORY_BUCKET_MINUTES,
    max_points: int = HISTORY_POINTS,
) -> list[DevicePoint]:
    """Mean temperature and humidity per fixed time bucket, oldest first.

    Buckets are anchored on the epoch, so the same samples always land in the
    same buckets. An empty bucket produces no point at all (a gap in the
    chart), and a bucket where only one of the two sensors reported keeps the
    other field ``None``. Only the newest ``max_points`` buckets are returned.
    """
    if not samples:
        return []
    width = max(1, bucket_minutes) * 60
    groups: dict[int, list[DeviceSample]] = {}
    for sample in samples:
        key = int(sample.received_at.timestamp()) // width
        groups.setdefault(key, []).append(sample)

    points: list[DevicePoint] = []
    for key in sorted(groups)[-max(1, max_points):]:
        group = groups[key]
        points.append(
            DevicePoint(
                at=_mean_time([item.received_at for item in group]),
                temperature=_mean([s.temperature for s in group if s.temperature is not None]),
                humidity=_mean([s.humidity for s in group if s.humidity is not None]),
            )
        )
    return points


def downsample(
    samples: Sequence[DeviceSample], max_points: int = HISTORY_MAX_POINTS
) -> list[DeviceSample]:
    """Fold ``samples`` into at most ``max_points`` evenly sized means."""
    if max_points < 1:
        raise ValueError("max_points must be at least 1")
    total = len(samples)
    if total <= max_points:
        return list(samples)

    reduced: list[DeviceSample] = []
    for index in range(max_points):
        start = (index * total) // max_points
        end = ((index + 1) * total) // max_points
        chunk = samples[start:max(end, start + 1)]
        reduced.append(
            DeviceSample(
                received_at=_mean_time([item.received_at for item in chunk]),
                device=chunk[-1].device,
                battery_voltage=_mean([s.battery_voltage for s in chunk if s.battery_voltage is not None]),
                battery_level=_mean([s.battery_level for s in chunk if s.battery_level is not None]),
                temperature=_mean([s.temperature for s in chunk if s.temperature is not None]),
                humidity=_mean([s.humidity for s in chunk if s.humidity is not None]),
                wifi_rssi=_mean([s.wifi_rssi for s in chunk if s.wifi_rssi is not None]),
                uptime_s=_mean([s.uptime_s for s in chunk if s.uptime_s is not None]),
                page=chunk[-1].page,
                battery_mode=chunk[-1].battery_mode,
                usb_present=chunk[-1].usb_present,
                charge_state=chunk[-1].charge_state,
                wake_cause=chunk[-1].wake_cause,
            )
        )
    return reduced


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def device_status(age_seconds: float | None) -> DeviceStatus:
    """``ok`` under three missed reports, ``stale`` above, ``unavailable`` if none."""
    if age_seconds is None:
        return DeviceStatus.UNAVAILABLE
    return (
        DeviceStatus.OK if age_seconds < DEVICE_STALE_AFTER_SECONDS else DeviceStatus.STALE
    )


def build_device_state(
    *,
    latest: DeviceSample | None,
    history: Sequence[DeviceSample],
    summary: TelemetrySummary,
    now: datetime,
    remote_addr: str | None = None,
    hub_host: str | None = None,
) -> DeviceState:
    """Fold the newest sample plus the 24 h window into the rendered model.

    ``remote_addr``/``hub_host`` come from :meth:`TelemetryStore.latest_origin`
    (store-backed callers only); the fixture path never passes them, so the
    demo device always shows a hatch for DEVICE IP / HUB URL on the System
    page rather than a made up origin.
    """
    if latest is None:
        return DeviceState(status=DeviceStatus.UNAVAILABLE, sample_count=summary.sample_count)
    age = (now - latest.received_at).total_seconds()
    return DeviceState(
        status=device_status(age),
        device=latest.device,
        received_at=latest.received_at,
        age_seconds=round(age, 1),
        battery_voltage=latest.battery_voltage,
        battery_level=latest.battery_level,
        temperature=latest.temperature,
        humidity=latest.humidity,
        wifi_rssi=latest.wifi_rssi,
        uptime_s=latest.uptime_s,
        page=latest.page,
        battery_mode=latest.battery_mode,
        usb_present=latest.usb_present,
        charge_state=latest.charge_state,
        wake_cause=latest.wake_cause,
        sample_count=summary.sample_count,
        oldest_at=summary.oldest,
        newest_at=summary.newest,
        history_24h=bucket_points(history),
        remote_addr=remote_addr,
        hub_host=hub_host,
    )


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------
def load_device_fixture(path: Path, *, now: datetime) -> list[DeviceSample]:
    """Read the system module's own ``fixtures/device.json``.

    Sample times are stored as ``offset_minutes`` relative to "now" rather than
    as absolute stamps: a rolling 24 h window only makes sense against the
    current clock, and a fixed anchor date would make the demo device look
    permanently stale.
    """
    if not path.is_file():
        raise AdapterUnavailable(f"fixture not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"fixture {path} must contain a JSON object")
    device = str(payload.get("device", "unknown"))
    raw: Any = payload.get("samples", [])
    if not isinstance(raw, list) or not raw:
        raise AdapterUnavailable(f"fixture {path} has no samples")

    samples: list[DeviceSample] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        offset = float(entry.get("offset_minutes", 0.0))
        samples.append(
            DeviceSample(
                received_at=now + timedelta(minutes=offset),
                device=str(entry.get("device", device)),
                battery_voltage=entry.get("battery_voltage"),
                battery_level=entry.get("battery_level"),
                temperature=entry.get("temperature"),
                humidity=entry.get("humidity"),
                wifi_rssi=entry.get("wifi_rssi"),
                uptime_s=entry.get("uptime_s"),
                page=entry.get("page"),
                # The demo device is always shown plugged in and topped off;
                # entries may still override any of the three explicitly.
                battery_mode=entry.get("battery_mode", False),
                usb_present=entry.get("usb_present", True),
                charge_state=entry.get("charge_state", "charged"),
            )
        )
    samples.sort(key=lambda item: item.received_at)
    return samples


def _state_from_samples(samples: Sequence[DeviceSample], *, now: datetime) -> DeviceState:
    window_start = now - timedelta(hours=HISTORY_HOURS)
    history = [item for item in samples if item.received_at >= window_start]
    summary = TelemetrySummary(
        sample_count=len(samples),
        oldest=samples[0].received_at if samples else None,
        newest=samples[-1].received_at if samples else None,
    )
    return build_device_state(
        latest=samples[-1] if samples else None,
        history=history,
        summary=summary,
        now=now,
    )


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------
def _state_from_store(device: DeviceSettings, env: Env, *, now: datetime) -> DeviceState | None:
    """Build the state from stored telemetry, or ``None`` when the store is empty."""
    store = get_telemetry_store(env.hub_db_file, device.retention_days)
    summary = store.summary()
    if summary.sample_count == 0:
        return None
    origin = store.latest_origin()
    remote_addr, hub_host = (None, None) if origin is None else (origin[0], origin[1])
    return build_device_state(
        latest=store.latest(),
        history=store.history(HISTORY_HOURS, now=now),
        summary=summary,
        now=now,
        remote_addr=remote_addr,
        hub_host=hub_host,
    )


def _log_state(state: DeviceState, source: str, origin: str) -> DeviceState:
    log(
        logger,
        logging.DEBUG,
        "device state built",
        source=source,
        origin=origin,
        status=state.status.value,
        samples=state.sample_count,
        points=len(state.history_24h),
    )
    return state


class StoreDeviceAdapter:
    """Whatever the device has actually posted, and nothing else."""

    name = "device"
    source = "store"

    def __init__(self, device: DeviceSettings, env: Env) -> None:
        self._device = device
        self._env = env

    async def fetch(self) -> DeviceState:
        state = _state_from_store(self._device, self._env, now=utc_now())
        if state is None:
            raise AdapterUnavailable("the device has not posted any telemetry yet")
        return _log_state(state, self.source, "store")


class FixtureDeviceAdapter:
    """The store when it has anything, the system module's own
    ``fixtures/device.json`` while it is empty."""

    name = "device"
    source = "fixture"

    def __init__(self, device: DeviceSettings, env: Env, fixture: Path) -> None:
        self._device = device
        self._env = env
        self._fixture = fixture

    async def fetch(self) -> DeviceState:
        now = utc_now()
        stored = _state_from_store(self._device, self._env, now=now)
        if stored is not None:
            return _log_state(stored, self.source, "store")
        samples = load_device_fixture(self._fixture, now=now)
        return _log_state(_state_from_samples(samples, now=now), self.source, "fixture")


def build_device_adapter(
    device: DeviceSettings, env: Env, fixture: Path
) -> StoreDeviceAdapter | FixtureDeviceAdapter:
    if device.source == "store":
        return StoreDeviceAdapter(device, env)
    return FixtureDeviceAdapter(device, env, fixture)
