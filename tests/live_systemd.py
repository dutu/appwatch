"""Opt-in deployment check. Run only in the disposable VM after installation.

Uses real systemd services and harmless helper processes, not installed wallets.
Keeps its temporary files and recovery records available for inspection.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from app_watch.config import load_config, prepare_config
from app_watch.services.monitor import service_name
from app_watch.services.systemd import RealSystemdController
from app_watch.trigger.paths import state_root


def run(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=180)
    if check and result.returncode:
        raise AssertionError(f"{argv}: {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def wait_for(probe, description: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


def main():
    assert os.geteuid() != 0, "Run as the disposable VM's application owner"
    assert RealSystemdController().template_ready()
    root = Path(tempfile.mkdtemp(prefix="app-watch-live-", dir=Path.home()))
    print(f"Deployment evidence: {root}", flush=True)
    data = root / "data"
    data.mkdir()
    wallet = root / "wallet.py"
    wallet.write_text("import time\ntime.sleep(120)\n")
    marker, release = root / "started.json", root / "release"
    action = root / "action.py"
    action.write_text(
        "import json, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[3:]))\n"
        "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.1)\n"
    )
    notifications = root / "notifications.jsonl"
    notifier = root / "notifier"
    notifier.write_text(
        "#!/usr/bin/python3\nimport json, sys\n"
        f"with open({str(notifications)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
    )
    notifier.chmod(0o700)
    config = root / "watch.yaml"
    payload = {
        "config_version": 1,
        "job": {"id": root.name},
        "application": {
            "name": "electrum",
            "data_dir": str(data),
            "matcher": {"entrypoints": [{"kind": "script", "value": str(wallet)}]},
        },
        "action": {
            "argv": [
                "/usr/bin/python3",
                str(action),
                str(marker),
                str(release),
                "argument with spaces",
                "$(touch never-created)",
                ";literal",
                "",
            ]
        },
        "trigger": {"enabled": True, "poll_seconds": 0.2, "confirm_seconds": 0.5},
        "desktop_notifications": {"command": str(notifier), "trigger": {"enabled": True}},
        "external_notifier": {"command": str(notifier)},
    }
    config.write_text(json.dumps(payload))
    base = ["/usr/local/bin/app-watch", "--user", "--config", str(config)]
    unit = service_name(config)
    watch = prepare_config(load_config(config), config)
    attempts = state_root(watch.scope) / "attempts"

    def status():
        return json.loads(run(*base, "status", "--json", "--verbose").stdout)

    def manage(operation):
        def attempt():
            result = run(*base, "trigger", operation, check=False)
            if result.returncode == 0:
                return True
            # A live poll can briefly own the nonblocking lifecycle lock.
            if "operation already active" in result.stderr and "service.lock" in result.stderr:
                return False
            raise AssertionError(result.stderr)

        wait_for(attempt, f"trigger {operation}")

    process = None
    try:
        manage("install")
        wait_for(lambda: status()["application"] == "NOT RUNNING", "initial observation")
        time.sleep(1)
        assert not marker.exists(), "Starting stopped must not execute an action"
        process = subprocess.Popen(["/usr/bin/python3", str(wallet), "-D", str(data)])
        wait_for(lambda: status()["application"] == "RUNNING", "running observation")
        run(*base, "validate")
        process.terminate()
        process.wait(timeout=5)
        wait_for(marker.exists, "independent action start")
        assert json.loads(marker.read_text()) == payload["action"]["argv"][4:]
        active = status()["active_attempt_id"]
        assert active
        worker = f"app-watch-exec@{watch.scope}_{active}.service"
        assert (
            run("systemctl", "--user", "is-active", worker, check=False).stdout.strip()
            == "activating"
        )
        refusal = run("sudo", "./install.sh", "update", check=False)
        assert refusal.returncode != 0, refusal.stdout
        assert "maintenance" in refusal.stderr.lower(), refusal.stderr
        blocked = run(*base, "trigger", "update", check=False)
        assert blocked.returncode == 2
        run("systemctl", "--user", "stop", unit)
        assert (
            run("systemctl", "--user", "is-active", worker, check=False).stdout.strip()
            == "activating"
        )
        run("systemctl", "--user", "start", unit)
        assert status()["active_attempt_id"] == active
        release.touch()
        wait_for(lambda: status()["last_result"] == "succeeded", "success receipt reconciliation")
        wait_for(lambda: status()["dispatch"] == "IDLE", "idle slot")
        assert status()["application"] == "NOT RUNNING"  # Confirmation preceded restart.
        receipt = json.loads((attempts / active / "receipt.json").read_text())
        assert receipt["outcome"] == "succeeded"
        wait_for(
            lambda: (
                notifications.exists() and "External command succeeded" in notifications.read_text()
            ),
            "terminal notification",
        )
        run(*base, "notify", "--level", "info", "literal notification")
        assert json.loads(notifications.read_text().splitlines()[-1]) == [
            "--level",
            "info",
            "literal notification",
        ]
        before = Path("/opt/app-watch/current").resolve()
        run("sudo", "./install.sh", "update")
        assert Path("/opt/app-watch/current").resolve() != before
        wait_for(lambda: status()["monitor"] == "RUNNING", "monitor refresh")
        run(*base, "validate")
        run("sudo", "/usr/bin/python3", "tests/live_rollback.py")
        wait_for(lambda: status()["application"] == "NOT RUNNING", "rollback monitor restart")
        run(*base, "validate")
        # Generic nonzero exits are final failures, including backup-style exit 10.
        payload["action"]["argv"] = ["/usr/bin/python3", "-c", "raise SystemExit(10)"]
        config.write_text(json.dumps(payload))
        manage("update")
        process = subprocess.Popen(["/usr/bin/python3", str(wallet), "-D", str(data)])
        wait_for(lambda: status()["application"] == "RUNNING", "second lifecycle")
        process.terminate()
        process.wait(timeout=5)
        wait_for(lambda: status()["last_result"] == "failed", "failure receipt")
        wait_for(lambda: status()["dispatch"] == "IDLE", "failed slot reconciliation")
        assert status()["application"] == "HAS STOPPED"
        assert len(list(attempts.glob("*/receipt.json"))) == 2
        run("systemctl", "--user", "restart", unit)
        time.sleep(1.5)
        assert len(list(attempts.glob("*/receipt.json"))) == 2, "Failures must not replay"
        # Verify maintenance refresh preserves a stopped/disabled watch's flags.
        manage("disable")
        run("sudo", "./install.sh", "update")
        report = status()
        assert report["monitor"] == "STOPPED" and report["service_enabled"] is False
        config.rename(root / "retained-config.yaml")
        manage("remove")
        assert not (Path.home() / ".config/systemd/user" / unit).exists()
        print("Live systemd, independent workers, receipts, notifications and update checks passed")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        release.touch()
        run("systemctl", "--user", "stop", unit, check=False)


if __name__ == "__main__":
    main()
