"""Versioned recovery records and frozen external-command requests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import uuid4

from pydantic import AfterValidator, Field, StringConstraints, field_validator, model_validator

from app_watch.config.models import ActionConfig, ApplicationConfig, Seconds, StrictModel
from app_watch.process import DetectionResult, Observation

if TYPE_CHECKING:
    from app_watch.config import PreparedWatch

type UUID = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
type Scope = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
type InstanceID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


def validate_stamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Timestamp must be an ISO 8601 UTC value") from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Timestamp must have a UTC offset")
    return value


type UTCStamp = Annotated[str, AfterValidator(validate_stamp)]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Versioned(StrictModel):
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class Request(Versioned):
    model_config = {"frozen": True}

    request_id: UUID
    instance_id: InstanceID
    scope: Scope
    confirmed_at: UTCStamp
    boot_id: str
    config_fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    application: ApplicationConfig
    action: ActionConfig
    environment: dict[str, str]
    home: str
    owner_uid: Annotated[int, Field(ge=0)]
    deadline_seconds: Seconds | None = None
    log_level: Literal["error", "warning", "info", "debug"] = "info"
    include_command_output: bool = False

    @classmethod
    def capture(
        cls,
        watch: PreparedWatch,
        *,
        environment: dict[str, str],
        detection: DetectionResult,
        now: str,
    ) -> Request:
        if not detection.boot_id:
            raise ValueError("Cannot create an execution request without a kernel boot ID")
        return cls(
            request_id=str(uuid4()),
            instance_id=watch.config.job.id,
            scope=watch.scope,
            confirmed_at=now,
            boot_id=detection.boot_id,
            config_fingerprint=watch.fingerprint(),
            application=watch.config.application.model_copy(deep=True),
            action=watch.config.action.model_copy(deep=True),
            environment=dict(environment),
            home=str(watch.home),
            owner_uid=watch.owner_uid,
            deadline_seconds=watch.config.trigger.deadline_seconds,
            log_level=watch.config.logging.level,
            include_command_output=watch.config.logging.include_command_output,
        )


class ObservationRecord(StrictModel):
    observation: Observation
    observed_at: UTCStamp
    boot_id: str | None = None
    pid: int | None = None
    create_time: float | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    reason: str | None = None


class Receipt(Versioned):
    model_config = {"frozen": True}

    attempt_id: UUID
    request_id: UUID
    instance_id: InstanceID
    outcome: Literal["succeeded", "failed", "blocked"]
    started_at: UTCStamp
    ended_at: UTCStamp
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    exit_code: Annotated[int, Field(ge=0)] | None = None
    signal_number: Annotated[int, Field(gt=0)] | None = None
    diagnostics_truncated: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Receipt:
        if self.exit_code is not None and self.signal_number is not None:
            raise ValueError("An action cannot both exit and terminate by signal")
        if self.outcome == "succeeded" and self.exit_code != 0:
            raise ValueError("Success requires exit zero")
        if self.outcome != "succeeded" and self.exit_code == 0:
            raise ValueError("Exit zero requires a successful outcome")
        if self.outcome == "blocked" and (
            self.exit_code is not None or self.signal_number is not None or not self.reason
        ):
            raise ValueError("A blocked action must have a reason and no command exit")
        return self


class LaunchRecord(Versioned):
    attempt_id: UUID
    request_id: UUID
    instance_id: InstanceID
    unit: str
    intent_at: UTCStamp
    # Only a failure to spawn the local systemctl proves non-submission.
    submission: Literal["intent", "accepted", "rejected", "uncertain"] = "intent"
    diagnostic: str = ""


class InstanceState(Versioned):
    instance_id: InstanceID
    boot_id: str | None = None
    config_fingerprint: str | None = None
    armed: bool = False
    candidate_since: str | None = None
    candidate_monotonic: float | None = Field(default=None, exclude=True)
    pending_reconfirmation_required: bool = False
    pending_request: Request | None = None
    pending_created_at: str | None = None
    retry_request_id: UUID | None = None
    retry_not_before: UTCStamp | None = None
    latest_request_id: str | None = None
    active_attempt_id: UUID | None = None
    active_request_id: UUID | None = None
    last_observation: ObservationRecord | None = None
    last_receipt: Receipt | None = None
    superseded_drop_count: Annotated[int, Field(ge=0)] = 0
    # Only reconciled attempts may be cleaned; retain bounded durable summaries.
    completed_attempts: list[UUID] = Field(default_factory=list)
    blocked_reason: str | None = None
    overdue_attempt_id: UUID | None = None

    @model_validator(mode="after")
    def consistent_ownership(self) -> InstanceState:
        if (self.active_attempt_id is None) != (self.active_request_id is None):
            raise ValueError("Active attempt and request associations must be recorded together")
        if (
            self.pending_request is not None
            and self.pending_request.instance_id != self.instance_id
        ):
            raise ValueError("Pending request belongs to another instance")
        if len(self.completed_attempts) != len(set(self.completed_attempts)):
            raise ValueError("Duplicate completed attempt references")
        if self.active_attempt_id is not None and self.active_attempt_id in self.completed_attempts:
            raise ValueError("Active attempt cannot be recorded as completed")
        if (self.retry_request_id is None) != (self.retry_not_before is None):
            raise ValueError("Retry request and deadline must be recorded together")
        return self

    def recovered(self) -> InstanceState:
        result = copy.deepcopy(self)
        result.candidate_monotonic = None
        result.candidate_since = None
        result.pending_reconfirmation_required = result.pending_request is not None
        return result
