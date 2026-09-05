"""Nerd Font glyphs, and the small choosers that pick one.

Every codepoint here exists in ``static/fonts/SymbolsNerdFontMono-Subset.ttf``;
nothing else does, so a glyph that is not named below renders as a blank box on
the panel. Templates never carry a codepoint: they either print a named
constant off the ``icons`` module that :mod:`app.view` puts in the context, or
they print a glyph that ``view.py`` already chose for that row.

The choosers are deliberately keyword based. Weather conditions, Home Assistant
entity keys and service health strings all arrive as free text from an adapter,
so the match has to be forgiving and has to fall back to something honest
rather than to a blank.
"""

from __future__ import annotations

import re
from typing import Final

# --- calendar and time -----------------------------------------------------
CALENDAR: Final[str] = "\U000f00ed"
CALENDAR_CHECK: Final[str] = "\U000f00ef"
CALENDAR_CLOCK: Final[str] = "\U000f00f0"
CLOCK: Final[str] = "\U000f0150"

# --- tasks -----------------------------------------------------------------
CHECK: Final[str] = "\U000f0e1e"
CHECKBOX_BLANK: Final[str] = "\U000f0131"
CHECKBOX_MARKED: Final[str] = "\U000f0132"
FLAG: Final[str] = "\U000f023b"

# --- alerts ----------------------------------------------------------------
ALERT: Final[str] = "\U000f0026"
ALERT_CIRCLE: Final[str] = "\U000f0028"
ALERT_OCTAGON: Final[str] = "\U000f0029"
CLOSE: Final[str] = "\U000f1398"

# --- power -----------------------------------------------------------------
BATTERY: Final[str] = "\U000f0079"
BATTERY_80: Final[str] = "\U000f0081"
BATTERY_50: Final[str] = "\U000f007e"
BATTERY_20: Final[str] = "\U000f007b"
BATTERY_ALERT: Final[str] = "\U000f0083"
BATTERY_CHARGING: Final[str] = "\U000f0084"
USB: Final[str] = "\U000f0553"
POWER_PLUG: Final[str] = "\U000f06a5"
POWER_PLUG_OFF: Final[str] = "\U000f06a6"
SLEEP: Final[str] = "\U000f04b2"
WIFI: Final[str] = "\U000f05a9"
WIFI_STRENGTH_4: Final[str] = "\U000f0922"
WIFI_STRENGTH_2: Final[str] = "\U000f0920"
WIFI_STRENGTH_OFF: Final[str] = "\U000f092d"

# --- measurement -----------------------------------------------------------
THERMOMETER: Final[str] = "\U000f050f"
WATER_PERCENT: Final[str] = "\U000f058e"
SUN_THERMOMETER: Final[str] = "\U000f18d6"
GAUGE: Final[str] = "\U000f029a"
SMOG: Final[str] = "\U000f0a71"
WHITE_BALANCE_SUNNY: Final[str] = "\U000f05a8"

# --- weather ---------------------------------------------------------------
WEATHER_SUNNY: Final[str] = "\U000f0599"
WEATHER_PARTLY_CLOUDY: Final[str] = "\U000f0595"
WEATHER_CLOUDY: Final[str] = "\U000f0590"
WEATHER_RAINY: Final[str] = "\U000f0597"
WEATHER_POURING: Final[str] = "\U000f0596"
WEATHER_LIGHTNING_RAINY: Final[str] = "\U000f067e"
WEATHER_FOG: Final[str] = "\U000f0591"
WEATHER_HAZY: Final[str] = "\U000f0f30"
WEATHER_WINDY: Final[str] = "\U000f059d"
WEATHER_NIGHT: Final[str] = "\U000f0594"
UMBRELLA: Final[str] = "\U000f054a"

# --- AI and notes ----------------------------------------------------------
ROBOT: Final[str] = "\U000f06a9"
CREATION: Final[str] = "\U000f0674"
BRAIN: Final[str] = "\U000f09d1"
MESSAGE_TEXT: Final[str] = "\U000f0369"
NOTE_TEXT: Final[str] = "\U000f039e"

