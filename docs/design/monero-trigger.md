# Monero GUI and RPC shutdown trigger

## Purpose and scope

Add `application.name: monero` for one watch covering both native Linux
`monero-wallet-gui` and `monero-wallet-rpc` processes owned by the execution
account. This is a design for implementation; Monero support is not yet available.

Keep `application.data_dir` as the watched wallet's containing directory. Read
the wallet filename from the application's settings and compare its canonical
parent directory with the canonical configured directory. The comparison is
exact, not recursive. Any wallet directly inside that directory can activate the
watch. Do not add a separate app-watch `wallet_path` field.

Once activated, wait for every process matching either configured executable name to
exit, including processes selecting other wallets. Changing the selected wallet
must not release the shutdown guard. Apply the existing confirmation interval
and worker revalidation before running the configured external command.

Initial compatibility targets native Debian/Linux installations and the observed
Monero v0.18.5.4 launch model. AppImage/Flatpak confinement, Tails persistence,
RPC `wallet-dir`/`generate-from-json` activation, `monero-wallet-cli`, and
`monerod` are outside this change. An unsupported wallet-selection mode on a
matched process remains uncertain and blocks shutdown; it is not evidence of
absence. This trigger does not provide wallet locking or backup consistency.

## Observed installation

| Component | Observed identity or setting |
| --- | --- |
| GUI executable | `/opt/monero-gui/monero-gui-v0.18.5.4/monero-wallet-gui` |
| GUI arguments | Only the executable path; no wallet or configuration argument |
| GUI environment | `HOME=/home/bisquser`, `XDG_CONFIG_HOME=/home/bisquser/.config`, `XDG_CONFIG_DIRS=/etc/xdg` |
| GUI settings | `/home/bisquser/.config/monero-project/monero-core.conf`, key `wallet_path` |
| RPC executable | `/opt/monero-cli/monero-x86_64-linux-gnu-v0.18.5.4/monero-wallet-rpc` |
| RPC arguments | `monero-wallet-rpc --config-file=/home/bisquser/.config/monero-project/monero-wallet-rpc.conf` |
| RPC settings | The explicitly selected file, key `wallet-file` |
| Watched directory | `/home/bisquser/Monero/wallets/monero_2026-11-11-error_sd7` |

Both settings currently name the wallet file
`/home/bisquser/Monero/wallets/monero_2026-11-11-error_sd7/monero_2026-11-11-error_sd7`.
The GUI updates `wallet_path` when the operator switches wallets. No portable
`monero-storage/settings.ini` existed beneath the observed GUI working directory.
These observations describe this installation, not universal executable presets.

## Configuration and executable identity

Use the existing matcher entrypoint alternatives, adding
`kind: executable_name`. Compare the configured literal name with the basename
of the actual executable path from process metadata (`/proc/<PID>/exe`
on Linux). Do not use `argv[0]`, arbitrary argument tokens, window titles, or the
potentially truncated process name. Supplying a GUI path as an argument to an
unrelated process must not identify that process as the GUI.

Matching intentionally covers every same-user installation with these executable
basenames, independent of its directory or version. Another executable with the
same basename also matches: `data_dir` distinguishes activation, while after
activation every such process blocks shutdown regardless of its wallet directory.
No installation symlink or versioned executable path is required in YAML.

### Deleted running executables

Linux can append ` (deleted)` to the `/proc/<PID>/exe` target when a running
executable is unlinked, including during replacement by an upgrade. An executable
file's absence on disk does not mean that its process has exited.

Handle this before basename mismatch rejection. If removing exactly one trailing
` (deleted)` marker yields a configured executable basename, classify the
identity as potentially matching but unavailable. Do not arm a new watch from
that uncertain identity alone; preserve existing activation/pending work, reset
confirmation, and block worker execution. With no other verified target match,
return `INSPECTION_UNAVAILABLE`, never `STOPPED`. A suffix on a clearly unrelated
executable does not by itself make that process a Monero candidate.

The process library may already normalize the marker away. In that case the
reported expected basename remains a normal match even though its on-disk path
does not exist. Support both normalized and marker-bearing provider output; do
not depend on a particular psutil normalization behavior. Extract the basename
from the reported executable target without resolving that old path through the
current filesystem again: a replacement file or symlink at the old pathname must
not redefine the executable identity of the still-running process. Do not use
file-existence checks or command-line fallback to turn uncertainty into absence.

### YAML and validation

Example application section, to be included in a disabled full example during
implementation:

```yaml
application:
  name: monero
  data_dir: /home/bisquser/Monero/wallets/monero_2026-11-11-error_sd7
  matcher:
    entrypoints:
      - kind: executable_name
        value: monero-wallet-gui
      - kind: executable_name
        value: monero-wallet-rpc
```

