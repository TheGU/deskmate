# Architecture

deskmate turns a Seeed reTerminal E1002 (ESP32-S3, 800x480 six-color
Spectra 6 e-paper) into a thin desk display. All logic lives in a server
called **dashboard-hub**; the device only downloads PNGs and shows them.

```
Data sources (fixtures, Obsidian vault, Open-Meteo, brief files, HA)
        |
        v
dashboard-hub (FastAPI, Python 3.12, Docker)
   adapters -> normalized models -> Jinja2 HTML -> Playwright/Chromium
   -> Pillow quantize to 6-color palette -> PNG 800x480
        |
        | HTTP on the LAN (no auth, no secrets in URLs)
        v
reTerminal E1002 running ESPHome
   http_request + online_image (PNG, RGB565 in PSRAM)
   epaper_spi model Seeed-reTerminal-E1002, update_interval: never
   buttons: prev / next / refresh, long-press refresh -> Today
   ESPHome API action show_alert(duration, beep) for HA alerts
```

## Repository layout

| Path | Purpose |
| --- | --- |
| `dashboard/` | dashboard-hub server (own `pyproject.toml`, Dockerfile, tests) |
| `firmware/` | ESPHome YAML for the E1002 and secrets example |
| `fixtures/` | Demo data used when no integration is enabled |
| `data/` | Runtime data written by other agents (brief, ai-usage). Gitignored |
| `docs/` | Architecture, flashing, restore, data sources |
| `scripts/` | Backup, verify, render helpers |
| `private-backups/` | Factory flash dumps. Gitignored, sensitive |
| `output/` | Generated example PNGs. Gitignored |

Root `pyproject.toml` only holds hardware tooling (esptool, esphome).

## dashboard-hub

### Endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/healthz` | open | `{"status":"ok"}` plus adapter status |
| GET, POST | `/setup` | open | Claim an unconfigured hub; see Auth below |
| GET | `/api/hub` | open | Hub name, base URL, configured, sources |
| GET | `/api/state` | open | Normalized state JSON that pages render from |
| GET | `/display/{page}.png` | open | `page` is one of the current pages (see README.md's Pages line) |
| GET | `/preview` | open | Browser page: switch between pages, shows PNG and HTML |
| GET | `/preview/{page}.html` | open | Raw HTML at 800x480, for CSS work in a browser |
| GET | `/preview/{page}-rgb.png` | open | RGB stage before quantization, no cache, dev only |
| POST | `/api/ai-usage` | token | Push AI quota; see docs/DATA-SOURCES.md |
| POST | `/api/brief` | token | Push the AI-written brief |
| POST | `/api/tasks` | token | Push the open task list (replaces it) |
| POST | `/api/alert` | token | Set the current alert `{title, message, priority}` |
| DELETE | `/api/alert` | token | Clear the current alert |
| POST | `/api/device/telemetry` | open | The device posts one sample every 5 min |
| GET | `/api/device/telemetry` | open | Latest sample plus sample count, oldest, newest |
| GET | `/api/device/history` | open | `?hours=` window of stored samples, downsampled |

`open` endpoints exist because the device fetches pages with no token; that
also means panel content (including anything pushed), and up to 30 days of
room climate and device battery history, are readable by anyone who can
reach the hub. Deploy on a LAN or behind a reverse proxy with its own
access control; see docs/DEPLOY.md.

`/display/{page}.png`:

- Renders on demand. Result is cached in memory keyed by page and a
  content hash of the state that page uses, with a page-specific TTL.
- Query `?t=<anything>` bypasses the server-side cache (cache busting
  used by the physical Refresh button). Any other query is ignored.
- Response headers: `ETag` = sha256 of PNG bytes, `Cache-Control:
  no-cache`. If the request carries a matching `If-None-Match`, reply
  `304 Not Modified` so ESPHome can skip the e-paper refresh.

### Auth

`POST /setup` claims an unconfigured hub: hub name, a public base URL
(`http` or `https`, a host, a port 1 to 65535, no path, query or
credentials), and the claim code the process logged at startup. A hub with
no `data/hub.json` yet generates an 8-character code
(`XXXX-XXXX`), logs it once, and keeps it only in memory until claimed;
`hub.json` holds `{schema, name, base_url, token_sha256, created_at}`, never
the plaintext token. `GET /setup` shows the claim form when unconfigured, a
short status page once claimed; `POST /setup` on an already-claimed hub is
`409`. There is no edit or regenerate mode: recovery is stopping the
container, deleting `data/hub.json`, and starting again (a new claim code is
logged).

Every token-protected route requires `Authorization: Bearer <token>`
(`Bearer` is case-insensitive), checked by hashing and
`hmac.compare_digest` against `token_sha256`. One error shape for the whole
API: a JSON body with `detail` (a string, or a list of validation problems
for a `422`).

| Status | Meaning |
| --- | --- |
| 401 | Missing or wrong bearer token |
| 403 | Wrong claim code on `POST /setup` |
| 409 | `POST /setup` on an already-claimed hub |
| 422 | Body rejected: unknown field, bad `schema_version`, a length or count cap, a duplicate task id, a naive datetime, or an invalid base URL |
| 503 | Hub not set up yet, or `data/hub.json` exists but is unreadable (the detail names the path) |

`POST /api/device/telemetry` stays open: the E1002 firmware does not send a
token today, though it already sends `request_headers` on that request
(`firmware/e1002.yaml`), so adding one is a small, tracked firmware
follow-up, not shipped here.

### Rendering pipeline

1. `adapters/*` produce typed models (`models.py`). Every adapter is
   wrapped: on exception the state carries `status="error"` and the
   previous or empty value. The page still renders with an "unavailable"
   marker. Never fabricate data.
2. `renderer/render.py` renders a Jinja2 template to HTML, screenshots it
   with Playwright Chromium at 4x (3200x1920), downsamples with Lanczos to
   800x480, then `renderer/palette.py` quantizes to the nearest of the six
   panel colors with `Image.quantize(palette=..., dither=NONE)` and saves an
   8-bit RGB, non-interlaced PNG (ESPHome's PNG decoder needs
   non-interlaced). A panel test on 2026-09-05 found 4x plus Lanczos sharper
   than rendering at 1x, and found dithering (Floyd-Steinberg, Atkinson,
   Bayer 8x8, edge-only) spiky on e-paper and rejected it. The PNG must
   already be six-ink: the ESPHome `epaper_spi` driver thresholds every
   pixel itself and cannot show anti-aliasing.
3. Determinism: bundled fonts only, no system fonts, no animations, no
   timestamps other than the state `updated_at`. `view.py` turns the state
   into a flat context and `icons.py` chooses every glyph, so the templates
   hold no logic and no codepoints.

Fonts live in `dashboard/app/static/fonts/` and are embedded as base64 data
URIs by `render.py` `font_css` (the developer preview serves the same files
from `/static/fonts/` instead). `FONT_FACES` carries a `font-weight` range
descriptor per face, because Google Sans is a variable font:

| File | Family | `font-weight` | Use |
| --- | --- | --- | --- |
| `GoogleSans-LatinThai-var.ttf` | Google Sans | `400 700` | all text |
| `SymbolsNerdFontMono-Subset.ttf` | Symbols Nerd Font Mono | `400` | icons |

The stack is `'Google Sans', 'Symbols Nerd Font Mono'`. Google Sans (SIL
OFL) carries Latin and Thai in one file, so no separate Thai fallback face
is needed; Google Sans Flex and Noto Sans Thai were replaced on 2026-09-05
after the owner asked for one font. The Nerd Font is a
subset: only the codepoints named in `app/icons.py` exist in it, and a test
reads the font's cmap to prove it. Chromium runs with
`--font-render-hinting=full` and the pages set no `text-rendering` or
`-webkit-font-smoothing` override, because the panel is 1-bit after
quantization and an unhinted stem lands as a smear of half-tones.

### Page design: Braun Panel

See `DESIGN.md` for the full recorded system.

The panel's pages share one frame, defined in `templates/base.html`: an 800x480
still in six pure colors, no dithering, no motion, no hover, no reflow.

**Color reports state, it never decorates.** A field takes a color only when
it carries a state: blue for rain or a calendar's identity, red for overdue,
urgent, down or dangerous heat, yellow for due today, warn, stale or low,
green for a confirmed-healthy reading. The one exception is the alert page's
top band, which is itself the state and is allowed to fill the whole region.
`view.py` decides every accent; the templates only print the class name.

- A 64 px header (`.hdr`), closed off by a 2 px rule: on the left the day
  numeral (56 px) and a weekday/month stack, a vertical rule, then the
  weather reading (a 40 px temperature, its icon, and a state line that
  carries a tell-tale dot when it has one, or a hatch box when the reading is
  unavailable); on the right an overdue chip (red, shown only on pages that
  do not already list the task), the Wi-Fi glyph, the battery reading (a red
  or yellow chip only below 20 percent, otherwise plain), and the clock.
- A 372 px body, closed off from the header and footer by 2 px rules and
  split internally by 2 px vertical and horizontal rules, never a boxed
  tile. Today, Agenda, Weather and Brief give the left column a fixed 456 px
  (the 456 Rule); System uses its own row and column widths instead. The
  grammar throughout is a numeral over its own small-caps label; a missing
  value is a hatch box, never a guess.
- Color inside the body is carried only by tell-tale dots (10 px filled
  circles that precede a word or reading to flag a state) and filled state
  chips (red, yellow, green or blue; `chip-plain` is the unfilled default for
  a value with nothing to report). Nothing else in the body is colored for
  its own sake.
- A 40 px footer window list: the five button-reachable pages as numbered
  entries, the current page inverted to a solid black block, and a page that
  wants attention marked with a trailing "!" rather than a color. A page's
  own name appears exactly once, here; nothing above the footer repeats it in
  a band, a title bar or a kicker.

Palette (server and device agree on pure primaries; the ESPHome
epaper_spi driver maps RGB to the nearest of the six panel colors):

| Name | RGB | Semantic |
| --- | --- | --- |
| white | 255,255,255 | background |
| black | 0,0,0 | text, structure |
| red | 255,0,0 | overdue, urgent, alert |
| yellow | 255,255,0 | warning |
| green | 0,255,0 | complete, healthy |
| blue | 0,0,255 | calendar, information accents |

### Adapters and configuration

Configuration is environment variables (`config.py`, pydantic-settings).
Each adapter has a `*_SOURCE` selector; `fixture` is always available.

| Adapter | Sources | Default | Config |
| --- | --- | --- | --- |
| tasks | fixture, file, obsidian, auto | auto | `TASKS_SOURCE`, `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TASK_GLOB` |
| calendar | fixture, ics | fixture | `CALENDAR_SOURCE`, `CALENDAR_ICS_URLS` |
| weather | fixture, open_meteo | fixture | `WEATHER_SOURCE`, `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, `WEATHER_LOCATION_NAME` |
| ai_usage | fixture, file, auto | auto | `AI_USAGE_SOURCE`, `AI_USAGE_PATH` (default `data/ai-usage.json`) |
| ai_brief | fixture, file, auto | auto | `BRIEF_SOURCE`, `BRIEF_DIR` (default `data/brief`) |
| home_assistant | fixture, rest | fixture | `HA_SOURCE`, `HA_URL`, `HA_TOKEN`, `HA_ENTITIES` (JSON) |

`auto` (tasks, ai_usage, ai_brief): the file adapter when its file exists and
is readable for the current state (for ai_brief, "readable" means either
`current.json` or the current brief mode's own Markdown file), otherwise
`fixture`. `fixture` and `file` keep their strict, non-auto meanings; `tasks`
alone also accepts `obsidian`, which `auto` never selects on its own.

`GET /api/hub`'s `sources.<dataset>.effective` and the footer's DEMO mark
report two different moments, not the same fact twice:

- `effective` is a live, pure check of what the *next* render will use,
  evaluated fresh on every `GET /api/hub` call. It shows a push immediately,
  before anything has re-rendered.
- DEMO tracks what the *last* render actually drew (the adapter's last real
  fetch), so it only catches up once the panel's own page cache expires (its
  TTL) or a push invalidates it. This is deliberate: DEMO must match the
  pixels currently on screen, not what will be there next time.

DEMO covers exactly the three datasets a remote agent pushes (ai_usage,
brief, tasks), and only on the Today and Brief pages, the only pages that
draw them: when one of a page's own pushed datasets is fixture-sourced, that
page's footer prints `DEMO` so a fresh install never passes demo numbers off
as real. Weather, calendar, home and device keep their own established
fixture-fallback story from earlier phases and never print DEMO: the hub
fetches them itself, or (device) the E1002 firmware pushes them, so there is
nothing there for a remote agent's push to represent. See "Staleness" below.

Global: `TIMEZONE` (default `Asia/Bangkok`), `UNITS` (`metric`).

Obsidian access is read only. The vault is mounted read-only in Docker.

### Staleness

`AI_USAGE_STALE_SECONDS` (default 21600), `BRIEF_STALE_SECONDS` and
`TASKS_STALE_SECONDS` (default 36000 each) bound how old a pushed dataset's
own age can get before the panel marks it: `view.py`'s `stale_info` compares
its reference time, which is the state's own `updated_at` converted to
`TIMEZONE` (not the wall-clock instant the comparison runs; `updated_at` is
the newest adapter timestamp in that state snapshot, so a cached, not yet
re-fetched page compares against the moment it was built, not against now)
against `AIUsage.collected_at` (the oldest provider), `Brief.generated_at`
or `TasksBlock.received_at`, and returns an hour-bucketed age label ("6 H
AGO") once the threshold is passed and the age is at least an hour; under an
hour is never marked, and a fixture-sourced block is never marked regardless
of age. This is computed in `view.py` only, never serialized onto a model,
so the state fingerprint and the device's `304` path do not move because of
it; the mark can therefore lag a push by up to one page's own cache TTL.

Grammar: a yellow tell-tale before the section label and the age in 16 px
caps after it (Today: AI CAPACITY, PRIORITIES; Brief: the mode label line),
exactly the System page's stale BATTERY pattern, plus a `!` on the page's
footer entry through the existing footer-flag logic.

### Alerts

- `POST /api/alert` and `DELETE /api/alert` require the bearer token (see
  Auth above). `POST /api/alert` stores the current alert (in memory plus
  `data/alert.json`). `/display/alert.png` renders it. Priority order:
  `critical > doorbell > important > normal`; a lower priority does not
  replace a higher one that is still active.
- The device is told to show it through its ESPHome API action
  `show_alert` (duration seconds, beep bool). Home Assistant calls the
  hub first, then the device action. See `docs/DATA-SOURCES.md`.
- The device restores the previous page after the duration.

## Device (ESPHome)

- Framework esp-idf, PSRAM octal. Flash layout 16MB (chip has 32MB, but ESP-IDF needs an experimental flag for 32MB with OTA).
- No business logic. Only: page list, current index, download, display,
  buttons, buzzer, sensors, alert timer.
- Page URL base comes from `secrets.yaml` (`hub_base_url`), so no server
  address is hard-coded in the YAML.
- Refresh policy on the device: a periodic timer (default 30 min)
  re-requests the current page URL; ESPHome sends the conditional request
  and only refreshes the panel when the image changed. Server-side TTLs
  per page enforce the proposal's cadence (today/agenda 30 min, weather
  60 min, system 15 min, brief when file changes).
- Any button press causes exactly one panel refresh, and only after the
  download succeeded. On download failure the old image stays on screen
  and a short low beep sounds.
- Refreshes are queued behind the panel driver: a Spectra 6 cycle takes
  about 32 s and the driver drops updates while busy, so the firmware waits
  for the driver to be idle before refreshing.
- Telemetry: every 5 min (or once per wake on battery) the device POSTs
  battery, temperature, humidity, Wi-Fi RSSI, uptime, page, power mode,
  USB presence and charge state to `/api/device/telemetry`.

### Power modes

- Always-on (USB): the behaviour above. Alerts, OTA and the API work.
- Battery mode: the device deep-sleeps and wakes at the local hours in
  `wake_hours` (default 08:00, 12:00, 17:00) or on any button. Each wake:
  Wi-Fi, fetch page, one refresh, one telemetry POST, then sleep after a
  45 s grace window (hard deadline 150 s, and 25 s if Wi-Fi never joins).
  Buttons wake the device and act (left previous, right next, green
  refresh). Alerts and OTA only work while awake.
- Auto power mode (default on): the SY6974B charger's status register on
  I2C1 (GPIO39/40, address 0x6B) is polled every 10 s while awake and once
  on every wake. USB removed switches to battery mode; USB found on a wake
  switches back to always-on. Overrides: the "Battery mode" and "Auto power
  mode" switches, and a left-button long press (toggles and pins the mode).
- Sleep housekeeping: button pads get RTC pull-ups (ext1 ANY_LOW wake),
  the battery divider enable and LED are pinned off, the buzzer gate is
  held low, and the panel is never put to sleep mid-cycle.