# --- home and infrastructure ----------------------------------------------
HOME: Final[str] = "\U000f02dc"
SERVER: Final[str] = "\U000f048b"
DOCKER: Final[str] = "\U000f0868"
DATABASE: Final[str] = "\U000f01bc"
CONSOLE: Final[str] = "\U000f018d"
LIGHTBULB: Final[str] = "\U000f0335"
FAN: Final[str] = "\U000f0210"
AIR_CONDITIONER: Final[str] = "\U000f001b"
DOOR: Final[str] = "\U000f081a"
LOCK: Final[str] = "\U000f033e"
MONITOR: Final[str] = "\U000f0379"
HISTORY: Final[str] = "\U000f02da"
REFRESH: Final[str] = "\U000f0450"
BELL_RING: Final[str] = "\U000f009e"

# --- structure -------------------------------------------------------------
ARROW_RIGHT_BOLD: Final[str] = "\U000f0734"
CHEVRON_RIGHT: Final[str] = "\U000f0142"
MAP_MARKER: Final[str] = "\U000f034e"
TIMER_SAND: Final[str] = "\U000f051f"


#: The glyph that stands for each page in the top bar. The weather page swaps
#: in the live condition glyph when the adapter has one.
PAGE_ICONS: Final[dict[str, str]] = {
    "today": CHECKBOX_MARKED,
    "agenda": CALENDAR,
    "weather": WEATHER_PARTLY_CLOUDY,
    "brief": ROBOT,
    "system": SERVER,
    "alert": BELL_RING,
}


def page_icon(page: str) -> str:
    """Glyph for the page segment of the status bar."""
    return PAGE_ICONS.get(page, SERVER)


#: Checked in order: the first keyword found in the condition text wins, so the
#: more specific words ("thunder", "pouring") come before the general ones.
_WEATHER_KEYWORDS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("thunder", "storm", "lightning"), WEATHER_LIGHTNING_RAINY),
    (("pour", "downpour", "heavy rain"), WEATHER_POURING),
    (("shower", "rain", "drizzle"), WEATHER_RAINY),
    (("fog", "mist"), WEATHER_FOG),
    (("haze", "hazy", "smoke", "smog"), WEATHER_HAZY),
    (("wind", "breez", "gale"), WEATHER_WINDY),
    (("partly", "partial", "mostly clear"), WEATHER_PARTLY_CLOUDY),
    (("overcast", "cloud"), WEATHER_CLOUDY),
    (("sun", "clear", "fair"), WEATHER_SUNNY),
)


def weather_icon(condition: str, is_night: bool = False) -> str:
    """Glyph for a free-text weather condition.

    Unknown text stays partly cloudy rather than claiming sun or rain.
    """
    text = (condition or "").lower()
    for keywords, glyph in _WEATHER_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            if glyph == WEATHER_SUNNY and is_night:
                return WEATHER_NIGHT
            return glyph
    return WEATHER_NIGHT if is_night else WEATHER_PARTLY_CLOUDY


def battery_icon(level: float | None, charging: bool | None = False) -> str:
    """Glyph for a battery level in percent.

    ``charging`` wins over the level: a device on the charger is telling the
    owner something different from a device at 20 percent. An unreported level
    gets the plain battery outline, never the alert glyph, because "we do not
    know" is not "it is nearly flat".
    """
    if charging:
        return BATTERY_CHARGING
    if level is None:
        return BATTERY
    if level >= 90:
        return BATTERY
    if level >= 60:
        return BATTERY_80
    if level >= 35:
        return BATTERY_50
    if level >= 15:
        return BATTERY_20
    return BATTERY_ALERT


