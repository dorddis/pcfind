"""Build ground truth for the real-string cases with a no-ignores Python crawl, and append them
to the run written by plant.py.

    python truth.py --config bench_config.json --run runs/run1

The crawl reads every file under the case's root except `.git` and the case's skip_dirs, with no
gitignore, no hidden-file rule and no extension filter, so it is independent of both arms. Hits
under the benchmark dir, the run dir, or a live_dirs entry modified after the run was built are
left out of truth (score.py drops them from answers the same way). Hits inside duplicate_dirs
(worktrees, backups) are recorded as duplicates, binary files separately.
"""
import argparse
import base64
import collections
import concurrent.futures as cf
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def expand(p):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(p)))


def walk(root, skip_dirs):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name not in skip_dirs and e.name != ".git":
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            yield e.path
                    except OSError:
                        pass
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--max-bytes", type=int, default=60 * 1024 * 1024)
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    run = os.path.abspath(a.run)
    key_path, scen_path = os.path.join(run, "answer_key.b64"), os.path.join(run, "scenarios.json")
    key = json.loads(base64.b64decode(open(key_path).read()))
    scen = json.load(open(scen_path, encoding="utf-8"))
    built = key["built_at"]
    if any(s["id"].startswith("R") for s in scen["scenarios"]):
        sys.exit("real-string cases already present in this run")

    dup_names = cfg.get("duplicate_dirs", ["worktrees", "_worktrees", ".worktrees", "_backups"])
    dup_re = re.compile(r"[\\/](" + "|".join(re.escape(d) for d in dup_names) + r")[\\/]", re.I)
    live = [expand(d).lower() for d in cfg.get("live_dirs", [])]
    own = [HERE.lower(), run.lower()]

    def excluded(path):
        low = path.lower()
        if any(low.startswith(o) for o in own):
            return True
        if any(low.startswith(d) for d in live):
            try:
                return os.stat(path).st_mtime >= built
            except OSError:
                return True
        return False

    def crawl(root, rx, skip_dirs):
        files = [f for f in walk(root, set(skip_dirs)) if not excluded(f)]

        def one(f):
            try:
                if os.path.getsize(f) > a.max_bytes:
                    return f, []
                data = open(f, "rb").read()
            except OSError:
                return f, []
            if rx.search(data) is None:
                return f, []
            binary = b"\0" in data[:8192]
            return f, [(n, binary) for n, line in enumerate(data.split(b"\n"), 1) if rx.search(line)]

        out = []
        with cf.ThreadPoolExecutor(16) as ex:
            for f, res in ex.map(one, files, chunksize=64):
                out += [(f, n, b) for n, b in res]
        return out, len(files)

    for rc in cfg.get("real_cases", []):
        root = expand(rc["root"])
        pat = rc["regex"]
        flags = 0
        if pat.startswith("(?i)"):
            pat, flags = pat[4:], re.I
        rx = re.compile(pat.encode("utf-8"), flags)
        hits, n = crawl(root, rx, rc.get("skip_dirs", []))
        kind = rc.get("kind", "lines")
        t = {"kind": kind, "truth": [], "duplicates": [], "binary": []}
        seen = set()
        for f, ln, isbin in sorted(hits):
            if kind == "files":
                if f in seen:
                    continue
                seen.add(f)
                ln = None
            h = {"path": f, "line": ln, "tags": []}
            (t["binary"] if isbin else t["duplicates"] if dup_re.search(f) else t["truth"]).append(h)
        if rc.get("count_lines"):
            t["line_count"] = sum(1 for f, _, b in hits if not b and not dup_re.search(f))
        key["scenarios"][rc["id"]] = t
        scen["scenarios"].append({
            "id": rc["id"], "tags": rc.get("tags", []), "question": rc["question"].format(root=root),
            "condition": rc.get("condition", "warm"),
            "arm_order": ["pcfind", "control"] if len(scen["scenarios"]) % 2 == 0 else ["control", "pcfind"]})
        print(f"{rc['id']}: crawled {n} files, truth={len(t['truth'])} dups={len(t['duplicates'])} "
              f"binary={len(t['binary'])}")

    open(key_path, "w").write(base64.b64encode(json.dumps(key).encode()).decode())
    json.dump(scen, open(scen_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
