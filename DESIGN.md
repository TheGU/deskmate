---
name: deskmate
description: A tmux status line printed on six-color e-paper, read at arm's length from one desk.
colors:
  paper-white: "#FFFFFF"
  ink-black: "#000000"
  alarm-red: "#FF0000"
  caution-yellow: "#FFFF00"
  healthy-green: "#00FF00"
  weather-blue: "#0000FF"
typography:
  hero:
    fontFamily: "Google Sans Flex, Noto Sans Thai, sans-serif"
    fontSize: "120px"
    fontWeight: 900
    lineHeight: 0.92
    letterSpacing: "-0.03em"
  display:
    fontFamily: "Google Sans Flex, Noto Sans Thai, sans-serif"
    fontSize: "72px"
    fontWeight: 900
    lineHeight: 1.05
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "Google Sans Flex, Noto Sans Thai, sans-serif"
    fontSize: "56px"
    fontWeight: 900
    lineHeight: 1
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Google Sans Flex, Noto Sans Thai, sans-serif"
    fontSize: "30px"
    fontWeight: 900
    lineHeight: 1
    letterSpacing: "normal"
  body:
    fontFamily: "Google Sans Flex, Noto Sans Thai, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "normal"
  label:
    fontFamily: "Google Sans Flex, Noto Sans Thai, sans-serif"
    fontSize: "20px"
    fontWeight: 900
    lineHeight: 1
    letterSpacing: "0.02em"
  icon:
    fontFamily: "Symbols Nerd Font Mono"
    fontSize: "22px"
    fontWeight: 400
    lineHeight: 1
rounded:
  none: "0"
spacing:
  xs: "2px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "20px"
components:
  status-entry:
    backgroundColor: "{colors.paper-white}"
    textColor: "{colors.ink-black}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 8px"
    height: "56px"
  status-entry-page:
    backgroundColor: "{colors.ink-black}"
    textColor: "{colors.paper-white}"
  status-entry-state:
    backgroundColor: "{colors.alarm-red}"
    textColor: "{colors.paper-white}"
  pane-title:
    backgroundColor: "{colors.ink-black}"
    textColor: "{colors.paper-white}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 12px"
    height: "34px"
  pane-title-caution:
    backgroundColor: "{colors.caution-yellow}"
    textColor: "{colors.ink-black}"
  pane-title-alarm:
    backgroundColor: "{colors.alarm-red}"
    textColor: "{colors.paper-white}"
  chip-neutral:
    backgroundColor: "{colors.paper-white}"
    textColor: "{colors.ink-black}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "1px 8px 3px 8px"
  chip-alarm:
    backgroundColor: "{colors.alarm-red}"
    textColor: "{colors.paper-white}"
  chip-caution:
    backgroundColor: "{colors.caution-yellow}"
    textColor: "{colors.ink-black}"
  chip-healthy:
    backgroundColor: "{colors.healthy-green}"
    textColor: "{colors.ink-black}"
  meter-cell:
    backgroundColor: "{colors.paper-white}"
    rounded: "{rounded.none}"
    width: "14px"
    height: "14px"
  meter-cell-filled:
    backgroundColor: "{colors.healthy-green}"
  state-bar:
    backgroundColor: "{colors.healthy-green}"
    rounded: "{rounded.none}"
    height: "8px"
  window-tab:
    backgroundColor: "{colors.paper-white}"
    textColor: "{colors.ink-black}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 7px"
    height: "44px"
  window-tab-active:
    backgroundColor: "{colors.ink-black}"
    textColor: "{colors.paper-white}"
  alert-frame:
    backgroundColor: "{colors.paper-white}"
    textColor: "{colors.ink-black}"
    typography: "{typography.display}"
    rounded: "{rounded.none}"
    padding: "0 28px"
---

# Design System: deskmate

## Overview

**Creative North Star: "The Desk Status Line"**

deskmate is a tmux status line that stopped being a terminal and became paper. A
band of entries divided by hard rules sits across the top, panes split by thick
black rules fill the middle, and a window list runs along the foot naming the
five pages and flagging the ones that want attention. It reads as something the
owner already knows how to read, because it borrows the grammar of the tool
already open on the monitor beside it.

The surface is unforgiving and the system is built out of that. Every page is an
exact 800x480 still, quantized to six pure colors with no dithering, refreshed
in about 32 seconds. There is no motion, no hover, no second state, no
breakpoint. Anti-aliased type collapses toward 1-bit at these sizes, so weight
and size are the only reliable hierarchy and 20 px is a hard floor rather than a
preference. Nothing is gray, nothing is tinted, nothing is translucent, because
the panel has no way to show it and quantization turns the attempt into noise.

