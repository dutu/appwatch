"""Translate durable lifecycle events without including command arguments or output."""

from app_watch.config import PreparedWatch
from app_watch.notifications.notifier import Notifier
from app_watch.trigger.lifecycle import LifecycleEvent

_MESSAGES = {
    "request_confirmed": "Application shutdown confirmed",
    "action_submitted": "External command submitted",
    "action_succeeded": "External command succeeded",
    "action_failed": "External command failed",
    "action_blocked": "External command blocked by final application check",
    "dispatch_blocked": "Recovery review required; automatic dispatch blocked",
    "action_overdue": "External command is overdue; it has not been stopped",
    "pending_superseded": "An older pending shutdown request was superseded",
}


def notify_events(notifier: Notifier, watch: PreparedWatch, events: list[LifecycleEvent]) -> None:
    for event in events:
        if event.kind not in _MESSAGES:
            continue
        terminal = event.kind in (
            "action_succeeded",
            "action_failed",
            "action_blocked",
            "dispatch_blocked",
        )
        notifier.notify(
            "App-watch",
            f"{watch.config.job.id}: {_MESSAGES[event.kind]}",
            key=watch.scope,
            finished=terminal,
            replace_pending=not terminal,
        )
