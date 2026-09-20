# Writing a module

A module is a Python package that brings its own piece of the panel: zero
or more datasets, at most one 800x480 page, and, optionally, its own
settings section and push routes. Core (the FastAPI app, the render
engine, auth, the settings machinery, the device routes and telemetry)
never has to know a module's name in advance; it discovers modules at
startup and treats every one of them the same way, built-in or not.

This document is the contract a module has to meet. The running example
is `examples/modules/hello/`, a complete, minimal module you can copy as
a starting point. `dashboard/tests/modules/test_example_hello.py` proves
the example itself follows this contract: it copies the directory into a
throwaway data directory, boots a hub over it, and checks that the page
shows up and renders.

See `docs/plan/2026-09-19-settings-modules-provisioning.md` (phase 2,
"pages as modules") for the design this document describes the shipped
half of. The Modules section on `/settings` and the
`HubSettings.section(name, model)` accessor for third-party settings
(work package 2.1b) are both shipped and described below, in "Enabling,
ordering and disabling a module" and "Settings for a module". Moving each
built-in's own template and fixture into `app/modules/<id>/` (2.2) is not
shipped yet, and does not change anything a third-party module author
reads in this document.

## What a module is

A *dataset* is a named block of normalized data with an adapter behind it
(`app/modules/__init__.py:DatasetSpec`), for example `tasks` or `weather`.
A *page* is one 800x480 render (`app/modules/__init__.py:PageSpec`). A
*module* (`app/modules/__init__.py:Module`) provides zero or more
datasets and at most one page, plus at most one settings section and at
most one router of push routes.

A module can be:

- a dataset only (the built-in `tasks` module: `app/modules/tasks/`, a
  push route and an adapter, no page of its own; other pages draw its
  block through `state.block("tasks", TasksBlock)`);
- a page only (the example, `examples/modules/hello/`: no dataset, it
  reads straight off `DashboardState`);
- both (the built-in `brief` module, `app/modules/brief/__init__.py`: a
  dataset, a push route, and a page that draws that dataset plus
  `tasks`).

Everything a module hands core is data, not behaviour: one frozen
`Module` dataclass instance, exposed as `MODULE` at the top of the
package (`app/modules/__init__.py:11-17`). `app/modules/registry.py` is
what finds those, validates them, and puts them in order.

## The `Module` / `DatasetSpec` / `PageSpec` / `HeaderSpec` / `ModuleContext` fields

These five are defined in `app/modules/__init__.py`; the field-by-field
meaning below is the same the docstrings there give, with a pointer to
where each one is used.

### `Module` (`app/modules/__init__.py:167-197`)

| Field | Meaning |
| --- | --- |
| `id` | `^[a-z][a-z0-9_]*$`, never all digits, never `"alert"` (see "Id rules" below). It is a package name, a URL segment (`/display/<id>.png`), a settings key and a template stem all at once. |
| `title` | The short name the footer's window list shows, upper case (`"HELLO"`, `"BRIEF"`). Not necessarily the same as the page's own on-page title (`PageSpec.title`). |
| `version` | The module's own version string, free-form. |
| `description` | One line, shown wherever modules are listed. |
| `settings_model` | The module's own pydantic settings model, or `None` if it has none. See "Settings" below. |
| `settings_section` | The settings key the model above is stored under, defaulting to `id` (`Module.section` property, `app/modules/__init__.py:194-197`). Set it when a module's dataset reads a section that predates the module, the way the built-in `system` module reads the core `device` section. |
| `datasets` | A tuple of `DatasetSpec`. |
| `page` | One `PageSpec`, or `None`. |
| `header` | One `HeaderSpec`, or `None`: the module's cell in the shared header. Independent of `page` -- a module may have a widget and no page (the built-in `ai_usage`), a page and no widget (`today`, `brief`), both, or neither. See "Header widget" below. |
| `routes` | A callable `(ModuleContext) -> APIRouter`, or `None`. See "Push routes" below. |
| `default_order` | Where the page sits in the window list when the `modules` settings section has no row for this module. |
| `default_enabled` | Whether the module is on when the `modules` settings section has no row for it. |

### `DatasetSpec` (`app/modules/__init__.py:102-131`)

