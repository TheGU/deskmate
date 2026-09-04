"""Verify reTerminal E1002 factory flash dumps.

Checks that both dumps exist, are exactly 32 MiB (33,554,432 bytes), have
identical SHA-256 hashes, and writes SHA256SUMS.txt next to them.

Usage:
    python scripts/verify-backup.py [private-backups]
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

EXPECTED_SIZE = 0x2000000  # 33,554,432 bytes, full 32 MiB flash
DUMPS = ("e1002-factory-backup-1.bin", "e1002-factory-backup-2.bin")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    backup_dir = Path(argv[1]) if len(argv) > 1 else Path("private-backups")
    ok = True
    hashes: dict[str, str] = {}
    for name in DUMPS:
        path = backup_dir / name
        if not path.is_file():
            print(f"MISSING  {path}")
            ok = False
            continue
        size = path.stat().st_size
        digest = sha256_of(path)
        hashes[name] = digest
        size_ok = size == EXPECTED_SIZE
        ok = ok and size_ok
        print(f"{'OK     ' if size_ok else 'BADSIZE'}  {name}  size={size}  sha256={digest}")

    if len(hashes) == 2:
        first, second = (hashes[n] for n in DUMPS)
        if first == second:
            print("MATCH    both dumps have identical SHA-256")
        else:
            print("MISMATCH dumps differ. Do not flash. Investigate.")
            ok = False

    if ok:
        sums = backup_dir / "SHA256SUMS.txt"
        sums.write_text("".join(f"{hashes[n]}  {n}\n" for n in DUMPS), encoding="ascii")
        print(f"WROTE    {sums}")
        print()
        print("FACTORY BACKUP VERIFIED")
        print("SAFE TO CONTINUE TO NON-DESTRUCTIVE DEVELOPMENT")
        print("WAITING FOR USER TO TYPE: FLASH")
        return 0
    print()
    print("BACKUP NOT VERIFIED. Do not flash.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
