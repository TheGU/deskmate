# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One person: the owner, a developer working at a desk in Bangkok (timezone
Asia/Bangkok). The reTerminal E1002 sits on that desk next to the monitor and
is read at arm's length, roughly 40 to 70 cm, while the owner is working on the
PC. Nobody else reads it. The owner reads Thai and English and their task
names, calendar events and briefs mix both scripts.

The job: a second, quick-glance paper. Answer "what matters now" (top
priorities, next event, remaining AI capacity, weather, home and device state)
without switching windows on the PC.

## Product Purpose

deskmate is a local-first desk dashboard. A server called dashboard-hub (the
brain) gathers data, renders each page as an exact 800x480 six-color PNG, and
serves it over the LAN. The E1002 (the paper) runs ESPHome as a thin display
client: it fetches the PNG, shows it, and reports its own telemetry. Success is
the owner getting the day's actionable picture in one glance, from paper, with
no logic and no credentials on the device.

## Positioning

- The device is deliberately dumb. Every integration, parser, credential and
  render decision lives on the hub, so the display can be replaced, moved or
  re-flashed without losing anything.
- It reads the owner's own working context: an Obsidian vault, an ICS calendar,
  Home Assistant, and data only the owner's PC can produce (AI session quota,
  AI-written briefs). A stock SenseCraft dashboard cannot see any of that.
- It is built for six-color e-paper, not adapted from an LCD design, and it can
  leave the desk: on battery it wakes on a fixed schedule and on button press.

## Operating Context

- Hardware: Seeed reTerminal E1002, ESP32-S3, 800x480 Spectra E6 e-paper
  (white, black, red, yellow, green, blue), three buttons (left, right, green),
  buzzer, built-in battery, SHT4x temperature and humidity sensor, SY6974B
  charger. A full panel refresh takes about 32 s.
- Power: normally USB-C. Unplugging switches the device to battery mode, in
  which it sleeps and wakes at 08:00, 12:00 and 17:00 or on a button press.
  Plugging USB back in returns it to always-on on the next wake. A long press
  on the left button pins the mode manually.
- Buttons: left = previous page, right = next page, green tap = refresh, green
  hold = back to Today. Page order: Today, Agenda, Weather, Brief, System. The
  Alert page is shown on demand and the previous page is restored after it.
- Hub: Docker container on the owner's Windows PC today (published on port
  18080 because 8080 is taken), planned to move to an unraid homelab server.
