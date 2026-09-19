"""Device telemetry: the store, the downsamplers, the adapter and the endpoints."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import AdapterUnavailable
from app.adapters.device import (
    HISTORY_MAX_POINTS,
    HISTORY_POINTS,
    FixtureDeviceAdapter,
    StoreDeviceAdapter,
    bucket_points,
    build_device_state,
    device_status,
    downsample,
    load_device_fixture,
)
from app.config import Env
from app.db import Database, get_database
from app.main import MAX_OPEN_BODY_BYTES, create_app
from app.models import (
    DEVICE_STALE_AFTER_SECONDS,
    AdapterStatus,
    DashboardState,
    DeviceBlock,
    DeviceSample,
    DeviceStatus,
    DeviceTelemetry,
)
from app.modules.device.settings import DeviceSettings
from app.renderer.chart import build_chart
from app.settings import HubSettings
from app.view import device_panel, power_label, system_context
from app.telemetry import (
    TelemetryStore,
    TelemetrySummary,
    get_telemetry_store,
    parse_utc,
    utc_iso,
)
from tests.conftest import FIXTURES_DIR, make_state, run

#: The exact payload firmware/e1002.yaml posts.
DEVICE_PAYLOAD: dict[str, object] = {
    "device": "reterminal-e1002",
    "battery_voltage": 4.056,
    "battery_level": 92.6,
    "temperature": 32.80,
    "humidity": 54.4,
    "wifi_rssi": -28,
    "uptime_s": 425,
    "page": "brief",
}

#: The same payload once the firmware also reports power state.
DEVICE_PAYLOAD_WITH_POWER: dict[str, object] = dict(
    DEVICE_PAYLOAD, battery_mode=False, usb_present=True, charge_state="charging"
)


def sample(minutes_ago: float, temperature: float | None, humidity: float | None) -> DeviceSample:
    return DeviceSample(
        received_at=datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
        - timedelta(minutes=minutes_ago),
        device="reterminal-e1002",
        temperature=temperature,
        humidity=humidity,
    )


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[Database]:
    """A fresh hub database. The store never owns the connection, so the
    lifetime is the fixture's."""
    instance = Database(tmp_path / "deskmate.sqlite")
    instance.migrate()
    yield instance
    instance.close()


@pytest.fixture()
def store(database: Database) -> TelemetryStore:
    return TelemetryStore(database, retention_days=30)


@pytest.fixture()
def device_env(tmp_path: Path) -> Iterator[Env]:
    """A hub whose database starts out empty. The database is process-wide
    and keyed by path, so the fixture closes it again on the way out rather
    than leaving a handle on a temp directory."""
    env = Env(
        _env_file=None,
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
    )
    yield env
    get_database(env.hub_db_file).close()


@pytest.fixture()
def device_hub_settings() -> HubSettings:
    return HubSettings(device=DeviceSettings(source="store"))


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def test_insert_stores_every_field(store: TelemetryStore) -> None:
    telemetry = DeviceTelemetry.model_validate(DEVICE_PAYLOAD)
    received_at = store.insert(telemetry)

    latest = store.latest()
    assert latest is not None
    assert latest.device == "reterminal-e1002"
    assert latest.battery_voltage == pytest.approx(4.056)
    assert latest.battery_level == pytest.approx(92.6)
    assert latest.temperature == pytest.approx(32.80)
    assert latest.humidity == pytest.approx(54.4)
    assert latest.wifi_rssi == pytest.approx(-28)
    assert latest.uptime_s == pytest.approx(425)
    assert latest.page == "brief"
    assert latest.received_at == received_at
    assert store.summary().sample_count == 1


def test_insert_stores_the_power_fields(store: TelemetryStore) -> None:
    telemetry = DeviceTelemetry.model_validate(DEVICE_PAYLOAD_WITH_POWER)
    store.insert(telemetry)
    latest = store.latest()
    assert latest is not None
    assert latest.battery_mode is False
    assert latest.usb_present is True
    assert latest.charge_state == "charging"


def test_insert_stores_remote_addr_and_hub_host(store: TelemetryStore) -> None:
    telemetry = DeviceTelemetry.model_validate(DEVICE_PAYLOAD)
    store.insert(telemetry, remote_addr="192.0.2.10", hub_host="https://192.0.2.1:8080")
    origin = store.latest_origin()
    assert origin is not None
    remote_addr, hub_host, received_at = origin
    assert remote_addr == "192.0.2.10"
    assert hub_host == "https://192.0.2.1:8080"
    assert isinstance(received_at, datetime)


def test_latest_origin_is_none_on_an_empty_store(store: TelemetryStore) -> None:
    assert store.latest_origin() is None


