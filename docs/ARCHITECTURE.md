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
| `data/` | The hub's own database, `deskmate.sqlite` (identity, settings, pushed datasets, telemetry). Gitignored |
| `docs/` | Architecture, flashing, restore, data sources |
| `scripts/` | Backup, verify, render helpers |
| `private-backups/` | Factory flash dumps. Gitignored, sensitive |
| `output/` | Generated example PNGs. Gitignored |

Root `pyproject.toml` only holds hardware tooling (esptool, esphome).

## dashboard-hub

### Endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/healthz` | open | Liveness only: each adapter's last known status, never fetches (`unknown` before the first render); always `200`; the full body only for an authenticated reader on a configured hub, a minimal `{status, version, renderer.connected}` body otherwise (unconfigured, or configured but unauthenticated) |
| GET, POST | `/setup` | open, address-guarded | Set up an unconfigured hub; see Auth below |
| GET, POST | `/setup/{step}` | admin | The setup wizard's optional steps (general, weather, calendar, home); see docs/SETTINGS.md |
| GET | `/login` | open | Browser sign-in form; see Auth below |
| GET | `/settings` | admin | Every settings section's form, then backup, restore and rotate; see docs/SETTINGS.md |
| GET | `/settings/geocode` | admin | The settings page with the weather section's location search results |
| POST | `/settings/{section}` | admin | Save one settings section, or "Save and test" it |
| POST | `/settings/backup` | admin | Download the whole database as one SQLite file |
| POST | `/settings/restore` | admin | Replace the database with an uploaded backup |
| POST | `/settings/rotate` | admin | Mint a fresh token, device key and session secret |
| GET | `/api/hub` | reader | Hub name, base URL, configured, sources |
| GET | `/api/state` | reader | Normalized state JSON that pages render from |
| GET | `/display/{page}.png` | reader | `page` is an enabled page's id, or its 0-based index in the window list |
| GET | `/preview` | reader | Browser page: switch between pages, shows PNG and HTML; an unauthenticated browser is redirected to `/login` |
| GET | `/preview/{page}.html` | reader | Raw HTML at 800x480, for CSS work in a browser; same redirect |
| POST | `/api/ai-usage` | token | Push AI quota; see docs/DATA-SOURCES.md |
| POST | `/api/brief` | token | Push the AI-written brief |
| POST | `/api/tasks` | token | Push the open task list (replaces it) |
| POST | `/api/alert` | token | Set the current alert `{title, message, priority}` |
| DELETE | `/api/alert` | token | Clear the current alert |
| POST | `/api/device/telemetry` | device | The device posts one sample every 5 min |
| GET | `/api/device/telemetry` | reader | Latest sample plus sample count, oldest, newest |
| GET | `/api/device/history` | reader | `?hours=` window of stored samples, downsampled |

Before the hub is set up, every `reader`, `admin`, `token` and `device`
route answers `503`: an unconfigured hub serves nothing but
`GET`/`POST /setup`, `GET /healthz` (minimal body), `/static`, `/docs` and
`/openapi.json`. `GET /` and the `reader`-`html` and `admin`-`html` routes
(`/preview`, `/preview/{page}.html`, `/settings*`, `/setup/{step}`)
redirect to `/setup` (`303`) instead of `503`. Set the hub up right after
the first start; see docs/DEPLOY.md. Once set up, `reader` routes accept
the bearer token, the device key, or a `/login` session cookie of either
role; `admin` routes accept only the bearer token or an admin session
cookie (never the device key, even as a cookie); the `device` route accepts
only the bearer token or device key (never the cookie); `token` routes
accept only the bearer token. See Auth below for the full rule and the two
secrets.

`/display/{page}.png`:

- `page` is either an enabled page's id (`today`, `alert`) or an integer,
  which is that page's 0-based position in the window list. The index is
  resolved to the id before anything else happens, so the cache key, the
  `ETag` and the `X-Deskmate-Page` header are the id either way, and an
  index past the last enabled page is `404`. `alert` is never an index.
