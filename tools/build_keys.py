"""Dictionary-key map from the MAP_KEYS mapper pass.

python tools/build_keys.py <raw keys dir> <raw keys split dir|-> <tests.json> <map.json.gz> <out dir>

One file per source module, <out dir>/<module path with / -> __>.json.gz:
  {"<file>::<class qualname>::<attr>": {
      "k": {key repr: [tests that read that key's value]},
      "a": [tests that read every value (values(), items(), ==, copies)],
      "n": [tests that read too many distinct keys],
      "i": [key reprs read at import time] or "many"}}
plus <out dir>/tests.json: tests mapped successfully.
"""

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

SUB_BASE = 10_000_000


def main(raw_dir, split_dir, tests_path, map_path, out_dir):
    tests = json.load(open(tests_path))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    subtasks = {int(k): v for k, v in json.load(gzip.open(map_path, "rt")).get("subtasks", {}).items()}
    nid = {t["number"]: t["id"] for t in tests}
    for n, k in subtasks.items():
        for i in range(k):
            num[f"{nid[n]}#task{i}"] = SUB_BASE + n * 1000 + i

    dicts: dict = defaultdict(lambda: {"k": defaultdict(set), "a": set(), "n": set(), "i": set()})
    mapped = set()
    for d in [raw_dir] + ([split_dir] if split_dir != "-" else []):
        for path in sorted(Path(d).glob("*.json")):
            if path.name.startswith("n") or (d != raw_dir and path.name == "base.json"):
                continue
            raw = json.load(open(path))
            if path.name != "base.json" and raw.get("ok") is not False:
                stem = path.stem
                # the test this file belongs to, even if it read no wrapped dict
                for key in list(raw.get("keys", {})) + list(raw.get("cmp", {})):
                    if key:
                        n = num.get(key.partition("\0")[0])
                        if n is not None:
                            mapped.add(n)
                if stem.startswith("t"):
                    t = next((x for x in tests if x["number"] == int(stem[1:])), None)
                    if t:
                        mapped.add(t["number"])
                elif stem.startswith("p"):
                    a, _, b = stem[1:].partition("_")
                    if int(a) in subtasks:
                        mapped.add(SUB_BASE + int(a) * 1000 + int(b))
            for key, per in raw.get("keys", {}).items():
                owner = "" if key == "" else num.get(key.partition("\0")[0])
                if owner is None:
                    continue
                for dk, keys in per.items():
                    e = dicts[dk]
                    if owner == "":
                        if keys is None or e["i"] == "many":
                            e["i"] = "many"
                        else:
                            e["i"].update(keys)
                        continue
                    if keys is None:
                        e["n"].add(owner)
                        continue
                    for k in keys:
                        if k == "*":
                            e["a"].add(owner)
                        else:
                            e["k"][k].add(owner)

    by_file: dict = defaultdict(dict)
    for dk, e in dicts.items():
        f = dk.split("::", 1)[0]
        by_file[f][dk] = {"k": {k: sorted(v) for k, v in e["k"].items()}, "a": sorted(e["a"]),
                          "n": sorted(e["n"]), "i": e["i"] if e["i"] == "many" else sorted(e["i"])}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for f, data in by_file.items():
        p = out / (f.replace("/", "__") + ".json.gz")
        with gzip.open(p, "wt", compresslevel=9) as fh:
            json.dump(data, fh, separators=(",", ":"))
        total += p.stat().st_size
    (out / "tests.json").write_text(json.dumps(sorted(mapped)))
    print(f"files={len(by_file)} dicts={len(dicts)} mapped={len(mapped)} bytes={total}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
