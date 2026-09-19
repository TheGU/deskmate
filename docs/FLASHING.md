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

Edit `firmware/secrets.yaml`: `hub_base_url` (LAN address of dashboard-hub,
for example `http://192.168.1.50`) and `hub_key` (the device key from the
hub's setup-done page) only seed the device's Hub base URL and Hub key
fields on its very first boot; both can be changed later without a
reflash (see "Provisioning at runtime" below). The example file's other
entries set up the device's own sign-in and its random per-device values;
fill each in with your own choice. There is no Wi-Fi entry: Wi-Fi is
provisioned at runtime only, never from `secrets.yaml`. `secrets.yaml` is
gitignored.

A `.local` mDNS hostname does not resolve on the device: ESP-IDF's resolver
sends a `.local` name to mDNS, and this firmware never joins that multicast
group, so the lookup just fails. `hub_base_url` must be the server's IP
address, or a DNS name the router or homelab's own DNS actually serves.

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

## Provisioning at runtime

Wi-Fi, the hub URL and the device key are never permanently baked into the
firmware image; moving the hub or updating the device's copy of its key
later is a browser edit, not a reflash.

**Wi-Fi.** The very first boot (and any boot where no network has been
saved yet) brings up an access point named `reTerminal-E1002 Setup` (see
`secrets.yaml.example` for its entry). Connect a phone or laptop to it and
ESPHome's captive portal opens automatically (or browse to the AP's
address) and lets you pick a network. Alternatively, use Improv over the
USB serial connection while the device is plugged in. Either path stores
the chosen network in NVS and it survives OTA updates; the setup AP does
not come back once a saved network works again.

**Hub base URL and Hub key.** These seed from `secrets.yaml` on the first
boot only. To change them afterwards:

- Open `http://<device-ip>/` in a browser (the device's own page, signed
  in with the entry `secrets.yaml.example` sets up for it) and edit the
  "Hub base URL" and "Hub key" text fields, or
- If the device is added to Home Assistant, edit its "Hub base URL" and
  "Hub key" text entities there.

Both changes take effect immediately, no reflash and no reboot required.
This is what makes `docs/DEPLOY.md`'s hub rotate and restore flows a
browser edit instead of a reflash event. Firmware built before this
change has no text fields or device page; a reflash is the fallback for
those older devices (see `docs/DEPLOY.md`, "Giving the device its hub
URL and key").

## Build gotcha on this PC

Run `esphome compile` and `esphome run` from PowerShell. Launched from Git
Bash (MSYS) the ESP-IDF build silently skips ninja and repackages the old
binary while still reporting success. `esphome config` is fine from either.

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
- Battery mode: unplug USB, expect a descending beep and sleep after 45 s;
  a button wakes it; the next scheduled hour wakes it; plugging USB back in
  is noticed on the next wake (rising beep, always-on again).