def test_latest_origin_never_reaches_a_device_sample(store: TelemetryStore) -> None:
    """remote_addr/hub_host live outside ``_COLUMNS``: ``latest()`` (which
    feeds :class:`DeviceSample`) must never carry them, only
    ``latest_origin()`` does."""
    telemetry = DeviceTelemetry.model_validate(DEVICE_PAYLOAD)
    store.insert(telemetry, remote_addr="192.0.2.10", hub_host="https://192.0.2.1:8080")
    latest = store.latest()
    assert latest is not None
    assert not hasattr(latest, "remote_addr")
    assert not hasattr(latest, "hub_host")


def test_insert_accepts_missing_power_fields(store: TelemetryStore) -> None:
    """Older firmware that never sends the three power fields still stores fine."""
    telemetry = DeviceTelemetry.model_validate(DEVICE_PAYLOAD)
    store.insert(telemetry)
    latest = store.latest()
    assert latest is not None
    assert latest.battery_mode is None
    assert latest.usb_present is None
    assert latest.charge_state is None


def test_store_migrates_a_database_from_before_the_power_fields(tmp_path: Path) -> None:
    """A database file written by an older build has no ``battery_mode`` /
    ``usb_present`` / ``charge_state`` columns. Opening it must add them
    without losing the row that is already there, and a sample with the new
    fields must round-trip alongside the old one."""
    path = tmp_path / "pre-power.sqlite"
    raw = sqlite3.connect(str(path))
    raw.execute(
        """
        CREATE TABLE telemetry (
            received_at     TEXT NOT NULL,
            device          TEXT NOT NULL,
            battery_voltage REAL,
            battery_level   REAL,
            temperature     REAL,
            humidity        REAL,
            wifi_rssi       REAL,
            uptime_s        REAL,
            page            TEXT
        )
        """
    )
    raw.execute("CREATE INDEX telemetry_received_at ON telemetry (received_at)")
    raw.execute(
        "INSERT INTO telemetry (received_at, device, temperature) VALUES (?, ?, ?)",
        ("2026-09-04T00:00:00.000+00:00", "reterminal-e1002", 30.0),
    )
    raw.commit()
    raw.close()

    database = Database(path)
    database.migrate()
    store = TelemetryStore(database, retention_days=30)
    try:
        store.insert(
            DeviceTelemetry.model_validate(DEVICE_PAYLOAD_WITH_POWER),
            received_at=datetime(2026, 9, 5, 0, 0, tzinfo=dt_timezone.utc),
        )
        rows = store.history(24 * 365)
        assert len(rows) == 2
        old_row, new_row = rows
        assert old_row.temperature == pytest.approx(30.0)
        assert old_row.battery_mode is None
        assert old_row.usb_present is None
        assert old_row.charge_state is None
        assert new_row.battery_mode is False
        assert new_row.usb_present is True
        assert new_row.charge_state == "charging"
    finally:
        database.close()


def test_insert_accepts_null_numeric_fields(store: TelemetryStore) -> None:
    telemetry = DeviceTelemetry(
        device="reterminal-e1002",
        battery_voltage=None,
        battery_level=None,
        temperature=None,
        humidity=None,
        wifi_rssi=None,
        uptime_s=None,
        page=None,
    )
    store.insert(telemetry)
    latest = store.latest()
    assert latest is not None
    assert latest.temperature is None
    assert latest.humidity is None
    assert latest.battery_level is None


def test_timestamps_are_stored_as_utc_iso_strings(store: TelemetryStore) -> None:
    bangkok = datetime(2026, 9, 5, 18, 30, tzinfo=dt_timezone(timedelta(hours=7)))
    store.insert(DeviceTelemetry(device="reterminal-e1002"), received_at=bangkok)
    with store.database.reading() as connection:  # the raw column is the point
        raw = connection.execute("SELECT received_at FROM telemetry").fetchone()[0]
    assert raw.endswith("+00:00")
    assert raw.startswith("2026-09-05T11:30:00")
    assert parse_utc(raw) == bangkok


def test_insert_prunes_rows_past_the_retention_window(database: Database) -> None:
    store = TelemetryStore(database, retention_days=2)
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    for days in (5, 3, 1, 0):
        store.insert(
            DeviceTelemetry(device="reterminal-e1002", temperature=30.0),
            received_at=now - timedelta(days=days),
        )
    summary = store.summary()
    # The two rows older than 2 days went out with the last insert.
    assert summary.sample_count == 2
    assert summary.oldest == now - timedelta(days=1)
    assert summary.newest == now


