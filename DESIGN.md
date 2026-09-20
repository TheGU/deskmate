---
name: deskmate
description: An instrument front panel on paper, rendered as a still six-color 800x480 PNG and read at arm's length.
colors:
  paper-white: "#FFFFFF"
  ink-black: "#000000"
  alarm-red: "#FF0000"
  caution-yellow: "#FFFF00"
  healthy-green: "#00FF00"
  weather-blue: "#0000FF"
typography:
  hero:
    fontFamily: "Google Sans, sans-serif"
    fontSize: "96px"
    fontWeight: 500
    lineHeight: 1
    letterSpacing: "normal"
  headline:
    fontFamily: "Google Sans, sans-serif"
    fontSize: "72px"
    fontWeight: 500
    lineHeight: 1.08
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Google Sans, sans-serif"
    fontSize: "48px"
    fontWeight: 500
    lineHeight: 1
    letterSpacing: "normal"
  body:
    fontFamily: "Google Sans, sans-serif"
    fontSize: "24px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "normal"
  label:
    fontFamily: "Google Sans, sans-serif"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.08em"
  icon:
    fontFamily: "Symbols Nerd Font Mono"
    fontSize: "24px"
    fontWeight: 400
    lineHeight: 1
rounded:
  none: "0"
  full: "50%"
spacing:
  rule: "2px"
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  field-label:
    textColor: "{colors.ink-black}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
  numeral:
    textColor: "{colors.ink-black}"
    typography: "{typography.title}"
    rounded: "{rounded.none}"
  chip-plain:
    backgroundColor: "transparent"
    textColor: "{colors.ink-black}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "2px 6px 3px"
  chip-red:
    backgroundColor: "{colors.alarm-red}"
    textColor: "{colors.paper-white}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "2px 6px 3px"
  chip-yellow:
    backgroundColor: "{colors.caution-yellow}"
    textColor: "{colors.ink-black}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "2px 6px 3px"
  telltale-red:
    backgroundColor: "{colors.alarm-red}"
    rounded: "{rounded.full}"
    width: "10px"
    height: "10px"
  telltale-yellow:
    backgroundColor: "{colors.caution-yellow}"
    rounded: "{rounded.full}"
    width: "10px"
    height: "10px"
  telltale-green:
    backgroundColor: "{colors.healthy-green}"
    rounded: "{rounded.full}"
    width: "10px"
    height: "10px"
  telltale-blue:
    backgroundColor: "{colors.weather-blue}"
    rounded: "{rounded.full}"
    width: "10px"
    height: "10px"
  window-tab:
    backgroundColor: "{colors.paper-white}"
    textColor: "{colors.ink-black}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 8px"
    height: "32px"
  window-tab-active:
    backgroundColor: "{colors.ink-black}"
    textColor: "{colors.paper-white}"
---

# Design System: deskmate

## Overview

**Creative North Star: "The Braun Panel"**

deskmate is an instrument front panel rendered on paper. Every value the owner
needs is a large numeral with its small capital label sitting beneath it;
fields are split by a 2 px rule, not a boxed tile. There is no title bar and
no page-name band: the page's own name lives once, in the footer window list,
so nothing on the page repeats what the footer already says.

The surface is unforgiving by construction and the system is built out of
that. Every page is an exact 800x480 still, quantized to six pure colors with
no dithering (tested on the physical panel and rejected), refreshed in about
32 seconds. There is no motion, no hover, no second state, no breakpoint.
Weight and size are the only reliable hierarchy at these dimensions, so a
numeral is drawn large and its label is drawn heavy rather than drawn small.

Color still reports state, not decoration, everywhere except one page: the
alert page is itself a state, so its priority color is allowed to fill the
top band the way nothing else on the panel is allowed to. Elsewhere a colored
field is always news: a chip, a tell-tale dot, a meter fill. The
explicit anti-reference remains the phone-widget look the owner named
directly: rounded app cards, app-store gloss, generic dashboard tiles. Nothing
here is rounded except the tell-tale and month-grid dots, which are marks,
never containers.

**Key Characteristics:**
- Numeral over small-caps label is the base grammar of the whole panel.
- 2 px black rules divide fields; there is no boxed tile and no title bar.
- Six pure colors, no dithering, no gray, no gradient, no shadow.
- A color fills a field only to report a state, with one exception: the
  alert page's top band, which is the state.
- Tell-tale dots and the month grid's day dots are the panel's only round
  shapes.
- Icons come only from the bundled Nerd Font subset.
- Fixed 800x480 frame: nothing reflows, overflow clips or two-line clamps.

## Colors

Six pure primaries, used as signals rather than as a decorative palette.

### Primary
- **Ink Black** (`#000000`): the structural color and the default voice.
  Every rule, every numeral and label at rest, body type, chart axes, and
  the footer's inverted "you are here" block.
