# Application setup

App-watch identifies same-user processes using strong entrypoints and explicit
data-directory evidence. A process name, window title or an executable such as
`java` alone is insufficient. Inspect the installed launch command and process
arguments as the application owner. Configure all GUI and daemon entrypoints
that can write the watched directory as alternatives in the same matcher.
Additional predicates apply to every alternative.

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
