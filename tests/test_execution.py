from __future__ import annotations

import json
import os
import signal
import sys
from contextlib import suppress
from uuid import uuid4

import pytest

from app_watch.actions.environment import build_child_environment
from app_watch.actions.runner import run_action
from app_watch.actions.worker import exec_worker
from app_watch.config import prepare_config, validate_document
from app_watch.errors import BlockedError, StateError
from app_watch.process import DetectionResult, Observation, ProcessInfo, boot_id
from app_watch.services.systemd import (
    SubmissionClass,
    SubmissionResult,
    UnitState,
    classify_submission,
)
from app_watch.trigger.lifecycle import ExecutionContext, progress
from app_watch.trigger.locking import exclusive_lock
from app_watch.trigger.models import InstanceState, LaunchRecord, Request, utc_now
from app_watch.trigger.monitor import Monitor, worker_unit
from app_watch.trigger.state import StateStore
from tests.helpers import FakeProvider


@pytest.fixture
def watch(tmp_path):
    return prepare_config(
        validate_document(
            {
                "config_version": 1,
                "job": {"id": "bisq-test"},
                "application": {
                    "name": "bisq",
                    "data_dir": str(tmp_path),
                    "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/wallet"}]},
                },
                "action": {"argv": [sys.executable, "-c", 'print("done")']},
                "trigger": {"enabled": True, "confirm_seconds": 5},
            }
        ),
        tmp_path / "job.yaml",
    )


@pytest.fixture
def store(tmp_path):
    result = StateStore(tmp_path / "state")
    result.ensure()
    return result


def observation(kind, boot=None):
    return DetectionResult(kind, boot_id=boot or boot_id())


def context(watch):
    return ExecutionContext(environment=build_child_environment(home=watch.home))


def request_for(watch):
    return Request.capture(
        watch,
        environment=context(watch).environment,
        detection=observation(Observation.STOPPED),
        now=utc_now(),
    )


def stage(store, watch, request=None):
    request = request or request_for(watch)
    attempt = str(uuid4())
    store.write_request(attempt, request)
    store.write_launch(
        LaunchRecord(
            attempt_id=attempt,
            request_id=request.request_id,
            instance_id=request.instance_id,
            unit=worker_unit(watch.scope, attempt),
            intent_at=utc_now(),
        )
    )
    store.write_instance(
        InstanceState(
            instance_id=request.instance_id,
            active_attempt_id=attempt,
            active_request_id=request.request_id,
        )
    )
    return attempt


def running(watch):
    return ProcessInfo(
        pid=1234,
        create_time=1.0,
        exe_realpath="/opt/wallet",
        cmdline=("/opt/wallet", "--appDataDir", watch.config.application.data_dir),
    )


class Manager:
    def __init__(self, classification=SubmissionClass.ACCEPTED):
        self.classification = classification
        self.starts = []
        self.units = {}

    def start_unit(self, unit):
        self.starts.append(unit)
        if self.classification == SubmissionClass.ACCEPTED:
            self.units[unit] = UnitState("loaded", "activating", "start")
        return SubmissionResult(self.classification, 0, "", True)

    def unit_state(self, unit):
        return self.units.get(unit, UnitState("not-found", "inactive", "dead"))


def confirmed_monitor(watch, store):
    provider, manager = FakeProvider([running(watch)]), Manager()
    monitor = Monitor(watch, store, provider, manager)
    monitor.tick(monotonic_now=0)
    provider.processes.clear()
    monitor.tick(monotonic_now=1)
    monitor.tick(monotonic_now=6)
    assert monitor.state.active_attempt_id
    return monitor, provider, manager


def test_lifecycle_start_stopped_then_continuous_absence(watch):
    state = InstanceState(instance_id=watch.config.job.id)
    ctx = context(watch)
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=0)
    assert state.pending_request is None and not state.armed
    progress(state, watch, observation(Observation.RUNNING), ctx, monotonic_now=1)
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=2)
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=6)
    assert state.pending_request is None
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=7)
    assert state.pending_request is not None and not state.armed
    request_id = state.pending_request.request_id
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=100)
    assert state.pending_request.request_id == request_id
    progress(state, watch, observation(Observation.RUNNING), ctx, monotonic_now=101)
    assert state.pending_request is None and state.armed


