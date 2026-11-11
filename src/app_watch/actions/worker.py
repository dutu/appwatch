"""Execute a durable request once, rechecking frozen application identity first."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app_watch.actions.runner import run_action
from app_watch.errors import BlockedError, StateError
from app_watch.process import (
    Observation,
    ProcessProvider,
    PsutilProcessProvider,
    boot_id,
    detect_spec,
    matcher_from_canonical,
)
from app_watch.services.systemd import worker_unit
from app_watch.trigger.locking import exclusive_lock
from app_watch.trigger.models import Receipt, utc_now
from app_watch.trigger.paths import account_home
from app_watch.trigger.state import StateStore


def exec_worker(
    store: StateStore,
    attempt_id: str,
    *,
    scope: str,
    provider: ProcessProvider | None = None,
) -> Receipt:
    """Independent worker entry point; never reload a live YAML file.

    execution.json is an irreversible claim. A crash between claiming and writing
    a receipt is uncertainty, not permission to rerun the external command.
    """
    # Refuse a missing attempt before lock creation. Re-read records under the lock
    # so history cleanup cannot leave us holding stale request objects.
    if store.read_request(attempt_id) is None:
        raise StateError("Worker attempt does not exist")
    directory = store.attempt_dir(attempt_id)
    with exclusive_lock(directory / "worker.lock"):
        request = store.read_request(attempt_id)
        launch = store.read_launch(attempt_id)
        if request is None or launch is None:
            raise StateError("Worker requires a durable request and launch intent")
        if (
            request.scope != scope
            or request.owner_uid != os.geteuid()
            or request.home != str(account_home())
            or request.environment.get("HOME") != request.home
            or launch.request_id != request.request_id
            or launch.instance_id != request.instance_id
            or launch.unit != worker_unit(scope, attempt_id)
        ):
            raise StateError("Worker request ownership does not match this execution namespace")
        existing = store.read_receipt(attempt_id)
        if existing is not None:
            if (
                existing.request_id != request.request_id
                or existing.instance_id != request.instance_id
            ):
                raise StateError("Receipt ownership does not match request")
            return existing
        state = store.read_instance(request.instance_id)
        if (
            state is None
            or state.active_attempt_id != attempt_id
            or state.active_request_id != request.request_id
            or attempt_id in state.completed_attempts
        ):
            raise BlockedError("Attempt is not the monitor's active request; refusing execution")
        claim = directory / "execution.json"
        if store.read_bytes(claim) is not None:
            raise BlockedError("Attempt was already claimed; preserve evidence and do not replay")
        store.write(claim, launch, immutable=True)
        reason = None
        if boot_id() != request.boot_id:
            reason = "Request belongs to another or unknown boot"
        else:
            provider = provider or PsutilProcessProvider(owner_uid=request.owner_uid)
            detection = detect_spec(
                app=request.application.name,
                data_dir=request.application.data_dir,
                matcher=matcher_from_canonical(request.application.matcher.model_dump()),
                provider=provider,
            )
            if detection.observation is not Observation.STOPPED:
                reason = "Final application observation is " + detection.observation.value
        if reason is not None:
            stamp = utc_now()
            receipt = Receipt(
                attempt_id=attempt_id,
                request_id=request.request_id,
                instance_id=request.instance_id,
                outcome="blocked",
                started_at=stamp,
                ended_at=stamp,
                duration_seconds=0.0,
                reason=reason,
            )
        else:
            result = run_action(
                list(request.action.argv),
                environment=dict(request.environment),
                cwd=Path(request.home),
            )
            store.write_diagnostics(attempt_id, result.diagnostics)
            if request.include_command_output and result.diagnostics:
                text = result.diagnostics.decode("utf-8", "replace")
                safe = "".join(
                    char if char.isprintable() or char == "\n" else ascii(char)[1:-1]
                    for char in text
                )
                logging.getLogger("app_watch.worker").info(
                    "Action output: attempt_id=%s\n%s", attempt_id, safe
                )
            receipt = Receipt(
                attempt_id=attempt_id,
                request_id=request.request_id,
                instance_id=request.instance_id,
                outcome="succeeded" if result.exit_code == 0 else "failed",
                started_at=result.started_at,
                ended_at=result.ended_at,
                duration_seconds=result.duration_seconds,
                exit_code=result.exit_code,
                signal_number=result.signal_number,
                diagnostics_truncated=result.diagnostics_truncated,
                reason=result.spawn_error,
            )
        store.write_receipt(receipt)
        return receipt
