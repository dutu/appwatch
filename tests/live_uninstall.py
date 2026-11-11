"""Opt-in VM check of real uninstall, custom paths and user-data preservation."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from uuid import uuid4


def run(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"{argv}\n{result.stdout}\n{result.stderr}"
    return result


def user_records():
    files = list((Path.home() / ".local/state/app-watch").rglob("*"))
    files += list(Path.home().glob("app-watch-live-*/*config.yaml"))
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files if path.is_file()
    }


def main():
    assert os.geteuid() != 0 and os.uname().nodename == "codex-test"
    before = user_records()
    assert before, "Run the live service check first to leave disposable recovery evidence"
    run("sudo", "./install.sh", "uninstall")
    assert not Path("/usr/local/bin/app-watch").exists()
    assert not Path("/etc/systemd/user/app-watch-exec@.service").exists()
    assert not list(Path("/opt/app-watch/releases").iterdir())
    run("sudo", "test", "-f", "/opt/app-watch/install-manifest.json")
    run("sudo", "./install.sh", "uninstall", "--purge")
    run("sudo", "test", "!", "-e", "/opt/app-watch/install-manifest.json")
    custom_prefix = Path("/opt") / ("app-watch-custom-" + uuid4().hex[:8])
    custom = [
        "sudo",
        "env",
        f"APP_WATCH_INSTALL_PREFIX={custom_prefix}",
        "APP_WATCH_BIN_LINK=/usr/local/bin/app-watch-custom",
        "APP_WATCH_USER_SYSTEMD_ROOT=/etc/systemd/user",
        "./install.sh",
    ]
    run(*custom, "install", "--dry-run")
    assert not custom_prefix.exists()
    run(*custom, "install")
    run("/usr/local/bin/app-watch-custom", "--version")
    worker = Path("/etc/systemd/user/app-watch-exec@.service").read_text()
    assert 'ExecStart="/usr/local/bin/app-watch-custom" ' in worker
    run(*custom, "update")
    run(*custom, "uninstall", "--purge")
    assert not Path("/usr/local/bin/app-watch-custom").exists()
    assert user_records() == before, "Uninstall must preserve user records byte for byte"
    # Leave the normal installation available for further checks in the running VM.
    run("sudo", "./install.sh", "install")
    assert user_records() == before
    print("Real uninstall/purge, custom install/update and user-data preservation passed")


if __name__ == "__main__":
    main()