- Renders on demand. Result is cached in memory keyed by page and a
  content hash of the state that page uses, with a page-specific TTL.
- Query `?t=<anything>` bypasses the server-side cache (cache busting
  used by the physical Refresh button). Any other query is ignored.
- Response headers: `ETag` = sha256 of PNG bytes, `Cache-Control:
  no-cache`. If the request carries a matching `If-None-Match`, reply
  `304 Not Modified` so ESPHome can skip the e-paper refresh.

### Auth

`POST /setup` sets up an unconfigured hub: hub name and a public base URL
(`http` or `https`, a host, a port 1 to 65535, no path, query or
credentials). There is no claim code: the first submission to reach an
unconfigured hub wins, first come first served. The only guard is the
caller's address - `request.client.host` must be loopback, RFC1918/ULA
private, or link-local (`ipaddress.ip_address(...).is_loopback` /
`.is_private` / `.is_link_local`); a host that fails to parse as an IP
address at all is refused too (fail closed), and only a missing client
(`request.client is None`, e.g. a unix socket) is let through unchecked.
Anything the check refuses is `403` before the form is even read. This is a
network-layer check on the immediate TCP peer, so it only works when the
hub is reached directly: behind a reverse proxy, `request.client.host` is
the proxy's own address, not the original caller's, and the guard always
passes - see "Reverse proxy" in `docs/DEPLOY.md`. Setup generates two
secrets, each shown once on the setup-done page and never displayable
again:

- **The bearer token**, for agents. Read and write: accepted by every
  `token` route and every `reader` route.
- **The device key**, for the E1002 firmware. Read only: accepted by
  every `reader` route and by the `device` route
  (`POST /api/device/telemetry`), never by a `token` route. The device key
  sits in unencrypted ESP32 flash, so it is treated as a read credential of
  the same power as a browser login, which is why it is never accepted for
  a write.

The hub's identity is the single row of the `hub` table in
`data/deskmate.sqlite` (`app/db.py`, `app/hub_config.py`):
`{name, base_url, token_sha256, device_key_sha256, session_secret,
created_at}`, never a plaintext secret. A row that exists but cannot be
trusted (a missing value, an unparseable `created_at`) makes the hub answer
`503` on every route until the database is fixed or reset. `GET /setup`
shows the setup form when unconfigured, a bare "already set up" page with a
link to `/login` once configured - it never reveals the name, base URL or
creation time to an anonymous caller; `POST /setup` on an already-configured
hub is `409`. There is no edit or regenerate mode: recovery is stopping the
container, deleting `data/deskmate.sqlite`, and starting again (see "Reset"
in docs/SETTINGS.md).

