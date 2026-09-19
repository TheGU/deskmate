"""Refuse the characters this repository does not use.

Scans every tracked text file for an em dash (U+2014), a rightwards arrow
(U+2192) and emoji: the repository writes plain keyboard punctuation. Run it
from the repository root before a commit; CONTRIBUTING.md explains the rule.

Usage: ``python scripts/check-plain-ascii.py [path ...]``. With no paths it
checks every file ``git ls-files`` reports. Exit status 1 when anything is
found, with one line per finding: ``path:line: reason``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EM_DASH = chr(0x2014)
ARROW = chr(0x2192)
BINARY_SUFFIXES = {".ttf", ".png", ".webp", ".jpg", ".jpeg", ".gif", ".ico", ".sqlite", ".bin"}


def is_emoji(char: str) -> bool:
    code = ord(char)
    return 0x1F300 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or 0x1F000 <= code <= 0x1F2FF


def problems(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if EM_DASH in line:
            found.append(f"{path}:{number}: em dash")
        if ARROW in line:
            found.append(f"{path}:{number}: arrow")
        if any(is_emoji(char) for char in line):
            found.append(f"{path}:{number}: emoji")
    return found


def tracked_files() -> list[Path]:
    output = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [Path(name) for name in output.decode("utf-8").split("\0") if name]


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv] or tracked_files()
    findings: list[str] = []
    for path in paths:
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        findings.extend(problems(path))
    for line in findings:
        print(line)
    print("clean" if not findings else f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
