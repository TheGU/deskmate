# Factory firmware restore (reTerminal E1002)

This document describes how to put the device back to the state it was in
when it arrived. Nothing here has been executed. Read it fully before
running anything.

## What was backed up

Backup taken on 2026-09-04 on the Windows 11 development PC.

| Item | Value |
| --- | --- |
| Device | Seeed reTerminal E1002 |
| Chip | ESP32-S3 (QFN56) revision v0.2, embedded 8 MB PSRAM |
| MAC | ac:27:6e:a6:a3:f0 |
| Flash | Winbond (manufacturer ef, device 4019), detected 32 MB, quad, 3.3 V |
| USB bridge | CH340 USB-UART, `USB\VID_1A86&PID_7523`, enumerated as COM3 |
| Secure Boot | SECURE_BOOT_EN = False |
| Flash encryption | SPI_BOOT_CRYPT_CNT = 0 (disabled) |
| Download mode | DIS_DOWNLOAD_MODE = False (enabled) |
| esptool | v5.4.0 in `.venv` (uv managed, Python 3.12) |

Files in `private-backups/` (gitignored, contains the factory NVS and
therefore possibly Wi-Fi credentials, treat as sensitive):

| File | Purpose |
| --- | --- |
| `e1002-factory-backup-1.bin` | Full flash dump, 0x0 to 0x2000000, 33,554,432 bytes |
| `e1002-factory-backup-2.bin` | Second independent dump, same range |
| `SHA256SUMS.txt` | SHA-256 of both dumps (must be identical) |
| `device-info.txt` | `esptool flash-id` output |
| `efuse-summary.txt` | `espefuse summary` output |

Factory partition table found in the dump (`scripts/inspect-backup.py`):

| Name | Type | Offset | Size |
| --- | --- | --- | --- |
| nvs | data | 0x9000 | 0x7d000 |
| otadata | data | 0x86000 | 0x2000 |
| phy_init | data | 0x88000 | 0x1000 |
| app0 | app (ota_0) | 0x90000 | 0xc00000 |
| app1 | app (ota_1) | 0xc90000 | 0xc00000 |
| spiffs | data | 0x1890000 | 0x600000 |

About 95% of the 32 MB is erased (0xFF), so esptool skips most of it on
restore.

Verify integrity at any time:

```powershell
.\.venv\Scripts\python.exe scripts\verify-backup.py private-backups
```

## Before restoring

1. Rear power switch ON. The device must be awake: press the green
   button once if it has been idle (Seeed: flashing fails while the
   device is asleep or shut down).
2. Connect USB-C to the PC. Confirm the port in PowerShell:

   ```powershell
   Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match 'COM\d+' } | Select-Object Name, DeviceID
   ```

   Expect `USB-SERIAL CH340 (COMx)`. Replace `COM3` below if it moved.
3. Make sure nothing else holds the port open (ESPHome logs, a serial
   monitor, the SenseCraft web flasher tab).
4. Run the verify script above. Do not restore from a dump that fails it.

## Path A: restore the full dump with esptool (exact factory image)

This rewrites all 32 MB: bootloader, partition table, factory app, OTA
slots, NVS and SPIFFS/LittleFS data. It restores the device byte for byte
to how it was on 2026-09-04, including any Wi-Fi credentials it held.

Command (esptool v5 syntax, hyphenated subcommands):

```powershell
.\.venv\Scripts\esptool.exe --chip esp32s3 --port COM3 --baud 460800 write-flash 0x0 private-backups\e1002-factory-backup-1.bin
```

Notes:

- Flash mode, frequency and size default to `keep`, so the bootloader
  header inside the dump is written unchanged. Do not add
  `--flash-size` or `--flash-mode` overrides.
- 460800 baud is the fastest rate that worked reliably through the CH340
  on this PC. 921600 caused "Corrupt data" during read-flash. If write
  fails with a similar error, drop `--baud` to 230400 or 115200.
- Expect roughly 15 to 25 minutes at 460800. esptool skips unchanged
  and all-0xFF blocks, so it may finish faster.
- esptool verifies each written block by MD5 from the chip; a final
  "Hash of data verified" line means the write matched.
- After the command finishes esptool performs a hard reset via RTS. If
  the screen does not come back, toggle the rear power switch off and on.

Optional read-back check after restore:

```powershell
.\.venv\Scripts\esptool.exe --chip esp32s3 --port COM3 --baud 460800 read-flash 0x0 0x2000000 private-backups\after-restore.bin
Get-FileHash private-backups\after-restore.bin -Algorithm SHA256
```

The hash should equal the value in `SHA256SUMS.txt`.

Linux or macOS equivalent:

```bash
esptool --chip esp32s3 --port /dev/ttyUSB0 --baud 460800 write-flash 0x0 private-backups/e1002-factory-backup-1.bin
```

## Path B: Seeed official factory firmware (SenseCraft HMI flasher)

Use this if the local dump is lost or you want the newest factory
firmware rather than the exact shipped image.

1. Open https://sensecraft.seeed.cc/hmi in desktop Chrome or Edge
   (Web Serial required) and sign in or create an account.
2. Left sidebar: Tools, then the Firmware Flasher tab. It shows the
   latest firmware for the reTerminal E1002.
3. Plug the device in over USB-C with the power switch ON, click Select
   and choose the CH340 serial port.
4. Choose the version. Enable "Full Flash" to wipe to factory state
   (all data, settings and designs are erased), or leave it off for a
   standard flash that keeps Wi-Fi and designs.
5. Click Flash and wait. If no progress appears, wake the device with
   the green button and retry.

Seeed also publishes demo firmware through the reTerminal E-Series
Firmware Hub at https://seeed-projects.github.io/OSHW-reTerminal-Series-E-D/
(Web Serial flasher in the browser). At the time of writing not every
platform card there had a published package.

After a factory restore the device behaves as documented in Seeed's
Getting Started guide: green LED on for about 30 s at boot, then it
sleeps; green button wakes it; hold both white buttons for 2 s to enter
Wi-Fi setup.

## Sources

- Seeed Getting Started with reTerminal E1002:
  https://wiki.seeedstudio.com/getting_started_with_reterminal_e1002/
- Zephyr board page (backup command reference):
  https://docs.zephyrproject.org/latest/boards/seeed/reterminal_e1002/doc/index.html
- esptool documentation: https://docs.espressif.com/projects/esptool/en/latest/esp32s3/
