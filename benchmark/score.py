"""Score every <case>__<arm>.yaml in a run's results dir against its answer key.

    python score.py --config bench_config.json --run runs/run1 [--results runs/run1/results]

Per run: recall (truth files found), line_recall (path and line), precision (found files that
are truth, a known duplicate or a known binary copy), false_zero (truth exists, answer empty),
confident_wrong (confidence high and recall < 1), duplicate_paths (alias spellings and
worktree/backup copies reported as distinct hits), disclosure (share of missed truth whose
location the answer's not_searched names), dropped (contaminated hits removed before scoring),
plus wall seconds, tool calls and tokens from the arm's agent transcript when transcripts_glob
finds it. Writes score_report.md and score_runs.json into the results dir.
"""
import argparse
import base64
import datetime
import glob
import json
import os
import re
import statistics
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def expand(p):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(p)))


def norm(p):
    p = str(p or "").strip().strip('"').replace("/", "\\")
    if p.startswith("\\\\?\\"):
        p = p[4:]
    if re.match(r"^\\[a-z]\\", p, re.I):
        p = p[1].upper() + ":" + p[2:]
    return p.lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--results")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    run = os.path.abspath(a.run)
    results_dir = os.path.abspath(a.results or os.path.join(run, "results"))
    key = json.loads(base64.b64decode(open(os.path.join(run, "answer_key.b64")).read()))
    built = key["built_at"]
    scen = {s["id"]: s for s in json.load(open(os.path.join(run, "scenarios.json"), encoding="utf-8"))["scenarios"]}

    aliases = {}
    for v in cfg.get("plant_dirs", {}).values():
        for al in v.get("aliases", []):
            aliases[norm(os.path.dirname(expand(al)))] = norm(os.path.dirname(expand(v["path"])))
    dup_names = cfg.get("duplicate_dirs", ["worktrees", "_worktrees", ".worktrees", "_backups"])
    dup_re = re.compile(r"\\(" + "|".join(re.escape(d) for d in dup_names) + r")\\", re.I)
    live = [norm(expand(d)) for d in cfg.get("live_dirs", [])]

    def canon(p):
        low = norm(p)
        for al, real in aliases.items():
            if low.startswith(al + "\\"):
                low = real + low[len(al):]
        return low

    def dropped_reason(p):
        low = norm(p)
        if low.startswith(norm(HERE)) or low.startswith(norm(run)):
            return "benchmark-dir"
        for d in live:
            if low.startswith(d):
                try:
                    if os.stat(canon(p)).st_mtime >= built:
                        return "live-file"
                except OSError:
                    return "live-file"
        return None

    def transcript_metrics(cid, arm, after):
        files = []
        for g in cfg.get("transcripts_glob", []):
            files += glob.glob(expand(g), recursive=True)
        want = [re.compile(rf"scenario: {re.escape(cid)}(?!\w)"), re.compile(rf"arm: {re.escape(arm)}(?!\w)")]
        for f in files:
            try:
                if os.stat(f).st_mtime < after:
                    continue
                with open(f, encoding="utf-8", errors="replace") as fh:
                    head = "".join(fh.readline() for _ in range(3))
                    if not all(w.search(head) for w in want):
                        continue
                    fh.seek(0)
                    ts, calls, tok = [], 0, 0
                    for line in fh:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("timestamp"):
                            try:
                                ts.append(datetime.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp())
                            except ValueError:
                                pass
                        msg = d.get("message") or {}
                        content = msg.get("content")
                        for c in content if isinstance(content, list) else []:
                            if isinstance(c, dict) and c.get("type") == "tool_use":
                                calls += 1
                        u = msg.get("usage") or {}
                        tok += sum(u.get(k, 0) or 0 for k in ("input_tokens", "output_tokens",
                                                              "cache_read_input_tokens", "cache_creation_input_tokens"))
                    return {"wall_s": round(max(ts) - min(ts), 1) if ts else None, "tool_calls": calls, "tokens": tok}
            except OSError:
                continue
        return {"wall_s": None, "tool_calls": None, "tokens": None}

    def score_file(path):
        text = open(path, encoding="utf-8", errors="replace").read()
        m = re.search(r"^scenario:", text, re.M)
        text = text[m.start():] if m else text
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            # agents put Windows paths in double quotes, where \U and \s are invalid escapes
            text = re.sub(r'"((?:[^"\\\n]|\\.)*)"', lambda q: "'" + q.group(1).replace("'", "''") + "'", text)
            doc = yaml.safe_load(text) or {}
        cid, arm = str(doc.get("scenario")), str(doc.get("arm"))
        t = key["scenarios"].get(cid)
        if not t:
            return {"scenario": cid, "arm": arm, "error": "no truth for scenario"}
        truth_paths = {canon(h["path"]): h for h in t["truth"]}
        truth_lines = {(canon(h["path"]), h["line"]) for h in t["truth"]}
        dup_paths = {canon(h["path"]) for h in t.get("duplicates", []) + t.get("binary", [])}
        hits_raw = doc.get("hits") or []
        hits, dropped = [], {}
        for h in hits_raw:
            p = h.get("path") if isinstance(h, dict) else h
            why = dropped_reason(p)
            if why:
                dropped[why] = dropped.get(why, 0) + 1
            else:
                hits.append((p, h.get("line") if isinstance(h, dict) else None))
        found = {canon(p) for p, _ in hits}
        tp = found & set(truth_paths)
        recall = len(tp) / len(truth_paths) if truth_paths else (1.0 if not found else 0.0)
        line_hits = {(canon(p), ln) for p, ln in hits}
        line_recall = len(line_hits & truth_lines) / len(truth_lines) if truth_lines and t["kind"] == "lines" else None
        ok = set(truth_paths) | dup_paths
        precision = sum(1 for p in found if p in ok) / len(found) if found else (1.0 if not truth_paths else 0.0)
        spellings = {norm(p) for p, _ in hits}
        dups = sum(1 for p in spellings if canon(p) in dup_paths or dup_re.search(p)) + \
            (len(spellings) - len({canon(p) for p in spellings}))
        claimed = doc.get("total_hits_claimed")
        conf = str(doc.get("confidence", "")).lower()
        missed = [truth_paths[p] for p in set(truth_paths) - tp]
        ns = " ".join(str(x) for x in (doc.get("not_searched") or [])).lower()

        def disclosed(h):
            parts = [x.lower() for x in re.split(r"[\\/]", h["path"])[3:-1]] + [x.lower() for x in h.get("tags", [])]
            return any(part and part in ns for part in parts)
        r = {"scenario": cid, "arm": arm, "kind": t["kind"], "truth_n": len(truth_paths), "found_n": len(found),
             "recall": round(recall, 3), "line_recall": None if line_recall is None else round(line_recall, 3),
             "precision": round(precision, 3),
             "false_zero": bool(truth_paths) and not hits_raw and claimed in (0, None),
             "confident_wrong": conf == "high" and recall < 1.0, "duplicate_paths": dups,
             "disclosure": round(sum(map(disclosed, missed)) / len(missed), 2) if missed else None,
             "dropped": dropped, "gave_up": bool(doc.get("gave_up")), "confidence": conf, "claimed": claimed}
        if "line_count" in t and isinstance(claimed, int) and t["line_count"]:
            r["count_ratio"] = round(claimed / t["line_count"], 2)
        r.update(transcript_metrics(cid, arm, os.stat(path).st_mtime - 6 * 3600))
        return r

    files = sorted(glob.glob(os.path.join(results_dir, "*__*.yaml")) + glob.glob(os.path.join(results_dir, "*__*.yml")))
    if not files:
        sys.exit(f"no <case>__<arm>.yaml files in {results_dir}")
    runs = [score_file(f) for f in files]

    out = ["# pcfind A/B score", "", "## Per run", "",
           "| case | arm | truth | found | recall | line_recall | precision | false_zero | conf_wrong | dups | disclosure | dropped | wall_s | calls | tokens |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        if "error" in r:
            out.append(f"| {r['scenario']} | {r['arm']} | {r['error']} |")
            continue
        out.append(f"| {r['scenario']} | {r['arm']} | {r['truth_n']} | {r['found_n']} | {r['recall']} | {r['line_recall']} | "
                   f"{r['precision']} | {r['false_zero']} | {r['confident_wrong']} | {r['duplicate_paths']} | {r['disclosure']} | "
                   f"{sum(r['dropped'].values())} | {r['wall_s']} | {r['tool_calls']} | {r['tokens']} |")
    by = {}
    for r in runs:
        if "error" not in r:
            by.setdefault(r["scenario"], {})[r["arm"]] = r

    def cell(r):
        return "-" if not r else f"{r['recall']} / {r['precision']} / {int(r['false_zero'])} / {int(r['confident_wrong'])} / {r['duplicate_paths']} / {r['wall_s']}"
    out += ["", "## Per case, pcfind vs control (recall / precision / false zero / confident wrong / dups / wall s)", "",
            "| case | tags | pcfind | control | verdict |", "|---|---|---|---|---|"]
    by_tag = {}
    for cid in sorted(by):
        p, c = by[cid].get("pcfind"), by[cid].get("control")
        verdict = "-"
        if p and c:
            kp, kc = (p["recall"], p["precision"]), (c["recall"], c["precision"])
            verdict = "pcfind" if kp > kc else "control" if kc > kp else "tie"
        tags = scen.get(cid, {}).get("tags", [])
        out.append(f"| {cid} | {', '.join(tags)} | {cell(p)} | {cell(c)} | {verdict} |")
        for tag in tags:
            for arm, r in (("pcfind", p), ("control", c)):
                if r:
                    by_tag.setdefault(tag, {}).setdefault(arm, []).append(r)

    def agg(rs):
        if not rs:
            return "-"
        return (f"r={statistics.mean(x['recall'] for x in rs):.2f} p={statistics.mean(x['precision'] for x in rs):.2f} "
                f"fz={sum(x['false_zero'] for x in rs)} cw={sum(x['confident_wrong'] for x in rs)} n={len(rs)}")
    out += ["", "## Per tag", "", "| tag | pcfind | control |", "|---|---|---|"]
    for tag in sorted(by_tag):
        out.append(f"| {tag} | {agg(by_tag[tag].get('pcfind', []))} | {agg(by_tag[tag].get('control', []))} |")
    total = {}
    for r in runs:
        for k, v in r.get("dropped", {}).items():
            total[k] = total.get(k, 0) + v
    out += ["", f"contaminated hits dropped before scoring, all runs: {total or 0}"]
    report = "\n".join(out)
    open(os.path.join(results_dir, "score_report.md"), "w", encoding="utf-8").write(report + "\n")
    json.dump(runs, open(os.path.join(results_dir, "score_runs.json"), "w"), indent=1)
    print(report)


if __name__ == "__main__":
    main()
