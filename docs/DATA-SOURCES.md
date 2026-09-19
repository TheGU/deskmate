# Data sources

Every adapter in dashboard-hub has a `*_SOURCE` selector. `fixture` is always
available, so the hub runs with an empty configuration and never needs the
network. Enable real sources one at a time.

Rules that hold for all of them:

- An adapter that is not configured reports `status: "unavailable"`.
- An adapter that raises reports `status: "error"`, or `status: "stale"` while a
  previously fetched value is still being shown.
- The page prints `unknown` / `unavailable`. Nothing is ever invented.
- One broken adapter never breaks a page.

Check what the hub currently thinks with:

```sh
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/api/state
curl -s http://127.0.0.1:8080/api/hub
```

`/healthz` always answers `200` with no credential; once the hub is claimed
an unauthenticated call gets a minimal body instead of the full one, and
`/api/state` and `/api/hub` need the bearer token, the device key, or a
`/login` session cookie (see "Auth" in `docs/ARCHITECTURE.md`). `/healthz`
only replays each adapter's last outcome and never fetches; `/api/state` is
the one that forces every adapter to fetch.

For AI usage, the brief and tasks, `POST`ing to the hub over HTTP is the
primary way to get real data onto the panel; a hand-written file under
`data/` is the fallback for offline testing or a tool that only writes files.
Both land in the same place. See `skills/deskmate/SKILL.md` for the
agent-facing version of the three push endpoints, and `GET /openapi.json` for
the schema of record.

---

## Overview

