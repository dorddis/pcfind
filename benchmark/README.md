# pcfind A/B benchmark

Does telling an agent to use pcfind change what it finds, how sure it is, and how long it takes?
Each case is one search question given to two agents whose briefs differ in one line: one is
told to use pcfind, the other may use anything except pcfind. Their answers are scored against
ground truth that neither agent can see.

Results of the first run: [results/2026-10-19_run1.md](results/2026-10-19_run1.md), summarised in
the top-level README's report card.

## Files

| file | what it does |
|---|---|
| `bench_config.example.json` | where to plant, which scopes the questions name, real-string cases |
| `plant.py` | writes random markers to disk, the plant cases, and the hidden answer key |
| `truth.py` | builds truth for the real-string cases with a no-ignore crawl, appends them |
| `arm_brief_template.md` | the prompt each agent gets; the two arms differ only in `{SEARCH_LINE}` |
| `score.py` | scores `<case>__<arm>.yaml` answers, writes `score_report.md` and `score_runs.json` |
| `cleanup.py` | deletes exactly the planted files (dry run unless `--apply`) |

Requires Python 3.9+, `git` (to check plant dirs are gitignored), and `pyyaml` for the scorer.

## Running it

1. Copy `bench_config.example.json` to `bench_config.json` and point it at your machine:
   - `scopes`: `repo` (one repo), `code` (the root holding your repos), `tool_config` (a dot-dir
     of tool state). Questions name these paths.
   - `plant_dirs`: where markers go. Each recipe needs specific keys (below); a recipe whose
     keys are missing is skipped. `must_be_ignored_in` makes `plant.py` refuse unless that repo
     gitignores the dir, so the gitignore cases really test gitignore.
   - `duplicate_dirs`: directory names whose hits count as duplicates (worktrees, backups).
   - `live_dirs`: dirs where files written during the run would contain the markers (agent
     transcripts). Hits there modified after `plant.py` ran are dropped as contamination.
   - `transcripts_glob`: where the scorer finds each agent's transcript (first lines must carry
     `scenario: <id>` and `arm: <arm>`, which the brief guarantees). Optional; without it wall
     time, tool calls and tokens are blank.
   - `real_cases`: questions about real content. `regex` is a Python bytes regex (prefix `(?i)`
     for case-insensitive), `kind` is `lines` or `files`, `skip_dirs` are pruned from the truth
     crawl, `count_lines` records the expected line count for counting questions.
2. `python plant.py --config bench_config.json --run runs/run1`
3. `python truth.py --config bench_config.json --run runs/run1`
4. For each case in `runs/run1/scenarios.json`, run both arms in `arm_order`, back to back, with
   the same model. Fill the template's placeholders; `{OUT}` is
   `runs/run1/results/<case>__<arm>.yaml`. The agent's working directory must not be the run dir.
   Conditions:
   - `cold`: plants are fresh; do not open them before the arms run.
   - `warm`: real files; nothing to do.
   - `timeout_pressure`: the scope is large enough that an unbounded command passes the agent's
     default command timeout. Nothing to do; this is what the case tests.
   - `everything_stopped`: stop Everything before both arms and restart it after. Check its IPC
     answers (`es -n 1 x`) before the Everything-on case, or the pair is invalid.
   - `concurrent_with`: start this case and its partner for the same arm at the same time.
   - Record CPU queue length before and after the cases tagged `under-load` if you want to show
     the load.
5. `python score.py --config bench_config.json --run runs/run1`
6. `python cleanup.py --run runs/run1`, then again with `--apply`.

`runs/` is gitignored. Never publish a run dir: the answer key, questions and answers are full
of real paths.

## Scenario schema

`runs/<id>/scenarios.json`:

```json
{
  "built_at": 1792000000.0,
  "scenarios": [
    {
      "id": "P06",
      "tags": ["skipped-extension"],
      "question": "Find every place the string <marker> is referenced under <repo>.",
      "condition": "cold",
      "arm_order": ["pcfind", "control"],
      "concurrent_with": "P07"
    }
  ]
}
```

`answer_key.b64` is base64 JSON, so a search of the run dir for a marker does not hit it:
`{"built_at": ..., "scenarios": {"P06": {"kind": "lines" | "files" | "absent", "truth": [{"path",
"line", "tags"}], "duplicates": [...], "binary": [...], "line_count": N}}}`. Never decode it to a
file inside a searched tree.

Markers are three groups of six random base32 characters joined by hyphens, fresh per run, so
no file on the machine can contain one by accident.

## Plant recipes

| case | tags | needs | plants |
|---|---|---|---|
| P01 | gitignored-path, exact-literal, small-scope, cold-cache | gitignored, repo | marker on line 4 of a note |
| P02 | hidden-dir, dotfile, gitignored-path | hidden, tool_config | marker in `.marker` |
| P03 | outside-default-roots, whole-machine, timeout-pressure, concurrent | outside_a, outside_b | marker in two files |
| P04 / P04e | filename-only, whole-machine (P04e: index-unavailable) | gitignored, outside_a, outside_b | `<marker>.txt` in three places |
| P05 | filename-and-content | no_repo, code | two `*.abtest.yaml` with the marker, one without |
| P06 | skipped-extension | gitignored, repo | marker in a text `.bin` and an uncompressed `.pdf` |
| P07 | over-size-cap, whole-machine, timeout-pressure, concurrent | gitignored, outside_a | marker at line 900,001 of two ~38 MB logs |
| P08 | secret-shaped-filename | gitignored, repo | marker in `.env.abtest` |
| P09 | junction-aliases, dedupe | aliased (+ aliases) | marker in a dir reachable by several paths |
| P10 | dangling-link, filename-only | dangling_link | nothing; asks about an existing dangling link |
| P11 | multiline | gitignored, no_repo, code | two markers on adjacent lines, two decoys |
| P12 | absent-everywhere, whole-machine, timeout-pressure | - | nothing; the marker exists nowhere |
| P13 | unicode-pattern | gitignored, repo | `naive` with a diaeresis, an em dash, the marker |
| P14 | backslash-pattern | gitignored, repo | `C:\ab-dash\<marker>\x.txt` |
| P15 | regex-metacharacters | gitignored, repo | `$total[0] = "<marker>" (x+y)*2` |
| P16 | live-vs-backup-copy, whole-machine | gitignored, backup_mirror | marker in a live file and its backup copy |
| P17 | multi-repo-scope, gitignored-path, timeout-pressure | gitignored, no_repo, code | marker in two repos |

## What it does not control for

- Agents are not deterministic; one run per case gives no variance. Run it more than once.
- Wall time is the agent's whole episode, thinking included, not the search command alone.
- The control arm is "whatever the agent finds", which varies by case. Record `tools_used`.
- Truth crawls skip files over 60 MB and anything under `.git`.
