"""Bounded fallback for protected executable paths; all other inspection stays local."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path


def protected_executables(owner_uid: int) -> dict[int, tuple[float, str]]:
    """Failure returns no identities, never permission to ignore a process."""
    if owner_uid != os.geteuid() or owner_uid == 0:
        return {}
    prefix = Path("/opt/app-watch")
    metadata = Path(sys.prefix).parent / "deployment.json"
    try:
        if metadata.exists():
            prefix = Path(json.loads(metadata.read_text())["layout"]["APP_WATCH_INSTALL_PREFIX"])
        helper = prefix / "process-inspect"
        if not helper.is_absolute() or not helper.is_file():
            return {}
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "--", str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        if result.returncode != 0 or len(result.stdout) > 4 * 1024 * 1024:
            return {}
        document = json.loads(result.stdout)
        if (
            type(document["version"]) is not int
            or document["version"] != 1
            or type(document["uid"]) is not int
            or document["uid"] != owner_uid
            or not isinstance(document["processes"], list)
        ):
            return {}
        records = {}
        for pid, created, executable in document["processes"]:
            if (
                type(pid) is not int
                or pid <= 0
                or type(created) not in (int, float)
                or not math.isfinite(created)
                or not isinstance(executable, str)
                or not executable.startswith("/")
                or "\0" in executable
                or pid in records
            ):
                return {}
            records[pid] = (created, executable)
        return records
    except (OSError, ValueError, OverflowError, KeyError, TypeError, subprocess.TimeoutExpired):
        return {}
