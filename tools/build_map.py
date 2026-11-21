"""Combine raw mapper output into the compact map the selector ships with.

python tools/build_map.py <rawmap dir> <tests.json> <out map.json.gz>

Map layout (test numbers are tests.json numbers):
  lines[file]   = {"sets": [[tests...]...], "line": {lineno: set index}}
  import[file]  = [lines executed at import / collection time]
  users[file]   = [tests that ran any line of file]   (derived, for speed)
  opens[file]   = [tests that opened that file]
  lists[dir]    = [tests that listed that dir]
  popen         = [tests that started a subprocess]
"""

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path


def main(raw_dir, tests_path, out_path):
    tests = json.load(open(tests_path))["tests"]
    num = {t["id"]: t["number"] for t in tests}

    line_tests = defaultdict(lambda: defaultdict(set))
    import_lines = defaultdict(set)
    opens, lists, popen = defaultdict(set), defaultdict(set), set()
    classes = defaultdict(set)
    class_setup_types = defaultdict(set)
    class_setup = defaultdict(lambda: defaultdict(set))  # class -> file -> lines
    unknown = set()

    def owner(key):
        nodeid, _, phase = key.partition("\0")
        return num.get(nodeid), nodeid, phase

    for path in sorted(Path(raw_dir).glob("*.json")):
        raw = json.load(open(path))
        for key, files in raw["hits"].items():
            if key == "":
                for f, ls in files.items():
                    import_lines[f].update(ls)
                continue
            n, nodeid, phase = owner(key)
            if n is None:
                unknown.add(nodeid)
                continue
            for f, ls in files.items():
                line_tests[f][0].add(n)  # placeholder bucket removed below
                for l in ls:
                    line_tests[f][l].add(n)
            if phase == "s":  # setUpClass etc. runs in the first test's setup only
                cls = nodeid.rsplit("::", 1)[0]
                for f, ls in files.items():
                    class_setup[cls][f].update(ls)
        for key, keys in raw.get("types", {}).items():
            if key == "":
                continue
            n, nodeid, phase = owner(key)
            if n is None:
                continue
            for c in keys:
                classes[c].add(n)
            if phase == "s":
                class_setup_types[nodeid.rsplit("::", 1)[0]].update(keys)
        for key, ev in raw["events"].items():
            if key == "":
                continue
            n, nodeid, _ = owner(key)
            if n is None:
                continue
            for f in ev.get("open", ()):
                opens[f].add(n)
            for d in ev.get("list", ()):
                lists[d].add(n)
            if ev.get("popen"):
                popen.add(n)

    # Setup-phase lines belong to every test in that class.
    by_class = defaultdict(set)
    for t in tests:
        by_class[t["id"].rsplit("::", 1)[0]].add(t["number"])
    for cls, files in class_setup.items():
        members = by_class.get(cls, set())
        for f, ls in files.items():
            for l in ls:
                line_tests[f][l] |= members
            line_tests[f][0] |= members
    for cls, keys in class_setup_types.items():
        for c in keys:
            classes[c] |= by_class.get(cls, set())

    out = {"lines": {}, "import": {}, "users": {}, "opens": {}, "lists": {}, "popen": sorted(popen),
           "classes": {c: sorted(s) for c, s in classes.items()}}
    for f, per_line in line_tests.items():
        users = per_line.pop(0, set())
        sets, index, line_idx = [], {}, {}
        for l, s in per_line.items():
            k = tuple(sorted(s))
            if k not in index:
                index[k] = len(sets)
                sets.append(list(k))
            line_idx[l] = index[k]
        out["lines"][f] = {"sets": sets, "line": line_idx}
        out["users"][f] = sorted(users)
    out["import"] = {f: sorted(ls) for f, ls in import_lines.items()}
    out["opens"] = {f: sorted(s) for f, s in opens.items()}
    out["lists"] = {f: sorted(s) for f, s in lists.items()}

    with gzip.open(out_path, "wt", compresslevel=9) as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"files={len(out['lines'])} unknown_ids={len(unknown)} popen={len(popen)} "
          f"size={Path(out_path).stat().st_size}", file=sys.stderr)
    for u in sorted(unknown)[:5]:
        print("unknown", u, file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