@pytest.mark.parametrize("kind", [Observation.AMBIGUOUS, Observation.INSPECTION_UNAVAILABLE])
def test_uncertainty_restarts_full_confirmation(watch, kind):
    state = InstanceState(instance_id=watch.config.job.id)
    ctx = context(watch)
    for kind_now, now in [
        (Observation.RUNNING, 0),
        (Observation.STOPPED, 1),
        (kind, 5),
        (Observation.STOPPED, 6),
        (Observation.STOPPED, 10),
    ]:
        progress(state, watch, observation(kind_now), ctx, monotonic_now=now)
    assert state.pending_request is None
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=11)
    assert state.pending_request is not None


def test_recovery_does_not_persist_monotonic_time_and_reconfirms_pending(watch, store):
    state = InstanceState(
        instance_id=watch.config.job.id,
        armed=True,
        candidate_monotonic=1000,
        config_fingerprint=watch.fingerprint(),
        boot_id=boot_id(),
    )
    store.write_instance(state)
    assert "candidate_monotonic" not in store.instance_path(state.instance_id).read_text()
    state = store.read_instance(state.instance_id).recovered()
    ctx = context(watch)
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=1)
    assert state.pending_request is None
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=6)
    request = state.pending_request
    assert request is not None
    store.write_instance(state)
    state = store.read_instance(state.instance_id).recovered()
    assert state.pending_reconfirmation_required
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=10)
    assert state.pending_reconfirmation_required
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=15)
    assert not state.pending_reconfirmation_required
    assert state.pending_request.request_id == request.request_id


def test_boot_and_config_changes_invalidate_running_evidence(watch):
    for change in ("boot", "config"):
        state = InstanceState(
            instance_id=watch.config.job.id,
            armed=True,
            candidate_monotonic=0,
            config_fingerprint="old" if change == "config" else watch.fingerprint(),
            boot_id="old-boot" if change == "boot" else boot_id(),
            pending_request=request_for(watch),
        )
        progress(state, watch, observation(Observation.STOPPED), context(watch), monotonic_now=100)
        assert not state.armed and state.pending_request is None


def test_snapshot_detaches_action_matcher_and_environment(watch):
    env = build_child_environment(home=watch.home)
    request = Request.capture(
        watch, environment=env, detection=observation(Observation.STOPPED), now=utc_now()
    )
    watch.config.action.argv.append("later-edit")
    watch.config.application.matcher.entrypoints[0].value = "/different"
    env["HOME"] = "/different"
    assert "later-edit" not in request.action.argv
    assert request.application.matcher.entrypoints[0].value == "/opt/wallet"
    assert request.environment["HOME"] == str(watch.home)


def test_runner_literal_arguments_environment_and_working_directory(tmp_path):
    args = ["$(touch SHOULD_NOT_EXIST)", "*", "space word", "a;b", ""]
    result = run_action(
        [
            sys.executable,
            "-c",
            "import os,sys,json; print(json.dumps([sys.argv[1:],dict(os.environ),os.getcwd()]))",
            *args,
        ],
        environment={"HOME": str(tmp_path), "EXPECTED": "yes"},
        cwd=tmp_path,
    )
    assert result.exit_code == 0
    argv, env, cwd = json.loads(result.diagnostics)
    assert argv == args and env["EXPECTED"] == "yes" and cwd == str(tmp_path)
    assert "PATH" not in env
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_runner_caps_both_streams_and_waits_after_output_eof(tmp_path):
    code = (
        "import os,time; os.write(1,b'o'*600000); os.write(2,b'e'*600000); "
        "os.close(1); os.close(2); time.sleep(.1)"
    )
    result = run_action([sys.executable, "-c", code], environment={}, cwd=tmp_path, cap=1024)
    assert result.exit_code == 0 and result.duration_seconds >= 0.1
    assert result.diagnostics_truncated and len(result.diagnostics) == 1024


@pytest.mark.parametrize(
    "code,exit_code,signal_number",
    [
        ("raise SystemExit(10)", 10, None),
        ("import os,signal; os.kill(os.getpid(),signal.SIGTERM)", None, signal.SIGTERM),
    ],
)
def test_runner_records_generic_exits_and_signals(tmp_path, code, exit_code, signal_number):
    result = run_action([sys.executable, "-c", code], environment={}, cwd=tmp_path)
    assert result.exit_code == exit_code and result.signal_number == signal_number


