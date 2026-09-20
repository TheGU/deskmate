# SQLite settings, page modules, HA dashboard, firmware provisioning

Status: in progress. See the Status table at the end. Reviewed once
(senior review 2026-09-19); the review's changes are folded in below.

## Goal

Make dashboard-hub configurable from its own web UI and extensible by
other developers:

1. One SQLite file holds everything the hub owns (identity, settings,
   pushed datasets, telemetry). A setup wizard and a settings page replace
   the `.env` variables. Backup and restore are one file.
2. Every panel page is a module: a Python package that brings its own
   settings, adapters, template, context builder, fixture and tests. The
   five pages become the default modules; a third party can add one.
3. A Home Assistant module that shows one Lovelace dashboard the user built
   at panel size, and firmware provisioning of Wi-Fi, hub URL and device
   key at runtime, so moving the hub or rotating a key never needs a
   reflash.

Owner feedback covered: items 3, 5, 8, 9, 10, 11 of the 2026-09-19 review
and the deployer's "hub URL and Wi-Fi baked into the firmware" point.

## Non-goals

- Configurable screen size. The panel is 800x480 throughout; a different
  panel is the next plan.
- A JSON settings API for agents. Settings are edited in the browser only.
- Installing or upgrading a module from the browser. A module is installed
  by placing a package under `DATA_DIR/modules/` (or `pip install` of a
  package that exposes the entry point) and restarting the container; the
  settings page then shows it and can enable, disable and order it.
  "Update features from the website" is read as changing each feature's
  settings, which the settings page does.
- Keeping the `auto` and `file` source selectors for the pushed datasets.
  `auto` existed to show demo data until the first push; `file` was the
  fallback for a tool that only writes files. With pushes stored in the
  database there is one path: `push`. Demo data is the explicit `fixture`
  choice on the settings page.

## Design

### Phase 1: one database, settings, wizard, backup

**File.** `DATA_DIR/deskmate.sqlite`, opened through a process-wide
registry keyed by path (`app/db.py:get_database`, the same pattern as
`telemetry.py:get_telemetry_store` today, with a `close_databases()` hook
for tests), WAL, `check_same_thread=False`, one `threading.Lock`, every
write commits. `Hub` holds a reference; it does not own the lifetime.
Tables:

| Table | Columns | Replaces |
| --- | --- | --- |
| `meta` | `key` PK, `value` | `db_schema_version` (starts at 1; unrelated to `HUB_CONFIG_SCHEMA`, the old `hub.json` number), `legacy_imported_at` |
| `hub` | `id` (always 1), `name`, `base_url`, `token_sha256`, `device_key_sha256`, `session_secret`, `created_at` | `data/hub.json` |
| `settings` | `section` PK, `value_json`, `updated_at` | every `*_SOURCE`, location, calendars, HA, TTLs |
| `datasets` | `name` PK, `payload_json`, `received_at` | `data/ai-usage.json`, `tasks.json`, `brief/current.json`, `alert.json` |
| `telemetry` | unchanged from `app/telemetry.py` | `data/telemetry.sqlite` |

`Database.migrate()` creates missing tables and bumps `db_schema_version`.
A database newer than the code refuses to start with one clear line.

**Import of the old files and the old environment**, once, at startup
(`app/legacy.py:import_legacy`), gated by `meta.legacy_imported_at`:

- `hub.json` into `hub`; `telemetry.sqlite` (at the path the old
  `TELEMETRY_DB_PATH` resolved to) into `telemetry` by ATTACH and copy; the
  pushed files (`ai-usage.json` at the old `AI_USAGE_PATH`, `tasks.json`,
  `brief/current.json` under the old `BRIEF_DIR`, `alert.json`) into
  `datasets`.
