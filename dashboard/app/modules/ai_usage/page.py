"""The ai_usage module's header widget.

This module draws no page of its own (the Today page draws the capacity
rows), so this file holds only what the header's slot needs: the one window
that is closest to running out, across every provider the agent reported.
A module with a widget and no page is exactly the case the registry's
``templates_dirs`` has to account for
(``app/modules/registry.py:Registry.templates_dirs``): the partial has to
be findable even though this module owns no page template.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models import AIUsageBlock, DashboardState

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from app.settings import HubSettings

#: Percent *remaining* at or below which the reading is worth a colour.
#: The same thresholds the Today page's capacity rows use
#: (``app/modules/today/page.py:percent_accent``), and never green: plenty
#: of quota left is not news, and a page where the healthy case is coloured
#: teaches the eye to ignore colour.
AI_USAGE_RED_REMAINING: int = 15
AI_USAGE_YELLOW_REMAINING: int = 35


def ai_usage_accent(remaining: int) -> str:
    if remaining <= AI_USAGE_RED_REMAINING:
        return "red"
    if remaining <= AI_USAGE_YELLOW_REMAINING:
        return "yellow"
    return ""


def ai_usage_header(state: DashboardState, settings: "HubSettings") -> dict[str, Any]:
    """This module's header widget: how much of the tightest window is used.

    Tightest means the smallest percent remaining across every provider's
    two windows (the short rolling one and the weekly one), because that is
    the number that decides whether the next prompt goes through. What is
    printed is the used percent, not the remaining one: a widget that says
    "92%" and means "nearly empty" reads backwards at a glance.

    A provider whose own collection failed is skipped rather than shown as
    a confident zero, and a block with nothing usable in it at all is the
    hatch flag.
    """
    block = state.block("ai_usage", AIUsageBlock)
    if not block.usable:
        return {"available": False}
    tightest: tuple[int, str, str] | None = None
    for provider in block.providers:
        if provider.collection_status != "ok":
            continue
        windows = (
            ("5H", provider.short_window_percent_remaining),
            ("7D", provider.weekly_percent_remaining),
        )
        for label, remaining in windows:
            if remaining is None:
                continue
            if tightest is None or remaining < tightest[0]:
                tightest = (remaining, provider.provider.upper(), label)
    if tightest is None:
        return {"available": False}
    remaining, provider_name, window = tightest
    return {
        "available": True,
        "percent": f"{100 - remaining}",
        "provider": provider_name,
        "window": window,
        "accent": ai_usage_accent(remaining),
    }


__all__ = [
    "AI_USAGE_RED_REMAINING",
    "AI_USAGE_YELLOW_REMAINING",
    "ai_usage_accent",
    "ai_usage_header",
]
