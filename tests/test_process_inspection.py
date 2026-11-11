from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app_watch.process import PsutilProcessProvider
from app_watch.process_inspection import protected_executables
from scripts import process_inspect
from tests.test_process import _Process


@pytest.fixture
def helper(monkeypatch, tmp_path):
    prefix = tmp_path / "release"
    prefix.mkdir()
    (prefix / "deployment.json").write_text(
        json.dumps({"layout": {"APP_WATCH_INSTALL_PREFIX": str(prefix)}})
    )
    (prefix / "process-inspect").touch()
    monkeypatch.setattr(sys, "prefix", str(prefix / "venv"))
    return prefix / "process-inspect"


def test_client_uses_fixed_noninteractive_command_and_validates_owner(helper, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {"version": 1, "uid": os.geteuid(), "processes": [[42, 1, "/bin/test"]]}
            ).encode(),
        )

    monkeypatch.setattr("app_watch.process_inspection.subprocess.run", run)
    assert protected_executables(os.geteuid()) == {42: (1, "/bin/test")}
    assert calls[0][0] == ["/usr/bin/sudo", "-n", "--", str(helper)]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert calls[0][1]["timeout"] == 2
    assert protected_executables(os.geteuid() + 1) == {}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    ["timeout", "denied", "json", "owner", "pid", "relative", "nan", "overflow", "duplicate"],
)
def test_client_failures_provide_no_identity(helper, monkeypatch, failure):
    payload = {"version": 1, "uid": os.geteuid(), "processes": [[42, 1, "/bin/test"]]}
    if failure == "owner":
        payload["uid"] += 1
    elif failure == "pid":
        payload["processes"][0][0] = True
    elif failure == "relative":
        payload["processes"][0][2] = "bin/test"
    elif failure == "nan":
        payload["processes"][0][1] = float("nan")
    elif failure == "overflow":
        payload["processes"][0][1] = 10**500
    elif failure == "duplicate":
        payload["processes"] *= 2

    def run(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 2)
        return subprocess.CompletedProcess(
            argv,
            1 if failure == "denied" else 0,
            b"invalid" if failure == "json" else json.dumps(payload).encode(),
        )

    monkeypatch.setattr("app_watch.process_inspection.subprocess.run", run)
    assert protected_executables(os.geteuid()) == {}


@pytest.mark.parametrize(
    "identity", [None, (2, "/bin/test"), (1.009, "/bin/test"), (1, "/bin/test (deleted)")]
)
def test_snapshot_only_accepts_verified_same_process(monkeypatch, identity):
    proc = _Process(42, os.geteuid())
    monkeypatch.setattr("app_watch.process.psutil.process_iter", lambda attrs: [proc])
    monkeypatch.setattr(PsutilProcessProvider, "alive", lambda *args: True)

    def denied(path):
        raise PermissionError(path)

    monkeypatch.setattr("app_watch.process.os.readlink", denied)
    monkeypatch.setattr(
        "app_watch.process.protected_executables", lambda uid: {42: identity} if identity else {}
    )
    result = PsutilProcessProvider().snapshot()[0]
    assert result.exe_realpath == (identity[1] if identity and identity[0] == 1 else None)


def test_snapshot_rejects_identity_when_process_exits_during_helper_call(monkeypatch):
    proc = _Process(42, os.geteuid())
    monkeypatch.setattr("app_watch.process.psutil.process_iter", lambda attrs: [proc])
    alive = iter([True, False])
    monkeypatch.setattr(PsutilProcessProvider, "alive", lambda *args: next(alive))

    def denied(path):
        raise PermissionError(path)

    monkeypatch.setattr("app_watch.process.os.readlink", denied)
    monkeypatch.setattr(
        "app_watch.process.protected_executables", lambda uid: {42: (1, "/bin/test")}
    )
    assert PsutilProcessProvider().snapshot()[0].exe_realpath is None


def test_helper_reads_only_verified_caller_processes(monkeypatch):
    monkeypatch.setattr(Path, "iterdir", lambda path: [Path("/proc/1"), Path("/proc/42")])
    monkeypatch.setattr(
        process_inspect, "identity", lambda path: (1000 if path.name == "42" else 0, 100)
    )
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path: "btime 123\n" if str(path) == "/proc/stat" else original(path),
    )
    paths = []

    def readlink(path):
        paths.append(path)
        return "/protected/test (deleted)"

    monkeypatch.setattr(os, "readlink", readlink)
    monkeypatch.setattr(os, "sysconf", lambda name: 100)
    assert process_inspect.inspect(1000) == [[42, 124, "/protected/test (deleted)"]]
    assert paths == [Path("/proc/42/exe")]


@pytest.mark.parametrize("changed", [(2000, 100), (1000, 101)])
def test_helper_rejects_ownership_changes_and_pid_reuse(monkeypatch, changed):
    monkeypatch.setattr(Path, "iterdir", lambda path: [Path("/proc/42")])
    values = iter([(1000, 100), changed])
    monkeypatch.setattr(process_inspect, "identity", lambda path: next(values))
    monkeypatch.setattr(Path, "read_text", lambda path: "btime 123\n")
    monkeypatch.setattr(os, "readlink", lambda path: "/protected/test")
    assert process_inspect.inspect(1000) == []


@pytest.mark.parametrize(
    "root,caller,args",
    [(False, "1000", []), (True, "", []), (True, "0", []), (True, "1000", ["/etc/shadow"])],
)
def test_helper_rejects_unprivileged_calls_missing_caller_and_arguments(
    monkeypatch, root, caller, args
):
    monkeypatch.setattr(os, "geteuid", lambda: 0 if root else 1000)
    monkeypatch.setenv("SUDO_UID", caller)
    monkeypatch.setattr(sys, "argv", ["process-inspect", *args])
    assert process_inspect.main() == 2


def test_helper_identity_uses_kernel_uid_and_stat_start_ticks(tmp_path):
    (tmp_path / "status").write_text("Name:\tfoo\nUid:\t1000\t1000\t1000\t1000\n")
    (tmp_path / "stat").write_text(
        "42 (name with ) parentheses) " + " ".join(["S", *(["0"] * 18), "12345"])
    )
    assert process_inspect.identity(tmp_path) == (1000, 12345)