- The old environment variables, read once through the old
  pydantic-settings class kept as `app/legacy.py:LegacyEnv` (same names
  and defaults as today's `config.py:Settings`), into the section rows:
  `TIMEZONE`, `UNITS`, every `*_SOURCE` (with `auto` and `file` mapped to
  `push`), `CALENDAR_ICS_URLS`/`NAMES`/`COLORS` into feeds, `WEATHER_*`,
  `HA_*`, `OBSIDIAN_*`, the TTLs, the stale thresholds,
  `BRIEF_EVENING_HOUR`, `MAX_PRIORITY_TASKS`, `AGENDA_DAYS`,
  `ALERT_DEFAULT_DURATION_SECONDS`, `TELEMETRY_RETENTION_DAYS`.
- Nothing is renamed or deleted: the files stay on disk so a rollback to
  the previous image still finds `hub.json` and does not re-claim the hub.
  docs/DEPLOY.md says to delete them by hand once the upgrade is confirmed.
- Every import is logged at INFO with what it took from where.

The deployed hub therefore comes up claimed, with its history, its
sources and its location, and the device keeps fetching.

**Settings model.** One pydantic model per section, so the form, the
validation and the defaults come from one definition. Phase 1 puts each
section model where phase 2 will find it, in `app/modules/<id>/settings.py`.
Defaults are the honest live selector, never `fixture`, so a fresh hub
shows an empty state and never demo data (`test_defaults.py` keeps
asserting this):

| Section | Fields (default) |
| --- | --- |
| `general` | `timezone` (validated IANA, `Asia/Bangkok`), `units` (`metric`) |
| `tasks` | `source` (`push`, `obsidian`, `fixture`; default `push`), `obsidian_vault_path`, `obsidian_task_glob`, `max_priority_tasks`, `ttl_seconds`, `stale_seconds` |
| `calendar` | `source` (`ics`, `fixture`; default `ics`), `feeds: list[Feed]`, `Feed = {url, name, color}`, `agenda_days`, `ttl_seconds` |
| `weather` | `source` (`open_meteo`, `fixture`; default `open_meteo`), `latitude`, `longitude`, `location_name`, `ttl_seconds` |
| `ai_usage` | `source` (`push`, `fixture`; default `push`), `ttl_seconds`, `stale_seconds` |
| `brief` | `source` (`push`, `fixture`; default `push`), `evening_hour`, `ttl_seconds`, `stale_seconds` |
| `home` | `source` (`rest`, `fixture`; default `rest`), `url`, `token` (secret), `entities: list[EntitySlot]`, `EntitySlot = {slot, entity_id}`, `ttl_seconds` |
| `device` | `source` (`store`, `fixture`; default `store`), `retention_days`, `ttl_seconds` |
| `alert` | `default_duration_seconds` |

`app/settings.py:HubSettings` is the composite (one attribute per section)
and the object every adapter, context builder and route reads.
`app/settings.py:SettingsStore` loads a section (row JSON validated by its
model, a missing row means defaults), saves one, and returns a
`HubSettings` snapshot. A `SecretStr` field is rendered as a password
input, never echoed back; an empty submission keeps the stored value and a
"clear" checkbox next to it empties it.

**What stays in the environment** (`app/config.py:Env`): `DATA_DIR`,
`LOG_LEVEL`, `RENDER_TIMEOUT_MS`, `HTTP_TIMEOUT_SECONDS`,
`FIXTURE_RELATIVE_DATES` (a demo knob), and in phase 1 only `FIXTURES_DIR`
(phase 2 moves fixtures into the modules). Compose keeps `HUB_PORT`,
`PUID`, `PGID` and the host path for the Obsidian bind mount. Everything
else in `.env.example` goes away.

**Runtime reload.** `Hub.reload()` (`app/main.py`, lands in 1.1) re-reads
the settings snapshot, rebuilds `HubIdentity` from the `hub` row, rebuilds
`StateService`, re-reads (never resets) the alert row, and drops the render
cache under `_cache_lock`. A render already in flight finishes on the old
service; the next request sees the new one.

**Identity in the database.** `app/hub_config.py` keeps its pure functions;
`load_hub_config` / `write_hub_config` read and write the `hub` row;
`HubIdentity(db)`. The rules do not change: first `POST /setup` from a
private address claims the hub, the two secrets are shown once, `GET
/setup` on a configured hub reveals nothing.

**Roles.** The session cookie gains a role: signing in with the bearer token
yields `admin`, with the device key `reader`. Cookie payload
`"<role>|<exp>"`, HMAC as today; a cookie in the old `browser|` format is
rejected (every browser is logged out once by the upgrade, DEPLOY says
so). Admin cookies last 7 days, reader cookies 30. The device key sits in
unencrypted flash, so it must never change the Home Assistant token or the
calendars: `require_admin` and `require_admin_html` accept the admin cookie
or the bearer token, checked with `verify_token` only, never
`verify_reader`. The cookie is `SameSite=Lax`, so a cross-site form cannot
post to a settings route with it; every state-changing settings action is
a POST, including the backup download, so a cross-site top-level GET can
never trigger one.

**Wizard.** `POST /setup` claims the hub as today, sets the admin cookie
(whoever won the claim was shown the token anyway, so asking them to paste
it back adds nothing) and shows the two secrets once, with one button:
"Continue to setup". The steps are the settings section forms in a fixed
order, `/setup/general`, `/setup/weather`, `/setup/calendar`,
`/setup/home`, each with "Save and continue", "Save and test" and "Skip",
ending at `/settings`. Every step after the claim is optional; a skipped
source shows its honest empty state. A configured hub answers `/setup/*`
only to an admin.

**Save and test.** Every section with a source has a "Save and test"
button: the section is saved, one live fetch of its adapter runs, and the
page prints the adapter's `Outcome.status` and `error` string
(`adapters/base.py:Outcome`) next to the form. That is what tells the
user an ICS URL or an HA token is wrong before they leave the wizard.

**Location search.** The weather step and section have a "Find" form
(plain GET, no JavaScript, admin only, query capped at 80 characters,
upstream host a constant) that queries Open-Meteo's geocoding API
(`https://geocoding-api.open-meteo.com/v1/search?name=<q>&count=8`) and
lists the results as radio buttons; choosing one fills latitude, longitude
and name. Coordinates can also be typed directly.

**Settings page.** `/settings`: general, one section per dataset, then
Backup and Danger zone. Forms are generated from the section model
(`app/forms.py`): `str`, `int`, `float`, `bool`, `Literal`, `SecretStr`,
and `list[<model>]` (rendered as indexed rows, three blank rows for adding,
a delete checkbox per row). Rules the generator follows:

- Every checkbox is paired with a hidden input of the same name carrying
  `0`, and the parser takes the last value (`form.getlist(name)[-1]`),
  never the first.
- List rows use indexed names (`feeds-0-url`), reassembled by index, with a
  server-side cap of 20 rows that yields a field error.
- `ValidationError` locations such as `("feeds", 0, "url")` map back to the
  input name so the message sits next to the field.
- An annotation outside the supported set raises at registry load, never
  a blank input.
- Timezone stays free text validated by `ZoneInfo` (no dropdown; the slim
  image has no full tzdata list).
- Settings POSTs have the same 64 KiB body cap as `/setup`.

No JavaScript anywhere in the admin pages.

**Backup and restore.** `POST /settings/backup` (admin, a form button):
`VACUUM INTO` a fresh temp name (fails if the name exists, so never reuse
one), streamed as `deskmate-backup-<utc timestamp>.sqlite` with
`Cache-Control: no-store`. The page and docs/SETTINGS.md say in one line
that the file is a credential (session secret, HA token, hashes): treat it
like the token. It does not carry `DATA_DIR/modules/` or an Obsidian vault.

`POST /settings/restore` (admin, multipart streamed through `UploadFile`
with a byte counter, 64 MiB cap): the upload is written out of place,
opened read-only, `PRAGMA integrity_check` must say `ok`,
`db_schema_version` at most the current one, the `hub` row present. The
form shows two warnings before the confirmation checkbox: the token and
device key become the backup's (if the device key hash differs from the
current one, the flashed device stops fetching until its key is updated;
with the runtime `hub_key` field from the firmware package that is a
browser edit, on older firmware a reflash), and every browser session
ends. Then, under a process-wide write lock: `Database.close()`, unlink
`deskmate.sqlite-wal` and `-shm`, `os.replace`, reopen, `migrate()`,
`Hub.reload()`, redirect to `/login`. A test restores while a render is in
flight.