def test_history_only_returns_the_requested_window(store: TelemetryStore) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    for hours in (30, 20, 2, 0):
        store.insert(
            DeviceTelemetry(device="reterminal-e1002", temperature=30.0 + hours),
            received_at=now - timedelta(hours=hours),
        )
    window = store.history(24.0, now=now)
    assert [round(item.temperature or 0.0) for item in window] == [50, 32, 30]
    assert store.summary().sample_count == 4


def test_utc_iso_orders_lexicographically() -> None:
    base = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    stamps = [utc_iso(base + timedelta(minutes=step)) for step in (0, 5, 61, 1440)]
    assert stamps == sorted(stamps)


# ---------------------------------------------------------------------------
# downsampling
# ---------------------------------------------------------------------------
def test_bucket_points_means_each_15_minute_bucket() -> None:
    samples = [
        sample(30, 30.0, 50.0),
        sample(25, 32.0, 54.0),  # same 15 min bucket as the one above
        sample(5, 31.0, 52.0),
    ]
    points = bucket_points(samples)
    assert len(points) == 2
    assert points[0].temperature == pytest.approx(31.0)
    assert points[0].humidity == pytest.approx(52.0)
    assert points[1].temperature == pytest.approx(31.0)
    assert points[0].at < points[1].at


def test_bucket_points_keeps_holes_and_partial_readings() -> None:
    points = bucket_points([sample(5, None, 55.0), sample(4, None, 57.0)])
    assert len(points) == 1
    assert points[0].temperature is None
    assert points[0].humidity == pytest.approx(56.0)
    assert bucket_points([]) == []


def test_bucket_points_caps_a_full_day_at_96_points() -> None:
    samples = [sample(minutes, 30.0, 55.0) for minutes in range(0, 1440, 5)]
    points = bucket_points(samples)
    assert len(points) == HISTORY_POINTS


def test_downsample_is_a_no_op_below_the_ceiling() -> None:
    samples = [sample(minutes, 30.0, 55.0) for minutes in range(10)]
    assert downsample(samples, 300) == samples


def test_downsample_folds_long_histories_into_means() -> None:
    samples = [sample(2000 - index, float(index), 50.0) for index in range(1000)]
    reduced = downsample(samples, HISTORY_MAX_POINTS)
    assert len(reduced) == HISTORY_MAX_POINTS
    assert reduced[0].received_at < reduced[-1].received_at
    # Means of contiguous chunks stay inside the original range.
    assert 0.0 <= (reduced[0].temperature or 0.0) <= 999.0
    assert (reduced[-1].temperature or 0.0) > (reduced[0].temperature or 0.0)


def test_downsample_rejects_a_zero_ceiling() -> None:
    with pytest.raises(ValueError):
        downsample([sample(1, 30.0, 50.0)], 0)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_device_status_thresholds() -> None:
    assert device_status(None) is DeviceStatus.UNAVAILABLE
    assert device_status(0.0) is DeviceStatus.OK
    assert device_status(DEVICE_STALE_AFTER_SECONDS - 1) is DeviceStatus.OK
    assert device_status(DEVICE_STALE_AFTER_SECONDS) is DeviceStatus.STALE
    assert device_status(86400.0) is DeviceStatus.STALE


def test_build_device_state_without_samples_is_unavailable() -> None:
    state = build_device_state(
        latest=None,
        history=[],
        summary=TelemetrySummary(sample_count=0, oldest=None, newest=None),
        now=datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc),
    )
    assert state.status is DeviceStatus.UNAVAILABLE
    assert state.has_reading is False
    assert state.temperature is None
    assert state.history_24h == []


def test_build_device_state_reports_age_and_history() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    history = [sample(minutes, 30.0, 55.0) for minutes in range(120, 0, -5)]
    state = build_device_state(
        latest=history[-1],
        history=history,
        summary=TelemetrySummary(
            sample_count=len(history),
            oldest=history[0].received_at,
            newest=history[-1].received_at,
        ),
        now=now,
    )
    assert state.status is DeviceStatus.OK
    assert state.age_seconds == pytest.approx(300.0)
    assert state.sample_count == len(history)
    assert 1 < len(state.history_24h) <= 96


def test_build_device_state_carries_the_origin_when_given_one() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = sample(0, 30.0, 55.0)
    state = build_device_state(
        latest=latest,
        history=[latest],
        summary=TelemetrySummary(sample_count=1, oldest=latest.received_at, newest=latest.received_at),
        now=now,
        remote_addr="192.0.2.10",
        hub_host="https://192.0.2.1:8080",
    )
    assert state.remote_addr == "192.0.2.10"
    assert state.hub_host == "https://192.0.2.1:8080"