| Field | Meaning |
| --- | --- |
| `name` | The dataset's key in `DashboardState.blocks` and in `registry.datasets()`. Same id rules as a module id, but reserved ids do not apply. |
| `block_model` | The `Block` subclass this dataset's adapter produces (`app/models.py`). |
| `value_field` | The one field of `block_model` the adapter's value lands in (`items` for tasks, `weather` for weather). Validated to actually exist on `block_model` at registry load, so a typo is a startup error, not a `ValidationError` on the first render. |
| `section` | The settings section the adapter reads. Usually the module's own id, but not always: the built-in `agenda` module owns the `calendar` dataset and reads the `calendar` section. |
| `build_adapter` | `(section_settings, general_settings, ModuleContext) -> Adapter`. |
| `ttl_seconds` | `(section_settings) -> float`: how long a fetched value is cached before the next state build re-fetches it. |
| `fixture` | Path to demo data used when the section's source is `fixture`, or `None`. |

### `PageSpec` (`app/modules/__init__.py:133-165`)

| Field | Meaning |
| --- | --- |
| `title` | The big on-page title ("NEXT 7 DAYS" for Agenda) -- not always the same string as the module's own `title` (the footer's shorter name). |
| `templates_dir` | The directory Jinja is told to also search for this module's templates (see "How templates are found"). |
| `context` | `(DashboardState, HubSettings) -> dict`. The module's own context dict; core merges its frame over it afterwards (see below). |
| `render_ttl_seconds` | How long a rendered PNG is cached before the next request re-renders it. |
| `template` | The file name in `templates_dir` (`"hello.html"`), or `None` if `screenshot` is set. |
| `needs` | The dataset names this page draws, for documentation and for whatever wants to know a page's dependencies. `examples/modules/hello` needs nothing: it reads `state.timezone` and `len(state.blocks)` directly, not any one dataset's block. |
| `demo_datasets` | Which of this page's datasets, when on `fixture`, should print the footer's DEMO mark. May only name a dataset in `PUSHED_DATASETS` (`tasks`, `ai_usage`, `brief`); a fetched dataset (weather, calendar) never prints DEMO even when it falls back to fixture data (`app/view.py:page_shows_demo_data`). Validated at registry load (`app/modules/__init__.py:244-249`). |
| `flag` | `(DashboardState, HubSettings) -> bool`: whether the footer marks this page's window with `!` right now. `None` means never. |
| `screenshot` | `(Browser, DashboardState, HubSettings) -> Image`, for a page drawn by a renderer of its own instead of a template. Exactly one of `template` or `screenshot` is set, never both, never neither (checked at registry load). No built-in page uses this yet; it is what phase 3's Home Assistant dashboard module needs. |

### `HeaderSpec`

| Field | Meaning |
| --- | --- |
| `context` | `(DashboardState, HubSettings) -> dict`, the same `PageContextFn` a page's `context` is. What it returns is merged under `header.widget`, so the partial reads its own values as `header.widget.<key>`. |
| `templates_dir` | The directory holding the partial. May be the same directory as the page's; `Registry.templates_dirs` collects both, so a module with a widget and no page still gets its directory searched. |

The partial's name is not a field: it is always `<module id>_header.html`
(`app/modules/__init__.py:header_template_name`), and `validate_module`
refuses a module whose partial is not in the directory it named. The id
prefix matters because every module's templates directory is searched by
one flat Jinja loader: two modules shipping a `header.html` would shadow
each other, and the winner would be whichever directory came first.

### `ModuleContext` (`app/modules/__init__.py:84-99`)

