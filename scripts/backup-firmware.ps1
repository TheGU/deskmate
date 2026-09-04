# Dump the full 32 MB flash of a reTerminal E1002 twice and verify.
# Read-only. Does not modify the device.
#
# Usage (from repo root, after `uv sync`):
#   .\scripts\backup-firmware.ps1 -Port COM3
#
# Notes learned on this machine:
#   - The E1002 exposes a CH340 USB-UART bridge (USB\VID_1A86&PID_7523).
#   - 921600 baud produced "Corrupt data" on read-flash. 460800 works.
#   - The device must be awake and the rear power switch ON.
param(
    [string]$Port = "COM3",
    [int]$Baud = 460800,
    [string]$OutDir = "private-backups"
)
$ErrorActionPreference = "Stop"
$esptool = Join-Path $PSScriptRoot "..\.venv\Scripts\esptool.exe"
New-Item -ItemType Directory -Force $OutDir | Out-Null

& $esptool --chip esp32s3 --port $Port flash-id | Tee-Object -FilePath (Join-Path $OutDir "device-info.txt")
& (Join-Path $PSScriptRoot "..\.venv\Scripts\espefuse.exe") --chip esp32s3 --port $Port summary | Out-File (Join-Path $OutDir "efuse-summary.txt")

foreach ($n in 1, 2) {
    $target = Join-Path $OutDir "e1002-factory-backup-$n.bin"
    & $esptool --chip esp32s3 --port $Port --baud $Baud read-flash 0x0 0x2000000 $target
    if ($LASTEXITCODE -ne 0) { throw "read-flash $n failed" }
}

& (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe") (Join-Path $PSScriptRoot "verify-backup.py") $OutDir
