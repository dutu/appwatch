# Monero trigger implementation plan

Implement the [Monero trigger design](../design/monero-trigger.md) after approval.
Keep changes uncommitted. The design defines behavior; this file gives the work
order and validation required before reporting completion.

## 1. Native executable alternatives

Extend `EntrypointConfig` in `src/app_watch/config/models.py` with
`executable_name`. Extend identity matching in `src/app_watch/process.py` to
compare that kind with the basename of the reported actual
`ProcessInfo.exe_realpath`, preserving OR semantics and returning unavailable
when an otherwise relevant executable cannot be inspected. Do not use process
names, `argv[0]`, or arbitrary argv tokens as executable identity.

Handle Linux ` (deleted)` executable markers conservatively before declaring a
basename mismatch. Follow the design's unavailable-result rule for potentially
matching marker-bearing targets and normal matching for provider-normalized
targets. Do not require the on-disk executable to exist or resolve its reported
old pathname through a replacement file/symlink. Check the production provider's
behavior as well as the matcher; do not rely only on fake-provider coverage.

Update `src/app_watch/config/__init__.py` so executable names remain literal
through preparation rather than being expanded or resolved as paths. Validate
basename-only values and preserve them through fingerprints, frozen requests,
and worker reconstruction. Add Monero-specific validation requiring the two
known names and rejecting narrowing supporting predicates as specified in the
design. Keep all existing application matchers and their path expansion compatible.

## 2. Monero settings evidence

Add `src/app_watch/apps/monero.py` and register `monero` in
`src/app_watch/apps/registry.py`. Extend `ProcessProvider` and its production/fake
implementations only as needed for bounded, nonblocking regular-file inspection
with verified process context. Keep file parsing in the Monero adapter.

Implement GUI normal/XDG and portable settings selection, Qt scalar decoding,
RPC explicit configuration/direct-wallet selection, exact parent-directory
comparison, and conservative failure results. Use synthetic settings in tests;
do not copy actual wallet files or production configuration into the VM.

## 3. Shutdown guard integration

Add the small adapter policy in `src/app_watch/apps/base.py` and apply it in
`detect_spec`: for Monero, identity matches without a target-directory match
must prevent a stopped result. Existing adapters retain their behavior.

Reuse `trigger/lifecycle.py`, `trigger/monitor.py`, and `actions/worker.py`
without adding a second activation state or application-specific execution
path. Add behavioral tests demonstrating the existing latch, recovery,
confirmation, pending-request reconfirmation, and worker checks with the new
detection results. Adjust shared orchestration only if those tests reveal a
necessary integration defect; do not change generic retry semantics.

## 4. Operator documentation and example

Add `examples/monero-trigger.yaml` as a disabled template with both literal
executable names, an explicit wallet directory, and a harmless printf action.
The YAML must require neither versioned executable paths nor installation
symlinks. Describe how to verify executable names and wallet directories, how
wallet settings are discovered, the distinction
between activation and the process guard, intentional waiting on other wallets,
unsupported selection/deployment modes, upgrade review, and polling limits.

Update `README.md`, `docs/applications.md`, `docs/configuration.md`, and relevant
sections of `docs/development/testing.md`. Preserve existing structure and
verify affected relative links/anchors. Update validation evidence only with
results actually obtained.

## 5. Required automated coverage

Extend `tests/test_config.py`, `tests/test_matcher_detection.py`,
`tests/test_process.py`, and `tests/test_execution.py`; use a focused
`tests/test_monero.py` for settings fixtures if that keeps existing tests clear.
Update `tests/helpers.py` and other provider fakes affected by the new reader.

- Exact GUI/RPC executable-basename matches across directories and versions,
  basename-only RPC argv, either/both running, multiple same-user instances,
  another executable with the same basename, unrelated argv containing expected
  names/paths, spoofed or truncated process names, renamed executable rejection,
  executable symlinks, unreadable identity, PID reuse, and process disappearance.
