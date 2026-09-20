# Data sources

Every section that reads live data has a Source field on `/settings` (see
docs/SETTINGS.md for the full list of sections and fields). `fixture` is
always available for development, but it is no longer the default: with a
freshly claimed hub every section below reports `unavailable` and the pages
render an honest empty state instead of demo data. Enable real sources one
at a time on `/settings`, or set a section's source to `fixture` there (see
also `scripts/render-all.py`, which pins every source to `fixture` to render
the demo pages during development) to see the demo pages.

Rules that hold for all of them:

- An adapter that is not configured reports `status: "unavailable"`.
- An adapter that raises reports `status: "error"`, or `status: "stale"` while a
  previously fetched value is still being shown.
- The page prints `unknown` / `unavailable`. Nothing is ever invented.
- One broken adapter never breaks a page.

Check what the hub currently thinks with:

```sh
curl -s http://127.0.0.1/healthz
curl -s http://127.0.0.1/api/state
curl -s http://127.0.0.1/api/hub
```

`/healthz` always answers `200` with no credential, but only a minimal body
unless the hub is set up and the caller is an authenticated reader;
`/api/state` and `/api/hub` answer `503` before the hub is set up and need
the bearer token, the device key, or a `/login` session cookie once it is
(see "Auth" in `docs/ARCHITECTURE.md`). `/healthz` only replays each
adapter's last outcome and never fetches; `/api/state` is the one that
forces every adapter to fetch.

For AI usage, the brief and tasks, `POST`ing to the hub over HTTP is the
only way to get real data onto the panel: each push writes a row of the
`datasets` table in `data/deskmate.sqlite`, and the `push` source (the
default for all three) always reads that row. See
`skills/deskmate/SKILL.md` for the agent-facing version of the three push
endpoints, and `GET /openapi.json` for the schema of record.

---

## Overview

| Section | Sources | Default | Settings |
| --- | --- | --- | --- |
| tasks | `fixture`, `push`, `obsidian` | `push` | Source, Obsidian vault path, Obsidian task glob |
| calendar | `fixture`, `ics` | `ics` | Source, Feeds (URL, name, color) |
| weather | `fixture`, `open_meteo` | `open_meteo` | Source, Latitude, Longitude, Location name |
| ai_usage | `fixture`, `push` | `push` | Source |
| brief | `fixture`, `push` | `push` | Source, Evening hour |
| home | `fixture`, `rest` | `rest` | Source, URL, Token, Entities |
| device | `store`, `fixture` | `store` | Source, Retention days |

Every default above is the honest, unconfigured choice: `push`, `ics`,
`open_meteo`, `rest` and `store` all report `unavailable` until you point
them at something real, rather than quietly drawing fixture data. `push`
always reads the matching `datasets` row - whatever the last
`POST /api/tasks`, `/api/ai-usage` or `/api/brief` wrote - so a push always
lands where the panel reads, and there is nothing left to distinguish a
"configured" selector from an "effective" one: `GET /api/hub` reports one
source per pushed dataset (`{dataset: {source}}`). Every field, its
description and its default is on `/settings`; see docs/SETTINGS.md.

Timezone and units live in the General section (default `Asia/Bangkok`,
`metric`). `FIXTURES_DIR`, `DATA_DIR` and `FIXTURE_RELATIVE_DATES` are
process-level knobs in `.env.example`, not settings-page fields.

---

## Fixtures

`fixtures/*.json` hold the demo data. Each file carries an `anchor_date`; when
`FIXTURE_RELATIVE_DATES=true` (the default) the loader shifts every date and
datetime in the file by `today - anchor_date` whole days, so the demo always
looks current. Clock times are never changed, only the day.

Set `FIXTURE_RELATIVE_DATES=false` in `.env` to read the literal dates in
the files.

---

## Tasks

Two ways to get tasks onto the panel: push, or a read-only Obsidian vault.
The tasks section's source (default `push`, set on `/settings`, see
docs/SETTINGS.md) picks between them, plus `fixture` for demo data.

### Push (default)

```sh
curl -s -X POST http://<hub-address>/api/tasks \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"tasks": [{"id": "agent-1", "title": "Ship the release notes", "priority": "high"}]}'
```

