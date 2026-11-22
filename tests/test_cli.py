from __future__ import annotations

import io
import json
import shlex
import sys
from pathlib import Path

import pytest

from app_watch.cli import create_parser, main
from app_watch.errors import BlockedError
from app_watch.services import monitor as service
from tests.test_execution import watch as watch


@pytest.mark.parametrize("argv", [["--user", "list"], ["trigger", "monitor"]])
def test_removed_cli_forms_are_rejected(argv):
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 2


def test_help_describes_current_account_and_flat_commands():
    help_text = create_parser().format_help()
    assert "current account" in help_text
    assert "--user" not in help_text
    assert "trigger" not in help_text
    assert "exec-worker" not in help_text
    assert "install this watch's user monitor service" in help_text


@pytest.mark.parametrize("command", ["install", "update"])
def test_flat_service_commands_prepare_watch(watch, monkeypatch, command, capsys):
    watch.source_path.write_text(json.dumps(watch.config.model_dump()))
    calls = []
    monkeypatch.setattr(service, "manage_service", lambda *args, **kw: calls.append((args, kw)))
    assert main(["--config", str(watch.source_path), command]) == 0
    args, kwargs = calls.pop()
    assert args == (watch.source_path, command)
    assert kwargs["watch"].scope == watch.scope
    captured = capsys.readouterr()
    assert captured.out == f"Trigger {command}:  OK\n"
    assert captured.err == ""


@pytest.mark.parametrize("command", ["disable", "remove"])
def test_service_recovery_commands_use_same_output(watch, monkeypatch, command, capsys):
    calls = []
    monkeypatch.setattr(service, "manage_service", lambda *args, **kw: calls.append((args, kw)))
    assert main(["--config", str(watch.source_path), command]) == 0
    assert calls == [((watch.source_path, command), {})]
    captured = capsys.readouterr()
    assert captured.out == f"Trigger {command}:  OK\n"
    assert captured.err == ""


@pytest.mark.parametrize("command", ["install", "update", "disable", "remove"])
def test_service_failure_uses_vaultkeep_diagnostic(watch, monkeypatch, command, capsys):
    watch.source_path.write_text(json.dumps(watch.config.model_dump()))

    def fail(*args, **kwargs):
        raise BlockedError("systemd user manager is unavailable")

    monkeypatch.setattr(service, "manage_service", fail)
    assert main(["--config", str(watch.source_path), command]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FAILED: BlockedError: systemd user manager is unavailable\n"


@pytest.mark.parametrize(
    "no_color,env_color,colored", [(False, "", True), (True, "", False), (False, "1", False)]
)
def test_install_colors_and_opt_out(watch, monkeypatch, no_color, env_color, colored):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    output, errors = Terminal(), Terminal()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("NO_COLOR", env_color)
    watch.source_path.write_text(json.dumps(watch.config.model_dump()))
    monkeypatch.setattr(service, "manage_service", lambda *args, **kw: None)
    argv = ["--no-color"] if no_color else []
    argv += ["--config", str(watch.source_path), "install"]
    assert main(argv) == 0
    status = "\033[32mOK\033[0m" if colored else "OK"
    assert output.getvalue() == f"Trigger install:  {status}\n"
    # Failed config loading uses the same stderr color controls.
    watch.source_path.unlink()
    assert main(argv) == 2
    prefix = "\033[1;31mFAILED\033[0m" if colored else "FAILED"
    assert errors.getvalue().startswith(f"{prefix}: ConfigurationError:")


def test_flat_monitor_passes_pinned_identity(watch, monkeypatch):
    watch.source_path.write_text(json.dumps(watch.config.model_dump()))
    calls = []
    monkeypatch.setattr(
        "app_watch.cli.monitor.run_monitor", lambda *args, **kw: calls.append((args, kw)) or 0
    )
    assert (
        main(["--config", str(watch.source_path), "monitor", "--expected-identity", watch.scope])
        == 0
    )
    args, kwargs = calls.pop()
    assert args[0].scope == watch.scope
    assert kwargs == {"expected_identity": watch.scope}


def test_list_needs_no_options(monkeypatch, capsys):
    monkeypatch.setattr("app_watch.cli.status.inventory", lambda: [])
    assert main(["list"]) == 0
    assert capsys.readouterr().out == (
        "Watch  Configuration  Monitor  Application  Action\nNo watches found.\n"
    )


def test_list_aligns_inventory_columns(monkeypatch, capsys):
    job_id = "monero_2026-11-11-error_sd7_nas-restic_full"
    rows = [
        {
            "job_id": job_id,
            "configuration": "VALID",
            "monitor": "RUNNING",
            "application": "NOT RUNNING",
            "dispatch": "IDLE",
            "diagnostics": [],
        },
        {
            "config": "/jobs/世界e\u0301\t.yaml",
            "configuration": "INVALID",
            "monitor": "NOT INSTALLED",
            "diagnostics": ["Invalid config"],
        },
    ]
    monkeypatch.setattr("app_watch.cli.status.inventory", lambda: rows)
    assert main(["list"]) == 0
    watch_width = len(job_id)
    config = "/jobs/世界e\u0301\\t.yaml"
    config_padding = " " * (watch_width - len(config) - 1)  # Two wide chars and one accent.
    assert capsys.readouterr().out.splitlines() == [
        f"{'Watch':<{watch_width}}  Configuration  Monitor        Application  Action",
        f"{job_id}  VALID          RUNNING        NOT RUNNING  IDLE",
        f"{config}{config_padding}  INVALID        NOT INSTALLED  UNKNOWN      UNKNOWN",
        "  Invalid config",
    ]


def test_generated_monitor_uses_flat_command(watch):
    content = service.render_service(watch.identity, Path("/usr/local/bin/app-watch"))
    command = next(
        line.removeprefix("ExecStart=")
        for line in content.splitlines()
        if line.startswith("ExecStart=")
    )
    assert shlex.split(command) == [
        "/usr/local/bin/app-watch",
        "--config",
        str(watch.source_path),
        "monitor",
        "--expected-identity",
        watch.scope,
    ]


@pytest.mark.parametrize(
    "command", ["monitor", "exec-worker", "install", "update", "disable", "remove"]
)
def test_privileged_commands_still_refuse_root(watch, monkeypatch, command, capsys):
    watch.source_path.write_text(json.dumps(watch.config.model_dump()))
    monkeypatch.setattr("app_watch.cli.prepare_config", lambda *args, **kwargs: watch)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    argv = (
        ["exec-worker", "invalid"]
        if command == "exec-worker"
        else ["--config", str(watch.source_path), command]
    )
    assert main(argv) == 2
    assert "non-root application owner" in capsys.readouterr().err
