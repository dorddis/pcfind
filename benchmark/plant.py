"""Plant random markers on disk and write the plant cases of a benchmark run.

    python plant.py --config bench_config.json --run runs/run1

Writes runs/run1/scenarios.json (questions only, safe to hand to agents), plants.json (every
file written, base64) and answer_key.b64 (truth, base64 so a casual search of the run dir does
not hit the markers). Never prints a marker. Refuses to overwrite any file, refuses a plant dir
that is not gitignored where the config says it must be, and refuses an existing run.

Run truth.py next to add the real-string cases, then cleanup.py --apply when done.
"""
import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time


def expand(p):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(p)))


def marker():
    return "-".join(base64.b32encode(secrets.token_bytes(5)).decode().lower()[:6] for _ in range(3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    run = os.path.abspath(a.run)
    if os.path.exists(os.path.join(run, "plants.json")):
        sys.exit("plants.json exists in this run - run cleanup.py --apply first or pick a new --run")
    os.makedirs(run, exist_ok=True)

    D = {k: expand(v["path"]) for k, v in cfg["plant_dirs"].items()}
    S = {k: expand(v) for k, v in cfg["scopes"].items()}
    for k, v in cfg["plant_dirs"].items():
        repo = v.get("must_be_ignored_in")
        if not repo:
            continue
        repo = expand(repo)
        rel = os.path.relpath(os.path.join(D[k], "probe.txt"), repo)
        if subprocess.run(["git", "-C", repo, "check-ignore", "-q", rel]).returncode != 0:
            sys.exit(f"plant dir '{k}' is NOT gitignored in its repo; refusing to plant there")

    M = {k: marker() for k in ["gitig", "hidden", "outside", "fname", "combo", "skipped", "big",
                               "secret", "junction", "multiA", "multiB", "absent", "unicode",
                               "backslash", "meta", "mirror", "multirepo"]}
    plants, truth, scen = [], {}, []

    def put(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            sys.exit(f"refusing to overwrite an existing file in plant dir {os.path.dirname(path)}")
        if isinstance(data, bytes):
            open(path, "wb").write(data)
        else:
            open(path, "w", encoding="utf-8", newline="").write(data)
        plants.append(path)
        return path

    def hit(path, line=None, tags=()):
        return {"path": path, "line": line, "tags": list(tags)}

    def case(cid, tags, question, condition, kind, hits, dups=()):
        scen.append({"id": cid, "tags": tags, "question": question, "condition": condition})
        truth[cid] = {"kind": kind, "truth": hits, "duplicates": list(dups)}

    have = lambda *keys: all(k in D for k in keys)
    built_at = time.time()

    if have("gitignored") and "repo" in S:
        p = put(os.path.join(D["gitignored"], "gitignored-note.md"), f"# ab plant\n\nnotes line\nref {M['gitig']} here\n")
        case("P01", ["gitignored-path", "exact-literal", "small-scope", "cold-cache"],
             f"Find every place the string {M['gitig']} is referenced under {S['repo']}.", "cold", "lines", [hit(p, 4)])

    if have("hidden") and "tool_config" in S:
        p = put(os.path.join(D["hidden"], ".marker"), f"{M['hidden']}\n")
        case("P02", ["hidden-dir", "dotfile", "gitignored-path"],
             f"Find every file under {S['tool_config']} that contains {M['hidden']}.", "cold", "lines", [hit(p, 1)])

    if have("outside_a", "outside_b"):
        x = put(os.path.join(D["outside_a"], "note.txt"), f"appdata note {M['outside']}\n")
        y = put(os.path.join(D["outside_b"], "note.txt"), f"downloads note {M['outside']}\n")
        case("P03", ["outside-default-roots", "whole-machine", "timeout-pressure", "concurrent"],
             f"Is the string {M['outside']} anywhere on this machine? Find every occurrence.",
             "cold,timeout_pressure", "lines", [hit(x, 1), hit(y, 1)])

    if have("gitignored", "outside_a", "outside_b"):
        fn = M["fname"] + ".txt"
        fs = [put(os.path.join(D[k], fn), "x\n") for k in ("gitignored", "outside_a", "outside_b")]
        q = f"Is there a file called {fn} anywhere on this PC? Give every full path."
        case("P04", ["filename-only", "whole-machine"], q, "cold", "files", [hit(f) for f in fs])
        case("P04e", ["filename-only", "whole-machine", "index-unavailable"], q, "everything_stopped",
             "files", [hit(f) for f in fs])

    if have("no_repo") and "code" in S:
        c1 = put(os.path.join(D["no_repo"], "alpha.abtest.yaml"), f"key: {M['combo']}\n")
        c2 = put(os.path.join(D["no_repo"], "sub", "beta.abtest.yaml"), f"other: 1\nkey: {M['combo']}\n")
        put(os.path.join(D["no_repo"], "gamma.abtest.yaml"), "key: none\n")
        case("P05", ["filename-and-content"],
             f"Under {S['code']}, find every file whose name ends in .abtest.yaml AND whose content contains {M['combo']}.",
             "cold", "files", [hit(c1, 1), hit(c2, 2)])

    if have("gitignored") and "repo" in S:
        b1 = put(os.path.join(D["gitignored"], "plant.bin"), f"binary-looking text file {M['skipped']}\n")
        content = f"BT /F1 12 Tf 72 700 Td ({M['skipped']}) Tj ET"
        objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
                f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        pdf, offs = "%PDF-1.4\n", []
        for i, o in enumerate(objs, 1):
            offs.append(len(pdf))
            pdf += f"{i} 0 obj\n{o}\nendobj\n"
        xref = len(pdf)
        pdf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
        pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        b2 = put(os.path.join(D["gitignored"], "plant.pdf"), pdf.encode("latin-1"))
        case("P06", ["skipped-extension"],
             f"Find every place the string {M['skipped']} is referenced under {S['repo']}.", "cold", "lines",
             [hit(b1, 1, [".bin"]), hit(b2, pdf.split("\n").index(content) + 1, [".pdf"])])

    if have("gitignored", "outside_a"):
        def big(path, m):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                sys.exit("refusing to overwrite an existing big plant")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                for i in range(1_000_000):
                    fh.write(f"needle line {m}\n" if i == 900_000 else f"ab filler line {i:08d} padding padding\n")
            plants.append(path)
            return path
        g1 = big(os.path.join(D["outside_a"], "big.log"), M["big"])
        g2 = big(os.path.join(D["gitignored"], "big.log"), M["big"])
        case("P07", ["over-size-cap", "whole-machine", "timeout-pressure", "concurrent"],
             f"Find every file on this machine that contains the string {M['big']}, with the line number.",
             "cold,timeout_pressure", "lines", [hit(g1, 900_001), hit(g2, 900_001)])

    if have("gitignored") and "repo" in S:
        s = put(os.path.join(D["gitignored"], ".env.abtest"), f"AB_KEY={M['secret']}\n")
        case("P08", ["secret-shaped-filename"],
             f"Find every place the string {M['secret']} is referenced under {S['repo']}.", "cold", "lines", [hit(s, 1)])

    if have("aliased"):
        j = put(os.path.join(D["aliased"], "j.txt"), f"junction plant {M['junction']}\n")
        aliases = [expand(x) for x in cfg["plant_dirs"]["aliased"].get("aliases", [])]
        roots = [S.get("tool_config", os.path.dirname(D["aliased"]))] + [os.path.dirname(x) for x in aliases]
        case("P09", ["junction-aliases", "dedupe"],
             f"Find every file under {', '.join(roots)} that contains {M['junction']}. Report each distinct file once.",
             "cold", "lines", [hit(j, 1)], [hit(os.path.join(x, "j.txt"), 1, ["alias"]) for x in aliases])

    if cfg.get("dangling_link"):
        link = expand(cfg["dangling_link"])
        case("P10", ["dangling-link", "filename-only"],
             f"Is there a file or link named {os.path.basename(link)} directly inside {os.path.dirname(link)}? "
             f"Give its full path, and say whether it can be read.", "warm", "files", [hit(link)])

    if have("gitignored", "no_repo") and "code" in S:
        m1 = put(os.path.join(D["gitignored"], "multi", "adjacent.md"), f"intro\n{M['multiA']}\n{M['multiB']}\nend\n")
        put(os.path.join(D["gitignored"], "multi", "far1.md"), f"{M['multiA']}\n" + "filler\n" * 20 + f"{M['multiB']}\n")
        put(os.path.join(D["no_repo"], "multi", "far2.md"), f"{M['multiB']}\nmiddle\n{M['multiA']}\n")
        case("P11", ["multiline"],
             f"Find every file under {S['code']} where a line reading exactly {M['multiA']} is immediately "
             f"followed by a line reading exactly {M['multiB']}.", "cold", "files", [hit(m1, 2)])

    case("P12", ["absent-everywhere", "whole-machine", "timeout-pressure"],
         f"Does the string {M['absent']} appear anywhere on this PC? Find every occurrence.",
         "cold,timeout_pressure", "absent", [])

    if have("gitignored") and "repo" in S:
        u = put(os.path.join(D["gitignored"], "unicode.md"), f"line one\nna\u00efve \u2014 {M['unicode']}\n")
        case("P13", ["unicode-pattern"],
             f"Find every place the exact text \"na\u00efve \u2014 {M['unicode']}\" appears under {S['repo']} "
             f"(that is the word naive with a diaeresis on the i, then an em dash).", "cold", "lines", [hit(u, 2)])
        w = put(os.path.join(D["gitignored"], "paths.txt"), f"log: C:\\ab-dash\\{M['backslash']}\\x.txt opened\n")
        case("P14", ["backslash-pattern"],
             f"Find every place the exact text C:\\ab-dash\\{M['backslash']}\\x.txt appears under {S['repo']}.",
             "cold", "lines", [hit(w, 1)])
        q = put(os.path.join(D["gitignored"], "meta.txt"), f'$total[0] = "{M["meta"]}" (x+y)*2\n')
        case("P15", ["regex-metacharacters"],
             f"Find every place the exact text $total[0] = \"{M['meta']}\" appears under {S['repo']}.",
             "cold", "lines", [hit(q, 1)])

    if have("gitignored", "backup_mirror"):
        l = put(os.path.join(D["gitignored"], "mirror.txt"), f"mirror {M['mirror']}\n")
        bk = put(os.path.join(D["backup_mirror"], "mirror.txt"), f"mirror {M['mirror']}\n")
        case("P16", ["live-vs-backup-copy", "whole-machine"],
             f"Find every file on this machine that contains {M['mirror']}. Report each distinct live file once "
             f"and say if any hit is a backup copy.", "cold", "lines", [hit(l, 1)], [hit(bk, 1, ["backup"])])

    if have("gitignored", "no_repo") and "code" in S:
        r1 = put(os.path.join(D["gitignored"], "multi-repo.txt"), f"{M['multirepo']}\n")
        r2 = put(os.path.join(D["no_repo"], "multi-repo.txt"), f"{M['multirepo']}\n")
        case("P17", ["multi-repo-scope", "gitignored-path", "timeout-pressure"],
             f"Find every place the string {M['multirepo']} is referenced under {S['code']}.",
             "cold,timeout_pressure", "lines", [hit(r1, 1), hit(r2, 1)])

    for i, sc in enumerate(scen):
        sc["arm_order"] = ["pcfind", "control"] if i % 2 == 0 else ["control", "pcfind"]
        if sc["id"] in ("P03", "P07") and {"P03", "P07"} <= {s["id"] for s in scen}:
            sc["concurrent_with"] = "P07" if sc["id"] == "P03" else "P03"

    json.dump({"built_at": built_at, "plant_dirs": sorted(set(D.values())), "count": len(plants),
               "paths_b64": [base64.b64encode(p.encode("utf-8")).decode() for p in plants]},
              open(os.path.join(run, "plants.json"), "w"), indent=1)
    key = {"built_at": built_at, "scenarios": truth}
    open(os.path.join(run, "answer_key.b64"), "w").write(base64.b64encode(json.dumps(key).encode()).decode())
    json.dump({"built_at": built_at, "scenarios": scen},
              open(os.path.join(run, "scenarios.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"planted {len(plants)} files; {len(scen)} plant cases written to {run}; run truth.py next")


if __name__ == "__main__":
    main()
