"""Quantization must snap to the six panel colors and never dither."""

from __future__ import annotations

import pytest
from PIL import Image

from app.renderer.palette import (
    PALETTE_RGB,
    assert_palette,
    palette_violations,
    quantize,
    to_png_bytes,
)
from tests.conftest import open_png


def test_quantize_snaps_arbitrary_colors() -> None:
    source = Image.new("RGB", (4, 1))
    source.putdata([(250, 250, 250), (10, 10, 10), (200, 30, 30), (30, 30, 200)])
    result = quantize(source)
    assert list(result.getdata()) == [
        (255, 255, 255),
        (0, 0, 0),
        (255, 0, 0),
        (0, 0, 255),
    ]


def test_quantize_does_not_dither_a_flat_field() -> None:
    source = Image.new("RGB", (16, 16), (128, 128, 128))
    result = quantize(source)
    # With Dither.NONE every pixel maps to the same palette entry.
    assert len(set(result.getdata())) == 1


def test_assert_palette_rejects_off_palette_colors() -> None:
    bad = Image.new("RGB", (2, 2), (17, 99, 42))
    assert palette_violations(bad) == {(17, 99, 42)}
    with pytest.raises(AssertionError):
        assert_palette(bad)


def test_assert_palette_accepts_panel_colors() -> None:
    good = Image.new("RGB", (len(PALETTE_RGB), 1))
    good.putdata(list(PALETTE_RGB))
    assert_palette(good)


def test_png_round_trip_keeps_the_palette() -> None:
    source = Image.new("RGB", (8, 8), (240, 12, 12))
    payload = to_png_bytes(quantize(source))
    image = open_png(payload)
    assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    assert_palette(image)
