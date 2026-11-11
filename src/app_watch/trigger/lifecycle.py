"""Observation -> arming -> confirmation -> pending-request lifecycle.

These transitions are pure with respect to time: the caller supplies UTC and monotonic
timestamps so tests can drive them with fake clocks.
"""

from __future__ import annotations

from dataclasses import dataclass

from app_watch.config import PreparedWatch
from app_watch.process import DetectionResult, Observation
from app_watch.trigger.models import InstanceState, ObservationRecord, Request, utc_now


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Frozen execution values captured when a request is confirmed."""

    environment: dict[str, str]


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    kind: str
    detail: str | None = None


def _build_request(
    instance: PreparedWatch,
    detection: DetectionResult,
    context: ExecutionContext,
    now: str,
) -> Request:
    return Request.capture(instance, environment=context.environment, detection=detection, now=now)


def progress(
    state: InstanceState,
    instance: PreparedWatch,
    detection: DetectionResult,
    context: ExecutionContext,
    *,
    monotonic_now: float,
    utc_now_value: str | None = None,
) -> list[LifecycleEvent]:
    """Advance one instance's lifecycle for a single observation and mutate ``state``.

    ``monotonic_now`` is used only for the in-process confirmation delay. Persisted
    timestamps are always UTC.
    """
    events: list[LifecycleEvent] = []
    now_utc = utc_now_value or utc_now()

    fingerprint = instance.fingerprint()
    boot_changed = state.boot_id is not None and detection.boot_id != state.boot_id
    config_changed = (
        state.config_fingerprint is not None and state.config_fingerprint != fingerprint
    )

    if boot_changed:
        state.armed = False
        state.candidate_since = None
        state.candidate_monotonic = None
        state.pending_reconfirmation_required = False
        state.pending_request = None
        events.append(LifecycleEvent("boot_changed"))
        state.boot_id = detection.boot_id
    elif state.boot_id is None:
        state.boot_id = detection.boot_id

    if config_changed:
        state.armed = False
        state.candidate_since = None
        state.candidate_monotonic = None
        state.pending_reconfirmation_required = False
        if state.pending_request is not None:
            state.pending_request = None
            events.append(LifecycleEvent("pending_invalidated", "configuration changed"))
        state.config_fingerprint = fingerprint
    elif state.config_fingerprint is None:
        state.config_fingerprint = fingerprint

    state.last_observation = ObservationRecord(
        observation=detection.observation,
        observed_at=now_utc,
        boot_id=detection.boot_id,
        pid=detection.pid,
        create_time=detection.create_time,
        evidence=dict(detection.evidence),
        reason=detection.reason,
    )

    observation = detection.observation
    if observation is Observation.RUNNING:
        if state.active_request_id is not None or state.pending_request is not None:
            state.latest_request_id = ""  # invalidate retries of the previous shutdown
        if not state.armed:
            state.armed = True
            events.append(LifecycleEvent("armed"))
        state.candidate_since = None
        state.candidate_monotonic = None
        state.pending_reconfirmation_required = False
        if state.pending_request is not None:
            state.pending_request = None
            events.append(LifecycleEvent("pending_cleared", "instance restarted"))
        return events

    if observation in (Observation.AMBIGUOUS, Observation.INSPECTION_UNAVAILABLE):
        if state.pending_request is not None:
            state.pending_reconfirmation_required = True
        if state.candidate_since is not None:
            state.candidate_since = None
            state.candidate_monotonic = None
            events.append(LifecycleEvent("candidate_cleared", observation.value))
        return events

    # STOPPED
    if state.pending_reconfirmation_required and state.pending_request is not None:
        if state.candidate_monotonic is None:
            state.candidate_since = now_utc
            state.candidate_monotonic = monotonic_now
            return events
        if monotonic_now - state.candidate_monotonic < instance.config.trigger.confirm_seconds:
            return events
        state.pending_reconfirmation_required = False
        state.candidate_since = None
        state.candidate_monotonic = None
        state.armed = False
        events.append(LifecycleEvent("pending_reconfirmed"))
        return events
    if boot_changed or not state.armed:
        return events

    if state.candidate_monotonic is None:
        state.candidate_since = now_utc
        state.candidate_monotonic = monotonic_now
        events.append(LifecycleEvent("candidate_started"))
        return events

    if monotonic_now - state.candidate_monotonic >= instance.config.trigger.confirm_seconds:
        if state.pending_request is not None:
            state.superseded_drop_count += 1
            events.append(LifecycleEvent("pending_superseded"))
        request = _build_request(instance, detection, context, now_utc)
        state.pending_request = request
        state.latest_request_id = request.request_id
        state.pending_created_at = now_utc
        state.armed = False
        state.candidate_since = None
        state.candidate_monotonic = None
        events.append(LifecycleEvent("request_confirmed", request.request_id))
    return events
