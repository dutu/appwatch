# Development and validation

Inspect and edit source in the host working tree. Execute all project code,
tests, builds and dependency installation in the disposable Debian 13 VM; never
run the installer or project code on the host. From the project root:

```sh
codex-vm start
codex-vm test -- python3 -m venv .venv
codex-vm test -- .venv/bin/python -m pip install -e '.[dev]' build
codex-vm test -- .venv/bin/pytest -q
codex-vm test -- .venv/bin/ruff check src tests scripts
codex-vm test -- .venv/bin/ruff format --check src tests scripts
codex-vm test -- .venv/bin/python -m pip check
```

The matcher/process tests are adapted from VaultKeep's trigger tests. They cover
same-user inspection, strong entrypoint identity, predicates, PID replacement,
metadata failures, multiple instances, relative process paths, and directory
evidence precedence for Bisq, Electrum, and Wasabi. Configuration tests cover safe
YAML, rejected backup settings, strict types, runtime parameters, filesystem
validation, executable symlink preservation, examples, and CLI errors.

`tests/test_monero.py` covers executable-basename configuration, GUI normal/XDG
and portable settings, Qt scalar parsing, RPC wallet/config options, changing
wallet selection, the shared process guard, recovery, pending reconfirmation,
and final worker blocking. It includes real Linux unlink regressions using
copied harmless native binaries named for GUI/RPC, and bounded regular-file
reader checks. Those integration cases isolate their process enumeration from
unrelated VM session services; unreadable same-user executable identities in a
full scan correctly prevent shutdown confirmation.

For the opt-in native Monero check, extract the official GUI and CLI distributions
inside the guest, outside the synchronized checkout. Install `xvfb`, `xauth`,
`xdotool`, `xclip`, and `imagemagick` only in the guest for display interaction and
failure screenshots, then run:

```sh
codex-vm test -- xvfb-run -a -s '-screen 0 1280x960x24' \
  .venv/bin/python tests/live_monero.py \
  --gui /tmp/monero-gui-v0.18.5.4/monero-wallet-gui \
  --cli /tmp/monero-x86_64-linux-gnu-v0.18.5.4/monero-wallet-cli \
  --rpc /tmp/monero-x86_64-linux-gnu-v0.18.5.4/monero-wallet-rpc
```

Adjust extracted paths. The check generates two disposable offline wallets and
isolated HOME/XDG/cwd under the guest's `/tmp`; it tests native GUI normal and
portable settings, a GUI wallet switch to a path containing spaces, accented and
non-BMP characters, and RPC explicit configuration/direct selection with no real
funds or host profiles. Logs and wallet data stay in the guest. Enumeration is restricted to
the launched wallet PID to isolate protected unrelated session processes. GUI
interaction uses the fixed English v0.18.5.4 layout and reads back actual
Qt-written settings, including UTF-16 surrogate escapes. A failed GUI check saves
a screenshot beside its logs; other versions/layouts may require updating the
test coordinates. Unit tests cover durable activation across selection changes.

`tests/test_execution.py` exercises the lifecycle, durable state, worker claims,
literal command execution, output bounds, exits/signals, monitor recovery,
independent watch slots, pending reconfirmation, retry-delay persistence,
corrupt-state isolation, history cleanup and foreground-monitor signal handling.
CLI worker checks verify zero exits for durable success, failure, missing-command
and blocked receipts, and nonzero exits or propagated exceptions for worker errors.
They also check worker start/result messages, failure tracebacks, result details
and log-level filtering. Monitor checks verify reconciled result details and
ERROR/WARNING severity for failed/blocked actions.
Its real subprocesses are harmless helpers; the controller is injected so tests
do not install services or write to the account's actual recovery directory.
`tests/test_systemd.py` ports the source adapter's service-state and template
checks, with stricter treatment of numeric start-command failures and private
worker umask requirements.

To build a wheel:

```sh
codex-vm test -- .venv/bin/python -m build
```

Inside the VM, install that wheel in another environment and invoke `app-watch`
from outside the checkout to check distribution independence. The wheel contains
the application package and license; the source distribution also includes the
examples, tests, and documentation.

`tests/test_services_status.py` covers managed selections, install/update/disable/
remove, protected recovery, failure rollback, configuration-independent removal,
unit ownership/overrides, fresh observations and inventory isolation.
`tests/test_async_controller.py` checks that slow manager submissions do not stop
process observation, that manager work is bounded and that absence fallbacks share
a deadline. Desktop discovery/delivery tests are adapted from VaultKeep; explicit
external-notifier tests use harmless local executables, not real message delivery.

`tests/test_installer.py` covers ownership, custom layouts, preflight refusal,
activation failure, rollback and interrupted cleanup with controlled managers.
Its real-build case downloads wheels from `requirements.lock` with hash
verification inside the VM, then creates a real environment and invokes the
installed command outside the checkout. It needs Internet access for that
wheelhouse; it never depends on a host package cache. Global maintenance-lock
tests cover shared runtime access and bounded installer contention.

For the opt-in real systemd deployment check, install only in the disposable VM:

```sh
codex-vm test -- sudo ./install.sh install
codex-vm test -- /opt/app-watch/current/venv/bin/python tests/live_systemd.py
```

This check uses actual user services and harmless helper processes. It exercises
startup while stopped, confirmed shutdown, literal arguments, active-work update
refusal, monitor stop/restart while a worker continues, success/failure receipts,
no failure replay, notifications, software refresh and stopped/disabled flag
preservation. Temporary files and private recovery history remain in the guest
for inspection. The helper is not evidence of installed-wallet compatibility.

Additional opt-in checks, after the service check:

```sh
codex-vm test -- /opt/app-watch/current/venv/bin/python tests/live_manager.py
codex-vm test -- /opt/app-watch/current/venv/bin/python tests/live_desktop.py
codex-vm test -- .venv/bin/python tests/live_uninstall.py
```

The desktop check needs guest-only `dunst`, `xvfb` and `xauth` packages. The
uninstall check verifies retained records and reinstalls the normal software
after exercising custom paths. The service check invokes the root rollback
helper automatically; it is not a normal operator command.

Keep wallet downloads and disposable wallet data inside the guest, outside the
synchronized checkout. Never synchronize credentials or real wallet data.
Leave the VM running after checks. Changes made in the VM do not update host
source; apply fixes to the host working tree and rerun through `codex-vm`.

See [validation evidence](validation.md) for observed results and remaining
deployment limits, and the [implementation plan](../plans/implementation.md) for
acceptance criteria.
