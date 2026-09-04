"""Sanity-check a full ESP32-S3 flash dump without touching the device.

Prints the bootloader magic, the partition table, and how much of the
image is erased (all 0xFF). Usage:
    python scripts/inspect-backup.py private-backups/e1002-factory-backup-1.bin
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(path: str) -> int:
    data = Path(path).read_bytes()
    print(f"size {len(data)} bytes")
    print(f"bootloader magic at 0x0: {data[0]:#04x} (0xe9 expected)")
    table = data[0x8000:0x9000]
    print(f"partition table magic at 0x8000: {table[:2].hex()} (aa50 expected)")
    offset = 0
    while offset < 0xC00 and table[offset:offset + 2] == b"\xaa\x50":
        entry = table[offset:offset + 32]
        ptype, subtype = entry[2], entry[3]
        start = int.from_bytes(entry[4:8], "little")
        size = int.from_bytes(entry[8:12], "little")
        name = entry[12:28].split(b"\0")[0].decode(errors="replace")
        print(f"  {name:12s} type={ptype} sub={subtype:#04x} offset={start:#09x} size={size:#09x}")
        offset += 32
    blank = b"\xff" * 4096
    erased = sum(1 for i in range(0, len(data), 4096) if data[i:i + 4096] == blank)
    print(f"all-0xFF 4K blocks: {erased} of {len(data) // 4096}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
