"""The header's widget slot: who fills it, what happens when nobody does,
and whether what lands there fits.

R.3 of docs/plan/2026-09-20-owner-feedback-round.md (findings 10a and 10b).
The slot is the header's second cell, after the vertical rule; a module
claims it with a ``HeaderSpec`` and core resolves which module that is per
page (``app/view.py:resolve_header_widget``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.models import (
    AdapterStatus,
    AIUsage,
    AIUsageBlock,
    CalendarBlock,
    DashboardState,
    DeviceBlock,
    DeviceState,
    DeviceStatus,
    Event,
)
from app.modules import (
    HEADER_WIDGET_HEIGHT_PX,
    HEADER_WIDGET_WIDTH_PX,
    HeaderSpec,
    Module,
    ModuleError,
    header_template_name,
    validate_module,
)
from app.modules.agenda.page import agenda_header
from app.modules.ai_usage.page import ai_usage_header
from app.modules.registry import ModulesSettings, ModuleToggle, Registry, builtin_registry
from app.modules.system.page import system_header
from app.renderer.render import Renderer
from app.settings import HubSettings
from app.timeutil import zone
from app.view import build_context, resolve_header_widget
from tests.conftest import make_state, run

#: Every built-in widget, in the order the registry hands them out.
BUILTIN_WIDGETS: tuple[str, ...] = ("agenda", "weather", "system", "ai_usage")

#: The widget root's own box, plus the white paper between it and the
#: header's right cluster.
_WIDGET_GEOMETRY = """(() => {
  const round = value => Math.round(value * 10) / 10;
  const widget = document.querySelector('.hdr-widget');
  const right = document.querySelector('.hdr-right').getBoundingClientRect();
  if (widget === null) {
    return { present: false, rules: document.querySelectorAll('.hdr-left .rule-v').length };
  }
  const box = widget.getBoundingClientRect();
  return {
    present: true,
    rules: document.querySelectorAll('.hdr-left .rule-v').length,
    width: round(box.width),
    height: round(box.height),
    gap: round(right.left - box.right),
    text: widget.textContent.replace(/\\s+/g, ' ').trim(),
  };
})()"""


def settings_with(
    hub_settings: HubSettings,
    *,
    default: str = "weather",
    overrides: dict[str, str] | None = None,
    disabled: frozenset[str] = frozenset(),
) -> HubSettings:
    """The session settings with a different header widget story on them.

    Every module that is mentioned at all gets a row, because that is how a
    saved Modules section looks: one row per installed module.
    """
    rows = [
        ModuleToggle(
            id=module.id,
            enabled=module.id not in disabled,
            header_widget=(overrides or {}).get(module.id, "default"),
        )
        for module in builtin_registry().modules
    ]
    return hub_settings.model_copy(
        update={
            "general": hub_settings.general.model_copy(update={"header_widget": default}),
            "modules": ModulesSettings(items=rows),
        }
    )


@pytest.fixture
def widget_renderer(renderer: Renderer) -> Iterator[Renderer]:
    """The session renderer, handed back with its settings and registry as
    they were: ``hub_settings`` and ``registry`` are both mutable in place
    on purpose (``app/renderer/render.py:Renderer``), which is what lets a
    test re-point the slot without starting a second Chromium."""
    original_settings = renderer.hub_settings
    original_registry = renderer.registry
    yield renderer
    renderer.hub_settings = original_settings
    renderer.registry = original_registry


def point_at(renderer: Renderer, settings: HubSettings) -> None:
    renderer.hub_settings = settings
    renderer.registry = builtin_registry(settings.modules)


def geometry(renderer: Renderer, state: DashboardState, page: str = "today") -> dict[str, Any]:
    return run(renderer.probe(page, state, _WIDGET_GEOMETRY))


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def test_the_default_is_the_weather_reading_the_header_always_drew() -> None:
    """The pinned default, which is what keeps the render gate meaningful:
    a fresh hub's header is the one it has always had."""
    from app.modules.general.settings import GeneralSettings

    assert GeneralSettings().header_widget == "weather"
    assert all(toggle.header_widget == "default" for toggle in ModulesSettings().items)
    assert ModuleToggle(id="today").header_widget == "default"


