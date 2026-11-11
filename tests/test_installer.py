from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from app_watch import maintenance
from app_watch.errors import BlockedError, ConfigurationError, StateError
from app_watch.services import monitor as service
from app_watch.trigger.models import InstanceState
from app_watch.trigger.state import StateStore as StateStoreForTest
from scripts.install import Host, Installer, InstallError, Layout, acquire_install_lock
from tests.test_execution import request_for
from tests.test_execution import watch as watch
from tests.test_services_status import manager as manager
from tests.test_services_status import recovery_store, save_watch

ROOT = Path(__file__).resolve().parents[1]


class ControlledHost(Host):
    owner_uid = os.geteuid()

    def __init__(self, account, *, real_build=False):
        self.account = account
        self.real_build = real_build
        self.calls = []
        self.fail = None
        self.after_resume = None

    def check_platform(self):
        pass

    def provision(self):
        self.calls.append("provision")

    def verify_unit(self, content):
        self.calls.append("verify-unit")

    def accounts(self):
        return [self.account]

    def build(self, release):
        if self.real_build:
            return super().build(release)
        binary = release / "venv/bin/app-watch"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            f"#!{sys.executable}\nimport sys\n"
            f"sys.path.insert(0, {str(release / 'src/src')!r})\n"
            "from app_watch.cli import main\nraise SystemExit(main())\n"
        )
        binary.chmod(0o755)
        (binary.parent / "python").symlink_to(sys.executable)

    def user(self, release, account, request):
        self.calls.append((request["action"], release.name))
        if self.fail == request["action"]:
            self.fail = None
            raise InstallError("simulated user maintenance failure")
        output = io.StringIO()
        previous = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(dict(request, protocol_version=1)))
            with redirect_stdout(output):
                maintenance.main()
        except (BlockedError, ConfigurationError, StateError, OSError) as error:
            raise InstallError("account maintenance failed") from error
        finally:
            sys.stdin = previous
        if request["action"] == "resume" and self.after_resume:
            callback, self.after_resume = self.after_resume, None
            callback()
            raise InstallError("simulated failure after possible resume")
        return json.loads(output.getvalue())["plan"]


