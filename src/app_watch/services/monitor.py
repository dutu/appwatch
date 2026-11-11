"""Verified per-path monitor units and safe user-service lifecycle operations."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app_watch.config import PreparedWatch
from app_watch.config.identity import WatchIdentity, canonical_source
from app_watch.errors import BlockedError, ConfigurationError
from app_watch.installation import command_path, software_guard, systemd_argument
from app_watch.services.systemd import RealSystemdController, _run
from app_watch.trigger.locking import exclusive_lock
from app_watch.trigger.paths import account_home, monitor_lock_path, path_scope
from app_watch.trigger.state import fsync_directory


def units_root() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else account_home() / ".config"
    return base / "systemd/user"


def jobs_root() -> Path:
    return units_root().parent.parent / "app-watch/jobs"


def service_name(source: Path) -> str:
    source = canonical_source(source)
    stem = source.stem
    escaped = "".join(
        chr(byte)
        if byte in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-."
        and not (index == 0 and byte == ord("."))
        else f"\\x{byte:02x}"
        for index, byte in enumerate(os.fsencode(stem))
    )
    unit = f"app-watch-monitor@{escaped}-{path_scope(source)}.service"
    if len(unit) > 255:
        raise ConfigurationError("Configuration filename is too long for a systemd unit name")
    return unit


def systemctl(*arguments: str) -> str:
    try:
        result = _run(["systemctl", "--user", *arguments], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BlockedError(f"Cannot contact systemd user manager: {error}") from error
    # Empty unit-file listings have a distinct documented exception in VaultKeep.
    empty = (
        arguments[0] == "list-unit-files"
        and result.returncode == 1
        and not (result.stdout.strip() or result.stderr.strip())
    )
    if result.returncode and not empty:
        raise BlockedError(f"systemctl {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def write_service(path: Path, content: str) -> None:
    # systemd/user may already be 0755; unit records themselves remain private.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise BlockedError("User unit directory is not safely owned")
    fd, name = tempfile.mkstemp(prefix=".app-watch-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def properties(unit: str) -> dict[str, str]:
    output = systemctl(
        "show",
        "--all",
        "--property=LoadState,ActiveState,SubState,Job,UnitFileState,"
        "FragmentPath,DropInPaths,NeedDaemonReload,ExecMainStatus",
        unit,
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def render_service(identity: WatchIdentity, executable: Path) -> str:
    unit = service_name(Path(identity.source_path))
    selection = {"identity": identity.model_dump(), "executable": str(executable)}
    argv = [str(executable), "--user", "--config", identity.source_path]
    for key, value in sorted(identity.params.items()):
        argv.extend(["--param", f"{key}={value}"])
    argv.extend(["trigger", "monitor", "--expected-identity", identity.scope])
    command = " ".join(
        systemd_argument(arg, executable=index == 0) for index, arg in enumerate(argv)
    )
    return (
        f"# Managed by App-watch: {unit}\n"
        f"# App-watch selection: {json.dumps(selection, sort_keys=True)}\n"
        "[Unit]\n"
        f"Description=App-watch shutdown monitor for {identity.job_id}\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\nRestartSec=5\nUMask=0077\n"
        "StandardOutput=journal\nStandardError=journal\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


@dataclass(frozen=True)
class InstalledService:
    unit: str
    identity: WatchIdentity
    executable: Path
    content: str


def read_service(unit: str) -> InstalledService:
    if (
        Path(unit).name != unit
        or not unit.startswith("app-watch-monitor@")
        or not unit.endswith(".service")
    ):
        raise ConfigurationError("Invalid monitor unit name")
    path = units_root() / unit
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o022
                or info.st_size > 65536
            ):
                raise ValueError("Expected a regular account-owned monitor unit")
            content = stream.read(65537)
        metadata = [
            line.removeprefix("# App-watch selection: ")
            for line in content.splitlines()
            if line.startswith("# App-watch selection: ")
        ]
        if len(metadata) != 1:
            raise ValueError("Missing or duplicate installed selection")
        selection = json.loads(metadata[0])
        if set(selection) != {"identity", "executable"} or not isinstance(
            selection["executable"], str
        ):
            raise ValueError("Invalid installed selection")
        identity = WatchIdentity.model_validate(selection["identity"])
        executable = Path(selection["executable"])
        if not executable.is_absolute():
            raise ValueError("Installed command must be absolute")
        if unit != service_name(Path(identity.source_path)) or content != render_service(
            identity, executable
        ):
            raise ValueError("Modified or inconsistent managed unit")
        overrides = path.with_name(unit + ".d")
        if overrides.exists() or overrides.is_symlink():
            raise ValueError("Monitor has local overrides; review them first")
        return InstalledService(unit, identity, executable, content)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, ValidationError) as error:
        raise ConfigurationError(f"Cannot verify monitor {unit}: {error}") from error


def discovered_units(*, manager: bool = True) -> list[str]:
    names = {path.name for path in units_root().glob("app-watch-monitor@*.service")}
    if manager:
        for operation in ("list-unit-files", "list-units"):
            output = systemctl(
                operation, "--all", "--no-legend", "--no-pager", "app-watch-monitor@*.service"
            )
            names.update(
                word
                for line in output.splitlines()
                for word in line.split()[:2]
                if word.startswith("app-watch-monitor@") and word.endswith(".service")
            )
    return sorted(names)


def find_service(source: Path, *, manager: bool = True) -> InstalledService | None:
    source = canonical_source(source)
    expected = service_name(source)
    matches = []
    for unit in discovered_units(manager=manager):
        installed = read_service(unit)
        if Path(installed.identity.source_path) == source or unit == expected:
            matches.append(installed)
    if len(matches) > 1:
        raise ConfigurationError("Multiple monitors reference this configuration")
    return matches[0] if matches else None


def verify_loaded(installed: InstalledService) -> dict[str, str]:
    values = properties(installed.unit)
    if (
        values.get("FragmentPath") != str(units_root() / installed.unit)
        or values.get("DropInPaths") != ""
        or values.get("NeedDaemonReload") != "no"
        or values.get("LoadState") != "loaded"
    ):
        raise BlockedError(
            "Effective managed monitor is unverified; inspect systemctl cat and daemon-reload"
        )
    return values


def require_monitor_identity(watch: PreparedWatch, expected: str | None = None) -> None:
    if expected is not None and expected != watch.scope:
        raise BlockedError("Installed monitor identity changed; use trigger update")
    installed = find_service(watch.source_path)
    if installed is not None:
        verify_loaded(installed)
        if installed.identity.scope != watch.scope:
            raise BlockedError("Installed execution identity differs; use trigger update")


def require_safe_stop(identity: WatchIdentity) -> None:
    from app_watch.trigger.inspect import inspect_recovery

    result = inspect_recovery(identity)
    if result["blocked"] or result["active_attempt_id"] or result["pending"]:
        raise BlockedError(
            "Active, pending or unreconciled work blocks monitor changes; inspect status"
        )


def manage_service(source: Path, action: str, *, watch: PreparedWatch | None = None) -> None:
    with software_guard():
        _manage_service(source, action, watch=watch)


def _manage_service(source: Path, action: str, *, watch: PreparedWatch | None = None) -> None:
    if action not in {"install", "update", "disable", "remove"}:
        raise ConfigurationError("Unknown monitor lifecycle action")
    if os.geteuid() == 0:
        raise BlockedError("Manage watches as their non-root application owner, without sudo")
    source = canonical_source(source)
    controller = RealSystemdController()
    if not controller.manager_reachable():
        raise BlockedError("systemd user manager is unavailable")
    lock_path = monitor_lock_path(source)
    with exclusive_lock(lock_path.with_name("service.lock")):
        installed = find_service(source)
        if action == "install" and installed is not None:
            raise ConfigurationError("Monitor already installed; use trigger update")
        if action != "install" and installed is None:
            raise ConfigurationError("Monitor is not installed; use trigger install")
        content = None
        if action in ("install", "update"):
            if watch is None or not watch.config.trigger.enabled or watch.source_path != source:
                raise ConfigurationError("Installation requires a prepared, enabled watch")
            if not controller.template_ready():
                raise BlockedError("App-watch worker template is unavailable or incompatible")
            require_safe_stop(watch.identity)
            content = render_service(watch.identity, command_path())
        if installed is not None:
            verify_loaded(installed)
            require_safe_stop(installed.identity)
        # Tick dispatch uses service.lock too, so no new work can race the stop.
        was_enabled = was_active = False
        if installed is not None:
            values = properties(installed.unit)
            was_enabled = values.get("UnitFileState") in ("enabled", "enabled-runtime")
            was_active = values.get("ActiveState") == "active"
            systemctl("disable", "--now", installed.unit)
        unit = service_name(source)
        destination = units_root() / unit
        changed = False
        try:
            with exclusive_lock(lock_path):
                if installed is not None:
                    require_safe_stop(installed.identity)
                if action in ("disable", "remove"):
                    if action == "remove":
                        changed = True
                        destination.unlink()
                        fsync_directory(destination.parent)
                        systemctl("daemon-reload")
                    return
                assert watch is not None and content is not None
                require_safe_stop(watch.identity)
                changed = True
                write_service(destination, content)
                systemctl("daemon-reload")
            systemctl("enable", "--now", unit)
        except (BlockedError, OSError) as error:
            if not changed:
                raise
            try:
                # A failed enable/start may already have activated a monitor.
                # Its selection stays intact unless stopping and recovery checks succeed.
                if destination.exists():
                    current = read_service(unit)
                    allowed = {content, installed.content if installed else None}
                    if current.content not in allowed:
                        raise BlockedError(
                            "Unit changed during lifecycle rollback; preserve it for review"
                        )
                    systemctl("disable", "--now", unit)
                with exclusive_lock(lock_path):
                    if watch is not None:
                        require_safe_stop(watch.identity)
                    if installed is not None:
                        require_safe_stop(installed.identity)
                        write_service(destination, installed.content)
                    elif destination.exists():
                        destination.unlink()
                        fsync_directory(destination.parent)
                    systemctl("daemon-reload")
                if installed is not None:
                    if was_enabled:
                        systemctl("enable", unit)
                    if was_active:
                        systemctl("start", unit)
            except (BlockedError, OSError, ConfigurationError) as rollback_error:
                raise BlockedError(
                    f"Monitor operation failed ({error}); "
                    f"safe rollback could not finish: {rollback_error}"
                ) from rollback_error
            raise
