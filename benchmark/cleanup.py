"""Remove exactly the files plant.py wrote, then any plant directory left empty.

    python cleanup.py --run runs/run1            dry run
    python cleanup.py --run runs/run1 --apply    delete

Refuses when the number of listed paths present on disk differs from the recorded count.
"""
import argparse
import base64
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    pj = os.path.join(os.path.abspath(a.run), "plants.json")
    if not os.path.exists(pj):
        sys.exit("no plants.json - nothing to clean")
    rec = json.load(open(pj))
    paths = [base64.b64decode(p).decode("utf-8") for p in rec["paths_b64"]]
    present = [p for p in paths if os.path.isfile(p)]
    print(f"recorded {rec['count']} plants, listed {len(paths)}, present on disk {len(present)}")
    if len(paths) != rec["count"] or len(present) != len(paths):
        sys.exit("COUNT MISMATCH - refusing. Missing: " + "; ".join(p for p in paths if p not in present))
    for p in paths:
        print(("delete " if a.apply else "would delete ") + p)
        if a.apply:
            os.remove(p)
    if not a.apply:
        print("dry run - pass --apply to delete")
        return
    for d in sorted({os.path.dirname(p) for p in paths} | set(rec["plant_dirs"]), key=len, reverse=True):
        while d and os.path.isdir(d) and not os.listdir(d) and \
                os.path.basename(d) in ("ab", ".hidden", "multi", "sub", "ab-plants"):
            os.rmdir(d)
            print("rmdir  " + d)
            d = os.path.dirname(d)
    os.remove(pj)
    print("removed plants.json")


if __name__ == "__main__":
    main()
