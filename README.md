# pcfind

Search your whole machine for a string or a filename, including the files your other tools quietly skip. And it tells you what it skipped.

```
pcfind text "parse_invoice"            # file contents
pcfind name "*invoice*.pdf"            # filenames, whole PC on Windows
pcfind doctor                          # which folders make your searches slow
```

A thin wrapper around [ripgrep](https://github.com/BurntSushi/ripgrep), plus [Everything](https://www.voidtools.com/) on Windows for filenames.

## Why

AI coding agents search a lot, and on a busy Windows machine their searches go wrong in three quiet ways:

- **They miss files.** Most search tools skip gitignored paths: notes, logs, drafts, build output, nested repos. On the machine this was built on, that was 87% of the code tree. The tool returns zero and you can't tell it apart from a real zero.
- **They're slow.** Windows Defender scans every file you open, about 10 ms each when cold. `grep -r` didn't finish one project in 5 minutes.
- **They return the same file 200 times.** A repo with 239 git worktrees will do that.

pcfind searches ignored and hidden files, skips folders that are never the answer (`node_modules`, `.git`, caches, worktrees), skips binaries and files over 20 MB, and prints every filter it used. If a filtered search comes back thin, it warns you instead of letting the result look complete.

## Install

Needs Python 3.9+ and ripgrep.

```
pipx install git+https://github.com/dorddis/pcfind
```

For whole-PC filename search on Windows, also install Everything:

```
winget install voidtools.Everything voidtools.Everything.Cli
```

## Usage

```
pcfind text "TODO" --root ~/code/app -C 2     # one folder, with context
pcfind text "api_key" --include-secrets       # also read .env and similar
pcfind text "marker" --deep                   # also read binaries and big files
```

Every run ends with a footer like this:

```
-- 3 matching lines in 0.42s across 1 root(s)
-- filters active: 21 dead-zone dirs skipped; 25 credential filename patterns skipped; ...
-- WARNING: only 3 match(es) and the filters above were active. ...
```

Exit code 0 means matches, 1 means a clean run with none, 2 means something failed. `pcfind --help` has the rest, and [docs/how-it-works.md](docs/how-it-works.md) covers config, defaults and the security model.

## For AI agents

Paste this into `CLAUDE.md` or `AGENTS.md`:

> Search file contents with `pcfind text "<pattern>" --root <dir>` and filenames with `pcfind name "<glob>"`, not `grep -r` or the built-in Grep tool, which skip gitignored files without telling you. Read the footer before answering. If it prints a WARNING, or you're about to say something doesn't exist, re-run with `--deep --include-secrets` first. Exit code 2 means the search failed, not that nothing was found.

## Does it actually work?

Same question, two Claude sub-agents: one told to use pcfind, one told it couldn't (it used `grep -r`, the built-in Grep tool, unfiltered ripgrep or its own Python crawler). 27 questions, each checked against planted markers or a full crawl of the folder. "Found" is the share of the right files the agent reported.

| Situation | pcfind | without pcfind | Time, pcfind vs without |
|---|---|---|---|
| Answer in gitignored, hidden or nested-repo folders (3) | found all | found all | 14 s vs 3 min, 3.5 vs 17 min, 1.3 vs 37 min |
| Repo with 239 worktrees (3) | found all, 0-10% of hits wrong, 0-15 duplicate paths | found all, 165-194 duplicate paths, 13-23% of hits wrong | 1.3-7 min vs 13-46 min |
| Whole machine, outside the usual folders (2) | found all, including "it's not there" | found all | 10 vs 97 min, 4.4 vs 14 min |
| Unicode, backslashes, `$[]"`, multiline patterns (4) | found all | found all | 17 s-2 min vs 37 s-17 min |
| Filename + content, symlinks, junctions (3) | found all | found all | 25 s-2.6 min vs 33 s-14 min |
| Small repo, definitions, deps, fuzzy "where do we retry" (4) | found all | found all | 15-90 s vs 17 s-7 min; plain search won one, 21 vs 34 s |
| **Answer inside a file pcfind skips (`.bin`, `.env`) (2)** | **found none, said "high confidence"** | found all | 16 s vs 25 s-2 min |
| Answer inside a 38 MB log (1) | found none | found none | 4.5 vs 16 min |
| "When did we decide X", from notes (1) | 3 of 7 files | 4 of 7 files | 1.4 vs 6 min |

Overall: same answer in 18 of 24 valid pairs, 3 wins each way, median 81 s vs 366 s per question (50 minutes vs 5.3 hours in total). Both of pcfind's real losses were its own default filters, so `.bin` files are searched now and a thin filtered result prints the WARNING shown above. Two filename questions were thrown out because Everything was broken during the run.

It's one run on one machine with one model, so take the numbers as a sign, not proof. The method, every case and the full list of losses are in [benchmark/results/2026-10-19_run1.md](benchmark/results/2026-10-19_run1.md). The harness is in [benchmark/](benchmark/) if you want to run it on your own machine.

## When not to use it

- **One clean repo, tracked files only:** plain ripgrep is a bit faster.
- **Linux and macOS:** it runs, but it's untested, and the speed work was Windows-specific.

## License

MIT
