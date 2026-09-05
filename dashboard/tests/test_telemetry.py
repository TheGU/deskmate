"""Device telemetry: the store, the downsamplers, the adapter and the endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Iterator

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
from app.config import Settings
from app.main import create_app
from app.models import (
    DEVICE_STALE_AFTER_SECONDS,
    AdapterStatus,
    DashboardState,
    DeviceBlock,
    DeviceSample,
    DeviceStatus,
    DeviceTelemetry,
)
from app.renderer.chart import build_chart
from app.view import device_panel, system_context
from app.telemetry import (
    TelemetryStore,
    TelemetrySummary,
    get_telemetry_store,
    parse_utc,
    utc_iso,
)
from tests.conftest import FIXTURES_DIR, run

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


def sample(minutes_ago: float, temperature: float | None, humidity: float | None) -> DeviceSample:
    return DeviceSample(
        received_at=datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc)
        - timedelta(minutes=minutes_ago),
        device="reterminal-e1002",
        temperature=temperature,
        humidity=humidity,
    )


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[TelemetryStore]:
    instance = TelemetryStore(tmp_path / "telemetry.sqlite", retention_days=30)
    yield instance
    instance.close()


@pytest.fixture()
def device_settings(tmp_path: Path) -> Settings:
    """A hub whose telemetry database starts out empty."""
    return Settings(
        TIMEZONE="Asia/Bangkok",
        FIXTURES_DIR=FIXTURES_DIR,
        DATA_DIR=tmp_path,
        LOG_LEVEL="WARNING",
        DEVICE_SOURCE="store",
    )


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
    with store._lock:  # noqa: SLF001 - the raw column is the point of the test
        raw = store._connection.execute("SELECT received_at FROM telemetry").fetchone()[0]
    assert raw.endswith("+00:00")
    assert raw.startswith("2026-09-05T11:30:00")
    assert parse_utc(raw) == bangkok


def test_insert_prunes_rows_past_the_retention_window(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "short.sqlite", retention_days=2)
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
    store.close()


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
    device_settings: Settings,
) -> None:
    adapter = StoreDeviceAdapter(device_settings)
    with pytest.raises(AdapterUnavailable):
        run(adapter.fetch())


def test_store_adapter_reads_what_was_posted(device_settings: Settings) -> None:
    store = get_telemetry_store(device_settings)
    store.insert(DeviceTelemetry.model_validate(DEVICE_PAYLOAD))
    state = run(StoreDeviceAdapter(device_settings).fetch())
    assert state.status is DeviceStatus.OK
    assert state.temperature == pytest.approx(32.80)
    assert state.sample_count == 1
    store.close()


def test_fixture_adapter_fills_an_empty_store(device_settings: Settings) -> None:
    state = run(FixtureDeviceAdapter(device_settings).fetch())
    assert state.status is DeviceStatus.OK
    assert state.device == "reterminal-e1002"
    assert state.sample_count == 288
    assert len(state.history_24h) == HISTORY_POINTS
    assert state.temperature is not None


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
    assert any(label.text == "NOW" for label in chart.labels)
    # Three clock ticks plus NOW, plus one range label per series.
    assert len([label for label in chart.labels if label.color == "#000000"]) == 4


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
    allowed = {"#FF0000", "#0000FF", "#000000"}
    assert {series.color for series in chart.series} <= allowed
    assert {label.color for label in chart.labels} <= allowed
    assert chart.series_stroke >= 4
    assert chart.axis_stroke >= 3


# ---------------------------------------------------------------------------
# DESK panel
# ---------------------------------------------------------------------------
def _state_with(device: DeviceBlock, timezone_name: str = "Asia/Bangkok") -> DashboardState:
    return DashboardState(
        generated_at=datetime(2026, 9, 5, 12, 0, tzinfo=dt_timezone.utc),
        timezone=timezone_name,
        device=device,
    )


def test_device_panel_without_a_device(device_settings: Settings) -> None:
    panel = device_panel(
        _state_with(DeviceBlock(status=AdapterStatus.UNAVAILABLE, source="store")),
        device_settings,
    )
    assert panel["available"] is False
    assert panel["empty_label"] == "NO DEVICE DATA YET"
    assert panel["chart"].has_data is False


def test_device_panel_separates_no_data_from_no_history(device_settings: Settings) -> None:
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
    panel = device_panel(state, device_settings)
    assert panel["available"] is True
    assert panel["temperature"] == "32.8"
    assert panel["humidity"] == "54%"
    assert panel["chart"].has_data is False
    assert panel["chart"].note == "NOT ENOUGH HISTORY YET"


def test_device_panel_formats_battery_and_wifi(device_settings: Settings) -> None:
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
    panel = device_panel(state, device_settings)
    assert panel["battery_percent"] == "12%"
    assert panel["battery_fill"] == 12
    assert panel["battery_accent"] == "red"
    assert panel["wifi"] == "-84 dBm"
    assert panel["wifi_accent"] == "red"
    # No reading is never drawn as a zero.
    assert panel["temperature"] == "--"
    assert panel["humidity"] == "--"


def test_system_page_drops_the_home_room_rows(settings: Settings, state: DashboardState) -> None:
    """The DESK panel owns temperature and humidity now."""
    context = system_context(state, settings)
    names = {row["name"] for row in context["sensors"]}
    assert "ROOM TEMP" not in names
    assert "ROOM HUMIDITY" not in names
    assert "FRONT DOOR" in names


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@pytest.fixture()
def device_client(device_settings: Settings) -> TestClient:
    """No lifespan on purpose: none of these endpoints renders, so the test
    does not need to pay for a Chromium start."""
    return TestClient(create_app(device_settings))


def test_post_telemetry_is_accepted_and_stored(device_client: TestClient) -> None:
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


def test_post_telemetry_accepts_null_numeric_fields(device_client: TestClient) -> None:
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
def test_post_telemetry_rejects_a_non_json_body(device_client: TestClient, body: str) -> None:
    response = device_client.post(
        "/api/device/telemetry",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["accepted"] is False


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
    device_client: TestClient, payload: object
) -> None:
    response = device_client.post("/api/device/telemetry", json=payload)
    assert response.status_code == 400
    assert response.json()["accepted"] is False


def test_get_telemetry_on_an_empty_store(device_client: TestClient) -> None:
    payload = device_client.get("/api/device/telemetry").json()
    assert payload["status"] == "unavailable"
    assert payload["latest"] is None
    assert payload["age_seconds"] is None
    assert payload["summary"]["sample_count"] == 0


def test_history_endpoint_downsamples_to_the_ceiling(device_client: TestClient) -> None:
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


def test_history_endpoint_rejects_a_bad_window(device_client: TestClient) -> None:
    assert device_client.get("/api/device/history", params={"hours": 0}).status_code == 422
    assert device_client.get("/api/device/history", params={"hours": -1}).status_code == 422


def test_state_and_healthz_carry_the_device_block(device_client: TestClient) -> None:
    health = device_client.get("/healthz").json()
    assert health["adapters"]["device"]["status"] == "unavailable"
    assert health["adapters"]["device"]["source"] == "store"

    device_client.post("/api/device/telemetry", json=DEVICE_PAYLOAD)
    state = device_client.get("/api/state").json()
    assert state["device"]["status"] == "ok"
    assert state["device"]["device"]["status"] == "ok"
    assert state["device"]["device"]["temperature"] == pytest.approx(32.80)
