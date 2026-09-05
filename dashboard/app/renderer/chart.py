"""Geometry for the 24 hour desk chart, drawn as inline SVG.

All the arithmetic lives here so the template stays a dumb list of shapes:
:func:`build_chart` returns polyline point strings and pre-positioned labels,
and ``system.html`` only pastes them into ``<polyline>`` and ``<text>``.

The chart has to survive quantization to six colors, so it is built from thick
pure primaries on white: 4 px red for temperature, 4 px blue for humidity,
3 px black axes. No gridlines, no gray, no gradients, nothing thinner than
3 px, because anything lighter turns into noise on the panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Sequence

from app.models import DevicePoint
from app.timeutil import to_local

#: Pure panel primaries. See docs/ARCHITECTURE.md.
RED: Final[str] = "#FF0000"
BLUE: Final[str] = "#0000FF"
BLACK: Final[str] = "#000000"
WHITE: Final[str] = "#FFFFFF"

CHART_WIDTH: Final[int] = 344
CHART_HEIGHT: Final[int] = 170

#: Room left of the plot for nothing at all; the value labels sit above it.
PLOT_LEFT: Final[float] = 7.0
PLOT_RIGHT: Final[float] = CHART_WIDTH - 7.0
PLOT_TOP: Final[float] = 30.0
PLOT_BOTTOM: Final[float] = CHART_HEIGHT - 34.0

SERIES_STROKE: Final[int] = 4
AXIS_STROKE: Final[int] = 3
#: The page floor: nothing on the panel is drawn below 20 px.
LABEL_SIZE: Final[int] = 20
#: The page stack, unquoted because an SVG presentation attribute takes a bare
#: font family list. Thai codepoints fall through to Noto Sans Thai per glyph.
LABEL_FONT: Final[str] = "Google Sans Flex, Noto Sans Thai, sans-serif"
TICK_SIZE: Final[int] = 20
#: Baselines for the two text rows.
VALUE_BASELINE: Final[float] = 19.0
TICK_BASELINE: Final[float] = CHART_HEIGHT - 8.0

#: Smallest span each axis is stretched to, so a flat day is not amplified
#: into a mountain range by autoscaling.
MIN_TEMPERATURE_SPAN: Final[float] = 2.0
MIN_HUMIDITY_SPAN: Final[float] = 6.0


@dataclass(frozen=True, slots=True)
class ChartText:
    """One pre-positioned SVG label."""

    x: float
    y: float
    text: str
    color: str
    anchor: str = "start"
    size: int = LABEL_SIZE


@dataclass(frozen=True, slots=True)
class ChartSeries:
    """One data series, already broken into gap-free polyline segments."""

    key: str
    color: str
    #: Each entry is an SVG ``points`` attribute: ``"x,y x,y ..."``.
    segments: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Chart:
    """Everything ``system.html`` needs to draw the SVG."""

    width: int = CHART_WIDTH
    height: int = CHART_HEIGHT
    plot_left: float = PLOT_LEFT
    plot_right: float = PLOT_RIGHT
    plot_top: float = PLOT_TOP
    plot_bottom: float = PLOT_BOTTOM
    axis_stroke: int = AXIS_STROKE
    series_stroke: int = SERIES_STROKE
    label_font: str = LABEL_FONT
    series: list[ChartSeries] = field(default_factory=list)
    labels: list[ChartText] = field(default_factory=list)
    has_data: bool = False
    #: Shown instead of the plot when there is nothing honest to draw.
    note: str = "NO DEVICE DATA YET"


def _extent(values: Sequence[float], minimum_span: float) -> tuple[float, float]:
    """Low and high bound for one axis, widened to ``minimum_span``."""
    low, high = min(values), max(values)
    if high - low < minimum_span:
        middle = (high + low) / 2.0
        low, high = middle - minimum_span / 2.0, middle + minimum_span / 2.0
    return low, high


def _segments(
    points: Sequence[DevicePoint],
    reader: str,
    start: datetime,
    span_seconds: float,
    low: float,
    high: float,
) -> list[str]:
    """Polyline segments for one field, split wherever a reading is missing."""
    plot_width = PLOT_RIGHT - PLOT_LEFT
    plot_height = PLOT_BOTTOM - PLOT_TOP
    value_span = high - low
    segments: list[str] = []
    current: list[str] = []
    for point in points:
        value = getattr(point, reader)
        if value is None:
            if len(current) > 1:
                segments.append(" ".join(current))
            current = []
            continue
        ratio = (point.at - start).total_seconds() / span_seconds
        x = PLOT_LEFT + max(0.0, min(1.0, ratio)) * plot_width
        y = PLOT_BOTTOM - ((value - low) / value_span) * plot_height
        current.append(f"{x:.1f},{y:.1f}")
    if len(current) > 1:
        segments.append(" ".join(current))
    return segments


def _tick_times(start: datetime, end: datetime) -> list[datetime]:
    """Three interior tick times, snapped to the hour on a long enough window."""
    span = (end - start).total_seconds()
    ticks = [start + timedelta(seconds=span * fraction) for fraction in (0.25, 0.5, 0.75)]
    if span < 4 * 3600:
        return ticks
    snapped: list[datetime] = []
    for tick in ticks:
        rounded = (tick + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        snapped.append(min(max(rounded, start), end))
    return snapped


def build_chart(
    points: Sequence[DevicePoint], timezone_name: str, *, note: str = "NO DEVICE DATA YET"
) -> Chart:
    """Lay out the temperature and humidity traces over the sampled window."""
    # Trim empty points off both ends but keep the ones in between: an interior
    # hole is what breaks the polyline where the sensor stopped reporting.
    readings = [
        index
        for index, point in enumerate(points)
        if point.temperature is not None or point.humidity is not None
    ]
    if len(readings) < 2:
        return Chart(note=note)
    usable = list(points[readings[0] : readings[-1] + 1])

    start, end = usable[0].at, usable[-1].at
    span_seconds = (end - start).total_seconds()
    if span_seconds <= 0:
        return Chart(note=note)

    temperatures = [p.temperature for p in usable if p.temperature is not None]
    humidities = [p.humidity for p in usable if p.humidity is not None]

    series: list[ChartSeries] = []
    labels: list[ChartText] = []

    if len(temperatures) >= 2:
        low, high = _extent(temperatures, MIN_TEMPERATURE_SPAN)
        series.append(
            ChartSeries(
                key="temperature",
                color=RED,
                segments=_segments(usable, "temperature", start, span_seconds, low, high),
            )
        )
        labels.append(
            ChartText(
                x=PLOT_LEFT,
                y=VALUE_BASELINE,
                text=f"{min(temperatures):.1f} TO {max(temperatures):.1f} C",
                color=RED,
                anchor="start",
            )
        )
    if len(humidities) >= 2:
        low, high = _extent(humidities, MIN_HUMIDITY_SPAN)
        series.append(
            ChartSeries(
                key="humidity",
                color=BLUE,
                segments=_segments(usable, "humidity", start, span_seconds, low, high),
            )
        )
        labels.append(
            ChartText(
                x=PLOT_RIGHT,
                y=VALUE_BASELINE,
                text=f"{min(humidities):.0f} TO {max(humidities):.0f}%",
                color=BLUE,
                anchor="end",
            )
        )

    if not any(item.segments for item in series):
        return Chart(note=note)

    plot_width = PLOT_RIGHT - PLOT_LEFT
    seen: set[str] = set()
    for tick in _tick_times(start, end):
        text = to_local(tick, timezone_name).strftime("%H:%M")
        # A window only minutes wide would otherwise print the same clock time
        # three times over.
        if text in seen:
            continue
        seen.add(text)
        ratio = (tick - start).total_seconds() / span_seconds
        labels.append(
            ChartText(
                x=PLOT_LEFT + max(0.0, min(1.0, ratio)) * plot_width,
                y=TICK_BASELINE,
                text=text,
                color=BLACK,
                anchor="middle",
                size=TICK_SIZE,
            )
        )
    labels.append(
        ChartText(
            x=PLOT_RIGHT,
            y=TICK_BASELINE,
            text="NOW",
            color=BLACK,
            anchor="end",
            size=TICK_SIZE,
        )
    )

    return Chart(series=series, labels=labels, has_data=True, note=note)


__all__ = ["Chart", "ChartSeries", "ChartText", "build_chart"]
