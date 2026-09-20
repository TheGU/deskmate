# Settings

Everything that used to live in `.env` (timezone, task/calendar/weather/AI
usage/brief/Home Assistant/device sources, credentials, cache TTLs) is now
edited on the hub's own web page and stored in `data/deskmate.sqlite`, one
row per section. This document covers the setup wizard, the settings page,
every section's fields and defaults, backup and restore, rotating secrets,
resetting the hub, and the one-time import of a pre-database install.
`.env.example` covers what is left outside the database (`HUB_PORT`, `PUID`,
`PGID`, the Obsidian bind mount, and a few process knobs).

## Who may open these pages

The setup wizard (`/setup/*`) and the settings page (`/settings` and
everything under it: save, save and test, the location search, backup,
restore, rotate) all require an **admin** session: either the bearer token
sent as `Authorization: Bearer <token>`, or a browser signed in at `/login`
with the token (an admin cookie, 7 days). Signing in at `/login` with the
**device key** instead grants a **reader** session (30 days): it can view
`/preview` and the rendered pages, but every settings and wizard route
refuses it with a redirect to `/login`, the same as no credential at all.
The device key is deliberately never an admin credential - it sits in the
E1002's unencrypted flash, so a device that is lost or dumped can never be
used to change a calendar feed, rotate secrets, or restore a database. See
"Auth" in `docs/ARCHITECTURE.md` for the full rule.

An unconfigured hub answers `/setup/*` to nobody (there is nothing to edit
yet: `POST /setup` claims it first) and redirects a browser on any admin
route to `/setup`.

## The setup wizard

`POST /setup` claims a fresh hub (hub name, public base URL) and shows the
bearer token and the device key once, with one button: "Continue to setup".
Whoever claimed the hub was just shown the token on that same page, so this
step also signs them in as admin - no need to paste the token back in at
`/login`.

The wizard walks four sections in a fixed order, each with "Save and
continue", "Save and test" (see below) and "Skip":

1. `/setup/general` - timezone and units
2. `/setup/weather` - source, coordinates (with the location search), TTL
3. `/setup/calendar` - source and ICS feeds
4. `/setup/home` - Home Assistant source, URL, token, entity slots

Every step is optional: skipping one just leaves that section on its
defaults, which is the same honest empty state described below. The last
step's "Save and continue" and every step's "Skip" land on `/settings`,
where the remaining sections (tasks, AI usage, brief, device, alert) are
edited. A configured hub answers `/setup/*` only to an admin; there is no
way to re-run the wizard from a browser without a database reset (see
"Reset" below).

## The settings page

`/settings` lists every section in the same order as the wizard plus the
sections the wizard does not ask for, then Backup, Restore and the Danger
zone (rotate). Each section is its own form, generated from that section's
pydantic model (`app/modules/<id>/settings.py` through `app/forms.py`), so
a field's type, label, help text and default all come from one definition.
Saving a section redirects back to `/settings#<section>` with a "saved"
notice; a bad submission re-renders the same page, 422, with each message
next to the input that caused it.

### Sections, fields and defaults

**General** (`general`)

| Field | Default | Notes |
| --- | --- | --- |
| Timezone | `Asia/Bangkok` | IANA name, validated by `ZoneInfo`. Free text: the slim image carries no full tzdata list to build a dropdown from. |
| Units | `metric` | `metric` or `imperial`. |

**Tasks** (`tasks`)

