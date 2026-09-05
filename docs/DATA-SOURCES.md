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
```

---

## Overview

| Adapter | Sources | Configuration |
| --- | --- | --- |
| tasks | `fixture`, `obsidian` | `TASKS_SOURCE`, `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TASK_GLOB` |
| calendar | `fixture`, `ics` | `CALENDAR_SOURCE`, `CALENDAR_ICS_URLS` |
| weather | `fixture`, `open_meteo` | `WEATHER_SOURCE`, `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, `WEATHER_LOCATION_NAME` |
| ai_usage | `fixture`, `file` | `AI_USAGE_SOURCE`, `AI_USAGE_PATH` (default `data/ai-usage.json`) |
| ai_brief | `fixture`, `file` | `BRIEF_SOURCE`, `BRIEF_DIR` (default `data/brief`), `BRIEF_EVENING_HOUR` |
| home_assistant | `fixture`, `rest` | `HA_SOURCE`, `HA_URL`, `HA_TOKEN`, `HA_ENTITIES` |
| device | `store`, `fixture` | `DEVICE_SOURCE`, `TELEMETRY_DB_PATH`, `TELEMETRY_RETENTION_DAYS` |

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

## Tasks: Obsidian (read only)

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
```

Comma separated. Each entry is either an `http(s)` URL or a local file path.
Simple `RRULE` recurrences are expanded inside the agenda window only (one day
back, three weeks forward, at most 50 occurrences per event); `EXDATE` is
honoured. All-day events keep `all_day: true`.

Normalized fields: `id`, `title`, `start`, `end`, `all_day`, `location`,
`source`.

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

## AI usage / quota: `data/ai-usage.json`

There is no supported public API for Claude or Codex quota, and the hub does no
browser or credential scraping. A separate collector writes a JSON file; the hub
only reads it.

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
| `short_window_reset_at` | ISO 8601 or null | Naive values are read as `TIMEZONE`. |
| `weekly_percent_remaining` | int or null | The 7 day window. |
| `weekly_reset_at` | ISO 8601 or null | |
| `collected_at` | ISO 8601 or null | When the collector ran. |
| `collection_status` | string | `ok`, `unknown` or `error`. Anything other than `ok` makes the page print `unknown` instead of the numbers. |

A bare JSON array of provider objects is also accepted. Write the file
atomically (write a temporary file next to it, then rename) so the hub never
reads half a file.

---

## AI brief: `data/brief/`

Opening the brief page never triggers an AI request. The hub reads whatever is
already on disk.

```sh
BRIEF_SOURCE=file
BRIEF_DIR=/data/brief
BRIEF_EVENING_HOUR=14
```

Mode is chosen by the local clock: morning before `BRIEF_EVENING_HOUR`, evening
from that hour on.

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
| `generated_at` | ISO 8601 or null | Falls back to the file mtime. |
| `headline` | string | One line, shown large. Keep it under about 60 characters. |
| `note` | string | One sentence. Also used as the AI NOTE bar on the Today page. |
| `sections` | list | At most 4 are drawn, each with at most 3 items. |

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

This is the only adapter whose data flows the other way: the reTerminal E1002
POSTs one sample every 5 minutes and the hub appends it to a local SQLite file.
There is no polling, no device credential and nothing to configure but paths.

```sh
DEVICE_SOURCE=store
#TELEMETRY_DB_PATH=/data/telemetry.sqlite
TELEMETRY_RETENTION_DAYS=30
```

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

`GET /api/device/telemetry` returns the newest sample, its `age_seconds` and a
summary (`sample_count`, `oldest`, `newest`, `retention_days`).
`GET /api/device/history?hours=24` returns the samples in that window, folded
into at most 300 evenly sized means (`downsampled` says whether that happened).
`hours` must be greater than 0 and at most 8760.

The device also shows up in `/healthz` and `/api/state` as the `device` block.

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
  "beep": true
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string, 1 to 60 chars | Required. Drawn very large. |
| `message` | string, up to 240 chars | Optional, drawn below the title. |
| `priority` | `critical`, `doorbell`, `important`, `normal` | Default `normal`. |
| `duration_seconds` | int, 5 to 600 | Default 90. How long the device shows it. |
| `beep` | bool | Default true. Whether the device buzzes. |

Responses: `201` with `{"accepted": true, "alert": {...}}`, or `409` with
`{"accepted": false, ...}` when a higher priority alert is still active
(`critical > doorbell > important > normal`; equal priority replaces).
`DELETE /api/alert` clears it and returns `{"cleared": true|false}`.

### Home Assistant example

`configuration.yaml`:

```yaml
rest_command:
  deskmate_alert:
    url: "http://dashboard-hub.lan:8080/api/alert"
    method: POST
    content_type: "application/json"
    payload: >-
      {"title": "{{ title }}", "message": "{{ message }}",
       "priority": "{{ priority }}", "duration_seconds": {{ duration }},
       "beep": {{ beep | lower }}}
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
    sequence:
      # 1. The hub renders the alert page first.
      - action: rest_command.deskmate_alert
        data:
          title: "{{ title }}"
          message: "{{ message | default('') }}"
          priority: "{{ priority | default('normal') }}"
          duration: "{{ duration | default(90) | int }}"
          beep: "{{ beep | default(true) }}"
        response_variable: hub
      # 2. Only then does the device download and show it.
      - condition: template
        value_template: "{{ hub.status in [200, 201] }}"
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
```

The device shows `/display/alert.png`, keeps it for `duration` seconds, then
returns to the page it was on. Clear the alert from the hub afterwards if you
want the page to go back to "no active alert":

```sh
curl -X DELETE http://dashboard-hub.lan:8080/api/alert
```

The `show_alert` ESPHome API action is defined in `firmware/e1002.yaml`
(Phase 2). It is listed here so the Home Assistant side can be written and
reviewed before the device is flashed.
