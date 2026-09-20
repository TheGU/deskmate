# 2026-09-20 Owner feedback round

Eleven points from the owner after reviewing the merged plan
(2026-09-19-settings-modules-provisioning.md). Facts checked against the
tree at 201fac3 before anything was decided.

## Goal

Answer or fix every point: repo hygiene and README screenshots, a device
wording that does not claim the hub is E1002-only, the System page battery
cell, the plug icon on power, a settings link and a fast raw view on
/preview, the Obsidian mount, the weather location name, the header year
and a configurable header widget, and a runtime refresh schedule for the
device.

## Non-goals

- Other display sizes. `DISPLAY_SIZE` (800x480) is a constant and every
  template is laid out in fixed pixel budgets (DESIGN.md), so a size setting
  would render broken pages. Documented as a limit, not built.
- Rewriting the design system. Every visual change here stays inside the
  Braun Panel rules (14 px floor, six inks, color as state).

## Findings

| # | Owner point | Fact | Decision |
| --- | --- | --- | --- |
| 1, 3 | `e2e-*.png` in the repo root | Six tracked PNGs added with the 1.6 docs merge, referenced by nothing. They are six-ink renders of the fixture state (little color in the fixtures, so they read as monochrome) | Delete. Capture README screenshots from `scripts/render-all.py` into `docs/images/` after the header work lands, and link them from README |
| 2 | Not E1002-only | README line 3 and 16 name the E1002 as the target; the hub only needs any ESPHome device that can fetch an 800x480 PNG and post telemetry | Reword README and PRODUCT: reference firmware for the E1002, hub works with any ESPHome e-paper device of that size; sizes are a limit (see Non-goals) |
| 4a | "26 H AGO" printed over the BATTERY label | When the device is stale, `system.html:117` prints `sys-age` as a fourth line in a 150 px cell that budgets three | Move the age into the label row, right of BATTERY, 16 px caps like the label, clipped; the 190 px cell fits it with about 10 px to spare, measured |
| 4b | HOME column shows INTERNET, HA, NAS, PROXMOX with no HA set | The owner's dev hub has `home.source = fixture` (imported from the old `.env`); those are the fixture's service names | No code change. Explained; the settings page shows the source |
| 4c | Alerts only while awake on USB | Firmware ignores show_alert in battery mode (commit 95fe3d9); the red flag "1" in the header is the overdue-task chip, not an alert | Document in DATA-SOURCES (alert section) and SKILL.md |
| 5 | Plug icon on power | The header pill and the System cell use a battery glyph in every state; the System cell already prints a power word (BATTERY, CHARGING, USB); `icons.POWER_PLUG` exists unused. Firmware `usb_present` means input power present (PG_STAT or VBUS), separate from `charge_state` | `usb_present is True` (never truthiness; `None` is old firmware) shows `POWER_PLUG` with the percent in the header and in the System cell, the power word stays and now prints USB for every powered state that is not charging; `BATTERY_CHARGING` and the `charging` parameter of `icons.battery_icon` are deleted. DATA-SOURCES notes that a charger that answers on neither bus makes the firmware assume USB present |
| 6 | Settings link on /preview | None exists | Add a SETTINGS button to the page row (reader sees it; the settings page itself redirects a reader to login) |
| 7 | Raw view waits 3 to 5 s | `preview.html:54` loads the hidden panel `<img src="/display/...png">` on every navigation, so each click takes the Playwright render lock even in raw view | Load the PNG only when the panel view is shown (data-src) |
| 8 | Obsidian mount in compose | The tasks section has an `obsidian` source reading a bind-mounted vault; docs/LOCAL-AGENT.md already says the local agent reads the vault and pushes | Remove the `obsidian` source, adapter, tests, the `/vault` mount and `OBSIDIAN_VAULT_PATH`. Tasks sources: `push`, `fixture`. Legacy `TASKS_SOURCE=obsidian` imports as `push` with a WARNING |
| 9 | Weather location | `WeatherSettings.location_name` exists and is not drawn | Print it in small caps under the page title |
| 10a | Year in the header | Day stack is weekday over month | Three-line stack: weekday, month, year; header stays 64 px |
| 10b | Header widget per module | The second cell of the header's left group (after the vertical rule, `base.html:235` `.hdr-weather`) is hard-coded to the weather reading; the right group (overdue chip, Wi-Fi, battery pill, clock) is core and stays. The slot is 380 px wide worst case once the year line lands (measured) and 40 px tall | Module API gains `header: HeaderSpec` (a partial named `<id>_header.html` in the module's templates dir plus a `PageContextFn`). The owner's shape: a default plus a per-page override. General settings gain `header_widget` (the default; a module id or `none`; plain `str` validated for shape only, the select on /settings is built from the live registry, the save refuses an id with no widget on this hub). The Modules section's row for a page module gains `header_widget`: `default`, `none`, or a widget id, so agenda can show `next_event` while brief shows nothing. Render resolves page override, then default, then the first available widget, then none, so a restore from another hub is safe. Built-ins with a widget: weather (reading, as today), agenda (`next_event`: start time and clipped title of the next event, or nothing today), system (desk temperature and humidity), ai_usage (usage percent of the tightest window). No time-based rotation: the PNG cache and ETag are keyed by state fingerprint and each page caches on its own TTL, so the widget must be a pure function of settings, registry, state and page, which the per-page override gives |
| 11 | Refresh schedule on the web UI | `auto_refresh_interval`, `telemetry_interval`, `wake_hours`, grace and deadline are ESPHome substitutions, compiled in | Device settings gain `refresh_minutes` (5 to 240), `telemetry_minutes` (1 to 60) and `wake_hours` (any subset of the 24 hours). The telemetry response carries them; the firmware stores them in restorable globals and applies them without a reflash: the two interval components (given ids) through `set_update_interval` followed by `stop_poller` and `start_poller` (setting the field alone does not touch a running poller), the wake slot through a restorable 24-bit hour mask (ascending and de-duplicated by construction). The firmware clamps regardless of the hub (5 to 240, 1 to 60, mask non-zero within 24 bits) and keeps the previous value on a bad or absent field. Compiled from PowerShell, not flashed |

