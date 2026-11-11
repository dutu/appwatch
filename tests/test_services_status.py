from __future__ import annotations

import json
import subprocess

import pytest

from app_watch.cli import main
from app_watch.cli.status import inventory, watch_status
from app_watch.config import prepare_config
from app_watch.errors import BlockedError, ConfigurationError
from app_watch.process import Observation, boot_id
from app_watch.services import monitor as service
from app_watch.trigger.locking import exclusive_lock, monitor_lock, monitor_started_at
from app_watch.trigger.models import InstanceState, ObservationRecord, utc_now
from app_watch.trigger.paths import monitor_lock_path
from app_watch.trigger.state import StateStore
from tests.test_execution import context, observation, request_for, stage
from tests.test_execution import watch as watch


class UserManager:
    def __init__(self):
        self.calls = []
        self.active = set()
        self.enabled = set()
        self.fail_reload = False
        self.fail_enable = False
        self.unavailable = False
        self.dropins = ""
        self.reload_needed = "no"

    def manager_reachable(self):
        return not self.unavailable

    def template_ready(self):
        return True

    def run(self, argv, **kwargs):
        assert argv[:2] == ["systemctl", "--user"]
        args = argv[2:]
        self.calls.append(tuple(args))
        if self.unavailable:
            raise OSError("manager unavailable")
        operation = args[0]
        unit = args[-1]
        output, error, code = "", "", 0
        if operation in ("list-units", "list-unit-files"):
            output = "".join(
                f"{path.name} enabled\n" for path in service.units_root().glob("*.service")
            )
        elif operation == "show":
            loaded = (service.units_root() / unit).exists()
            values = {
                "LoadState": "loaded" if loaded else "not-found",
                "ActiveState": "active" if unit in self.active else "inactive",
                "SubState": "running" if unit in self.active else "dead",
                "Job": "",
                "UnitFileState": "enabled" if unit in self.enabled else "disabled",
                "FragmentPath": str(service.units_root() / unit) if loaded else "",
                "DropInPaths": self.dropins,
                "NeedDaemonReload": self.reload_needed,
                "ExecMainStatus": "0",
            }
            output = "".join(f"{key}={value}\n" for key, value in values.items())
        elif operation == "disable":
            self.enabled.discard(unit)
            if "--now" in args:
                self.active.discard(unit)
        elif operation == "enable":
            self.enabled.add(unit)
            if "--now" in args:
                self.active.add(unit)
            if self.fail_enable:
                self.fail_enable = False
                code, error = 1, "enable failed after possible start"
        elif operation == "start":
            self.active.add(unit)
        elif operation == "daemon-reload":
            if self.fail_reload:
                self.fail_reload = False
                code, error = 1, "reload failed"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(argv, code, output, error)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    manager = UserManager()
    monkeypatch.setattr(service, "_run", manager.run)
    monkeypatch.setattr(service, "RealSystemdController", lambda: manager)
    monkeypatch.setattr(
        "app_watch.trigger.inspect.state_root", lambda scope: tmp_path / "states" / scope
    )
    return manager


def recovery_store(watch):
    from app_watch.trigger.inspect import state_root

    store = StateStore(state_root(watch.scope))
    store.ensure()
    return store


def install(watch):
    service.manage_service(watch.source_path, "install", watch=watch)
    return service.service_name(watch.source_path)


def test_install_update_disable_remove_and_missing_yaml(watch, manager):
    unit = install(watch)
    installed = service.read_service(unit)
    assert installed.identity.scope == watch.scope
    assert unit in manager.active and unit in manager.enabled
    assert "--expected-identity " + watch.scope in installed.content
    watch.config.job.id = "changed-id"
    newer = prepare_config(watch.config, watch.source_path)
    service.manage_service(watch.source_path, "update", watch=newer)
    assert service.read_service(unit).identity.scope == newer.scope
    # The YAML never existed: recovery operations use the installed identity.
    service.manage_service(watch.source_path, "disable")
    assert unit not in manager.active and unit not in manager.enabled
    assert (service.units_root() / unit).exists()
    service.manage_service(watch.source_path, "remove")
    assert not (service.units_root() / unit).exists()