def test_build_device_state_origin_defaults_to_none() -> None:
    """The fixture demo device never posted, so it never has an origin."""
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = sample(0, 30.0, 55.0)
    state = build_device_state(
        latest=latest,
        history=[latest],
        summary=TelemetrySummary(sample_count=1, oldest=latest.received_at, newest=latest.received_at),
        now=now,
    )
    assert state.remote_addr is None
    assert state.hub_host is None


def test_build_device_state_goes_stale() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    old = sample(60, 30.0, 55.0)
    state = build_device_state(
        latest=old,
        history=[old],
        summary=TelemetrySummary(sample_count=1, oldest=old.received_at, newest=old.received_at),
        now=now,
    )
    assert state.status is DeviceStatus.STALE
    assert state.age_seconds == pytest.approx(3600.0)


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------
def test_store_adapter_is_unavailable_until_the_device_reports(
    device_hub_settings: HubSettings, device_env: Env,
) -> None:
    adapter = StoreDeviceAdapter(device_hub_settings.device, device_env)
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


def test_store_adapter_reads_what_was_posted(
    device_hub_settings: HubSettings, device_env: Env
) -> None:
    store = get_telemetry_store(device_env.hub_db_file, device_hub_settings.device.retention_days)
    store.insert(DeviceTelemetry.model_validate(DEVICE_PAYLOAD))
    state = run(StoreDeviceAdapter(device_hub_settings.device, device_env).fetch())
    assert state.status is DeviceStatus.OK
    assert state.temperature == pytest.approx(32.80)
    assert state.sample_count == 1


def test_store_adapter_carries_the_telemetry_origin(
    device_hub_settings: HubSettings, device_env: Env
) -> None:
    store = get_telemetry_store(device_env.hub_db_file, device_hub_settings.device.retention_days)
    store.insert(
        DeviceTelemetry.model_validate(DEVICE_PAYLOAD),
        remote_addr="192.0.2.10",
        hub_host="https://192.0.2.1:8080",
    )
    state = run(StoreDeviceAdapter(device_hub_settings.device, device_env).fetch())
    assert state.remote_addr == "192.0.2.10"
    assert state.hub_host == "https://192.0.2.1:8080"


def test_fixture_adapter_fills_an_empty_store(
    device_hub_settings: HubSettings, device_env: Env
) -> None:
    state = run(FixtureDeviceAdapter(device_hub_settings.device, device_env).fetch())
    assert state.status is DeviceStatus.OK
    assert state.device == "reterminal-e1002"
    assert state.sample_count == 288
    assert len(state.history_24h) == HISTORY_POINTS
    assert state.temperature is not None
    # The demo device never actually posted: no origin to show.
    assert state.remote_addr is None
    assert state.hub_host is None


def test_fixture_file_holds_a_day_of_five_minute_samples() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    samples = load_device_fixture(FIXTURES_DIR / "device.json", now=now)
    assert len(samples) == 288
    assert samples[-1].received_at == now
    assert (samples[-1].received_at - samples[0].received_at) == timedelta(minutes=1435)
    # The first reports of a cold boot carry no sensor reading, and the loader
    # keeps the hole instead of inventing one.
    assert samples[0].temperature is None
    assert samples[-1].temperature is not None


# ---------------------------------------------------------------------------
# chart geometry
# ---------------------------------------------------------------------------
def test_chart_points_stay_inside_the_plot_box() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    samples = load_device_fixture(FIXTURES_DIR / "device.json", now=now)
    chart = build_chart(bucket_points(samples), "Asia/Bangkok")
    assert chart.has_data is True
    assert {series.key for series in chart.series} == {"temperature", "humidity"}

    for series in chart.series:
        assert series.segments, f"{series.key} has no polyline"
        for segment in series.segments:
            pairs = segment.split(" ")
            assert len(pairs) >= 2
            for pair in pairs:
                x_text, y_text = pair.split(",")
                x, y = float(x_text), float(y_text)
                assert chart.plot_left - 0.01 <= x <= chart.plot_right + 0.01
                assert chart.plot_top - 0.01 <= y <= chart.plot_bottom + 0.01

    for label in chart.labels:
        assert 0.0 <= label.x <= chart.width
        assert 0.0 <= label.y <= chart.height
    for leader in chart.leaders:
        assert 0.0 <= leader.x1 <= chart.width and 0.0 <= leader.x2 <= chart.width
        assert 0.0 <= leader.y1 <= chart.height and 0.0 <= leader.y2 <= chart.height
    assert any(label.text == "NOW" for label in chart.labels)
    # Three clock ticks plus NOW, plus a min and a max annotation for
    # temperature (humidity gets no point labels, only its line color).
    assert len([label for label in chart.labels if label.color == "#000000"]) == 6