| Field | Default | Notes |
| --- | --- | --- |
| Source | `push` | `push`, `obsidian` or `fixture`. |
| Obsidian vault path | (blank) | Only read when the source is `obsidian`. In Docker, enter `/vault` (the container's mount point), not the host path from `OBSIDIAN_VAULT_PATH`. |
| Obsidian task glob | `**/*.md` | Which files in the vault are scanned. |
| Max priority tasks | `3` | How many open tasks the Today page lists. |
| TTL seconds | `300` | How long a fetched or pushed list is cached. |
| Stale seconds | `36000` | How old a pushed list can get before it is marked stale (`push` source only). |

Until a source is configured: `push` shows nothing until the first
`POST /api/tasks` (see docs/DATA-SOURCES.md); `obsidian` reports
`unavailable` until a vault path is set and mounted; `fixture` always shows
demo data and marks the page DEMO.

**Calendar** (`calendar`)

| Field | Default | Notes |
| --- | --- | --- |
| Source | `ics` | `ics` or `fixture`. |
| Feeds | (empty) | A list of `{url, name, color}` rows. Name left blank falls back to the URL host, then "calendar N". Color is one of blue, green, yellow, red, black; unset cycles blue, green, yellow. |
| Agenda days | `7` | How many days ahead the Agenda page shows. |
| TTL seconds | `300` | How long a fetched calendar is cached. |

`ics` with no feeds reports `unavailable`, the same as `ics` with a feed
that fails to fetch (the page still renders, marked accordingly).

**Weather** (`weather`)

| Field | Default | Notes |
| --- | --- | --- |
| Source | `open_meteo` | `open_meteo` or `fixture`. |
| Latitude, Longitude | (blank) | Both required for `open_meteo` to report anything but `unavailable`. |
| Location name | (blank) | Label shown on the Weather page. |
| TTL seconds | `900` | How long a fetched forecast is cached. |

**AI usage** (`ai_usage`)

| Field | Default | Notes |
| --- | --- | --- |
| Source | `push` | `push` or `fixture`. |
| TTL seconds | `300` | How long a pushed snapshot is cached. |
| Stale seconds | `21600` | How old the newest pushed sample can get before it is marked stale. |

Until the first `POST /api/ai-usage`, `push` shows `unavailable`.

**Brief** (`brief`)

| Field | Default | Notes |
| --- | --- | --- |
| Source | `push` | `push` or `fixture`. |
| Evening hour | `14` | Local hour (0-23) the brief switches from morning to evening mode. |
| TTL seconds | `60` | How long a pushed brief is cached. |
| Stale seconds | `36000` | How old the pushed brief can get before it is marked stale. |

**Home** (`home`)

| Field | Default | Notes |
| --- | --- | --- |
| Source | `rest` | `rest` or `fixture`. |
| URL | (blank) | Base URL of the Home Assistant instance, for example `http://192.168.1.50:8123`. |
| Token | (blank, secret) | Long-lived access token. |
| Entities | the panel's built-in slot map | A list of `{slot, entity_id}` rows; see docs/DATA-SOURCES.md for the slot names. |
| TTL seconds | `120` | How long a fetched snapshot is cached. |

`rest` with no URL, or no token, reports `unavailable`.

**Device** (`device`)

| Field | Default | Notes |
| --- | --- | --- |
| Source | `store` | `store` or `fixture`. |
| Retention days | `30` | How many days of posted telemetry are kept. |
| TTL seconds | `60` | How long the latest summary is cached. |
| Refresh minutes | `30` | How often the device asks the hub for a fresh panel image. Sent to the device on its next telemetry post and applied without a reflash (5 to 240). |
| Telemetry minutes | `5` | How often the device posts a telemetry sample. Applied the same way (1 to 60). |
| Wake hours | `8, 12, 17` | Local hours the device wakes at on battery power, comma separated (0 to 23, any of the 24 hours). Applied the same way. More wake hours cost more battery: each wake is roughly two to three minutes of radio time. |

`store` reports `unavailable` (System page: "NO DEVICE DATA YET") until the
E1002 posts its first sample; this is the honest production setting, since
`fixture` would otherwise hide a device that never came online.

Refresh minutes, Telemetry minutes and Wake hours used to be
`firmware/e1002.yaml` substitutions, compiled in and only changed by a
reflash. They are now this section's settings instead: the telemetry POST
response carries them (docs/DATA-SOURCES.md, "Device telemetry"), and the
device stores whatever it was last told in restorable globals and applies
it right away. The compiled substitutions still exist, as the defaults a
freshly flashed device uses before its first telemetry post; see
docs/FLASHING.md.

**Alert** (`alert`)

| Field | Default | Notes |
| --- | --- | --- |
| Default duration seconds | `90` | Used when `POST /api/alert` does not specify its own `duration_seconds`. |

**Modules** (`modules`)

One row per installed module, whether it ships with the hub, was installed
as a package, or was dropped into `DATA_DIR/modules/`.

| Field | Default | Notes |
| --- | --- | --- |
| ID | (the module's own) | The id its package reports. Also the `/display/<id>.png` segment. |
| Enabled | the module's manifest default | Unticked: its page leaves the window list and `/display/<id>.png` answers 404, and its datasets stop being fetched. |
| Order | the module's manifest default | Where it sits in the window list, which is also what `/display/<n>.png` counts. |

A module with no row yet shows its manifest defaults, so installing one is
enough to see it here. Saving the section reloads the hub: the window list,
the index URLs, the telemetry response's `page_count` and `pages`, and the
render cache all follow immediately, with no restart. Disabling a module
never touches its own settings section: that section stays on this page,
with its stored values, so the module comes back as it was.

Disabling a module that provides a *dataset* rather than a page leaves the
pages that draw it rendering, with that block saying `unavailable`. Never a
guess, and never a blank page.

Two rules the form enforces:

- At least one module with a page has to stay enabled, `today` included.
  Disabling the last one is refused with the reason next to the rows: a
  panel asking for `/display/0.png` on a hub with no page would get a 404
  and keep its last image forever.
- A row naming a module that is not installed here is kept, not dropped,
  and the section says so in a warning: an id in the database is the
  owner's intent, and a module can come back after an upgrade.

There is no `fixture` default anywhere on this page: a fresh hub with
nothing configured shows every block's honest empty state, never demo data.
`fixture` is always available as an explicit choice for development or a
live demo.

Every row here runs with the hub's own privileges (see docs/MODULES.md):
enabling a row is not a sandboxed permission, it is running that module's
code with this hub's database, data directory and network.

**HA Dashboard** (`ha_dashboard`, off by default)

| Field | Default | Notes |
| --- | --- | --- |
| Dashboard url | (blank) | A full Lovelace view URL, for example `http://192.168.1.50:8123/lovelace-kiosk/0?kiosk`. Must start with `http://` or `https://` when set. |
| Token | (blank, secret) | Long-lived access token for that Home Assistant instance. |
| Settle ms | `2000` | How long to wait after the page loads before it is screenshotted. 0-4000. |
| Ttl seconds | `300` | How long a rendered screenshot is cached before it is taken again. |

This page has no dataset of its own and no Source field: it screenshots the
configured URL directly instead of drawing a template from fetched data (see
docs/HA-DASHBOARD.md), so there is no "Save and test" button for it either -
saving the section is the only way to see whether it works, on the panel or
through `/display/ha_dashboard.png`. It is off by default because an
unconfigured or wrong dashboard URL is a page that has nothing honest to
show; enable it once the URL and token are set.

### Save and test

Every section with a Source field (tasks, calendar, weather, ai_usage,
brief, home, device - not general, alert or ha_dashboard) has a "Save and
test" button next to "Save": it saves the section exactly like "Save" does,
then runs one live fetch through that section's adapter and prints the
result on the page - the adapter's status (`ok`, `error`, `unavailable`,
...) and its error text, if any. That is what tells you an ICS URL or a
Home Assistant token is wrong before you leave the page, rather than after
the fact on the rendered panel.

### The location search

The weather section (both in the wizard and on the settings page) has a
"Find" box: type a place name and submit (a plain GET, no JavaScript) to
query Open-Meteo's geocoding API and get back up to 8 matches as radio
buttons. Choosing one and pressing Save fills latitude, longitude and the
location name. Coordinates can also be typed in directly; the search is a
convenience, not a requirement. A search failure shows one line under the
box and never blocks typing the numbers in by hand.

### Secrets

The Home Assistant token (and any future secret field) is stored in the
database as its real value - this database is the hub's own secret store,
not a place secrets get masked - but it is never echoed back into a form.
The password box on the page is always blank:

- Leave it blank and press Save: the stored value is kept unchanged.
- Tick the "clear" checkbox next to it and press Save: the stored value is
  emptied.
- Type a new value: it replaces the stored one.

There is no way to view a stored secret's value again through the settings
page; if you have lost track of a Home Assistant token, generate a new one
in Home Assistant and paste it in here.

## Backup

`POST /settings/backup` (a form button on the settings page, admin only)
downloads the hub's entire database as one file,
`deskmate-backup-<UTC timestamp>.sqlite`. It is a consistent point-in-time
copy (`VACUUM INTO`, not a plain file copy of the live database), served
with `Cache-Control: no-store` and deleted from the server the moment the
download finishes.

**The backup file is a credential.** It carries the session secret and the
hashes of the bearer token and device key, plus any secret stored in a
section (the Home Assistant token). Treat it exactly like the bearer token:
store it somewhere only you can read, never attach it to a public issue or
chat.

The backup does **not** carry `DATA_DIR/modules/` (any locally installed
module package) or an Obsidian vault - both are files on disk beside the
database, not rows in it. Restoring a backup on a different install still
needs those, if you use them, put back separately.

## Restore

`POST /settings/restore` (admin, multipart upload, 64 MiB cap) replaces the
hub's database with an uploaded file. The upload is validated before
anything is touched: it must be a readable SQLite file, pass
`PRAGMA integrity_check`, carry a database schema version no newer than
this build understands, and have a `hub` row. A file that fails any of
those checks is refused with a 422 and the reason on the page; the live
database is left exactly as it was.

Two warnings sit above the confirmation checkbox, and both matter:

- **The token and device key become the backup's.** If the backup's device
  key hash differs from the one currently in use, the flashed E1002 stops
  fetching pages and posting telemetry until its own Hub key field is
  updated to match (its own web page, or its Home Assistant text entity;
  see docs/DEPLOY.md - no reflash needed on firmware with the runtime
  `hub_key` field, a reflash only on older firmware).
- **Every browser session ends.** The backup's session secret is not this
  hub's, so every cookie this hub ever signed - including the one you are
  reading this page with - stops verifying the moment the swap happens.

A restore that passes validation swaps the file in (close the live
connection, remove its WAL/SHM sidecars, replace the file, reopen, migrate,
reload every in-memory service), then sends the browser to `/login` with
its cookie cleared. `/login` shows a notice that the database was restored,
and whether the device key changed, so you know to update the device
before you rely on the panel again. Sign in with whatever credential the
restored backup actually holds.

## Rotate secrets

`POST /settings/rotate` (admin, its own confirmation checkbox, in the
Danger zone) mints a fresh bearer token, device key and session secret, and
shows the two new secrets once - the same "copy them now, there is no way
to see them again" rule as the first setup. What changes:

- The old token and device key stop verifying immediately.
- Every browser session ends (a fresh admin cookie is issued for the page
  you are already on, so you are not locked out mid-flow).
- **The flashed device stops fetching** until its Hub key field is updated
  to the new device key (its own web page, or the Home Assistant text
  entity - see docs/DEPLOY.md). No reflash needed on firmware with the
  runtime `hub_key` field; a reflash is only the fallback for firmware
  built before it existed.

Every agent or hook that pushes to the hub (`skills/deskmate/SKILL.md`,
`docs/LOCAL-AGENT.md`, `docs/HOOKS.md`) also needs its saved token updated
after a rotate.

## Reset

There is no in-place "unclaim" or "edit identity" action. To reset a hub
completely: stop the container, delete `data/deskmate.sqlite` (and its
`-wal`/`-shm` sidecars if present), and start it again. This is the same
first-come-first-served `/setup` race as a brand new install, and it throws
away everything - settings, pushed datasets, telemetry history, not just
the identity - so back it up first (see Backup above) if any of that is
worth keeping.

```sh
docker compose down
rm data/deskmate.sqlite data/deskmate.sqlite-wal data/deskmate.sqlite-shm
docker compose up -d
```

## The one-time legacy import

An install from before this database existed kept its identity in
`data/hub.json`, its telemetry in `data/telemetry.sqlite`, its pushed
datasets in `data/ai-usage.json`, `data/tasks.json`,
`data/brief/current.json` and `data/alert.json`, and everything else in
`.env`. The first time this version starts against such a `DATA_DIR`, it
imports all of it into `data/deskmate.sqlite` once, automatically: the hub
comes up already claimed, with its push history, its sources and its
location, and the device keeps fetching without you touching anything. See
"Upgrading an existing install" in docs/DEPLOY.md for the full sequence.

**Nothing on disk is renamed or deleted by the import.** The old files stay
exactly where they were, so a rollback to the previous image still finds
`hub.json` and does not think the hub is unclaimed. Once you have confirmed
the upgrade on `/settings` - the sections you had configured are there, the
device is still fetching - delete the old files by hand:

```sh
rm data/hub.json data/telemetry.sqlite data/ai-usage.json data/tasks.json \
   data/alert.json
rm -rf data/brief
```

Leaving them in place costs nothing (they are never read again once the
import has run), but they no longer mean anything once `deskmate.sqlite`
is the truth, so deleting them avoids confusing a future you.