Requires the bearer token. Replaces the whole task list on every push (not a
diff): `tasks` is 0 to 60 of `{id (1-64 chars, required, the agent's own
stable identifier, reused across pushes to update the same task), title
(1-200 chars), due (date or null), priority (`high`/`medium`/`low`/`none`,
default `none`), completed (default false), tags (0-8)}`. A duplicate `id`
inside one push is `422`. The push writes a `tasks` row of the `datasets`
table (`data/deskmate.sqlite`) and invalidates the cached adapter, so
`/api/state` reflects it on the very next build. Response `200`:
`{"stored": "tasks", "received_at": "<local ISO>", "count": <n>,
"source": "push"|"fixture"|"obsidian"}`, plus `"warning"` when the
tasks section's source is pinned to `fixture` or set to `obsidian` (either
way the push is stored, but the panel will not show it, and
`source` names which). Full shape: `GET /openapi.json`.

### Obsidian (read only)

Set the tasks section's source to `obsidian` on `/settings`, with a vault
path (in Docker, `/vault`, the container's mount point - see
`OBSIDIAN_VAULT_PATH` in `.env.example` for the host side of that bind
mount) and a task glob (default `**/*.md`).

The vault is opened read only and is bind-mounted `:ro` in Docker. Directories
whose name starts with `.` (such as `.obsidian`) are skipped, and the scan stops
after 5000 files.

Two task dialects are parsed, and they may be mixed in one file:

Dataview inline fields:

```markdown
- [ ] Send the vendor quote (due:: 2026-09-05) [priority:: high]
- [x] Draft retro notes (due:: 2026-09-03)
```

Tasks plugin, emoji markers (calendar emoji = due date, double-up arrow = high
priority, and so on):

```markdown
- [ ] Review PR 482 auth refactor <calendar emoji> 2026-09-06 <double-up emoji> #code
```

Recognised: `due` / `deadline` and `priority` / `prio` as inline fields;
due, scheduled, start, done, created and cancelled date emoji; the five Tasks
priority emoji; `#tags`; `[x]` as completed; `[-]` (cancelled) is dropped.
Recurrence text after the repeat emoji is ignored. The emoji never reach the
rendered page.

Normalized fields: `id`, `title`, `due`, `priority`, `completed`, `source`,
`tags`.

---

## Calendar: ICS

Set the calendar section's source to `ics` (the default) on `/settings` and
add one or more feeds, each a row of `{url, name, color}`. `url` is either
an `http(s)` URL or a local file path. Simple `RRULE` recurrences are
expanded inside the agenda window only (one day back, three weeks forward,
at most 50 occurrences per event); `EXDATE` is honoured. All-day events keep
`all_day: true`.

Normalized fields: `id`, `title`, `start`, `end`, `all_day`, `location`,
`source`, `calendar`.

### Naming and colouring the feeds

Each feed row's `name` and `color` are both optional:

| Field | Default | Notes |
| --- | --- | --- |
| `name` | the URL host, else `calendar N` | Goes into `Event.calendar`, which only decides which feed gets which colour below; no page prints the name. |
| `color` | cycles blue, green, yellow | One of `blue`, `green`, `yellow`, `red`, `black`. |

The agenda and the Today NEXT pane print each event's time in its calendar's
colour - that colour coding is the only place a feed's identity shows on the
panel; no page prints a legend naming which colour is which feed. Red and
yellow are not handed out by default: they mean overdue and caution
everywhere else on the panel, and a calendar is not a state. The fixture
calendar tags every event `work` or `personal`, so the demo shows blue and
green through the same default cycle.

---

## Weather: Open-Meteo

Set the weather section's source to `open_meteo` (the default) on
`/settings`, with a latitude, longitude and location name - typed in
directly, or filled in from the section's own "Find" location search (see
docs/SETTINGS.md). No API key. Two endpoints are called:

- `https://api.open-meteo.com/v1/forecast` for current temperature, apparent
  temperature, humidity, weather code, daily high/low, daily maximum UV index,
  daily maximum precipitation probability and the hourly precipitation
  probability series.
- `https://air-quality-api.open-meteo.com/v1/air-quality` for `pm2_5` and
  `us_aqi`. If this call fails the rest of the page still renders and the
  PM2.5 / AQI tiles show `--`.

Rain timing comes from the hourly series: the first and last hour of today whose
precipitation probability is at least 50 percent become
`rain likely from HH:MM` / `to HH:MM`. If no hour crosses the threshold the page
says `NONE EXPECTED`.

No location is assumed. Without both coordinates the adapter reports
`unavailable`.

---

## AI usage / quota

There is no supported public API for Claude or Codex quota, and the hub does
no browser or credential scraping. An agent that pushes the numbers is the
only way to get them onto the panel; the ai_usage section's source (default
`push`, set on `/settings`) picks between that and `fixture` demo data.

