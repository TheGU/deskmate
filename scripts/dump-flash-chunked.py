"""Read the full 32 MiB flash of an ESP32-S3 in chunks with retries.

The CH340 USB-UART bridge on the reTerminal E1002 aborted two single-pass
read-flash runs on this PC. Reading in 2 MiB chunks and retrying a failed
chunk keeps one serial hiccup from throwing away the whole dump. Read-only.

Usage:
    python scripts/dump-flash-chunked.py --port COM3 --out private-backups/e1002-factory-backup-1.bin
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FLASH_SIZE = 0x2000000
CHUNK = 0x200000  # 2 MiB


def read_chunk(esptool: Path, port: str, baud: int, offset: int, target: Path) -> bool:
    cmd = [
        str(esptool), "--chip", "esp32s3", "--port", port, "--baud", str(baud),
        "read-flash", hex(offset), hex(CHUNK), str(target),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = result.stdout.replace("\r", "\n").strip().splitlines()[-1:] + result.stderr.strip().splitlines()[-1:]
        print(f"  chunk {offset:#010x} failed at {baud}: {' | '.join(tail)}", flush=True)
        return False
    return target.stat().st_size == CHUNK


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--esptool", type=Path, default=Path(".venv/Scripts/esptool.exe"))
    parser.add_argument("--bauds", default="460800,230400,115200")
    args = parser.parse_args()
    bauds = [int(b) for b in args.bauds.split(",")]

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="e1002-chunks-") as tmp:
        parts: list[Path] = []
        for offset in range(0, FLASH_SIZE, CHUNK):
            part = Path(tmp) / f"chunk-{offset:#010x}.bin"
            ok = False
            for attempt in range(1, 4):
                baud = bauds[min(attempt - 1, len(bauds) - 1)]
                print(f"chunk {offset:#010x} attempt {attempt} baud {baud}", flush=True)
                if read_chunk(args.esptool, args.port, baud, offset, part):
                    ok = True
                    break
                time.sleep(2)
            if not ok:
                print(f"GIVING UP at {offset:#010x}", flush=True)
                return 2
            parts.append(part)

        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("wb") as out:
            for part in parts:
                out.write(part.read_bytes())

    size = args.out.stat().st_size
    print(f"wrote {args.out} size={size} elapsed={time.time() - started:.0f}s", flush=True)
    if size != FLASH_SIZE:
        print("SIZE MISMATCH", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
