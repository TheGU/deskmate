"""AI brief adapters: fixtures and files another agent writes.

Opening the brief page never triggers an AI request. The hub only reads what is
already on disk:

* ``data/brief/current.json`` - preferred, structured.
* ``data/brief/morning.md`` and ``data/brief/evening.md`` - fallback, Markdown
  with ``## Section`` headings and ``- item`` bullets.

The mode is chosen by the local clock: morning before ``BRIEF_EVENING_HOUR``
(default 14:00), evening after.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from app.adapters.base import AdapterUnavailable
from app.adapters.fixtures import day_delta, load_fixture, shift_tree
from app.config import Settings
from app.models import Brief, BriefMode, BriefSection
from app.timeutil import now_local, to_local, today_local

logger = logging.getLogger("app.adapters.ai_brief")

DATE_KEYS = frozenset({"generated_at"})
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
BULLET = re.compile(r"^\s*[-*+]\s+(?P<item>.+?)\s*$")


def current_mode(settings: Settings) -> BriefMode:
    """Morning before ``BRIEF_EVENING_HOUR``, evening from that hour on."""
    hour = now_local(settings.timezone).hour
    return BriefMode.MORNING if hour < settings.brief_evening_hour else BriefMode.EVENING


def parse_markdown_brief(text: str, mode: BriefMode) -> Brief:
    """Parse a simple Markdown brief into headline, note and sections."""
    headline = ""
    note = ""
    sections: list[BriefSection] = []
    current: BriefSection | None = None
    paragraphs: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = HEADING.match(line)
        if heading is not None:
            title = heading.group("title").strip()
            if not headline and not sections and line.lstrip().startswith("# "):
                headline = title
                continue
            current = BriefSection(title=title)
            sections.append(current)
            continue
        bullet = BULLET.match(line)
        if bullet is not None:
            item = bullet.group("item").strip()
            if current is None:
                current = BriefSection(title="Notes")
                sections.append(current)
            current.items.append(item)
            continue
        if current is None:
            paragraphs.append(stripped)
        else:
            current.items.append(stripped)

    if paragraphs:
        if not headline:
            headline = paragraphs[0]
            paragraphs = paragraphs[1:]
        if paragraphs:
            note = paragraphs[0]
    if not note and sections and sections[0].items:
        note = sections[0].items[0]
    return Brief(mode=mode, headline=headline, note=note, sections=sections, source="file")


class FixtureBriefAdapter:
    """Brief from ``fixtures/brief.json`` (holds both modes)."""

    name = "brief"
    source = "fixture"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self) -> Brief:
        settings = self._settings
        payload = load_fixture(settings.fixtures_dir / "brief.json")
        delta = day_delta(
            payload, today_local(settings.timezone), enabled=settings.fixture_relative_dates
        )
        mode = current_mode(settings)
        raw: Any = payload.get(mode.value)
        if raw is None:
            raw = payload.get("morning") or payload.get("evening")
        if raw is None:
            raise AdapterUnavailable("brief fixture has no morning or evening block")
        brief = Brief.model_validate(shift_tree(raw, delta, DATE_KEYS))
        brief.source = "fixture"
        if brief.generated_at is not None:
            brief.generated_at = to_local(brief.generated_at, settings.timezone)
        return brief


class FileBriefAdapter:
    """Brief from ``BRIEF_DIR`` (default ``data/brief``)."""

    name = "brief"
    source = "file"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self) -> Brief:
        settings = self._settings
        directory = settings.brief_directory
        mode = current_mode(settings)
        current = directory / "current.json"
        if current.is_file():
            return self._from_json(current, mode)
        markdown = directory / f"{mode.value}.md"
        if markdown.is_file():
            brief = parse_markdown_brief(
                markdown.read_text(encoding="utf-8", errors="replace"), mode
            )
            brief.generated_at = _mtime(markdown, settings.timezone)
            return brief
        raise AdapterUnavailable(f"no brief in {directory} (looked for current.json, {mode.value}.md)")

    def _from_json(self, path: Path, mode: BriefMode) -> Brief:
        with path.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        # Either a single brief, or an object holding both modes.
        if "sections" not in payload and (payload.get("morning") or payload.get("evening")):
            payload = payload.get(mode.value) or payload.get("morning") or payload.get("evening")
        brief = Brief.model_validate(payload)
        brief.source = "file"
        if brief.generated_at is None:
            brief.generated_at = _mtime(path, self._settings.timezone)
        else:
            brief.generated_at = to_local(brief.generated_at, self._settings.timezone)
        return brief


def _mtime(path: Path, timezone_name: str) -> datetime:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=dt_timezone.utc)
    return to_local(stamp, timezone_name)


def build_brief_adapter(settings: Settings) -> FixtureBriefAdapter | FileBriefAdapter:
    if settings.brief_source == "file":
        return FileBriefAdapter(settings)
    return FixtureBriefAdapter(settings)