- **Paper White** (`#FFFFFF`): the ground of every page and every field that
  has nothing to report.

### Secondary
- **Alarm Red** (`#FF0000`): the loudest report. An overdue task's chip and
  header flag, a service marked down, a dangerous UV/PM2.5/AQI reading,
  dangerous heat on the header and weather hero, a critical or doorbell
  alert band, and the temperature trace on the desk chart.
- **Caution Yellow** (`#FFFF00`): the middle report. A task due today, a
  battery between 11 and 20 percent, a stale device tell-tale, a warned
  sensor or service, an important (non-critical) alert band, and an AI
  quota window at or below 35 percent.

### Tertiary
- **Healthy Green** (`#00FF00`): a state confirmed as fine: an AQI reading
  inside its clean band, a service marked OK on its status mark. It never
  fills a title or a percentage; the panel spends no color on "nothing to
  report."
- **Weather Blue** (`#0000FF`): rain (probability at or above 50 percent, or
  a rain window in the header), a calendar's own identity color on the
  agenda list and month grid, and the humidity trace on the desk chart.

### Named Rules

**The State-Or-Nothing Rule.** A field takes a color only at the instant it
reports a state: red for overdue, down, urgent or dangerous heat/air; yellow
for due today, warn, stale, or low; green for a confirmed-healthy reading;
blue for rain or a calendar's identity. The one exception in the whole build
is the alert page's top band, which is the state and is allowed to fill a
whole region in its priority color.

**The Six-Colors-Exactly Rule.** Only the six values above may appear in a
shipped raster (`app/renderer/palette.py:assert_palette`). No gray, no tint,
no opacity, no gradient, no anti-aliased edge color that survives
quantization. A seventh color is a build failure, not a rounding error.

**The Quiet-Green Rule.** Green confirms; it never leads. It appears on a
status mark or a tell-tale, never as a fill behind a percentage or a whole
field, so a bar that lights up for good news never trains the eye to ignore
color.

**Note: screenshot pages.** The optional Home Assistant dashboard module
(`ha_dashboard`) is not drawn from this system: it screenshots a Lovelace
view the owner built elsewhere and hands the result straight to the same
six-ink quantize step every other page goes through, with no template, no
numeral-and-label grammar and none of this file's rules applied to it. Only
large flat type and high-contrast, near-primary colors survive that snap;
gradients, translucency and mid-grey text do not. See
`docs/HA-DASHBOARD.md` for what a dashboard view needs to look like to
survive it; this file's own rules are unaffected and still bind everything
else on the panel.

## Typography

**Display and Body Font:** Google Sans (variable, weights 400 to 700),
self-hosted, one file carrying Latin and Thai glyphs together.
**Icon Font:** Symbols Nerd Font Mono (bundled subset; a codepoint not in the
subset renders as a blank box, so nothing outside it is ever used).

**Character:** One geometric humanist family carries the whole ramp. Numerals
run at medium-to-semibold weight with tabular figures; their labels run
heavier, smaller and in small caps, so the label never competes with the
number it explains. Thai and English sit inside the same line at the same
optical weight, because both scripts come from the one bundled file.

### Hierarchy
- **Hero** (500, 96px, line-height 1): the weather page's own NOW
  temperature. At most one per page; nothing else on the panel is this
  large.
- **Headline** (500 to 600, 56px to 72px): a page's single dominant reading
  or statement: the header's day numeral (56), the System page's battery
  percent (72), the alert page's title (64, prose rather than a numeral,
  clamped to two lines).
- **Title** (500 to 600, 32px to 48px): the numbers inside a field: AI
  capacity and uptime percentages (32), the header's own weather
  temperature (40), System's desk temperature and humidity (48).
- **Body** (500, 20px to 28px, line-height 1.3): every reading row and
  running-text line on the panel: priority and agenda titles (24), the
  brief's headline (28) and running text (20), sensor and service names
  (20), the alert message (28).
- **Label** (700, 14px to 16px, 0.02em to 0.08em tracking,
  uppercase): the small caps caption beneath or beside every numeral and
  every section: PRIORITIES, AGENDA, BATTERY, the footer's window names,
  chart tick labels. 14px (the System page's chart key and one month-grid
  row) is the panel's floor; 16px is the default.
- **Icon** (Symbols Nerd Font Mono, 400, 16px to 64px): set one to three px
  larger than the text it sits beside so the two share an optical line; the
  weather hero's own glyph is the largest at 64px.

### Named Rules

**The Numeral-And-Label Rule.** Every value on the panel is a large numeral
(or, on the alert page, a large statement) with its own small-caps label
beneath or beside it. A page never shows a bare number without the label
that says what it is, and a label never stands in place of the number.