```sh
curl -s -X POST http://<hub-address>/api/ai-usage \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"providers": [{"provider": "claude", "short_window_percent_remaining": 62,
       "weekly_percent_remaining": 40}]}'
```

Requires the bearer token (see "Auth" in `docs/ARCHITECTURE.md`).
`providers`: 1 to 8 of `{provider (1-32 chars, required), short_window_percent_remaining
0-100 or null, short_window_reset_at, weekly_percent_remaining, weekly_reset_at,
collected_at}`. Percent fields are percent **remaining**. Every datetime must
carry a UTC offset; a naive one is rejected with `422`. `collected_at`
defaults to the moment of the push if omitted. The push writes an
`ai_usage` row of the `datasets` table (`data/deskmate.sqlite`) and
invalidates the cached adapter, so `/api/state` reflects it on the very next
build. Response `200`: `{"stored": "ai_usage", "received_at": "<local ISO>",
"count": <n>, "source": "push"|"fixture"}`, plus `"warning"` when
the ai_usage section's source is pinned to `fixture` (the push is stored,
but the panel will not show it until the section's source changes). Unknown
fields, a `schema_version` other than 1, or more than 8 providers are `422`.
Full shape: `GET /openapi.json`.

| Field | Type | Notes |
| --- | --- | --- |
| `provider` | string | Shown uppercased on the Today page. Required. |
| `short_window_percent_remaining` | int or null | The 5 hour window. `null` renders as `unknown`. |
| `short_window_reset_at` | ISO 8601 or null | Must carry a UTC offset. |
| `weekly_percent_remaining` | int or null | The 7 day window. |
| `weekly_reset_at` | ISO 8601 or null | |
| `collected_at` | ISO 8601 or null | When the reading was taken. Also the age the ai_usage section's `stale_seconds` measures from (the oldest provider), see `docs/ARCHITECTURE.md`. |
| `collection_status` | string | `ok`, `unknown` or `error`. Anything other than `ok` makes the page print `unknown` instead of the numbers. |

---

## AI brief

Opening the brief page never triggers an AI request. The hub only shows
whatever was last pushed. The brief section's source (default `push`, set
on `/settings`) picks between that and `fixture` demo data.

Mode is chosen by the local clock: morning before the brief section's
evening hour (default 14), evening from that hour on, unless the push names
a mode.

```sh
curl -s -X POST http://<hub-address>/api/brief \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"headline": "Two hard deadlines today", "note": "Vendor quote is one day overdue.",
       "sections": [{"title": "Key tasks", "items": ["Ship it"]}]}'
```

Requires the bearer token. `mode` (`morning`/`evening`) is optional and
defaults to what the clock would pick. `headline` 1-120 chars, `note`
optional up to 280, `sections` 0-6 of `{title 1-40, items 0-12 of up to 160
chars}`, `generated_at` optional (a UTC-offset datetime; a naive value is
`422`), defaults to the moment of the push. The push writes a `brief` row of
the `datasets` table (`data/deskmate.sqlite`) and invalidates the cached
adapter. Response `200`: `{"stored": "brief", "received_at": "<local ISO>",
"count": <n sections>, "source": "push"|"fixture"}`, plus
`"warning"` when the brief section's source is pinned to `fixture`. Full
shape: `GET /openapi.json`.

| Field | Type | Notes |
| --- | --- | --- |
| `mode` | `morning` or `evening` | Defaults to what the clock would pick. |
| `generated_at` | ISO 8601 | Must carry a UTC offset. Also the age the brief section's `stale_seconds` measures from, see `docs/ARCHITECTURE.md`. |
| `headline` | string | One line, shown large. Keep it under about 60 characters. |
| `note` | string | One sentence. Also used as the AI NOTE bar on the Today page. |
| `sections` | list | The Brief page has room for 9 lines total (`BRIEF_MAX_LINES` in `app/view.py`); each section title and each item counts as one line, and whatever does not fit past that budget is cut. |

Suggested sections. Morning: today's schedule, key tasks, suggested focus,
things at risk, unfinished work from yesterday. Evening: work completed, open
tasks, things that changed, what should happen tomorrow.

---

## Home Assistant: REST

Set the home section's source to `rest` (the default) on `/settings`, with
a URL (for example `http://192.168.1.50:8123`), a long-lived access token,
and the entity slot rows below. The hub calls `GET {url}/api/states` once
per refresh with `Authorization: Bearer <token>` and picks out the entities
named in the entity list. The token stays on the server; it is never sent
to the E1002.