def test_every_page_resolves_to_the_general_default(hub_settings: HubSettings) -> None:
    registry = builtin_registry()
    for page in registry.page_ids():
        module = resolve_header_widget(registry, hub_settings, page)
        assert module is not None and module.id == "weather", page


def test_a_page_override_beats_the_general_default(hub_settings: HubSettings) -> None:
    settings = settings_with(hub_settings, default="weather", overrides={"agenda": "agenda"})
    registry = builtin_registry(settings.modules)
    assert resolve_header_widget(registry, settings, "agenda").id == "agenda"
    assert resolve_header_widget(registry, settings, "today").id == "weather"


def test_none_at_either_level_means_an_empty_slot(hub_settings: HubSettings) -> None:
    per_page = settings_with(hub_settings, default="weather", overrides={"brief": "none"})
    registry = builtin_registry(per_page.modules)
    assert resolve_header_widget(registry, per_page, "brief") is None
    assert resolve_header_widget(registry, per_page, "today").id == "weather"

    everywhere = settings_with(hub_settings, default="none")
    registry = builtin_registry(everywhere.modules)
    assert all(
        resolve_header_widget(registry, everywhere, page) is None
        for page in registry.page_ids()
    )


def test_a_disabled_modules_widget_falls_back_to_the_first_one_installed(
    hub_settings: HubSettings,
) -> None:
    """A hub restored from a backup made elsewhere, or one whose weather
    module was just turned off, draws the first enabled widget rather than
    a silent hole."""
    settings = settings_with(hub_settings, default="weather", disabled=frozenset({"weather"}))
    registry = builtin_registry(settings.modules)
    assert registry.header_widget("weather") is None
    chosen = resolve_header_widget(registry, settings, "today")
    assert chosen is not None and chosen.id == registry.header_widgets()[0].id == "agenda"


def test_an_id_no_module_answers_for_falls_back_too(hub_settings: HubSettings) -> None:
    settings = settings_with(hub_settings, default="from_another_hub")
    registry = builtin_registry(settings.modules)
    assert resolve_header_widget(registry, settings, "today").id == "agenda"


def test_the_widget_context_carries_the_module_and_its_partial(
    hub_settings: HubSettings, state: DashboardState
) -> None:
    context = build_context("today", state, hub_settings)
    widget = context["header"]["widget"]
    assert widget["module"] == "weather"
    assert widget["template"] == "weather_header.html" == header_template_name("weather")


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------
def test_the_registry_lists_the_installed_widgets_in_module_order() -> None:
    registry = builtin_registry()
    assert tuple(module.id for module in registry.header_widgets()) == BUILTIN_WIDGETS
    assert tuple(module.id for module in registry.installed_header_widgets()) == BUILTIN_WIDGETS


def test_a_disabled_widget_leaves_the_installed_list_alone() -> None:
    settings = ModulesSettings(items=[ModuleToggle(id="weather", enabled=False)])
    registry = builtin_registry(settings)
    assert "weather" not in [module.id for module in registry.header_widgets()]
    assert "weather" in [module.id for module in registry.installed_header_widgets()]


def test_templates_dirs_covers_a_module_with_a_widget_and_no_page() -> None:
    """ai_usage draws no page at all. Its partial still has to be on the
    Jinja search path or the first page that resolves to it dies with a
    TemplateNotFound, on the device."""
    registry = builtin_registry()
    from app.modules.ai_usage import MODULE as AI_USAGE

    assert AI_USAGE.page is None
    assert AI_USAGE.header is not None
    assert AI_USAGE.header.templates_dir in registry.templates_dirs()


