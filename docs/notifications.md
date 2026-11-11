# Notifications

App-watch has independent desktop notifications for trigger/action events and an
explicit external-notifier command. It does not automatically send lifecycle
events through the external notifier. Neither delivery path changes action
success or authorizes replay.

## Desktop delivery

Enable delivery in a watch:

```yaml
desktop_notifications:
  command: notify-send
  timeout_ms: 5000
  trigger:
    enabled: true
```

The default command is provided by `libnotify-bin` on Debian; bus discovery uses
`busctl` from systemd. Delivery needs an accessible same-user graphical notification
service. App-watch checks local Unix-socket peer ownership, prefers a working
configured/user bus, and otherwise requires an unambiguous desktop bus. It does
not import another session's environment into systemd or create a new D-Bus bus.

Notifications cover confirmed shutdown, command submission, success/failure,
blocked final checks, recovery blocks, advisory overdue work and pending-request
supersession. Bodies include the watch ID and event, without raw output or command
arguments. The default notify-send command replaces this watch's existing update
where possible. A custom command receives title and body as separate arguments.

Delivery runs outside the polling loop with per-command timeouts, ordered updates
and a queue capped at 64 pending messages. Superseded progress is collapsed;
overflow drops the oldest pending update with a warning. Shutdown gives the
notifier a bounded drain budget. Missing sessions, failures and timeouts are
logged and never fail the action or monitoring loop. Delivery is best-effort,
without guaranteed delivery or replay after a crash. Startup of an already blocked
monitor logs its existing recovery problem instead of repeatedly alerting it.

## Explicit external notifier

Configure one executable:

```yaml
external_notifier:
  command: telegram-notify
  timeout_seconds: 15
```

It must be on PATH or specified by an absolute executable path. The value is one
executable, not a shell command string. App-watch neither configures that tool's
credentials nor selects its recipients.

Configuration validation rejects arguments, whitespace (including spaces in
paths), shell syntax, relative paths such as `./tool`, directory paths ending in
`/`, and names beginning with `-`. Supply arguments to `notify` instead:

```sh
app-watch --user --config /absolute/path/to/watch.yaml notify --level info 'Wallet event'
```

Every argument after `notify` is forwarded unchanged and in order, including
`--help`, `--config`, empty strings and arguments containing spaces. They are not
parsed as App-watch options and never undergo shell expansion. NUL characters are
rejected. The watch must have valid YAML and an `external_notifier`; the explicit
call does not require wallet directories or monitor readiness.

The external command runs with its normal inherited command environment, with
stdin/stdout/stderr disconnected. Timeout terminates its process group, including
descendants. Failures warn without changing the command's zero result; arguments
and provider stderr are omitted from diagnostics. The tool's own argument contract
and recipient configuration remain its responsibility.

[User guide](../README.md)