- Deleted GUI and RPC executables with raw ` (deleted)` markers and already
  normalized provider paths, including an old pathname replaced with a different
  file/symlink and a clearly unrelated deleted executable. Verify uncertain
  candidates cannot arm by themselves, but preserve prior activation, reset
  confirmation, and block final worker execution. Confirm stopping one normal
  instance while a deleted instance survives cannot yield `STOPPED`. Cover
  provider and matcher behavior, monitor/pending-request handling, and worker
  revalidation; missing on-disk files must not be mistaken for process exit.
- Schema validation for both roles, duplicate alternatives, prohibited
  predicates, invalid executable-name paths/patterns/placeholders, literal-name
  preparation, existing path expansion, frozen requests, and the disabled example.
- GUI primary/XDG/default paths, process versus monitor environment, portable
  precedence and mode changes, absent/unreadable cwd/environment/files, unsupported
  fallback settings, and changes to `wallet_path` on the same PID across polls.
- GUI Qt quoting/escaping, RPC separated/equals arguments, explicit config,
  direct wallet selection, agreeing/conflicting sources, malformed/duplicate
  relevant options/keys, unrelated credential keys, relative paths, Unicode and
  spaces, symlink resolution, exact parent comparison, unsupported modes,
  oversized/nonregular files, decoding errors, and partial settings writes.
- Startup without target activation never dispatches, even after unrelated
  Monero processes stop. Switching A to watched B arms; switching B back to A
  does not release the guard. A surviving unrelated-wallet RPC/GUI blocks it.
- Once armed, missing/deleted/changed settings cannot cause premature shutdown.
  Confirm only after all matched processes exit; restart during confirmation
  resets the interval. Preserve activation through same-boot monitor recovery;
  reset correctly for configuration/boot changes.
- A confirmed pending request encountering another-wallet process must require
  fresh confirmation after that process exits. A worker encountering any
  configured Monero process must record a blocked receipt and not run the action,
  even with unreadable settings. Zero matching processes permit the worker check
  without requiring obsolete configuration files to remain readable.
- Existing Bisq/Electrum/Wasabi, lifecycle, worker, and status tests continue to
  pass; guard diagnostics are meaningful through existing status reporting.

## 6. Execution and acceptance

Run all project execution and dependency installation inside the disposable VM:

```sh
codex-vm start
codex-vm test -- .venv/bin/pytest -q tests/test_config.py tests/test_process.py tests/test_matcher_detection.py tests/test_execution.py
codex-vm test -- .venv/bin/pytest -q
codex-vm test -- .venv/bin/ruff check src tests scripts
codex-vm test -- .venv/bin/ruff format --check src tests scripts
codex-vm test -- .venv/bin/python -m build
```

Include any new focused test module in targeted execution. Create the VM's dev
environment using the [existing instructions](../development/testing.md) if it
does not exist. Use deterministic providers, temporary settings files, fake
clocks, and harmless actions for regression coverage.

Perform a disposable native v0.18.5.4 GUI/RPC smoke check in the VM with Xvfb as
needed, disposable wallets and isolated HOME/XDG/cwd. Verify actual executable
metadata, selected settings paths, GUI wallet switching and settings writes,
RPC explicit config selection, and running-to-stopped detection. Extend
`tests/live_wallet.py` only where necessary for these Monero checks. Keep wallet
creation data, application downloads and credentials in the guest, outside the
synchronized source tree; use no real funds or host profiles. Report any missing
live validation as a limitation rather than treating fixtures as proof of binary
compatibility. Leave the VM running.

Also run a Linux provider integration regression in the VM using a copied
harmless native executable with a configured Monero basename: start it, unlink
the copy while it remains alive, and verify detection/worker checks remain
blocked until it exits. Do not unlink installed wallet binaries. Exercise both
GUI and RPC names and isolate all helper paths and settings in guest temporary
directories.

Completion requires the agreed behavior, passing relevant regressions and full
suite, lint/format/package checks, current operator documentation, and an honest
record of live validation and remaining limits. Do not stage, commit, or push.
