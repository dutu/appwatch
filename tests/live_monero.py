"""Opt-in native Monero smoke check; run under xvfb-run only in the disposable VM."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from app_watch.apps.monero import _wallet_setting
from app_watch.process import (
    Entrypoint,
    MatcherSpec,
    Observation,
    PsutilProcessProvider,
    detect_spec,
)


class SmokeProvider(PsutilProcessProvider):
    """Isolate launched wallets from unrelated protected VM session processes."""

    pids: set[int]

    def snapshot(self):
        return [proc for proc in super().snapshot() if proc.pid in self.pids]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--rpc", type=Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() != 0 and os.environ.get("DISPLAY")
    root = Path(tempfile.mkdtemp(prefix="appwatch-monero-native-"))
    home = root / "home"
    home.mkdir()
    xdg = home / ".config"
    config = xdg / "monero-project/monero-core.conf"
    config.parent.mkdir(parents=True)
    wallets = root / "wallets"
    wallets.mkdir()
    wallet = wallets / "disposable"
    switched_dir = root / "wallets with spaces é 😀"
    switched_dir.mkdir()
    switched_wallet = switched_dir / "second wallet é 😀"
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
    # No daemon, funds, existing wallets, production profiles, or real passwords.
    for index, generated_wallet in enumerate((wallet, switched_wallet)):
        with (root / f"generation-{index}.log").open("w") as output:
            subprocess.run(
                [
                    str(args.cli),
                    "--offline",
                    "--generate-new-wallet",
                    str(generated_wallet),
                    "--password",
                    "",
                    "--mnemonic-language",
                    "English",
                    "exit",
                ],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                check=True,
                timeout=60,
            )
    assert wallet.with_suffix(".keys").is_file()
    matcher = MatcherSpec(
        entrypoints=tuple(
            Entrypoint("executable_name", name)
            for name in ("monero-wallet-gui", "monero-wallet-rpc")
        )
    )
    provider = SmokeProvider()
    provider.pids = set()

    def observe(directory=wallets):
        return detect_spec(
            app="monero", data_dir=str(directory), matcher=matcher, provider=provider
        )

    def wait_for(predicate, description, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise AssertionError(f"Timed out: {description}; see {root}")

    def switch_gui_wallet(process):
        def xdo(*arguments):
            return subprocess.check_output(["xdotool", *arguments], env=env, text=True).strip()

        windows = []

        def main_window_visible():
            result = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--pid", str(process.pid)],
                env=env,
                capture_output=True,
                text=True,
            )
            windows[:] = result.stdout.splitlines()
            return bool(windows)

        wait_for(main_window_visible, "GUI window")
        main_window = windows[0]
        # Fixed native v0.18.5.4 English layout under Xvfb, independent of centering.
        xdo("windowsize", main_window, "980", "800")
        xdo("windowmove", main_window, "150", "80")

        def click(x, y):
            xdo("mousemove", "--window", main_window, str(x), str(y), "click", "1")
            time.sleep(0.5)

        click(670, 445)  # Unlock the initial disposable wallet (empty password).
        wait_for(lambda: "disposable" in xdo("getwindowname", main_window), "initial wallet open")
        click(75, 25)  # Close wallet and return to the wallet chooser.
        click(300, 340)  # Open a wallet from file.
        click(790, 196)  # Browse filesystem.
        dialog = xdo("getwindowfocus")
        xdo("mousemove", "--window", dialog, "200", "22", "click", "1", "key", "ctrl+a")
        # Clipboard paste preserves Unicode; xdotool type depends on the X keyboard map.
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=str(switched_wallet.with_suffix(".keys")).encode(),
            env=env,
            check=True,
            stderr=subprocess.DEVNULL,
        )
        xdo("key", "ctrl+v")
        xdo("key", "Return")
        time.sleep(0.5)
        click(670, 445)  # Unlock the newly selected disposable wallet.
        wait_for(
            lambda: "second wallet" in xdo("getwindowname", main_window),
            "switched wallet open on the same GUI process",
        )

        def qt_wrote_new_path():
            try:
                return _wallet_setting(config.read_text(), gui=True) == str(
                    switched_wallet.with_suffix(".keys")
                )
            except (OSError, ValueError):
                return False

        wait_for(qt_wrote_new_path, "GUI-written switched wallet_path")
        assert process.poll() is None
        assert observe(switched_dir).observation == Observation.RUNNING
        assert observe().observation == Observation.AMBIGUOUS
        print(
            "gui-switch: same PID selected Unicode/spaced wallet; Qt-written settings parsed",
            flush=True,
        )

    def run_case(label, command, interaction=None):
        assert observe().observation == Observation.STOPPED
        with (root / f"{label}.log").open("w") as output:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            provider.pids.add(process.pid)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    result = observe()
                    if result.observation == Observation.RUNNING:
                        break
                    assert process.poll() is None, f"{label} exited; see {root}"
                    time.sleep(0.2)
                else:
                    raise AssertionError(f"{label}: {result}; see {root}")
                # Let startup settle and Qt flush pending properties.
                time.sleep(3)
                assert process.poll() is None and observe().observation == Observation.RUNNING
                if interaction is not None:
                    interaction(process)
                print(f"{label}: native executable and wallet selection detected", flush=True)
            except Exception:
                subprocess.run(
                    ["import", "-window", "root", str(root / f"{label}-failure.png")],
                    env=env,
                    check=False,
                    timeout=5,
                )
                raise
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                provider.pids.clear()
        assert observe().observation == Observation.STOPPED

    settings = (
        f"[General]\nwallet_path={wallet}\nuseRemoteNode=true\ncheckForUpdates=false\n"
        "askDesktopShortcut=false\ndisplayWalletNameInTitleBar=true\nlocale=en_US\nwalletMode=2\n"
    )
    config.write_text(settings)
    run_case("gui-normal", [str(args.gui)], switch_gui_wallet)
    portable = root / "monero-storage/settings.ini"
    portable.parent.mkdir()
    portable.write_text(settings)
    # Conflicting normal settings prove that portable selection takes precedence.
    config.write_text("[General]\nwallet_path=/unrelated/wallet\n")
    run_case("gui-portable", [str(args.gui)])
    rpc_config = root / "wallet-rpc.conf"
    rpc_config.write_text(
        f"wallet-file={wallet}\npassword=\nrpc-bind-ip=127.0.0.1\n"
        "rpc-bind-port=28089\ndisable-rpc-login=1\noffline=1\n"
    )
    run_case("rpc-config", [str(args.rpc), f"--config-file={rpc_config}"])
    run_case(
        "rpc-direct",
        [
            str(args.rpc),
            f"--wallet-file={wallet}",
            "--password",
            "",
            "--rpc-bind-ip=127.0.0.1",
            "--rpc-bind-port=28089",
            "--disable-rpc-login",
            "--offline",
        ],
    )
    print(f"Isolated native smoke checks passed; guest-only disposable data/logs: {root}")


if __name__ == "__main__":
    main()