def test_runner_missing_executable(tmp_path):
    result = run_action(["/no/such/app-watch-command"], environment={}, cwd=tmp_path)
    assert result.spawn_error and result.exit_code is None


def test_environment_is_explicit_and_drops_arbitrary_secrets(tmp_path):
    env = build_child_environment(
        home=tmp_path,
        environ={
            "HOME": "/untrusted",
            "SECRET": "secret",
            "PATH": "/untrusted",
            "XDG_STATE_HOME": "relative",
            "XDG_CACHE_HOME": "/valid/cache",
            "LANG": "C",
        },
    )
    assert env == {
        "HOME": str(tmp_path),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C",
        "XDG_STATE_HOME": str(tmp_path / ".local/state"),
        "XDG_CACHE_HOME": "/valid/cache",
    }


def test_worker_runs_once_and_receipt_is_immutable(watch, store, tmp_path):
    output = tmp_path / "calls"
    watch.config.action.argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; p=Path({str(output)!r}); "
        'p.write_text(p.read_text()+"x" if p.exists() else "x")',
    ]
    attempt = stage(store, watch)
    first = exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
    second = exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
    assert first == second and first.outcome == "succeeded"
    assert output.read_text() == "x"
    with pytest.raises(StateError):
        store.write_receipt(first)


def test_worker_claim_without_receipt_blocks_replay(watch, store):
    attempt = stage(store, watch)
    store.write(
        store.attempt_dir(attempt) / "execution.json", store.read_launch(attempt), immutable=True
    )
    with pytest.raises(BlockedError, match="already claimed"):
        exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
    assert store.read_receipt(attempt) is None


@pytest.mark.parametrize("observation_kind", ["running", "unavailable"])
def test_worker_final_recheck_prevents_execution(watch, store, monkeypatch, observation_kind):
    attempt = stage(store, watch)
    provider = FakeProvider([running(watch)])
    if observation_kind == "unavailable":
        provider.alive_result = False

    def unexpected_run(*args, **kwargs):
        raise AssertionError("Must not run while app is running or uncertain")

    monkeypatch.setattr("app_watch.actions.worker.run_action", unexpected_run)
    receipt = exec_worker(store, attempt, scope=watch.scope, provider=provider)
    assert receipt.outcome == "blocked" and receipt.exit_code is None


def test_worker_exit_ten_is_failure_not_retryable(watch, store):
    watch.config.action.argv = [sys.executable, "-c", "raise SystemExit(10)"]
    attempt = stage(store, watch)
    receipt = exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
    assert receipt.outcome == "failed" and receipt.exit_code == 10


def test_monitor_dispatches_reconciles_and_does_not_repeat(watch, store):
    monitor, provider, manager = confirmed_monitor(watch, store)
    attempt = monitor.state.active_attempt_id
    receipt = exec_worker(store, attempt, scope=watch.scope, provider=provider)
    monitor.tick(monotonic_now=7)
    assert monitor.state.active_attempt_id == attempt  # Receipt alone does not release slot.
    manager.units.clear()
    monitor.tick(monotonic_now=8)
    assert monitor.state.active_attempt_id is None
    assert monitor.state.last_receipt == receipt
    monitor.tick(monotonic_now=100)
    assert len(manager.starts) == 1


def test_monitor_restart_recovers_live_worker(watch, store):
    monitor, provider, manager = confirmed_monitor(watch, store)
    attempt = monitor.state.active_attempt_id
    restarted = Monitor(watch, store, provider, manager)
    restarted.tick(monotonic_now=10)
    assert restarted.state.active_attempt_id == attempt and len(manager.starts) == 1
    exec_worker(store, attempt, scope=watch.scope, provider=provider)
    manager.units.clear()
    restarted.tick(monotonic_now=11)
    assert restarted.state.active_attempt_id is None


def test_missing_receipt_blocks_replay_across_monitor_restarts(watch, store):
    monitor, provider, manager = confirmed_monitor(watch, store)
    manager.units.clear()
    monitor.tick(monotonic_now=8)
    assert monitor.state.blocked_reason and store.blocked_reason(watch.config.job.id)
    restarted = Monitor(watch, store, provider, manager)
    restarted.tick(monotonic_now=100)
    assert len(manager.starts) == 1