def test_chart_breaks_the_line_where_a_reading_is_missing() -> None:
    points = bucket_points(
        [
            sample(60, 30.0, 50.0),
            sample(45, 30.5, 51.0),
            sample(30, None, None),
            sample(15, 31.0, 52.0),
            sample(0, 31.5, 53.0),
        ]
    )
    chart = build_chart(points, "Asia/Bangkok")
    temperature = next(series for series in chart.series if series.key == "temperature")
    assert len(temperature.segments) == 2


def test_chart_without_enough_history_has_no_data() -> None:
    assert build_chart([], "Asia/Bangkok").has_data is False
    assert build_chart(bucket_points([sample(0, 30.0, 50.0)]), "Asia/Bangkok").has_data is False


def test_chart_uses_only_panel_colors() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    samples = load_device_fixture(FIXTURES_DIR / "device.json", now=now)
    chart = build_chart(bucket_points(samples), "Asia/Bangkok")
    # Black temperature, blue humidity: the panel has no gray, so the two
    # series are told apart by color alone, never red.
    allowed = {"#0000FF", "#000000"}
    assert {series.color for series in chart.series} <= allowed
    assert {label.color for label in chart.labels} <= allowed
    assert chart.series_stroke >= 2
    assert chart.axis_stroke >= 2


def test_chart_annotates_temperature_min_and_max_inside_the_box() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    samples = load_device_fixture(FIXTURES_DIR / "device.json", now=now)
    chart = build_chart(bucket_points(samples), "Asia/Bangkok")
    # One leader and one value label for the minimum, one for the maximum.
    assert len(chart.leaders) == 2
    for leader in chart.leaders:
        assert 0.0 <= leader.x1 <= chart.width and 0.0 <= leader.x2 <= chart.width
        assert 0.0 <= leader.y1 <= chart.height and 0.0 <= leader.y2 <= chart.height
        # The leader actually leaves the point rather than sitting on it.
        assert leader.y1 != leader.y2

    numeric_labels = []
    for label in chart.labels:
        try:
            numeric_labels.append(float(label.text))
        except ValueError:
            continue
        assert 0.0 <= label.x <= chart.width
        assert 0.0 <= label.y <= chart.height
    assert len(numeric_labels) == 2
    assert min(numeric_labels) < max(numeric_labels)


# ---------------------------------------------------------------------------
# DESK panel
# ---------------------------------------------------------------------------
def _state_with(device: DeviceBlock, timezone_name: str = "Asia/Bangkok") -> DashboardState:
    return make_state(
        generated_at=datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc),
        timezone=timezone_name,
        device=device,
    )


def test_device_panel_without_a_device(device_hub_settings: HubSettings) -> None:
    panel = device_panel(
        _state_with(DeviceBlock(status=AdapterStatus.UNAVAILABLE, source="store")),
        device_hub_settings,
    )
    assert panel["available"] is False
    assert panel["chart"].has_data is False
    assert panel["chart"].note == "NO DEVICE DATA YET"


def test_device_panel_separates_no_data_from_no_history(device_hub_settings: HubSettings) -> None:
    """A device that just booted has readings but nothing to plot yet."""
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    fresh = sample(0, 32.8, 54.4)
    state = _state_with(
        DeviceBlock(
            status=AdapterStatus.OK,
            source="store",
            device=build_device_state(
                latest=fresh,
                history=[fresh],
                summary=TelemetrySummary(
                    sample_count=1, oldest=fresh.received_at, newest=fresh.received_at
                ),
                now=now,
            ),
        )
    )
    panel = device_panel(state, device_hub_settings)
    assert panel["available"] is True
    assert panel["temperature_text"] == "32.8"
    assert panel["humidity_text"] == "54"
    assert panel["chart"].has_data is False
    assert panel["chart"].note == "NOT ENOUGH HISTORY YET"


