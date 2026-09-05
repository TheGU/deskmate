"""Thai and English mixed content is a product requirement (PRODUCT.md).

Google Sans carries Latin and Thai in the same file, so no separate fallback
face is involved any more. A page that silently renders tofu boxes still
passes the size and palette checks, so these tests ask the browser itself
whether the bundled face actually loaded, and compare the drawn pixels
against the same page in Latin.
"""

from __future__ import annotations

from typing import Any

import pytest
from PIL import Image

from app.models import DashboardState
from app.renderer.palette import DISPLAY_SIZE, assert_palette, palette_violations
from app.renderer.render import Renderer
from tests.conftest import open_png, run

#: Real Thai, with ascenders (the vowel over the first cluster) and descenders
#: (the loop below), because those are what a too-tight row clips first.
THAI_TASK = "ส่งใบเสนอราคาให้ลูกค้า"
THAI_EVENT = "ประชุมทีมออกแบบ"

#: The PRIORITIES pane body: left column inside the 4 px border, under the
#: 56 px status bar, its 4 px rule and its 34 px title bar.
PRIORITIES_BOX = (14, 96, 488, 248)


def _retitled(state: DashboardState, task_title: str, event_title: str) -> DashboardState:
    """The fixture state with every task and event title replaced."""
    tasks = state.tasks.model_copy(
        update={
            "items": [item.model_copy(update={"title": task_title}) for item in state.tasks.items]
        }
    )
    calendar = state.calendar.model_copy(
        update={
            "items": [
                item.model_copy(update={"title": event_title}) for item in state.calendar.items
            ]
        }
    )
    return state.model_copy(update={"tasks": tasks, "calendar": calendar})


@pytest.fixture(scope="module")
def thai_state(state: DashboardState) -> DashboardState:
    return _retitled(state, THAI_TASK, THAI_EVENT)


@pytest.fixture(scope="module")
def latin_state(state: DashboardState) -> DashboardState:
    return _retitled(state, "Send the quote to the customer", "Design team meeting")


def _ink(image: Image.Image, box: tuple[int, int, int, int]) -> int:
    """How many pixels in the region are not paper white."""
    region = image.convert("RGB").crop(box)
    return sum(count for count, color in (region.getcolors(1 << 24) or []) if color != (255, 255, 255))


def test_thai_page_renders_clean(renderer: Renderer, thai_state: DashboardState) -> None:
    image = open_png(run(renderer.render_png("today", thai_state)))
    assert image.size == DISPLAY_SIZE
    assert palette_violations(image) == set()
    assert_palette(image)


def test_thai_bundled_font_is_loaded_in_the_rendered_document(
    renderer: Renderer, thai_state: DashboardState
) -> None:
    """Ask the browser, not the pixels: did the bundled face actually load?"""
    loaded: Any = run(
        renderer.probe("today", thai_state, "document.fonts.check(\"24px 'Google Sans'\")")
    )
    assert loaded is True


def test_thai_and_latin_priorities_differ_on_the_page(
    renderer: Renderer, thai_state: DashboardState, latin_state: DashboardState
) -> None:
    thai = open_png(run(renderer.render_png("today", thai_state)))
    latin = open_png(run(renderer.render_png("today", latin_state)))
    assert thai.convert("RGB").crop(PRIORITIES_BOX).tobytes() != latin.convert("RGB").crop(
        PRIORITIES_BOX
    ).tobytes()


def test_thai_priorities_are_drawn_not_blank(
    renderer: Renderer, thai_state: DashboardState, latin_state: DashboardState
) -> None:
    """Tofu boxes and blank rows both fail here.

    Missing glyphs would either draw nothing (far less ink than the Latin
    control) or draw a grid of identical boxes (far more), so the Thai row has
    to land within a wide band around the Latin one.
    """
    thai = _ink(open_png(run(renderer.render_png("today", thai_state))), PRIORITIES_BOX)
    latin = _ink(open_png(run(renderer.render_png("today", latin_state))), PRIORITIES_BOX)
    assert thai > 0
    assert 0.4 * latin < thai < 2.5 * latin