def test_pending_after_active_attempt_waits_for_receipt_and_worker_exit(watch, store):
    monitor, provider, manager = confirmed_monitor(watch, store)
    attempt = monitor.state.active_attempt_id
    provider.processes = [running(watch)]
    monitor.tick(monotonic_now=7)
    provider.processes.clear()
    monitor.tick(monotonic_now=8)
    monitor.tick(monotonic_now=13)
    assert monitor.state.pending_request is not None and len(manager.starts) == 1
    exec_worker(store, attempt, scope=watch.scope, provider=provider)
    manager.units.clear()
    monitor.tick(monotonic_now=14)
    assert len(manager.starts) == 2 and monitor.state.active_attempt_id != attempt


def test_nonzero_systemctl_codes_do_not_prove_non_submission():
    for code in (1, 2, 4, 5, 6, None):
        assert classify_submission(spawned=True, exit_code=code) == SubmissionClass.INCONCLUSIVE
    assert classify_submission(spawned=False, exit_code=None) == SubmissionClass.REJECTED


def test_state_private_atomic_and_corrupt_evidence_preserved(watch, store):
    state = InstanceState(instance_id=watch.config.job.id)
    store.write_instance(state)
    path = store.instance_path(state.instance_id)
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    path.write_text("{corrupt")
    with pytest.raises(StateError):
        Monitor(watch, store, FakeProvider(), Manager())
    assert path.read_text() == "{corrupt"
    assert store.blocked_reason(state.instance_id)


