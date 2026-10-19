# Arm brief template

The runner fills `{CASE_ID}`, `{ARM}`, `{QUESTION}`, `{SEARCH_LINE}`, `{MACHINE}` and `{OUT}` and
sends the result as the agent's prompt. The two arms differ ONLY in `{SEARCH_LINE}`; everything
else is byte-identical, so a difference in outcome is the search instruction.

## SEARCH_LINE, arm "pcfind"

Use `pcfind text "<pattern>"` rather than `grep -r`, because `grep -r` is slow on a large tree,
and because gitignore-honouring search tools silently miss gitignored paths such as build
caches, logs and local notes. For filenames use `pcfind name "<glob>"`. Run `pcfind --help`
once if you need its flags.

## SEARCH_LINE, arm "control"

Search with any tool available to you except `pcfind`, which you must not run, read or import.

## Brief (both arms)

---
You are answering one search question on this machine. {MACHINE}

<question>
{QUESTION}
</question>

<search_tools>
{SEARCH_LINE}
</search_tools>

<done_when>
You have either found every occurrence the question asks for, or you can say exactly which
places you did not or could not search and why. A "nothing found" answer is a claim about where
you looked: state the roots and anything your tools skipped. Give any long command a timeout so
it is not cut off.
</done_when>

Change no files except {OUT}. Write your answer to {OUT} as YAML in exactly this shape, then
return the same YAML as your final message:

```yaml
scenario: {CASE_ID}
arm: {ARM}
answer: <one or two sentences answering the question as asked>
hits:            # every occurrence you found; empty list if none
  - path: <full absolute path>
    line: <line number, or null for a filename hit>
total_hits_claimed: <integer>
confidence: high | medium | low
searched_roots: [<each root you actually searched>]
not_searched: [<places you knew of but skipped or could not search, with reason>]
tools_used: [<each distinct tool or command you used>]
gave_up: true | false
```

Stop every background process you started before you return.
