"""Installed command resolution and literal systemd argument quoting."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from app_watch.errors import BlockedError, ConfigurationError


class MaintenanceBusy(BlockedError):
    """Software activation or interrupted recovery temporarily excludes runtime changes."""


@contextmanager
def software_guard():
    """Share the installer's existing lock; development pip installs have no deployment layout."""
    metadata = Path(sys.prefix).parent / "deployment.json"
    try:
        prefix = Path(json.loads(metadata.read_text())["layout"]["APP_WATCH_INSTALL_PREFIX"])
    except FileNotFoundError:
        yield
        return
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigurationError("Cannot verify software maintenance state") from error
    if not prefix.is_absolute():
        raise ConfigurationError("Installation prefix must be absolute")
    fd = os.open(prefix / "install.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != prefix.stat().st_uid
            or info.st_mode & 0o022
        ):
            raise ConfigurationError("Installer lock ownership is unsafe")
        try:
            # POSIX write locks require a writable descriptor. A read-only local
            # user cannot impersonate the installer's exclusive maintenance lock.
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceBusy("Software maintenance is active") from error
        if (prefix / "transaction.json").exists():
            raise MaintenanceBusy("Interrupted software maintenance needs installer recovery")
        yield
    finally:
        os.close(fd)


def command_path() -> Path:
    """Use this release's deployment layout, or its development console entrypoint."""
    metadata = Path(sys.prefix).parent / "deployment.json"
    try:
        document = json.loads(metadata.read_text())
        value = document["layout"]["APP_WATCH_BIN_LINK"]
    except FileNotFoundError:
        value = str(Path(sys.executable).parent / "app-watch")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigurationError(f"Cannot read deployment layout: {error}") from error
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ConfigurationError("Installed command must be an absolute path")
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigurationError(f"App-watch command is unavailable: {path}")
    return path


def _escape_systemd(value: str, *, executable: bool = False) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("systemd arguments must not contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    # systemd does not expand environment variables in the executable token.
    return escaped if executable else escaped.replace("$", "$$")


def systemd_argument(value: str, *, executable: bool = False) -> str:
    """Quote a literal ExecStart argument without shell or systemd expansion."""
    escaped = _escape_systemd(value, executable=executable)
    return value if re.fullmatch(r"[A-Za-z0-9_./:=,@+-]+", value) else f'"{escaped}"'
