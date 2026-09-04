# Flashing ESPHome to the reTerminal E1002

Nothing in this document runs by itself. The first firmware overwrite only
happens after the factory backup is verified (see `FACTORY-RESTORE.md`)
and the owner explicitly types `FLASH`.

## Tooling layout

| Tool | Location | Why separate |
| --- | --- | --- |
| esptool 5.4.0 | `.venv` (root `pyproject.toml`) | Used for backup and restore |
| ESPHome | `firmware/.venv` | ESPHome pins its own esptool; keeping it apart protects the backup tooling |

Create the ESPHome venv once:

```powershell
cd firmware
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe esphome
```

## Prepare secrets

```powershell
Copy-Item firmware\secrets.yaml.example firmware\secrets.yaml
```

Edit `firmware/secrets.yaml`: Wi-Fi, `hub_base_url` (LAN address of
dashboard-hub, for example `http://192.168.1.50:8080`), and fresh random
values for `api_encryption_key` (32 bytes base64), `ota_password`,
`ap_password`. `secrets.yaml` is gitignored.

## Validate (non-destructive)

```powershell
firmware\.venv\Scripts\esphome.exe config firmware\e1002.yaml
```

Compile without touching the device (downloads the ESP-IDF toolchain on
first run, several minutes):

```powershell
firmware\.venv\Scripts\esphome.exe compile firmware\e1002.yaml
```

Build output lands in `firmware/.esphome/` (gitignored).

## The flash command (only after FLASH)

Device awake, power switch ON, USB-C connected, port COM3 free.

```powershell
firmware\.venv\Scripts\esphome.exe run firmware\e1002.yaml --device COM3
```

What it alters: ESPHome writes its bootloader, partition table and
application to the flash, replacing the SenseCraft factory firmware. NVS
(factory Wi-Fi credentials and settings) is not deliberately erased but
the factory app is gone; treat it as a full replacement. Recovery is
`docs/FACTORY-RESTORE.md`.

If ESPHome's own upload fails on the CH340 at its default baud, retry
with a lower rate, which was the reliable rate on this PC:

```powershell
firmware\.venv\Scripts\esphome.exe run firmware\e1002.yaml --device COM3 --upload-speed 460800
```

## After the first flash

- Logs over USB: `esphome logs firmware\e1002.yaml --device COM3`
- Logs over the network once Wi-Fi is up: `esphome logs firmware\e1002.yaml`
- OTA for every later change: `esphome run firmware\e1002.yaml` and pick
  the OTA target. USB is no longer required.

## Bring-up checklist (Phase 3)

- Boots, log shows Wi-Fi connected and an IP.
- Home Assistant discovers `reterminal-e1002` (optional).
- Panel shows the Today PNG after the first successful download.
- Left button: previous page. Right button: next page. Wraps both ways.
- Green short press: refresh (click beep, panel refreshes once).
- Green long press (1 to 6 s): back to Today.
- Stop dashboard-hub: buttons produce the error beep, the screen keeps
  the last image, no reboot loop in the logs.
- `esphome.reterminal_e1002_show_alert` with `duration: 20`, `beep: true`
  sounds the buzzer, shows the alert page, and restores the page after.
- OTA update works from the ESPHome CLI.
