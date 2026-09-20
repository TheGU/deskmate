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
| 4a | "26 H AGO" printed over the BATTERY label | `system.html:117` prints `sys-age` as a fourth line in a 150 px cell that only budgets three | Move the age into the label row, right of BATTERY, same 14 px caps |
| 4b | HOME column shows INTERNET, HA, NAS, PROXMOX with no HA set | The owner's dev hub has `home.source = fixture` (imported from the old `.env`); those are the fixture's service names | No code change. Explained; the settings page shows the source |
| 4c | Alerts only while awake on USB | Firmware ignores show_alert in battery mode (commit 95fe3d9); the red flag "1" in the header is the overdue-task chip, not an alert | Document in DATA-SOURCES (alert section) and SKILL.md |
| 5 | Plug icon on power | `view.py:header_context` and `system/page.py:device_panel` use the battery glyph in every state; `icons.POWER_PLUG` exists unused | On `usb_present`: header and System show `POWER_PLUG` with the percent; `BATTERY_CHARGING` is no longer used |
| 6 | Settings link on /preview | None exists | Add a SETTINGS button to the page row (reader sees it; the settings page itself redirects a reader to login) |
| 7 | Raw view waits 3 to 5 s | `preview.html:54` loads the hidden panel `<img src="/display/...png">` on every navigation, so each click takes the Playwright render lock even in raw view | Load the PNG only when the panel view is shown (data-src) |
| 8 | Obsidian mount in compose | The tasks section has an `obsidian` source reading a bind-mounted vault; docs/LOCAL-AGENT.md already says the local agent reads the vault and pushes | Remove the `obsidian` source, adapter, tests, the `/vault` mount and `OBSIDIAN_VAULT_PATH`. Tasks sources: `push`, `fixture`. Legacy `TASKS_SOURCE=obsidian` imports as `push` with a WARNING |
| 9 | Weather location | `WeatherSettings.location_name` exists and is not drawn | Print it in small caps under the page title |
| 10a | Year in the header | Day stack is weekday over month | Three-line stack: weekday, month, year; header stays 64 px |
| 10b | Header widget per module | The header's right side is hard-coded to the weather block | Module API gains `header: HeaderSpec` (template partial in the module's templates dir plus a context function). General settings gain `header_widget`: a module id, `none`, or `rotate` (next widget on every render, in module order). Built-ins with a widget: weather (reading, as today), system (desk temperature and humidity), ai_usage (usage percent of the tightest window). Default `weather`, falling back to the first available widget when that module is disabled |
| 11 | Refresh schedule on the web UI | `auto_refresh_interval`, `telemetry_interval`, `wake_hours`, grace and deadline are ESPHome substitutions, compiled in | Device settings gain `refresh_minutes` (5 to 240), `telemetry_minutes` (1 to 60) and `wake_hours` (1 to 8 hours, 0 to 23). The telemetry response carries them; the firmware stores them in restorable globals and applies them without a reflash: the two interval components through `set_update_interval`, the wake slot through the globals array. Compiled from PowerShell, not flashed |

## Work packages

| # | Package | Acceptance |
| --- | --- | --- |
| R.1 | Repo hygiene and small page fixes: delete `e2e-*.png`; System battery age in the label row; plug icon on power (header and System); weather location line; /preview SETTINGS button and lazy PNG; README and PRODUCT device wording; alert-while-awake note | tests: header and System context carry the plug icon when `usb_present`; weather context carries `location_name`; preview HTML has no `src="/display` until toggled; gate hashes regenerated once, in this package, with the reason in the commit |
| R.2 | Obsidian removal | `rtk proxy git grep -i obsidian` finds only the legacy import note; compose has no `/vault`; tests green |
| R.3 | Header year and header widgets (after R.1) | tests: three-line stack; `header_widget` round-trips through the form; `rotate` advances per render; a disabled module's widget falls back; a directory module can declare a widget; MODULES.md documents it; gate hashes regenerated once |
| F.2 | Runtime refresh schedule | `esphome config` and `compile` pass from PowerShell; telemetry response carries the three fields; the device settings section validates the bounds; DATA-SOURCES documents the fields; FLASHING notes the first OTA seeds the globals from the compiled defaults |
| R.4 | Screenshots and README (after R.3) | `docs/images/<page>.png` for the six pages from `scripts/render-all.py`, linked from README |

Sequencing: R.1, R.2 and F.2 in parallel worktrees; R.3 after R.1; R.4 last.

## Verification

`uv run pytest -q`, `uv run ruff check .`, the render gate, `scripts/check-plain-ascii.py`, Docker build plus `scripts/e2e-check.py`, `esphome config` and `compile` for F.2.

## Status

| Package | State | Notes |
| --- | --- | --- |
| R.1 | planned | |
| R.2 | planned | |
| R.3 | planned | |
| F.2 | planned | |
| R.4 | planned | |