What core hands a module when it builds an adapter or a router: `env`
(`app/config.py:Env`), `db` (the hub's one `Database`), `data_dir`,
`http_timeout_seconds`, and a `logger` already named
`app.modules.<id>` (`app/main.py:255-263`, `Hub.module_context`). A
module reads its own settings from the section model it declared, never
from `ModuleContext`.

## Id rules and the reserved id

`MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")`
(`app/modules/__init__.py:56`) applies to a module id, a dataset name and
a settings section name. An id must not be all digits (a future
`/display/{n}.png` reads an all-digit path segment as a page index, so an
all-digit id would be ambiguous the moment the pattern is loosened;
checked at `app/modules/__init__.py:255-260`).

`alert` is reserved (`RESERVED_IDS`, `app/modules/__init__.py:60`): it is
the interrupt page core draws over whatever page was showing and hands
back afterwards, so it is core's, never a module's, and it never takes a
slot in the window list. A module named `alert` refuses to load.

A duplicate id across any two installed modules, or two modules both
declaring a dataset of the same name, also refuses the whole hub to
start (`app/modules/registry.py:Registry.__init__`, lines 115-127): a
module that would silently draw something the settings page cannot
explain is treated as a startup error, not a warning.

## How core draws the frame

A module's `context` function returns its own plain dict. Core computes
`header` and `footer` itself, through `app/view.py:base_context`, and
merges them over that dict *after* the module ran
(`app/view.py:build_context`, `_with_frame`, lines 1853-1908), so a
module cannot overwrite the frame by accident or on purpose.
`page_title` is set from `PageSpec.title` the same way, for the same
reason: the footer and the title bar are what the device navigates by.

Concretely, `examples/modules/hello/__init__.py:hello_context` returns:

```python
own = settings.section("hello", HelloSettings)
return {
    "timezone": state.timezone,
    "block_count": len(state.blocks),
    "greeting": own.greeting,
}
```

and its template never has to build `header` or `footer` -- it only
extends `base.html` and fills the `content` block; see "Templates" in
`examples/modules/hello/templates/hello.html` for how that pans out on
the page itself, and "Design expectations for a page" below for the
frame `base.html` draws.

## Header widget

The header's left group is the day numeral over its weekday, month and
year, then a 2 px vertical rule, then one module's widget. The right
group (the overdue chip, Wi-Fi, the battery pill and the clock) is core's
and stays core's. The widget slot is the one part of the frame a module
may fill.

**Declaring one.** Add a `HeaderSpec` to the module and put
`<module id>_header.html` in the directory it names:

```python
def hello_header(state: DashboardState, settings: HubSettings) -> dict[str, Any]:
    own = settings.section("hello", HelloSettings)
    return {"greeting": own.greeting[:HEADER_GREETING_MAX_CHARS]}


MODULE = Module(
    id="hello",
    ...,
    header=HeaderSpec(context=hello_header, templates_dir=HERE / "templates"),
)
```

and `examples/modules/hello/templates/hello_header.html`:

```jinja
<style>
.hdr-hello { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.hdr-hello-text { font-size: 20px; font-weight: 500; max-width: 380px; }
</style>
<div class="hdr-widget hdr-hello">
<span class="hdr-hello-text clip">{{ header.widget.greeting }}</span>
</div>
```

The partial is a fragment, not a page: it does not extend `base.html` and
it has no `content` block. Its CSS goes in a `<style>` element inside the
partial, because a module cannot edit core's template; prefix the class
names with the module id, for the same reason the partial is prefixed.
Core's own shared classes are available: `.num` for a tabular numeral,
`.i` for a Nerd Font glyph, `.telltale`, `.hatch`, `.chip`, `.label`, and
`.clip` (overflow hidden, ellipsis, nowrap) for variable text.

**The budget: 380 x 40 px.** That is what the header's left group has left
once the day stack carries its year line and the right cluster is at its
widest, measured, not guessed. Core clamps the root element to it
(`.hdr-widget` in `app/templates/base.html`, and the constants
`HEADER_WIDGET_WIDTH_PX` / `HEADER_WIDGET_HEIGHT_PX`), so a widget that
wants more is clipped rather than allowed to push the clock off the panel.
The clamp is the backstop, not the plan: clip variable text in Python to a
measured character budget the way `app/modules/agenda/page.py`
(`AGENDA_HEADER_TITLE_MAX_CHARS`) does, and carry `.clip` as well for a
string with no word boundary to break on.

**What core decides, and what the module decides.** The module decides
what its widget reads and how it draws. Core decides which module fills
the slot on which page, from two settings fields and the registry
(`app/view.py:resolve_header_widget`):

1. the page's own override, the `header_widget` column of that module's
   row in the Modules section: `default`, `none`, or a widget id;
2. the General section's `header_widget` when the override is `default`:
   a widget id, or `none`;
3. `none` at either level draws no widget and no vertical rule;
4. an id that names no enabled widget here falls back to the first
   enabled widget in module order, so a hub restored from a backup taken
   on another hub still draws something;
5. no widget installed at all leaves the slot empty.

Neither field is validated against the registry by its pydantic model: a
backup restore and the one-time legacy import both validate those models
with no registry in reach. The settings page builds both selects from the
live registry and refuses a save naming a widget this hub does not have
(`app/settings_pages.py`); the render falls back rather than failing.

**No clock in it.** The rendered PNG is cached per page and its ETag is
keyed by a state fingerprint, so a widget that changed with the time of
day would be served stale for a whole page TTL. A widget must be a pure
function of state, settings, registry and page. The per-page override is
what gives variety instead: agenda's page can show the next event while
brief's shows nothing.

**The built-ins.** `weather` (the reading the header always drew, and the
default), `agenda` (`next_event`: when the next event starts and what it
is), `system` (the desk's temperature and humidity) and `ai_usage` (the
used percent of the tightest capacity window). `ai_usage` draws no page at
all, which is the proof that a widget does not need one.

## `flag` and `demo_datasets`

`flag` puts the "!" beside a page's name in the footer's window list
(`app/view.py:footer_context`, `_flagged`, lines 726-763). It takes its
reference time from `state.updated_at` and its condition from whichever
block it reads, so a page owns its own reason to be flagged instead of
one shared function knowing about every page by name. The example's
`hello_flag` (`examples/modules/hello/__init__.py`) always returns
`False`; a real one looks like `app/view.py:today_flag`, which flags
Today when a task is overdue.

`demo_datasets` decides the footer's DEMO mark
(`app/view.py:page_shows_demo_data`, `footer_context`). It may only name
a dataset in `PUSHED_DATASETS = {"tasks", "ai_usage", "brief"}`
(`app/modules/__init__.py:66`) -- the datasets an agent pushes to the
hub. A dataset the hub fetches itself (weather, calendar) never prints
DEMO even on a fixture fallback, because "demo" here means "an agent
has not pushed anything yet, so you are looking at seed data", not "some
adapter failed". Naming any other dataset refuses the module to load
(`app/modules/__init__.py:244-249`). The example declares
`demo_datasets=()`: it has nothing pushed to mark.

## Push routes

A module that wants a push endpoint sets `routes` to a callable that
takes a `ModuleContext` and returns a `fastapi.APIRouter`
(`app/modules/tasks/routes.py:build_router` is the built-in example).
Core mounts every installed module's router under `/api` with
`Depends(require_token)` already applied, in the registry loop in
`app/main.py:create_app` (`app/main.py:535-541`):

```python
for module in app.state.hub.registry.modules:
    if module.routes is None:
        continue
    app.include_router(
        module.routes(app.state.hub.module_context(module)),
        prefix="/api",
        dependencies=[Depends(require_token)],
    )
```

so a module's own route handler never has to check a credential itself,
and cannot forget to. This runs for every *installed* module, not only
the enabled ones: routes are fixed at startup (FastAPI has no unmount),
so a disabled module's push route stays reachable, writes its dataset
row same as always, and says so in its own response's `warning` field
when nothing draws it (`app/modules/tasks/routes.py:40-52`,
`app/datasets.py:push_warning`). The example module declares no routes.

## How templates are found

The renderer's Jinja environment searches core's own `app/templates`
first, then every enabled page's own `templates_dir`, without repeats
(`app/renderer/render.py:_build_environment`, lines 143-163; the
directory list itself is `app/modules/registry.py:Registry.templates_dirs`,
lines 191-203). Core first means a module cannot shadow `base.html` or a
core macro by shipping a file of the same name.

A template is named `<id>.html` by convention (`hello.html` for the
`hello` module), because `PageSpec.template` is exactly what you point
at your own file with -- there is no requirement the file share the
module's id, but every built-in module does it that way and there is no
reason to deviate.

Every page template extends the same base the built-ins use:

```jinja
{% extends "base.html" %}

{% block style %}
.my-thing { ... }
{% endblock %}

{% block content %}
...
{% endblock %}
```

`base.html` (`app/templates/base.html`) draws the 800x480 white canvas,
the header (day stack, the widget slot, wifi, battery, clock) and the
footer (window list, DEMO mark) from the `header` and `footer` context
keys core fills in; a module template only ever fills `content` (and, if
it needs its own CSS classes, `style`). See
`examples/modules/hello/templates/hello.html` for a complete, working
instance of this.

A header widget's partial is the exception to all of the above: it is a
fragment core `{% include %}`s into the header, so it neither extends
`base.html` nor fills a block. Its file name is fixed
(`<module id>_header.html`) and its directory comes from the `HeaderSpec`
rather than the `PageSpec`, which is what lets a module with no page ship
one. See "Header widget".

## The three ways to install a module

1. **Built-in**, under `app/modules/<id>/`, listed by import path in
   `app/modules/registry.py:BUILTIN_MODULE_PACKAGES` (lines 60-68). This
   is for modules that ship with the hub; the list is explicit rather
   than a directory scan because several packages under `app/modules/`
   are settings sections only (`general`, `alert`, `device`, `home`,
   `calendar`) and are not modules at all.

2. **An entry point**, in the `deskmate.modules` group
   (`app/modules/registry.py:ENTRY_POINT_GROUP`, line 71), for a module
   distributed as its own installable package:

   ```toml
   # pyproject.toml of the third-party package
   [project.entry-points."deskmate.modules"]
   hello = "my_package.hello:MODULE"
   ```

   A broken entry point is a WARNING and is skipped at load, not a dead
   hub (`app/modules/registry.py:entry_point_modules`, lines 248-275);
   a module that loads but fails validation still refuses the whole hub
   to start, in `Registry.__init__`. Installing or upgrading a package
   this way means rebuilding the image and restarting the container.

3. **A directory drop**, under `DATA_DIR/modules/<name>/`
   (`app/modules/registry.py:MODULES_DIRNAME`, line 74;
   `directory_modules`, lines 278-311): any subdirectory with an
   `__init__.py` exposing `MODULE` is imported by its own name on
   restart, no packaging needed. This is how `examples/modules/hello/`
   is meant to be tried: copy the whole directory to
   `DATA_DIR/modules/hello/` and restart the hub.

   In Docker, that means bind-mounting your module's source directory
   under the container's `/data/modules/<name>/` (`DATA_DIR` is `/data`
   in `docker-compose.yml`). It does not have to live under the same
   host path as the runtime `./data` volume; keeping module source in
   its own directory outside the gitignored runtime data is clearer:

   ```yaml
   services:
     dashboard-hub:
       volumes:
         - ./data:/data
         - ./my-modules/hello:/data/modules/hello:ro
   ```

   Restart the container after adding, updating or removing a mounted
   module directory; modules are discovered at hub startup, not on the
   fly.