For Monero, require exactly these two executable-name alternatives, covering
both roles. Role selection uses the matched literal executable name. Reject
other entrypoint kinds, unknown role names, duplicate alternatives, and nonempty
supporting predicates (`exe_realpath`,
`argv_tokens`, `cmdline_regex`, `env_eq`) for Monero: narrowing by a wallet token
or a single executable could exclude another writer from the agreed guard.
Existing applications keep their current configuration rules.

Executable-name values are literal basenames: reject slashes, empty names,
`.`/`..`, wildcard patterns, and parameter placeholders. They receive no path
canonicalization, home expansion, or parameter substitution. Preserve them
unchanged through preparation, fingerprinting, frozen requests, and worker
reconstruction. Existing path entrypoints and `data_dir` retain their current
preparation rules. An upgrade retaining the executable names needs no YAML edit;
verify settings-format compatibility after upgrades. These additions are backward
compatible extensions of configuration version 1.

## Wallet evidence

### GUI

The v0.18.5.4 GUI has no application `--config-file` option. Its Linux settings
selection is:

1. If `<process-cwd>/monero-storage/settings.ini` exists as a regular file, use
   that portable Qt INI file.
2. Otherwise use
   `<process-XDG_CONFIG_HOME>/monero-project/monero-core.conf` when that environment
   value is a nonempty absolute path.
3. With the variable unset or empty, use
   `<process-HOME>/.config/monero-project/monero-core.conf`.

Read `wallet_path` from Qt's general settings section. An unreadable portable
candidate must not be treated as absent and cause fallback to another file.
Unreadable environment/cwd metadata, invalid relative environment roots, or a
missing/invalid wallet setting make identification uncertain. Use the process's
environment, never the monitor's `HOME` or XDG settings. If process `HOME` is
unavailable, use verified execution-account home metadata only when the Qt
fallback behavior has been verified; otherwise remain uncertain.

Re-evaluate settings selection and read the wallet setting on every poll while
processes are present. Portable mode can change while the GUI runs. Normal Qt
organization/system fallback settings are not reconstructed in this initial
implementation: a missing primary `wallet_path` remains uncertain, even if Qt
could find it in another file. Do not silently read a Tails persistence path.

### RPC

Read the configuration selected by `--config-file PATH` or `--config-file=PATH`.
Do not assume that RPC automatically reads the observed file when the option is
absent. Read `wallet-file` from that configuration. Also recognize an explicit
`--wallet-file PATH` or `--wallet-file=PATH` for installations specifying the
wallet directly. If both sources are present and agree after resolution, accept
them; conflicting values are uncertain rather than guessing which was used.
Repeated/conflicting selection options and unsupported selection modes are also
uncertain. Unrelated RPC settings, including credentials, are not needed.

Read configuration on every poll. A running RPC process may retain a wallet
different from an edited configuration; configuration is selection evidence,
not proof of its current internal wallet state. The process guard remains in
force regardless of this evidence.

### Reading and interpreting files

Extend the existing process-provider seam with a small bounded regular-file
reader suitable for fake providers. Distinguish missing files from access
failures, and reject nonregular files without blocking on FIFOs/devices. Bound
each settings read to 1 MiB and reject oversized, undecodable, truncated, or
malformed content. Revalidate PID/create-time identity around environment, cwd,
and file inspection. Treat replacement or disappearance during inspection as
uncertain for that poll.

Use application-specific parsing: Qt INI for the GUI's `[General]` setting and
Monero/Boost-style `key=value` for RPC. Preserve case and disable interpolation.
Handle the quoted/escaped scalar path forms emitted by the supported GUI
release, with fixtures based on that format. Reject duplicate relevant keys,
invalid escapes, nonstring Qt encodings, empty values, and unsupported forms
rather than inventing a path. Ignore unrelated keys; do not deserialize Qt
objects, load wallets, or interpret configuration as executable content.

Resolve relative configuration paths and relative wallet filenames against the
inspected process's verified cwd, not the configuration file's parent or the
monitor's cwd. Do not perform shell expansion. Canonicalize the wallet filename
before taking its parent directory, so file and directory symlinks have defined
behavior. Missing wallet files need not prove absence; the selected path is the
evidence, and the matching process still blocks shutdown. Do not read wallet
contents, passwords, RPC credentials, logs, or network endpoints. Retain only
necessary sanitized path/source evidence, never entire configuration contents.

## Detection and lifecycle