- Sources that will be live on the desk: Obsidian vault tasks (read only), an
  ICS calendar feed, Open-Meteo weather, Home Assistant (state on the System
  page, alerts pushed from HA automations to the hub's alert API), and device
  telemetry pushed by the E1002 itself.
- PC-side feed: some data cannot be produced by the hub because it needs the
  owner's context or credentials. Examples the owner named: an AI summary from
  a scheduled or looped AI session, remaining AI license or quota from a live
  Claude Code session, a calendar summary pushed by a local script or by that
  same scheduled AI session. Today the hub reads these from files under the
  data directory (ai-usage.json, brief/). The push mechanism beyond that is
  undecided (see Capabilities and Constraints).

## Capabilities and Constraints

Confirmed:

- Pages: today, agenda, weather, brief, system, alert. Rendered by Jinja2 and
  headless Chromium at 4x, downsampled, then quantized by Pillow to the six
  panel colors with no dithering (dithering was tested on the panel on
  2026-09-05 and rejected). Every image is exactly 800x480 and tests enforce
  size, PNG validity and palette.
- Rendering is deterministic: bundled fonts only, no network at render time, no
  clock that changes every minute (a small "Updated HH:MM" instead).
- No partial refresh, no animation, no gradients, shadows, grays or tiny text.
  The panel needs a full 32 s cycle per change, so a page is a still.
- Data is never invented. An adapter that is unset or failing makes the page
  say "unknown" or "unavailable".
- Hub endpoints: healthz, api/state, display/{page}.png with ETag and 304,
  preview pages, alert set and clear, device telemetry in and history out.
  Telemetry (battery, temperature, humidity, Wi-Fi, power state, wake cause)
  is stored in SQLite with 30-day retention and charted on the System page.
- Text: Thai and English mixed content is a requirement. Google Sans Flex
  carries Latin; Noto Sans Thai is bundled as the per-glyph fallback for Thai.
  Thai wrapping with real Thai content has not been checked on the panel yet.
- Terminology: hub (server), device or paper (E1002), page, adapter, source
  (fixture or live), fixture, battery mode, always-on, DESK panel (the device
  section of the System page), alert.

Undecided:

- How the PC-side feed reaches the hub: files dropped into the mounted data
  directory, an HTTP push endpoint, or a scheduled AI session that does both.
  The owner is not sure yet; do not build on an assumed mechanism.
- Which AI products appear in the AI capacity view and where those numbers come
  from. The fixture shows Claude and ChatGPT/Codex with 5-hour and 7-day
  windows; treat that as illustration, not confirmed data.
- When the hub moves to unraid.

## Brand Commitments

- Name: deskmate. Device name on the network: reterminal-e1002.
- Voice: terse and factual. Labels are short uppercase section names; body
  text states facts and times. No exclamation, no filler, no invented
  reassurance.
- Fonts, pinned by the owner on 2026-09-05: Google Sans Flex (SIL OFL) for all
  text, and Nerd Font symbols for icons, so one glyph can replace a label and
  save space. Verified fact: the open-source Google Sans Flex (v4.007 release
  and the Google Fonts hosted build) carries Latin only, no Thai glyphs, so
  Thai falls through to Noto Sans Thai (SIL OFL) in the font stack.
- Color: the spec's white-ground, color-as-accent rule was lifted by the owner
  on 2026-09-05. Color may own whole regions (solid panel fields with white or
  black type). The panel's six colors are still the only colors.
- The owner found the first design too plain. What would feel wrong: anything
  that looks like a phone widget (rounded app cards, app-store gloss, generic
  dashboard tiles). Nothing about page set, section contents or color meaning
  is locked; regrouping is allowed when it improves the glance.
- Earlier spec preferences that still hold as product truth: strong
  typography, large numbers, clear hierarchy, no gradients, shadows, grays,
  animation or tiny text.

## Evidence on Hand

- Demo data: fixtures/*.json for every adapter and the device. Example feed
  files: data-examples/ (ai-usage.json, brief/current.json, morning.md,
  evening.md).
- Real device telemetry accumulating in data/telemetry.sqlite from the
  physical E1002 (battery, temperature, humidity, Wi-Fi, power state).
- Rendered example PNGs in output/ (gitignored, regenerate with
  scripts/render-all.py).
- Hardware verification log in docs/FLASHING.md and docs/ARCHITECTURE.md.
- Absent, never fabricate: real tasks, events, briefs or AI quota numbers (all
  live sources are still set to fixture), testimonials, other users, any
  product comparison numbers.

## Product Principles

1. Glance beats browse. Each page answers one question in a few seconds at
   arm's length; the top three items matter more than the complete list.
2. The hub is the brain, the device is paper. Logic, credentials and
   integrations never move onto the ESP32.
3. Honest data only. Unknown is shown as unknown; nothing is estimated or
   filled in to look complete.
4. The panel's limits are the design. Six flat colors, a still image, a full
   refresh per change. Anything that needs motion, gray or fine detail does
   not belong.
5. Local and owner-controlled. Everything runs on the LAN; the owner's PC and
   the hub hold the context, and the device can leave the desk on battery.

## Accessibility & Inclusion

- Legibility at 40 to 70 cm on reflective e-paper: high-contrast black on
  white, no gray text, no thin type.
- Thai script must render correctly (glyphs, combining marks, wrapping), see
  Capabilities and Constraints.