**Rotate secrets.** `POST /settings/rotate` (admin, confirmation checkbox
with the same device-key warning) mints a new token, device key and
session secret, shows the two secrets once, and ends every session, which
is what the old "delete hub.json" reset did. The full reset (delete the
database) stays documented for a wipe.

**Pushed datasets.** `POST /api/tasks`, `/api/ai-usage`, `/api/brief` write
a `datasets` row and invalidate the adapter. Source `push` reads that row;
`fixture` reads the demo file. The push routes warn in their response when
the section's source is `fixture` ("the panel is showing demo data for
this dataset"), with a test. The alert store persists to the `alert` row.
`GET /api/hub`'s `sources` block becomes `{dataset: {source}}`; `effective`
is gone because nothing is ambiguous any more. `skills/deskmate/SKILL.md`
and docs/LOCAL-AGENT.md are updated to match.

### Phase 2: pages as modules

**Vocabulary.** A *dataset* is a named block of normalized data with an
adapter behind it (`tasks`, `weather`). A *page* is one 800x480 render. A
*module* provides zero or more datasets and at most one page. Core provides
the frame (header, footer, rules), the render engine, the alert page and
API (id `alert` is reserved: never a module, never an index), auth, the
settings machinery, the device routes and telemetry.

**Module API** (`app/modules/__init__.py`):