| Adapter | Sources | Default | Configuration |
| --- | --- | --- | --- |
| tasks | `fixture`, `file`, `obsidian`, `auto` | `auto` | `TASKS_SOURCE`, `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TASK_GLOB` |
| calendar | `fixture`, `ics` | `fixture` | `CALENDAR_SOURCE`, `CALENDAR_ICS_URLS` |
| weather | `fixture`, `open_meteo` | `fixture` | `WEATHER_SOURCE`, `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, `WEATHER_LOCATION_NAME` |
| ai_usage | `fixture`, `file`, `auto` | `auto` | `AI_USAGE_SOURCE`, `AI_USAGE_PATH` (default `data/ai-usage.json`) |
| ai_brief | `fixture`, `file`, `auto` | `auto` | `BRIEF_SOURCE`, `BRIEF_DIR` (default `data/brief`), `BRIEF_EVENING_HOUR` |
| home_assistant | `fixture`, `rest` | `fixture` | `HA_SOURCE`, `HA_URL`, `HA_TOKEN`, `HA_ENTITIES` |
| device | `store`, `fixture` | `fixture` | `DEVICE_SOURCE`, `TELEMETRY_DB_PATH`, `TELEMETRY_RETENTION_DAYS` |

`auto` picks the file adapter once its file exists and is readable, otherwise
`fixture`; a push always lands where the file adapter reads, so `auto` is
what makes a push show up on the panel with no other configuration.
`GET /api/hub` reports both the configured selector and the effective one
per dataset (`{dataset: {configured, effective}}`).

Global: `TIMEZONE` (default `Asia/Bangkok`), `UNITS` (`metric`),
`FIXTURES_DIR`, `DATA_DIR`, `FIXTURE_RELATIVE_DATES`.

---

## Fixtures

`fixtures/*.json` hold the demo data. Each file carries an `anchor_date`; when
`FIXTURE_RELATIVE_DATES=true` (the default) the loader shifts every date and
datetime in the file by `today - anchor_date` whole days, so the demo always
looks current. Clock times are never changed, only the day.

Set `FIXTURE_RELATIVE_DATES=false` to read the literal dates in the files.

---

## Tasks

Three ways to get tasks onto the panel: push, a hand-written or agent-written
file, or a read-only Obsidian vault. `TASKS_SOURCE=auto` (the default) picks
the file adapter once `data/tasks.json` exists, otherwise fixture; it never
selects Obsidian on its own.

### Push (primary)

```sh
curl -s -X POST http://deskmate.local:8080/api/tasks \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"tasks": [{"id": "agent-1", "title": "Ship the release notes", "priority": "high"}]}'
```

Requires the bearer token. Replaces the whole task list on every push (not a
diff): `tasks` is 0 to 60 of `{id (1-64 chars, required, the agent's own
stable identifier, reused across pushes to update the same task), title
(1-200 chars), due (date or null), priority (`high`/`medium`/`low`/`none`,
default `none`), completed (default false), tags (0-8)}`. A duplicate `id`
inside one push is `422`. Response `200`: `{"stored": "tasks.json",
"received_at": "<local ISO>", "count": <n>, "effective_source":
"file"|"fixture"|"obsidian"}` (`obsidian` only for tasks; ai-usage and brief
are always `file` or `fixture`), plus `"warning"` when `TASKS_SOURCE` is
pinned to `fixture` or set to `obsidian` (either way the push will not show
on the panel, and `effective_source` names which). Full shape:
`GET /openapi.json`.

### File (fallback): `data/tasks.json`

```sh
TASKS_SOURCE=file
```

```json
{
  "tasks": [
    {"id": "agent-1", "title": "Ship the release notes", "priority": "high"}
  ]
}
```

Same shape the push endpoint writes, plus an optional top-level
`received_at` (falls back to the file's own mtime; also the age
`TASKS_STALE_SECONDS` measures from, see `docs/ARCHITECTURE.md`). No date
shifting: a `due` date is used exactly as sent, unlike the fixture. Write
atomically (`tmp` file plus rename).

### Obsidian (read only)

```sh
TASKS_SOURCE=obsidian
OBSIDIAN_VAULT_PATH=D:/Obsidian/MyVault
OBSIDIAN_TASK_GLOB=**/*.md
```

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

```sh
CALENDAR_SOURCE=ics
CALENDAR_ICS_URLS=https://example.com/basic.ics,/data/work.ics
CALENDAR_NAMES=work,personal
CALENDAR_COLORS=blue,green
```

Comma separated. Each entry is either an `http(s)` URL or a local file path.
Simple `RRULE` recurrences are expanded inside the agenda window only (one day
back, three weeks forward, at most 50 occurrences per event); `EXDATE` is
honoured. All-day events keep `all_day: true`.

Normalized fields: `id`, `title`, `start`, `end`, `all_day`, `location`,
`source`, `calendar`.

### Naming and colouring the feeds

`CALENDAR_NAMES` and `CALENDAR_COLORS` are comma lists in the same order as
`CALENDAR_ICS_URLS`, and both are optional:

| Setting | Default | Notes |
| --- | --- | --- |
| `CALENDAR_NAMES` | the URL host, else `calendar N` | Goes into `Event.calendar` and is what the agenda legend prints. |
| `CALENDAR_COLORS` | cycles blue, green, yellow | One of `blue`, `green`, `yellow`, `red`, `black` per feed. |

The agenda and the Today NEXT pane print each event's time in its calendar's
colour, and the agenda's status bar entry is the legend that says which name
is which colour. Red and yellow are not handed out by default: they mean
overdue and caution everywhere else on the panel, and a calendar is not a
state. The fixture calendar tags every event `work` or `personal`, so the demo
shows blue and green through the same default cycle.

---

## Weather: Open-Meteo

```sh
WEATHER_SOURCE=open_meteo
WEATHER_LATITUDE=13.7563
WEATHER_LONGITUDE=100.5018
WEATHER_LOCATION_NAME=Bangkok
```

No API key. Two endpoints are called:

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
no browser or credential scraping. Something else has to produce the numbers,
either an agent that pushes them or a separate collector that writes a file.

### Push (primary)

```sh
curl -s -X POST http://deskmate.local:8080/api/ai-usage \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"providers": [{"provider": "claude", "short_window_percent_remaining": 62,
       "weekly_percent_remaining": 40}]}'
