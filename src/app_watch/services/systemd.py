"""Interaction with the systemd user manager.

Only the monitor submits worker starts, via ``systemctl --user start --no-block`` with
an argv array and no shell. Submission success is not proof that a worker started.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Protocol

from app_watch.trigger.paths import validate_scope
from app_watch.trigger.state import checked_id

_SYSTEMCTL = "systemctl"
_DEFAULT_TIMEOUT = 15.0
_LOG = logging.getLogger("app_watch.services.systemd")


def worker_unit(scope: str, attempt_id: str) -> str:
    return f"app-watch-exec@{validate_scope(scope)}_{checked_id(attempt_id, attempt=True)}.service"


class SubmissionClass(StrEnum):
    """Classification of a worker-start submission attempt."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INTEGRATION_ERROR = "integration_error"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    classification: SubmissionClass
    exit_code: int | None
    stderr: str
    spawned: bool


@dataclass(frozen=True, slots=True)
class UnitState:
    load_state: str | None
    active_state: str | None
    sub_state: str | None
    job_pending: bool = False

    def is_active_or_pending(self) -> bool:
        """Return whether the unit is running or has a start job in flight."""
        if self.job_pending or self.active_state not in ("inactive", "failed"):
            return True
        return self.sub_state in ("start", "start-pre", "start-post", "run", "stop")


class SystemdController(Protocol):
    def start_unit(self, unit: str) -> SubmissionResult:
        """Submit a non-blocking start for ``unit``."""

    def unit_state(self, unit: str) -> UnitState | None:
        """Return the unit's state, or ``None`` when the manager is unavailable."""

    def manager_reachable(self) -> bool:
        """Return whether the user manager is reachable."""

    def template_ready(self) -> bool:
        """Return whether the installed worker template is loadable and unmasked."""


def classify_submission(*, spawned: bool, exit_code: int | None) -> SubmissionClass:
    """Only local spawn failure proves non-submission; other failures are uncertain."""
    if not spawned:
        # Failure to spawn the local systemctl process proves the start was not accepted.
        return SubmissionClass.REJECTED
    if exit_code == 0:
        return SubmissionClass.ACCEPTED
    # Generic failure, timeout, or lost bus response: execution may still have queued.
    return SubmissionClass.INCONCLUSIVE


def _run(argv: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _probe(argv: list[str], *, deadline: float | None = None) -> subprocess.CompletedProcess | None:
    try:
        remaining = _DEFAULT_TIMEOUT if deadline is None else deadline - monotonic()
        if remaining <= 0:
            return None
        return _run(argv, timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _LOG.warning("systemd probe timed out after %ss: %s", error.timeout, shlex.join(argv))
        return None
    except OSError:
        return None


def _confirm_unit_absence(unit: str, deadline: float) -> UnitState | None:
    """Require successful unit and job listings; a failed lookup/code alone proves nothing."""
    units = _probe(
        [
            _SYSTEMCTL,
            "--user",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
            "--full",
            unit,
        ],
        deadline=deadline,
    )
    if units is None or units.returncode != 0 or units.stdout.strip():
        return None
    jobs = _probe(
        [_SYSTEMCTL, "--user", "list-jobs", "--no-legend", "--no-pager", "--full", unit],
        deadline=deadline,
    )
    if jobs is None or jobs.returncode != 0:
        return None
    rows = [line.split() for line in jobs.stdout.splitlines() if line.strip()]
    if any(len(row) != 4 or not row[0].isdecimal() or row[1] != unit for row in rows):
        return None
    return UnitState("not-found", "inactive", "dead", job_pending=bool(rows))


class RealSystemdController:
    """Systemd controller using the local ``systemctl --user``."""

    @staticmethod
    def start_unit(unit: str) -> SubmissionResult:
        argv = [_SYSTEMCTL, "--user", "start", "--no-block", unit]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
        except OSError:
            return SubmissionResult(
                classification=SubmissionClass.REJECTED,
                exit_code=None,
                stderr="failed to spawn systemctl",
                spawned=False,
            )
        try:
            _, stderr = process.communicate(timeout=_DEFAULT_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as error:
            # Failures after Popen succeeds cannot prove that a start was not queued.
            with suppress(OSError):
                process.kill()
            with suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            diagnostic = (
                "systemctl start timed out after spawn"
                if isinstance(error, subprocess.TimeoutExpired)
                else "systemctl communication failed after spawn"
            )
            return SubmissionResult(SubmissionClass.INCONCLUSIVE, None, diagnostic, True)
        classification = classify_submission(spawned=True, exit_code=process.returncode)
        return SubmissionResult(
            classification=classification,
            exit_code=process.returncode,
            stderr=(stderr or "").strip()[:2048],
            spawned=True,
        )

    @staticmethod
    def unit_state(unit: str) -> UnitState | None:
        deadline = monotonic() + _DEFAULT_TIMEOUT
        argv = [
            _SYSTEMCTL,
            "--user",
            "show",
            "--all",
            "--property=LoadState,ActiveState,SubState,Job",
            unit,
        ]
        completed = _probe(argv, deadline=deadline)
        if completed is None:
            return None
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
        if completed.returncode != 0 and values.get("LoadState") != "not-found":
            return _confirm_unit_absence(unit, deadline)
        # Missing/malformed job evidence cannot establish absence.
        job = values.get("Job")
        if (
            job is None
            or not values.get("LoadState")
            or (job != "" and not job.isdecimal())
            or not values.get("ActiveState")
            or not values.get("SubState")
        ):
            return None
        return UnitState(
            load_state=values.get("LoadState"),
            active_state=values.get("ActiveState"),
            sub_state=values.get("SubState"),
            job_pending=job not in ("", "0"),
        )

    @staticmethod
    def manager_reachable() -> bool:
        completed = _probe([_SYSTEMCTL, "--user", "show-environment"])
        return completed is not None and completed.returncode == 0

    @staticmethod
    def template_ready() -> bool:
        template = "app-watch-exec@.service"
        # A bare template has no runtime unit object. Inspect its installed files
        # without inventing/starting a probe instance.
        completed = _probe(
            [
                _SYSTEMCTL,
                "--user",
                "list-unit-files",
                "--no-legend",
                "--no-pager",
                "--full",
                template,
            ]
        )
        if completed is None or completed.returncode != 0:
            return False
        states = [
            line.split()[1]
            for line in completed.stdout.splitlines()
            if len(line.split()) >= 2 and line.split()[0] == template
        ]
        if len(states) != 1 or states[0] in ("masked", "masked-runtime", "bad"):
            return False
        content = _probe([_SYSTEMCTL, "--user", "cat", "--no-pager", template])
        if content is None or content.returncode != 0:
            return False
        section = ""
        settings: dict[str, str] = {}
        commands: list[str] = []
        for line in content.stdout.replace("\\\n", " ").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
            elif section == "[Service]" and "=" in line:
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                settings[key] = value
                if key == "ExecStart":
                    if not value:
                        commands.clear()
                    else:
                        commands.append(value)
        if (
            settings.get("Type") != "oneshot"
            or len(commands) != 1
            or settings.get("Restart", "no") != "no"
            or settings.get("TimeoutStartSec") != "infinity"
            or settings.get("UMask") not in ("0077", "77")
        ):
            return False
        try:
            argv = shlex.split(commands[0])
        except ValueError:
            return False
        if len(argv) != 3 or argv[1:] != ["exec-worker", "%i"]:
            return False
        executable = Path(argv[0])
        return (
            executable.is_absolute()
            and executable.is_file()
            and not any(token in argv[0] for token in ("$", "%"))
            and os.access(executable, os.X_OK)
        )
