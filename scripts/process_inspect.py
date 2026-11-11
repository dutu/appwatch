#!/usr/bin/python3 -I
"""Root-only, read-only executable inspection for the sudo caller's processes.

Installed as a standalone script. Never import application code or accept paths,
arguments, stdin requests, or a caller-selected UID.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def identity(directory: Path) -> tuple[int, int]:
    """Read kernel ownership and start ticks, without trusting process argv/name."""
    status = (directory / "status").read_text()
    uid = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("Uid:"))
    fields = (directory / "stat").read_text().rpartition(") ")[2].split()
    return uid, int(fields[19])


def inspect(uid: int) -> list[list]:
    records = []
    ticks = os.sysconf("SC_CLK_TCK")
    boot = next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    for directory in Path("/proc").iterdir():
        if not directory.name.isascii() or not directory.name.isdecimal():
            continue
        try:
            before = identity(directory)
            if before[0] != uid:
                continue
            executable = os.readlink(directory / "exe")
            if not executable.startswith("/") or identity(directory) != before:
                continue
            records.append([int(directory.name), boot + before[1] / ticks, executable])
        except (OSError, ValueError, IndexError, StopIteration):
            # An unverified record is never evidence of absence for the caller.
            continue
    return records


def main() -> int:
    caller = os.environ.get("SUDO_UID", "")
    if os.geteuid() != 0 or len(sys.argv) != 1 or not caller.isascii() or not caller.isdecimal():
        return 2
    uid = int(caller)
    if uid <= 0:
        return 2
    result = json.dumps({"version": 1, "uid": uid, "processes": inspect(uid)}, ensure_ascii=True)
    if len(result) > 4 * 1024 * 1024:
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
