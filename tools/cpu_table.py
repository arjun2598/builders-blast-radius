"""cpu_probe output -> data/cpu.json: per test (call + teardown), per class
(setup of its first test), per module (import). Keeps "duck"/"sub" entries
(from split_probe.py) already in the output file.

python tools/cpu_table.py <cpu_raw.json> <tests.json> <data/cpu.json>
"""

import collections
import json
import os
import sys


def main(raw_path, tests_path, out_path):
    raw = json.load(open(raw_path))
    tests = json.load(open(tests_path))["tests"]
    per, cls = {}, collections.defaultdict(float)
    for t in tests:
        c = raw["tests"].get(t["id"], {})
        per[t["number"]] = round(c.get("c", 0) + c.get("t", 0), 4)
        k = t["id"].rsplit("::", 1)[0]
        cls[k] = max(cls[k], round(c.get("s", 0), 4))
    out = json.load(open(out_path)) if os.path.exists(out_path) else {}
    out.update({"test": per, "class": cls, "module": {k: round(v, 4) for k, v in raw["modules"].items()}})
    json.dump(out, open(out_path, "w"))
    print("total test cpu", round(sum(per.values()), 1), file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