A module is arbitrary Python that the hub imports and runs with its own
process: your database (token hashes, the session secret, a Home Assistant
token if you have configured one), your data directory and your network,
whichever way it was installed. None of the three install paths above
sandbox a module from any of that. Install only a module you have read or
trust, the same way you would before running any other program with access
to your own credentials.

## Enabling, ordering and disabling a module

The `modules` settings section
(`app/modules/registry.py:ModulesSettings`, `ModuleToggle`, lines
80-103) is one row per module: `id`, `enabled`, and an optional `order`.
A module with no row keeps its own `default_enabled` / `default_order`
(`Registry.is_enabled`, `Registry.order_of`, lines 136-144), so
installing a module is enough to see it; nothing has to be enabled by
hand for the default case to work. An id the section names that is not
installed is kept and reported through `Registry.missing_ids()`
(line 152-154), never silently dropped, so a module can come back after
an upgrade without losing the row that named it.

`/settings` has a dedicated Modules section (`app/settings_pages.py:MODULES_SECTION`,
rendered through the same generic form generator every other section
uses): one row already filled in per installed module -- id, an enable
checkbox, an order field -- not a blank form you type ids into, plus a
warning line for a row naming a module that is not installed here
(`app/settings_pages.py:_missing_module_warnings`). See docs/SETTINGS.md,
"Modules", for the field-by-field table and the two rules the form
enforces: at least one module with a page has to stay enabled, and a
stored id for a module that is not installed is kept and reported, never
silently dropped. `dashboard/tests/test_modules.py`'s "a third-party
section" tests exercise this against a module core has never heard of, not
only the built-ins.

