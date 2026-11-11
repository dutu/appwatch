from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import replace

import psutil
import pytest

from app_watch.actions.worker import exec_worker
from app_watch.apps.monero import _qt_string
from app_watch.config import prepare_config, validate_document
from app_watch.config.models import EntrypointConfig
from app_watch.errors import ConfigurationError
from app_watch.process import (
    Entrypoint,
    MatcherSpec,
    Observation,
    ProcessInfo,
    PsutilProcessProvider,
    detect_spec,
    match_identity,
    matcher_from_canonical,
)
from app_watch.trigger.models import InstanceState
from app_watch.trigger.monitor import Monitor
from app_watch.trigger.state import StateStore
from tests.helpers import FakeProvider
from tests.test_execution import Manager, stage

GUI = "monero-wallet-gui"
RPC = "monero-wallet-rpc"
MATCHER = MatcherSpec(entrypoints=tuple(Entrypoint("executable_name", name) for name in (GUI, RPC)))
TARGET = "/wallets/target"
SETTINGS = "/home/owner/.config/monero-project/monero-core.conf"


def process(name=GUI, pid=100, args=()):
    return ProcessInfo(pid, 1.0, f"/opt/version/{name}", (name, *args))


class Provider(FakeProvider):
    def __init__(self, processes=None):
        super().__init__(processes, env={100: {"HOME": "/home/owner"}})
        self.files = {SETTINGS: (f"[General]\nwallet_path={TARGET}/wallet\n", "resolved")}
        self.cwd = "/work"

    def resolve_directory(self, pid, value):
        return os.path.realpath(os.path.join(self.cwd, value)) if self.cwd else None

    def read_text_file(self, pid, path):
        return self.files.get(path, (None, "missing"))


def detect(provider, data_dir=TARGET):
    return detect_spec(app="monero", data_dir=data_dir, matcher=MATCHER, provider=provider)


def document(directory):
    return {
        "config_version": 1,
        "job": {"id": "monero-test"},
        "application": {
            "name": "monero",
            "data_dir": str(directory),
            "matcher": MATCHER.canonical(),
        },
        "action": {"argv": [sys.executable, "-c", "raise SystemExit(0)"]},
        "trigger": {"enabled": True, "confirm_seconds": 5},
    }


@pytest.mark.parametrize("name", [GUI, RPC])
@pytest.mark.parametrize("prefix", ["/opt/old/", "/opt/new/", "/another/"])
def test_executable_basename_ignores_version_and_argv(name, prefix):
    proc = replace(process(name), exe_realpath=prefix + name, cmdline=("unrelated",))
    assert match_identity(MATCHER, proc).matched
    assert match_identity(MATCHER, replace(proc, cmdline_available=False)).matched
    assert not match_identity(MATCHER, replace(proc, exe_realpath="/opt/other")).matched


@pytest.mark.parametrize("name", [GUI, RPC])
def test_deleted_executable_is_uncertain_not_absent(name):
    provider = Provider([process(name + " (deleted)")])
    assert match_identity(MATCHER, provider.processes[0]).unavailable
    assert detect(provider).observation == Observation.INSPECTION_UNAVAILABLE
    provider.processes = [process("other (deleted)", args=(GUI, RPC))]
    assert detect(provider).observation == Observation.STOPPED


def test_literal_names_survive_preparation_and_frozen_matcher(tmp_path):
    watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
    assert watch.matcher == MATCHER
    assert matcher_from_canonical(watch.matcher.canonical()) == MATCHER


def test_replacement_symlink_does_not_redefine_reported_executable(tmp_path):
    reported = tmp_path / GUI
    reported.symlink_to(tmp_path / "different-name")
    proc = replace(process(), exe_realpath=str(reported))
    assert match_identity(MATCHER, proc).matched
    assert match_identity(
        MATCHER, replace(proc, exe_realpath=str(reported) + " (deleted)")
    ).unavailable


def test_running_target_still_blocks_with_an_unavailable_second_identity():
    provider = Provider([process(), replace(process(RPC, pid=101), exe_realpath=None)])
    assert detect(provider).observation == Observation.RUNNING


