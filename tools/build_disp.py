"""Generator dispatch map from the MAP_DISPATCH mapper pass.

python tools/build_disp.py <raw dispatch dir> <tests.json> <out .json.gz>

{"by": {"<module>:<generator class>": {"<expression key>": [tests]}},
 "mro": {"<module>:<generator class>": [its classes, itself first]},
 "mapped": [tests the pass ran]}
"""

import glob
import gzip
import json
import os
import sys


def main(raw, tests_path, out):
    num = {t["id"]: t["number"] for t in json.load(open(tests_path))["tests"]}
    by: dict = {}
    mro: dict = {}
    mapped = []
    for p in sorted(glob.glob(os.path.join(raw, "t*.json"))):
        d = json.load(open(p))
        mro.update(d.get("disp_mro", {}))
        if d.get("ok") is False:
            continue
        n = int(os.path.basename(p)[1:-5])
        mapped.append(n)
        for key, pairs in d.get("disp", {}).items():
            if key and num.get(key.partition("\0")[0]) != n:
                continue
            for g, k in pairs:
                by.setdefault(g, {}).setdefault(k, set()).add(n)
    data = {"by": {g: {k: sorted(v) for k, v in ks.items()} for g, ks in by.items()}, "mro": mro, "mapped": mapped}
    with gzip.open(out, "wt", compresslevel=9) as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"generators={len(by)} mapped={len(mapped)} bytes={os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
