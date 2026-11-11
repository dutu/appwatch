"""Responsive watch orchestration; commands run in independently supervised workers."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app_watch.actions.environment import build_child_environment
from app_watch.config import PreparedWatch
from app_watch.errors import StateError
from app_watch.process import DetectionResult, Observation, ProcessProvider, detect_spec
from app_watch.services.systemd import SubmissionClass, SystemdController, worker_unit
from app_watch.trigger.lifecycle import ExecutionContext, LifecycleEvent, progress
from app_watch.trigger.models import InstanceState, LaunchRecord, Request, utc_now
from app_watch.trigger.state import StateStore


class Monitor:
    def __init__(
        self,
        watch: PreparedWatch,
        store: StateStore,
        provider: ProcessProvider,
        systemd: SystemdController,
        *,
        environment: dict[str, str] | None = None,
    ):
        self.watch, self.store, self.provider, self.systemd = watch, store, provider, systemd
        self.context = ExecutionContext(
            environment=environment
            if environment is not None
            else build_child_environment(home=watch.home)
        )
        self.next_dispatch = 0.0
        store.ensure()
        try:
            self.state = (
                store.read_instance(watch.config.job.id)
                or InstanceState(
                    instance_id=watch.config.job.id,
                )
            ).recovered()
            self.state.blocked_reason = store.blocked_reason(self.state.instance_id)
            if self.state.retry_not_before is not None:
                remaining = (
                    datetime.fromisoformat(self.state.retry_not_before) - datetime.now(UTC)
                ).total_seconds()
                self.next_dispatch = time.monotonic() + max(0.0, remaining)
            self._recover_association()
        except StateError as error:
            store.block(watch.config.job.id, str(error))
            raise

    def _request(self, attempt_id: str) -> Request:
        request = self.store.read_request(attempt_id)
        if (
            request is None
            or request.scope != self.watch.scope
            or request.instance_id != self.state.instance_id
        ):
            raise StateError("Missing or contradictory attempt ownership")
        return request

    def _recover_association(self) -> None:
        for attempt_id in self.store.list_attempt_ids():
            if attempt_id in self.state.completed_attempts:
                continue
            request = self._request(attempt_id)
            if self.state.active_attempt_id not in (None, attempt_id):
                raise StateError("Multiple unreconciled attempts for this watch")
            if self.state.active_request_id not in (None, request.request_id):
                raise StateError("Contradictory active request association")
            self.state.active_attempt_id = attempt_id
            self.state.active_request_id = request.request_id
        if self.state.active_attempt_id is not None:
            self._request(self.state.active_attempt_id)

    def _finish(self, attempt_id: str) -> None:
        if attempt_id not in self.state.completed_attempts:
            self.state.completed_attempts.append(attempt_id)
        self.state.active_attempt_id = None
        self.state.active_request_id = None

    def _restore_unsubmitted(self, request: Request, now: float) -> None:
        if (
            self.state.pending_request is None
            and self.state.latest_request_id == request.request_id
            and request.config_fingerprint == self.watch.fingerprint()
        ):
            self.state.pending_request = request
            self.state.pending_reconfirmation_required = True
            self.state.candidate_monotonic = None
        self.next_dispatch = now + 30.0
        self.state.retry_request_id = request.request_id
        self.state.retry_not_before = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()

    def _reconcile(self, now: float, events: list[LifecycleEvent]) -> None:
        attempt_id = self.state.active_attempt_id
        if attempt_id is None:
            return
        request = self._request(attempt_id)
        launch = self.store.read_launch(attempt_id)
        receipt = self.store.read_receipt(attempt_id)
        if launch is None:
            if (
                receipt is not None
                or self.store.read_bytes(self.store.attempt_dir(attempt_id) / "execution.json")
                is not None
            ):
                raise StateError("Execution evidence without a launch intent")
            # Dispatch persists intent before calling systemctl; this gap is provably safe.
            self._restore_unsubmitted(request, now)
            self._finish(attempt_id)
            events.append(LifecycleEvent("submission_not_attempted", attempt_id))
            return
        unit = worker_unit(self.watch.scope, attempt_id)
        if (
            launch.unit != unit
            or launch.request_id != request.request_id
            or launch.instance_id != request.instance_id
        ):
            raise StateError("Contradictory launch association")
        poll_start = getattr(self.systemd, "poll_start", None)
        if poll_start is not None:
            submitted = poll_start(unit)
            if submitted is not None:
                launch.submission = {
                    SubmissionClass.ACCEPTED: "accepted",
                    SubmissionClass.REJECTED: "rejected",
                }.get(submitted.classification, "uncertain")
                launch.diagnostic = submitted.stderr
                self.store.write_launch(launch)
        unit_state = self.systemd.unit_state(unit)
        if unit_state is None or unit_state.is_active_or_pending():
            self._overdue(attempt_id, request, events)
            return
        if receipt is not None:
            if (
                receipt.request_id != request.request_id
                or receipt.instance_id != request.instance_id
            ):
                raise StateError("Contradictory receipt association")
            self.state.last_receipt = receipt
            self._finish(attempt_id)
            events.append(LifecycleEvent("action_" + receipt.outcome, attempt_id))
        elif launch.submission == "rejected":
            if (
                self.store.read_bytes(self.store.attempt_dir(attempt_id) / "execution.json")
                is not None
            ):
                raise StateError("Rejected submission has contradictory execution evidence")
            self._restore_unsubmitted(request, now)
            self._finish(attempt_id)
        else:
            raise StateError(
                "Worker ended or is absent without a receipt; execution outcome unknown"
            )

    def _overdue(self, attempt_id: str, request: Request, events: list[LifecycleEvent]) -> None:
        if request.deadline_seconds is None or self.state.overdue_attempt_id == attempt_id:
            return
        launch = self.store.read_launch(attempt_id)
        if launch is None:
            return
        try:
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(launch.intent_at)).total_seconds()
        except ValueError as error:
            raise StateError("Malformed dispatch timestamp") from error
        if elapsed >= request.deadline_seconds:
            self.state.overdue_attempt_id = attempt_id
            events.append(LifecycleEvent("action_overdue", attempt_id))

    def _dispatch(self, now: float, events: list[LifecycleEvent]) -> None:
        request = self.state.pending_request
        if (
            request is None
            or self.state.active_attempt_id is not None
            or self.state.pending_reconfirmation_required
            or (request.request_id == self.state.retry_request_id and now < self.next_dispatch)
            or request.config_fingerprint != self.watch.fingerprint()
        ):
            return
        attempt_id = str(uuid4())
        self.store.write_request(attempt_id, request)
        self.state.active_attempt_id = attempt_id
        self.state.active_request_id = request.request_id
        self.state.pending_request = None
        self.store.write_instance(self.state)
        launch = LaunchRecord(
            attempt_id=attempt_id,
            request_id=request.request_id,
            instance_id=request.instance_id,
            unit=worker_unit(self.watch.scope, attempt_id),
            intent_at=utc_now(),
        )
        self.store.write_launch(launch)
        result = self.systemd.start_unit(launch.unit)
        launch.submission = {
            SubmissionClass.ACCEPTED: "accepted",
            SubmissionClass.REJECTED: "rejected",
        }.get(result.classification, "uncertain")
        launch.diagnostic = result.stderr
        self.store.write_launch(launch)
        events.append(LifecycleEvent("action_submitted", attempt_id))

    def _cleanup(self) -> None:
        for attempt_id in list(self.state.completed_attempts[:-50]):
            if attempt_id == self.state.active_attempt_id:
                continue
            request = self.store.read_request(attempt_id)
            if (
                request is not None
                and self.state.pending_request is not None
                and request.request_id == self.state.pending_request.request_id
            ):
                continue
            unit = self.systemd.unit_state(worker_unit(self.watch.scope, attempt_id))
            if unit is None or unit.is_active_or_pending():
                continue
            if self.store.remove_attempt(attempt_id):
                self.state.completed_attempts.remove(attempt_id)
        if len(self.state.completed_attempts) > 70:
            raise StateError("History bound held by protected attempts; review retained evidence")

    def tick(self, *, monotonic_now: float | None = None) -> list[LifecycleEvent]:
        now = time.monotonic() if monotonic_now is None else monotonic_now
        events: list[LifecycleEvent] = []
        if self.state.blocked_reason:
            return events
        try:
            self._reconcile(now, events)
            detection = detect_spec(
                app=self.watch.config.application.name,
                data_dir=self.watch.config.application.data_dir,
                matcher=self.watch.matcher,
                provider=self.provider,
            )
            if not detection.boot_id:
                detection = DetectionResult(
                    Observation.INSPECTION_UNAVAILABLE,
                    reason="Kernel boot ID is unavailable",
                )
            if self.watch.config.trigger.enabled:
                events.extend(
                    progress(
                        self.state,
                        self.watch,
                        detection,
                        self.context,
                        monotonic_now=now,
                    )
                )
                self.store.write_instance(self.state)
                if detection.observation is Observation.STOPPED:
                    self._dispatch(now, events)
            else:
                self.state.pending_request = None
                self.state.armed = False
                self.state.candidate_monotonic = None
            # Persist completion before cleaning files, so restart never replays history.
            self.store.write_instance(self.state)
            self._cleanup()
            self.store.write_instance(self.state)
        except (StateError, OSError) as error:
            self.store.block(self.state.instance_id, str(error))
            self.state.blocked_reason = str(error)
            events.append(LifecycleEvent("dispatch_blocked", str(error)))
        return events