Color is spent, not decorated. The chrome is deliberately neutral: white band,
black rules, black pane title bars, one inverted block marking the page you are
on. A field takes a color only at the moment it has a state to report, which
means a colored field on this paper is always news. The explicit anti-reference
is the e-ink dashboard default of a white page with hairline rules and small
polite text; this refuses it at every level, from the 4 px rules to the 120 px
temperature.

**Key Characteristics:**
- Six pure colors, no dithering, no gray, no gradient, no shadow, no radius.
- Neutral chrome; a color appears only where a field reports a state.
- Type floor of 20 px, row text at 24 px / 700, headline weights at 900.
- Structure carried entirely by 4 px black rules and inverted blocks.
- Icons only from the bundled Nerd Font subset, never a Unicode stand-in.
- Fixed 800x480 frame: everything clips or clamps, nothing reflows.

## Colors

Six pure primaries and nothing between them: the panel's own inks, used as
signals rather than as a palette.

### Primary
- **Ink Black** (`#000000`): the structural color. Every dividing rule, every
  pane title bar at rest, the inverted block marking the current page and the
  active window tab, the chart axes, and all body type on white. Black is the
  system's default voice, not an accent.
- **Paper White** (`#FFFFFF`): the ground of every page, the status band, and
  every reading field inside a pane. White is the resting state of a surface
  that has nothing to report.

### Secondary
- **Alarm Red** (`#FF0000`): the loudest report. Overdue tasks (the one filled
  entry in the status band), a due chip past its date, a service that is down, a
  dangerous UV or PM2.5 reading, dangerous heat on the weather NOW bar, a
  critical or doorbell alert frame, and the temperature trace on the desk chart.
- **Caution Yellow** (`#FFFF00`): the middle report. Due today, a degraded
  service or sensor, a stale device, a battery between 15 and 35 percent, an
  important-but-not-critical alert, and a quota below 35 percent.

### Tertiary
- **Healthy Green** (`#00FF00`): a state that is fine and is worth confirming:
  a sensor reading inside its band, a service marked OK, a charged or charging
  device, a filled battery meter cell, "nothing overdue". Green reports; it
  never leads.
- **Weather Blue** (`#0000FF`): the non-urgent facts that still deserve their
  own voice: rain (probability at or above 50 percent, or a rain window in the
  status band), a calendar's identity color, and the humidity trace on the desk
  chart.

### Named Rules

**The State-Or-Nothing Rule.** Chrome is neutral. A field takes a color only at
the instant it reports a state: blue for rain or a calendar identity, red for
overdue, urgent, down or heat, yellow for caution, due today, warn or stale,
green for healthy, charged or done. If a surface is colored for emphasis,
grouping, branding or decoration, it is wrong.

**The Worst-First Rule.** A pane title bar carries the loudest state underneath
it, ranked red, then yellow, then green. One red row colors the whole bar even
when every other row is fine.

**The Quiet-Green Rule.** Healthy is the default and the default is silent.
Green may fill a chip, a meter cell or a state bar, but it never colors a pane
title bar and never colors a percentage: a bar that lights for good news teaches
the eye to ignore color. Title accents demote green to black.

**The Six-Colors-Exactly Rule.** Only the six values above may appear in a
shipped raster. No gray, no tint, no opacity, no gradient, no anti-aliased edge
color that survives quantization. The renderer asserts this on every page and a
seventh color is a build failure, not a rounding error.

## Typography

**Display Font:** Google Sans Flex (variable, 300 to 1000), self-hosted
**Body Font:** Google Sans Flex, with Noto Sans Thai as the per-glyph fallback
**Label/Icon Font:** Symbols Nerd Font Mono (bundled subset)

**Character:** One geometric humanist family carrying the whole ramp, worked
hard at its heavy end. The pages read as confident and blunt rather than
delicate: nothing whispers, because on this panel a whisper is a smudge. Thai
and English mix inside a single line and the Thai fallback is chosen so the two
scripts sit at the same optical weight.

### Hierarchy
- **Hero** (900, 120px, 0.92, -0.03em): the one number a page exists to show.
  Currently the weather NOW temperature; at most one per page.
- **Display** (900, 72px, 1.05, -0.01em): the alert title, read across a room.
  Clamped to two lines.
- **Headline** (900, 48px to 60px, 1): a page's own large reading. Desk
  temperature (56), rain probability (60), alert time (48).
- **Title** (900, 28px to 40px, 1): the numbers inside cells and rows. Desk
  humidity (40), air metric values (34), forecast highs (30), quota
  percentages (28).
- **Body** (700, 24px, 1.3): every reading row on the paper. Event titles, task
  titles, agenda lines, brief bullets, service details, empty-state notes.
  Priority titles run one step up at 26px for the same job.
