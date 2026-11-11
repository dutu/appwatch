# Validation evidence

All execution for this validation ran inside the disposable Debian 13 VM through
`codex-vm test`. Host activity was limited to source inspection/editing, Git
inspection and VM orchestration. The host installer was not run. The sibling
VaultKeep working tree remains unchanged.

## Automated and package checks

- Python 3.13.5; systemd 257.13.
- 517 automated tests pass, including the real hash-locked environment build and
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

## Monero trigger validation

The Monero implementation passed the complete 517-test suite, Ruff lint and
format checks, wheel/source-distribution builds, and `pip check` inside the
disposable Debian VM. Coverage includes executable names across versions and
directories, settings changes on a continuing PID, remembered activation,
pending reconfirmation, exclusion of spoofed RPC argument zero, UTF-16 surrogate
pair decoding, in-place settings mutation, and unavailable settings at the final
worker check. Real Linux regressions unlink
copied harmless native executables named for both GUI/RPC while they remain
alive and verify that detection and worker execution stay blocked.

`tests/live_monero.py` also passed against official native GUI/CLI v0.18.5.4
distributions downloaded into the guest, using the full production scan and
installed helper with no PID filtering. It generated two disposable offline
wallets and isolated HOME/XDG/cwd, then verified GUI normal settings, GUI portable
settings taking precedence over a conflicting normal file, and RPC explicit
`--config-file` and direct `--wallet-file` selection. It unlocked the first wallet
and switched the continuing GUI process to a second wallet whose directory and
filename contain spaces, `é`, and an emoji. It then read back Qt's actual emitted
`wallet_path`, including escaped UTF-16 surrogate pairs and the selected `.keys`
suffix, and verified detection for both the new directory and the still-blocked
original directory. Each launched process was detected running and
then absent after termination. Wallet data and logs remained only in guest
temporary directories; no existing wallets, host profiles, or funds were used.

The production provider uses the installed read-only privileged helper to resolve
executable paths hidden by protected same-user session processes (`systemd`,
`sd-pam`, SSH). `tests/live_inspection.py` passed with an unrestricted scan: a
deliberately non-dumpable helper named `monero-wallet-rpc` armed the real monitor,
blocked execution while alive, and allowed a successful unprivileged worker
receipt after exit. It also verified caller-only results and rejected argument
and `SUDO_UID` overrides using an account without general sudo rights. Unit
coverage checks PID reuse, exact start times, process exit during inspection,
deleted paths, malformed output and timeouts. Installer tests cover helper/policy
ownership, legacy-layout upgrades, rollback and removal. Unverified executable
identities still block confirmation rather than being ignored.

Native GUI automation uses the fixed English v0.18.5.4 layout; other
versions/layouts may require changes to the test coordinates. These checks do
not certify backup consistency or close the race between final inspection and
a wallet restarting.

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