```python
@dataclass(frozen=True)
class Module:
    id: str                      # ^[a-z][a-z0-9_]*$, never all digits, never "alert"
    title: str                   # footer name, upper case
    version: str
    description: str
    settings_model: type[BaseModel] | None
    datasets: tuple[DatasetSpec, ...] = ()
    page: PageSpec | None = None
    routes: Callable[[ModuleContext], APIRouter] | None = None
    default_order: int = 100
    default_enabled: bool = True

@dataclass(frozen=True)
class DatasetSpec:
    name: str
    block_model: type[Block]
    build_adapter: Callable[[BaseModel, ModuleContext], Adapter]
    ttl_seconds: Callable[[BaseModel], float]
    fixture: Path | None

@dataclass(frozen=True)
class PageSpec:
    template: str | None          # file in the module's templates/ dir
    templates_dir: Path
    context: Callable[[DashboardState, HubSettings], dict[str, Any]]
    render_ttl_seconds: float
    needs: tuple[str, ...]        # dataset names it draws
    demo_datasets: tuple[str, ...]  # DEMO mark when one of these is fixture; pushed datasets only
    flag: Callable[[DashboardState, HubSettings], bool] | None
    screenshot: ScreenshotFn | None   # phase 3: custom RGB renderer
```

Core computes `header` and `footer` through `base_context` and merges them
over the module's context dict after the module ran, so a module cannot
overwrite them. A module's `flag` derives its reference time from
`state.updated_at` and its counts from the blocks it reads; the current
cross-module flags (overdue tasks flag `agenda`, a down service or a stale
device flag `system`, stale pushes flag `today` and `brief`) each move to
the page they flag. `demo_datasets` may only name pushed datasets
(`tasks`, `ai_usage`, `brief`); a fetched dataset on `fixture` never
prints DEMO, as today (`view.py:page_shows_demo_data`).

`ModuleContext` carries `env`, `db`, `data_dir`, `http_timeout_seconds` and
a logger. Push routes a module declares are mounted under `/api/` with the
`require_token` dependency applied by core, so a module cannot forget auth.

