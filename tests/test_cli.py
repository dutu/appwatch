from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from app_watch.cli import create_parser, main
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
    assert "install this watch's user monitor service" in help_text


@pytest.mark.parametrize("command", ["install", "update"])
def test_flat_service_commands_prepare_watch(watch, monkeypatch, command):
    watch.source_path.write_text(json.dumps(watch.config.model_dump()))
    calls = []
    monkeypatch.setattr(service, "manage_service", lambda *args, **kw: calls.append((args, kw)))
    assert main(["--config", str(watch.source_path), command]) == 0
    args, kwargs = calls.pop()
    assert args == (watch.source_path, command)
    assert kwargs["watch"].scope == watch.scope


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


def test_list_needs_no_options(monkeypatch):
    monkeypatch.setattr("app_watch.cli.status.inventory", lambda: [])
    assert main(["list"]) == 0


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
