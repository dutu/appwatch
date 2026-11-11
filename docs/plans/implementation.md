# App-watch implementation plan

Implement the [project specification](../design/app-watch.md) in the following
order. The package, configuration/preparation, app adapters, process detector,
durable lifecycle, bounded command runner, worker, foreground-monitor CLI and
systemd adapter, managed service lifecycle, status/listing and notifications are
implemented and covered by tests. The transactional installer, operator guides
and live deployment checks are implemented. Results and precise deployment
boundaries are recorded in [validation evidence](../development/validation.md).
Keep changes in this repository, uncommitted, and preserve existing IDE changes.

## 1. Package, configuration, and application adapters

Implemented. The CLI provides `--version` and `validate`; runtime
validation checks configuration and paths, plus enabled-monitor health. Tests cover
ported process contracts and the trigger-only schema/parameter boundary. Package
build and standalone-wheel checks are described in the development guide.

Create the `app_watch` package, `app-watch` console entrypoint, project metadata,
development configuration, and required license attribution. Adapt strict YAML
validation and runtime parameter preparation without importing VaultKeep.

Extract shared process inspection/matching from `application.py`. Put Bisq,
Electrum, and Wasabi directory rules into independent adapters behind a small
static registry. Add one disabled example per app, containing only watch settings
and a harmless illustrative external action.

Verify actual installed dependencies and package metadata. Port the relevant
matcher, process, configuration, and ownership tests. Cover all three adapters,
multiple instances, option precedence, relative paths, metadata access failures,
PID replacement, alternative entrypoints, and invalid configuration. Prove that
application-specific branching does not remain in monitor or worker code.

## 2. Durable lifecycle and external action execution

Implemented and tested with real harmless commands and an injected systemd
controller. Recovery covers frozen requests, irreversible worker claims,
monitor-independent receipts, pending reconfirmation, proven non-submission retry
delays, conservative unknown outcomes, corrupt-state isolation and bounded
completed history. Foreground monitoring and the internal worker CLI are wired;
the compatible worker template is installed by the software installer, not by
development pip. Live worker independence and receipt reconciliation have also
been exercised in the disposable VM.

Adapt lifecycle, models, persistence, monitor, bounded subprocess capture, and
worker receipts. Replace backup job snapshots with prepared action snapshots.
Remove backup-specific result classification, version coupling, and retries.

Test startup while stopped, running-to-stopped confirmation, restart during the
delay, ambiguity resetting the delay, configuration/boot changes, pending
supersession, same-watch serialization, and independent watches. Exercise argument
arrays with spaces and shell metacharacters using a harmless real helper process;
confirm they are passed literally and run only after stopped revalidation.

Verify exit zero/nonzero/signals, missing executable, bounded output, advisory
overdue reporting, explicit environment, frozen settings, atomic state writes,
corrupt-state isolation, receipt recovery, and no automatic replay after uncertain
execution. Retain source recovery tests only after adapting their assumptions to
generic commands.

## 3. User services, operational CLI, and notifications

Implemented under controlled process/manager tests: service lifecycle and safe
failure rollback, generated-unit verification, installed-identity guards,
configuration-independent disable/remove, read-only listing/status, health
validation, desktop delivery and explicit external notification. A bounded
background manager adapter keeps observation responsive during slow requests and
defers absence evidence until in-flight starts are collected. Its probes share a
single deadline. Real unit-manager behavior and desktop delivery have also been
exercised in the disposable VM.

Adapt monitor install/update/disable/remove, worker template, systemd querying,
locks, and recovery identity. Remove dependencies on backup workflow locks and
schedule-status modules. Wire foreground monitoring and signal handling.

Implement validation, detailed and JSON status, configured-watch listing, and
trigger notifications. Adapt external notifier support independently of backup
hooks. List healthy, disabled, invalid, and missing-config installed watches.

