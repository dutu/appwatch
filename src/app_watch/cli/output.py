"""VaultKeep-compatible output for per-watch service commands."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def _status(value: str, stream: TextIO, *, no_color: bool) -> str:
    if (
        not no_color
        and stream.isatty()
        and os.environ.get("TERM", "dumb") != "dumb"
        and not os.environ.get("NO_COLOR")
    ):
        color = {"OK": "32", "FAILED": "1;31"}[value]
        return f"\033[{color}m{value}\033[0m"
    return value


def service_result(action: str, *, no_color: bool) -> None:
    print(f"Trigger {action}:  {_status('OK', sys.stdout, no_color=no_color)}", flush=True)


def service_error(message: str, *, no_color: bool) -> None:
    status = _status("FAILED", sys.stderr, no_color=no_color)
    print(f"{status}: {message}", file=sys.stderr, flush=True)