Disabling a module through this section takes effect on the next
`Hub.reload()`: any page that draws one of its datasets falls back to
`state.block`'s unavailable placeholder for that dataset rather than
failing (`app/models.py:DashboardState.block`, lines 580-594).

## Settings for a module

A module that needs its own settings sets `Module.settings_model` to a
pydantic `BaseModel` subclass and, if the section name should differ from
the module id, `Module.settings_section` (`app/modules/__init__.py:Module`;
see `app/modules/brief/__init__.py` for a built-in that uses both
`settings_model` and a `section` constant imported from its own
`settings.py`). `Registry.sections()` (`app/modules/registry.py:Registry.sections`)
collects every *installed* module's section, enabled or not, into the
section map `app/settings.py:sections_for` builds `HubSettings` from, so
a disabled module's settings still load, save and render, and still get a
form on `/settings` (see "Enabling, ordering and disabling a module"
above).

Core has no fixed attribute for a third-party section: a built-in's
section is one (`HubSettings.weather`, `HubSettings.tasks`, ...), but a
module core has never heard of lands in `HubSettings.extra` instead
(`app/settings.py:HubSettings.extra`). `HubSettings.section(name, model)`
is the one accessor a module's context builder, adapter or route needs:
it reads either kind without the caller having to know which, answers
with `model()`'s own defaults rather than `None` when nothing has been
saved yet, and re-validates a stored value through `model` if it was ever
saved under a different one (a snapshot taken before the module was
installed, say).

