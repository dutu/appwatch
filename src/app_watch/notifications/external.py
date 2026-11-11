"""Best-effort delivery through one configured external executable."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Sequence
from contextlib import suppress

from app_watch.config.models import ExternalNotifier as ExternalNotifierConfig

_LOG = logging.getLogger(__name__)
_TERMINATION_GRACE = 1.0


def notify(config: ExternalNotifierConfig, arguments: Sequence[str]) -> None:
    """Send once; expected delivery failures never change the command result."""
    try:
        process = subprocess.Popen(
            [config.command, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            process.wait(timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate(process)
            _warn(f"External notifier timed out after {config.timeout_seconds:g} seconds")
            return
        except BaseException:
            _terminate(process)
            raise
        if process.returncode:
            _warn(f"External notifier exited with status {process.returncode}")
    except OSError as error:
        # Neither provider output nor arguments are safe diagnostics: either may
        # contain credentials. strerror omits executable/argument values as well.
        _warn(
            f"Cannot execute external notifier: {concise(error.strerror or type(error).__name__)}"
        )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Terminate the session, including descendants whose parent exits on TERM."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_TERMINATION_GRACE)
    # The leader may have exited while its children ignored TERM.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_TERMINATION_GRACE)


def _warn(reason: str) -> None:
    _LOG.warning("External notification delivery failed: %s", concise(reason))


def concise(value: str) -> str:
    return " ".join("".join(char if char.isprintable() else " " for char in value).split())[:400]
