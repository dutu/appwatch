"""Opt-in VM check of notify-send through a real same-user notification bus."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

from app_watch.notifications.notifier import NotificationConfig, build_notifier


def main():
    assert os.geteuid() != 0
    daemon = subprocess.Popen(
        ["xvfb-run", "-a", "dunst"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["dunstctl", "count", "displayed"], capture_output=True, text=True, check=False
            )
            if result.returncode == 0:
                break
            assert daemon.poll() is None, "Notification daemon exited"
            time.sleep(0.1)
        else:
            raise AssertionError("Notification daemon unavailable")
        notifier = build_notifier(NotificationConfig(enabled=True))
        notifier.notify("App-watch", "Disposable desktop delivery check", key="live", finished=True)
        notifier.close()
        subprocess.run(["dunstctl", "close-all"], check=True)
        history = json.loads(subprocess.check_output(["dunstctl", "history"], text=True))
        assert "Disposable desktop delivery check" in json.dumps(history), history
        print("Real notify-send, same-user bus discovery and desktop delivery passed")
    finally:
        os.killpg(daemon.pid, signal.SIGTERM)
        daemon.wait(timeout=10)


if __name__ == "__main__":
    main()
