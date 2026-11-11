# Application setup

App-watch identifies same-user processes using strong entrypoints and explicit
data-directory evidence. A process name, window title or an executable such as
`java` alone is insufficient. Inspect the installed launch command and process
arguments as the application owner. Configure all GUI and daemon entrypoints
that can write the watched directory as alternatives in the same matcher.
Additional predicates apply to every alternative. Monero uses actual executable
basenames and prohibits additional predicates to preserve its shared shutdown guard.

The [examples](../examples) are disabled templates. Their paths and class names
require local verification. `validate` checks configuration, ownership and setup;
it cannot prove that a matcher covers every writer. Test one complete application
lifecycle with a harmless action before configuring a consequential command.

## Bisq 1 desktop and API daemon

Use exact observed Java main classes, JAR paths, or launcher/script paths as
entrypoints. Configure the process's explicit `--appDataDir` as
`application.data_dir`. Both `--appDataDir=/path` and `--appDataDir /path` are
recognized. Do not infer an installation's directory from an application name
or assume a daemon uses the desktop directory. The adapter does not read
environment variables or infer default Bisq directories.

## Electrum client

Use the canonical installed launcher/script path, not the Python interpreter,
as the strong entrypoint. Explicit `-D` / `--dir` directory arguments take
precedence over `ELECTRUMDIR`. Both separated and `--dir=/path` forms are
recognized. Match the actual data directory, not a wallet file beneath it.

`ELECTRUMDIR` can also be required as an exact environment predicate, but such a
predicate remains mandatory even when a command-line directory takes precedence.
App-watch does not infer Electrum's default directory. Launch with an explicit
directory for verifiable matching.

## Wasabi GUI and daemon

Use the observed native launcher/script path or .NET DLL entrypoint, with
alternatives covering both GUI and daemon writers. An interpreter/runtime alone
does not identify Wasabi. `WASABI_DATADIR` takes precedence over the explicit
`--datadir` argument; separated and equals forms are recognized. Default
directories are not inferred. Set an explicit directory and verify the launched
process retains that evidence.

## Monero GUI and RPC

Use the [disabled Monero template](../examples/monero-trigger.yaml). Configure
`application.data_dir` as the wallet's containing directory. Both literal
`executable_name` entrypoints, `monero-wallet-gui` and `monero-wallet-rpc`, are
required. Names match the actual `/proc/PID/exe` basename, independent of version
and installation directory; no symlinks or versioned paths are required. Process
names and command-line tokens do not establish executable identity. Another
same-user executable with one of these basenames also matches.

The GUI adapter first checks `monero-storage/settings.ini` under the process's
working directory. If absent, it reads
`$XDG_CONFIG_HOME/monero-project/monero-core.conf`, falling back to
`$HOME/.config/monero-project/monero-core.conf` when `XDG_CONFIG_HOME` is unset or
empty. Environment values belong to the inspected process. Read failures do not
permit fallback. It reads the Qt `[General]` key `wallet_path`. The GUI has no
`--config-file` option in the supported v0.18.5.4 launch model.

For RPC, launch with an explicit `--config-file PATH` or `--config-file=PATH`
containing `wallet-file=PATH`, or supply `--wallet-file PATH` / `--wallet-file=PATH`
directly. No default RPC configuration path is inferred. If both sources select
wallets, they must agree after resolution. Duplicates, conflicts, missing values,
and unsupported selection modes are uncertain. Relative paths use the inspected
process's working directory; shell expressions are not expanded. The canonical
wallet filename's parent must equal `data_dir` exactly, not a nested directory.

Settings are re-read each poll, so opening the watched wallet later in a running
GUI can activate the watch. Once activated, **every matching same-user GUI/RPC
process must exit**, including those now selecting other wallets. Switching
wallets, editing/deleting configuration, or closing only one application cannot
confirm shutdown. Other-wallet processes intentionally report `AMBIGUOUS` and
can delay the action indefinitely. Once all exit, the normal confirmation
interval and final worker check apply. Same-boot monitor recovery preserves
activation but restarts confirmation. A worker blocked by a new process retains
the existing blocked-receipt/no-automatic-retry behavior.

Deleted running executables remain guards: a potentially matching ` (deleted)`
target reports inspection unavailable until it exits. Any same-user process with
unreadable executable identity can also prevent confirmation, including protected
session services; app-watch cannot safely assume that such a process is unrelated.

Native Linux GUI/RPC is supported. Tails persistence, Flatpak/AppImage confinement,
Qt organization/system settings fallback, RPC `wallet-dir`/`generate-from-json`
activation, renamed executables, `monero-wallet-cli`, and `monerod` are outside
this adapter's coverage. Review settings compatibility after upgrades. Settings
reads are limited to regular UTF-8 files of at most 1 MiB; malformed or unsupported
values prevent activation and never establish shutdown while a process remains.

Delayed settings writes and polling can miss brief wallet visits. A saved
`wallet_path` can also name a previously selected wallet at the startup/password
screen. This is conservative selection evidence, not proof of an unlocked wallet.
Process polling provides no wallet lock: keep applications closed during actions
that require exclusive access, as they can restart after the final check.

## Ambiguous or inaccessible observations

Relative process directory values require readable process working-directory
metadata. Multiple matching instances, conflicting directory evidence or
inaccessible metadata do not count as a verified shutdown. They reset shutdown
confirmation and prevent final worker execution. Supporting exact argument,
executable, regex and permitted environment predicates can narrow identity;
they must reflect stable properties of the actual installation.

Each adapter lives under `src/app_watch/apps/` and is selected by a static
registry. Adding an application requires its directory-evidence adapter and
tests; lifecycle, worker and service code remain application-independent.

[Configuration reference](configuration.md) · [User guide](../README.md)
