"""Installer maintenance requests executed with the application owner's privileges.

This is an internal JSON protocol, not a command execution or replay interface.
The administrator holds the returned service/lifetime locks across release changes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from app_watch.config import load_config, prepare_config
from app_watch.config.identity import WatchIdentity
from app_watch.errors import BlockedError, ConfigurationError, StateError
from app_watch.services.monitor import (
    discovered_units,
    properties,
    read_service,
    render_service,
    require_safe_stop,
    service_name,
    systemctl,
    units_root,
    verify_loaded,
    write_service,
)
from app_watch.services.systemd import RealSystemdController
from app_watch.trigger.locking import lock_held
from app_watch.trigger.paths import (
    account_home,
    monitor_lock_path,
    runtime_dir,
    state_root,
    validate_scope,
)
from app_watch.trigger.state import StateStore, checked_id, private_directory


def check_all_recovery() -> None:
    """Include orphan namespaces, even when no installed monitor references them."""
    root = state_root("0" * 16).parent
    if not root.exists():
        return
    for directory in root.iterdir():
        validate_scope(directory.name)
        store = StateStore(directory)
        store._check(directory / "unused")
        states = {}
        for path in (directory / "instances").glob("*.json"):
            instance_id = checked_id(path.stem)
            state = store.read_instance(instance_id)
            if state is None:
                raise StateError("Instance disappeared during maintenance")
            states[instance_id] = state
            if state.active_attempt_id or state.pending_request or state.blocked_reason:
                raise BlockedError(
                    "Active, pending or blocked recovery prevents software maintenance"
                )
        if (directory / "blocked").exists() and any((directory / "blocked").iterdir()):
            raise BlockedError("Blocked recovery prevents software maintenance")
        for attempt_id in store.list_attempt_ids():
            # Completed history may be partly cleaned after a monitor interruption.
            if any(attempt_id in state.completed_attempts for state in states.values()):
                continue
            raise BlockedError("Unreconciled attempt prevents software maintenance")


def check_foreground(plan: dict) -> None:
    known = {item["monitor_lock"] for item in plan["units"]}
    for path in (runtime_dir() / "app-watch/jobs").glob("*/monitor.lock"):
        if str(path) not in known and lock_held(path):
            raise BlockedError("An unmanaged foreground monitor prevents software maintenance")


def snapshot(command: Path, *, validate_config: bool) -> dict:
    worker = units_root() / "app-watch-exec@.service"
    overrides = worker.with_name(worker.name + ".d")
    if any(path.exists() or path.is_symlink() for path in (worker, overrides)):
        raise BlockedError("Per-user worker overrides require review before software maintenance")
    names = discovered_units()
    workers = systemctl(
        "list-units", "--all", "--no-legend", "--no-pager", "app-watch-exec@*.service"
    )
    for line in workers.splitlines():
        words = line.split()
        if words and words[0] == "●":
            words = words[1:]
        if not words:
            continue
        if len(words) < 4 or words[2] not in ("inactive", "failed"):
            raise BlockedError("Active or unknown worker prevents software maintenance")
    jobs = systemctl("list-jobs", "--no-legend", "--no-pager", "--full")
    if any("app-watch-exec@" in line for line in jobs.splitlines()):
        raise BlockedError("Queued worker prevents software maintenance")
    check_all_recovery()
    units = []
    for unit in names:
        installed = read_service(unit)
        if installed.executable != command:
            raise BlockedError("A monitor belongs to another installation; review it first")
        values = verify_loaded(installed)
        require_safe_stop(installed.identity)
        if validate_config:
            try:
                watch = prepare_config(
                    load_config(Path(installed.identity.source_path)),
                    Path(installed.identity.source_path),
                    runtime_params=installed.identity.params,
                )
                if watch.scope != installed.identity.scope or not watch.config.trigger.enabled:
                    raise ConfigurationError("Installed selection differs from configuration")
            except ConfigurationError as error:
                # Do not expose YAML/provider contents to the administrative transcript.
                raise ConfigurationError(
                    f"Configuration validation failed for {installed.identity.source_path}; "
                    "run app-watch validate as its owner"
                ) from error
        lock = monitor_lock_path(Path(installed.identity.source_path))
        active = values.get("ActiveState")
        if active not in ("active", "activating", "reloading", "inactive", "failed"):
            raise BlockedError("Monitor activity is unknown")
        units.append(
            {
                "unit": unit,
                "identity": installed.identity.model_dump(),
                "before": installed.content,
                "after": installed.content,
                "was_enabled": values.get("UnitFileState") in ("enabled", "enabled-runtime"),
                "was_active": active in ("active", "activating", "reloading")
                or values.get("Job") not in ("", "0"),
                "service_lock": str(lock.with_name("service.lock")),
                "monitor_lock": str(lock),
            }
        )
    plan = {"units": units, "home": str(account_home()), "units_root": str(units_root())}
    check_foreground(plan)
    return plan


def validate_snapshot(
    plan: dict,
    command: Path,
    *,
    config: bool,
    recovery: bool = False,
    capture_activity: bool = False,
) -> None:
    if plan["home"] != str(account_home()) or plan["units_root"] != str(units_root()):
        raise BlockedError("Account paths changed during software maintenance")
    check_all_recovery()
    check_foreground(plan)
    for item in plan["units"]:
        identity = WatchIdentity.model_validate(item["identity"])
        if item["unit"] != service_name(Path(identity.source_path)):
            raise BlockedError("Contradictory maintenance selection")
        path = units_root() / item["unit"]
        if recovery and item.get("remove_intended") and not path.exists() and not path.is_symlink():
            require_safe_stop(identity)
            item["after"] = render_service(identity, command)
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
            ):
                raise BlockedError("Unsafe monitor file during maintenance")
            content = stream.read(65537)
        allowed = (item["before"], item["after"]) if recovery else (item["before"],)
        if content not in allowed or path.with_name(path.name + ".d").exists():
            raise BlockedError("Monitor changed during software maintenance")
        values = properties(item["unit"])
        if capture_activity:
            item["was_enabled"] = values.get("UnitFileState") in ("enabled", "enabled-runtime")
            item["was_active"] = values.get("ActiveState") in (
                "active",
                "activating",
                "reloading",
            ) or values.get("Job") not in ("", "0")
        if (
            values.get("FragmentPath") != str(path)
            or values.get("DropInPaths") != ""
            or values.get("LoadState") != "loaded"
            or (not recovery and values.get("NeedDaemonReload") != "no")
        ):
            raise BlockedError("Effective monitor ownership is unverified")
        require_safe_stop(identity)
        if config:
            try:
                watch = prepare_config(
                    load_config(Path(identity.source_path)),
                    Path(identity.source_path),
                    runtime_params=identity.params,
                )
                if watch.scope != identity.scope or not watch.config.trigger.enabled:
                    raise ConfigurationError("Configuration selection changed")
            except ConfigurationError as error:
                raise ConfigurationError(
                    f"Configuration validation failed for {identity.source_path}; "
                    "run app-watch validate as its owner"
                ) from error
        item["after"] = render_service(identity, command)


def prepare_locks(plan: dict) -> None:
    for item in plan["units"]:
        lock = monitor_lock_path(Path(item["identity"]["source_path"]))
        for path in (lock, lock.with_name("service.lock")):
            private_directory(path.parent)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077
                ):
                    raise BlockedError("Unsafe maintenance lock")
            finally:
                os.close(fd)


def pause(plan: dict) -> None:
    check_all_recovery()
    for item in plan["units"]:
        require_safe_stop(WatchIdentity.model_validate(item["identity"]))
        systemctl("stop", item["unit"])
        values = properties(item["unit"])
        if values.get("ActiveState") not in ("inactive", "failed") or values.get("Job") not in (
            "",
            "0",
        ):
            raise BlockedError("Monitor did not stop")


def refresh(
    plan: dict,
    *,
    restore: bool = False,
    remove: bool = False,
    worker: Path | None = None,
) -> None:
    check_all_recovery()
    for item in plan["units"]:
        identity = WatchIdentity.model_validate(item["identity"])
        require_safe_stop(identity)
        path = units_root() / item["unit"]
        if remove:
            systemctl("disable", item["unit"])
            path.unlink()
        else:
            write_service(path, item["before"] if restore else item["after"])
            if restore and item["was_enabled"]:
                systemctl("enable", item["unit"])
    systemctl("daemon-reload")
    if worker is not None:
        content = systemctl("cat", "--no-pager", "app-watch-exec@.service")
        headers = [line[2:] for line in content.splitlines() if line.startswith("# /")]
        if headers != [str(worker)] or not RealSystemdController().template_ready():
            raise BlockedError("Effective shared worker differs from this installation")


def resume(plan: dict) -> None:
    for item in plan["units"]:
        if item["was_active"]:
            systemctl("start", item["unit"])


def main() -> int:
    if os.geteuid() == 0:
        raise BlockedError("User maintenance must run as the non-root application owner")
    request = json.load(sys.stdin)
    if type(request.get("protocol_version")) is not int or request["protocol_version"] != 1:
        raise BlockedError("Unsupported maintenance protocol")
    action = request["action"]
    plan = request.get("plan", {})
    command = Path(request["command"])
    if action == "snapshot":
        plan = snapshot(command, validate_config=request.get("validate_config", True))
    elif action == "validate":
        validate_snapshot(
            plan,
            command,
            config=request.get("validate_config", True),
            recovery=request.get("recovery", False),
            capture_activity=request.get("capture_activity", False),
        )
    elif action == "locks":
        prepare_locks(plan)
    elif action == "pause":
        pause(plan)
    elif action == "refresh":
        refresh(
            plan,
            restore=request.get("restore", False),
            remove=request.get("remove", False),
            worker=Path(request["worker_path"]) if request.get("verify_worker") else None,
        )
    elif action == "resume":
        resume(plan)
    else:
        raise BlockedError("Unknown maintenance request")
    print(json.dumps({"plan": plan}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BlockedError, ConfigurationError, StateError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(2) from None