The current implementation already provides the needed durable latch:
`InstanceState.armed` survives ambiguous observations and same-boot monitor
recovery. A pending request also survives ambiguity but requires fresh stop
confirmation. Use these behaviors rather than adding another persistent latch.

Add a small adapter policy declaring that a matched process blocks shutdown even
when its wallet directory does not match. Default it off for existing adapters
and enable it for Monero. Preserve the stateless `detect_spec` entry point used
by both monitor and worker. Its Monero result rules are:

| Process observation | Detection result | Effect |
| --- | --- | --- |
| At least one verified process selects a wallet in `data_dir` | `RUNNING` | Arm; clear stop confirmation |
| A matched process exists, but none has verified target-directory evidence | `AMBIGUOUS`, or `INSPECTION_UNAVAILABLE` for unreadable metadata | Do not arm a new watch; preserve previous activation/pending request; reset confirmation |
| No matched process and no unresolved executable-identity inspection | `STOPPED` | Allow existing lifecycle to confirm shutdown if already armed/pending |
| Snapshot or potentially relevant identity cannot be inspected reliably | `INSPECTION_UNAVAILABLE` | Prevent shutdown confirmation/execution |
| Executable metadata retains a deletion marker on a potentially matching name | `INSPECTION_UNAVAILABLE`, unless another process already establishes `RUNNING` | Preserve activation; reset confirmation; block execution |

Report a clear reason when another Monero wallet/process is keeping the guard
active. `AMBIGUOUS` here means shutdown cannot be confirmed for this watch; it
can be intentional even when the other wallet's location is known. A known
target selection may return `RUNNING` despite another uncertain process because
it already prevents execution. Never return `STOPPED` merely because all readable
wallet settings name other directories.

This yields the required sequence:

1. GUI opens wallet A outside the watched directory: no activation, no action.
2. GUI switches to watched wallet B: a later poll observes it and arms the watch.
3. GUI switches back to A: activation remains remembered, confirmation cannot start.
4. GUI exits but RPC remains, even on wallet C: continue waiting.
5. All configured Monero processes exit: begin `confirm_seconds`.
6. Any configured process reappears during confirmation: reset confirmation.
7. After confirmed absence, dispatch the existing frozen request. The worker
   performs the same conservative detection immediately before executing.

No monitor or worker application-specific branches, new observation enum value,
state-schema migration, extra daemon, or dependency is needed. Existing boot and
configuration-fingerprint changes reset activation. A same-boot restart discards
elapsed confirmation time and preserves the existing latch/reconfirmation rules.
Action outcome, blocked-worker receipts, retries, and no-replay behavior retain
their current semantics; this change does not add automatic retries for blocked
or failed workers.

## Limits

Polling and delayed Qt settings writes can miss a wallet opened and closed
between observations. Persisted `wallet_path` may also describe the last selected
wallet while the GUI is at a startup/password screen. Treat it as conservative
selection evidence, not proof that wallet opening succeeded. No guarantee of
observing every wallet visit is made.

A new process can start after final inspection and before the external command
starts, or while it runs. Process polling cannot eliminate this race without
cooperation from wallet applications. Existing action execution is not aborted
when an application starts. Users must keep wallet applications closed during
actions requiring exclusive access.

The guard covers all same-user processes whose actual executable basenames match
the configured names, across installation directories and versions. Renamed
binaries and another user's processes are outside that coverage. A different
open wallet or another executable with the same basename can delay an action
indefinitely by design. Deleting or editing configuration never establishes
shutdown while a matched process exists.

## Implementation and verification

Follow the [implementation plan](../plans/monero-trigger.md). Update the
[application guide](../applications.md), [configuration reference](../configuration.md),
[README](../../README.md), disabled examples, and relevant development test
instructions during implementation. Do not describe Monero as implemented until
that work is complete. Record actual test results separately from this design.

Source references for the observed release:

- [GUI option parser and Qt application identity](https://github.com/monero-project/monero-gui/blob/v0.18.5.4/src/main/main.cpp)
- [GUI settings selection and portable mode](https://github.com/monero-project/monero-gui/blob/v0.18.5.4/src/qt/MoneroSettings.cpp)
- [GUI wallet_path and Tails settings](https://github.com/monero-project/monero-gui/blob/v0.18.5.4/main.qml)
- [RPC shared option/configuration parsing](https://github.com/monero-project/monero/blob/v0.18.5.4/src/wallet/wallet_args.cpp)
- [Qt 5 settings paths and serialization](https://doc.qt.io/archives/qt-5.15/qsettings.html)
- [Linux executable metadata and deletion markers](https://man7.org/linux/man-pages/man5/proc_pid_exe.5.html)
