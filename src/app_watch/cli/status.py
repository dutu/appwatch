"""Read-only watch health and inventory, including broken or missing YAML recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app_watch.config import PreparedWatch, load_config, prepare_config
from app_watch.config.identity import canonical_source
from app_watch.errors import BlockedError, ConfigurationError, StateError
from app_watch.process import Observation, boot_id
from app_watch.services.monitor import (
    discovered_units,
    find_service,
    jobs_root,
    read_service,
    verify_loaded,
)
from app_watch.trigger.inspect import inspect_recovery
from app_watch.trigger.locking import lock_held, monitor_started_at
from app_watch.trigger.models import InstanceState
from app_watch.trigger.paths import monitor_lock_path
from app_watch.trigger.state import StateStore


def _application_status(
    state: InstanceState, store: StateStore, watch: PreparedWatch, started: datetime
) -> str:
    """A stopped observation is distinct from a confirmed shutdown in this session."""
    observation = state.last_observation
    if observation.observation is not Observation.STOPPED:
        return {
            Observation.RUNNING: "RUNNING",
            Observation.AMBIGUOUS: "AMBIGUOUS",
            Observation.INSPECTION_UNAVAILABLE: "INSPECTION UNAVAILABLE",
        }[observation.observation]
    if state.candidate_since or state.pending_reconfirmation_required:
        return "CONFIRMING STOP"
    if state.armed:
        return "UNKNOWN"  # Absence alone has not confirmed the armed lifecycle.
    if state.latest_request_id:
        request = state.pending_request
        if request is None or request.request_id != state.latest_request_id:
            request = None
            attempts = ([state.active_attempt_id] if state.active_attempt_id else []) + list(
                reversed(state.completed_attempts)
            )
            for attempt in attempts:
                candidate = store.read_request(attempt)
                if candidate is not None and candidate.request_id == state.latest_request_id:
                    request = candidate
                    break
        if request is not None:
            if request.scope != watch.scope or request.instance_id != state.instance_id:
                raise StateError("Contradictory shutdown request ownership")
            confirmed = datetime.fromisoformat(request.confirmed_at)
            if (
                request.config_fingerprint == state.config_fingerprint
                and started <= confirmed <= datetime.fromisoformat(observation.observed_at)
            ):
                return "HAS STOPPED"
    return "NOT RUNNING"


def safe_text(value: object) -> str:
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in str(value))


def watch_status(source: Path, *, runtime_params: dict[str, str] | None = None) -> dict:
    result = {
        "config": str(source.absolute()),
        "job_id": None,
        "configuration": "INVALID",
        "enabled": None,
        "monitor": "NOT INSTALLED",
        "service_enabled": None,
        "application": "UNKNOWN",
        "dispatch": "UNKNOWN",
        "active_attempt_id": None,
        "last_result": None,
        "diagnostics": [],
        "details": {},
    }
    try:
        source = canonical_source(source)
        result["config"] = str(source)
    except ConfigurationError as error:
        result["diagnostics"].append(str(error))
        return result
    watch = installed = identity = None
    try:
        installed = find_service(source, manager=False)
        if installed is not None:
            identity = installed.identity
            result["details"]["monitor_service"] = installed.unit
            values = verify_loaded(installed)
            result["service_enabled"] = values.get("UnitFileState") in (
                "enabled",
                "enabled-runtime",
            )
            result["monitor"] = {
                "active": "RUNNING",
                "inactive": "STOPPED",
                "failed": "FAILED",
                "activating": "STARTING",
                "deactivating": "STOPPING",
            }.get(values.get("ActiveState"), "UNKNOWN")
    except (ConfigurationError, BlockedError, StateError, OSError) as error:
        result["monitor"] = "UNKNOWN"
        result["diagnostics"].append(str(error))
    try:
        watch = prepare_config(
            load_config(source), source, runtime_params=runtime_params, check_runtime=False
        )
        result.update(configuration="VALID", enabled=watch.config.trigger.enabled)
        if identity is None:
            identity = watch.identity
        if identity.scope != watch.scope:
            result["diagnostics"].append(
                "Installed execution identity differs from YAML/parameters; use update"
            )
        result["details"]["selected_scope"] = watch.scope
        result["details"]["application_name"] = watch.config.application.name
    except (ConfigurationError, ValueError, OSError) as error:
        result["diagnostics"].append(str(error))
    if identity is None:
        return result
    result["job_id"] = identity.job_id
    result["details"].update(execution_scope=identity.scope, installed_params=identity.params)
    try:
        lock = monitor_lock_path(source)
        started = monitor_started_at(lock)
        recovery = inspect_recovery(identity)
        state = recovery["state"]
        result["details"]["state_directory"] = recovery["state_directory"]
        result["details"]["state"] = state.model_dump(mode="json") if state else None
        result["active_attempt_id"] = recovery["active_attempt_id"]
        result["dispatch"] = (
            "BLOCKED"
            if recovery["blocked"]
            else "ACTIVE"
            if recovery["active_attempt_id"]
            else "PENDING"
            if recovery["pending"]
            else "IDLE"
            if state
            else "UNKNOWN"
        )
        if recovery["blocked"]:
            result["diagnostics"].append(recovery["blocked"])
        if state and state.last_receipt:
            result["last_result"] = state.last_receipt.outcome
        held = lock_held(lock)
        same_session = started is not None and monitor_started_at(lock) == started
        if installed is None and held:
            result["monitor"] = "RUNNING (foreground)"
        if state and state.last_observation and watch is not None:
            observation = state.last_observation
            age = (
                datetime.now(UTC) - datetime.fromisoformat(observation.observed_at)
            ).total_seconds()
            fresh = (
                0 <= age <= max(10, watch.config.trigger.poll_seconds * 3)
                and state.config_fingerprint == watch.fingerprint()
                and observation.boot_id is not None
                and observation.boot_id == boot_id()
                and same_session
                and datetime.fromisoformat(observation.observed_at) >= started
            )
            result["details"]["observation_age_seconds"] = age
            if held and fresh and identity.scope == watch.scope:
                result["application"] = _application_status(
                    state, StateStore(Path(recovery["state_directory"])), watch, started
                )
            elif result["monitor"].startswith("RUNNING"):
                result["monitor"] = "UNKNOWN"
                result["diagnostics"].append("Monitor observation is stale or cannot be verified")
        elif result["monitor"].startswith("RUNNING"):
            result["monitor"] = "UNKNOWN"
            result["diagnostics"].append("Monitor has no verifiable observation yet")
        if result["monitor"].startswith("RUNNING") and not held:
            result["monitor"] = "UNKNOWN"
            result["diagnostics"].append("Monitor lifetime lock is not held")
    except (ConfigurationError, BlockedError, StateError, OSError) as error:
        result["dispatch"] = "BLOCKED"
        result["diagnostics"].append(str(error))
    return result


def inventory() -> list[dict]:
    paths = set(jobs_root().glob("*.yaml")) | set(jobs_root().glob("*.yml"))
    errors = []
    names = set(discovered_units(manager=False))
    try:
        names.update(discovered_units())
    except BlockedError as error:
        # Preserve local entries even when the user manager is unavailable.
        errors.append({"config": "(systemd discovery)", "diagnostics": [str(error)]})
    for unit in sorted(names):
        try:
            paths.add(Path(read_service(unit).identity.source_path))
        except (ConfigurationError, OSError) as error:
            errors.append({"config": unit, "diagnostics": [str(error)]})
    selected = set()
    for path in paths:
        try:
            selected.add(canonical_source(path))
        except ConfigurationError:
            selected.add(path.absolute())
    return [watch_status(path) for path in sorted(selected)] + errors


def print_status(result: dict, *, verbose: bool = False) -> None:
    for label, field in (
        ("Configuration", "config"),
        ("Job", "job_id"),
        ("Config validation", "configuration"),
        ("Monitor service", "monitor"),
        ("Application", "application"),
        ("Dispatch", "dispatch"),
        ("Last action", "last_result"),
    ):
        print(f"{label}: {safe_text(result.get(field) or 'UNKNOWN')}")
    for diagnostic in result["diagnostics"]:
        print(f"Diagnostic: {safe_text(diagnostic)}")
    if verbose:
        for key, value in result["details"].items():
            print(f"{key}: {safe_text(value)}")


def print_inventory(rows: list[dict]) -> None:
    print("Watch\tConfiguration\tMonitor\tApplication\tAction")
    for row in rows:
        fields = [
            row.get("job_id") or row["config"],
            row.get("configuration", "UNKNOWN"),
            row.get("monitor", "UNKNOWN"),
            row.get("application", "UNKNOWN"),
            row.get("dispatch", "UNKNOWN"),
        ]
        print("\t".join(safe_text(value) for value in fields))
        for diagnostic in row["diagnostics"]:
            print("  " + safe_text(diagnostic))
    if not rows:
        print("No watches found.")
