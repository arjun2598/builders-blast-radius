"""Module-needs map from the mapper_imports pass.

python tools/build_imports.py <raw imports dir> <sqlglot checkout> <out .json.gz>

{"<module file>": [tests that imported or asked for it at run time]},
plus "__mapped__": [tests the pass ran].
"""

import glob
import gzip
import json
import os
import sys


def main(raw, repo, out):
    data: dict = {}
    mapped = []
    for p in sorted(glob.glob(os.path.join(raw, "t*.json"))):
        n = int(os.path.basename(p)[1:-5])
        d = json.load(open(p))
        mapped.append(n)
        for mod in d["mods"]:
            base = mod.replace(".", "/")
            for f in (base + ".py", base + "/__init__.py"):
                if os.path.exists(os.path.join(repo, f)):
                    data.setdefault(f, []).append(n)
                    break
    data["__mapped__"] = mapped
    with gzip.open(out, "wt", compresslevel=9) as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"files={len(data) - 1} mapped={len(mapped)} bytes={os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
