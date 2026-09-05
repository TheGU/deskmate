"""The glyph choosers, and the promise that every glyph exists in the subset.

The bundled Nerd Font is a subset: a codepoint that is not in it renders as a
blank box on the panel, which is worse than no icon at all. So the first test
reads the font's own cmap and checks every constant against it.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app import icons
from app.config import REPO_ROOT

FONT_PATH = REPO_ROOT / "dashboard" / "app" / "static" / "fonts" / "SymbolsNerdFontMono-Subset.ttf"


def _codepoints(path: Path) -> set[int]:
    """Every codepoint the font maps, read straight out of its cmap table."""
    data = path.read_bytes()
    table_count = struct.unpack(">H", data[4:6])[0]
    tables: dict[str, int] = {}
    for index in range(table_count):
        record = 12 + 16 * index
        tag = data[record : record + 4].decode("latin1")
        tables[tag] = struct.unpack(">I", data[record + 8 : record + 12])[0]
    cmap = tables["cmap"]
    found: set[int] = set()
    for index in range(struct.unpack(">H", data[cmap + 2 : cmap + 4])[0]):
        record = cmap + 4 + 8 * index
        offset = struct.unpack(">I", data[record + 4 : record + 8])[0]
        sub = cmap + offset
        fmt = struct.unpack(">H", data[sub : sub + 2])[0]
        if fmt == 4:
            seg_x2 = struct.unpack(">H", data[sub + 6 : sub + 8])[0]
            segments = seg_x2 // 2
            ends = struct.unpack(f">{segments}H", data[sub + 14 : sub + 14 + seg_x2])
            start_at = sub + 14 + seg_x2 + 2
            starts = struct.unpack(f">{segments}H", data[start_at : start_at + seg_x2])
            for first, last in zip(starts, ends):
                if last != 0xFFFF:
                    found.update(range(first, last + 1))
        elif fmt == 12:
            groups = struct.unpack(">I", data[sub + 12 : sub + 16])[0]
            for group in range(groups):
                record = sub + 16 + 12 * group
                first, last, _glyph = struct.unpack(">III", data[record : record + 12])
                found.update(range(first, last + 1))
    return found


def _named_glyphs() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(icons).items()
        if name.isupper() and isinstance(value, str) and len(value) == 1
    }


def test_every_named_glyph_exists_in_the_bundled_subset() -> None:
    available = _codepoints(FONT_PATH)
    missing = {
        name: hex(ord(glyph))
        for name, glyph in _named_glyphs().items()
        if ord(glyph) not in available
    }
    assert missing == {}


def test_named_glyphs_are_all_private_use_codepoints() -> None:
    # Nerd Font icons live in the supplementary private use area; anything in
    # the Latin range would be a Unicode symbol standing in for an icon.
    for name, glyph in _named_glyphs().items():
        assert 0xF0000 <= ord(glyph) <= 0xFFFFD, name


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("Humid, storms later", icons.WEATHER_LIGHTNING_RAINY),
        ("Thunderstorms", icons.WEATHER_LIGHTNING_RAINY),
        ("Pouring", icons.WEATHER_POURING),
        ("Showers", icons.WEATHER_RAINY),
        ("Light rain", icons.WEATHER_RAINY),
        ("Fog", icons.WEATHER_FOG),
        ("Mist", icons.WEATHER_FOG),
        ("Hot and hazy", icons.WEATHER_HAZY),
        ("Smoke", icons.WEATHER_HAZY),
        ("Windy", icons.WEATHER_WINDY),
        ("Partly cloudy", icons.WEATHER_PARTLY_CLOUDY),
        ("Cloudy", icons.WEATHER_CLOUDY),
        ("Overcast", icons.WEATHER_CLOUDY),
        ("Sunny", icons.WEATHER_SUNNY),
        ("Clear", icons.WEATHER_SUNNY),
        ("", icons.WEATHER_PARTLY_CLOUDY),
        ("something nobody parsed", icons.WEATHER_PARTLY_CLOUDY),
    ],
)
def test_weather_icon_keywords(condition: str, expected: str) -> None:
    assert icons.weather_icon(condition) == expected


def test_weather_icon_at_night() -> None:
    assert icons.weather_icon("Clear", is_night=True) == icons.WEATHER_NIGHT
    assert icons.weather_icon("", is_night=True) == icons.WEATHER_NIGHT
    # Night does not turn rain into a moon.
    assert icons.weather_icon("Showers", is_night=True) == icons.WEATHER_RAINY


@pytest.mark.parametrize(
    ("level", "charging", "expected"),
    [
        (100.0, False, icons.BATTERY),
        (92.0, False, icons.BATTERY),
        (72.0, False, icons.BATTERY_80),
        (40.0, False, icons.BATTERY_50),
        (20.0, False, icons.BATTERY_20),
        (4.0, False, icons.BATTERY_ALERT),
        (4.0, True, icons.BATTERY_CHARGING),
        (None, True, icons.BATTERY_CHARGING),
    ],
)
def test_battery_icon(level: float | None, charging: bool, expected: str) -> None:
    assert icons.battery_icon(level, charging) == expected


def test_battery_icon_without_a_level_is_not_an_alert() -> None:
    # "we do not know" must not read as "it is nearly flat".
    assert icons.battery_icon(None, False) == icons.BATTERY


def test_health_icon() -> None:
    assert icons.health_icon("ok") == icons.CHECK
    assert icons.health_icon("warn") == icons.ALERT
    assert icons.health_icon("down") == icons.CLOSE
    assert icons.health_icon("unknown") == icons.ALERT_CIRCLE
    assert icons.health_icon("") == icons.ALERT_CIRCLE


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("room_temperature", icons.THERMOMETER),
        ("room_humidity", icons.WATER_PERCENT),
        ("front_door", icons.DOOR),
        ("doorbell", icons.BELL_RING),
        ("front_lock", icons.LOCK),
        ("desk_light", icons.LIGHTBULB),
        ("ceiling_fan", icons.FAN),
        ("ac", icons.AIR_CONDITIONER),
        ("aircon_living", icons.AIR_CONDITIONER),
        ("motion", icons.HOME),
        ("", icons.HOME),
    ],
)
def test_sensor_icon(key: str, expected: str) -> None:
    assert icons.sensor_icon(key) == expected


def test_sensor_icon_does_not_match_two_letter_keywords_inside_words() -> None:
    # "backup" and "place" are not air conditioners.
    assert icons.sensor_icon("backup") == icons.HOME
    assert icons.sensor_icon("place_holder") == icons.HOME


def test_brief_icon_falls_back_to_a_note() -> None:
    assert icons.brief_icon("Today's schedule") == icons.CALENDAR
    assert icons.brief_icon("At risk") == icons.ALERT
    assert icons.brief_icon("Key tasks") == icons.CHECKBOX_MARKED
    assert icons.brief_icon("Suggested focus") == icons.BRAIN
    assert icons.brief_icon("Something else entirely") == icons.NOTE_TEXT


def test_page_icons_cover_every_page() -> None:
    from app.renderer.render import PAGES

    for page in PAGES:
        assert icons.page_icon(page) in _named_glyphs().values()
    assert set(icons.PAGE_ICONS) == set(PAGES)
