# Contributing

deskmate is a local-first desk dashboard: a small server called
dashboard-hub renders panel pages to an 800x480 six-color PNG for a
Seeed reTerminal E1002 e-paper display, which only downloads and shows
them. The binding docs are [README.md](README.md) for what it does and
how to run it, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
endpoints, auth, rendering pipeline and config names, [DESIGN.md](DESIGN.md)
for the panel's visual design, and [docs/plan/](docs/plan/) for any
plan that spans more than one commit.

## Repository layout

| Path | Purpose |
| --- | --- |
| `dashboard/` | The dashboard-hub server: its own `pyproject.toml`, Dockerfile and tests |
| `examples/` | Copyable examples for extending the hub, e.g. `examples/modules/hello/`, a minimal third-party module (see [docs/MODULES.md](docs/MODULES.md)) |
| `firmware/` | ESPHome YAML for the E1002, secrets example, its own venv |
| `fixtures/` | Demo data used when no integration is enabled. Moving into per-module fixtures in phase 2 (see docs/plan/) |
| `docs/` | Architecture, data sources, deploy, hooks, flashing, factory restore |
| `scripts/` | Backup, verify, render and check helpers |
| `skills/` | `deskmate/SKILL.md`, how a remote agent pushes data to the hub |
| `data/` | Runtime data written by the hub and by other agents. Gitignored |
| `private-backups/` | Factory flash dumps. Gitignored, sensitive |

## Development setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
cd dashboard
uv sync --all-groups
uv run playwright install chromium
```

Run the server, then open `http://127.0.0.1:8080/setup` to set up a fresh
hub, and `/settings` afterwards to look at the settings page:

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

To render every page to `output/` without starting a server, using demo
data:

```sh
uv run python ../scripts/render-all.py
```

See README.md's Quick start for the environment variables that pin each
adapter to fixture data during development.

## Tests and checks

Run all of these before proposing a change:

```sh
cd dashboard
uv run pytest -q
uv run ruff check .
```

`dashboard/tests/test_render_gate.py` is a PNG sha256 gate: it renders the
six panel pages from a frozen fixture state
(`dashboard/tests/assets/frozen-state.json`) and asserts each PNG's hash
still matches `dashboard/tests/assets/frozen-hashes.json`. A failure here
means a change to the rendering pipeline changed pixels. Only regenerate
the frozen files with `uv run python tests/assets/freeze_state.py` when
that pixel change is intentional; run the same script with `--check`
first to confirm it still reproduces the currently committed hashes
without writing anything. Any intentional pixel change must be reviewed
against the frozen renders (`scripts/render-all.py` output is useful for
that), not just against the gate turning green.

From the repository root, check plain-ASCII punctuation:

```sh
python scripts/check-plain-ascii.py
```

And, for a change that touches setup, settings, push, backup, restore or
rotate, run the Docker end-to-end check against a throwaway container
(never a production hub, it claims the hub and rotates its secrets); see
"End-to-end check" in [docs/DEPLOY.md](docs/DEPLOY.md):

```sh
python scripts/e2e-check.py http://127.0.0.1:<port>
```

## Writing rules

- Plain keyboard punctuation only: no em dashes, no arrow glyphs, no
  emoji. `scripts/check-plain-ascii.py` enforces this over every tracked
  text file.
- Docstrings say why a piece of code exists or works the way it does, not
  just what it does.
- No secrets, real addresses, e-mail addresses or personal file paths in
  tracked files. Use example values like `192.168.1.50` instead.
- Data is never invented on the panel: an adapter that is unset or broken
  makes its block say "unknown" or "unavailable", never a guess or a
  stand-in number.
- Renders are deterministic: bundled fonts only, no system fonts, no
  network calls at render time, no clock that changes every minute.

## Design rules

[DESIGN.md](DESIGN.md) is binding for anything drawn on the 800x480
panel. The admin pages (`/setup` and `/settings`) are plain HTML with no
JavaScript; do not add any.

## Plans and reviews

Non-trivial work starts with a plan under `docs/plan/`, following
[docs/plan/README.md](docs/plan/README.md). A plan is reviewed before the
work starts, and lands in small commits, each one passing the checks
above. Commit messages are plain imperative lines describing the change;
no attribution trailers.

## Firmware

ESPHome configuration for the E1002 lives in `firmware/`, with its own
venv (kept separate from the root venv's esptool tooling):

```powershell
cd firmware
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe esphome
```

Validate a change with `firmware\.venv\Scripts\esphome.exe config
firmware\e1002.yaml`, and compile with `... compile firmware\e1002.yaml`.
On Windows, run `esphome compile` and `esphome run` from PowerShell, not
Git Bash: launched from Git Bash (MSYS) the ESP-IDF build silently skips
ninja and repackages the old binary while still reporting success;
`esphome config` is fine from either shell. `firmware/secrets.yaml` is
gitignored and seeded from `firmware/secrets.yaml.example`. Never flash a
device you do not own.

## Modules

Panel pages and the datasets behind them are moving to a module system
(see docs/plan/2026-09-19-settings-modules-provisioning.md, phase 2): a
module is a package that brings its own settings, adapter, template and
fixture. [docs/MODULES.md](docs/MODULES.md) is the module authoring
guide, with a complete minimal example at `examples/modules/hello/`. A
few pieces phase 2 still has in progress (a bespoke Modules section on
`/settings`, moving each built-in's template and fixture into its own
package) are called out in that doc where they matter.

## License

MIT. See [LICENSE](LICENSE). Contributions are accepted under the same
license.