**Registry** (`app/modules/registry.py`): built-in modules from
`app/modules/<id>/` (each package exposes `MODULE`), then entry points in
group `deskmate.modules`, then packages under `DATA_DIR/modules/` (added to
`sys.path`). A duplicate id or a bad id refuses to start. The `modules`
settings section is `{id: {enabled, order}}`; an id in settings that is
not installed is shown as a warning on `/settings`, not silently dropped;
new ids get the manifest defaults. `registry.pages()` is the enabled pages
in order, `registry.datasets()` the datasets of enabled modules. The
built-in ids stay exactly `today`, `agenda`, `weather`, `brief`, `system`
with that default order, so a flashed device's `/display/today.png` and its
`page` strings keep lining up.

**State.** `DashboardState` becomes `{schema: 2, generated_at, timezone,
blocks: dict[str, SerializeAsAny[Block]], alert}`. `SerializeAsAny` is
required: without it pydantic serializes every block as the base `Block`
and `/api/state` carries no data (verified in review); a test asserts a
task title survives the round trip. `state.block("tasks", TasksBlock)`
returns the typed block or an `unavailable` placeholder of that type when
the dataset is missing or its module is disabled, so a page never
None-checks. The core header reads the well-known names `weather`,
`tasks`, `device` when present; any module may provide them. The seven
top-level keys of the old `/api/state` are gone; `schema: 2` marks the
break and SKILL.md notes it.

**Built-in modules**: `today` (page), `agenda` (page, dataset `calendar`),
`weather` (page, dataset), `brief` (page, dataset), `system` (page,
datasets `device`, `home`), `tasks` (dataset, push route), `ai_usage`
(dataset, push route). Each carries its fixture under
`app/modules/<id>/fixtures/`, its template under `templates/`, and its
tests under `dashboard/tests/modules/test_<id>.py`. `app/view.py` keeps
the shared helpers (formatting, header, footer, base context, stale rules)
and the layout constants that more than one page uses; it will still be a
sizeable file. Repo-root `fixtures/` and `FIXTURES_DIR` go away.

**Renderer.** The Jinja loader searches core `templates/` then every
enabled module's `templates/` (module templates are named `<id>.html`).
`PAGES` and `PAGE_TTL_SECONDS` come from the registry. A page whose spec has
`screenshot` set is rendered by calling it instead of a template.

**Device.** `/display/{n}.png` with an integer `n` serves the n-th enabled
page (0-based, in settings order): the route resolves `n` to the module id
before the render cache, so the cache key and the `X-Deskmate-Page` header
stay the id; `n` past the end is `404`. `/display/{id}.png` keeps working.
The telemetry response adds `page_count` and `pages` (ids in order). The
firmware keeps `page_count` in a restorable global (default 5, clamped on
boot against a restored `page_index`), reads it from every telemetry
response (`capture_response: true`, `json::parse_json`), builds URLs by
index, and keeps sending `page` as the resolved id (the stored column the
System page draws) plus `page_index`; the hub treats `page` as
authoritative and `page_index` as the fallback. The alternative of wrapping
on a `404` was rejected: `online_image` cannot tell a `404` from a network
failure and cannot read a response header. `alert` is never in the list.
The footer window list is the enabled pages, numbered from 1.

**Settings page**: a Modules section listing every module with an enable
checkbox and an order field, plus each enabled module's own section.
Disabling a dataset module leaves the pages that draw it showing
`unavailable`.

### Phase 3: HA dashboard module and firmware provisioning

