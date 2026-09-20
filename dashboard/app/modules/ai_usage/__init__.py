"""The ``ai_usage`` section (``settings.py``), its dataset and its push route.

AI capacity has no page of its own: the Today page draws it. Like ``tasks``
it is a module so the dataset, the settings section and ``POST
/api/ai-usage`` stay one thing that can be turned off in one place.
"""

from __future__ import annotations

from pathlib import Path

from app import __version__
from app.adapters.ai_usage import build_ai_usage_adapter
from app.models import AIUsageBlock
from app.modules import DatasetSpec, Module
from app.modules.ai_usage.routes import build_router
from app.modules.ai_usage.settings import SECTION, AIUsageSettings

#: The demo data this module falls back to on ``source: fixture``.
FIXTURE: Path = Path(__file__).parent / "fixtures" / "ai_usage.json"

MODULE = Module(
    id="ai_usage",
    title="AI USAGE",
    version=__version__,
    description="Per-provider AI capacity, as an agent reports it.",
    settings_model=AIUsageSettings,
    settings_section=SECTION,
    datasets=(
        DatasetSpec(
            name="ai_usage",
            block_model=AIUsageBlock,
            value_field="providers",
            section=SECTION,
            build_adapter=lambda ai_usage, general, context: build_ai_usage_adapter(
                ai_usage, general, context.env, FIXTURE
            ),
            ttl_seconds=lambda ai_usage: ai_usage.ttl_seconds,
            fixture=FIXTURE,
        ),
    ),
    routes=build_router,
    default_order=70,
)

__all__ = ["FIXTURE", "MODULE"]
