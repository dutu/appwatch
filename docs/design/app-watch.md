# App-watch project specification

## Purpose and scope

Build an independent Linux application named `app-watch`, with Python package
`app_watch`, by extracting and adapting VaultKeep's shutdown-trigger functionality.
After observing a configured application running and then confirming its absence,
execute an external command configured for that watch. Commands are argument
arrays executed without an implicit shell. App-watch has no dependency on an
installed VaultKeep or on the sibling checkout.

Support Bisq 1 desktop/API daemon, Electrum client, and Wasabi GUI/daemon first.
Preserve their existing identity rules and multi-instance separation. Application
adapters must be independent of each other and of action execution. Future Monero
GUI and RPC wallet support belongs in new adapters, with its own process-model
verification; it is outside the initial implementation.

Provide installation, update, uninstallation, YAML configuration and examples,
runtime parameter settings, validation, watch listing, status, notifications,
foreground monitoring, and systemd user-service management. Follow VaultKeep's
operator workflow where it applies to triggers. Monitoring and action execution
run as the application owner, without root or sudo. System-wide software
installation may require administrator privileges, as in VaultKeep.

Exclude sources, destinations, archives, encryption, retention, backup
verification, backup lifecycle hooks, timers, and schedules. An external command
may invoke a backup tool, but App-watch neither understands nor manages backups.
Do not modify the sibling VaultKeep project or migrate its live configuration,
units, or recovery data.

## Source findings and extraction boundaries

The initial review used the clean sibling `vaultkeep` worktree at commit
`cf9dd0165f7100f11fdfee58b1523410984d28e2`.

| VaultKeep source | Treatment in App-watch |
| --- | --- |
| `src/vaultkeep/application.py` | Separate shared process inspection/matching from app directory rules; remove backup consistency and job preparation dependencies. |
| `src/vaultkeep/trigger/lifecycle.py` | Retain observation, arming, stop confirmation, and request supersession behavior; freeze command settings instead of a backup job. |
| `src/vaultkeep/trigger/monitor.py`, `models.py`, `state.py`, `paths.py` | Adapt durable orchestration and recovery to App-watch identities and action outcomes. |
| `src/vaultkeep/trigger/executor.py`, `preflight.py`, `childproc.py` | Replace VaultKeep invocation, version checks, backup validation, and result classification with generic command validation and execution. |
| `src/vaultkeep/trigger/service.py`, `systemd.py` | Retain user-service workflow and independent workers; replace imports from backup paths, schedule status, and backup locks. |
| `src/vaultkeep/trigger/status.py`, `activity.py` | Report application, monitor, action, and recovery state without backup status. |
| `src/vaultkeep/config/`, `cli/`, `notifications/` | Adapt only trigger-relevant validation, parameters, output, and notification facilities. |
| `install.sh`, `scripts/install_support.py`, `scripts/install_maintenance.py`, `installation.py`, `maintenance.py` | Preserve deployment safety and operator workflow while removing backup dependencies and timer maintenance. |
| `systemd/vaultkeep-trigger-exec@.service` | Adapt the independent worker template. Do not carry over backup services or timers. |
| `tests/trigger/`, relevant application and installer tests | Port meaningful contracts and regressions; add tests for the new command boundary and watch inventory. |

Trigger is not currently a standalone library: its config loader prepares complete
backup jobs, its worker parses backup results, and service management uses backup
locks and schedule helpers. Copying the trigger directory alone is insufficient.
Retain required MIT attribution when incorporating source. Do not create a shared
package or require coordinated changes to both projects.

VaultKeep's `list` inventories archives. App-watch's `list` instead inventories
configured watches and summarizes their application, service, and action state.
Backup inventory formatting and retention logic do not belong in App-watch.

## Module boundaries

```text
src/app_watch/
  apps/
    base.py               # Small adapter contract and directory-resolution result
    registry.py           # Explicit mapping of supported application names
    bisq.py
    electrum.py
    wasabi.py
  process.py              # Same-user inspection, identity matching, PID race checks
  config/                 # Strict schema, paths, runtime parameters, preparation
  trigger/                # Lifecycle, monitor, requests, persistence, recovery
  actions/                # Frozen command execution and receipts
  services/               # systemd operations and monitor/worker management
  notifications/          # Trigger/action notifications and external notifier
  cli/                    # Arguments, validation, inventory, status, safe output
  installation.py         # Installed layout and stable executable resolution
```

These are responsibility boundaries, not a requirement to create empty packages
or abstractions. Keep related small helpers together. Process inspection is shared;
adapters describe application-specific directory evidence, its precedence, and
application-specific matcher validation. Adapters do not launch commands, write
state, send notifications, or manage services. The monitor asks the registry for
an adapter rather than branching on wallet names.

Use a static built-in registry, not a dynamic plugin loader. Adding a wallet should
require its adapter, registration, examples, tests, and documentation; it should
not require changes to lifecycle, persistence, workers, or the installer.

### Initial application behavior

| Application | Directory evidence and identity requirements |
| --- | --- |
| Bisq | Explicit `--appDataDir`, including separate and `=` forms. Preserve configured script, JAR, and Java main-class entrypoints for desktop and daemon. |
| Electrum | Explicit `-D`/`--dir` takes precedence over `ELECTRUMDIR`. Explicit directory arguments do not require environment inspection unless another configured predicate requires it. |
| Wasabi | `WASABI_DATADIR` takes precedence over `--datadir`; unreadable required environment remains uncertainty. Preserve configured GUI/daemon entrypoints. |

Retain alternative-entrypoint OR semantics, additional mandatory matcher
predicates, canonical paths, process-relative directory resolution, owner checks,
and PID/create-time revalidation. Do not infer an instance solely from its process
name or silently assume an application's default data directory. Shipped examples
are templates, not verified presets for every installed wallet package.