def test_pid_change_during_settings_read_is_uncertain():
    provider = Provider([process()])
    read = provider.read_text_file

    def replaced(pid, path):
        provider.alive_result = False
        return read(pid, path)

    provider.read_text_file = replaced
    assert detect(provider).observation == Observation.INSPECTION_UNAVAILABLE


def test_unreadable_cwd_prevents_falling_back_to_default_settings():
    provider = Provider([process()])
    provider.cwd = None
    assert detect(provider).observation == Observation.INSPECTION_UNAVAILABLE


def test_wallet_file_symlink_is_resolved_before_parent_comparison(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target / "wallet")
    provider = Provider([process(RPC, args=(f"--wallet-file={alias}",))])
    assert detect(provider, str(target)).observation == Observation.RUNNING


def test_stop_without_activation_does_not_dispatch(tmp_path):
    watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
    provider = Provider([process(RPC, args=("--wallet-file=/other/wallet",))])
    manager = Manager()
    monitor = Monitor(watch, StateStore(tmp_path / "state"), provider, manager)
    monitor.tick(monotonic_now=0)
    provider.processes.clear()
    monitor.tick(monotonic_now=1)
    monitor.tick(monotonic_now=6)
    assert not monitor.state.armed and not manager.starts


def test_worker_needs_no_settings_files_after_all_processes_exit(tmp_path):
    watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
    store = StateStore(tmp_path / "state")
    store.ensure()
    attempt = stage(store, watch)
    provider = Provider()
    provider.files.clear()
    assert exec_worker(store, attempt, scope=watch.scope, provider=provider).outcome == "succeeded"


@pytest.mark.parametrize(
    "value", ["/opt/gui", "~/gui", "..", ".", "gui*", "gui?", "{NAME}", "gui/name"]
)
def test_executable_names_reject_paths_and_patterns(value):
    with pytest.raises(ValueError):
        EntrypointConfig(kind="executable_name", value=value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("argv_tokens", ["--wallet-file"]),
        ("exe_realpath", "/opt/gui"),
        ("cmdline_regex", "wallet"),
        ("env_eq", {"HOME": "/home/owner"}),
    ],
)
def test_monero_rejects_narrowing_guard(tmp_path, key, value):
    raw = document(tmp_path)
    raw["application"]["matcher"][key] = value
    with pytest.raises(ConfigurationError):
        validate_document(raw)


def test_monero_requires_both_roles(tmp_path):
    raw = document(tmp_path)
    raw["application"]["matcher"]["entrypoints"].pop()
    with pytest.raises(ConfigurationError):
        validate_document(raw)


def test_gui_wallet_switch_and_shared_guard():
    provider = Provider([process()])
    assert detect(provider).observation == Observation.RUNNING
    provider.files[SETTINGS] = ("[General]\nwallet_path=/other/wallet\n", "resolved")
    assert detect(provider).observation == Observation.AMBIGUOUS
    provider.processes.append(process(RPC, pid=101))
    provider.processes.pop(0)
    assert detect(provider).observation == Observation.AMBIGUOUS
    provider.processes.clear()
    assert detect(provider).observation == Observation.STOPPED


def test_gui_xdg_and_portable_precedence_and_mode_change():
    provider = Provider([process()])
    provider.env[100]["XDG_CONFIG_HOME"] = "/xdg"
    provider.files["/xdg/monero-project/monero-core.conf"] = (f"wallet_path={TARGET}/w", "resolved")
    assert detect(provider).observation == Observation.RUNNING
    portable = "/work/monero-storage/settings.ini"
    provider.files[portable] = ("[General]\nwallet_path=/other/w", "resolved")
    assert detect(provider).observation == Observation.AMBIGUOUS
    provider.files[portable] = (None, "inaccessible")
    assert detect(provider).observation == Observation.INSPECTION_UNAVAILABLE
    del provider.files[portable]
    assert detect(provider).observation == Observation.RUNNING


@pytest.mark.parametrize("env", [{}, {"HOME": "relative"}, {"XDG_CONFIG_HOME": "relative"}])
def test_gui_does_not_use_monitor_environment(env):
    provider = Provider([process()])
    provider.env[100] = env
    assert detect(provider).observation != Observation.STOPPED
    assert detect(provider).observation != Observation.RUNNING


