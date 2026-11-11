# Validation evidence

All execution for this validation ran inside the disposable Debian 13 VM through
`codex-vm test`. Host activity was limited to source inspection/editing, Git
inspection and VM orchestration. The host installer was not run. The sibling
VaultKeep working tree remains unchanged.

## Automated and package checks

- Python 3.13.5; systemd 257.13.
- 346 automated tests pass, including the real hash-locked environment build and
  installed command invocation outside the checkout.
- Ruff lint and format checks cover `src`, `tests` and `scripts`.
- Wheel and source distribution build successfully. The installed package has
  no VaultKeep dependency or sibling checkout in the VM.
- Dependencies pass `pip check`. Hatchling's packaging dependencies are included
  in the installer's hash lock; pathspec is a build dependency, not a backup
  runtime requirement.
- Regression coverage rejects invalid external-notifier executable forms before
  delivery and distinguishes stopped observations from current-session shutdown
  confirmations. Session-change checks reject recent pre-restart observations.

## Live deployment

The opt-in checks in `tests/live_systemd.py` use real user services and harmless
helper processes. They verify startup while stopped creates no action; running
then confirmed shutdown executes literal arguments; active work refuses software
and watch updates; a worker continues while its monitor stops/restarts; success
and exit-10 failure receipts reconcile; failures do not replay; notification and
explicit notifier delivery work; software refresh preserves enabled/active flags;
and a stopped/disabled watch stays stopped/disabled. Status reports `NOT RUNNING`
at stopped startup/after restart and `HAS STOPPED` only after a confirmation in
that monitor session. Removal works with missing
YAML. The test invokes `live_rollback.py` to inject one failure after real user-unit
refresh and verify restoration of the release, manifest and monitor health.

`tests/live_manager.py` exercises an actual queued worker start behind a harmless
ordering barrier, then verifies terminal failure when no durable request exists.
It verifies absent-unit evidence and unreachable-manager behavior using only
process-local environment changes. A missing-unit start on this systemd version
returns exit 5 and is classified as inconclusive; numeric codes do not establish
non-submission.

`tests/live_desktop.py` runs `notify-send` against a real same-user D-Bus
notification daemon (dunst with Xvfb), verifies App-watch's bus discovery and
checks delivered notification history. This verifies the delivery path, not a
particular operator's desktop session.

`tests/live_uninstall.py` checks default uninstall and receipt purge, custom
installation paths, actual custom install/update/uninstall, and byte-for-byte
preservation of retained user configuration and recovery files. It restores the
default installation for inspection.

## Installed application process checks

`tests/live_wallet.py` launches real applications in new disposable data
directories, checks their live identity and directory evidence, then checks
stopped detection after process-group termination. GUI cases use Xvfb. No
existing wallets, private-network services or host profiles are used.

| Model | Observed version and result |
| --- | --- |
| Electrum GUI client | Debian `4.5.8+ds-6`; `/usr/bin/electrum --offline -D PATH`; running/stopped checks pass. |
| Bisq 1 desktop | Upstream `1.10.11` Linux package; native `Bisq --appDataDir=PATH`; running/stopped checks pass. |
| Bisq 1 API daemon | Built upstream tag `v1.10.11` with its submodules using `:daemon:installDist`; Java main `bisq.daemon.app.BisqDaemonMain` with explicit `--appDataDir`; running/stopped checks pass. |
| Wasabi GUI | Upstream `2.8.5`; native `wassabee --datadir=PATH`; running/stopped checks pass. |
| Wasabi daemon | Upstream `2.8.5`; native `wassabeed --datadir=PATH`; running/stopped checks pass. |

Sources: [Bisq release](https://github.com/bisq-network/bisq/releases/tag/v1.10.11),
[Wasabi release](https://github.com/WalletWasabi/WalletWasabi/releases/tag/v2.8.5).
These are process-identity checks, not wallet synchronization, trading,
coinjoin, backup-consistency or funds-handling tests. Matchers remain specific to
the installed launch model and must be checked locally after wallet upgrades.

## Reproducing checks

See [testing commands](testing.md). Run the real service check before the
uninstall check so there are retained records to inspect. Run desktop and manager
checks while the software is installed. Install `dunst`, `xvfb` and `xauth` only
inside the disposable guest for the desktop check. The rollback helper runs as
root only inside the VM and deliberately fails one installer refresh.

The wallet helper accepts `--application NAME --data-dir NEW_PATH --entrypoint
KIND=VALUE -- COMMAND...`. The directory must not exist; include the matching
explicit directory argument in the launch command. Download/extract wallet
packages inside the guest, outside the synchronized source tree. Keep logs and
disposable data there. Leave the VM running after validation.

The Bisq Linux desktop package does not contain its API-daemon distribution. For
that check the guest built the official release tag with initialized submodules,
a full JDK and the upstream Gradle wrapper. All downloaded dependencies and
build outputs stayed in the guest. The helper keeps `KIND=VALUE` in one argument
so its own command line cannot masquerade as a Java-main entrypoint.
