# Security

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/prudhviy99/omarchy-parametric-eq/security/advisories/new).
Do not include credentials, private configuration, or exploit details in a
public issue.

## Permission boundary

The plugin runs unsandboxed as the current user inside `omarchy-shell`. It does
not use the network, `sudo`, or `pkexec`. Its controller reads and writes only
the plugin state directory and its dedicated WirePlumber fragment, reads
`~/.config/omarchy/shell.json` only to confirm a real disable/removal, reads an
AutoEQ file only when the user supplies a path, calls local PipeWire tools, and
may restart `wireplumber.service` through the current user's systemd manager.

The controller refuses to overwrite or remove an unrecognized or symlinked
WirePlumber fragment. Failed graph rebuilds restore the previous managed
fragment before returning an error.

Persistent state is stored in a private, user-owned directory. The controller
locks the opened directory descriptor, reads `state.json` through a bounded
no-follow file descriptor, rejects non-regular or multiply-linked files, and
uses descriptor-relative atomic replacement for writes. Output from local
commands is read incrementally with a fixed combined limit; an oversized
producer is terminated.

The exact paths and removal procedure are documented in [README.md](README.md).
