"""Fixed account-owned recovery paths and private runtime locks."""

from __future__ import annotations

import hashlib
import os
import pwd
import re
import stat
from pathlib import Path

from app_watch.errors import BlockedError


def account_home() -> Path:
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (KeyError, OSError) as error:
        raise BlockedError("Cannot resolve execution account home") from error
    if not home.is_absolute():
        raise BlockedError("Execution account home must be absolute")
    return home


def validate_scope(scope: str) -> str:
    if re.fullmatch(r"[0-9a-f]{16}", scope) is None:
        raise BlockedError("Invalid App-watch execution namespace")
    return scope


def path_scope(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def state_root(scope: str) -> Path:
    return account_home() / ".local/state/app-watch/jobs" / validate_scope(scope)


def runtime_dir() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR", "")
    path = Path(value)
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise BlockedError("XDG_RUNTIME_DIR must be a private directory owned by this user")
    except OSError as error:
        raise BlockedError("A usable XDG_RUNTIME_DIR is required") from error
    return path


def monitor_lock_path(path: Path) -> Path:
    return runtime_dir() / "app-watch/jobs" / path_scope(path) / "monitor.lock"
