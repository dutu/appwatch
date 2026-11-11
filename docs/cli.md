# Command reference

All watch commands require `--user`; watch-specific commands also require
`--config PATH`. Put those options and repeated `--param NAME=VALUE` before the
command. Watch management runs as the non-root application owner, without sudo.
Software setup is described in the [user guide](../README.md#requirements-and-installation).

| Command | Behavior |
| --- | --- |
| `app-watch --version` | Show the package version. |
| `validate --schema-only` | Check strict YAML/schema without account, path or service checks. |
| `validate` | Resolve parameters and paths, check directory ownership/executable availability; for an enabled watch also check monitoring health and worker readiness. |
| `status` | Show configuration, installed selection, monitor, application, dispatch and last action outcome. |
| `status --verbose` | Include service name, recovery directory, scope, parameters and persisted state. |
| `status --json` | Emit structured status; add `--verbose` to include details. |
| `list` | With `--config`, list that watch; otherwise scan configured YAML files and managed installed monitors. |
| `trigger monitor` | Monitor one enabled watch in the foreground, handling SIGINT/SIGTERM without cancelling workers. |
| `trigger install` | Write, enable and start a new per-watch user monitor service. Refuse an existing installation. |
| `trigger update` | Refresh, enable and restart an installed monitor after safe recovery checks. |
| `trigger disable` | Stop and disable an installed monitor; preserve its unit and recovery state. |
| `trigger remove` | Stop, disable and remove an installed monitor; preserve recovery state. |
| `notify ARGUMENTS...` | Pass all remaining arguments literally to the configured external notifier; see [notifications](notifications.md). |

The internal worker service entry point is not an operator replay interface.
There are no backup, archive, retention, verification, manual action-run or
scheduling commands.

## Service identity and safety

Each canonical config path has one monitor unit named
`app-watch-monitor@<escaped-filename>-<path-hash>.service`. Its generated content
pins the original job ID, effective parameters, execution identity and command
path. Units live in `${XDG_CONFIG_HOME:-~/.config}/systemd/user/`. The shared
`app-watch-exec@.service` template supervises workers independently of monitors.

Install/update require valid prepared YAML, `trigger.enabled: true`, a reachable
user manager and a compatible worker template. Development pip installation does
not install the template. Disable/remove decode the installed selection without
opening YAML; omit `--param` for those operations. Removing a config file does
not make retained active or uncertain work safe to discard.

Lifecycle commands verify managed content, ownership, loaded fragment and absence
of overrides. Modified, overridden or unverified units require explicit review
before the application will change them. A lifecycle lock excludes monitor
dispatch, and a separate lifetime lock excludes competing foreground monitors
from final safety checks and unit replacement. Active, pending, blocked and
unreconciled recovery records prevent lifecycle changes.

Changing the job ID or effective parameters changes recovery identity. Use
`trigger update` to transition the pinned selection after outstanding work has
been reconciled. Direct monitor startup refuses a different installed identity.
Remove the old monitor before moving/renaming YAML. Recovery data is not migrated
or erased by these commands.

Failed activation attempts stop any possibly started new monitor and inspect its
recovery state before restoring the old generated selection and activation flags.
If stopping, ownership verification or recovery checks cannot establish a safe
rollback, the command reports that rollback could not finish and preserves the
selection/evidence for review. It never rolls a selection back across new work.

## Status and inventory

`status` is read-only and reports configuration errors alongside installed recovery
information when available. It does not repair records or reconcile workers.
Observation freshness requires the expected configuration fingerprint, current
boot, held monitor lock, the current monitor session and an observation no older
than the larger of 10 seconds or three poll intervals. A recent observation from
before a monitor restart remains `UNKNOWN` until that session observes again.
An active unit alone produces no healthy application claim.

`NOT RUNNING` means a fresh stopped observation without a confirmed shutdown in
the current monitor session, including startup while already stopped.
`CONFIRMING STOP` indicates confirmation is in progress. `HAS STOPPED` requires
a frozen shutdown request confirmed during this session; request history from a
previous session cannot establish it. Inspect diagnostics when the monitor or
application is `UNKNOWN`.

`list` scans `*.yaml` and `*.yml` under the user's App-watch jobs directory and
includes managed services for other paths, including missing configs. Bad YAML,
symlink loops and discovery errors remain visible alongside other entries.
Columns show watch, config validity, monitor, application and dispatch state.
Manager unavailability does not hide locally discoverable watches.

`status` and `list` return zero when the report is produced; unhealthy states are
represented in the output. Validation errors, incomplete enabled setup, management
failures and usage errors return 2. `notify` delivery is best-effort: configured
command failures/timeouts warn and return zero, while invalid/missing notifier
configuration returns 2. SIGINT/SIGTERM give foreground monitoring an orderly
shutdown; running worker lifetime remains a separate systemd concern.

[User guide](../README.md)
