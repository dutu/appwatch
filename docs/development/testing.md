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

`tests/test_execution.py` exercises the lifecycle, durable state, worker claims,
literal command execution, output bounds, exits/signals, monitor recovery,
independent watch slots, pending reconfirmation, retry-delay persistence,
corrupt-state isolation, history cleanup and foreground-monitor signal handling.
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