- **Label** (800 to 900, 20px, 0.01em to 0.06em, uppercase): pane title bars
  (900, 0.06em), status band entries (800, 0.01em), window tabs (900, 0.02em),
  chips (900), cell labels, chart labels and ticks. The floor of the system.
- **Icon** (Symbols Nerd Font Mono, 400, 20px to 34px inline, 88px as a page
  glyph): set one to three px larger than the text it sits beside so the two
  share an optical line.

### Named Rules

**The Twenty-Pixel Floor Rule.** Nothing on the panel is drawn below 20 px, in
HTML or in SVG. This is a legibility law, not a taste: below it, anti-aliased
strokes quantize to 1-bit and the word breaks apart. When a label will not fit
at 20 px, shorten the word (HUMID, not HUMIDITY) or let the glyph carry the
meaning; never shrink the type.

**The Heavy-At-The-Floor Rule.** Type at or near the floor is set at 800 to 900.
Weight, not size, is what survives quantization, so small text is heavy text and
uppercase, and long-form reading text is the only place 600 to 700 appears.

**The Uppercase-Chrome Rule.** Every label the system writes is uppercase: page
names, pane titles, chips, day labels, status entries. Content the owner or an
adapter wrote (task titles, event titles, brief lines, alert messages) keeps its
own casing and its own script.

## Layout

The frame is fixed at 800x480 and never changes: no breakpoints, no scroll, no
reflow. Vertically the page is a 56 px status band, a body that takes the rest,
and a 44 px window list at the foot, with a 4 px black rule closing the band and
opening the list. The body is one or two columns split by a 4 px vertical rule,
and each column is a stack of panes split by 4 px horizontal rules. Column
widths are literal and per page (500/300 on Today, 400/400 on Agenda and System,
340/460 on Weather), chosen so the wider column holds the longest strings.

A pane is a 34 px title bar plus a white reading field padded 8 px 12 px, tuned
down to 4 px or 6 px vertically on the pages whose rows would otherwise clip a
descender. Rows are fixed-height and quantized to the grid: 26 to 30 px for
dense list rows, 34 px for event rows, 38 px for provider rows, 48 px for
priority rows, 56 px for the desk hero. Gaps run 2, 6, 8, 10, 12 px; nothing
falls between those steps.

Because the frame cannot grow, every list is truncated at the view layer to what
its pane can hold (four priorities, four next events, three sensors, six
services, four brief sections of two lines), and every flexible text field is
either ellipsis-clipped on one line or clamped to two.

### Named Rules

**The Thick-Rule Rule.** Every division is a 4 px black rule; internal cell
dividers and component borders are 3 px; the alert frame is 10 px. Nothing is
ever drawn thinner than 3 px, because a hairline on this panel is a dotted line.

**The Clip-Never-Reflow Rule.** The page cannot get taller, so overflow is
resolved by clipping, ellipsis or a two-line clamp, decided in the view layer.
A row that would push the last row's descenders out of its pane is a layout bug,
not an acceptable overflow.

## Elevation & Depth

There is no elevation. No shadow, no glow, no layering, no opacity, no
translucency anywhere in the build; the panel has no way to render them and a
soft edge is exactly what quantization destroys. Depth is entirely a matter of
inversion and rules: a black title bar over a white field, a black block marking
the page you are on, a 4 px rule where a boundary needs to be felt. Where a
surface must be set apart without a color change, it is given a 3 px black
border, not a lift.

### Named Rules

**The Flat-Paper Rule.** Surfaces are flat, always. Emphasis is inversion (black
field, white type) or a heavier rule. Any request for a shadow, a card lift, a
frosted layer or an offset block is answered with inversion or a rule.

## Shapes

Every corner is square: radius 0, with no exception anywhere in the build.
Shapes are rectangles and rules, sized in whole pixels. Chips are 3 px bordered
rectangles with asymmetric padding (1px 8px 3px 8px) that optically centers
uppercase type. Meter cells are 14 px squares with a 3 px border. The state bar
under an air metric is a flat 8 px block. The alert page is a 10 px frame in the
alert's own color, inset in the pane. Icons come from the Nerd Font subset and
are the only curved forms on the page.

## Components

### Status Band
The device's own status line, and the one component every page shares.
- **Shape:** 56 px band, entries divided by 4 px black rules, closed by a 4 px
  rule under it.
- **Left cluster:** date, then the page entry, then a page-specific context
  entry that is the one allowed to give way (it is the flexible, clipping one).
- **Right cluster:** overdue flag, battery, clock. The clock and date carry no
  glyph; the value says what it is, and six glyph-led entries do not fit 800 px.