**The Fourteen-Pixel Floor Rule.** Nothing on the panel is drawn below 14 px,
in HTML or in SVG; 16 px is the default label size and 14 px is reserved for
the three places measured to need it (the System chart key, the agenda month
weekday row, Brief's own due-chip type).

**The Heavy-Label Rule.** Labels are set at 700 weight in uppercase; body and
numeral type stay at 500 to 600. Weight, not size, is what survives
quantization at the label's own small sizes.

## Layout

The frame is fixed at 800x480 and never changes: no breakpoints, no scroll,
no reflow. Vertically the page is a 64 px header, a body that takes the
rest, and a 40 px footer, each closed off by a 2 px black rule (`.rule-h`).
The header holds the day numeral and weekday/month stack on the left, a
vertical 2 px rule, the weather reading, then (right-aligned) the overdue
flag chip, the Wi-Fi glyph, the battery reading and the clock. The footer is
a single row: the five-page window list, each entry numbered, the current
page inverted to a solid black block, and a flagged page marked with "!" in
its own weight.

The body is one or two columns split by a 2 px vertical rule (`.rule-v`).
Today, Agenda and Weather share one shape: a fixed 456 px left column
(padded 16 px on its rule side) and a flexible right column (padded 16 px on
its own rule side). Brief fixes both sides instead (456 px left, 296 px
right), because its right column is a packed task list rather than
overflow-absorbing prose. System breaks from the shared shape entirely: a
fixed 150 px top row (the device instrument cluster) over a bottom row of
three columns (296 px chart, then HUB and HOME splitting whatever width is
left evenly). HUB is a fixed nine-row list (one age per pushed/fetched
dataset, then the device's own sync age, IP and bound hub URL); HOME is one
merged, budgeted list of home sensors then services (the two used to be
separate boxes with separate caps; a busy home now shares one list and one
cap, sensors first). The top row was cut from 200 to 150 px to make room for
HUB's nine rows; see `HOME_ROW_BUDGET` in `dashboard/app/view.py` for the
exact arithmetic both columns' row caps are derived from. Inside a column,
stacked fields are themselves split by 2 px horizontal rules; nothing
between two fields is ever a boxed tile.

Rows are fixed height and quantized to a small set of steps: 20 to 28 px for
dense list rows (agenda, sensor, service, all-day, and the System page's own
denser HUB rows at 20 px), 32 to 36 px for a one-line task or event row,
40 px for a priority row, 52 to 56 px for a two-line wrapped title or the
device battery block. Gaps run 2, 4, 8, 12, 16, 24 px; nothing falls between
those steps. Because the frame cannot grow, every list is truncated at the
view layer to what its own field can hold, and every flexible text field is
either ellipsis-clipped on one line or clamped to two.

### Named Rules

**The 456 Rule.** Today, Agenda, Weather and Brief all give their left
column exactly 456 px; whatever is left, minus the 2 px rule and its 16 px
padding on each side, is the right column's own width. A new page in this
family inherits the 456 px split rather than choosing its own.

**The No-Name-Band Rule.** A page's own name appears exactly once, in the
footer window list. No page repeats it in a header band, a title bar or a
kicker above its own content; the direction that built this panel names this
refusal directly, against the page-name band the previous design carried.

**The Clip-Never-Reflow Rule.** The page cannot get taller or scroll, so
overflow is resolved by clipping, ellipsis or a two-line clamp, decided once
in the view layer (`app/view.py`), never left to the template to discover at
render time.

## Elevation & Depth

There is no elevation: no shadow, no glow, no layering, no translucency
anywhere in the build. Depth is carried entirely by the 2 px rule grid and,
in exactly two places, by inversion to a solid black field: the footer's
current-page block and the agenda month grid's "today" cell. Nowhere else on
the panel does a field invert; a title bar that inverted to mark state, the
previous design's main depth device, does not exist in this build. Where a
surface must be set apart without a rule or an inversion, it is given a 2 px
black border (the hatch box, the line meter's outline, the weather rain bar).

### Named Rules

**The Flat-Paper Rule.** Surfaces are flat, always. The only emphasis
available is a 2 px rule, a 2 px border, or full inversion to black; a
request for a shadow, a lift, a frosted layer or a title-bar accent is
answered with one of those three instead.

## Shapes

Every rectangle and rule is square: radius 0, no exception. The one family of
round shapes on the panel is the mark, not the container: tell-tale dots
(10 px) and the month grid's event dot (6 px) are full circles, used exactly
where a point of attention needs marking. A hatch box (45 degree black and
white stripe, 2 px black border) stands in for any value that is unknown or
unavailable, in place of a numeral, never as a numeral's decoration. Icons
from the Nerd Font subset are the only other curved forms on the page.

## Components

### Field
The base unit of the panel: a small-caps `.label` caption, then its content,
with no border and no background of its own. Fields are separated only by
the grid's 2 px rules, never by a box. A field with nothing to show prints a
plain sentence ("No open tasks", "unknown") in body weight, never a blank
space with no explanation.

### Numeral + Label
The panel's core grammar (`.reading` plus `.label` in `base.html`): a
tabular-figure number at 32 to 96 px, its unit (if any) riding smaller at the
number's own baseline, with the small-caps label sitting beneath or beside
it. A missing value never guesses; it is replaced by a hatch box the same
size the numeral would have occupied.

### Chip
A borderless, filled state pill for a value that belongs to a row (a due
date, the header's overdue count or battery percent).
- **Shape:** no border, padding 2px 6px 3px, 16 px label type (14 px on
  Brief's own narrower task rows, the panel's own floor).
- **Variants:** red (alarm, filled) and yellow (caution, filled) are both
  exercised; `chip-plain` (no fill, black label type) is the default for a
  due date that is not yet urgent. Green and blue chip variants are declared
  in the shared stylesheet but not yet exercised by any page.

### Tell-tale
A small filled circle (10 px) that precedes a word or a reading to flag a
state without coloring the reading itself: the header's weather condition,
a UV or AQI reading, a brief section's risk flag, a stale device. It never
appears for a healthy or neutral state; its absence is the quiet case.

### Event List (Agenda)
A plain list of fixed-height rows: a time (weight 700, the calendar's own
identity color) beside its title in black, one row per event, starting with
today's (including ones already past) and spilling into later days once
today's own events run out. A later day is introduced by its own divider
row, set in the shared `.label` style with a thin rule beside it: "TOMORROW"
for the very next day, the weekday and date (for example "SAT 21 SEP") for
anything further out. How many rows fit, and where the list has to stop and
print "+N more" instead of a partial day, is decided once in `app/view.py`,
never discovered by the template at render time.

### Line Meter
A thin horizontal gauge: a 2 px (or 4 px, "thick") black-outlined bar on a
white ground, filled black (or the state color) from the left for the used
fraction. Used for AI capacity windows and the System page's battery level.
An unknown quantity renders as an empty outline, never a guessed fill.

### Desk Chart
The 24-hour temperature and humidity trace, inline SVG at 280x118. Black for
temperature, blue for humidity, a black L-shaped axis, no gridlines, no
legend box (a one-line color key sits under the chart instead). The
temperature's minimum and maximum are called out with a short leader line and
their value at the point itself. A series breaks into separate polylines
wherever a reading is missing, autoscaling is floored so a flat day is not
amplified into a mountain, and fewer than two readings prints a note instead
of a plot.

### Window List (footer)
The device's own navigation, matching its physical left/right buttons: five
entries, each numbered, gapped 24 px. The current page is inverted to a
solid black block; a page that wants attention is marked with a trailing
"!" rather than a color, because color on the panel means state and a page
itself is not a state.

## Do's and Don'ts

### Do:
- **Do** pair every numeral with its own small-caps label beneath or beside
  it (The Numeral-And-Label Rule).
- **Do** split fields with a 2 px rule; never box a field in a bordered tile.
- **Do** give Today, Agenda, Weather and Brief a 456 px left column (The 456
  Rule).
- **Do** spend color only to report a state, and remember the alert page's
  top band is the one field allowed to be colored for its own sake (The
  State-Or-Nothing Rule).
- **Do** hold the 14 px floor, default to 16 px, and set label type at 700
  weight (The Fourteen-Pixel Floor Rule, The Heavy-Label Rule).
- **Do** draw an unknown or unavailable value as a hatch box, never as a
  guessed number.
- **Do** name a page exactly once, in the footer window list (The
  No-Name-Band Rule).
- **Do** decide truncation once in `app/view.py` and only clip or two-line
  clamp in the template (The Clip-Never-Reflow Rule).

### Don't:
- **Don't** introduce a seventh color, a gray, a tint, an opacity or a
  gradient (The Six-Colors-Exactly Rule).
- **Don't** add a shadow, a lift, a frosted layer or any depth besides a
  rule, a 2 px border, or full inversion to black (The Flat-Paper Rule).
- **Don't** fill a title bar or a whole field with green, or color a healthy
  percentage at all (The Quiet-Green Rule).
- **Don't** put a boxed tile, a title bar, or a page-name band above a
  page's own content; the footer already carries the page's name.
- **Don't** round a corner on a field, chip, meter or hatch box; the round
  shapes on this panel are marks (tell-tale and event dots) only.
- **Don't** use a Unicode symbol, emoji, or plain text character as a
  stand-in for an icon the bundled Nerd Font subset does not contain.
- **Don't** write hover, focus, transition, animation or a media query; the
  panel has one state and one size.
