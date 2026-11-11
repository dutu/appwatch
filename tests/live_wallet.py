"""Observe a real wallet in a fresh disposable directory; opt-in VM check."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from app_watch.process import (
    Entrypoint,
    MatcherSpec,
    Observation,
    PsutilProcessProvider,
    detect_spec,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    # Keep Java class identity out of a separate argv token in this observer.
    parser.add_argument("--entrypoint", metavar="KIND=VALUE", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    assert os.geteuid() != 0
    # Never open an existing wallet profile in this test.
    args.data_dir.mkdir(mode=0o700)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    assert command
    kind, value = args.entrypoint.split("=", 1)
    provider = PsutilProcessProvider(owner_uid=os.geteuid())

    def observe():
        return detect_spec(
            app=args.application,
            data_dir=str(args.data_dir.resolve()),
            matcher=MatcherSpec(entrypoints=(Entrypoint(kind, value),)),
            provider=provider,
        )

    initial = observe()
    assert initial.observation == Observation.STOPPED, initial
    log = args.data_dir.with_suffix(".launch.log")
    with log.open("w") as output:
        process = subprocess.Popen(command, stdout=output, stderr=output, start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = observe()
                if result.observation == Observation.RUNNING:
                    break
                if process.poll() is not None:
                    raise AssertionError(f"Wallet exited: see {log}")
                time.sleep(0.2)
            else:
                raise AssertionError(f"Wallet not detected: {result}; see {log}")
            print(json.dumps(asdict(result)), flush=True)
            time.sleep(3)
            assert observe().observation == Observation.RUNNING, f"Wallet stopped: see {log}"
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
    assert observe().observation == Observation.STOPPED
    print(f"Real {args.application} running/stopped identity verified; log: {log}")


if __name__ == "__main__":
    main()