def test_existing_install_and_missing_update_are_explicit_errors(watch, manager):
    with pytest.raises(ConfigurationError, match="not installed"):
        service.manage_service(watch.source_path, "update", watch=watch)
    install(watch)
    with pytest.raises(ConfigurationError, match="already installed"):
        install(watch)


@pytest.mark.parametrize("work", ["pending", "active", "blocked", "orphan"])
def test_service_changes_refuse_protected_recovery(watch, manager, work):
    install(watch)
    store = recovery_store(watch)
    if work == "pending":
        store.write_instance(
            InstanceState(instance_id=watch.config.job.id, pending_request=request_for(watch))
        )
    elif work in ("active", "orphan"):
        stage(store, watch)
        if work == "orphan":
            store.instance_path(watch.config.job.id).unlink()
    else:
        store.block(watch.config.job.id, "unknown outcome")
    count = len(manager.calls)
    with pytest.raises(BlockedError, match="Active, pending"):
        service.manage_service(watch.source_path, "disable")
    assert not any(call[0] == "disable" for call in manager.calls[count:])


def test_new_scope_is_checked_before_stopping_old_monitor(watch, manager):
    unit = install(watch)
    old_scope = watch.scope
    watch.config.job.id = "new"
    newer = prepare_config(watch.config, watch.source_path)
    store = recovery_store(newer)
    store.block(newer.config.job.id, "new identity already has unresolved work")
    with pytest.raises(BlockedError):
        service.manage_service(watch.source_path, "update", watch=newer)
    assert unit in manager.active
    assert service.read_service(unit).identity.scope == old_scope


@pytest.mark.parametrize("failure", ["reload", "enable"])
def test_failed_update_restores_selection_and_activation(watch, manager, failure):
    unit = install(watch)
    original = service.read_service(unit)
    watch.config.job.id = "new"
    newer = prepare_config(watch.config, watch.source_path)
    manager.fail_reload = failure == "reload"
    manager.fail_enable = failure == "enable"
    with pytest.raises(BlockedError):
        service.manage_service(watch.source_path, "update", watch=newer)
    assert service.read_service(unit).content == original.content
    assert unit in manager.enabled and unit in manager.active


def test_failed_new_install_leaves_no_managed_unit(watch, manager):
    manager.fail_enable = True
    with pytest.raises(BlockedError):
        install(watch)
    assert not list(service.units_root().glob("*.service"))
    assert not manager.active and not manager.enabled


@pytest.mark.parametrize("kind", ["modified", "dropin", "symlink", "reload", "effective-dropin"])
def test_unverified_units_are_never_overwritten(watch, manager, kind):
    unit = install(watch)
    path = service.units_root() / unit
    if kind == "modified":
        path.write_text(path.read_text() + "# custom change\n")
    elif kind == "dropin":
        path.with_name(unit + ".d").mkdir()
    elif kind == "symlink":
        original = path.read_text()
        path.unlink()
        other = path.parent / "other"
        other.write_text(original)
        path.symlink_to(other)
    elif kind == "reload":
        manager.reload_needed = "yes"
    else:
        manager.dropins = "/other/override.conf"
    before = path.read_text()
    with pytest.raises((ConfigurationError, BlockedError)):
        service.manage_service(watch.source_path, "update", watch=watch)
    assert path.read_text() == before


def test_lifecycle_and_foreground_monitor_locks_prevent_races(watch, manager):
    with (
        exclusive_lock(monitor_lock_path(watch.source_path).with_name("service.lock")),
        pytest.raises(BlockedError),
    ):
        install(watch)
    with exclusive_lock(monitor_lock_path(watch.source_path)), pytest.raises(BlockedError):
        install(watch)
    assert not list(service.units_root().glob("*.service"))


def test_foreground_cannot_adopt_different_installed_identity(watch, manager):
    install(watch)
    watch.config.job.id = "different"
    changed = prepare_config(watch.config, watch.source_path)
    with pytest.raises(BlockedError, match="Installed execution identity differs"):
        service.require_monitor_identity(changed)


