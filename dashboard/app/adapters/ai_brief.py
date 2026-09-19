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

from app.adapters.base import AdapterUnavailable, received_at_or_mtime
from app.adapters.fixtures import day_delta, load_fixture, shift_tree
from app.config import Env
from app.models import Brief, BriefMode, BriefSection
from app.modules.brief.settings import BriefSettings
from app.modules.general.settings import GeneralSettings
from app.timeutil import now_local, to_local, today_local

logger = logging.getLogger("app.adapters.ai_brief")

DATE_KEYS = frozenset({"generated_at"})
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
BULLET = re.compile(r"^\s*[-*+]\s+(?P<item>.+?)\s*$")


def current_mode(brief: BriefSettings, general: GeneralSettings) -> BriefMode:
    """Morning before ``brief.evening_hour``, evening from that hour on."""
    hour = now_local(general.timezone).hour
    return BriefMode.MORNING if hour < brief.evening_hour else BriefMode.EVENING


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

    def __init__(self, brief: BriefSettings, general: GeneralSettings, env: Env) -> None:
        self._brief = brief
        self._general = general
        self._env = env

    def resolve(self) -> str:
        return self.source

    async def fetch(self) -> Brief:
        env = self._env
        timezone_name = self._general.timezone
        payload = load_fixture(env.fixtures_dir / "brief.json")
        delta = day_delta(
            payload, today_local(timezone_name), enabled=env.fixture_relative_dates
        )
        mode = current_mode(self._brief, self._general)
        raw: Any = payload.get(mode.value)
        if raw is None:
            raw = payload.get("morning") or payload.get("evening")
        if raw is None:
            raise AdapterUnavailable("brief fixture has no morning or evening block")
        brief = Brief.model_validate(shift_tree(raw, delta, DATE_KEYS))
        brief.source = "fixture"
        if brief.generated_at is not None:
            brief.generated_at = to_local(brief.generated_at, timezone_name)
        return brief


class FileBriefAdapter:
    """Brief from ``DATA_DIR/brief``: ``current.json``, the shape
    ``POST /api/brief`` writes, or a hand-authored Markdown fallback."""

    name = "brief"
    source = "file"

    def __init__(self, brief: BriefSettings, general: GeneralSettings, env: Env) -> None:
        self._brief = brief
        self._general = general
        self._env = env
        #: Set on every successful fetch: the file's own ``received_at``, or
        #: its mtime. Read by ``CachedAdapter`` for ``BriefBlock.received_at``.
        self.last_received_at: datetime | None = None

    def resolve(self) -> str:
        return self.source

    @property
    def _directory(self) -> Path:
        return self._env.data_dir / "brief"

    async def fetch(self) -> Brief:
        directory = self._directory
        mode = current_mode(self._brief, self._general)
        current = directory / "current.json"
        if current.is_file():
            return self._from_json(current, mode)
        markdown = directory / f"{mode.value}.md"
        if markdown.is_file():
            brief = parse_markdown_brief(
                markdown.read_text(encoding="utf-8", errors="replace"), mode
            )
            brief.generated_at = _mtime(markdown, self._general.timezone)
            self.last_received_at = brief.generated_at
            return brief
        raise AdapterUnavailable(f"no brief in {directory} (looked for current.json, {mode.value}.md)")

    def _from_json(self, path: Path, mode: BriefMode) -> Brief:
        with path.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        received_raw: Any = payload.get("received_at")
        # Either a single brief, or an object holding both modes.
        if "sections" not in payload and (payload.get("morning") or payload.get("evening")):
            payload = payload.get(mode.value) or payload.get("morning") or payload.get("evening")
            received_raw = payload.get("received_at") if isinstance(payload, dict) else None
        brief = Brief.model_validate(payload)
        brief.source = "file"
        if brief.generated_at is None:
            brief.generated_at = _mtime(path, self._general.timezone)
        else:
            brief.generated_at = to_local(brief.generated_at, self._general.timezone)
        self.last_received_at = received_at_or_mtime(received_raw, path, self._general.timezone)
        return brief


def _mtime(path: Path, timezone_name: str) -> datetime:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=dt_timezone.utc)
    return to_local(stamp, timezone_name)


class AutoBriefAdapter:
    """"auto": the file adapter when a brief file exists, else fixture.

    Re-checked on every ``fetch()`` (see ``AutoAIUsageAdapter``) so a push
    made while the process is running switches the effective source once the
    endpoint calls ``invalidate()``. Also falls back to fixture, instead of
    surfacing an ``error`` block, when the file delegate raises
    ``AdapterUnavailable`` (see ``_file_available``) or the file vanishes
    between the check here and the delegate's own read (TOCTOU).
    """

    name = "brief"

    def __init__(self, brief: BriefSettings, general: GeneralSettings, env: Env) -> None:
        self._brief = brief
        self._general = general
        self._file = FileBriefAdapter(brief, general, env)
        self._fixture = FixtureBriefAdapter(brief, general, env)
        self.last_received_at: datetime | None = None
        #: The delegate actually used on the last fetch; see
        #: ``AutoAIUsageAdapter._last_source``.
        self._last_source = "fixture"

    def _file_available(self) -> bool:
        """Matches exactly what ``FileBriefAdapter.fetch`` reads for the
        *current* mode: ``current.json``, or that mode's own ``.md`` file.
        Checking both ``morning.md`` and ``evening.md`` regardless of mode
        would say "file" is available when only the other mode's Markdown
        exists, and ``fetch`` would then raise ``AdapterUnavailable``."""
        directory = self._file._directory
        if (directory / "current.json").is_file():
            return True
        mode = current_mode(self._brief, self._general)
        return (directory / f"{mode.value}.md").is_file()

    @property
    def source(self) -> str:
        return self._last_source

    def resolve(self) -> str:
        """A pure, live check of what the *next* ``fetch()`` would use; see
        ``AutoAIUsageAdapter.resolve``."""
        return "file" if self._file_available() else "fixture"

    async def fetch(self) -> Brief:
        if self._file_available():
            try:
                value = await self._file.fetch()
            except (AdapterUnavailable, OSError):
                pass
            else:
                self._last_source = "file"
                self.last_received_at = getattr(self._file, "last_received_at", None)
                return value
        value = await self._fixture.fetch()
        self._last_source = "fixture"
        self.last_received_at = getattr(self._fixture, "last_received_at", None)
        return value


def build_brief_adapter(
    brief: BriefSettings, general: GeneralSettings, env: Env
) -> FixtureBriefAdapter | FileBriefAdapter:
    """``push`` is today's ``FileBriefAdapter`` (see ``adapters/base.py``'s
    docstring); 1.2d replaces this with the ``datasets`` row."""
    if brief.source == "push":
        return FileBriefAdapter(brief, general, env)
    return FixtureBriefAdapter(brief, general, env)
