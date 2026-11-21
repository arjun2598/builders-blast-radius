"""Comparison-operand map from the MAP_CMP mapper pass.

python tools/build_cmp.py <raw cmp dir> <raw cmp split dir|-> <tests.json> <map.json.gz> <out dir>

Writes one file per source module, <out dir>/<module path with / -> __>.json.gz:
  {site: {"o": [observation, ...],          # distinct (left, right, left is right, result)
          "g": [[[obs idx...], [tests...]]], # tests grouped by what they saw
          "n": [tests],                      # saw too many distinct operands
          "i": [obs idx...] or null}}        # seen at import time (key "")
plus <out dir>/tests.json: the tests (and split tasks) the pass mapped
successfully. A mapped test absent from a site never evaluated it.
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

    sites: dict = defaultdict(lambda: {"obs": {}, "per": {}, "many": set(), "imp": None})
    mapped = set()
    dirs = [raw_dir] + ([split_dir] if split_dir != "-" else [])
    for d in dirs:
        for path in sorted(Path(d).glob("*.json")):
            if path.name.startswith("n") or (d != raw_dir and path.name == "base.json"):
                continue
            raw = json.load(open(path))
            for key, per in raw.get("cmp", {}).items():
                if key == "":
                    owner = ""
                else:
                    owner = num.get(key.partition("\0")[0])
                    if owner is None:
                        continue
                    if raw.get("ok") is not False:
                        mapped.add(owner)
                for site, obs in per.items():
                    s = sites[site]
                    if obs is None:
                        if owner == "":
                            s["imp"] = "many"
                        else:
                            s["many"].add(owner)
                        continue
                    idx = []
                    for o in obs:
                        k = json.dumps(o)
                        if k not in s["obs"]:
                            s["obs"][k] = len(s["obs"])
                        idx.append(s["obs"][k])
                    if owner == "":
                        if s["imp"] != "many":
                            s["imp"] = sorted(set(idx) | set(s["imp"] or ()))
                    else:
                        s["per"].setdefault(owner, set()).update(idx)
            # tests that ran but evaluated no instrumented comparison at all
            if raw.get("ok") is not False:
                for key in raw.get("hits", {}) or raw.get("events", {}) or {}:
                    if key:
                        n = num.get(key.partition("\0")[0])
                        if n is not None:
                            mapped.add(n)

    by_file: dict = defaultdict(dict)
    for site, s in sites.items():
        groups = defaultdict(list)
        for t, idx in s["per"].items():
            groups[tuple(sorted(idx))].append(t)
        f = site.rsplit(":", 2)[0]
        by_file[f][site] = {
            "o": [json.loads(k) for k, _ in sorted(s["obs"].items(), key=lambda kv: kv[1])],
            "g": [[list(k), sorted(v)] for k, v in groups.items()],
            "n": sorted(s["many"]),
            "i": s["imp"] if s["imp"] != "many" else "many",
        }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for f, data in by_file.items():
        p = out / (f.replace("/", "__") + ".json.gz")
        with gzip.open(p, "wt", compresslevel=9) as fh:
            json.dump(data, fh, separators=(",", ":"))
        total += p.stat().st_size
    (out / "tests.json").write_text(json.dumps(sorted(mapped)))
    print(f"files={len(by_file)} sites={len(sites)} mapped={len(mapped)} bytes={total}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