The entities field is a list of `{slot, entity_id}` rows mapping a
dashboard slot to a Home Assistant entity id.

HOME slots: `front_door`, `doorbell`, `motion`, `room_temperature`,
`room_humidity`.
SYSTEM slots: `internet`, `home_assistant`, `nas`, `proxmox`, `backup`,
`assistant`, `assistant_last_run`.

Slots you leave out are not drawn. Slots that name an entity Home Assistant does
not return are drawn as `unknown`. `binary_sensor.*` entities are labelled from
their `device_class` (door/window/opening/lock give Open / Closed,
motion/occupancy/presence give Detected / Clear).

---

## Device telemetry: the E1002 pushes, the hub stores

The reTerminal E1002 POSTs one sample every 5 minutes and the hub appends it
to the `telemetry` table of `data/deskmate.sqlite`. There is no polling.
The device section's source (default `store`, set on `/settings`) is
`store` in production; its Retention days field (default 30) controls how
long samples are kept.

Once the hub is set up, `POST /api/device/telemetry` requires
`Authorization: Bearer <token or device key>` (`firmware/e1002.yaml` sends
the device key); a `/login` session cookie is never accepted here, only a
bearer credential. Before the hub is set up the endpoint answers `503`,
like every other read and the device's own image fetches; see "Auth" in
`docs/ARCHITECTURE.md`. Keep the hub on a LAN or behind a reverse proxy with
its own access control rather than facing the public internet; see
`docs/DEPLOY.md`.

### Payload

