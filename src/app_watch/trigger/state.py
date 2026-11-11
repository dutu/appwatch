"""Private, atomic and durable recovery records; corrupt evidence is never repaired."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from app_watch.errors import BlockedError, StateError
from app_watch.trigger.models import (
    InstanceState,
    LaunchRecord,
    Receipt,
    Request,
    StrictModel,
    utc_now,
)

T = TypeVar("T", bound=StrictModel)
DIAGNOSTICS_CAP = 256 * 1024


def private_directory(path: Path) -> None:
    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        fsync_directory(directory.parent)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise StateError(f"Expected private account-owned directory: {path}")


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, immutable: bool = False) -> None:
    private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            # Unlike exists()+replace(), link cannot clobber another writer's record.
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def checked_id(value: str, *, attempt: bool = False) -> str:
    pattern = (
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
        if attempt
        else r"[A-Za-z0-9_-]{1,128}"
    )
    if re.fullmatch(pattern, value) is None:
        raise StateError("Invalid recovery record identifier")
    return value


class StateStore:
    def __init__(self, root: Path):
        self.root = root

    def _check(self, path: Path) -> None:
        # Walk only App-watch's private subtree; do not chmod parent directories.
        current = self.root
        directories = [current]
        for name in path.relative_to(self.root).parts[:-1]:
            current = current / name
            directories.append(current)
        for current in directories:
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                return
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                raise StateError(f"Unsafe recovery directory: {current}")

    def ensure(self) -> None:
        self._check(self.root / "state.json")
        private_directory(self.root)
        for name in ("instances", "attempts", "blocked"):
            private_directory(self.root / name)
        fsync_directory(self.root.parent)

    def instance_path(self, instance_id: str) -> Path:
        return self.root / "instances" / f"{checked_id(instance_id)}.json"

    def attempt_dir(self, attempt_id: str) -> Path:
        return self.root / "attempts" / checked_id(attempt_id, attempt=True)

    def read_bytes(self, path: Path) -> bytes | None:
        self._check(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StateError(f"Cannot read recovery record {path}: {error}") from error
        try:
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077
                    or metadata.st_size > 4 * 1024 * 1024
                ):
                    raise StateError(f"Unsafe recovery record: {path}")
                return stream.read(4 * 1024 * 1024 + 1)
        except OSError as error:
            raise StateError(f"Cannot read recovery record {path}: {error}") from error

    def read(self, path: Path, model: type[T]) -> T | None:
        data = self.read_bytes(path)
        if data is None:
            return None
        try:
            return model.model_validate_json(data)
        except (ValidationError, ValueError) as error:
            raise StateError(f"Invalid recovery record {path}: {error}") from error

    def write(self, path: Path, record: StrictModel, *, immutable: bool = False) -> None:
        self._check(path)
        try:
            atomic_write(path, record.model_dump_json(indent=2).encode(), immutable=immutable)
        except OSError as error:
            raise StateError(f"Cannot persist recovery record {path}: {error}") from error

    def read_instance(self, instance_id: str) -> InstanceState | None:
        result = self.read(self.instance_path(instance_id), InstanceState)
        if result is not None and result.instance_id != instance_id:
            raise StateError("Contradictory instance ownership")
        return result

    def write_instance(self, state: InstanceState) -> None:
        self.write(self.instance_path(state.instance_id), state)

    def write_request(self, attempt_id: str, request: Request) -> None:
        self.write(self.attempt_dir(attempt_id) / "request.json", request, immutable=True)

    def read_request(self, attempt_id: str) -> Request | None:
        return self.read(self.attempt_dir(attempt_id) / "request.json", Request)

    def write_launch(self, launch: LaunchRecord) -> None:
        self.write(self.attempt_dir(launch.attempt_id) / "launch.json", launch)

    def read_launch(self, attempt_id: str) -> LaunchRecord | None:
        result = self.read(self.attempt_dir(attempt_id) / "launch.json", LaunchRecord)
        if result is not None and result.attempt_id != attempt_id:
            raise StateError("Contradictory launch ownership")
        return result

    def write_receipt(self, receipt: Receipt) -> None:
        self.write(self.attempt_dir(receipt.attempt_id) / "receipt.json", receipt, immutable=True)

    def read_receipt(self, attempt_id: str) -> Receipt | None:
        result = self.read(self.attempt_dir(attempt_id) / "receipt.json", Receipt)
        if result is not None and result.attempt_id != attempt_id:
            raise StateError("Contradictory receipt ownership")
        return result

    def list_attempt_ids(self) -> list[str]:
        self._check(self.root / "attempts" / "unused")
        try:
            return sorted(
                checked_id(path.name, attempt=True) for path in (self.root / "attempts").iterdir()
            )
        except FileNotFoundError:
            return []

    def block(self, instance_id: str, reason: str) -> None:
        path = self.root / "blocked" / f"{checked_id(instance_id)}.json"
        self._check(path)
        if self.read_bytes(path) is not None:
            return
        atomic_write(
            path,
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": instance_id,
                    "reason": reason,
                    "blocked_at": utc_now(),
                }
            ).encode(),
            immutable=True,
        )

    def blocked_reason(self, instance_id: str) -> str | None:
        path = self.root / "blocked" / f"{checked_id(instance_id)}.json"
        data = self.read_bytes(path)
        if data is None:
            return None
        try:
            record = json.loads(data)
            if (
                type(record.get("schema_version")) is not int
                or record["schema_version"] != 1
                or record.get("instance_id") != instance_id
                or not isinstance(record.get("reason"), str)
                or not record["reason"]
            ):
                raise ValueError("Invalid blocked-instance diagnostic")
            return record["reason"]
        except (ValueError, AttributeError) as error:
            raise StateError(f"Corrupt blocked-instance diagnostic: {path}") from error

    def write_diagnostics(self, attempt_id: str, data: bytes) -> None:
        path = self.attempt_dir(attempt_id) / "log.txt"
        self._check(path)
        atomic_write(path, data[-DIAGNOSTICS_CAP:])

    def remove_attempt(self, attempt_id: str) -> bool:
        """Remove only reconciled history after the caller verifies worker absence."""
        from app_watch.trigger.locking import exclusive_lock

        directory = self.attempt_dir(attempt_id)
        self._check(directory / "unused")
        if not directory.exists():
            return True
        allowed = {
            "request.json",
            "launch.json",
            "receipt.json",
            "execution.json",
            "log.txt",
            "worker.lock",
        }
        try:
            with exclusive_lock(directory / "worker.lock"):
                for path in directory.iterdir():
                    if path.name not in allowed or path.is_symlink() or not path.is_file():
                        raise StateError("Unexpected cleanup evidence; refusing to delete")
                for path in directory.iterdir():
                    path.unlink()
                directory.rmdir()
                fsync_directory(directory.parent)
        except BlockedError:
            return False
        return True