def test_device_panel_formats_battery_and_wifi(device_hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = DeviceSample(
        received_at=now,
        device="reterminal-e1002",
        battery_level=12.0,
        wifi_rssi=-84.0,
        temperature=None,
        humidity=None,
    )
    state = _state_with(
        DeviceBlock(
            status=AdapterStatus.OK,
            source="store",
            device=build_device_state(
                latest=latest,
                history=[latest],
                summary=TelemetrySummary(sample_count=1, oldest=now, newest=now),
                now=now,
            ),
        )
    )
    panel = device_panel(state, device_hub_settings)
    assert panel["battery_text"] == "12"
    assert panel["battery_fraction"] == pytest.approx(0.12)
    # 12 is above the 10 percent red floor, so this only warrants yellow.
    assert panel["battery_accent"] == "yellow"
    assert panel["wifi_text"] == "-84"
    # No reading is never drawn as a zero.
    assert panel["temperature_text"] == "--"
    assert panel["humidity_text"] == "--"


def test_power_label_maps_the_three_named_states() -> None:
    assert power_label(True, "charging") == "CHARGING"
    assert power_label(True, "charged") == "USB"
    assert power_label(False, "charging") == "BATTERY"
    assert power_label(False, None) == "BATTERY"


def test_power_label_is_nothing_when_it_cannot_be_determined() -> None:
    # Older firmware never sends usb_present at all.
    assert power_label(None, None) is None
    # usb_present true but the gauge itself doesn't know the charge state.
    assert power_label(True, "unknown") is None
    assert power_label(True, "pre_charge") is None
    assert power_label(True, "not_charging") is None
    assert power_label(True, None) is None


def _device_state_with_power(
    usb_present: bool | None, charge_state: str | None, *, now: datetime
) -> DeviceSample:
    return DeviceSample(
        received_at=now,
        device="reterminal-e1002",
        temperature=30.0,
        humidity=50.0,
        usb_present=usb_present,
        charge_state=charge_state,
    )


def test_device_panel_shows_the_power_label_on_usb(device_hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = _device_state_with_power(True, "charging", now=now)
    state = _state_with(
        DeviceBlock(
            status=AdapterStatus.OK,
            source="store",
            device=build_device_state(
                latest=latest,
                history=[latest],
                summary=TelemetrySummary(sample_count=1, oldest=now, newest=now),
                now=now,
            ),
        )
    )
    panel = device_panel(state, device_hub_settings)
    assert panel["power_word"] == "CHARGING"


def test_device_panel_shows_the_power_label_on_battery(device_hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = _device_state_with_power(False, "not_charging", now=now)
    state = _state_with(
        DeviceBlock(
            status=AdapterStatus.OK,
            source="store",
            device=build_device_state(
                latest=latest,
                history=[latest],
                summary=TelemetrySummary(sample_count=1, oldest=now, newest=now),
                now=now,
            ),
        )
    )
    panel = device_panel(state, device_hub_settings)
    assert panel["power_word"] == "BATTERY"


def test_device_panel_hides_the_power_label_when_unreported(device_hub_settings: HubSettings) -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
    latest = _device_state_with_power(None, None, now=now)
    state = _state_with(
        DeviceBlock(
            status=AdapterStatus.OK,
            source="store",
            device=build_device_state(
                latest=latest,
                history=[latest],
                summary=TelemetrySummary(sample_count=1, oldest=now, newest=now),
                now=now,
            ),
        )
    )
    panel = device_panel(state, device_hub_settings)
    assert panel["power_word"] is None


def test_system_page_drops_the_home_room_rows(
    hub_settings: HubSettings, state: DashboardState
) -> None:
    """The DESK panel owns temperature and humidity now."""
    context = system_context(state, hub_settings)
    names = {row["name"] for row in context["home_rows"] if row["kind"] == "sensor"}
    assert "ROOM TEMP" not in names
    assert "ROOM HUMIDITY" not in names
    assert "FRONT DOOR" in names


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
class _DeviceKeyClient:
    """A thin wrapper around a plain ``TestClient``, injecting the device
    key bearer on every ``get()``/``post()``: /api/device/*, /api/state and
    /healthz all now require a reader credential once the hub is set up
    (see hub_config.py), and this fixture's whole point is to test those
    routes, not the auth gate itself.
    """

    def __init__(self, client: TestClient, device_key: str) -> None:
        self._client = client
        self._device_key = device_key
        self.app = client.app

    def _auth_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._device_key}", **kwargs.pop("headers", {})}
        kwargs["headers"] = headers
        return kwargs

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._client.get(url, **self._auth_kwargs(kwargs))

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._client.post(url, **self._auth_kwargs(kwargs))


@pytest.fixture()
def device_client(device_hub_settings: HubSettings, device_env: Env) -> _DeviceKeyClient:
    """No lifespan on purpose: none of these endpoints renders, so the test
    does not need to pay for a Chromium start. Claims the hub so the
    endpoints below are reachable at all, then hands back a client that
    sends the device key on every call.
    """
    client = TestClient(create_app(device_env, device_hub_settings))
    secrets = asyncio.run(
        client.app.state.hub.identity.claim(
            name="deskmate", base_url="http://dashboard-hub.lan:8080"
        )
    )
    return _DeviceKeyClient(client, secrets.device_key)


def test_post_telemetry_is_accepted_and_stored(device_client: _DeviceKeyClient) -> None:
    response = device_client.post("/api/device/telemetry", json=DEVICE_PAYLOAD)
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    # Presented in TIMEZONE, not UTC.
    assert body["received_at"].endswith("+07:00")

    latest = device_client.get("/api/device/telemetry").json()
    assert latest["status"] == "ok"
    assert latest["latest"]["temperature"] == pytest.approx(32.80)
    assert latest["latest"]["page"] == "brief"
    assert latest["summary"]["sample_count"] == 1
    assert latest["summary"]["retention_days"] == 30


def test_post_telemetry_stores_and_returns_the_power_fields(device_client: _DeviceKeyClient) -> None:
    response = device_client.post("/api/device/telemetry", json=DEVICE_PAYLOAD_WITH_POWER)
    assert response.status_code == 202

    latest = device_client.get("/api/device/telemetry").json()["latest"]
    assert latest["battery_mode"] is False
    assert latest["usb_present"] is True
    assert latest["charge_state"] == "charging"


def test_post_telemetry_without_power_fields_is_still_accepted(device_client: _DeviceKeyClient) -> None:
    """The exact payload today's firmware sends, with no power fields at all."""
    response = device_client.post("/api/device/telemetry", json=DEVICE_PAYLOAD)
    assert response.status_code == 202

    latest = device_client.get("/api/device/telemetry").json()["latest"]
    assert latest["battery_mode"] is None
    assert latest["usb_present"] is None
    assert latest["charge_state"] is None


def test_post_telemetry_stores_and_returns_the_wake_cause(device_client: _DeviceKeyClient) -> None:
    payload = dict(DEVICE_PAYLOAD, wake_cause="button_left")
    response = device_client.post("/api/device/telemetry", json=payload)
    assert response.status_code == 202

    latest = device_client.get("/api/device/telemetry").json()["latest"]
    assert latest["wake_cause"] == "button_left"


def test_post_telemetry_without_wake_cause_is_still_accepted(device_client: _DeviceKeyClient) -> None:
    """Older firmware that never sends ``wake_cause`` still posts fine."""
    response = device_client.post("/api/device/telemetry", json=DEVICE_PAYLOAD)
    assert response.status_code == 202

    latest = device_client.get("/api/device/telemetry").json()["latest"]
    assert latest["wake_cause"] is None


def test_post_telemetry_stores_the_remote_addr_and_hub_host(device_client: _DeviceKeyClient) -> None:
    response = device_client.post(
        "/api/device/telemetry",
        json=DEVICE_PAYLOAD,
        headers={"Host": "192.0.2.1:8080"},
    )
    assert response.status_code == 202

    hub = device_client.app.state.hub
    origin = hub.telemetry.latest_origin()
    assert origin is not None
    remote_addr, hub_host, _ = origin
    assert remote_addr  # TestClient's synthetic client address
    assert hub_host == "http://192.0.2.1:8080"


def test_post_telemetry_hub_host_uses_the_first_forwarded_proto(
    device_client: _DeviceKeyClient,
) -> None:
    """A proxy chain lists the original client's scheme first in a comma
    separated ``X-Forwarded-Proto``; ``hub_host`` must take that first value
    (here "https"), not the whole raw header or the hub's own plain-http
    scheme."""
    response = device_client.post(
        "/api/device/telemetry",
        json=DEVICE_PAYLOAD,
        headers={"Host": "192.0.2.1:8080", "X-Forwarded-Proto": "https, http"},
    )
    assert response.status_code == 202

    hub = device_client.app.state.hub
    origin = hub.telemetry.latest_origin()
    assert origin is not None
    _remote_addr, hub_host, _received_at = origin
    assert hub_host == "https://192.0.2.1:8080"


def test_post_telemetry_stores_no_hub_host_for_an_invalid_host_header(
    device_client: _DeviceKeyClient,
) -> None:
    """A Host header hub_config.validate_base_url rejects (here: one that
    carries a path) stores ``None`` rather than a garbage URL."""
    response = device_client.post(
        "/api/device/telemetry",
        json=DEVICE_PAYLOAD,
        headers={"Host": "evil.example.com/path"},
    )
    assert response.status_code == 202

    hub = device_client.app.state.hub
    origin = hub.telemetry.latest_origin()
    assert origin is not None
    assert origin[1] is None


def test_telemetry_api_json_never_carries_the_origin_columns(device_client: _DeviceKeyClient) -> None:
    """remote_addr/hub_host are stored (previous tests) but must never reach
    GET /api/device/telemetry or /api/device/history: they are not on
    DeviceTelemetry/DeviceSample, only in the store's own table."""
    device_client.post(
        "/api/device/telemetry",
        json=DEVICE_PAYLOAD,
        headers={"Host": "192.0.2.1:8080"},
    )
    latest_body = device_client.get("/api/device/telemetry").json()
    assert "remote_addr" not in latest_body["latest"]
    assert "hub_host" not in latest_body["latest"]
    assert "remote_addr" not in json.dumps(latest_body)
    assert "hub_host" not in json.dumps(latest_body)

    history_body = device_client.get("/api/device/history").json()
    assert "remote_addr" not in json.dumps(history_body)
    assert "hub_host" not in json.dumps(history_body)


def test_post_telemetry_rejects_an_unrecognized_charge_state(device_client: _DeviceKeyClient) -> None:
    payload = dict(DEVICE_PAYLOAD, charge_state="fully_charged")
    response = device_client.post("/api/device/telemetry", json=payload)
    assert response.status_code == 400
    assert response.json()["accepted"] is False


def test_post_telemetry_accepts_null_numeric_fields(device_client: _DeviceKeyClient) -> None:
    payload = dict(DEVICE_PAYLOAD, temperature=None, humidity=None, battery_level=None)
    response = device_client.post("/api/device/telemetry", json=payload)
    assert response.status_code == 202
    latest = device_client.get("/api/device/telemetry").json()["latest"]
    assert latest["temperature"] is None
    assert latest["humidity"] is None


@pytest.mark.parametrize(
    "body",
    ["not json at all", "{", '{"device": }'],
)
def test_post_telemetry_rejects_a_non_json_body(device_client: _DeviceKeyClient, body: str) -> None:
    response = device_client.post(
        "/api/device/telemetry",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["accepted"] is False


def test_post_telemetry_rejects_a_body_over_the_cap(device_client: _DeviceKeyClient) -> None:
    oversized = json.dumps({"device": "reterminal-e1002", "note": "x" * MAX_OPEN_BODY_BYTES})
    response = device_client.post(
        "/api/device/telemetry",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert device_client.get("/api/device/telemetry").json()["summary"]["sample_count"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"temperature": 30.0},  # no device id
        {"device": "reterminal-e1002", "temperature": "warm"},
        {"device": ""},
        ["not", "an", "object"],
    ],
)
def test_post_telemetry_rejects_an_invalid_payload(
    device_client: _DeviceKeyClient, payload: object
) -> None:
    response = device_client.post("/api/device/telemetry", json=payload)
    assert response.status_code == 400
    assert response.json()["accepted"] is False


def test_get_telemetry_on_an_empty_store(device_client: _DeviceKeyClient) -> None:
    payload = device_client.get("/api/device/telemetry").json()
    assert payload["status"] == "unavailable"
    assert payload["latest"] is None
    assert payload["age_seconds"] is None
    assert payload["summary"]["sample_count"] == 0


def test_history_endpoint_downsamples_to_the_ceiling(device_client: _DeviceKeyClient) -> None:
    empty = device_client.get("/api/device/history").json()
    assert empty["samples"] == []
    assert empty["sample_count"] == 0

    for index in range(5):
        assert (
            device_client.post(
                "/api/device/telemetry", json=dict(DEVICE_PAYLOAD, temperature=30.0 + index)
            ).status_code
            == 202
        )
    payload = device_client.get("/api/device/history", params={"hours": 24}).json()
    assert payload["hours"] == 24.0
    assert payload["sample_count"] == 5
    assert payload["point_count"] == 5
    assert payload["downsampled"] is False
    assert payload["max_points"] == HISTORY_MAX_POINTS
    assert payload["samples"][0]["received_at"].endswith("+07:00")


def test_history_endpoint_rejects_a_bad_window(device_client: _DeviceKeyClient) -> None:
    assert device_client.get("/api/device/history", params={"hours": 0}).status_code == 422
    assert device_client.get("/api/device/history", params={"hours": -1}).status_code == 422


def test_state_and_healthz_carry_the_device_block(device_client: _DeviceKeyClient) -> None:
    # /healthz only replays the last outcome, so force one before reading it.
    device_client.get("/api/state")
    health = device_client.get("/healthz").json()
    assert health["adapters"]["device"]["status"] == "unavailable"
    assert health["adapters"]["device"]["source"] == "store"

    device_client.post("/api/device/telemetry", json=DEVICE_PAYLOAD)
    state = device_client.get("/api/state").json()
    assert state["blocks"]["device"]["status"] == "ok"
    assert state["blocks"]["device"]["device"]["status"] == "ok"
    assert state["blocks"]["device"]["device"]["temperature"] == pytest.approx(32.80)