## Work packages

| # | Package | Acceptance |
| --- | --- | --- |
| R.1 | Repo hygiene and small page fixes: delete `e2e-*.png`; System battery age in the label row; plug icon on power (header and System); weather location line; /preview SETTINGS button and lazy PNG; README and PRODUCT device wording; alert-while-awake note | tests: header and System context carry the plug icon when `usb_present`; weather context carries `location_name`; preview HTML has no `src="/display` until toggled; gate hashes regenerated once, in this package, with the reason in the commit |
| R.2 | Obsidian removal | `rtk proxy git grep -i obsidian` finds only the legacy import note; compose has no `/vault`; tests green |
| R.3 | Header year and header widgets (after R.1) | tests: three-line stack (16 px lines, the numeral drops 1.6 px, header stays 64 px); `GeneralSettings().header_widget == "weather"` and every page override defaults to `default` so the gate stays pinned; the default and a per-page override round-trip through the forms and both selects list installed widgets; a page set to `none` renders without the widget and its rule; a disabled module's widget falls back; a page-less module (ai_usage) and a directory module can declare a widget (`templates_dirs` must include header-only modules; `build_context` takes the registry); each built-in widget renders in a non-gate test; MODULES.md documents the contract and the 380x40 px budget; gate hashes regenerated once |
| F.2 | Runtime refresh schedule | `esphome config` and `compile` pass from PowerShell; telemetry response carries the three fields; the device settings section validates the bounds; DATA-SOURCES documents the fields; FLASHING notes the first OTA seeds the globals from the compiled defaults |
| R.4 | Screenshots and README (after R.3) | `docs/images/<page>.png` for the six pages from `scripts/render-all.py`, linked from README |

Sequencing: R.1, R.2 and F.2 in parallel worktrees; R.3 after R.1; R.4 last.

## Verification

`uv run pytest -q`, `uv run ruff check .`, the render gate, `scripts/check-plain-ascii.py`, Docker build plus `scripts/e2e-check.py`, `esphome config` and `compile` for F.2.

## Status

| Package | State | Notes |
| --- | --- | --- |
| R.1 | done | e2e PNGs deleted; battery age in the label row (16 px, clipped); plug icon on usb_present with power word USB/CHARGING; weather location under the hero; /preview Settings link and lazy panel PNG; device wording; alert-while-awake note; gate refrozen |
| R.2 | done | obsidian source, adapter, tests, /vault mount and OBSIDIAN_VAULT_PATH removed; a stored or legacy obsidian source upgrades to push with a warning; PROPOSAL.md kept as the historical brief |
| R.3 | done | year line; HeaderSpec with <id>_header.html partials; general.header_widget default plus a per-page override in the Modules section; widgets for weather, agenda (next event), system, ai_usage and the hello example; gate refrozen; 806 tests |
| F.2 | done | wake hours may name any of the 24 hours (the owner wakes hourly 06 to 18); refresh_minutes, telemetry_minutes, wake_hours in the device section and the telemetry response; firmware restorable globals with a 24-bit hour mask, poller restart, independent clamps; compiled on ESPHome 2026.8.2, not flashed; 775 tests |
| R.4 | done | docs/images/<page>.png from render-all.py, linked from README; senior review of the round: six findings fixed (wake_hours max_length, core-side widget clamp, e2e forms, hidden-cell save, forms docstring, README telemetry row); e2e 76 checks ok; 810 tests |
