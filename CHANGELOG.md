# Changelog

All notable changes to this project are documented here.

## 1.0.1 - 2026-08-24

- Hardened persistent state access with descriptor-relative, bounded,
  no-follow reads and atomic private writes.
- Replaced the predictable lock file with a lock on the private state
  directory itself.
- Bounded combined subprocess output and terminate producers that exceed the
  limit, preventing command output from exhausting controller memory.
- Added regression coverage for symlink, hard-link, FIFO, oversized-state, and
  oversized-command-output attacks.

## 1.0.0 - 2026-08-23

- Initial public release.
- Added ten-band parametric EQ with seven filter shapes and live PipeWire
  controls.
- Added functional preamp, automatic headroom, response modeling, presets,
  undo, A/B snapshots, and AutoEQ import.
- Added explicit master and per-band state, exact numeric entry, auto-save, and
  commit-on-band-switch behavior.
- Added safe user-level installation, recoverable removal, isolated tests, and
  live PipeWire integration checks.
- Added automatic graph teardown on standard Omarchy disable/removal, guarded
  fragment ownership, failed-rebuild rollback, and verified master bypass.
- Added deterministic headless QML tests and lifecycle safety regressions.
