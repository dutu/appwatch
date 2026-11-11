from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys

import pytest

from app_watch.installation import MaintenanceBusy, software_guard


@pytest.fixture
def installed_prefix(tmp_path, monkeypatch):
    prefix = tmp_path / "installation"
    prefix.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    (release / "deployment.json").write_text(
        json.dumps({"layout": {"APP_WATCH_INSTALL_PREFIX": str(prefix)}})
    )
    lock = prefix / "install.lock"
    lock.touch(mode=0o644)
    monkeypatch.setattr(sys, "prefix", str(release / "venv"))
    return prefix, release


def test_interrupted_journal_excludes_runtime_changes(installed_prefix):
    prefix, _ = installed_prefix
    with software_guard():
        pass
    (prefix / "transaction.json").write_text("{}")
    with pytest.raises(MaintenanceBusy, match="Interrupted"), software_guard():
        pass


def test_exclusive_installer_lock_blocks_other_processes(installed_prefix):
    prefix, release = installed_prefix
    fd = os.open(prefix / "install.lock", os.O_RDWR)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = (
            "import sys; from app_watch.installation import software_guard, MaintenanceBusy; "
            f"sys.prefix={str(release / 'venv')!r}; "
            "exec(\"try:\\n with software_guard(): print('unsafe')\\n"
            "except MaintenanceBusy: print('blocked')\")"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert result.stdout.strip() == "blocked"
    finally:
        os.close(fd)


def test_read_only_clients_cannot_impersonate_exclusive_maintenance(installed_prefix):
    prefix, _ = installed_prefix
    fd = os.open(prefix / "install.lock", os.O_RDONLY)
    try:
        with pytest.raises(OSError):
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