@pytest.mark.parametrize(
    "text",
    [
        "wallet_path=",
        "wallet_path=/a/w\nwallet_path=/b/w",
        "[Other]\nwallet_path=/a/w",
        'wallet_path="unterminated',
        "wallet_path=@Variant(garbage)",
        "wallet_path=/a,/b",
        "wallet_path=/bad\\q",
        "invalid line",
        "wallet_path=/bad\\x",
    ],
)
def test_gui_invalid_settings_block_without_arming(text):
    provider = Provider([process()])
    provider.files[SETTINGS] = (text, "resolved")
    assert detect(provider).observation == Observation.AMBIGUOUS


@pytest.mark.parametrize(
    "encoded,expected",
    [
        ('"/wallets/with,comma/w"', "/wallets/with,comma/w"),
        (r'"/wallets/a\\b/w"', "/wallets/a\\b/w"),
        (r"/wallets/\x00e9/w", "/wallets/é/w"),
        (r"/wallets/\xd83d\xde00/wallet", "/wallets/😀/wallet"),
        (
            r"/wallets with spaces \xe9 \xd83d\xde00/second wallet \xe9 \xd83d\xde00.keys",
            "/wallets with spaces é 😀/second wallet é 😀.keys",
        ),
        ("/wallets/with spaces/w", "/wallets/with spaces/w"),
    ],
)
def test_qt_path_scalars(encoded, expected):
    assert _qt_string(encoded) == expected


@pytest.mark.parametrize(
    "encoded",
    [
        r"/wallets/\xd83d/wallet",
        r"/wallets/\xde00/wallet",
        r"/wallets/\xde00\xd83d/wallet",
        r"/wallets/\xd83d\x0041/wallet",
    ],
)
def test_qt_path_rejects_unpaired_surrogates(encoded):
    with pytest.raises(ValueError):
        _qt_string(encoded)


@pytest.mark.parametrize("spoof", [f"--wallet-file={TARGET}/wallet", "--config-file=/spoof.conf"])
@pytest.mark.parametrize(
    "real_args,expected",
    [
        ((), Observation.AMBIGUOUS),
        (("--wallet-file=/other/wallet",), Observation.AMBIGUOUS),
        ((f"--wallet-file={TARGET}/wallet",), Observation.RUNNING),
        (("--config-file=/real.conf",), Observation.AMBIGUOUS),
    ],
)
def test_rpc_never_parses_argv_zero_as_selection(spoof, real_args, expected):
    provider = Provider([replace(process(RPC), cmdline=(spoof, *real_args))])
    provider.files["/spoof.conf"] = (f"wallet-file={TARGET}/wallet", "resolved")
    provider.files["/real.conf"] = ("wallet-file=/other/wallet", "resolved")
    assert detect(provider).observation == expected


def test_non_bmp_gui_wallet_path_activates_watch():
    provider = Provider([process()])
    provider.files[SETTINGS] = (r"wallet_path=/wallets/\xd83d\xde00/wallet", "resolved")
    assert detect(provider, "/wallets/😀").observation == Observation.RUNNING


@pytest.mark.parametrize("equals", [False, True])
def test_rpc_explicit_config_and_wallet(equals):
    args = ("--config-file=/rpc.conf",) if equals else ("--config-file", "/rpc.conf")
    provider = Provider([process(RPC, args=args)])
    provider.files["/rpc.conf"] = (f"rpc-login=secret\nwallet-file={TARGET}/wallet", "resolved")
    assert detect(provider).observation == Observation.RUNNING
    provider.processes = [process(RPC, args=(*args, "--wallet-file", TARGET + "/wallet"))]
    assert detect(provider).observation == Observation.RUNNING
    provider.processes = [process(RPC, args=(*args, "--wallet-file=/other/wallet"))]
    assert detect(provider).observation == Observation.AMBIGUOUS


@pytest.mark.parametrize(
    "args,text",
    [
        (("--wallet-dir=/wallets",), ""),
        (("--generate-from-json=/a",), ""),
        (("--wallet-file",), ""),
        (("--wallet-file=/a", "--wallet-file=/b"), ""),
        (("--config-file=/rpc",), "wallet-file=/a\nwallet-file=/b"),
        (("--config-file=/rpc",), "wallet-dir=/wallets"),
        (("--config-file=/rpc",), "wallet-file="),
    ],
)
def test_rpc_unsupported_or_conflicting_selection(args, text):
    provider = Provider([process(RPC, args=args)])
    provider.files["/rpc"] = (text, "resolved")
    assert detect(provider).observation == Observation.AMBIGUOUS


