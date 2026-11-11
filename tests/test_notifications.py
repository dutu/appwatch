from __future__ import annotations

import json
import subprocess
import sys
from threading import Event

import pytest

from app_watch.cli import main
from app_watch.notifications.events import notify_events
from app_watch.notifications.notifier import NotificationConfig, NotifySendNotifier
from app_watch.trigger.lifecycle import LifecycleEvent
from tests.test_execution import watch as watch


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--help"],
        ["--level", "error", "message"],
        ["--config", "another.ini", "--user", "--timeout", "5"],
        ["", "space word", "a\nb", "--", "-x", "$(touch SHOULD_NOT_EXIST)"],
    ],
)
def test_external_notify_arguments_are_opaque(watch, tmp_path, arguments):
    recorded = tmp_path / "arguments.json"
    executable = tmp_path / "notifier"
    executable.write_text(
        f"#!{sys.executable}\nimport sys,json\n"
        f'open({str(recorded)!r}, "w").write(json.dumps(sys.argv[1:]))\n'
    )
    executable.chmod(0o700)
    document = watch.config.model_dump()
    document["external_notifier"] = {"command": str(executable)}
    watch.source_path.write_text(json.dumps(document))
    assert main(["--user", "--config", str(watch.source_path), "notify", *arguments]) == 0
    assert json.loads(recorded.read_text()) == arguments
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_external_notify_missing_program_is_best_effort_without_private_arguments(watch, caplog):
    document = watch.config.model_dump()
    document["external_notifier"] = {"command": "/no/such/notifier"}
    watch.source_path.write_text(json.dumps(document))
    assert main(["--user", "--config", str(watch.source_path), "notify", "PRIVATE_VALUE"]) == 0
    assert "Cannot execute external notifier" in caplog.text and "PRIVATE_VALUE" not in caplog.text


def test_external_notifier_must_be_configured(watch, capsys):
    watch.source_path.write_text(watch.config.model_dump_json())
    assert main(["--user", "--config", str(watch.source_path), "notify", "hello"]) == 2
    assert "external_notifier is required" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["tool --option", "./tool"])
def test_external_notify_rejects_invalid_command_before_delivery(
    watch, capsys, monkeypatch, command
):
    document = watch.config.model_dump()
    document["external_notifier"] = {"command": command}
    watch.source_path.write_text(json.dumps(document))
    monkeypatch.setattr(
        "app_watch.notifications.external.notify", lambda *args: pytest.fail("must not deliver")
    )
    assert main(["--user", "--config", str(watch.source_path), "notify", "hello"]) == 2
    assert "one executable" in capsys.readouterr().err


def test_external_notify_rejects_nul_before_delivery(watch, monkeypatch):
    monkeypatch.setattr(
        "app_watch.notifications.external.notify", lambda *args: pytest.fail("must not send")
    )
    with pytest.raises(SystemExit) as caught:
        main(["--user", "--config", str(watch.source_path), "notify", "bad\0arg"])
    assert caught.value.code == 2


def test_notification_events_exclude_action_settings_and_raw_errors(watch):
    class RecordingNotifier:
        def __init__(self):
            self.calls = []

        def notify(self, title, body, **kwargs):
            self.calls.append((title, body, kwargs))

    notifier = RecordingNotifier()
    events = [LifecycleEvent("action_failed", "PRIVATE_OUTPUT"), LifecycleEvent("armed")]
    notify_events(notifier, watch, events)
    assert len(notifier.calls) == 1
    assert notifier.calls[0][2]["finished"]
    assert "PRIVATE_OUTPUT" not in str(notifier.calls)
    assert str(watch.config.action.argv) not in str(notifier.calls)


def test_notification_queue_has_a_hard_bound(monkeypatch):
    started, release = Event(), Event()

    def slow_run(argv, **kwargs):
        started.set()
        assert release.wait(3)
        return subprocess.CompletedProcess(argv, 0, b"42\n", b"")

    monkeypatch.setattr("app_watch.notifications.notifier.subprocess.run", slow_run)
    monkeypatch.setattr(
        "app_watch.notifications.notifier.notification_bus", lambda *args: "unix:path=/mock"
    )
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    try:
        notifier.notify("start", "start")
        assert started.wait(3)
        for number in range(100):
            notifier.notify("phase", str(number), key=f"key-{number}")
        assert len(notifier._pending) == 64
    finally:
        release.set()
        notifier.close()


def test_external_notifier_timeout_is_bounded_and_best_effort(watch, tmp_path, caplog):
    executable = tmp_path / "slow-notifier"
    executable.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
    executable.chmod(0o700)
    document = watch.config.model_dump()
    document["external_notifier"] = {"command": str(executable), "timeout_seconds": 0.05}
    watch.source_path.write_text(json.dumps(document))
    assert main(["--user", "--config", str(watch.source_path), "notify", "hello"]) == 0
    assert "timed out after 0.05 seconds" in caplog.text
