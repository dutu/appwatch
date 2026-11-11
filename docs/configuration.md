# Configuration reference

The monitor requires a compatible installed systemd worker template. The
[software installer](../README.md#requirements-and-installation) installs it;
development pip installation alone does not.

Use a separate YAML file per application instance. YAML 1.2 safe loading rejects
duplicate keys, recursive aliases, and unsafe object tags. Unknown settings,
incorrect types, NUL characters, and all backup/schedule sections are errors.
The schema version is App-watch's own version, independent of VaultKeep.

| Setting | Meaning and default |
| --- | --- |
| `config_version` | Required integer `1`. |
| `job.id` | Required ID: 1–128 letters, digits, underscores, or hyphens. |
| `application.name` | Required `bisq`, `electrum`, or `wasabi`. |
| `application.data_dir` | Required absolute or `~/` account-home path. Runtime validation requires an existing directory owned by the execution account. |
| `application.matcher.entrypoints` | Required nonempty list of `{kind, value}` mappings; duplicates are errors. |
| `application.matcher.exe_realpath` | Optional absolute or `~/` executable path predicate. |
| `application.matcher.argv_tokens` | Additional exact mandatory tokens; default `[]`. |
| `application.matcher.cmdline_regex` | Optional valid regex, at most 200 characters; must fully match at least one argument token. |
| `application.matcher.env_eq` | Exact environment predicates; default `{}`. Electrum allows `ELECTRUMDIR`, Wasabi `WASABI_DATADIR`, Bisq none. Values compare literally, without path canonicalization. |
| `action.argv` | Required nonempty string list. First element is an absolute or `~/` executable path. Other arguments may be empty and are never split as shell words. |
| `trigger.enabled` | Boolean; default `false`. |
| `trigger.poll_seconds` | Positive finite number; default `2`. |
| `trigger.confirm_seconds` | Positive finite number; default `5`. |
| `trigger.deadline_seconds` | Optional positive finite advisory deadline; default `null`. Will report overdue work without killing it. |
| `trigger.params` | Default explicit runtime parameter mapping; default `{}`. |
| `desktop_notifications.command` | Notification executable; default `notify-send`. |
| `desktop_notifications.timeout_ms` | Positive integer; default `5000`. |
| `desktop_notifications.trigger.enabled` | Boolean; default `false`. |
| `external_notifier` | Optional `{command, timeout_seconds}` mapping. Command required; timeout defaults to `15`, positive and at most `3600` seconds. |
| `logging.level` | `error`, `warning`, `info`, or `debug`; default `info`. |
| `logging.include_command_output` | Boolean; default `false`. Log bounded action output at INFO in the worker journal when enabled; the configured log level still applies. Private per-attempt diagnostics are retained regardless. |

An entrypoint's `kind` is `script`, `jar`, or `java_main`. Script/JAR values use
absolute or `~/` paths; Java main classes use exact class-name tokens. Alternative
entrypoints have OR semantics; additional matcher predicates remain mandatory.
An executable predicate alone does not replace the required entrypoints list.

## Runtime parameters and paths

Repeat `--param NAME=VALUE` before `validate` to override `trigger.params`.
Names match `[A-Za-z_][A-Za-z0-9_]*`. Values are nonempty and contain only letters,
digits, `.`, `_`, `:`, `@`, `+`, or `-`, matching VaultKeep's restricted parameter
values. Duplicate CLI keys, unused parameters, and missing values are errors.

Plain `{NAME}` substitution is supported in data-directory paths, script/JAR
entrypoint paths, executable path predicates, environment-predicate values, and
action arguments. Java class names, token predicates, and regexes are literal.
Use `{{` and `}}` for literal braces in templated fields; attribute/index access,
conversions, and format specifications are rejected. There are no implicit built-in
placeholders. Substitution never invokes a shell.

For example, with `action.argv: [/usr/bin/printf, '%s\n', '{NETWORK}']`:

```sh
app-watch --config /absolute/path/to/watch.yaml \
    --param NETWORK=testnet validate
```

Application and matcher paths are canonicalized. The action executable path
preserves symlinks so a virtual-environment interpreter retains its environment.
`~/` uses the operating-system account home, regardless of the shell's `HOME`.
`$HOME`, command substitutions, wildcards, and other shell expressions are not
expanded. Relative paths are rejected.

## Validation limits

`validate --schema-only` checks document structure and accepted settings without
resolving runtime templates or checking accounts/files. It does not accept CLI
`--param`; use normal validation to check effective parameter values.

Normal `validate` resolves settings and checks the data directory's ownership and
the action executable's availability. For an enabled watch it also checks the
worker template and manager, managed-service enablement, fresh monitoring evidence
and dispatch warnings. Missing/incomplete setup reports `SETUP INCOMPLETE` and
returns 2; schema/path errors and usage errors also return 2. Validation does not
run the action or certify wallet writer coverage. These commands require `--config`.

## Foreground monitoring

The implemented command is:

```text
app-watch --config PATH [--param KEY=VALUE ...] monitor
```

It requires `trigger.enabled: true`, non-root execution as the application owner,
a private account-owned `XDG_RUNTIME_DIR`, a reachable systemd user manager, and
a compatible `app-watch-exec@.service` template. The template shipped under
`systemd/` uses `/usr/local/bin/app-watch`; a development pip installation does
not install it. Use the [software installer](../README.md#requirements-and-installation);
per-watch management is documented in the [CLI reference](cli.md).

The monitor reads configuration once; stop/restart is required for edits. SIGINT
and SIGTERM stop monitoring without cancelling submitted workers. There is one
monitor lock per canonical configuration path, including invocations with different
parameters. A monitor started while the application is already stopped creates
no action. Polling can miss a complete application lifecycle between samples.

Worker selection is internal to systemd and reads the durable frozen request,
not the current YAML file. There is no public manual-run or replay command.
See [recovery](recovery.md) for state and command-execution limits.

[User guide](../README.md)
