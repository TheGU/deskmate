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

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/healthz` | `{"status":"ok"}` plus adapter status |
| GET | `/api/state` | Normalized state JSON that pages render from |
| GET | `/display/{page}.png` | page in `today agenda weather brief system alert` |
| GET | `/preview` | Browser page: switch between pages, shows PNG and HTML |
| GET | `/preview/{page}.html` | Raw HTML at 800x480, for CSS work in a browser |
| GET | `/preview/{page}-rgb.png` | RGB stage before quantization, no cache, dev only |
| POST | `/api/alert` | Set the current alert `{title, message, priority}` |
| DELETE | `/api/alert` | Clear the current alert |

`/display/{page}.png`:

- Renders on demand. Result is cached in memory keyed by page and a
  content hash of the state that page uses, with a page-specific TTL.
- Query `?t=<anything>` bypasses the server-side cache (cache busting
  used by the physical Refresh button). Any other query is ignored.
- Response headers: `ETag` = sha256 of PNG bytes, `Cache-Control:
  no-cache`. If the request carries a matching `If-None-Match`, reply
  `304 Not Modified` so ESPHome can skip the e-paper refresh.

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
descriptor per face, because the first two are variable fonts:

| File | Family | `font-weight` | Use |
| --- | --- | --- | --- |
| `GoogleSansFlex-wght.ttf` | Google Sans Flex | `300 1000` | all text |
| `NotoSansThai-wdth-wght.ttf` | Noto Sans Thai | `100 900` | Thai fallback |
| `SymbolsNerdFontMono-Subset.ttf` | Symbols Nerd Font Mono | `400` | icons |

The stack is `'Google Sans Flex', 'Noto Sans Thai', sans-serif`, so Thai
codepoints fall through to Noto Sans Thai per glyph. The Nerd Font is a
subset: only the codepoints named in `app/icons.py` exist in it, and a test
reads the font's cmap to prove it. Chromium runs with
`--font-render-hinting=full` and the pages set no `text-rendering` or
`-webkit-font-smoothing` override, because the panel is 1-bit after
quantization and an unhinted stem lands as a smear of half-tones.

### Page design: Status Line

The six pages share one frame, described in full in the direction contract at
the top of `templates/base.html`. It reads as a terminal status line on paper:

**Color reports state, it never decorates.** The chrome is neutral: a white
status band with black type, black pane title bars, 4 px black rules. A field
takes a color only when it carries a state, and the color is that state's
meaning: blue for rain or a calendar, red for overdue, urgent, down or heat,
yellow for caution, due today, warn or stale, green for healthy or charged.
Healthy is the quiet default, so a page with nothing to report has almost no
color on it. `view.py` decides every one of those accents; the templates only
print the class name.

- A 56 px top status band, white, with entries divided by 4 px black vertical
  rules. Left: date, the page name inverted (the same black block the window
  list uses), then a page-specific context entry. Right: overdue count (red,
  it is a state), device battery (filled only when yellow or red), updated
  time. The date and the clock carry no glyph: six glyph-led entries do not
  fit an 800 px band and those two values name themselves.
- A 380 px body of panes split by 4 px black rules. Every pane has a 34 px
  title bar carrying a glyph and an uppercase title. The bar is black unless
  the pane's own state is the message: weather NOW red in a heat wave, RAIN
  blue when rain today is 50 percent or more, AIR red or yellow with the air,
  agenda OVERDUE red (green when there is nothing overdue), system DESK yellow
  when the device is stale, HOME and SERVICES red or yellow with the worst
  thing under them, brief risk sections red when they have items.
- Calendars are told apart by color, not by a label. `Event.calendar` names
  the feed, `CALENDAR_NAMES` and `CALENDAR_COLORS` map feeds to panel colors
  (see docs/DATA-SOURCES.md), and the agenda's context entry is the legend.
- A 44 px window list bar at the foot: the five button-reachable pages as
  numbered entries with the active one inverted, plus the device Wi-Fi RSSI.
  A page that wants attention carries a `!` in its entry, tmux style, and the
  bar is deliberately hard to set: AGENDA when a task is overdue, SYSTEM when
  a service is down or the device has gone stale, WEATHER when a UV, PM2.5 or
  AQI reading is in the red. Rain never raises a flag because the status band
  already carries it on every page.
- Type floors, because the panel is 1 bit per color at 125 ppi and stair-step
  edges scale with the ratio of pixel size to stroke width: row text 24 px
  weight 700, labels and chips 20 px weight 900, and nothing anywhere below
  20 px. Copy is re-fitted by shortening a label or dropping a row, never by
  shrinking type.
- Quantities that are not a single number are block meters of ten bordered
  cells, filled solid in the semantic color. Never a thin bar, never a ring.

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

| Adapter | Sources | Config |
| --- | --- | --- |
| tasks | fixture, obsidian | `TASKS_SOURCE`, `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TASK_GLOB` |
| calendar | fixture, ics | `CALENDAR_SOURCE`, `CALENDAR_ICS_URLS` |
| weather | fixture, open_meteo | `WEATHER_SOURCE`, `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, `WEATHER_LOCATION_NAME` |
| ai_usage | fixture, file | `AI_USAGE_SOURCE`, `AI_USAGE_PATH` (default `data/ai-usage.json`) |
| ai_brief | fixture, file | `BRIEF_SOURCE`, `BRIEF_DIR` (default `data/brief`) |
| home_assistant | fixture, rest | `HA_SOURCE`, `HA_URL`, `HA_TOKEN`, `HA_ENTITIES` (JSON) |

Global: `TIMEZONE` (default `Asia/Bangkok`), `UNITS` (`metric`).

Obsidian access is read only. The vault is mounted read-only in Docker.

### Alerts

- `POST /api/alert` stores the current alert (in memory plus
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