def test_relative_paths_use_process_cwd_and_match_exact_parent():
    provider = Provider([process(RPC, args=("--config-file=rpc.conf",))])
    provider.cwd = "/wallets"
    provider.files["/wallets/rpc.conf"] = ("wallet-file=target/wallet", "resolved")
    assert detect(provider).observation == Observation.RUNNING
    provider.files["/wallets/rpc.conf"] = ("wallet-file=target/nested/wallet", "resolved")
    assert detect(provider).observation == Observation.AMBIGUOUS


def test_unknown_identity_and_missing_settings_cannot_prove_stop():
    provider = Provider([replace(process(), exe_realpath=None)])
    assert detect(provider).observation == Observation.INSPECTION_UNAVAILABLE
    provider.processes = [process()]
    provider.files.clear()
    assert detect(provider).observation == Observation.AMBIGUOUS
    provider.env_readable = False
    assert detect(provider).observation == Observation.INSPECTION_UNAVAILABLE


def test_monitor_latches_wallet_visit_until_all_processes_exit(tmp_path):
    watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
    provider = Provider([process()])
    store = StateStore(tmp_path / "state")
    manager = Manager()
    monitor = Monitor(watch, store, provider, manager)
    provider.files[SETTINGS] = ("wallet_path=/other/wallet", "resolved")
    monitor.tick(monotonic_now=0)
    assert not monitor.state.armed
    provider.files[SETTINGS] = (f"wallet_path={tmp_path}/wallet", "resolved")
    monitor.tick(monotonic_now=1)
    assert monitor.state.armed
    provider.files[SETTINGS] = ("wallet_path=/other/wallet", "resolved")
    monitor.tick(monotonic_now=2)
    assert monitor.state.armed and not manager.starts
    monitor = Monitor(watch, store, provider, manager)
    monitor.tick(monotonic_now=3)
    assert monitor.state.armed
    provider.processes = [process(RPC, args=("--wallet-file=/other/wallet",))]
    monitor.tick(monotonic_now=4)
    assert monitor.state.candidate_since is None
    provider.processes.clear()
    monitor.tick(monotonic_now=5)
    provider.processes = [process(GUI + " (deleted)")]
    monitor.tick(monotonic_now=10)
    assert monitor.state.armed and monitor.state.candidate_since is None
    provider.processes.clear()
    monitor.tick(monotonic_now=11)
    monitor.tick(monotonic_now=16)
    assert len(manager.starts) == 1


@pytest.mark.parametrize("name", [GUI, RPC, GUI + " (deleted)", RPC + " (deleted)"])
def test_worker_blocks_any_wallet_or_deleted_process(tmp_path, name):
    watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
    store = StateStore(tmp_path / "state")
    store.ensure()
    attempt = stage(store, watch)
    provider = Provider([process(name)])
    provider.files.clear()
    receipt = exec_worker(store, attempt, scope=watch.scope, provider=provider)
    assert receipt.outcome == "blocked"


@pytest.mark.parametrize("failure", ["environment", "settings"])
def test_worker_blocks_unavailable_monero_settings(tmp_path, failure):
    watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
    store = StateStore(tmp_path / "state")
    store.ensure()
    attempt = stage(store, watch)
    provider = Provider([process()])
    if failure == "environment":
        provider.env_readable = False
    else:
        provider.files[SETTINGS] = (None, "inaccessible")
    assert detect(provider, str(tmp_path)).observation == Observation.INSPECTION_UNAVAILABLE
    receipt = exec_worker(store, attempt, scope=watch.scope, provider=provider)
    assert receipt.outcome == "blocked"
    assert "inspection_unavailable" in receipt.reason