```

Requires the bearer token (see "Auth" in `docs/ARCHITECTURE.md`).
`providers`: 1 to 8 of `{provider (1-32 chars, required), short_window_percent_remaining
0-100 or null, short_window_reset_at, weekly_percent_remaining, weekly_reset_at,
collected_at}`. Percent fields are percent **remaining**. Every datetime must
carry a UTC offset; a naive one is rejected with `422` rather than assumed to
be `TIMEZONE`. `collected_at` defaults to the moment of the push if omitted.
Response `200`: `{"stored": "ai-usage.json", "received_at": "<local ISO>",
"count": <n>, "effective_source": "file"|"fixture"}`, plus `"warning"` when
`AI_USAGE_SOURCE` is pinned to `fixture` (the push is stored, but the panel
will not show it until the selector changes). Unknown fields, a
`schema_version` other than 1, or more than 8 providers are `422`. Full shape:
`GET /openapi.json`.

The push writes `data/ai-usage.json` (below) plus a top-level `received_at`,
then invalidates the cached adapter so `/api/state` reflects it on the very
next build.

### File (fallback)

```sh
AI_USAGE_SOURCE=file
AI_USAGE_PATH=/data/ai-usage.json
```

Schema (see `data-examples/ai-usage.json`):

```json
{
  "providers": [
    {
      "provider": "Claude",
      "short_window_percent_remaining": 72,
      "short_window_reset_at": "2026-09-04T13:00:00+07:00",
      "weekly_percent_remaining": 48,
      "weekly_reset_at": "2026-09-08T09:00:00+07:00",
      "collected_at": "2026-09-04T08:35:00+07:00",
      "collection_status": "ok"
    }
  ]
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `provider` | string | Shown uppercased on the Today page. Required. |
| `short_window_percent_remaining` | int or null | The 5 hour window. `null` renders as `unknown`. |
| `short_window_reset_at` | ISO 8601 or null | Naive values in a hand-written file are read as `TIMEZONE` (the push endpoint instead rejects a naive value; see above). |
| `weekly_percent_remaining` | int or null | The 7 day window. |
| `weekly_reset_at` | ISO 8601 or null | |
| `collected_at` | ISO 8601 or null | When the collector ran. Also the age `AI_USAGE_STALE_SECONDS` measures from (the oldest provider), see `docs/ARCHITECTURE.md`. |
| `collection_status` | string | `ok`, `unknown` or `error`. Anything other than `ok` makes the page print `unknown` instead of the numbers. |
| `received_at` | ISO 8601, optional | Written by the push endpoint; a hand-authored file can omit it and the adapter falls back to the file's own mtime. |

A bare JSON array of provider objects is also accepted for a hand-authored
file (the push endpoint always writes the object form). Write the file
atomically (write a temporary file next to it, then rename) so the hub never
reads half a file.

---

## AI brief: `data/brief/`

Opening the brief page never triggers an AI request. The hub only shows
whatever it was given, by push or by file.

Mode is chosen by the local clock: morning before `BRIEF_EVENING_HOUR`
(default 14), evening from that hour on, unless the push names a mode.

### Push (primary)

```sh
curl -s -X POST http://deskmate.local:8080/api/brief \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"headline": "Two hard deadlines today", "note": "Vendor quote is one day overdue.",
       "sections": [{"title": "Key tasks", "items": ["Ship it"]}]}'
```

Requires the bearer token. `mode` (`morning`/`evening`) is optional and
defaults to what the clock would pick. `headline` 1-120 chars, `note`
optional up to 280, `sections` 0-6 of `{title 1-40, items 0-12 of up to 160
chars}`, `generated_at` optional (a UTC-offset datetime; a naive value is
`422`), defaults to the moment of the push. Response `200`:
`{"stored": "current.json", "received_at": "<local ISO>", "count": <n
sections>, "effective_source": "file"|"fixture"}`, plus `"warning"` when
`BRIEF_SOURCE` is pinned to `fixture`. Writes `data/brief/current.json`
(below) and invalidates the cached adapter. Full shape: `GET /openapi.json`.

### File (fallback)

```sh
BRIEF_SOURCE=file
BRIEF_DIR=/data/brief
BRIEF_EVENING_HOUR=14
```

Lookup order:

1. `data/brief/current.json`
2. `data/brief/morning.md` or `data/brief/evening.md`, whichever matches the mode

### JSON (`current.json`)

```json
{
  "mode": "morning",
  "generated_at": "2026-09-04T06:30:00+07:00",
  "headline": "Two hard deadlines today, storms from 15:00",
  "note": "Vendor quote is one day overdue.",
  "sections": [
    { "title": "Today's schedule", "items": ["09:30 standup", "16:30 demo"] }
  ]
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `mode` | `morning` or `evening` | Defaults to `morning`. |
| `generated_at` | ISO 8601 or null | Falls back to the file mtime. Also the age `BRIEF_STALE_SECONDS` measures from, see `docs/ARCHITECTURE.md`. |
| `headline` | string | One line, shown large. Keep it under about 60 characters. |
| `note` | string | One sentence. Also used as the AI NOTE bar on the Today page. |
| `sections` | list | The Brief page has room for 9 lines total (`BRIEF_MAX_LINES` in `app/view.py`); each section title and each item counts as one line, and whatever does not fit past that budget is cut. |
| `received_at` | ISO 8601, optional | Written by the push endpoint; a hand-authored file can omit it. |

The file may also hold both modes at once, as
`{"morning": {...}, "evening": {...}}`; the hub picks by the clock.

Suggested sections. Morning: today's schedule, key tasks, suggested focus,
things at risk, unfinished work from yesterday. Evening: work completed, open
tasks, things that changed, what should happen tomorrow.

### Markdown (`morning.md` / `evening.md`)

```markdown
# Headline goes here

One sentence that becomes the note.

## Today's schedule

- 09:30 standup
- 16:30 client demo
```

`#` is the headline, the first plain paragraph is the note, `##` starts a
section, `-` / `*` / `+` are items.

Write atomically: `tmp` file plus rename. The brief adapter re-reads at most
every `BRIEF_TTL_SECONDS` (default 60).

---

## Home Assistant: REST

```sh
HA_SOURCE=rest
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<long-lived access token>
HA_ENTITIES={"front_door":"binary_sensor.front_door","nas":"binary_sensor.nas_online"}
```

The hub calls `GET {HA_URL}/api/states` once per refresh with
`Authorization: Bearer <HA_TOKEN>` and picks out the entities named in
`HA_ENTITIES`. The token stays on the server; it is never sent to the E1002.

`HA_ENTITIES` is a JSON object mapping a dashboard slot to an entity id.

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
to a local SQLite file. There is no polling and nothing to configure but
paths.

```sh
DEVICE_SOURCE=store
#TELEMETRY_DB_PATH=/data/telemetry.sqlite
TELEMETRY_RETENTION_DAYS=30
```

Once the hub is claimed, `POST /api/device/telemetry` requires
`Authorization: Bearer <token or device key>` (`firmware/e1002.yaml` sends
the device key); a `/login` session cookie is never accepted here, only a
bearer credential. Before the hub is claimed the endpoint stays open, like
every other read and the device's own image fetches; see "Auth" in
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
| `page` | string or null, up to 32 chars | Page the device is showing. |
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

Responses: `202` with `{"accepted": true, "received_at": "<local ISO>"}`, or
`400` with `{"accepted": false, "error": ..., "detail": [...]}` when the body is
not JSON or fails validation.

### Reading it back

```sh
curl -s http://127.0.0.1:8080/api/device/telemetry
curl -s "http://127.0.0.1:8080/api/device/history?hours=6"
```

Both need the bearer token, the device key, or a `/login` session cookie
once the hub is claimed.

`GET /api/device/telemetry` returns the newest sample, its `age_seconds` and a
summary (`sample_count`, `oldest`, `newest`, `retention_days`).
`GET /api/device/history?hours=24` returns the samples in that window, folded
into at most 300 evenly sized means (`downsampled` says whether that happened).
`hours` must be greater than 0 and at most 8760.

The device also shows up in `/api/state` as the `device` block, and in
`/healthz` as that block's last known status (`/healthz` never fetches).

### Storage and retention

One file, `{DATA_DIR}/telemetry.sqlite`, one table `telemetry` with an index on
`received_at`, WAL journal so a page render never blocks the device. Timestamps
are stored as fixed-width UTC ISO strings and presented in `TIMEZONE`. Rows
older than `TELEMETRY_RETENTION_DAYS` (default 30) are deleted inside the same
transaction as each insert, so there is no background job to keep alive. At one
sample per 5 minutes that is about 8600 rows per month.

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

`DEVICE_SOURCE=fixture` falls back to `fixtures/device.json` (24 hours at 5
minute spacing) **only while the store is empty**, so the page can be designed
before the device is flashed. The first real sample retires the fixture. Sample
times in that file are `offset_minutes` relative to now rather than absolute
stamps, because a rolling 24 hour window only means anything against the
current clock. Production should set `DEVICE_SOURCE=store`.

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
    url: "http://deskmate.local:8080/api/alert"
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
curl -X DELETE http://deskmate.local:8080/api/alert \
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
2. Add a `POST /api/<name>` endpoint in `app/main.py` that writes atomically
   and calls the matching adapter's `invalidate()`.
3. Add or extend a file adapter under `app/adapters/` plus an `auto` variant
   (with its own pure `resolve()` for `GET /api/hub`) if it should fall back
   to a fixture.
4. In `app/view.py`: give it a label on the page and in the matching
   template; add a `<dataset>_stale` wrapper around `stale_info` if it has a
   staleness threshold; list every page that actually draws it in
   `PAGE_PUSH_DATASETS`; and, if it should ever flag a page's footer with
   `!`, add that check to `window_flags`. **A page missing from
   `PAGE_PUSH_DATASETS` silently gets no DEMO mark and no stale flag for
   that dataset**, even if the page draws it, so double check every page
   that reads the new field, not just the page it is "for".
5. Add one curl example and its cadence here.
