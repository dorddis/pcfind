# Changelog

## 0.1.0 - 2026-10-19

First public release, extracted from a personal script that was the required search tool for
every AI coding agent on one Windows workstation.

- `text`: one ripgrep walk per root, `--no-ignore --hidden` by default, dead-zone directories,
  binary-extension and >20 MB skips, credential-filename skips, a filter-disclosure footer on
  every run, and a WARNING when a filtered search returns fewer than 5 matches.
- `name`: whole-PC filename search through Everything (`es.exe`) on Windows, with an 8 s IPC
  timeout, a 5-minute back-off marker while Everything rebuilds its index, stale-copy hiding,
  and a ripgrep traversal fallback that says it is not a whole-PC search.
- `doctor`: ranks top-level directories by the cost of the search `text` actually runs.
- Config file (`roots`, `deadzones`, `stale_copy_dirs`, `rg`, `es`); with no config the default
  root is the current directory.
- ripgrep discovery: config, then PATH, then copies bundled with Claude Code and VS Code.
- Output hardening: control bytes and bidi marks escaped, `--` before the pattern, binary-match
  notices suppressed for file roots and counted in the footer, `RIPGREP_CONFIG_PATH` ignored.

Changes against the private script it came from:

- Removed: hardcoded roots, machine-specific dead zones and stale-copy directories (now config).
- ripgrep is looked up on PATH before vendored copies. The private script did the reverse,
  because PATH[0] on that machine was user-writable. If that applies to you, set `rg` in the
  config file; `doctor` and `text --show-filters` print the binary actually used.
- The binary-notice filter is back. The private script had dropped it when it moved to one walk
  per root, because a walk never prints the notice; it still matters when `--root` is a file.