def test_pending_request_requires_reconfirmation_after_other_wallet(tmp_path):
    watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
    provider = Provider()
    from app_watch.trigger.lifecycle import progress
    from tests.test_execution import context, request_for

    state = InstanceState(instance_id=watch.config.job.id, pending_request=request_for(watch))
    provider.processes = [process(RPC, args=("--wallet-file=/other/wallet",))]
    progress(state, watch, detect(provider, str(tmp_path)), context(watch), monotonic_now=6)
    assert state.pending_request and state.pending_reconfirmation_required
    provider.processes.clear()
    progress(state, watch, detect(provider, str(tmp_path)), context(watch), monotonic_now=7)
    assert state.pending_reconfirmation_required
    progress(state, watch, detect(provider, str(tmp_path)), context(watch), monotonic_now=12)
    assert not state.pending_reconfirmation_required


@pytest.mark.parametrize("name", [GUI, RPC])
def test_live_unlinked_native_executable_remains_a_guard(tmp_path, name, monkeypatch):
    binary = tmp_path / name
    shutil.copyfile("/bin/sleep", binary)
    binary.chmod(0o700)
    home = tmp_path / "home"
    config = home / ".config/monero-project/monero-core.conf"
    config.parent.mkdir(parents=True)
    config.write_text(f"[General]\nwallet_path={tmp_path}/wallet\n")
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    provider = PsutilProcessProvider()
    with subprocess.Popen([str(binary), "30"], cwd=tmp_path, env=env) as child:
        try:
            iterate = psutil.process_iter
            # Isolate this real process from unrelated unreadable VM session processes.
            monkeypatch.setattr(
                psutil,
                "process_iter",
                lambda attrs: (proc for proc in iterate(attrs) if proc.pid == child.pid),
            )
            binary.unlink()
            raw = os.readlink(f"/proc/{child.pid}/exe")
            assert raw.endswith(" (deleted)")
            assert detect(provider, str(tmp_path)).observation != Observation.STOPPED
            watch = prepare_config(validate_document(document(tmp_path)), tmp_path / "watch.yaml")
            store = StateStore(tmp_path / "state")
            store.ensure()
            attempt = stage(store, watch)
            assert (
                exec_worker(store, attempt, scope=watch.scope, provider=provider).outcome
                == "blocked"
            )
        finally:
            child.terminate()
            child.wait(timeout=5)
    assert detect(provider, str(tmp_path)).observation == Observation.STOPPED


@pytest.mark.parametrize("kind", ["normal", "missing", "fifo", "large", "invalid", "nul"])
def test_production_bounded_settings_reader(tmp_path, kind):
    provider = PsutilProcessProvider()
    provider.snapshot()
    path = tmp_path / "settings"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind != "missing":
        content = {
            "normal": b"wallet-file=/wallet",
            "large": b"x" * (1024 * 1024 + 1),
            "invalid": b"\xff",
            "nul": b"\0",
        }[kind]
        path.write_bytes(content)
    text, status = provider.read_text_file(os.getpid(), str(path))
    assert status == {"normal": "resolved", "missing": "missing"}.get(kind, "unresolved")
    assert text == ("wallet-file=/wallet" if kind == "normal" else None)


def test_settings_disappearing_after_open_is_not_missing_fallback(tmp_path, monkeypatch):
    provider = PsutilProcessProvider()
    provider.snapshot()
    path = tmp_path / "settings"
    path.write_text("wallet_path=/target/wallet")
    original = os.stat

    def disappeared(candidate, *args, **kwargs):
        if candidate == str(path):
            raise FileNotFoundError(candidate)
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(os, "stat", disappeared)
    assert provider.read_text_file(os.getpid(), str(path)) == (None, "inaccessible")


def test_settings_modified_during_read_are_uncertain(tmp_path, monkeypatch):
    provider = PsutilProcessProvider()
    provider.snapshot()
    path = tmp_path / "settings"
    path.write_text("wallet_path=/target/wallet")
    original = os.fstat
    calls = 0

    def mutate(descriptor):
        nonlocal calls
        result = original(descriptor)
        calls += 1
        if calls == 1:
            # Change the opened inode in place, after its initial metadata snapshot.
            path.write_text("wallet_path=/otherx/wallet")
            # VM clocks can stamp both writes identically; make the change observable.
            os.utime(path, ns=(result.st_atime_ns, result.st_mtime_ns + 1_000_000_000))
        return result

    monkeypatch.setattr(os, "fstat", mutate)
    assert provider.read_text_file(os.getpid(), str(path)) == (None, "unresolved")
