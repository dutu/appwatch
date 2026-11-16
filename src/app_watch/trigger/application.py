"""Application state labels shared by status reporting and desktop notifications."""

from datetime import datetime

from app_watch.config import PreparedWatch
from app_watch.errors import StateError
from app_watch.process import Observation
from app_watch.trigger.models import InstanceState
from app_watch.trigger.state import StateStore


def application_status(
    state: InstanceState, store: StateStore, watch: PreparedWatch, started: datetime
) -> str:
    """A stopped observation is distinct from a confirmed shutdown in this session."""
    observation = state.last_observation
    if observation is None:
        return "UNKNOWN"
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
