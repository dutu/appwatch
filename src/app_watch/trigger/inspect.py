"""Read-only recovery inspection shared by status and lifecycle safety checks."""

from __future__ import annotations

from app_watch.config.identity import WatchIdentity
from app_watch.errors import StateError
from app_watch.trigger.paths import state_root
from app_watch.trigger.state import StateStore, checked_id


def inspect_recovery(identity: WatchIdentity) -> dict:
    store = StateStore(state_root(identity.scope))
    state = store.read_instance(identity.job_id)
    blocked = store.blocked_reason(identity.job_id) or (state.blocked_reason if state else None)
    active = state.active_attempt_id if state else None
    # Unexpected records cannot be silently omitted from the selected namespace.
    for directory in ("instances", "blocked"):
        store._check(store.root / directory / "unused")
        for path in (store.root / directory).glob("*.json"):
            if checked_id(path.stem) != identity.job_id:
                raise StateError("Foreign instance record in execution namespace")
    if (
        state is not None
        and state.pending_request is not None
        and state.pending_request.scope != identity.scope
    ):
        raise StateError("Pending request belongs to another execution namespace")
    for attempt_id in store.list_attempt_ids():
        if state is not None and attempt_id in state.completed_attempts:
            continue
        request = store.read_request(attempt_id)
        if (
            request is None
            or request.scope != identity.scope
            or request.instance_id != identity.job_id
        ):
            raise StateError("Missing or contradictory retained attempt ownership")
        if active not in (None, attempt_id):
            raise StateError("Multiple unreconciled attempts")
        if state and state.active_request_id not in (None, request.request_id):
            raise StateError("Contradictory active request identity")
        active = attempt_id
    if active is not None and store.read_request(active) is None:
        raise StateError("Active attempt has no durable request")
    return {
        "blocked": blocked,
        "active_attempt_id": active,
        "pending": state is not None and state.pending_request is not None,
        "state": state,
        "state_directory": str(store.root),
    }
