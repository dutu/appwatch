"""Nonblocking account-owned monitor, lifecycle and attempt locks."""

import fcntl
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app_watch.errors import BlockedError, StateError
from app_watch.trigger.state import private_directory


@contextmanager
def exclusive_lock(path: Path):
    private_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise StateError(f"Unsafe lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BlockedError(f"App-watch operation already active: {path}") from error
        yield fd
    finally:
        os.close(fd)


@contextmanager
def monitor_lock(path: Path):
    """Mark the lifetime lock with this monitor session's start time."""
    with exclusive_lock(path) as fd:
        os.ftruncate(fd, 0)
        os.write(fd, datetime.now(UTC).isoformat().encode("ascii"))
        try:
            yield
        finally:
            # A later maintenance lock holder must not inherit this session.
            os.ftruncate(fd, 0)


def monitor_started_at(path: Path) -> datetime | None:
    """Read an existing session marker without creating or changing files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StateError(f"Unsafe lock file: {path}")
        try:
            started = datetime.fromisoformat(os.read(fd, 128).decode("ascii"))
        except (ValueError, UnicodeError):
            return None
        if started.tzinfo is None or started.utcoffset().total_seconds() != 0:
            return None
        return started
    finally:
        os.close(fd)


def lock_held(path: Path) -> bool:
    """Inspect an existing lock without creating directories or lock records."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StateError(f"Unsafe lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)