- **Color:** white with black type. The page entry is inverted black. Exactly
  two things may fill an entry: a state (overdue red, low battery yellow or red,
  rain blue, heat red) and the alert page's priority color on the page entry.

### Pane
- **Structure:** a 34 px title bar (glyph plus uppercase label) over a white
  reading field.
- **Title bar:** black at rest. It takes red or yellow only when the worst state
  under it says so; green demotes to black. The Brief page's headline is the one
  pane-like block with no title bar, because the headline is the page's voice.
- **Border:** none of its own; panes are separated by the 4 px rules of the grid.

### Chip
A bordered value tag for a state that belongs to a row.
- **Shape:** 3 px black border, square, padding 1px 8px 3px 8px, label type.
- **Variants:** neutral (white field, black type), alarm red, caution yellow,
  healthy green, weather blue. Used for due labels, sensor readings, device
  power state and device freshness.

### Block Meter
The battery, and the shape any 0 to 100 quantity takes when it is a level
rather than a number.
- **Shape:** ten 14 px squares, 3 px border, 2 px gaps. One cell per ten percent.
- **Fill:** the state color for the level (green healthy, yellow low, red flat).
  An unknown quantity is ten empty cells, never a guessed fill.
- **Never:** a thin progress bar, a ring, or a partially filled cell.

### State Bar
An 8 px solid block under an air-quality metric, filled with that metric's state
color. It carries the qualitative word that would not fit the cell.

### Window List
The device's navigation, matching the physical left and right buttons.
- **Shape:** 44 px foot bar over a 4 px rule, tabs padded 0 7px with an 8 px gap.
- **States:** the current page is inverted black, the same block the status band
  uses. A page that wants attention gets a tmux flag ("!") set in the tab's own
  weight and color, offset by a 5 px gap.
- **Right end:** the Wi-Fi glyph and RSSI, with no unit, because the glyph
  already says what the number is.
- **Flag discipline:** something must be late, down or stale. A flag on
  everything is a flag on nothing.

### Alert Frame
The one page that is not a dashboard.
- **Shape:** the pane's title bar in the alert's priority color, then a 10 px
  frame of the same color around a centered white field.
- **Type:** title at 72/900 clamped to two lines, message at 30/600 clamped to
  two, time at 48/900.
- **Color:** red for critical and doorbell, yellow for important, blue for
  normal. The status band's page entry takes the same color; it is the only page
  that fills it.

### Desk Chart
The 24 hour temperature and humidity trace, inline SVG at 344x170.
- **Strokes:** 4 px red for temperature, 4 px blue for humidity, 3 px black
  L-shaped axis. No gridlines, no fill, no legend box.
- **Labels:** the two ranges at 20 px in their series color, three interior time
  ticks plus NOW at 20 px black.
- **Honesty:** a series breaks into separate polylines wherever a reading is
  missing, autoscaling is floored at a 2 C / 6 percent span so a flat day is not
  amplified, and fewer than two readings prints a note instead of a plot.

## Do's and Don'ts

### Do:
- **Do** keep chrome neutral and spend color only where a field reports a state
  (The State-Or-Nothing Rule).
- **Do** set every division as a 4 px black rule, component borders at 3 px, and
  nothing below 3 px (The Thick-Rule Rule).
- **Do** hold the 20 px floor everywhere, including SVG text, and set anything
  at the floor in uppercase at 800 to 900 (The Twenty-Pixel Floor Rule).
- **Do** set reading rows at 24 px / 700 with 1.3 line height.
- **Do** give a pane title bar the worst state under it, and let green demote to
  black (The Worst-First Rule, The Quiet-Green Rule).
- **Do** print an unknown as "--", "unknown" or an empty meter, and let a broken
  adapter say so in its pane rather than showing a plausible number.
- **Do** decide truncation in the view layer and clip or two-line clamp in the
  template.
- **Do** take icons only from the bundled Nerd Font subset, and check the glyph
  survives quantization at its size before using it.

### Don't:
- **Don't** introduce a seventh color, a gray, a tint, an opacity or a gradient.
- **Don't** add a shadow, a lift, a card layer or a rounded corner; emphasis is
  inversion or a heavier rule (The Flat-Paper Rule).
- **Don't** color a title bar green, or color a healthy percentage at all.
- **Don't** use a Unicode symbol, an emoji or a text character as a stand-in for
  an icon the subset does not contain.
- **Don't** write hover, focus, transition, animation or a media query; the
  surface has one state and one size.
- **Don't** shrink type to make a label fit. Shorten the word or let the glyph
  carry it.
- **Don't** put an eyebrow, kicker or repeated product name above a heading; the
  bar above a title carries a source or a state, or it carries nothing.
- **Don't** flag a page in the window list for anything less than late, down or
  stale.
