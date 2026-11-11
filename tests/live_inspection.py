"""Opt-in installed helper and unrestricted Monero lifecycle check, only in the VM."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app_watch.process import PsutilProcessProvider
from app_watch.process_inspection import protected_executables


def run(*argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)


def wait_for(probe, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


def main():
    assert os.geteuid() != 0 and os.uname().nodename == "codex-test"
    helper = Path("/opt/app-watch/process-inspect")
    records = protected_executables(os.geteuid())
    assert records and 1 not in records, "Helper must return only the caller's processes"
    assert records[os.getpid()][1] == os.readlink("/proc/self/exe")
    # Exercise only the helper rule, without this test account's general sudo rights.
    restricted = ["sudo", "/usr/sbin/runuser", "-u", "nobody", "--"]
    caller = run(*restricted, "/usr/bin/sudo", "-n", "--", str(helper))
    assert json.loads(caller.stdout)["uid"] == pwd.getpwnam("nobody").pw_uid
    forged = run(
        *restricted, "/usr/bin/env", "SUDO_UID=0", "/usr/bin/sudo", "-n", "--", str(helper)
    )
    assert json.loads(forged.stdout)["uid"] == pwd.getpwnam("nobody").pw_uid
    override = subprocess.run(
        [*restricted, "/usr/bin/sudo", "-n", "SUDO_UID=0", str(helper)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert override.returncode != 0 and not override.stdout
    refused = subprocess.run(
        [*restricted, "/usr/bin/sudo", "-n", "--", str(helper), "/etc/shadow"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert refused.returncode != 0 and not refused.stdout
    root = Path(tempfile.mkdtemp(prefix="app-watch-inspection-", dir=Path.home()))
    binary = root / "monero-wallet-rpc"
    shutil.copyfile("/usr/bin/python3", binary)
    binary.chmod(0o700)
    wallet_dir = root / "wallets"
    wallet_dir.mkdir()
    ready, marker = root / "ready", root / "action"
    config = root / "watch.yaml"
    config.write_text(
        json.dumps(
            {
                "config_version": 1,
                "job": {"id": root.name},
                "application": {
                    "name": "monero",
                    "data_dir": str(wallet_dir),
                    "matcher": {
                        "entrypoints": [
                            {"kind": "executable_name", "value": "monero-wallet-gui"},
                            {"kind": "executable_name", "value": "monero-wallet-rpc"},
                        ]
                    },
                },
                "action": {"argv": ["/usr/bin/touch", str(marker)]},
                "trigger": {"enabled": True, "poll_seconds": 0.2, "confirm_seconds": 0.5},
            }
        )
    )
    base = ["/usr/local/bin/app-watch", "--config", str(config)]

    def status():
        return json.loads(run(*base, "status", "--json").stdout)

    installed = False
    child = None
    try:
        run(*base, "install")
        installed = True
        wait_for(lambda: status()["application"] == "NOT RUNNING", "unrestricted stopped scan")
        assert not marker.exists()
        child = subprocess.Popen(
            [
                str(binary),
                "-c",
                "import ctypes,pathlib,time; "
                "assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0; "
                f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)",
                "--wallet-file",
                str(wallet_dir / "disposable"),
            ]
        )
        wait_for(ready.exists, "protected wallet helper startup")
        try:
            os.readlink(f"/proc/{child.pid}/exe")
        except PermissionError:
            pass
        else:
            raise AssertionError("Test wallet executable must require privileged inspection")
        assert protected_executables(os.geteuid())[child.pid][1] == str(binary)
        assert next(
            p for p in PsutilProcessProvider().snapshot() if p.pid == child.pid
        ).exe_realpath == str(binary)
        wait_for(lambda: status()["application"] == "RUNNING", "protected wallet activation")
        time.sleep(1)
        assert not marker.exists(), "Protected wallet must still block the action"
        child.terminate()
        child.wait(timeout=5)
        wait_for(marker.exists, "unprivileged worker after protected wallet exit")
        wait_for(lambda: status()["last_result"] == "succeeded", "worker receipt")
        print(f"Caller-only helper, argument refusal and full-scan Monero lifecycle passed: {root}")
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        if installed:
            wait_for(lambda: status()["dispatch"] == "IDLE", "idle cleanup")
            run(*base, "remove")


if __name__ == "__main__":
    main()