```http
POST /api/device/telemetry
Content-Type: application/json

{
  "device": "reterminal-e1002",
  "battery_voltage": 4.056,
  "battery_level": 92.6,
  "temperature": 32.80,
  "humidity": 54.4,
  "wifi_rssi": -28,
  "uptime_s": 425,
  "page": "brief",
  "page_index": 3,
  "battery_mode": false,
  "usb_present": true,
  "charge_state": "charged"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `device` | string, 1 to 64 chars | Required. The ESPHome node name. |
| `battery_voltage` | float or null | Volts. |
| `battery_level` | float or null | Percent. Drives the battery bar. |
| `temperature` | float or null | Degrees C at the desk. |
| `humidity` | float or null | Percent relative humidity. |
| `wifi_rssi` | float or null | dBm, negative. |
| `uptime_s` | float or null | Seconds since boot. |
| `page` | string or null, up to 32 chars | Page the device is showing. Authoritative when present. |
| `page_index` | int or null | Which slot of the window list the device is on, 0-based. Read only when `page` is null or missing, and resolved to that slot's page id; an index past the last enabled page stores null. Optional; older firmware never sends it. |
| `battery_mode` | bool or null | Whether the gauge is running on battery. Optional; older firmware never sends it. |
| `usb_present` | bool or null | Whether USB power is plugged in. Optional; older firmware never sends it. |
| `charge_state` | string or null | One of `charging`, `charged`, `pre_charge`, `not_charging`, `unknown`. Optional; older firmware never sends it. |
| `wake_cause` | string or null, up to 32 chars | Free string, e.g. `power_on`, `timer`, `button_left`, `button_right`, `button_green`, `button_unknown`, `other`. No fixed enum. Optional; older firmware never sends it. |

The three power fields are all optional and default to null, so a payload from
older firmware with none of them is still accepted. The DESK panel on the
System page turns them into one power label next to WIFI: "ON USB, CHARGING"
when `usb_present` is true and `charge_state` is `charging`, "ON USB, CHARGED"
when `charge_state` is `charged`, "ON BATTERY" when `usb_present` is false, and
nothing at all for any other combination (including a payload that never sent
the fields).

Every numeric field may be `null`: on a cold boot the sensors are not ready
yet, and the firmware reports the hole rather than a made up reading. A hole is
stored as `NULL` and becomes a gap in the chart, never an interpolated point.

Responses: `202` with `{"accepted": true, "received_at": "<local ISO>",
"page_count": 5, "pages": ["today", "agenda", "weather", "brief", "system"]}`,
or `400` with `{"accepted": false, "error": ..., "detail": [...]}` when the body
is not JSON or fails validation.

`page_count` is how many pages are enabled right now and `pages` is their ids
in window-list order; `alert` is never in the list. The device holds no page
list of its own: `firmware/e1002.yaml` parses these two fields out of every
telemetry response into restorable globals and builds its `/display/{n}.png`
URLs from them, so enabling, disabling or reordering a module on `/settings`
reaches the panel on its next post, with no reflash. Until a session has had
one response the device knows its index but not the id, which is why `page`
may be null and `page_index` is there to resolve it.

### Reading it back

```sh
curl -s http://127.0.0.1/api/device/telemetry
curl -s "http://127.0.0.1/api/device/history?hours=6"
```

Both answer `503` before the hub is set up, and need the bearer token, the
device key, or a `/login` session cookie once it is.

`GET /api/device/telemetry` returns the newest sample, its `age_seconds` and a
summary (`sample_count`, `oldest`, `newest`, `retention_days`). The stored
sample's `page` is always a page id, never an index: `page_index` is a request
field the hub resolves on the way in and no column holds it, so it is not part
of what comes back.
`GET /api/device/history?hours=24` returns the samples in that window, folded
into at most 300 evenly sized means (`downsampled` says whether that happened).
`hours` must be greater than 0 and at most 8760.

The device also shows up in `/api/state` as the `device` block, and in
`/healthz` as that block's last known status (`/healthz` never fetches).

### Storage and retention

One table, `telemetry`, in the hub's own `data/deskmate.sqlite`, with an
index on `received_at`, WAL journal so a page render never blocks the
device. Timestamps are stored as fixed-width UTC ISO strings and presented
in the general section's timezone. Rows older than the device section's
Retention days field (default 30) are deleted inside the same transaction
as each insert, so there is no background job to keep alive. At one sample
per 5 minutes that is about 8600 rows per month.

### Freshness

| `status` | Meaning |
| --- | --- |
| `ok` | The newest sample is younger than 15 minutes (three missed reports). |
| `stale` | The device reported before, but not recently enough. |
| `unavailable` | Nothing has ever been stored. |

### The DESK panel and the chart

The System page draws temperature (big), humidity, battery percent with a bar,
Wi-Fi dBm and a 24 hour chart. Home Assistant's `room_temperature` and
`room_humidity` slots are no longer drawn there: the device owns those numbers
now.

History for the chart is meaned into 15 minute buckets, at most 96 points. The
SVG is built in `app/renderer/chart.py` and drawn with pure panel primaries so
it survives quantization: 4 px red temperature, 4 px blue humidity, 3 px black
axes, white ground, three clock labels plus `NOW`, and the min/max of each
series in bold. With no history the panel prints `NO DEVICE DATA YET`; it never
draws an invented line.

### Fixture

Setting the device section's source to `fixture` falls back to
`fixtures/device.json` (24 hours at 5 minute spacing) **only while the
store is empty**, so the page can be designed before the device is flashed.
The first real sample retires the fixture. Sample times in that file are
`offset_minutes` relative to now rather than absolute stamps, because a
rolling 24 hour window only means anything against the current clock.
Production should leave the source at `store`.

---

## Alerts

`POST /api/alert` stores the current alert; `/display/alert.png` renders it.

```http
POST /api/alert
Content-Type: application/json

