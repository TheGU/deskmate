#!/usr/bin/env bash
# Dump the full 32 MB flash of a reTerminal E1002 twice and verify.
# Read-only. Does not modify the device.
#
# Usage: scripts/backup-firmware.sh /dev/ttyUSB0 [460800]
set -euo pipefail
PORT="${1:-/dev/ttyUSB0}"
BAUD="${2:-460800}"
OUT="private-backups"
mkdir -p "$OUT"
ESPTOOL="${ESPTOOL:-esptool}"
ESPEFUSE="${ESPEFUSE:-espefuse}"

"$ESPTOOL" --chip esp32s3 --port "$PORT" flash-id | tee "$OUT/device-info.txt"
"$ESPEFUSE" --chip esp32s3 --port "$PORT" summary > "$OUT/efuse-summary.txt"
for n in 1 2; do
  "$ESPTOOL" --chip esp32s3 --port "$PORT" --baud "$BAUD" read-flash 0x0 0x2000000 "$OUT/e1002-factory-backup-$n.bin"
done
python scripts/verify-backup.py "$OUT"