**`ha_dashboard` module** (page only, no dataset). Settings: `dashboard_url`
(a full Lovelace view URL), `token` (long-lived access token, secret),
`settle_ms` (default 2000, max 4000), `ttl_seconds`. Its `screenshot`
opens a fresh browser context on the shared browser (it holds the render
lock like any page, so its whole path is bounded to 8 s: navigation
timeout 4 s, settle, screenshot), runs an init script that stores
`hassTokens` in `localStorage` for the HA origin (`hassUrl`,
`access_token`, `token_type: Bearer`, the three fields sibbl/hass-lovelace-
kindle-screensaver has always stored; the earlier draft of this plan listed
`expires`, `expires_in` and `clientId`, which upstream never sets) plus
`selectedLanguage`, navigates to the dashboard at 800x480 with the 4x device
scale factor, waits, screenshots, and hands the RGB image to the usual
quantize step. A timeout, a network error or a login page (URL containing
`/auth/`) becomes an error render: the frame with a hatch box and the
reason, never a stale screenshot. The URL and the init script are never
logged (the token is in them). Docs say plainly that an arbitrary
dashboard quantized to six inks looks rough and how to build one that does
not (large type, flat colours, no gradients). The REST `home` dataset stays
as the System page's source.

**Firmware provisioning** (`firmware/e1002.yaml`), facts confirmed against
ESPHome 2026.8.2 (the installed version) on 2026-09-19:

- `hub_base_url` and `hub_key` become `text` components (template, `mode:
  text` and `mode: password`, `restore_value: true`, `optimistic: true`,
  `max_length: 255`), seeded from `secrets.yaml` on first boot only. Every
  URL and header is built from `.state`; `online_image.request_headers`
  and `http_request.post.request_headers` values are templatable lambdas.
- Wi-Fi: `wifi:` may omit `ssid` and `password` when `ap:` is set; the
  captive portal saves entered credentials to NVS and they survive OTA.
  The YAML keeps them optional from `secrets.yaml` for a lab flash.
- `improv_serial:` shares the logger's `UART0` (the CH340 port). It must
  never move to native USB on this board: `USB_SERIAL_JTAG`/`USB_CDC` use
  GPIO19/20, which are the I2C bus for the SHT4x and the RTC.
- `web_server:` (version 3, basic auth from `secrets.yaml`) so a home
  without Home Assistant can set the two text fields from a browser;
  `POST /text/<id>/set?value=` is the setter. The captive portal already
  pulls in the web server base.
- Compiled with `esphome config` and `esphome compile` from PowerShell.
  Not flashed here: the owner flashes.

This package runs first, in parallel with phase 1: it is what makes
rotate and restore browser edits instead of reflash events.

### Docs for other developers

- `CONTRIBUTING.md`: layout, dev setup, tests, lint, plain-ASCII rule, how
  a change is reviewed, how to write a plan.
- `docs/MODULES.md`: the module contract with a complete minimal module
  (`examples/modules/hello/`), how to install one, how to test one.
- `docs/SETTINGS.md`: every section and field, save and test, backup and
  restore (what the file holds and does not), rotate, reset.
- README, ARCHITECTURE, DATA-SOURCES, DEPLOY, LOCAL-AGENT, FLASHING,
  `.env.example`, `docker-compose.yml`, `skills/deskmate/SKILL.md` and
  PRODUCT.md updated for the database, the settings page, module pages and
  the provisioning flow.

## Work packages

Each package is one branch merged to `main` only when its checks pass.
Numbers order the dependencies; the Sequencing section gives the parallel
groups.

### Firmware (parallel with phase 1)

| # | Package | Acceptance |
| --- | --- | --- |
| F.1 | Firmware provisioning: text components for URL and key, optional Wi-Fi credentials, `improv_serial` on UART0, `web_server` v3 with auth, secrets example, FLASHING and DEPLOY sections on the runtime flow | `esphome config` and `esphome compile` pass from PowerShell; the YAML holds no default that reaches the network without the user's own values |

### Phase 1

