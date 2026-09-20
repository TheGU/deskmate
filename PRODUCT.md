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
serves it over the LAN to any ESPHome device that can fetch that PNG and post
telemetry back. This owner's paper is a Seeed reTerminal E1002, running
ESPHome as a thin display client: it fetches the PNG, shows it, and reports
its own telemetry. Success is the owner getting the day's actionable picture
in one glance, from paper, with no logic and no credentials on the device.

## Positioning

- The device is deliberately dumb. Every integration, parser, credential and
  render decision lives on the hub, so the display can be replaced, moved or
  re-flashed without losing anything.
- It reads the owner's own working context: tasks a local agent pushes, an
  ICS calendar, Home Assistant, and data only the owner's PC can produce (AI
  session quota, AI-written briefs). A stock SenseCraft dashboard cannot see
  any of that.
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
- Hub: runs as a Docker container on a home server.
- Sources that will be live on the desk: tasks pushed by a local agent, an
  ICS calendar feed, Open-Meteo weather, Home Assistant (state on the System
  page, alerts pushed from HA automations to the hub's alert API), and device
  telemetry pushed by the E1002 itself.
- PC-side feed: some data cannot be produced by the hub because it needs the
  owner's context or credentials. Examples the owner named: an AI summary from
  a scheduled or looped AI session, remaining AI license or quota from a live
  Claude Code session, an open task list a local agent keeps. The hub accepts
  these over three bearer-token HTTP push endpoints
  (`POST /api/ai-usage`, `/api/brief`, `/api/tasks`; see `docs/DATA-SOURCES.md`
  and `skills/deskmate/SKILL.md`).

## Capabilities and Constraints

Confirmed:

- Pages: today, agenda, weather, brief, system, alert, each a module
  (`app/modules/<id>/`) that brings its own settings, adapter, template and
  fixture; a third party can add one without touching the hub's own code
  (see docs/MODULES.md). An optional Home Assistant dashboard module
  (`ha_dashboard`, off by default) screenshots a Lovelace view the owner
  built straight to the panel instead of rendering a template
  (docs/HA-DASHBOARD.md). Rendered by Jinja2 and headless Chromium at 4x,
  downsampled, then quantized by Pillow to the six panel colors with no
  dithering (dithering was tested on the panel on 2026-09-05 and rejected).
  Every image is exactly 800x480 and tests enforce size, PNG validity and
  palette.
- Rendering is deterministic: bundled fonts only, no network at render time, no
  clock that changes every minute (a small "Updated HH:MM" instead).
- No partial refresh, no animation, no gradients, shadows, grays or tiny text.
  The panel needs a full 32 s cycle per change, so a page is a still.
- Data is never invented. An adapter that is unset or failing makes the page
  say "unknown" or "unavailable".
- Hub endpoints: healthz, api/state, display/{page}.png (by page id or by
  its 0-based index) with ETag and 304, preview pages, setup and hub info,
  AI usage/brief/tasks push, alert set and clear, device telemetry in and
  history out, plus the settings page (`/settings`, admin only) for every
  section, the Modules section (enable, disable, order), backup, restore
  and rotate. `/setup` needs no token, only a caller on this hub's own
  loopback or private network, and is first come first served (no claim
  code). Reads need the bearer token, the device key, or a browser session
  from `/login`; writes (the three pushes, the alert endpoints and the
  settings routes) need the bearer token or an admin session only; device
  telemetry needs the device key or the token. One SQLite database
  (`data/deskmate.sqlite`) holds the hub's identity, every setting, pushed
  datasets and telemetry (battery, temperature, humidity, Wi-Fi, power
  state, wake cause; 30-day retention, charted on the System page). Each POST also records who sent it and which hub URL
  they used (remote address, Host header); the System page's HUB column
  shows those as DEVICE IP and HUB URL, next to an age for every pushed or
  fetched dataset and the device's own last sync. Both fields also reach
  `/api/state` (reader-authenticated) as `device.remote_addr` and
  `device.hub_host`, never any other API response.
- Text: Thai and English mixed content is a requirement. Google Sans (SIL
  OFL) carries Latin and Thai in one file, weights 400 to 700; Google Sans
  Flex and Noto Sans Thai were replaced on 2026-09-05 after the owner asked
  for one font. Thai wrapping with real Thai content has not been checked on
  the panel yet.
- Terminology: hub (server), device or paper (E1002), module (a page and the
  datasets behind it, packaged together), page, adapter, source (fixture or
  live), fixture, battery mode, always-on, DESK panel (the device section of
  the System page), HUB column (the System page's dataset ages and device
  origin), alert.

Decided: the PC-side feed reaches the hub over three bearer-token HTTP push
endpoints (`/api/ai-usage`, `/api/brief`, `/api/tasks`); see
`docs/DATA-SOURCES.md`.

Undecided:

- Which AI products appear in the AI capacity view and where those numbers come
  from. The fixture shows Claude and ChatGPT/Codex with 5-hour and 7-day
  windows; treat that as illustration, not confirmed data.
- When the hub moves to unraid.

## Brand Commitments

- Name: deskmate. Device name on the network: reterminal-e1002.
- Voice: terse and factual. Labels are short uppercase section names; body
  text states facts and times. No exclamation, no filler, no invented
  reassurance.
- Fonts, pinned by the owner on 2026-09-05: Google Sans (SIL OFL) for all
  text, and Nerd Font symbols for icons, so one glyph can replace a label and
  save space. Google Sans carries Latin and Thai in one file, weights 400 to
  700; Google Sans Flex and Noto Sans Thai were replaced on 2026-09-05 after
  the owner asked for one font.
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

- Demo data: each built-in module's own `app/modules/<id>/fixtures/*.json`
  for every adapter and the device.
- Real device telemetry accumulating in data/deskmate.sqlite from the
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
