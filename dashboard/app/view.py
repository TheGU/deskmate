"""Turns :class:`DashboardState` into the flat context each template needs.

Templates stay dumb: no adapter knowledge, no arithmetic, no fallbacks. Every
"unknown" decision is made here, once.

2.2 (docs/plan/2026-09-19-settings-modules-provisioning.md) moved each
built-in page's own context builder and its page-only helpers into that
page's module package (``app/modules/<id>/page.py``: ``today``, ``agenda``,
``weather``, ``brief``, ``system``). What stays here is what more than one
page, or core itself, reads:

* formatting helpers (``fmt_number``, ``fmt_percent``, ``fmt_time``,
  ``fmt_day_header``, ``fmt_long_date``, ``clip_words``), kept together as one
  category rather than split by their current callers, since any future page
  reaches for the same handful;
* task and event helpers more than one page shares: ``open_tasks`` and
  ``task_sort_key`` (Today and Brief both build a priority list from them),
  ``due_label``/``due_chip_kind`` (Today and Brief), ``task_accent`` (Today's
  own rows and Brief's), ``overdue_tasks`` (the header's chip and Agenda's
  flag) and ``calendar_colors``/``event_color`` (Today and Agenda both paint
  events);
* the stale rules (``stale_info`` and its three dataset wrappers
  ``ai_usage_stale``/``brief_stale``/``tasks_stale``) and ``block_note``,
  each read by three or more pages;
* the palette/chart glue any page may reach for (``worst_accent``,
  ``meter_cells``) even though no built-in page calls them today;
* the shared header and footer (``header_context``, ``footer_context``,
  ``base_context``) and the flag plumbing every page's own ``PageSpec.flag``
  is built from (``page_reference``), plus the DEMO-mark bookkeeping
  (``PAGE_PUSH_DATASETS``, ``PUSHED_BLOCK_MODELS``, ``page_shows_demo_data``);
* the header's widget slot (``resolve_header_widget``,
  ``header_widget_context``): which module fills the cell after the vertical
  rule is core's decision, from the ``general`` and ``modules`` settings and
  the registry, even though every widget's own reading belongs to a module
  (2026-09-20 owner feedback, R.3);
* ``is_heat``, since both the weather module's header widget and the
  Weather page's own hero reading rank heat the same way;
* the alert page itself (``alert_context`` and its own small helpers):
  ``alert`` is reserved to core (``app/modules/__init__.py:RESERVED_IDS``), it
  is never a module, and it is the one page core draws itself;
* ``build_context``/``_with_frame``, which merge a module's own context dict
  with core's frame, and ``PAGE_TITLES``/``PAGE_NAMES``, the one place every
  page's display names (including ``alert``'s) are written down.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, TYPE_CHECKING

from app import icons
from app.models import (
    PRIORITY_RANK,
    AdapterStatus,
    AIUsageBlock,
    Alert,
    AlertPriority,
    Block,
    BriefBlock,
    CalendarBlock,
    DashboardState,
    DeviceBlock,
    DeviceState,
    Event,
    Priority,
    Task,
    TasksBlock,
    Weather,
)
from app.timeutil import to_local

from app.modules import header_template_name
from app.modules.general.settings import HEADER_WIDGET_NONE

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    # app/settings.py's SECTIONS registry is derived from the module
    # registry (app/modules/registry.py), and the registry imports the
    # built-in module packages, which import this module for the shared
    # helpers below. Importing HubSettings only for annotations is what keeps
    # that chain acyclic; ``from __future__ import annotations`` at the top
    # makes every use below a string.
    from app.modules import Module, PageContextFn
    from app.modules.registry import Registry
    from app.settings import HubSettings

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

#: What the status bar and the window list call each page. Short enough to sit
#: in a segment; the window list is the device's navigation, so the order is
#: the button order in PRODUCT.md.
PAGE_NAMES: dict[str, str] = {
    "today": "TODAY",
    "agenda": "AGENDA",
    "weather": "WEATHER",
    "brief": "BRIEF",
    "system": "SYSTEM",
    "alert": "ALERT",
}

# The window list itself is no longer a constant here. It is the registry's
# enabled pages, in settings order (``app/modules/registry.py:Registry.
# pages``), which is what lets a module add a window and the settings page
# take one away. Alert is still never in it: it interrupts and then hands
# the previous page back.


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


#: The ellipsis :func:`clip_words` appends: one real character, never the
#: three ASCII dots CSS ``text-overflow: ellipsis`` draws.
ELLIPSIS = "…"


def clip_words(text: str, budget_chars: int) -> str:
    """Clip ``text`` to ``budget_chars``, never cutting inside a word.

    Returns ``text`` unchanged when it already fits. Otherwise returns the
    longest whole-word prefix that still fits the budget with a trailing
    :data:`ELLIPSIS`, so a title reads "Send vendor quote..." rather than
    stemming mid-word ("Send vendor quote answ...").

    A single word (or a Thai phrase, which carries no spaces to break a line
    on) longer than the whole budget is the one exception: there is no word
    boundary to honour, so it is cut at the budget instead, exactly where
    ``text-overflow: ellipsis`` would have cut it.
    """
    if len(text) <= budget_chars:
        return text
    words = text.split(" ")
    kept: list[str] = []
    length = 0
    for word in words:
        addition = len(word) + (1 if kept else 0)
        if length + addition + len(ELLIPSIS) > budget_chars:
            break
        kept.append(word)
        length += addition
    if not kept:
        return text[: max(budget_chars - len(ELLIPSIS), 0)] + ELLIPSIS
    return " ".join(kept) + ELLIPSIS


# ---------------------------------------------------------------------------
# task and event selection shared by more than one page
# ---------------------------------------------------------------------------
def open_tasks(state: DashboardState) -> list[Task]:
    return [task for task in state.block("tasks", TasksBlock).items if not task.completed]


def task_sort_key(task: Task, today: date) -> tuple[int, int, str]:
    """Overdue first, then priority, then due date, then title."""
    overdue = 0 if (task.due is not None and task.due < today) else 1
    due_rank = (task.due - today).days if task.due is not None else 3650
    return (overdue, PRIORITY_RANK[task.priority] * 1000 + min(due_rank, 999), task.title)


def due_label(due: date | None, today: date) -> str:
    """Short enough to sit next to a task title on an 800px screen.

    The word TOMORROW never appears here (the owner asked for it gone
    everywhere, not just on Today): a task due tomorrow prints its 3-letter
    weekday instead.
    """
    if due is None:
        return ""
    delta = (due - today).days
    if delta < 0:
        return f"{-delta}D LATE"
    if delta == 0:
        return "TODAY"
    if delta == 1:
        return due.strftime("%a").upper()
    return f"DUE {due.strftime('%d %b').upper()}"


def due_chip_kind(due: date | None, today: date) -> str:
    """Which of the five due-chip shapes ``due`` renders as.

    Matches :func:`due_label`'s (and each page's own compact due-text
    helper's) branching exactly, so a row's title budget can be sized to
    that row's own chip instead of assuming every row carries the widest one.
    """
    if due is None:
        return "none"
    delta = (due - today).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta == 1:
        return "day"
    return "date"


def task_accent(task: Task, today: date) -> str:
    if task.due is not None and task.due < today:
        return "red"
    if task.due is not None and task.due == today:
        return "yellow"
    if task.priority is Priority.HIGH:
        return "black"
    return "black"


# ---------------------------------------------------------------------------
# palette / chart glue shared across pages
# ---------------------------------------------------------------------------
#: Colours handed out to calendars nobody named, in the order the calendars
#: first appear. Red and yellow are left out: they mean overdue and caution
#: everywhere else and a calendar is not a state.
DEFAULT_CALENDAR_COLORS: tuple[str, ...] = ("blue", "green", "yellow")


def calendar_colors(state: DashboardState, settings: HubSettings) -> dict[str, str]:
    """Colour per calendar name, configured first, then the default cycle."""
    mapping: dict[str, str] = {}
    for index, feed in enumerate(settings.calendar.feeds):
        mapping[settings.calendar.feed_name(index).lower()] = feed.color
    unnamed = 0
    for event in state.block("calendar", CalendarBlock).items:
        key = (event.calendar or "").lower()
        if not key or key in mapping:
            continue
        mapping[key] = DEFAULT_CALENDAR_COLORS[unnamed % len(DEFAULT_CALENDAR_COLORS)]
        unnamed += 1
    return mapping


def event_color(event: Event, colors: dict[str, str]) -> str:
    """The colour an event's time prints in. Black when it has no calendar."""
    return colors.get((event.calendar or "").lower(), "black")


