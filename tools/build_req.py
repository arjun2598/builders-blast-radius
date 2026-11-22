"""Required-flag map from the MAP_REQ mapper pass (see tools/mapper.py).

python tools/build_req.py <raw dir> <raw split dir|-> <tests.json> <map.json.gz> <out.json.gz>

Writes {"mapped": [tests], "import": [[class, [missing]]...],
        "mro": {class: [bases]},
        "calls": {class: {arg: [tests whose validation of an instance of
                                that class found arg missing]}}}.
Tests and split tasks are numbered as in the coverage map.
"""

import gzip
import json
import sys
from pathlib import Path

SUB_BASE = 10_000_000


def main(raw_dir, split_dir, tests_path, map_path, out):
    tests = json.load(open(tests_path))["tests"]
    by_num = {t["number"] for t in tests}
    mapped, imp, mro, calls = set(), set(), {}, {}
    dirs = [raw_dir] + ([split_dir] if split_dir != "-" else [])
    for d in dirs:
        for path in sorted(Path(d).glob("*.json")):
            name = path.stem
            if name.startswith("t"):
                owner = int(name[1:])
            elif name.startswith("p"):
                n, i = name[1:].split("_")
                owner = SUB_BASE + int(n) * 1000 + int(i)
            elif name == "base" and d == raw_dir:
                owner = ""
            else:
                continue
            raw = json.load(open(path))
            mro.update(raw.get("req_mro", {}))
            ok = raw.get("ok") is not False
            if owner != "" and ok:
                mapped.add(owner)
            for key, entries in raw.get("req", {}).items():
                for cls, missing in entries:
                    if owner == "" or key == "":
                        imp.add((cls, tuple(missing)))
                        continue
                    for a in missing:
                        calls.setdefault(cls, {}).setdefault(a, set()).add(owner)
    # bases of each recorded class, from the pinned checkout (cwd on sys.path)
    import importlib

    sys.path.insert(0, ".")
    for cname in calls:
        if cname in mro:
            continue
        mod, _, qual = cname.partition(":")
        try:
            obj = importlib.import_module(mod)
            for part in qual.split("."):
                obj = getattr(obj, part)
            mro[cname] = [f"{b.__module__}:{b.__qualname__}" for b in obj.__mro__ if b is not object]
        except Exception:  # noqa: BLE001  local or unimportable: no bases known
            pass
    data = {"mapped": sorted(mapped), "import": sorted([c, list(m)] for c, m in imp), "mro": mro,
            "calls": {c: {a: sorted(v) for a, v in d.items()} for c, d in calls.items()}}
    with gzip.open(out, "wt") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"mapped={len(mapped)} classes={len(calls)} import={len(imp)} of {len(by_num)} tests")


if __name__ == "__main__":
    main(*sys.argv[1:])