| # | Package | Acceptance |
| --- | --- | --- |
| 1.1 | `app/db.py` (registry, Database, migrate), telemetry store on the shared connection, `hub` table in `hub_config.py`, `HubIdentity(db)`, `app/legacy.py` file and env import, `Hub.reload()` with identity rebuild and locked cache drop | tests: fresh db has version 1, legacy files and env import once and are left on disk, a newer version refuses to start, claim/verify unchanged, a second `create_app` over the same path shares the Database, reload rebuilds identity; all existing tests green |
| 1.2a | Section models in `app/modules/<id>/settings.py`, `HubSettings`, `SettingsStore`, `HubSettings.from_env(Settings)`, a `hub_settings` conftest fixture | no call site changes; section defaults asserted in `test_defaults.py`; all tests green |
| 1.2b | Adapters, view, state, renderer, main read `HubSettings` + `Env`, still built through `from_env`; `DashboardState` unchanged | byte-identical PNGs for the fixture state; all tests green |
| 1.2c | `SettingsStore` becomes the source of `HubSettings`; `Env` shrinks; `Settings` and `from_env` deleted; conftest seeds fixture sources through the store | `Settings` no longer exists; tests green |
| 1.2d | Drop `auto` and `file`; `push` source and `datasets` rows for tasks, ai_usage, brief, alert; push routes write rows and warn on `fixture`; `/api/hub` sources shape | tests: a push is visible on the next render, the warning fires on `fixture`; tests green |
| 1.3 | Roles in the cookie, `require_admin`/`require_admin_html` (token only), `/login` role by credential, setup-done sets the admin cookie, old cookie format rejected | tests: device key gives reader, token gives admin, reader cookie and device key bearer are both refused at `POST /settings/general`, an old-format cookie is rejected |
| 1.4 | `app/forms.py`, `/settings`, section save + reload, save and test, wizard routes, geocoding search | tests: every section round-trips through the form, checkbox pairing, indexed rows, secret keep and clear, a bad timezone re-renders with the error next to the field, unsupported annotation raises, a rendered PNG changes after saving a source, save and test prints the adapter error for a bad URL |
| 1.5 | Backup (POST), restore with validation and the close-replace-reopen sequence, rotate with session secret, warnings | tests: backup opens as sqlite with the hub row and carries `no-store`, restore of a bad file is 422 and leaves the db untouched, restore of a good file swaps and reloads while a render is in flight, rotate invalidates the old token and every cookie |
| 1.6 | Docs and packaging for phase 1: `.env.example`, compose, DEPLOY (upgrade, backup), SETTINGS.md, ARCHITECTURE, DATA-SOURCES, README, SKILL, LOCAL-AGENT | char scan clean; `grep` for every removed variable name finds nothing outside `app/legacy.py` and the upgrade note |

### Phase 2

| # | Package | Acceptance |
| --- | --- | --- |
| 2.1a | Module API, registry, the five pages plus `tasks` and `ai_usage` registered as modules whose specs point at the existing `view.py` builders and `app/templates`; `PAGES`, `PAGE_TTL_SECONDS`, `WINDOW_PAGES`, `CONTEXT_BUILDERS` registry-derived; `DashboardState.blocks` with `SerializeAsAny` and `schema: 2` | tests: a test module from a `tmp_path` `DATA_DIR/modules/` shows up as a page and in the footer, duplicate id refuses to start, `/api/state` carries a task title; PNG sha256 gate green |
| 2.1b | `/display/{n}.png`, telemetry `page_count`/`pages` and `page_index`, `modules` settings section with enable and order, footer from enabled pages, missing-module warning | tests: disabling a module removes it from the footer and the index route, `page_index` resolves to the id when `page` is absent |
| 2.2 | Move files into `app/modules/<id>/` with fixtures, templates and per-module tests; delete repo-root `fixtures/` and `FIXTURES_DIR`; shared helpers stay in `view.py` | PNG sha256 gate green (the six fixture-state renders hashed in a test before the move); tests green |
| 2.3 | `docs/MODULES.md`, `examples/modules/hello/`, `CONTRIBUTING.md` | a test installs the example module into a `tmp_path` modules dir and asserts its page renders and appears in the footer |
| 2.4 | Firmware: `page_count` from the telemetry response, index URLs, `page_index` plus `page` in telemetry, boot clamp | `esphome config` and `compile` pass |