def test_state_rejects_symlinks_and_path_traversal(watch, store, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    store.instance_path(watch.config.job.id).symlink_to(outside)
    with pytest.raises(StateError):
        store.read_instance(watch.config.job.id)
    assert outside.read_text() == "untouched"
    for value in ("../escape", "/etc/passwd", ".", "not-a-uuid"):
        with pytest.raises(StateError):
            store.attempt_dir(value)


def test_exclusive_monitor_or_worker_lock(tmp_path):
    path = tmp_path / "locks" / "monitor.lock"
    with exclusive_lock(path), pytest.raises(BlockedError), exclusive_lock(path):
        pass
    with exclusive_lock(path):
        pass


def test_pending_supersession_counts_replaced_request(watch):
    original = request_for(watch)
    state = InstanceState(
        instance_id=watch.config.job.id,
        armed=True,
        pending_request=original,
        candidate_monotonic=0,
        candidate_since=utc_now(),
        config_fingerprint=watch.fingerprint(),
        boot_id=boot_id(),
    )
    events = progress(
        state, watch, observation(Observation.STOPPED), context(watch), monotonic_now=6
    )
    assert state.superseded_drop_count == 1
    assert state.pending_request.request_id != original.request_id
    assert "pending_superseded" in [event.kind for event in events]


def test_local_spawn_rejection_can_retry_only_after_reconfirmation(watch, store):
    provider, manager = FakeProvider([running(watch)]), Manager(SubmissionClass.REJECTED)
    monitor = Monitor(watch, store, provider, manager)
    monitor.tick(monotonic_now=0)
    provider.processes.clear()
    monitor.tick(monotonic_now=1)
    monitor.tick(monotonic_now=6)
    attempt = monitor.state.active_attempt_id
    monitor.tick(monotonic_now=7)
    assert monitor.state.active_attempt_id is None
    assert monitor.state.pending_request is not None
    assert attempt in monitor.state.completed_attempts
    monitor.tick(monotonic_now=12)
    assert len(manager.starts) == 1
    monitor.tick(monotonic_now=37)
    assert len(manager.starts) == 2


def test_crash_before_launch_intent_is_proven_unsubmitted(watch, store):
    request = request_for(watch)
    state = InstanceState(
        instance_id=watch.config.job.id,
        pending_request=request,
        latest_request_id=request.request_id,
        boot_id=boot_id(),
        config_fingerprint=watch.fingerprint(),
    )
    store.write_instance(state)
    attempt = str(uuid4())
    store.write_request(attempt, request)  # Simulate crash before active association/launch.
    manager = Manager()
    monitor = Monitor(watch, store, FakeProvider(), manager)
    monitor.tick(monotonic_now=10)
    assert not manager.starts and monitor.state.active_attempt_id is None
    assert monitor.state.pending_request.request_id == request.request_id
    assert monitor.state.pending_reconfirmation_required
    monitor.tick(monotonic_now=15)
    assert not manager.starts  # Minimum retry delay also applies.
    monitor.tick(monotonic_now=40)
    assert len(manager.starts) == 1


def test_ambiguous_submission_is_never_automatically_retried(watch, store):
    provider, manager = FakeProvider([running(watch)]), Manager(SubmissionClass.INCONCLUSIVE)
    monitor = Monitor(watch, store, provider, manager)
    monitor.tick(monotonic_now=0)
    provider.processes.clear()
    monitor.tick(monotonic_now=1)
    monitor.tick(monotonic_now=6)
    monitor.tick(monotonic_now=7)
    assert monitor.state.blocked_reason
    monitor.tick(monotonic_now=100)
    assert len(manager.starts) == 1


def test_unreachable_manager_keeps_slot_until_evidence_returns(watch, store):
    monitor, provider, manager = confirmed_monitor(watch, store)
    attempt = monitor.state.active_attempt_id
    manager.units[worker_unit(watch.scope, attempt)] = None
    monitor.tick(monotonic_now=7)
    assert monitor.state.active_attempt_id == attempt and not monitor.state.blocked_reason
    exec_worker(store, attempt, scope=watch.scope, provider=provider)
    monitor.tick(monotonic_now=8)
    assert monitor.state.active_attempt_id == attempt
    manager.units.clear()
    monitor.tick(monotonic_now=9)
    assert monitor.state.active_attempt_id is None


def test_failed_action_is_terminal_and_later_shutdown_is_independent(watch, store):
    watch.config.action.argv = [sys.executable, "-c", "raise SystemExit(10)"]
    monitor, provider, manager = confirmed_monitor(watch, store)
    attempt = monitor.state.active_attempt_id
    exec_worker(store, attempt, scope=watch.scope, provider=provider)
    manager.units.clear()
    monitor.tick(monotonic_now=7)
    assert monitor.state.last_receipt.outcome == "failed"
    monitor.tick(monotonic_now=100)
    assert len(manager.starts) == 1
    provider.processes = [running(watch)]
    monitor.tick(monotonic_now=101)
    provider.processes.clear()
    monitor.tick(monotonic_now=102)
    monitor.tick(monotonic_now=107)
    assert len(manager.starts) == 2


def test_overdue_is_advisory_and_reported_once(watch, store):
    watch.config.trigger.deadline_seconds = 0.001
    monitor, provider, manager = confirmed_monitor(watch, store)
    attempt = monitor.state.active_attempt_id
    launch = store.read_launch(attempt)
    launch.intent_at = "2020-01-01T00:00:00Z"
    store.write_launch(launch)
    first = monitor.tick(monotonic_now=7)
    second = monitor.tick(monotonic_now=8)
    assert "action_overdue" in [event.kind for event in first]
    assert "action_overdue" not in [event.kind for event in second]
    assert monitor.state.active_attempt_id == attempt  # No kill/cancel path.
    assert manager.units[worker_unit(watch.scope, attempt)].is_active_or_pending()


def test_two_watches_have_independent_execution_slots(watch, store, tmp_path):
    first, provider, manager = confirmed_monitor(watch, store)
    config = watch.config.model_copy(deep=True)
    other_data = tmp_path / "other-data"
    other_data.mkdir()
    config.application.data_dir = str(other_data)
    other = prepare_config(config, tmp_path / "other.yaml")
    second_store = StateStore(tmp_path / "second-state")
    second, _, second_manager = confirmed_monitor(other, second_store)
    assert first.state.active_attempt_id and second.state.active_attempt_id
    assert first.watch.scope != second.watch.scope
    assert len(manager.starts) == len(second_manager.starts) == 1


def test_cleanup_retains_latest_fifty_completed_attempts(watch, store):
    monitor = Monitor(watch, store, FakeProvider(), Manager())
    attempts = [stage(store, watch) for _ in range(52)]
    monitor.state.completed_attempts = attempts[:]
    store.write_instance(monitor.state)
    monitor._cleanup()
    assert monitor.state.completed_attempts == attempts[2:]
    assert not store.attempt_dir(attempts[0]).exists()
    assert len(store.list_attempt_ids()) == 50


def test_cleanup_preserves_busy_worker_evidence(watch, store):
    attempt = stage(store, watch)
    path = store.attempt_dir(attempt) / "worker.lock"
    with exclusive_lock(path):
        assert not store.remove_attempt(attempt)
        assert store.read_request(attempt) is not None
    assert store.remove_attempt(attempt)


def test_atomic_failure_preserves_old_record(watch, store, monkeypatch):
    state = InstanceState(instance_id=watch.config.job.id)
    store.write_instance(state)
    original = store.instance_path(state.instance_id).read_bytes()

    def fail_replace(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("app_watch.trigger.state.os.replace", fail_replace)
    state.armed = True
    with pytest.raises(StateError):
        store.write_instance(state)
    assert store.instance_path(state.instance_id).read_bytes() == original
    assert not list(store.instance_path(state.instance_id).parent.glob(".*"))


def test_corrupt_watch_does_not_block_another(watch, store, tmp_path):
    store.instance_path(watch.config.job.id).write_text("corrupt")
    store.instance_path(watch.config.job.id).chmod(0o600)
    with pytest.raises(StateError):
        Monitor(watch, store, FakeProvider(), Manager())
    config = watch.config.model_copy(deep=True)
    config.job.id = "healthy"
    other = prepare_config(config, tmp_path / "healthy.yaml")
    healthy_store = StateStore(tmp_path / "healthy-state")
    healthy, _, manager = confirmed_monitor(other, healthy_store)
    assert healthy.state.active_attempt_id and len(manager.starts) == 1


def test_worker_wrong_scope_or_account_never_claims_execution(watch, store):
    attempt = stage(store, watch)
    with pytest.raises(StateError):
        exec_worker(store, attempt, scope="0" * 16, provider=FakeProvider())
    assert store.read_bytes(store.attempt_dir(attempt) / "execution.json") is None


def test_competing_worker_cannot_run_same_attempt(watch, store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app_watch.actions.runner import ActionResult

    entered, release = Event(), Event()
    calls = []

    def slow_action(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return ActionResult(utc_now(), utc_now(), 0.1, 0, None, b"", False)

    monkeypatch.setattr("app_watch.actions.worker.run_action", slow_action)
    attempt = stage(store, watch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(exec_worker, store, attempt, scope=watch.scope, provider=FakeProvider())
        try:
            assert entered.wait(5)
            with pytest.raises(BlockedError):
                exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
        finally:
            release.set()
        assert first.result(timeout=5).outcome == "succeeded"
    assert len(calls) == 1


def test_monitor_cli_preflight_and_signal_handlers(watch, store, tmp_path, monkeypatch):
    from threading import Event

    from app_watch.cli.monitor import run_monitor

    class StopAfterTick(Event):
        def wait(self, timeout=None):
            self.set()
            return True

    class ReadyManager(Manager):
        def manager_reachable(self):
            return True

        def template_ready(self):
            return True

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr("app_watch.cli.monitor.Event", StopAfterTick)
    monkeypatch.setattr("app_watch.cli.monitor.RealSystemdController", ReadyManager)
    monkeypatch.setattr("app_watch.cli.monitor.state_root", lambda scope: store.root)
    monkeypatch.setattr("app_watch.cli.monitor.require_monitor_identity", lambda *args: None)
    monkeypatch.setattr(
        "app_watch.cli.monitor.PsutilProcessProvider", lambda **kwargs: FakeProvider()
    )
    before = signal.getsignal(signal.SIGTERM)
    assert run_monitor(watch, expected_identity=watch.scope) == 0
    assert signal.getsignal(signal.SIGTERM) == before
    assert store.read_instance(watch.config.job.id).pending_request is None
    with pytest.raises(BlockedError, match="identity changed"):
        run_monitor(watch, expected_identity="0" * 16)


def test_cli_worker_rejects_arbitrary_paths(capsys):
    from app_watch.cli import main

    assert main(["exec-worker", "../../request.json"]) == 2
    assert "Invalid internal worker identity" in capsys.readouterr().err


def test_background_pipe_writer_cannot_hold_runner_forever(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    writer = "import os; exec(\"while True: os.write(1,b'x'*8192)\")"
    parent = (
        "import subprocess,sys; from pathlib import Path; "
        f'p=subprocess.Popen([sys.executable,"-c",{writer!r}]); '
        f"Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    descendant = None
    try:
        result = run_action([sys.executable, "-c", parent], environment={}, cwd=tmp_path, cap=1024)
        descendant = int(pid_file.read_text())
        assert result.exit_code == 0 and result.diagnostics_truncated
        assert result.duration_seconds < 2
    finally:
        if descendant is not None:
            with suppress(ProcessLookupError):
                os.kill(descendant, signal.SIGTERM)


def test_malformed_launch_timestamp_is_preserved_and_blocks_dispatch(watch, store):
    monitor, provider, manager = confirmed_monitor(watch, store)
    attempt = monitor.state.active_attempt_id
    path = store.attempt_dir(attempt) / "launch.json"
    data = json.loads(path.read_bytes())
    data["intent_at"] = "2026-01-01T00:00:00"  # Naive timestamp cannot establish elapsed time.
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    monitor.tick(monotonic_now=7)
    assert monitor.state.blocked_reason
    assert path.read_bytes() == original and len(manager.starts) == 1


def test_poll_or_logging_changes_do_not_invalidate_execution_fingerprint(watch):
    original = watch.fingerprint()
    watch.config.trigger.poll_seconds = 8
    watch.config.logging.level = "debug"
    assert watch.fingerprint() == original


def test_retry_delay_survives_monitor_restart(watch, store, monkeypatch):
    provider, manager = FakeProvider([running(watch)]), Manager(SubmissionClass.REJECTED)
    monitor = Monitor(watch, store, provider, manager)
    monitor.tick(monotonic_now=0)
    provider.processes.clear()
    monitor.tick(monotonic_now=1)
    monitor.tick(monotonic_now=6)
    monitor.tick(monotonic_now=7)
    monkeypatch.setattr("app_watch.trigger.monitor.time.monotonic", lambda: 100.0)
    restarted = Monitor(watch, store, provider, manager)
    restarted.tick(monotonic_now=100)
    restarted.tick(monotonic_now=105)
    assert len(manager.starts) == 1
    restarted.tick(monotonic_now=129)
    assert len(manager.starts) == 1
    restarted.tick(monotonic_now=131)
    assert len(manager.starts) == 2


def test_confirmation_ignores_wall_clock_jumps(watch):
    state = InstanceState(instance_id=watch.config.job.id)
    ctx = context(watch)
    progress(state, watch, observation(Observation.RUNNING), ctx, monotonic_now=0)
    progress(state, watch, observation(Observation.STOPPED), ctx, monotonic_now=1)
    progress(
        state,
        watch,
        observation(Observation.STOPPED),
        ctx,
        monotonic_now=2,
        utc_now_value="2099-01-01T00:00:00Z",
    )
    assert state.pending_request is None
    progress(
        state,
        watch,
        observation(Observation.STOPPED),
        ctx,
        monotonic_now=6,
        utc_now_value="2000-01-01T00:00:00Z",
    )
    assert state.pending_request is not None


def test_worker_records_executable_disappearing_after_dispatch(watch, store):
    watch.config.action.argv = ["/no/such/app-watch-command"]
    attempt = stage(store, watch)
    receipt = exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
    assert receipt.outcome == "failed" and receipt.reason and receipt.exit_code is None
    assert store.read_receipt(attempt) == receipt


def test_completed_attempt_cannot_execute_after_receipt_cleanup_gap(watch, store, monkeypatch):
    attempt = stage(store, watch)
    # Emulate a late worker start during deletion of already-reconciled history:
    # the request/launch remain, but the claim and receipt have been removed.
    store.write_instance(
        InstanceState(
            instance_id=watch.config.job.id,
            completed_attempts=[attempt],
        )
    )

    def unexpected_run(*args, **kwargs):
        raise AssertionError("Completed history must never execute again")

    monkeypatch.setattr("app_watch.actions.worker.run_action", unexpected_run)
    with pytest.raises(BlockedError, match="not the monitor"):
        exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
    assert store.read_bytes(store.attempt_dir(attempt) / "execution.json") is None


@pytest.mark.parametrize("include_output", [False, True])
def test_action_output_is_logged_only_when_configured(watch, store, caplog, include_output):
    watch.config.action.argv = [sys.executable, "-c", 'print("PRIVATE_ACTION_OUTPUT")']
    watch.config.logging.include_command_output = include_output
    attempt = stage(store, watch)
    with caplog.at_level("INFO", logger="app_watch.worker"):
        receipt = exec_worker(store, attempt, scope=watch.scope, provider=FakeProvider())
    assert receipt.outcome == "succeeded"
    assert ("PRIVATE_ACTION_OUTPUT" in caplog.text) is include_output
    assert b"PRIVATE_ACTION_OUTPUT" in (store.attempt_dir(attempt) / "log.txt").read_bytes()
