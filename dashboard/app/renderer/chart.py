"""Geometry for the 24 hour desk chart, drawn as inline SVG.

All the arithmetic lives here so the template stays a dumb list of shapes:
:func:`build_chart` returns polyline point strings and pre-positioned labels,
and ``system.html`` only pastes them into ``<polyline>``, ``<line>`` and
``<text>``.

The panel has no gray, so both series are told apart by color alone: black
for temperature, blue for humidity. There is no legend box; the page prints a
one-line key under the chart instead. Temperature's minimum and maximum are
called out at their own points with a short leader line and the value,
because a reader can find "how hot did it get" faster from a number at the
peak than from scanning the whole curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Sequence

from app.models import DevicePoint
from app.timeutil import to_local

#: Pure panel primaries. See docs/ARCHITECTURE.md.
BLACK: Final[str] = "#000000"
BLUE: Final[str] = "#0000FF"
WHITE: Final[str] = "#FFFFFF"

#: Sized to sit under the "24 H" label and above the one-line color key in the
#: system page's left column (296 px wide, 170 px tall bottom row).
CHART_WIDTH: Final[int] = 280
CHART_HEIGHT: Final[int] = 118

PLOT_LEFT: Final[float] = 8.0
PLOT_RIGHT: Final[float] = CHART_WIDTH - 8.0
#: Top and bottom margins leave room for the temperature extreme labels
#: (drawn outside the plot box, above the max and below the min) and, at the
#: bottom, the hour tick labels underneath those.
PLOT_TOP: Final[float] = 28.0
PLOT_BOTTOM: Final[float] = 68.0

SERIES_STROKE: Final[int] = 2
AXIS_STROKE: Final[int] = 2
#: The page floor: nothing on the panel is drawn below 16 px.
LABEL_SIZE: Final[int] = 16
LABEL_FONT: Final[str] = "Google Sans, sans-serif"
TICK_SIZE: Final[int] = 16
TICK_BASELINE: Final[float] = CHART_HEIGHT - 4.0

#: Length of the leader line from a temperature extreme point to its label.
LEADER_LEN: Final[float] = 8.0
#: A label within this many px of the plot edge anchors from that edge
#: instead of centering on the point, so it never runs off the chart.
EDGE_MARGIN: Final[float] = 34.0

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
class ChartLeader:
    """One short straight line from a data point to its annotation."""

    x1: float
    y1: float
    x2: float
    y2: float
    color: str = BLACK


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
    leaders: list[ChartLeader] = field(default_factory=list)
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


Coord = tuple[float, float]


def _xy(at: datetime, value: float, start: datetime, span_seconds: float, low: float, high: float) -> Coord:
    plot_width = PLOT_RIGHT - PLOT_LEFT
    plot_height = PLOT_BOTTOM - PLOT_TOP
    ratio = (at - start).total_seconds() / span_seconds
    x = PLOT_LEFT + max(0.0, min(1.0, ratio)) * plot_width
    y = PLOT_BOTTOM - ((value - low) / (high - low)) * plot_height
    return x, y


def _segments(
    points: Sequence[DevicePoint],
    reader: str,
    start: datetime,
    span_seconds: float,
    low: float,
    high: float,
) -> list[str]:
    """Polyline segments for one field, split wherever a reading is missing."""
    segments: list[str] = []
    current: list[str] = []
    for point in points:
        value = getattr(point, reader)
        if value is None:
            if len(current) > 1:
                segments.append(" ".join(current))
            current = []
            continue
        x, y = _xy(point.at, value, start, span_seconds, low, high)
        current.append(f"{x:.1f},{y:.1f}")
    if len(current) > 1:
        segments.append(" ".join(current))
    return segments


def _extreme_anchor(x: float) -> str:
    """Which edge a label at ``x`` should hang from, so it stays on the chart."""
    if x <= PLOT_LEFT + EDGE_MARGIN:
        return "start"
    if x >= PLOT_RIGHT - EDGE_MARGIN:
        return "end"
    return "middle"


def _annotate_extreme(x: float, y: float, value: float, *, upward: bool) -> tuple[ChartLeader, ChartText]:
    """A leader line and value label for one temperature extreme point.

    The maximum always renders at ``y == PLOT_TOP`` and the minimum always at
    ``y == PLOT_BOTTOM`` (the axis is scaled to the data's own extent), so the
    leader only ever needs to run outward into the margin reserved for it,
    never across the line itself.
    """
    anchor = _extreme_anchor(x)
    if upward:
        tip_y = y - LEADER_LEN
        text_y = tip_y - 4.0
    else:
        tip_y = y + LEADER_LEN
        text_y = tip_y + LABEL_SIZE - 2.0
    leader = ChartLeader(x1=x, y1=y, x2=x, y2=tip_y, color=BLACK)
    label = ChartText(x=x, y=text_y, text=f"{value:.1f}", color=BLACK, anchor=anchor, size=LABEL_SIZE)
    return leader, label


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
    leaders: list[ChartLeader] = []

    if len(temperatures) >= 2:
        low, high = _extent(temperatures, MIN_TEMPERATURE_SPAN)
        series.append(
            ChartSeries(
                key="temperature",
                color=BLACK,
                segments=_segments(usable, "temperature", start, span_seconds, low, high),
            )
        )
        # First occurrence of the min and the max: the axis is scaled to
        # exactly this range, so the two points land on the top and bottom
        # plot edges and their leaders never have to cross the line.
        min_point = min(
            ((p.at, p.temperature) for p in usable if p.temperature is not None),
            key=lambda item: item[1],
        )
        max_point = max(
            ((p.at, p.temperature) for p in usable if p.temperature is not None),
            key=lambda item: item[1],
        )
        max_x, max_y = _xy(max_point[0], max_point[1], start, span_seconds, low, high)
        min_x, min_y = _xy(min_point[0], min_point[1], start, span_seconds, low, high)
        leader, label = _annotate_extreme(max_x, max_y, max_point[1], upward=True)
        leaders.append(leader)
        labels.append(label)
        leader, label = _annotate_extreme(min_x, min_y, min_point[1], upward=False)
        leaders.append(leader)
        labels.append(label)
    if len(humidities) >= 2:
        low, high = _extent(humidities, MIN_HUMIDITY_SPAN)
        series.append(
            ChartSeries(
                key="humidity",
                color=BLUE,
                segments=_segments(usable, "humidity", start, span_seconds, low, high),
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

    return Chart(series=series, labels=labels, leaders=leaders, has_data=True, note=note)


__all__ = ["Chart", "ChartLeader", "ChartSeries", "ChartText", "build_chart"]
