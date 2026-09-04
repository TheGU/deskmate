# deskmate

A local-first desk dashboard for the Seeed Studio reTerminal E1002
(ESP32-S3, 800x480 six-color e-paper).

All the logic lives in a small server called **dashboard-hub**: it reads tasks,
calendar, weather, a pre-generated AI brief, AI quota and Home Assistant state,
renders one of six pages to an 800x480 PNG constrained to the panel's six
colors, and serves it over the LAN. The device is a thin display client that
downloads a PNG and shows it.

```
Data sources -> dashboard-hub (FastAPI + Jinja2 + Chromium + Pillow)
             -> 800x480 six-color PNG over HTTP -> reTerminal E1002 (ESPHome)
```

Pages: `today`, `agenda`, `weather`, `brief`, `system`, `alert`.

Status (2026-09-04): factory firmware backed up and verified, server runs
entirely from `fixtures/`, ESPHome firmware validated and compiled. The device
has not been flashed yet; that only happens on an explicit `FLASH` from the
owner.

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
cd dashboard
uv sync --all-groups
uv run playwright install chromium

# render all six pages to ../output/ without starting a server
uv run python ../scripts/render-all.py

# or run the server
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Then open <http://127.0.0.1:8080/preview> to flip through the pages, with the
device PNG on the left and the raw HTML on the right.

Tests:

```sh
cd dashboard && uv run pytest
```

## Docker quick start

```sh
cp .env.example .env      # optional, the defaults are fixture-only
docker compose up -d --build
curl -s http://127.0.0.1:8080/healthz
curl -o today.png http://127.0.0.1:8080/display/today.png
```

If host port 8080 is already in use, set `HUB_PORT` in `.env` (for example
`HUB_PORT=18080`) and use that port in the URLs above and in the device's
`hub_base_url`.

The compose service mounts `./data` read-write (files other agents write),
`./fixtures` read-only, and optionally an Obsidian vault read-only at `/vault`.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/healthz` | Status plus one entry per adapter |
| GET | `/api/state` | The normalized state the pages render from |
| GET | `/display/{page}.png` | 800x480 PNG, `ETag` + `304`, `?t=` busts the cache |
| GET | `/preview` | Developer page for switching between pages |
| GET | `/preview/{page}.html` | Raw HTML at 800x480, for CSS work |
| POST | `/api/alert` | Set the current alert |
| DELETE | `/api/alert` | Clear it |

## Layout

| Path | Purpose |
| --- | --- |
| `dashboard/` | The dashboard-hub server, its Dockerfile and tests |
| `fixtures/` | Demo data, used when no integration is enabled |
| `data-examples/` | Templates for the files other agents write into `data/` |
| `data/` | Runtime data (brief, ai-usage, alert). Gitignored |
| `output/` | Generated example PNGs. Gitignored |
| `docs/` | Architecture, data sources, flashing, factory restore |
| `scripts/` | Backup, verify and render helpers |
| `firmware/` | ESPHome YAML for the E1002, secrets example, its own venv |
| `private-backups/` | Factory flash dumps. Gitignored, sensitive |

## Firmware and hardware

Tooling is split on purpose: root `.venv` holds esptool 5.4 for backups and
restore, `firmware/.venv` holds ESPHome (it pins its own esptool).

```powershell
uv sync                                   # root: esptool
cd firmware; uv venv .venv --python 3.12; uv pip install --python .venv\Scripts\python.exe esphome
```

- Backup (read only): `scriptsackup-firmware.ps1 -Port COM3`, or the chunked
  reader `scripts\dump-flash-chunked.py` when the CH340 link drops packets.
  Verify with `scriptserify-backup.py`.
- Firmware config: `firmware\e1002.yaml`, secrets in `firmware\secrets.yaml`
  (copy from `secrets.yaml.example`, gitignored).
- Validate and compile without touching the device:
  `firmware\.venv\Scripts\esphome.exe config firmware\e1002.yaml` and
  `... compile firmware\e1002.yaml`.
- Flashing and bring-up: `docs/FLASHING.md`. Recovery: `docs/FACTORY-RESTORE.md`.

Device behaviour: left white button previous page, right white button next
page, green short press refresh, green long press back to Today. Home
Assistant can call `esphome.reterminal_e1002_show_alert` with `duration` and
`beep` to show `/display/alert.png` and return afterwards.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - the binding spec: endpoints,
  palette, config names, refresh policy.
- [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) - how to configure each adapter,
  the `data/` file schemas, the alert API and a Home Assistant example.
- [docs/FACTORY-RESTORE.md](docs/FACTORY-RESTORE.md) - restoring the factory
  firmware dump.
- [docs/FLASHING.md](docs/FLASHING.md) - flashing procedure.
- [.env.example](.env.example) - every configuration variable, commented.

## Design rules

- Exactly 800x480, landscape, quantized to white, black, red, yellow, green and
  blue with no dithering.
- Swiss information-dashboard style: white ground, bold black type, thick
  dividers, color used only as a semantic accent. No gradients, shadows, grays,
  animation or tiny text.
- Deterministic rendering: bundled Inter (OFL), no system fonts, no network at
  render time, no clock that changes every minute.
- Data is never invented. An adapter that is unset or broken makes the page say
  `unknown` or `unavailable`.