`examples/modules/hello` uses exactly this. It sets
`settings_model = HelloSettings` (`examples/modules/hello/settings.py`)
and reads its own section in its context builder
(`examples/modules/hello/__init__.py:hello_context`):

```python
own = settings.section("hello", HelloSettings)
return {
    "timezone": state.timezone,
    "block_count": len(state.blocks),
    "greeting": own.greeting,
}
```

Disabling the module never drops its section or its row on `/settings`
(see above): a disabled `hello`'s greeting is exactly as saved, whenever
it is re-enabled. `docs/SETTINGS.md`, "Modules", documents the Modules
section from the settings-page side; `dashboard/tests/test_modules.py`'s
"a third-party section" tests and `dashboard/tests/modules/test_example_hello.py`
prove the accessor and the rendered form both against a synthetic module
and against this example.

A field named `source` on your own model does not earn a "Save and test"
button next to Save: that button only ever appears for the built-in
sections named in `app/settings_pages.py:TESTABLE_SECTIONS`, because it
runs a forced fetch through that section's own adapter on
`StateService.adapters` (`app/state.py`), and core has no adapter
registered under a third-party module's section name to fetch through.

## How to test a module

`dashboard/tests/modules/test_example_hello.py` is the pattern: copy the
module's own directory (not a string embedded in the test) into a
`pytest tmp_path`'s `modules/` subdirectory, point `Env(DATA_DIR=...)` at
that temp directory, build the app with `app.main.create_app`, and drive
it with `fastapi.testclient.TestClient` the same way any other endpoint
test does. Claim the hub once (`hub.identity.claim(...)`) to get a
bearer token, then assert:

- the module shows up in `hub.registry.page_ids()` (installed);
- its title appears in a rendered page's footer window list (a GET of
  any `/preview/<page>.html` shows every enabled page's window, not
  just its own);
- `GET /display/<id>.png` returns `200`, `content-type: image/png`, and
  a real 800x480 image (`app/renderer/palette.py:DISPLAY_SIZE`).

`dashboard/tests/test_modules.py` covers the registry and validation
rules themselves (a reserved id, a duplicate id, an invalid dataset
name, and so on) against synthetic modules built in Python, not against
a package on disk; read it alongside this doc for the negative cases
this document only describes in prose.

## Design expectations for a page

Every page is exactly 800x480 (`app/renderer/palette.py:DISPLAY_SIZE`),
drawn once and quantized to the six panel inks -- white, black, red,
yellow, green, blue (`app/renderer/palette.py:PALETTE`) -- with no
dithering (the panel and the ESPHome driver agree on pure primaries; see
`app/renderer/palette.py`'s module docstring). `DESIGN.md` at the repo
root is binding for anything drawn on the panel; read it before
designing a real page. In short:

- **Six inks, no grays.** A color reports a state (red for alarm, yellow
  for caution, green for healthy, blue for weather); it is never
  decoration. `base.html`'s `.t-*` / `.bar-*` / `.chip-*` classes are
  the only colors a template should reach for.
- **Arm's length legibility.** The panel sits on a desk, read from a
  couple of feet away, not held like a phone: labels are 16px upper
  case with wide letter-spacing (`.label` in `base.html`), values are
  large numerals (the `.reading` family goes from 32px to 96px). The
  example's own `.hello-value` at 40px and its note at 20px sit
  comfortably inside that range.
- **No phone-widget look.** No rounded corners, no shadows, no
  gradients, no card chrome. `base.html` has no `border-radius` or
  `box-shadow` anywhere and a module page should not add one.
- **Google Sans Flex plus Nerd Font icons**, both bundled and loaded
  through `font_css` (`app/renderer/render.py:font_css`, `base.html`'s
  `{{ font_css|safe }}`); never a system font, never a Unicode symbol
  standing in for an icon (`.i` in `base.html` is the Nerd Font glyph
  class; see `app/icons.py` for the bundled subset).
- **Plain ASCII, deterministic renders.** No em dashes, no arrow
  glyphs, no emoji anywhere a module's own file lands
  (`python scripts/check-plain-ascii.py` enforces this over every
  tracked text file, examples included); no network calls or
  system-clock-sensitive text at render time, so a page renders the
  same PNG for the same state every time.