def test_unit_quoting_and_selection_handles_special_paths(watch, manager, tmp_path):
    special = tmp_path / 'wallet $HOME %i "quoted".yaml'
    changed = prepare_config(watch.config, special)
    unit = install(changed)
    content = service.read_service(unit).content
    assert "$$HOME" in content and "%%i" in content and '\\"quoted\\"' in content
    assert service.find_service(special).identity.source_path == str(special)


def save_watch(watch):
    watch.source_path.write_text(watch.config.model_dump_json())


def observed_state(watch, store, *, timestamp=None):
    state = InstanceState(
        instance_id=watch.config.job.id,
        boot_id=boot_id(),
        config_fingerprint=watch.fingerprint(),
        last_observation=ObservationRecord(
            observation=Observation.RUNNING,
            observed_at=timestamp or utc_now(),
            boot_id=boot_id(),
        ),
    )
    store.write_instance(state)
    return state


def test_status_requires_fresh_observation_and_lock(watch, manager):
    install(watch)
    save_watch(watch)
    store = recovery_store(watch)
    observed_state(watch, store)
    result = watch_status(watch.source_path)
    assert result["monitor"] == "UNKNOWN" and result["application"] == "UNKNOWN"
    with exclusive_lock(monitor_lock_path(watch.source_path)):
        result = watch_status(watch.source_path)
        assert result["application"] == "UNKNOWN"  # A maintenance lock is not a session.
    with monitor_lock(monitor_lock_path(watch.source_path)):
        result = watch_status(watch.source_path)
        assert result["application"] == "UNKNOWN"  # Fresh, but from before session startup.
        observed_state(watch, store)
        result = watch_status(watch.source_path)
        assert result["monitor"] == "RUNNING" and result["application"] == "RUNNING"
        observed_state(watch, store, timestamp="2020-01-01T00:00:00Z")
        result = watch_status(watch.source_path)
        assert result["monitor"] == "UNKNOWN" and result["application"] == "UNKNOWN"


def test_stopped_status_requires_current_session_shutdown_confirmation(watch, manager):
    from app_watch.trigger.lifecycle import progress

    install(watch)
    save_watch(watch)
    store = recovery_store(watch)
    path = monitor_lock_path(watch.source_path)
    with monitor_lock(path):
        state = observed_state(watch, store)
        state.last_observation.observation = Observation.STOPPED
        store.write_instance(state)
        assert watch_status(watch.source_path)["application"] == "NOT RUNNING"
        progress(state, watch, observation(Observation.RUNNING), context(watch), monotonic_now=0)
        progress(state, watch, observation(Observation.STOPPED), context(watch), monotonic_now=1)
        store.write_instance(state)
        assert watch_status(watch.source_path)["application"] == "CONFIRMING STOP"
        progress(state, watch, observation(Observation.STOPPED), context(watch), monotonic_now=6)
        store.write_instance(state)
        assert watch_status(watch.source_path)["application"] == "HAS STOPPED"
        # Confirmation remains visible when its frozen request moves to an attempt/history.
        attempt = stage(store, watch, state.pending_request)
        state.pending_request = None
        state.active_attempt_id = attempt
        state.active_request_id = state.latest_request_id
        store.write_instance(state)
        assert watch_status(watch.source_path)["application"] == "HAS STOPPED"
        state.active_attempt_id = state.active_request_id = None
        state.completed_attempts = [attempt]
        store.write_instance(state)
        assert watch_status(watch.source_path)["application"] == "HAS STOPPED"
    assert monitor_started_at(path) is None
    with monitor_lock(path):
        # Both recent history and a held lock survive a quick monitor restart.
        assert watch_status(watch.source_path)["application"] == "UNKNOWN"
        state.last_observation.observed_at = utc_now()
        store.write_instance(state)
        assert watch_status(watch.source_path)["application"] == "NOT RUNNING"


def test_status_refuses_session_change_during_recovery_read(watch, manager, monkeypatch):
    from app_watch.cli import status

    install(watch)
    save_watch(watch)
    store = recovery_store(watch)
    path = monitor_lock_path(watch.source_path)
    original = status.inspect_recovery

    def restarted(identity):
        recovery = original(identity)
        path.write_text(utc_now())
        return recovery

    with monitor_lock(path):
        observed_state(watch, store)
        monkeypatch.setattr(status, "inspect_recovery", restarted)
        assert watch_status(watch.source_path)["application"] == "UNKNOWN"


