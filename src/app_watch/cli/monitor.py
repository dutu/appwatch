"""Foreground monitor and internal independent-worker command dispatch."""

from __future__ import annotations

import logging
import os
import re
import signal
from contextlib import ExitStack
from threading import Event

from app_watch.actions.worker import exec_worker
from app_watch.config import PreparedWatch
from app_watch.errors import BlockedError
from app_watch.installation import MaintenanceBusy, software_guard
from app_watch.notifications.events import notify_events
from app_watch.notifications.notifier import NotificationConfig, build_notifier
from app_watch.process import PsutilProcessProvider
from app_watch.services.async_controller import AsyncSystemdController
from app_watch.services.monitor import require_monitor_identity
from app_watch.services.systemd import RealSystemdController
from app_watch.trigger.locking import exclusive_lock, monitor_lock
from app_watch.trigger.models import Receipt
from app_watch.trigger.monitor import Monitor
from app_watch.trigger.paths import monitor_lock_path, runtime_dir, state_root
from app_watch.trigger.state import StateStore


def _log_action_result(logger: logging.Logger, receipt: Receipt) -> None:
    level = {"failed": logging.ERROR, "blocked": logging.WARNING}.get(receipt.outcome, logging.INFO)
    logger.log(
        level,
        "action_%s: attempt_id=%s exit_code=%s signal_number=%s reason=%r",
        receipt.outcome,
        receipt.attempt_id,
        receipt.exit_code,
        receipt.signal_number,
        receipt.reason,
    )


def run_monitor(watch: PreparedWatch, *, expected_identity: str | None = None) -> int:
    if os.geteuid() == 0:
        raise BlockedError("Run monitoring as the non-root application owner, without sudo")
    if not watch.config.trigger.enabled:
        raise BlockedError("Set trigger.enabled to true before starting the monitor")
    if expected_identity is not None and expected_identity != watch.scope:
        raise BlockedError("Installed monitor identity changed; use update")
    runtime_dir()
    controller = RealSystemdController()
    if not controller.manager_reachable():
        raise BlockedError("systemd user manager is unavailable")
    if not controller.template_ready():
        raise BlockedError("App-watch worker service template is unavailable or incompatible")
    stop = Event()
    previous = {}

    def stop_monitor(_signum, _frame):
        stop.set()

    logger = logging.getLogger("app_watch.monitor")
    with ExitStack() as resources:
        while True:
            try:
                with software_guard():
                    resources.enter_context(monitor_lock(monitor_lock_path(watch.source_path)))
                break
            except MaintenanceBusy:
                stop.wait(0.1)
        asynchronous = resources.enter_context(AsyncSystemdController(controller))
        # Validate the installed pin under the lifetime lock: a completed update
        # cannot change selection between this check and monitor startup.
        require_monitor_identity(watch, expected_identity)
        monitor = Monitor(
            watch,
            StateStore(state_root(watch.scope)),
            PsutilProcessProvider(owner_uid=watch.owner_uid),
            asynchronous,
        )
        if monitor.state.blocked_reason:
            logger.error("dispatch_blocked: %s", monitor.state.blocked_reason)
        desktop = watch.config.desktop_notifications
        notifier = build_notifier(
            NotificationConfig(
                enabled=desktop.trigger.enabled,
                command=desktop.command,
                timeout_ms=desktop.timeout_ms,
            )
        )
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, stop_monitor)
            while not stop.is_set():
                try:
                    with (
                        software_guard(),
                        exclusive_lock(
                            monitor_lock_path(watch.source_path).with_name("service.lock")
                        ),
                    ):
                        events = monitor.tick()
                except BlockedError:
                    events = []  # A lifecycle command owns dispatch until its final safety check.
                for event in events:
                    receipt = monitor.state.last_receipt
                    if (
                        event.kind in ("action_succeeded", "action_failed", "action_blocked")
                        and receipt is not None
                        and receipt.attempt_id == event.detail
                    ):
                        _log_action_result(logger, receipt)
                        continue
                    logger.log(
                        {
                            "dispatch_blocked": logging.ERROR,
                            "action_failed": logging.ERROR,
                            "action_blocked": logging.WARNING,
                        }.get(event.kind, logging.INFO),
                        "%s: %s",
                        event.kind,
                        event.detail or watch.config.job.id,
                    )
                notify_events(notifier, watch, events)
                stop.wait(watch.config.trigger.poll_seconds)
        finally:
            notifier.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return 0


def run_worker(selection: str) -> int:
    if os.geteuid() == 0:
        raise BlockedError("Run workers as the non-root application owner, without sudo")
    match = re.fullmatch(r"([0-9a-f]{16})_([0-9a-f-]{36})", selection)
    if match is None:
        raise BlockedError("Invalid internal worker identity")
    scope, attempt_id = match.groups()
    store = StateStore(state_root(scope))
    request = store.read_request(attempt_id)
    if request is not None:
        logging.basicConfig(level=request.log_level.upper())
    logger = logging.getLogger("app_watch.worker")
    logger.info("worker_started: attempt_id=%s", attempt_id)
    try:
        receipt = exec_worker(store, attempt_id, scope=scope)
    except Exception:
        logger.exception("worker_failed: attempt_id=%s", attempt_id)
        raise
    _log_action_result(logger, receipt)
    # A durable receipt completes the worker's job, regardless of action outcome.
    return 0
