"""Literal argv execution with explicit environment, cwd and bounded diagnostics."""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app_watch.trigger.models import utc_now
from app_watch.trigger.state import DIAGNOSTICS_CAP


@dataclass(frozen=True, slots=True)
class ActionResult:
    started_at: str
    ended_at: str
    duration_seconds: float
    exit_code: int | None
    signal_number: int | None
    diagnostics: bytes
    diagnostics_truncated: bool
    spawn_error: str | None = None


def run_action(
    argv: list[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    cap: int = DIAGNOSTICS_CAP,
) -> ActionResult:
    """Wait for the command; an advisory deadline never terminates the child.

    Drain both streams even after the storage limit. A background descendant cannot
    keep the worker stuck solely by inheriting pipes after the command exits.
    """
    if cap <= 0:
        raise ValueError("diagnostics cap must be positive")
    start = time.monotonic()
    started_at = utc_now()
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=cwd,
            shell=False,
        )
    except OSError as error:
        return ActionResult(
            started_at,
            utc_now(),
            time.monotonic() - start,
            None,
            None,
            b"",
            False,
            str(error),
        )
    output = bytearray()
    truncated = False
    child_exited_at = None
    with selectors.DefaultSelector() as selector:
        for stream in (child.stdout, child.stderr):
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        try:
            while selector.get_map():
                ready = selector.select(timeout=0.1)
                if child.poll() is not None:
                    if child_exited_at is None:
                        child_exited_at = time.monotonic()
                    elif time.monotonic() - child_exited_at >= 0.2:
                        truncated = True
                        break
                if not ready and child.poll() is not None:
                    # Descendants still owning the pipes are not the configured command.
                    truncated = True
                    break
                for key, _ in ready:
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    output.extend(data)
                    if len(output) > cap:
                        del output[: len(output) - cap]
                        truncated = True
            code = child.wait()
        finally:
            for key in list(selector.get_map().values()):
                key.fileobj.close()
    return ActionResult(
        started_at,
        utc_now(),
        time.monotonic() - start,
        code if code >= 0 else None,
        -code if code < 0 else None,
        bytes(output),
        truncated,
    )