def test_a_widget_whose_partial_is_missing_refuses_to_load(tmp_path: Path) -> None:
    module = Module(
        id="nowhere",
        title="NOWHERE",
        version="1.0.0",
        description="Declares a widget whose partial does not exist.",
        header=HeaderSpec(context=lambda state, settings: {}, templates_dir=tmp_path),
    )
    with pytest.raises(ModuleError, match="nowhere_header.html"):
        validate_module(module)


# ---------------------------------------------------------------------------
# what each widget reads
# ---------------------------------------------------------------------------
def test_the_agenda_widget_names_the_next_event_and_clips_its_title(
    hub_settings: HubSettings,
) -> None:
    tz = zone("Asia/Bangkok")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=tz)
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(
            status=AdapterStatus.OK,
            items=[
                Event(id="past", title="Standup", start=now - timedelta(hours=3)),
                Event(
                    id="next",
                    title="Quarterly planning with the whole of engineering and design",
                    start=now + timedelta(hours=2),
                ),
                Event(id="later", title="Drill", start=now + timedelta(days=1)),
            ],
        ),
    )
    widget = agenda_header(state, hub_settings)
    assert widget["available"] is True
    assert widget["when"] == "14:00"
    assert widget["title"].endswith("…")
    assert widget["title"].startswith("Quarterly planning")


def test_the_agenda_widget_says_so_when_nothing_is_left(hub_settings: HubSettings) -> None:
    tz = zone("Asia/Bangkok")
    now = datetime(2026, 9, 20, 12, 0, tzinfo=tz)
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        calendar=CalendarBlock(
            status=AdapterStatus.OK,
            items=[Event(id="past", title="Standup", start=now - timedelta(hours=3))],
        ),
    )
    assert agenda_header(state, hub_settings) == {"available": False}


def test_the_system_widget_is_the_desks_own_temperature_and_humidity(
    hub_settings: HubSettings,
) -> None:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=zone("Asia/Bangkok"))
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        device=DeviceBlock(
            status=AdapterStatus.OK,
            device=DeviceState(
                status=DeviceStatus.OK,
                received_at=now,
                age_seconds=30.0,
                temperature=28.44,
                humidity=61.2,
            ),
        ),
    )
    assert system_header(state, hub_settings) == {
        "available": True,
        "temperature": "28.4",
        "humidity": "61",
    }


def test_the_system_widget_is_unavailable_without_a_device(hub_settings: HubSettings) -> None:
    state = make_state(
        generated_at=datetime(2026, 9, 20, 12, 0, tzinfo=zone("Asia/Bangkok")),
        timezone="Asia/Bangkok",
    )
    assert system_header(state, hub_settings) == {"available": False}


def test_the_ai_usage_widget_reports_the_tightest_window_as_used_percent(
    hub_settings: HubSettings,
) -> None:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=zone("Asia/Bangkok"))
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="claude",
                    short_window_percent_remaining=60,
                    weekly_percent_remaining=12,
                    collection_status="ok",
                ),
                AIUsage(
                    provider="codex",
                    short_window_percent_remaining=90,
                    weekly_percent_remaining=80,
                    collection_status="ok",
                ),
            ],
        ),
    )
    widget = ai_usage_header(state, hub_settings)
    assert widget == {
        "available": True,
        "percent": "88",
        "provider": "CLAUDE",
        "window": "7D",
        "accent": "red",
    }


def test_the_ai_usage_widget_skips_a_provider_that_failed_to_collect(
    hub_settings: HubSettings,
) -> None:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=zone("Asia/Bangkok"))
    state = make_state(
        generated_at=now,
        timezone="Asia/Bangkok",
        ai_usage=AIUsageBlock(
            status=AdapterStatus.OK,
            providers=[
                AIUsage(
                    provider="claude",
                    short_window_percent_remaining=1,
                    collection_status="error",
                )
            ],
        ),
    )
    assert ai_usage_header(state, hub_settings) == {"available": False}