def wifi_icon(rssi: float | None) -> str:
    """Glyph for a Wi-Fi signal in three levels, no number attached.

    An unreported reading gets the off glyph, the same one a genuinely dead
    radio would show: "we do not know" and "no signal" both read as "do not
    trust this link" at a glance, and the header has no room for a fourth
    state.
    """
    if rssi is None or rssi < -80:
        return WIFI_STRENGTH_OFF
    if rssi <= -68:
        return WIFI_STRENGTH_2
    return WIFI_STRENGTH_4


_HEALTH_ICONS: Final[dict[str, str]] = {
    "ok": CHECK,
    "warn": ALERT,
    "down": CLOSE,
}


def health_icon(health: str) -> str:
    """Glyph for a service health string. Anything unrecognised is unknown."""
    return _HEALTH_ICONS.get((health or "").lower(), ALERT_CIRCLE)


#: Matched against the words of a Home Assistant entity key, longest intent
#: first: "doorbell" is a bell, "front_door" is a door.
_SENSOR_KEYWORDS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("temp", "thermo"), THERMOMETER),
    (("humid",), WATER_PERCENT),
    (("bell", "chime"), BELL_RING),
    (("door", "gate"), DOOR),
    (("lock", "bolt"), LOCK),
    (("light", "lamp"), LIGHTBULB),
    (("fan",), FAN),
    (("ac", "aircon", "airconditioner", "climate"), AIR_CONDITIONER),
)

_WORD_SPLIT: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def _word_matches(word: str, keyword: str) -> bool:
    """A keyword hits a word by prefix, or anywhere inside a long enough word.

    "doorbell" has to reach BELL and "front_door" has to reach DOOR, so short
    keywords cannot be free-floating substrings: "ac" must be the whole word or
    start it, otherwise "backup" and "place" would both be air conditioners.
    """
    if len(keyword) <= 2:
        return word == keyword
    return word.startswith(keyword) or keyword in word


def sensor_icon(key: str) -> str:
    """Glyph for a home sensor, chosen from the words in its key."""
    words = [word for word in _WORD_SPLIT.split((key or "").lower()) if word]
    for keywords, glyph in _SENSOR_KEYWORDS:
        for keyword in keywords:
            if any(_word_matches(word, keyword) for word in words):
                return glyph
    return HOME


#: Brief section titles are free text written by the AI session, so the pane
#: title bar picks both its glyph and its title bar colour the same forgiving
#: way the sensors do. Order matters twice over: "unfinished" must be caught
#: before "finished", or a section of leftovers would read as done.
_BRIEF_SECTIONS: Final[tuple[tuple[tuple[str, ...], str, str], ...]] = (
    (("schedule", "calendar", "meeting", "agenda"), CALENDAR, "blue"),
    (("risk", "blocker", "problem", "warning"), ALERT, "red"),
    (("open", "unfinished", "remaining", "still"), TIMER_SAND, "black"),
    (("done", "complete", "finished"), CHECK, "green"),
    (("task", "todo", "to do", "action"), CHECKBOX_MARKED, "black"),
    (("focus", "suggest", "priorit"), BRAIN, "yellow"),
    (("change", "moved", "update"), REFRESH, "black"),
    (("tomorrow", "next", "later"), CALENDAR_CLOCK, "black"),
)


def _brief_section(title: str) -> tuple[str, str]:
    """Glyph and title bar accent for one AI brief section title."""
    text = (title or "").lower()
    for keywords, glyph, accent in _BRIEF_SECTIONS:
        if any(keyword in text for keyword in keywords):
            return glyph, accent
    return NOTE_TEXT, "black"


def brief_icon(title: str) -> str:
    """Glyph for one AI brief section title."""
    return _brief_section(title)[0]


def brief_accent(title: str) -> str:
    """Title bar colour for one AI brief section title."""
    return _brief_section(title)[1]


__all__ = [
    "PAGE_ICONS",
    "battery_icon",
    "brief_accent",
    "brief_icon",
    "health_icon",
    "page_icon",
    "sensor_icon",
    "weather_icon",
    "wifi_icon",
]