@pytest.fixture
def installation(tmp_path, monkeypatch, manager):
    # Adapt the controlled user-manager fixture for installer stop/worker discovery.
    original = manager.run

    def run(argv, **kwargs):
        args = argv[2:]
        import subprocess

        if args[0] == "list-jobs" or (
            args[0] == "list-units" and args[-1] == "app-watch-exec@*.service"
        ):
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[0] == "stop":
            manager.calls.append(tuple(args))
            manager.active.discard(args[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[0] == "cat" and args[-1] == "app-watch-exec@.service":
            return subprocess.CompletedProcess(argv, 0, f"# {layout.worker}\n", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(maintenance, "RealSystemdController", lambda: manager)
    monkeypatch.setattr(maintenance, "state_root", lambda scope: tmp_path / "states" / scope)
    account = {
        "name": "test-owner",
        "uid": os.geteuid(),
        "env": {
            "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
        },
    }
    host = ControlledHost(account)
    layout = Layout(tmp_path / "software", tmp_path / "bin/app-watch", tmp_path / "shared-units")
    monkeypatch.setattr(service, "command_path", lambda: layout.binary)
    return Installer(ROOT, layout, host), manager


def add_watch(installer, watch):
    save_watch(watch)
    service.manage_service(watch.source_path, "install", watch=watch)
    return service.service_name(watch.source_path)


def test_fresh_install_starts_no_watches_and_reinstall_update_uninstall(installation, watch):
    installer, manager = installation
    installer.execute("install")
    first = installer.layout.current.resolve()
    assert installer.layout.binary.is_symlink() and installer.layout.worker.exists()
    assert not manager.active
    unit = add_watch(installer, watch)
    store = recovery_store(watch)
    store.write_instance(InstanceState(instance_id=watch.config.job.id))
    installer.execute("update")
    second = installer.layout.current.resolve()
    assert second != first and first.exists()
    assert unit in manager.active and unit in manager.enabled
    assert service.read_service(unit).executable == installer.layout.binary
    installer.execute("install")
    assert installer.layout.current.resolve() != second
    installer.execute("uninstall")
    assert not installer.layout.binary.exists() and not installer.layout.worker.exists()
    assert not (service.units_root() / unit).exists()
    assert watch.source_path.exists() and store.root.exists()
    assert not first.exists()  # All software releases are removed on uninstall.


def test_dry_run_is_read_only_for_fresh_and_installed_tree(installation):
    installer, _ = installation
    installer.execute("install", dry_run=True)
    assert not installer.layout.prefix.exists()
    assert installer.host.calls == []
    installer.execute("install")
    before = installer.layout.manifest.read_bytes()
    selected = os.readlink(installer.layout.current)
    installer.execute("update", dry_run=True)
    assert installer.layout.manifest.read_bytes() == before
    assert os.readlink(installer.layout.current) == selected


@pytest.mark.parametrize("invalid", ["yaml", "pending", "orphan", "modified-unit"])
def test_preflight_rejects_invalid_or_protected_watches(installation, watch, invalid):
    installer, manager = installation
    installer.execute("install")
    unit = add_watch(installer, watch)
    selected = os.readlink(installer.layout.current)
    if invalid == "yaml":
        watch.source_path.write_text("bad: [")
    elif invalid == "modified-unit":
        path = service.units_root() / unit
        path.write_text(path.read_text() + "# edited\n")
    else:
        store = recovery_store(watch)
        if invalid == "pending":
            store.write_instance(
                InstanceState(instance_id=watch.config.job.id, pending_request=request_for(watch))
            )
        else:
            # No monitor unit owns this unresolved namespace.
            orphan = StateStoreForTest(store.root.parent / ("0" * 16))
            orphan.ensure()
            (orphan.root / "attempts" / "11111111-1111-4111-8111-111111111111").mkdir()
    with pytest.raises(InstallError):
        installer.execute("update")
    assert os.readlink(installer.layout.current) == selected
    assert unit in manager.active
    assert not installer.layout.journal.exists()


@pytest.mark.parametrize("resource", ["release", "command", "template"])
def test_modified_managed_files_are_preserved(installation, resource):
    installer, _ = installation
    installer.execute("install")
    if resource == "release":
        path = installer.layout.current.resolve() / "src/README.md"
    elif resource == "command":
        installer.layout.binary.unlink()
        path = installer.layout.binary
    else:
        path = installer.layout.worker
    path.write_text("operator change")
    with pytest.raises(InstallError):
        installer.execute("update")
    assert path.read_text() == "operator change"


def test_failure_before_and_after_activation_rolls_back(installation, watch):
    installer, manager = installation
    installer.execute("install")
    unit = add_watch(installer, watch)
    previous = os.readlink(installer.layout.current)
    previous_manifest = installer.layout.manifest.read_bytes()
    installer.host.fail = "refresh"
    with pytest.raises(InstallError):
        installer.execute("update")
    assert os.readlink(installer.layout.current) == previous
    assert installer.layout.manifest.read_bytes() == previous_manifest
    assert unit in manager.active and not installer.layout.journal.exists()
    installer.host.after_resume = lambda: None
    with pytest.raises(InstallError):
        installer.execute("update")
    assert os.readlink(installer.layout.current) == previous and unit in manager.active


def test_unsafe_rollback_preserves_candidate_and_recovers_on_next_run(installation, watch):
    installer, manager = installation
    installer.execute("install")
    unit = add_watch(installer, watch)
    previous = os.readlink(installer.layout.current)
    store = recovery_store(watch)

    def new_work():
        store.write_instance(
            InstanceState(instance_id=watch.config.job.id, pending_request=request_for(watch))
        )

    installer.host.after_resume = new_work
    with pytest.raises(InstallError):
        installer.execute("update")
    assert installer.layout.journal.exists()
    assert os.readlink(installer.layout.current) != previous
    with pytest.raises(InstallError):
        installer.execute("update", dry_run=True)
    # Operator review resolves the synthetic pending request in this disposable scope.
    store.write_instance(InstanceState(instance_id=watch.config.job.id))
    installer.execute("update")
    assert not installer.layout.journal.exists() and unit in manager.active


def test_purge_removes_owned_releases_but_never_user_data(installation, watch):
    installer, _ = installation
    installer.execute("install")
    add_watch(installer, watch)
    store = recovery_store(watch)
    store.write_instance(InstanceState(instance_id=watch.config.job.id))
    installer.execute("uninstall", purge=True)
    assert not installer.layout.manifest.exists()
    assert not list((installer.layout.prefix / "releases").iterdir())
    assert watch.source_path.exists() and store.root.exists()


@pytest.fixture(scope="session")
def wheelhouse(tmp_path_factory):
    root = tmp_path_factory.mktemp("app-watch-wheelhouse")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(ROOT / "requirements.lock"),
            "--dest",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return root


def test_real_locked_environment_and_standalone_installed_command(
    installation, wheelhouse, monkeypatch, tmp_path
):
    installer, _ = installation
    installer.host.real_build = True
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    installer.execute("install")
    import subprocess

    result = subprocess.run(
        [str(installer.layout.binary), "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "app-watch 0.1.0.dev0"
    metadata = json.loads((installer.layout.current / "deployment.json").read_text())
    assert metadata["layout"]["APP_WATCH_BIN_LINK"] == str(installer.layout.binary)
    installer.execute("uninstall", purge=True)


def test_interrupted_cleanup_can_resume_without_restoring_software(installation, monkeypatch):
    installer, _ = installation
    installer.execute("install")
    original = shutil.rmtree
    attempted = []

    def partial_delete(path, *args, **kwargs):
        if Path(path).parent == installer.layout.prefix / "releases" and not attempted:
            attempted.append(path)
            sample = Path(path) / "src/README.md"
            sample.unlink()
            raise OSError("simulated interruption during committed software deletion")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", partial_delete)
    with pytest.raises(OSError):
        installer.execute("uninstall")
    assert installer.layout.journal.exists()
    assert not installer.layout.binary.exists()
    installer.execute("uninstall")
    assert not installer.layout.journal.exists()
    assert not list((installer.layout.prefix / "releases").iterdir())
    assert json.loads(installer.layout.manifest.read_text())["active_release"] is None


def test_purge_after_default_uninstall_can_remove_the_receipt(installation):
    installer, _ = installation
    installer.execute("install")
    installer.execute("uninstall")
    assert installer.layout.manifest.exists()
    installer.execute("uninstall", purge=True)
    assert not installer.layout.manifest.exists()


def test_modified_resources_after_interruption_are_not_overwritten(installation, watch):
    installer, _ = installation
    installer.execute("install")
    add_watch(installer, watch)
    store = recovery_store(watch)

    def protect_new_work():
        store.write_instance(
            InstanceState(instance_id=watch.config.job.id, pending_request=request_for(watch))
        )

    installer.host.after_resume = protect_new_work
    with pytest.raises(InstallError):
        installer.execute("update")
    store.write_instance(InstanceState(instance_id=watch.config.job.id))
    installer.layout.worker.write_text("operator change after interruption")
    with pytest.raises(InstallError, match="resources changed"):
        installer.execute("update")
    assert installer.layout.worker.read_text() == "operator change after interruption"
    assert installer.layout.journal.exists()


def test_managed_root_cannot_hide_an_untrusted_ancestor(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts.install import safe_directory

    parent = tmp_path / "untrusted"
    child = parent / "owned-root"
    child.mkdir(parents=True)
    original = Path.lstat

    def metadata(path):
        if path == parent:
            actual = original(path)
            return SimpleNamespace(st_mode=actual.st_mode, st_uid=os.geteuid() + 1)
        return original(path)

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(InstallError, match="not safely owned"):
        safe_directory(child, os.geteuid())


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/home/test/config", "/home/test/config"),
        ("$'/home/test/config\\040with\\040spaces'", "/home/test/config with spaces"),
        ('""', ""),
    ],
)
def test_manager_environment_decoding_is_literal(value, expected):
    assert Host.environment_value(value) == expected


def test_user_maintenance_launch_is_isolated_and_never_runs_as_root():
    argv = Host.user_command(
        "owner",
        {"HOME": "/home/owner", "XDG_CONFIG_HOME": "/home/owner/cfg"},
        ["/opt/app-watch/current/venv/bin/python", "-I", "-m", "app_watch.maintenance"],
    )
    assert argv[:5] == ["runuser", "-u", "owner", "--", "env"]
    assert "-i" in argv and "-I" in argv
    assert "HOME=/home/owner" in argv
    assert "XDG_CONFIG_HOME=/home/owner/cfg" in argv


def test_installer_waits_for_runtime_guard_and_reports_persistent_contention(tmp_path):
    lock = tmp_path / "install.lock"
    lock.touch()
    code = (
        "import fcntl,sys,time; f=open(sys.argv[1]); "
        "fcntl.lockf(f, fcntl.LOCK_SH); print('ready',flush=True); time.sleep(.5)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True
    ) as holder:
        assert holder.stdout.readline().strip() == "ready"
        with lock.open("r+") as stream:
            with pytest.raises(InstallError, match="maintenance is busy"):
                acquire_install_lock(stream.fileno(), timeout=0)
            acquire_install_lock(stream.fileno(), timeout=5)
        holder.wait(timeout=5)
