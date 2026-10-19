# How pcfind works

The detail behind the README: what each default does and why, config, the speed numbers, and the security model.

## The problem

Three things go wrong when you (or an agent) search a real working machine, and none of them
announces itself.

**Gitignore-honouring tools silently miss your own files.** ripgrep, `git grep`, editor search
and most agent "Grep" tools skip gitignored paths by default. On the machine this was built on,
that hid 87% of the code tree: build caches, local notes, session logs, drafts, scratch dirs,
nested repos that a parent repo ignores. Those are often exactly what you are looking for. A
zero from such a tool cannot be told apart from a true zero, so the miss is invisible.

**On Windows, every file opened costs an antivirus scan.** Microsoft Defender scans on open.
Measured on that machine: a cold first open cost 8-12 ms per file (11.6 ms in the cleanest
test), a warm repeat 0.8 ms, and Microsoft-signed system files 1.9 ms. Search cost scales with
files opened, not bytes read; ripgrep itself ran at 1.8 GB/s on a single large file. So the win
is opening fewer files that cannot match: images, archives, compiled output, vendored
dependencies, multi-hundred-MB captures. Plain `grep -r` from Git Bash did not finish a search
of one project tree in 300 s.

**Copies multiply hits.** A super-repo with 239 git worktrees returns every match a few hundred
times; a backup mirror of the code tree returns every file twice. In the benchmark below, the
control arm reported 165 and 194 duplicate paths on two super-repo questions.

On top of that, agents read a short result as a complete one. In the benchmark, twice an agent
got 0-1 hits from a filtered search and reported "confidence: high" while the answer sat in a
skipped file.

## What pcfind does about it

- `--no-ignore` and `--hidden` are on by default: gitignored and dot-paths are searched.
- **Dead zones**: a short list of directory names that are never the answer (`node_modules`,
  `.git`, `.venv`, `site-packages`, `__pycache__`, tool caches, `worktrees`, `_backups`, `target`,
  volume-root system dirs) is pruned without being opened. Add your own in a config file.
  A `--root` inside a dead zone lifts it, and the footer says so.
- **Binary extensions and files over 20 MB are skipped**, matched case-insensitively (`*.pdf`
  alone lets `SCAN.PDF` through). ripgrep checks the size cap against directory-listing
  metadata, so an oversized file is never opened.
- **Credential-shaped filenames** (`.env`, `*.pem`, `id_rsa*`, `.ssh/`, `.npmrc`, ...) are skipped
  by `text` by default, because the two defaults above would otherwise make them ordinary
  content. `name` does not skip them: a path is not a secret.
- **Every run discloses its filters** in a footer on stderr, including when it found matches.
  When a filtered search returns fewer than 5 matches it also prints a WARNING that the result
  does not show absence.
- **One ripgrep walk per root, 16 threads.** Opens wait on the scanner, so more in flight hides
  more latency (measured: `-j 4` 64/41 s, default 34/27 s, `-j 16` 21/14 s on one walk).
- **`name` uses Everything's NTFS index** on Windows: whole-drive filename search in well under a
  second. If Everything is missing, hung or rebuilding (8 s IPC timeout, then a 5-minute
  back-off), it falls back to a ripgrep traversal of the default roots and says that this is NOT
  a whole-PC search. Hits under backup trees are hidden by default and counted.
- **Output is treated as hostile.** File content is attacker-controlled text that may end up in
  an agent's context. Control bytes and bidi marks are escaped, the pattern always follows `--`
  (so `pcfind text -- --help` searches for `--help`), `RIPGREP_CONFIG_PATH` is ignored, and the
  `path:line:` prefix cannot be forged by a `\r` inside a line.
- **Honest exit codes**: 0 matches, 1 clean run with no matches, 2 anything failed or was
  degraded (bad regex, unreadable root, timeout, missing root). A failure never exits 1.

Example footer:

```
-- 3 matching lines in 0.42s across 1 root(s)
-- filters active: 21 dead-zone dirs skipped; 25 credential filename patterns skipped
   (--include-secrets to read them); 69 binary extensions and files >20M skipped. Re-run with
   --deep, --include-secrets and --no-deadzones to widen.
-- WARNING: only 3 match(es) and the filters above were active. This does NOT show the pattern
   is absent from skipped files. Before reporting it absent or complete, re-run with --deep
   --include-secrets.
```

## Config

Optional. Default location `%APPDATA%\pcfind\config.toml` on Windows,
`$XDG_CONFIG_HOME/pcfind/config.toml` (else `~/.config/pcfind/config.toml`) elsewhere; override
with `PCFIND_CONFIG` or `--config`. See [examples/config.toml](examples/config.toml).

```toml
roots = ["~/code", "~/notes"]          # default roots; without this, the current directory
deadzones = [".devcache", "*-wt-*"]    # extra directory names or globs to skip
stale_copy_dirs = ["projects-backup"]  # `name` hides Everything hits under these
# rg = "C:/tools/rg.exe"               # pin binaries instead of discovering them
# es = "C:/Program Files/Everything/es.exe"
```

Python 3.9-3.10 needs `tomli` to read the config; `pipx install` pulls it in.

## Speed: one walk vs batched search

Speed of the tool itself, before and after moving from "enumerate files, then search them in
batches of explicit paths" to one ripgrep walk per root (two runs each, same hit totals):

| search | batched | one walk | hits |
|---|---|---|---|
| literal, super-repo (87k files) | 101.6 / 143.8 s | 10.8 / 7.9 s | 74 |
| literal, repo with vendored reference clones | 243.9 / 96.7 s | 8.7 / 8.9 s | 7 |
| `TODO`, super-repo, `--limit 50` | 46.5 / 53.9 s | 4.7 / 5.5 s | 10,161 |
| literal, tool-config dir with 8.6 GB of transcripts | 11.6 / 9.9 s | 3.6 / 4.1 s | ~29.3k |
| case-insensitive word, notes repo | 1.5 / 1.7 s | 0.31 / 0.31 s | 733 |
| literal, one code repo | 3.8 / 2.4 s | 2.8 / 2.6 s | 0 |
| literal, whole code root (383k files) | 87.6 / 132.3 s | 98.0 / 150.3 s | 4 |

The batched design came from an earlier belief that ripgrep opens a file to apply
`--max-filesize` during a walk. ripgrep's source (`crates/ignore/src/walk.rs`, 13.0.0 and 14.1.1)
reads the size from directory metadata, and a 1.44 GB test file confirmed it within noise. What
the batching actually cost was process spawn under load: 481 batches for the super-repo. The
whole-code-root row is flat because that search had already been falling back to a single walk.
Whole-root searches stay around 100 s on that loaded machine because they read ~380k files.

## Security notes

Scope is not a security boundary: `--root` reads anything the caller can read, `name` sees the
whole drive through Everything, and the exit code alone tells you whether a string exists.
Everything pcfind reaches, its caller could already read directly. The secrets skip matches
filenames only; it is a guard against dumping credentials into an agent's context by accident,
not a data-loss-prevention tool.

pcfind writes nothing except stdout, stderr and one marker file
(`%LOCALAPPDATA%\pcfind\es_timeout` or `~/.cache/pcfind/es_timeout`) used for the Everything
back-off.