#: Worst first. A pane title bar carries the most urgent thing under it, so a
#: single red row colours the whole title even when the rest is green.
ACCENT_ORDER: tuple[str, ...] = ("red", "yellow", "green")


def worst_accent(accents: Any) -> str:
    """The loudest accent in a group, or black when the group says nothing."""
    seen = set(accents)
    for accent in ACCENT_ORDER:
        if accent in seen:
            return accent
    return "black"


def meter_cells(value: float | None, filled: bool = True) -> list[bool]:
    """Ten block meter cells, one per ten percent.

    An unknown quantity is ten empty cells, never a guessed fill.
    """
    if value is None or not filled:
        return [False] * 10
    lit = int(round(max(0.0, min(100.0, float(value))) / 10.0))
    return [index < lit for index in range(10)]


def overdue_tasks(state: DashboardState, today: date) -> list[Task]:
    if not state.block("tasks", TasksBlock).usable:
        return []
    return [
        task
        for task in sorted(open_tasks(state), key=lambda item: (item.due or today, item.title))
        if task.due is not None and task.due < today
    ]


# ---------------------------------------------------------------------------
# Staleness (Part 2, DESIGN.md): view-layer only. A dataset a remote agent
# pushes (ai_usage, brief, tasks) carries its own age, and past a threshold
# the panel marks it, exactly the System page's stale BATTERY pattern: a
# yellow tell-tale before the section label, the age in 16 px caps after it.
# Never serialized onto a model: the state fingerprint (state.py) and the
# device's PNG 304 path stay untouched. Because pages are cached (Today's
# TTL is 1800 s, render.py:PAGE_TTL_SECONDS), the mark itself can lag a push
# by up to one page TTL.
# ---------------------------------------------------------------------------
def stale_info(
    reference: datetime | None, source: str, threshold_seconds: float, now: datetime
) -> str | None:
    """None when the age is unknown, the source is a fixture (demo data is
    never marked stale), or the age is under an hour or under the
    threshold; otherwise an age label bucketed to whole hours ("6 H AGO").
    """
    if source == "fixture" or reference is None:
        return None
    age_seconds = (now - reference).total_seconds()
    if age_seconds < threshold_seconds:
        return None
    hours = int(age_seconds // 3600)
    if hours < 1:
        return None
    return f"{hours} H AGO"


def ai_usage_stale(state: DashboardState, settings: HubSettings, now: datetime) -> str | None:
    """Age source: the oldest provider's ``collected_at``."""
    block = state.block("ai_usage", AIUsageBlock)
    collected = [p.collected_at for p in block.providers if p.collected_at is not None]
    reference = min(collected) if collected else None
    return stale_info(reference, block.source, settings.ai_usage.stale_seconds, now)


def brief_stale(state: DashboardState, settings: HubSettings, now: datetime) -> str | None:
    """Age source: ``Brief.generated_at``."""
    block = state.block("brief", BriefBlock)
    reference = block.brief.generated_at if block.brief is not None else None
    return stale_info(reference, block.source, settings.brief.stale_seconds, now)


def tasks_stale(state: DashboardState, settings: HubSettings, now: datetime) -> str | None:
    """Age source: ``TasksBlock.received_at``."""
    block = state.block("tasks", TasksBlock)
    return stale_info(block.received_at, block.source, settings.tasks.stale_seconds, now)


#: Which pushed datasets each page actually shows: what the footer's DEMO
#: mark (any of them sourced "fixture") and the "!" flag (any of them
#: stale) are about. Weather/calendar/home/device keep their own
#: established fixture-fallback story from earlier phases (fetched, or
#: pushed by the device itself, not by an agent), so they are never listed
#: here and never print DEMO. A page missing from this map, or missing one
#: of its own datasets, silently gets no DEMO mark and no stale flag for
#: that dataset: keep it in step with what each page's template actually
#: draws (see "Adding a new pushed dataset" in docs/DATA-SOURCES.md). Today
#: also draws the brief note (``app/modules/today/page.py:brief_note``, the
#: Today page's NOTE field), so "brief" belongs here too, not just on the
#: brief page. These are also each built-in page's ``PageSpec.demo_datasets``;
#: the map stays here as the one place those tuples are written down.
PAGE_PUSH_DATASETS: dict[str, tuple[str, ...]] = {
    "today": ("ai_usage", "brief", "tasks"),
    "agenda": (),
    "weather": (),
    "brief": ("brief", "tasks"),
    "system": (),
    "alert": (),
}

#: The block type each pushed dataset arrives in. Only these three can ever
#: be named in a page's ``demo_datasets`` (``app/modules/__init__.py``:
#: ``PUSHED_DATASETS``), so this map is complete by construction.
PUSHED_BLOCK_MODELS: dict[str, type[Block]] = {
    "ai_usage": AIUsageBlock,
    "brief": BriefBlock,
    "tasks": TasksBlock,
}


def page_shows_demo_data(state: DashboardState, datasets: Sequence[str]) -> bool:
    """True when one of the pushed datasets a page shows is a fixture,
    so a fresh install (nothing pushed yet) never passes demo numbers off
    as real.

    ``datasets`` is the page's own ``demo_datasets``; the caller is the
    footer, which gets it from the page spec.
    """
    return any(
        state.block(name, PUSHED_BLOCK_MODELS[name]).source == "fixture" for name in datasets
    )


# ---------------------------------------------------------------------------
# window flags: page_reference is what each page's own PageSpec.flag builds
# its reference time from.
# ---------------------------------------------------------------------------
# tmux flags a window that wants attention, and a flag on everything is a
# flag on nothing, so the bar is deliberately hard to set: something has to
# be late, broken or unhealthy, not merely worth reading. Rain is not a flag
# because the status bar already carries it on every page.
#
# Until 2.1a these five were one ``window_flags`` function that knew about
# every page at once; 2.1a split them into one function per page and 2.2
# moved each into its own module, next to the page it flags. Each takes its
# reference time from ``state.updated_at``, which is exactly what the footer
# passed in before.
def page_reference(state: DashboardState) -> datetime:
    """The moment a page reasons from: the newest adapter timestamp."""
    return to_local(state.updated_at, state.timezone)


#: Header battery reading: yellow at or below 20 percent, red at or below 10.
#: Different (tighter) thresholds than each page's own battery accent, which
#: colours the System page's own battery reading; the header only has room to
#: warn right before the device actually goes flat.
def header_battery_chip(level: float | None) -> str:
    if level is None:
        return ""
    if level <= 10:
        return "red"
    if level <= 20:
        return "yellow"
    return ""


def wifi_level(rssi: float | None) -> str:
    """The word for a Wi-Fi reading, matching :func:`icons.wifi_icon`."""
    if rssi is None or rssi < -80:
        return "off"
    if rssi <= -68:
        return "low"
    return "strong"


#: The panel is read in Bangkok, where 36 C in the shade or a 40 C feel is the
#: difference between walking and taking a taxi.
HEAT_TEMPERATURE_C: float = 36.0
HEAT_FEELS_LIKE_C: float = 40.0


def is_heat(weather: Weather | None) -> bool:
    if weather is None:
        return False
    return (weather.temperature_c or 0.0) >= HEAT_TEMPERATURE_C or (
        weather.feels_like_c or 0.0
    ) >= HEAT_FEELS_LIKE_C


# ---------------------------------------------------------------------------
# the header's widget slot: which module fills it, and with what
# ---------------------------------------------------------------------------
#: A page override that defers to the General section's own choice, which is
#: what every page does until someone says otherwise
#: (``app/modules/registry.py:ModuleToggle.header_widget``). Kept pinned at
#: this value by default so the slot behaves exactly as it did when the
#: weather reading was hard-coded into ``base.html``.
HEADER_WIDGET_DEFAULT = "default"


def resolve_header_widget(
    registry: Registry, settings: HubSettings, page: str
) -> Module | None:
    """Which module draws ``page``'s header widget, or ``None`` for none.

    In order: the page's own override from the ``modules`` section, then the
    General section's default when the override defers to it. ``none`` at
    either level means an empty slot, rule and all. An id that names no
    enabled widget on this hub falls back to the first enabled widget in
    module order rather than drawing nothing: a hub restored from a backup
    taken elsewhere, or one whose weather module has just been turned off,
    still shows something rather than a silent hole.

    A pure function of settings, registry, state and page, deliberately: the
    PNG cache and the ETag are keyed by a state fingerprint, so a widget
    that changed with the clock would be served stale for a whole page TTL
    (docs/plan/2026-09-20-owner-feedback-round.md, finding 10b).
    """
    override = registry.header_override(page)
    pick = settings.general.header_widget if override == HEADER_WIDGET_DEFAULT else override
    if pick == HEADER_WIDGET_NONE:
        return None
    chosen = registry.header_widget(pick)
    if chosen is not None:
        return chosen
    widgets = registry.header_widgets()
    return widgets[0] if widgets else None


def header_widget_context(
    state: DashboardState, settings: HubSettings, registry: Registry, page: str
) -> dict[str, Any] | None:
    """The chosen widget's own dict, plus the two keys core owns.

    ``module`` and ``template`` are merged *over* the module's dict, the
    same rule ``_with_frame`` follows for the frame as a whole: a widget
    cannot point core's include at another file.
    """
    module = resolve_header_widget(registry, settings, page)
    if module is None or module.header is None:
        return None
    return {
        **module.header.context(state, settings),
        "module": module.id,
        "template": header_template_name(module.id),
    }


#: Pages that already show the overdue task themselves, in the red 1D LATE
#: chip on their own priority row: the header's own overdue chip would only
#: repeat it, so those pages hide it there.
PAGES_WITH_OWN_OVERDUE_CHIP: frozenset[str] = frozenset({"today", "brief"})


def header_context(
    state: DashboardState,
    settings: HubSettings,
    today: date,
    reference: datetime,
    page: str,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """Everything the shared header draws, on every page.

    ``registry`` is the hub's own, which the widget slot is resolved
    against. Leaving it out uses the built-ins, which is what a direct
    caller (a test, or a module calling ``base_context`` itself, whose
    header core overwrites anyway) wants.
    """
    live = default_registry(settings) if registry is None else registry
    device: DeviceState | None = state.block("device", DeviceBlock).device
    has_device = state.block("device", DeviceBlock).usable and device is not None and device.has_reading
    level = device.battery_level if has_device else None
    rssi = device.wifi_rssi if has_device else None
    usb_present = device.usb_present if has_device else None
    overdue_count = len(overdue_tasks(state, today))
    return {
        "day": reference.strftime("%d"),
        "weekday": reference.strftime("%a").upper(),
        "month": reference.strftime("%b").upper(),
        # The third line of the day stack. 16 px like the other two, so the
        # stack grows inside the 64 px header rather than past it; the
        # numeral beside it drops 1.6 px, which is the whole of this
        # change's effect on every page's pixels (finding 10a).
        "year": reference.strftime("%Y"),
        "widget": header_widget_context(state, settings, live, page),
        "overdue_count": overdue_count,
        "show_overdue_chip": overdue_count > 0 and page not in PAGES_WITH_OWN_OVERDUE_CHIP,
        "wifi_icon": icons.wifi_icon(rssi),
        "battery": {
            "icon": icons.battery_icon(level, usb_present),
            "percent": fmt_number(level, digits=0),
            "chip_accent": header_battery_chip(level),
        },
        "clock": reference.strftime("%H:%M"),
    }


def default_registry(settings: HubSettings | None = None) -> Registry:
    """The built-ins, for a caller that has no registry to hand.

    Core always passes the hub's own registry (``app/main.py`` builds it,
    the renderer carries it). This fallback is for the direct callers -
    tests, and a module context builder calling ``base_context`` on its own,
    whose header and footer core overwrites anyway - and it is imported
    lazily because the registry imports the built-in module packages, which
    import this file.

    ``settings`` is applied when it is given, so a direct caller's own
    ``modules`` section (a disabled page, a per-page header override) is
    honoured rather than silently ignored.
    """
    from app.modules.registry import builtin_registry

    return builtin_registry(None if settings is None else settings.modules)


def enabled_pages() -> tuple[Module, ...]:
    """The built-in pages, for a caller that has no registry to hand."""
    return default_registry().pages()


def footer_context(
    state: DashboardState,
    settings: HubSettings,
    today: date,
    reference: datetime,
    page: str,
    pages: Sequence[Module] | None = None,
    demo_datasets: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The window list: the enabled pages the buttons walk through, plus the
    DEMO mark at the footer's right end for this page.

    ``pages`` is the registry's enabled pages in order, and each one's own
    ``PageSpec.flag`` decides its "!". ``alert`` is never in the list: it
    interrupts and then hands the previous page back.
    """
    window_pages = enabled_pages() if pages is None else tuple(pages)
    if demo_datasets is None:
        demo_datasets = PAGE_PUSH_DATASETS.get(page, ())
    return {
        "windows": [
            {
                "index": index,
                "name": module.title,
                "flag": _flagged(module, state, settings),
                "active": module.id == page,
            }
            for index, module in enumerate(window_pages, start=1)
        ],
        "demo": page_shows_demo_data(state, demo_datasets),
    }


def _flagged(module: Module, state: DashboardState, settings: HubSettings) -> bool:
    spec = module.page
    if spec is None or spec.flag is None:
        return False
    return bool(spec.flag(state, settings))


def base_context(
    state: DashboardState,
    settings: HubSettings,
    page: str,
    pages: Sequence[Module] | None = None,
    demo_datasets: Sequence[str] | None = None,
    registry: Registry | None = None,
) -> dict[str, Any]:
    reference = to_local(state.updated_at, state.timezone)
    today = reference.date()
    return {
        "page": page,
        "page_title": PAGE_TITLES.get(page, page.upper()),
        "state": state,
        "settings": settings,
        "today": today,
        "reference": reference,
        # Named constants for the fixed glyphs; the chosen ones are per row.
        "icons": icons,
        "header": header_context(state, settings, today, reference, page, registry),
        "footer": footer_context(
            state, settings, today, reference, page, pages, demo_datasets
        ),
        # Each page builder fills this with one accent per pane title bar.
        # Kept for agenda, weather, brief and system until their own parts
        # rebuild them off the older pane-title chrome.
        "title_accents": {},
    }


def block_note(status: AdapterStatus, label: str) -> str:
    if status is AdapterStatus.OK:
        return ""
    if status is AdapterStatus.STALE:
        return f"{label}: showing last known data"
    if status is AdapterStatus.UNAVAILABLE:
        return f"{label} {UNAVAILABLE}"
    return f"{label} error"


def alert_priority_word(priority: AlertPriority) -> str:
    return priority.value.upper()


#: The alert page is the one page a colour may own a whole region, because
#: the page itself is a state. Only the three colours this world allows for a
#: state band: red for the two priorities that should interrupt whatever the
#: owner is doing, yellow for one that is merely worth noticing, black for
#: the default. Blue is a calendar's own colour and rain's; it never means an
#: alert.
ALERT_BAND_ACCENT: dict[AlertPriority, str] = {
    AlertPriority.CRITICAL: "red",
    AlertPriority.DOORBELL: "red",
    AlertPriority.IMPORTANT: "yellow",
    AlertPriority.NORMAL: "black",
}


def alert_context(state: DashboardState, settings: HubSettings) -> dict[str, Any]:
    context = base_context(state, settings, "alert")
    alert: Alert | None = state.alert
    context["alert"] = alert
    if alert is None:
        context["band_label"] = "ALERT"
        context["band_right"] = context["header"]["clock"]
        context["band_accent"] = "black"
        context["title"] = "NO ACTIVE ALERT"
        context["message"] = "The hub has nothing to show right now."
        return context
    context["band_label"] = alert.source.upper() if alert.source else "ALERT"
    time_text = to_local(alert.created_at, state.timezone).strftime("%H:%M")
    context["band_right"] = f"{alert_priority_word(alert.priority)} {time_text}"
    context["band_accent"] = ALERT_BAND_ACCENT[alert.priority]
    context["title"] = alert.title.upper()
    context["message"] = alert.message
    return context


#: The one page core draws itself. Every other page comes from a module's
#: ``PageSpec`` (``app/modules/registry.py``); ``alert`` never does, because
#: it interrupts whatever is on screen and then hands it back, which is not
#: something a module may claim (``app/modules/__init__.py:RESERVED_IDS``).
ALERT_PAGE: str = "alert"

CORE_CONTEXT_BUILDERS: dict[str, PageContextFn] = {ALERT_PAGE: alert_context}


def build_context(
    page: str,
    state: DashboardState,
    settings: HubSettings,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """The page's own context, with core's frame merged over it.

    The module builds its dict first and core computes ``header`` and
    ``footer`` after, so a module cannot overwrite the frame by accident or
    on purpose. ``page_title`` comes from the page spec for the same reason:
    the footer and the title bar are what the device navigates by.

    ``registry`` is the hub's own: the footer's window list and the flags
    come from its enabled pages, and the header's widget slot is resolved
    against its installed widgets and its per-page overrides. It is the
    registry rather than the bare page sequence it used to be because the
    slot needs both of those and a page list can answer neither. Leaving it
    out uses the built-ins under ``settings.modules``, which is what a test
    asking for one page's context wants.
    """
    live = default_registry(settings) if registry is None else registry
    core = CORE_CONTEXT_BUILDERS.get(page)
    if core is not None:
        return _with_frame(
            core(state, settings), state, settings, page, live, (), PAGE_TITLES[page]
        )

    module = next((candidate for candidate in live.pages() if candidate.id == page), None)
    if module is None or module.page is None:
        raise KeyError(f"unknown page {page!r}")
    spec = module.page
    return _with_frame(
        spec.context(state, settings),
        state,
        settings,
        page,
        live,
        spec.demo_datasets,
        spec.title,
    )


def _with_frame(
    context: dict[str, Any],
    state: DashboardState,
    settings: HubSettings,
    page: str,
    registry: Registry,
    demo_datasets: Sequence[str],
    title: str,
) -> dict[str, Any]:
    """The page's dict with core's frame merged over it, never under it."""
    frame = base_context(state, settings, page, registry.pages(), demo_datasets, registry)
    merged = dict(context)
    merged["page"] = page
    merged["page_title"] = title
    merged["header"] = frame["header"]
    merged["footer"] = frame["footer"]
    return merged