@pytest.mark.parametrize("marker", ["", "broken", "2026-01-01T00:00:00", "\u00ff"])
def test_status_refuses_missing_or_invalid_session_marker(watch, manager, marker):
    install(watch)
    save_watch(watch)
    store = recovery_store(watch)
    path = monitor_lock_path(watch.source_path)
    with exclusive_lock(path):
        path.write_text(marker)
        observed_state(watch, store)
        before = path.read_bytes()
        assert watch_status(watch.source_path)["application"] == "UNKNOWN"
        assert path.read_bytes() == before


def test_status_recovers_missing_yaml_and_active_attempt(watch, manager):
    install(watch)
    store = recovery_store(watch)
    attempt = stage(store, watch)
    result = watch_status(watch.source_path)
    assert result["configuration"] == "INVALID"
    assert result["job_id"] == watch.config.job.id
    assert result["active_attempt_id"] == attempt and result["dispatch"] == "ACTIVE"


def test_status_identity_change_keeps_original_recovery_visible(watch, manager):
    install(watch)
    store = recovery_store(watch)
    attempt = stage(store, watch)
    watch.config.job.id = "changed"
    save_watch(watch)
    result = watch_status(watch.source_path)
    assert result["active_attempt_id"] == attempt and result["job_id"] == "bisq-test"
    assert any("identity differs" in message for message in result["diagnostics"])


def test_inventory_keeps_invalid_missing_and_healthy_entries(watch, manager):
    install(watch)
    jobs = service.jobs_root()
    jobs.mkdir(parents=True)
    (jobs / "healthy.yaml").write_text(watch.config.model_dump_json())
    (jobs / "invalid.yml").write_text("broken: [")
    rows = inventory()
    assert len(rows) == 3
    assert sum(row["configuration"] == "INVALID" for row in rows) == 2
    assert sum(row["configuration"] == "VALID" for row in rows) == 1


def test_offline_inventory_still_lists_local_watches(watch, manager):
    save_watch(watch)
    jobs = service.jobs_root()
    jobs.mkdir(parents=True)
    (jobs / "healthy.yaml").write_text(watch.source_path.read_text())
    manager.unavailable = True
    rows = inventory()
    assert any(row.get("configuration") == "VALID" for row in rows)
    assert any(row["config"] == "(systemd discovery)" for row in rows)


def test_status_json_and_cli_disable_do_not_require_valid_yaml(watch, manager, capsys):
    install(watch)
    assert main(["--config", str(watch.source_path), "status", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configuration"] == "INVALID" and "details" not in result
    assert main(["--config", str(watch.source_path), "disable"]) == 0
    assert "DISABLED" in capsys.readouterr().out
    assert main(["--config", str(watch.source_path), "remove"]) == 0
    assert "REMOVED" in capsys.readouterr().out


def test_enabled_validation_reports_setup_incomplete_then_health(watch, manager, capsys):
    save_watch(watch)
    assert main(["--config", str(watch.source_path), "validate"]) == 2
    assert "SETUP INCOMPLETE" in capsys.readouterr().out
    install(watch)
    store = recovery_store(watch)
    with monitor_lock(monitor_lock_path(watch.source_path)):
        observed_state(watch, store)
        assert main(["--config", str(watch.source_path), "validate"]) == 0
    assert "Result: OK" in capsys.readouterr().out


def test_status_and_listing_do_not_create_recovery_or_runtime_state(watch, manager, tmp_path):
    save_watch(watch)
    result = watch_status(watch.source_path)
    assert result["configuration"] == "VALID"
    assert not (tmp_path / "states").exists()
    assert not (tmp_path / "runtime/app-watch").exists()


def test_inventory_isolates_symlink_loops(watch, manager):
    jobs = service.jobs_root()
    jobs.mkdir(parents=True)
    (jobs / "healthy.yaml").write_text(watch.config.model_dump_json())
    loop = jobs / "loop.yaml"
    loop.symlink_to(loop)
    rows = inventory()
    assert len(rows) == 2
    assert sum(row["configuration"] == "VALID" for row in rows) == 1
    assert sum(row["configuration"] == "INVALID" for row in rows) == 1
