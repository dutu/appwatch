"""Opt-in VM checks of queued jobs, terminal units and real manager failures."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from app_watch.services.systemd import (
    RealSystemdController,
    SubmissionClass,
    worker_unit,
)


def main():
    assert os.geteuid() != 0
    controller = RealSystemdController()
    assert controller.manager_reachable()
    unit = worker_unit("0" * 16, str(uuid4()))
    barrier = f"app-watch-live-barrier-{uuid4().hex}.service"
    dropin = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user" / (unit + ".d")
    dropin.mkdir(parents=True)
    (dropin / "queue.conf").write_text(f"[Unit]\nAfter={barrier}\nRequires={barrier}\n")

    def systemctl(*args):
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, check=True)

    try:
        systemctl("daemon-reload")
        subprocess.run(
            [
                "systemd-run",
                "--user",
                "--no-block",
                "--unit",
                barrier,
                "--property=Type=oneshot",
                "/usr/bin/sleep",
                "3",
            ],
            capture_output=True,
            check=True,
        )
        assert controller.start_unit(unit).classification == SubmissionClass.ACCEPTED
        state = controller.unit_state(unit)
        assert state and state.job_pending and state.is_active_or_pending(), state
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = controller.unit_state(unit)
            if state and not state.is_active_or_pending():
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"Worker with no durable request did not finish safely: {state}")
        assert state.active_state == "failed", state
        missing = "app-watch-live-missing-" + uuid4().hex + ".service"
        result = controller.start_unit(missing)
        assert result.classification == SubmissionClass.INCONCLUSIVE, result
        print(
            f"Real missing-unit start exit={result.exit_code}; "
            f"classification={result.classification}"
        )
        absent = controller.unit_state(missing)
        assert absent and not absent.is_active_or_pending(), absent
        # Process-local addresses exercise failure without changing the manager.
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from app_watch.services.systemd import RealSystemdController as C; "
                "assert not C().manager_reachable(); "
                "assert C().unit_state('missing.service') is None",
            ],
            env=dict(
                os.environ,
                DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent-app-watch-bus",
                XDG_RUNTIME_DIR="/nonexistent-app-watch-runtime",
            ),
            check=True,
        )
        print("Real queued-job, terminal/absent-unit and unavailable-manager checks passed")
    finally:
        systemctl("stop", unit, barrier)
        systemctl("reset-failed", unit)
        shutil.rmtree(dropin)
        systemctl("daemon-reload")


if __name__ == "__main__":
    main()