### Phase 3

| # | Package | Acceptance |
| --- | --- | --- |
| 3.1 | `ha_dashboard` module with the screenshot renderer and error frame | tests with a local stub HA page served by the test: token injected, screenshot is 800x480 six-ink, path bounded; login redirect and timeout produce the error frame; nothing logged contains the token |
| 3.3 | Docs for phase 3 and the final sweep: README status, ARCHITECTURE, PRODUCT, DESIGN note, plan Status table | char scan clean, secrets scan clean, `uv run pytest` and `ruff check` green, Docker image builds and the end-to-end script passes |

## Sequencing

1. F.1 (firmware) and 1.1 start together in separate worktrees; they share
   no files.
2. After 1.1: 1.3 (small) then 1.2a and 1.2b in sequence, 1.5 in parallel
   with them (needs only 1.1).
3. 1.2c, 1.2d, then 1.4 (needs 1.2a and 1.3), then 1.6.
4. 2.1a, then 2.1b and 2.3 in parallel, then 2.2, with 2.4 in parallel
   with any of them.
5. 3.1, then 3.3.

## Verification

- `cd dashboard && uv run pytest` and `uv run ruff check .` after every
  package.
- PNG sha256 gate: the six fixture-state renders are hashed into a test
  before 1.2b and checked through 2.2.
- Docker: `docker compose build`, then the scratchpad end-to-end script
  (setup, wizard, settings save, push, display by id and index, backup,
  restore, rotate) against the container.
- Firmware: `esphome config` and `esphome compile` from PowerShell for
  F.1 and 2.4. Flashing is the owner's step.
- Public-release scan: no e-mail, LAN address, vault path or token in any
  tracked file; plain ASCII punctuation.

## Status

| Package | State | Notes |
| --- | --- | --- |
| gate | done | tests/test_render_gate.py, frozen state and six hashes, 74cfa57 |
| F.1 | done | text components, AP-only Wi-Fi, improv_serial, web_server v3; compiled on ESPHome 2026.8.2; upgrade note for Wi-Fi re-provisioning after OTA; not flashed |
| 1.1 | done | db.py, legacy.py, hub row, Hub.reload, 544 tests |
| 1.2a | done | section models, HubSettings, SettingsStore, Env |
| 1.2b | done | call sites read HubSettings + Env, gate green |
| 1.2c | done | SettingsStore is the source; config.Settings deleted |
| 1.2d | done | Push adapters, datasets rows, File/Auto gone, data-examples removed |
| 1.3 | done | roles, require_admin, settings stub |
| 1.4 | done | forms.py, geocode.py, /settings, wizard, save and test |
| 1.5 | done | backup POST, restore close-replace-reopen, rotate with session secret |
| 1.6 | done | SETTINGS.md, .env.example cut, docs rewritten, render-all.py fixed |
| 2.1a | done | module API, registry, seven built-ins, DashboardState schema 2; push field renamed to source; main.py split into settings_pages.py and httputil.py; 686 tests |
| 2.1b | done | /display/{n}.png, telemetry page_count/pages and page_index, HubSettings.extra with section() accessor, SettingsStore over the live section map, Modules section with missing-id warning and no-page guard; 722 tests |
| 2.2 | done | fixtures, templates, context builders and tests live in app/modules/<id>/; fixtures/ and FIXTURES_DIR gone; view.py 699 lines of shared helpers; gate green without regeneration |
| 2.3 | done | CONTRIBUTING.md, docs/MODULES.md, examples/modules/hello/ with an install test |
| 2.4 | done | page_count/page_names globals, index URLs, page_index in telemetry; compiled; not flashed |
| 3.1 | done | ha_dashboard built-in (off by default), screenshot renderer bounded to 8 s, error frames, token never logged, docs/HA-DASHBOARD.md; 729 tests |
| 3.3 | planned | |
