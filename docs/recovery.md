# Command execution and recovery

Software maintenance and per-watch lifecycle commands preserve recovery evidence
and refuse changes across active, pending, blocked or unreconciled work.

## Lifecycle

The monitor needs verified running evidence before it can create a shutdown
request. Continuous reliable absence must then last `trigger.confirm_seconds`.
Ambiguous or inaccessible metadata resets the confirmation interval. A restart
clears pending work and arms the next lifecycle. Confirmation uses monotonic time;
wall-clock changes do not shorten the running monitor's delay.

One watch permits one active attempt and one latest pending shutdown request.
Independent watches have separate execution slots. An action already running is
not stopped by a later wallet restart; App-watch cannot enforce offline data
access throughout an arbitrary external command.

The worker rechecks the frozen application identity before running the frozen
argv. The environment contains the account home, validated XDG paths, a fixed
safe PATH, and LANG when present. The working directory is the account home.
Arbitrary shell/service environment variables are not inherited. Use an explicit
wrapper executable if a command needs other setup. Request snapshots freeze
settings, not the contents of an executable or its dependencies.

Commands should run synchronously: a worker records the configured command's
exit, not the successful completion of background work that command launches.
Output pipes inherited by background descendants cannot keep diagnostic capture
open indefinitely after the command exits. Stored diagnostics are marked truncated
when inherited pipes must be closed.

## Recovery evidence

Recovery uses the execution account's operating-system home, independent of
`HOME` and `XDG_STATE_HOME`:

```text
~/.local/state/app-watch/jobs/<execution-identity>/
  instances/<job-id>.json
  blocked/<job-id>.json
  attempts/<attempt-uuid>/
    request.json
    launch.json
    execution.json
    receipt.json
    log.txt
    worker.lock
```

The execution identity binds canonical config path, job ID, and effective runtime
parameters. Monitor exclusivity instead binds only the canonical config path at
`$XDG_RUNTIME_DIR/app-watch/jobs/<path-hash>/monitor.lock`. While held by a monitor,
this lock contains its session start time. Status requires observations and
shutdown confirmations from that session; the marker is cleared on orderly exit.
Pre-restart observations and confirmation history do not establish current
application status, even when still recent.

Directories are private (0700), records are private (0600), and record writes use
atomic replacement with file and directory fsync. Requests, execution claims and
receipts cannot overwrite an existing record. Unsafe ownership/permissions,
symlinks in the recovery subtree, malformed records, contradictory associations,
and unknown schema versions block execution. Corrupt instance evidence is
preserved; a separate blocked diagnostic records the reason.

`launch.json` proves only dispatch intent. `execution.json` is an irreversible
worker claim written before final observation and command execution. An existing
claim without a receipt never authorizes another execution, even if the worker
crashed before spawning its command. This conservative boundary avoids replaying
an action whose effects are unknown. Exactly-once external effects cannot be
guaranteed across crashes.

Before claiming execution, a worker must also match the monitor's persisted active
attempt/request association. It reads the request again under its exclusive lock.
This prevents a late worker start from executing completed history while cleanup
is removing old claims and receipts.

Receipt success means exit zero. Other exits, including 10, and signals are
failures. A failed or uncertain action is never automatically rerun. A worker
blocked by a final running/uncertain observation writes a blocked receipt without
executing. An existing receipt is immutable, and a second worker invocation
returns that receipt rather than executing again.

Monitor restart preserves active work. Recovered pending requests need a fresh
full confirmation interval. An execution slot remains occupied until a receipt
exists and systemd confirms that both the worker and its queued job are gone.
Unreachable or incomplete manager evidence keeps the slot occupied. Terminal or
absent workers without receipts produce a persistent recovery block.

Only a missing durable launch intent or a failure to spawn the local systemctl
process proves non-submission. Such requests may be retried after reconfirmation
and a minimum 30-second delay. That delay is persisted across monitor restarts.
Other systemctl failures, including numeric codes 4/5/6, remain uncertain.

Monitor manager calls run through a bounded background queue so slow submissions
or queries do not stop process observation. Absence probes wait for any in-flight
start result and share one 15-second budget, including fallback listings. Stopping
the monitor cancels queued manager requests; an in-flight request keeps its normal
bounded timeout. If a start's final result was not durably collected before exit,
recovery retains uncertainty rather than assuming that cancellation prevented it.

Combined stdout/stderr diagnostics retain at most the last 256 KiB. Cleanup
targets the latest 50 completed attempts, retaining older protected records while
workers, queued jobs, pending requests or unavailable manager evidence prevent
safe removal. More than 70 retained completed references blocks further dispatch
for review. Active and unresolved attempts are preserved.

## Inspecting a blocked watch

Preserve the instance file and its attempt directory. Read the blocked diagnostic,
request, launch intent, execution claim, receipt if present, and worker journal
together. Raw command output may contain private data; review it before sharing.
Changing the YAML file does not resolve uncertainty about an earlier command's
effects. There is no automatic repair or public replay command.

Advisory deadlines report overdue attempts once without terminating the command.
Stopping the foreground monitor does not cancel workers, but worker independence
does not guarantee completion across logout, user-manager shutdown or host reboot.
Deployment evidence and remaining validation gates are tracked in the
[implementation plan](plans/implementation.md).

[User guide](../README.md)