## Configuration and command interface

Use one YAML file per watch under
`${XDG_CONFIG_HOME:-~/.config}/app-watch/jobs/`. Preserve applicable VaultKeep
settings: `job.id`, `application.name`, `application.data_dir`, matcher entrypoints
and predicates, trigger enablement, polling, confirmation, advisory deadline,
runtime parameters, logging, and trigger notifications. Add an `action.argv`
list whose first element resolves to an absolute executable. Reject an empty
command, non-string arguments, NUL characters, unknown keys, unsupported apps,
and backup/schedule configuration sections with actionable diagnostics.

The action is fixed by the effective configuration for that watch; there is no
application-specific action implementation. Resolve paths and supported runtime
parameters during preparation, without shell expansion or implicit inheritance of
arbitrary environment variables. Preserve VaultKeep's explicit child environment:
account home, validated XDG paths, fixed safe PATH, and LANG when present. Run in
the account home rather than an incidental monitor working directory. Commands
needing other setup can invoke an explicitly configured wrapper executable.

Preserve the familiar command structure:

```text
app-watch --version
app-watch --user --config PATH [--param KEY=VALUE ...] validate [--schema-only]
app-watch --user --config PATH [--param KEY=VALUE ...] status [--json] [--verbose]
app-watch --user list
app-watch --user --config PATH list
app-watch --user --config PATH [--param KEY=VALUE ...] trigger monitor
app-watch --user --config PATH [--param KEY=VALUE ...] trigger install
app-watch --user --config PATH [--param KEY=VALUE ...] trigger update
app-watch --user --config PATH trigger disable
app-watch --user --config PATH trigger remove
app-watch --user --config PATH notify ARGUMENTS...
```

No-config listing discovers the user's configured job files and installed managed
monitors, including missing/invalid configurations. Report errors per watch rather
than hiding unhealthy entries or preventing healthy entries from being listed.
Single-config listing selects that watch. Status separates configuration validity,
monitor health and observation freshness, application state, pending/active action,
last result, and recovery warnings. An active systemd unit alone is not proof of
healthy monitoring. Keep internal worker commands out of normal usage guidance.

## Monitoring and execution guarantees

Retain the source lifecycle and conservative recovery behavior:

- Only verified running evidence arms shutdown detection. Starting while the app
  is stopped launches nothing.
- Confirm continuous reliable absence with a monotonic delay. A restart cancels
  confirmation; ambiguous or inaccessible evidence requires a fresh full delay.
- Persist a request before dispatch, freezing effective argv, environment,
  application identity, matcher, execution account, and configuration fingerprint.
  Later YAML edits must not alter an already accepted command.
- Allow one active attempt and one pending request per watch; retain latest-pending
  supersession behavior. Independent watches may execute concurrently.
- Recheck stopped evidence before executing. A restarted app invalidates stale
  pending work. App-watch cannot prevent the application restarting after that
  final check and cannot guarantee that a command operates on offline data.
- Supervise actions in independent systemd workers. Monitor restart does not kill
  an executing action; worker receipts remain available for reconciliation.
- Record success for exit zero and failure for other exits or signals. Preserve
  bounded diagnostics, timestamps, IDs, and advisory overdue reporting. Do not
  interpret VaultKeep result markers or treat exit 10 as retryable contention.
- Do not automatically rerun a command after failure or uncertain execution.
  Recovery must distinguish proven non-submission from possible execution;
  ambiguous submission or missing receipts block replay pending review. Exactly-once
  effects cannot be guaranteed for arbitrary external commands.
- Preserve evidence on corrupt state and isolate affected watches. Removing a
  monitor does not erase recovery history. Keep lifecycle operations safe around
  active/pending/unreconciled work, using App-watch locks rather than backup locks.

Polling can miss an entire start/stop cycle between samples. Process disappearance
does not prove clean application shutdown. Advisory deadlines report overdue work
and do not terminate the command. Worker independence does not guarantee survival
of logout, user-manager termination, or host shutdown.

## Installation and isolation

Keep VaultKeep's release-directory and stable-command workflow, adapted to
`/opt/app-watch`, `/usr/local/bin/app-watch`, and App-watch's own installation
manifest. The installer supports `install`, `update`, `uninstall`, dry-run, and
explicit optional data cleanup. Retain staged verification, safe activation,
rollback, managed-file ownership checks, and validation/refresh of existing
managed user monitors. Updates refuse unsafe in-flight work and preserve recovery
evidence. A fresh software installation starts no watches and enables no lingering.

Monitor units use an `app-watch` namespace and remain keyed by canonical config
path. Recovery identity binds the selected config path, job ID, and effective
parameters, using only App-watch state. Use private account-owned fixed recovery
storage under `~/.local/state/app-watch/`, separate locks under
`$XDG_RUNTIME_DIR/app-watch/`, and independent worker units. Preserve atomic
replacement and file/directory fsync, directory mode 0700, and file mode 0600.
Lifecycle commands must still identify installed monitors when YAML is invalid or
missing; configuration changes cannot silently abandon old active recovery state.

Retain the source Linux/systemd and Python 3.13+ baseline initially. Keep only
runtime dependencies required for process inspection and YAML/schema validation;
omit archive tools and backup-only runtime dependencies. The installer also locks
the packaging backend's build dependencies, including hatchling's pathspec.
Software installation
must leave VaultKeep files, services, and data untouched.

## Verification boundary

Ported tests establish behavior under controlled process and manager conditions.
Production readiness additionally requires real systemd user-manager exercises
and verification of the installed Bisq, Electrum, and Wasabi process models.
Neither schema validation nor mocked systemctl tests certify those deployment
properties. The [implementation plan](../plans/implementation.md) specifies gates
for the complete project.