{
  "title": "Doorbell",
  "message": "Someone is at the door",
  "priority": "doorbell",
  "duration_seconds": 90,
  "beep": true,
  "source": "Front door"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string, 1 to 60 chars | Required. Drawn very large. |
| `message` | string, up to 240 chars | Optional, drawn below the title. |
| `priority` | `critical`, `doorbell`, `important`, `normal` | Default `normal`. |
| `duration_seconds` | int, 5 to 600 | Default 90. How long the device shows it. |
| `beep` | bool | Default true. Whether the device buzzes. |
| `source` | string, up to 48 chars | Optional. What raised the alert, in your own words. Shown in the alert page title bar; without it the bar shows the priority class. |

Responses: `201` with `{"accepted": true, "alert": {...}}`, or `409` with
`{"accepted": false, ...}` when a higher priority alert is still active
(`critical > doorbell > important > normal`; equal priority replaces).
`DELETE /api/alert` clears it and returns `{"cleared": true|false}`.

### Home Assistant example

`configuration.yaml`:

`POST` and `DELETE /api/alert` require the hub's bearer token now
(`Authorization: Bearer <token>`, shown once on `/setup`). A YAML tag such as
`!secret` cannot sit inside a quoted scalar, so put the whole header value in
`secrets.yaml` instead of just the token:

```yaml
# secrets.yaml
deskmate_auth: "Bearer <token>"
```

```yaml
rest_command:
  deskmate_alert:
    url: "http://<hub-address>/api/alert"
    method: POST
    headers:
      Authorization: !secret deskmate_auth
    content_type: "application/json"
    payload: >-
      {"title": "{{ title }}", "message": "{{ message }}",
       "priority": "{{ priority }}", "duration_seconds": {{ duration }},
       "beep": {{ beep | lower }}, "source": "{{ source }}"}
```

Script that pushes the alert to the hub and then tells the device to show it.
The ESPHome device is named `reterminal-e1002`, so its action is
`esphome.reterminal_e1002_show_alert`:

```yaml
script:
  deskmate_show_alert:
    alias: Deskmate show alert
    mode: single
    fields:
      title:
        selector: { text: }
      message:
        selector: { text: }
      priority:
        selector:
          select:
            options: [critical, doorbell, important, normal]
      duration:
        selector:
          number: { min: 5, max: 600, unit_of_measurement: s }
      beep:
        selector: { boolean: }
      source:
        selector: { text: }
    sequence:
      # 1. The hub renders the alert page first.
      - action: rest_command.deskmate_alert
        data:
          title: "{{ title }}"
          message: "{{ message | default('') }}"
          priority: "{{ priority | default('normal') }}"
          duration: "{{ duration | default(90) | int }}"
          beep: "{{ beep | default(true) }}"
          source: "{{ source | default('') }}"
        response_variable: hub
      # 2. Only then does the device download and show it, and only while it
      #    is always-on. In battery mode it sleeps for hours between wakes and
      #    the firmware ignores show_alert, so skip the call instead of
      #    letting it fail on an unavailable device.
      - condition: template
        value_template: "{{ hub.status in [200, 201] }}"
      - condition: template
        value_template: "{{ not states('sensor.reterminal_e1002_power_mode').startswith('battery') }}"
      - action: esphome.reterminal_e1002_show_alert
        data:
          duration: "{{ duration | default(90) | int }}"
          beep: "{{ beep | default(true) }}"

automation:
  - alias: Doorbell to deskmate
    triggers:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    actions:
      - action: script.deskmate_show_alert
        data:
          title: Doorbell
          message: Someone is at the door
          priority: doorbell
          duration: 90
          beep: true
          source: Front door
```

The device shows `/display/alert.png`, keeps it for `duration` seconds, then
returns to the page it was on. Clear the alert from the hub afterwards if you
want the page to go back to "no active alert":

```sh
curl -X DELETE http://<hub-address>/api/alert \
  -H "Authorization: Bearer <token>"
```

The `show_alert` ESPHome API action is defined in `firmware/e1002.yaml`
(Phase 2). It is listed here so the Home Assistant side can be written and
reviewed before the device is flashed.

---

## Adding a new pushed dataset

To add a new kind of pushed data to the hub:

1. Add its pydantic model in `app/models.py` (`extra="forbid"`,
   `schema_version`, length caps, `AwareDatetime` for any timestamp).
2. Add a `POST /api/<name>` route in a `build_router(context)` function
   in the owning module's `app/modules/<name>/routes.py` (the built-in
   `app/modules/tasks/routes.py` is the pattern to copy) that calls
   `app/datasets.py:write_dataset` (the `datasets` table upsert) and then
   the matching adapter's `invalidate()`. Point the module's `Module.routes`
   at that function; core mounts it under `/api` with `Depends(require_token)`
   already applied, in the registry loop in `app/main.py:create_app`, so the
   route itself never has to check a credential (see docs/MODULES.md,
   "Push routes").
3. Add a `Push<Name>Adapter` under `app/adapters/` that reads the row back
   through `app/datasets.py:read_dataset`, plus a `Fixture<Name>Adapter` if
   it should fall back to demo data, and a settings section
   (`app/modules/<name>/settings.py`) with a `source` field naming both.
4. In `app/view.py`: give it a label on the page and in the matching
   template; add a `<dataset>_stale` wrapper around `stale_info` if it has a
   staleness threshold; list every page that actually draws it in
   `PAGE_PUSH_DATASETS` (which is what each page's `PageSpec.demo_datasets`
   is built from); and, if it should ever flag a page's footer with `!`,
   add that check to that page's own flag function (`today_flag`,
   `agenda_flag`, ...), which is what its `PageSpec.flag` points at.
   **A page missing from `PAGE_PUSH_DATASETS` silently gets no DEMO mark
   and no stale flag for that dataset**, even if the page draws it, so
   double check every page that reads the new field, not just the page it
   is "for".
5. Add one curl example and its cadence here, and its section to
   docs/SETTINGS.md.