**Roles.** A `/login` session cookie carries one of two roles. Signing in
with the bearer token grants **admin**; signing in with the device key
grants **reader**. An admin cookie lasts 7 days, a reader cookie 30 days -
the credential that reaches the settings pages is shorter-lived on purpose.
The device key is never an admin credential, even though it is a valid
reader credential: it sits in the E1002's unencrypted flash, so every
`admin` route checks the bearer against the token only, never the device
key, whether presented as a header or through a cookie. Losing a flashed
device therefore never hands out admin access. A cookie in the pre-role
format (from before this version) is rejected the same as no cookie at all,
which is why upgrading logs every browser out once (see "Upgrading an
existing install" in docs/DEPLOY.md).

**Before the hub is set up**, every `reader`, `admin`, `token` and `device`
route answers `503` (see the endpoint table above for the handful of routes
that stay open, and the redirects on `GET /` and the HTML preview and
settings routes).

**Once the hub is set up:**

- **`reader` routes** (`/display/{page}.png`, `/preview`,
  `/preview/{page}.html`, `/api/state`,
  `/api/hub`, `GET /api/device/telemetry`, `/api/device/history`) require
  `Authorization: Bearer <token or device key>`, or a browser session
  cookie of either role obtained at `/login`.
- **`admin` routes** (`/settings*`, `/setup/{step}`) require
  `Authorization: Bearer <token>`, or an admin session cookie. The device
  key is never accepted here, as a header or as a cookie - see Roles above.
- **The `device` route** (`POST /api/device/telemetry`) requires
  `Authorization: Bearer <token or device key>`. The cookie is never
  accepted there.
- **`token` routes** (`POST /api/ai-usage`, `/api/brief`, `/api/tasks`,
  `POST`/`DELETE /api/alert`) require `Authorization: Bearer <token>`. The
  device key and the cookie are never accepted for a write.
- **`GET /login`** shows a form; entering the token sets an admin cookie,
  the device key a reader cookie (`deskmate_session`, `HttpOnly`,
  `SameSite=Lax`, signed with a role and an expiry, `Secure` when the hub's
  base URL is `https`), and redirects to the page that was asked for. An
  unauthenticated browser opening `/preview`, `/preview/{page}.html`,
  `/settings*` or `/setup/{step}` is redirected to `/login` (`303`) instead
  of getting a `401`.
- Still open by design, configured or not: `GET`/`POST /setup` (guarded by
  the caller's address instead of a credential while unconfigured), `GET
  /healthz` (minimal body until authenticated), `/docs` and `/openapi.json`
  (the schema is public in the repository anyway - Swagger UI needs no
  credential to load, only to call a route through it), and `/static`
  (fonts). `GET /login` also stays open once configured; while unconfigured
  it simply redirects to `/setup`.
- **Rotate** (`POST /settings/rotate`) mints a fresh token, device key and
  session secret, which rotates the session secret (every cookie stops
  working) and the device key (the device's Hub key field must be updated,
  its own web page or the Home Assistant text entity, before it can fetch
  again; a reflash is only needed for firmware built before these runtime
  fields existed). **Reset** (delete `data/deskmate.sqlite`) does the same,
  plus throws away every setting and pushed dataset. See docs/SETTINGS.md.

`Bearer` is case-insensitive, checked by hashing and
`hmac.compare_digest` against the stored hash. One error shape for the
whole API: a JSON body with `detail` (a string, or a list of validation
problems for a `422`).

| Status | Meaning |
| --- | --- |
| 401 | A `reader`, `admin`, `device` or `token` route was called with no credential or the wrong one, once the hub is set up (a reader cookie or the device key on an `admin` route counts as no credential) |
| 303 | An unauthenticated browser opened `/preview`, `/preview/{page}.html`, `/settings*` or `/setup/{step}` (redirected to `/login`), or any browser opened `/` or an HTML `reader`/`admin` route on an unconfigured hub (redirected to `/setup`) |
| 403 | `POST /setup` was called from an address that is not loopback, private, or link-local |
| 409 | `POST /setup` on an already-configured hub |
| 422 | Body rejected: unknown field, bad `schema_version`, a length or count cap, a duplicate task id, a naive datetime, an invalid base URL, or a settings form field that failed validation |
| 503 | The hub is not set up yet (every `reader`, `admin`, `token` and `device` route), or the database's `hub` row exists but is unreadable (the detail names the problem) |

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

Configuration is sections on `/settings` (`app/modules/<id>/settings.py`,
one pydantic model per section, stored as a row of `data/deskmate.sqlite`),
not environment variables; see docs/SETTINGS.md for every field. Each
section with a source has a Source selector; `fixture` is always available,
for development, but no section defaults to it: an unconfigured hub reports
every block `unavailable` and renders an honest empty state rather than
demo data.

| Section | Sources | Default |
| --- | --- | --- |
| tasks | `push`, `obsidian`, `fixture` | `push` |
| calendar | `ics`, `fixture` | `ics` |
| weather | `open_meteo`, `fixture` | `open_meteo` |
| ai_usage | `push`, `fixture` | `push` |
| brief | `push`, `fixture` | `push` |
| home | `rest`, `fixture` | `rest` |
| device | `store`, `fixture` | `store` |

`push` (tasks, ai_usage, brief) always reads the matching `datasets` row -
whatever the last `POST /api/tasks`, `/api/ai-usage` or `/api/brief` wrote -
so there is no separate "file" or "auto" selector left to distinguish from
it (see the plan's Non-goals: both used to mean "the pushed file", which is
now just `push`). `GET /api/hub`'s `sources` block is therefore a plain
`{dataset: {source}}` per pushed dataset, straight from the section models:
there is no live/last-fetch split left to report.

The footer's DEMO mark covers exactly the three pushed datasets (ai_usage,
brief, tasks), and only on the Today and Brief pages, the only pages that
draw them: when one of a page's own pushed datasets is `fixture`-sourced,
that page's footer prints `DEMO` so a fresh install never passes demo
numbers off as real. Weather, calendar, home and device never print DEMO:
the hub fetches them itself, or (device) the E1002 firmware pushes them, so
there is nothing there for a remote agent's push to represent. See
"Staleness" below.

Obsidian access is read only. The vault is mounted read-only in Docker.

### Staleness

Each pushed dataset's own `stale_seconds` field (ai_usage 21600, brief and
tasks 36000, all defaults; see docs/SETTINGS.md) bounds how old it can get
before the panel marks it: `view.py`'s `stale_info` compares its reference
time, which is the state's own `updated_at` converted to the general
section's timezone (not the wall-clock instant the comparison runs;
`updated_at` is the newest adapter timestamp in that state snapshot, so a
cached, not yet re-fetched page compares against the moment it was built,
not against now)
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
  the `alert` row of `data/deskmate.sqlite`). `/display/alert.png` renders
  it. Priority order:
  `critical > doorbell > important > normal`; a lower priority does not
  replace a higher one that is still active.
- The device is told to show it through its ESPHome API action
  `show_alert` (duration seconds, beep bool). Home Assistant calls the
  hub first, then the device action. See `docs/DATA-SOURCES.md`.
- The device restores the previous page after the duration.

## Device (ESPHome)

- Framework esp-idf, PSRAM octal. Flash layout 16MB (chip has 32MB, but ESP-IDF needs an experimental flag for 32MB with OTA).
- No business logic. Only: current page index, download, display, buttons,
  buzzer, sensors, alert timer.
- Page URL base and the device key are runtime text components
  (`hub_base_url`, `hub_key`), not hard-coded in the YAML: `secrets.yaml`
  only seeds their first-boot value, and both are edited afterwards on the
  device's own web page or as Home Assistant text entities, with no
  reflash. Wi-Fi is likewise provisioned at runtime, through the captive
  portal or Improv, never from `secrets.yaml`.
- The device holds no fixed page list. It cycles a page index (0-based,
  wrapping on the buttons and the ESPHome API) and requests
  `/display/<index>.png`; it learns the page count and the id at each
  index from every telemetry response's `page_count` and `pages` fields
  and keeps them in restorable globals, so enabling, disabling or
  reordering a module on the hub's settings page reaches the device on its
  next telemetry post, with no reflash. Before the first response of a
  session (a fresh boot, or a hub that has never answered), the device
  assumes a page count of 5 and does not yet know any ids; `/display/{n}.png`
  still resolves for it either way. `alert` is always requested by name.
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
  battery, temperature, humidity, Wi-Fi RSSI, uptime, page, page_index,
  power mode, USB presence and charge state to `/api/device/telemetry`.
  `page` is the resolved page id when the device already knows it, `alert`
  while an alert is showing (with `page_index` still naming the page
  underneath), or `null` when the device has not yet been told the id for
  that index; the hub treats `page` as authoritative and `page_index` as
  the fallback. The hub also records that POST's own origin - the caller's
  address and the `Host` header it used - and carries it on `DeviceState`
  as `remote_addr` and `hub_host`. Both reach `GET /api/state`
  (reader-authenticated) and the System page's HUB column (DEVICE IP, HUB
  URL); neither is returned by `GET`/`POST /api/device/telemetry` itself.

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