# ---------------------------------------------------------------------------
# what each widget draws
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("widget", BUILTIN_WIDGETS)
def test_every_built_in_widget_renders_inside_its_budget(
    widget_renderer: Renderer, state: DashboardState, hub_settings: HubSettings, widget: str
) -> None:
    """Each widget drawn into a real page, measured in the browser: the root
    fits the 380 x 40 px slot and keeps white paper between itself and the
    header's right cluster."""
    point_at(widget_renderer, settings_with(hub_settings, default=widget))
    measured = geometry(widget_renderer, state)
    assert measured["present"] is True, widget
    assert measured["rules"] == 1, measured
    assert measured["width"] <= HEADER_WIDGET_WIDTH_PX + 0.5, measured
    assert measured["height"] <= HEADER_WIDGET_HEIGHT_PX + 0.5, measured
    assert measured["gap"] >= 8, measured
    assert measured["text"], measured


def test_a_page_set_to_none_draws_neither_the_widget_nor_its_rule(
    widget_renderer: Renderer, state: DashboardState, hub_settings: HubSettings
) -> None:
    point_at(
        widget_renderer,
        settings_with(hub_settings, default="weather", overrides={"today": "none"}),
    )
    empty = geometry(widget_renderer, state, "today")
    assert empty == {"present": False, "rules": 0}

    # The page next to it is untouched: the override is one page's, not the hub's.
    drawn = geometry(widget_renderer, state, "agenda")
    assert drawn["present"] is True and drawn["rules"] == 1


def test_the_page_less_modules_widget_renders_on_a_page_it_does_not_own(
    widget_renderer: Renderer, state: DashboardState, hub_settings: HubSettings
) -> None:
    """ai_usage has no page of its own, so its partial is only ever drawn
    into somebody else's header. That is the whole point of the slot."""
    point_at(widget_renderer, settings_with(hub_settings, default="ai_usage"))
    html = widget_renderer.render_html("agenda", state, embed_fonts=False)
    assert 'class="hdr-widget hdr-ai"' in html


def test_core_clamps_the_slot_even_when_the_partial_does_not(
    widget_renderer: Renderer, state: DashboardState, hub_settings: HubSettings, tmp_path: Path
) -> None:
    """A third-party widget whose own root carries no ``hdr-widget`` class
    (docs/MODULES.md asks a module to add it, but nothing enforces that) is
    still clamped to the 380 x 40 px slot, because ``base.html`` wraps the
    ``{% include %}`` in its own ``.hdr-widget`` div rather than relying on
    the partial to apply the class itself (finding 2,
    docs/plan/2026-09-20-owner-feedback-round.md)."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "acme_header.html").write_text(
        '<div class="hdr-acme" style="width: 2000px; white-space: nowrap;">'
        + "ACME " * 400
        + "</div>",
        encoding="utf-8",
    )
    acme = Module(
        id="acme",
        title="ACME",
        version="1.0.0",
        description="A widget that does not clamp its own root.",
        header=HeaderSpec(context=lambda state, settings: {}, templates_dir=templates_dir),
    )
    settings = hub_settings.model_copy(
        update={"general": hub_settings.general.model_copy(update={"header_widget": "acme"})}
    )
    registry = Registry([*builtin_registry().modules, acme], settings.modules)

    # The wrapper is in the markup, around the partial's own (unclamped) root.
    html_registry_renderer = widget_renderer
    html_registry_renderer.hub_settings = settings
    html_registry_renderer.registry = registry
    html = html_registry_renderer.render_html("today", state, embed_fonts=False)
    assert '<div class="hdr-widget">' in html
    wrapper_start = html.index('<div class="hdr-widget">')
    acme_start = html.index('<div class="hdr-acme"')
    assert wrapper_start < acme_start, "the acme root must sit inside core's wrapper"

    # And the clamp really holds on screen, not just in the source order.
    measured = geometry(widget_renderer, state, "today")
    assert measured["present"] is True, measured
    assert measured["rules"] == 1, measured
    assert measured["width"] <= HEADER_WIDGET_WIDTH_PX + 0.5, measured
    assert measured["height"] <= HEADER_WIDGET_HEIGHT_PX + 0.5, measured
    assert measured["gap"] >= 8, measured
