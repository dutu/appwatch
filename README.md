# app-watch

App-watch watches application instances and runs a configured external command
after confirmed shutdown.

App-watch observes a running instance before arming, confirms continuous absence,
and saves a frozen command request before submitting an independent systemd
worker. Arguments are passed literally, without a shell. Failed or uncertain
actions are never automatically replayed. See [recovery](docs/recovery.md) for
the execution guarantees and limits.

This is version `0.1.0.dev0`. Process matchers must be reviewed against the actual
installed application's GUI and daemon writers; the examples are disabled
templates, not verified installation presets.

## Requirements and installation

The software installer requires Debian 13 or newer, Python 3.13+, root access,
and Internet access to the Debian and Python package repositories. Watches run
as the non-root application owner with an active systemd user manager. Desktop
notifications additionally require that user's graphical notification session.

From a trusted source checkout, inspect the destination and install:

```sh
./install.sh install --dry-run
sudo ./install.sh install
app-watch --version
```

The installer provisions Python/venv, systemd, D-Bus user-session support and
`libnotify-bin` on fresh installation. It creates a dedicated environment with
hash-locked dependencies, validates the staged release and shared worker unit,
and activates it only after safety checks. Installing software starts no watch.

| Resource | Default path |
| --- | --- |
| Releases and installer records | `/opt/app-watch/` |
| Stable command | `/usr/local/bin/app-watch` |
| Shared worker template | `/etc/systemd/user/app-watch-exec@.service` |
| Suggested watch files | `~/.config/app-watch/jobs/` |

For custom destinations, set `APP_WATCH_INSTALL_PREFIX`, `APP_WATCH_BIN_LINK`
and `APP_WATCH_USER_SYSTEMD_ROOT` with `sudo env NAME=value ... ./install.sh ...`.
Use the same settings for every operation. Paths must be absolute, trusted,
nonoverlapping and safely owned; the unit root must be in systemd's user-unit
search path. The binary path cannot contain shell/systemd metacharacters.
Never select VaultKeep's installation paths.

## Configure and validate a watch

Copy a disabled template: [Bisq](examples/bisq-trigger.yaml),
[Electrum](examples/electrum-trigger.yaml), or [Wasabi](examples/wasabi-trigger.yaml).
For example, as the application owner:

```sh
mkdir -p ~/.config/app-watch/jobs
cp examples/electrum-trigger.yaml ~/.config/app-watch/jobs/electrum.yaml
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml validate --schema-only
```

Set a unique `job.id`, the account-owned application data directory, and strong
entrypoints matching the installed application's observed process identity.
Include every GUI/daemon writer for that directory. See the
[configuration reference](docs/configuration.md) and
[application setup guide](docs/applications.md) for identity and directory rules.

Replace `action.argv` with the command and its arguments. Its first element must
be an absolute or `~/` executable path. The external command is responsible for
its own work, including any backup behavior. Keep it synchronous; background
work is outside the recorded command result.

```sh
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml validate
```

With triggering disabled, expect `Result: OK (configuration and paths)`.
Validation checks settings, directory ownership and executable availability; it
does not run the action or certify that all wallet writers are covered.

## Enable and operate

Set `trigger.enabled: true`, then run as the application owner, without sudo:

```sh
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml trigger install
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml status
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml validate
app-watch --user list
```

Installation enables and starts the monitor. Allow its first observation before
expecting healthy validation. It must observe the application running before a
later confirmed shutdown can run the command. An already stopped application
does not trigger an action. Enabled but incomplete setup reports
`Result: SETUP INCOMPLETE` and returns 2.

After configuration edits, refresh the monitor and check health again:

```sh
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml trigger update
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml status --verbose
```

Use the same `--param NAME=VALUE` selections when managing parametrized watches.
To stop watching or remove its service:

```sh
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml trigger disable
app-watch --user --config ~/.config/app-watch/jobs/electrum.yaml trigger remove
```

Disable/remove use the installed selection even if YAML is invalid or missing;
omit `--param` for these commands. They preserve recovery files and refuse
active, pending, blocked or unreconciled work. They never cancel workers. Remove
the old monitor before moving its configuration file.

## Troubleshooting

Use `status --verbose` or `status --json --verbose` to find the service name,
recovery directory and diagnostics. Inspect `journalctl --user -u SERVICE` for
the monitor or worker. `UNKNOWN` application state requires fresh observation,
an intact monitor lock and matching configuration; an active unit alone does
not establish healthy monitoring.

Preserve blocked recovery evidence and review command effects before making any
manual changes. There is no public replay command. Monitor shutdown does not
cancel workers, but logout, user-manager shutdown and reboot can interrupt them.
Read [recovery](docs/recovery.md), [CLI reference](docs/cli.md) and
[notifications](docs/notifications.md) for details.

## Update and uninstall

From the desired trusted source checkout:

```sh
sudo ./install.sh update --dry-run
sudo ./install.sh update
sudo ./install.sh uninstall
```

Updates validate installed ownership, effective services, configuration and
recovery state; pause managed monitors; activate a staged release; refresh their
units; and resume only monitors that were active. Existing enabled/disabled
flags are preserved. Changed managed files, overrides, foreground monitors or
unresolved work require review before proceeding. Repeating `install` performs
the same safe replacement workflow.

A failed change rolls back only when no new work or operator edits make that
unsafe. An interrupted transaction keeps a private journal and blocks runtime
changes. Resolve the reported problem and rerun the installer with the same
paths; dry-run reports the interruption without repairing it. Detailed installer
output is private at `/opt/app-watch/install.log`.

Uninstall removes managed software releases and monitor units after safe-stop
checks. It always preserves user configuration, recovery records and application
data. `uninstall --purge` additionally removes the installer receipt; the private
log and coordination file remain. Debian packages installed as prerequisites are
left installed.

## Development

Tests, builds, applications and dependency installation run only in the
disposable Debian VM. See [development and validation](docs/development/testing.md)
for commands and validation evidence.

- [Project specification](docs/design/app-watch.md)
- [Implementation plan and acceptance checks](docs/plans/implementation.md)