Verify duplicate monitor refusal, identity changes, stale monitor observations,
malformed/missing YAML recovery, lifecycle operations during active work,
monitor/worker separation, notification failure isolation, manager timeouts, and
unknown submission outcomes. Ensure no generic command exit code is treated as a
systemctl submission result.

## 4. Installation and update lifecycle

Implemented with hash-locked environments, manifest ownership verification,
release staging, global/runtime coordination, owner-user maintenance helpers,
safe activation/rollback and interrupted-transaction recovery. Controlled tests
cover failure and corruption cases; live checks cover real installation,
monitor refresh, post-activation rollback and uninstall preservation.

Adapt VaultKeep's installer and maintenance helpers to the trigger-only layout.
Remove archive package provisioning, backup services, timer handling, and legacy
VaultKeep migration paths. Preserve release staging, manifest ownership checks,
verification, activation, rollback, safe update checks, managed-monitor refresh,
and preservation of operator configuration and recovery state.

Use isolated disposable installation roots for installer tests. Verify fresh
install, dry-run, reinstall/update, validation failure before activation,
activation/refresh failure and rollback, custom paths, uninstall, optional cleanup,
and modified-file protection. Verify that no operation touches VaultKeep paths or
units and that installing software alone starts no monitor.

## 5. Operator documentation and deployment verification

The README is the operational guide, with separate configuration, CLI,
application setup, notification and recovery references. Test instructions
require disposable-VM execution. Live systemd, notifier and installed wallet
process checks cover all five initial launch models: Bisq desktop/API daemon,
Electrum client and Wasabi GUI/daemon. Results are recorded separately in the
validation evidence. All implementation and deployment gates below have been
exercised in the disposable VM; operator-specific matchers still require local
verification.

Replace the early-development README with the operational guide once commands
exist. Document requirements, installation, configuration, validation, enabling,
health checks, daily operation, troubleshooting, updating, and uninstallation in
that order. Add full configuration/CLI references and app-specific setup guidance
where detail warrants separate documents. Keep the design and plan separate from
user procedures. Verify relative links and command examples.

Run a disposable live systemd user-manager deployment with harmless external
commands. Exercise real monitor stop/restart while a worker is running, queued
and activating workers, terminal/absent units, receipts, manager failures, and
update refusal during active work. Verify rejection diagnostics and codes on the
deployed systemd version; numeric codes alone do not prove non-submission.

Validate real process identity and directory evidence for each supported installed
wallet model, including configured GUI and daemon entrypoints. If a required
environment or wallet is unavailable, record the precise unverified gate instead
of claiming production readiness from mocked tests.

## Completion evidence

| Requirement | Evidence required |
| --- | --- |
| Independent trigger-only app | Built/installed package runs without the sibling checkout or installed VaultKeep; no backup/schedule commands, schema sections, tools, or runtime imports. |
| Bisq, Electrum, Wasabi | Adapter tests preserving source behavior, disabled examples, operational guidance, and installed-process validation for supported launch models. |
| Modular application support | Independent adapter files and static registration; lifecycle/action/service code has no wallet-specific branches. |
| External command actions | Real harmless subprocess tests for literal argv, exit/signal handling, frozen settings, and conservative recovery. |
| Configuration and settings | Strict schema and semantic tests, runtime parameter tests, useful invalid-config diagnostics, and documented effective settings. |
| Monitoring and recovery | Lifecycle, persistence, isolation, concurrency, restart, and uncertain-execution tests plus live worker/monitor exercise. |
| Listing, validation, status | CLI tests for configured and installed watches, invalid/missing configs, stale observations, action state, and recovery warnings. |
| Notifications | Delivery tests, failure isolation, and deployment checks for configured notifier/session behavior. |
| Install/update/uninstall | Isolated installer integration tests plus live installed monitor refresh and rollback verification. |
| Operator usability | README procedures match executable commands; references, links, examples, and troubleshooting verified. |

Passing unit tests alone does not satisfy the live deployment gates. The project
goal remains active until the full required behavior is implemented and verified.
