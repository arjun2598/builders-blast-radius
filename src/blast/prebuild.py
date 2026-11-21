"""Work that depends only on the pinned checkout, done once when the image is
built instead of in every run.

python -m blast.prebuild <repo> <out.pkl>

Stores the static import graph of the pinned tree (module-level imports,
imports inside functions, dynamic import sites, the eager set) and the
pinned tree's import-state fingerprint (see fingerprint.py), taken twice
with different hash seeds to find the values that vary between runs.
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

from . import imports


def sources(repo: Path) -> dict[str, str]:
    out = {}
    for f in list(repo.glob("sqlglot/**/*.py")) + list(repo.glob("tests/**/*.py")):
        out[str(f.relative_to(repo))] = f.read_text()
    return out


def graph(srcs: dict[str, str]) -> imports.Graph:
    g = imports.Graph(srcs)
    g.eager = g.closure(imports.EAGER_ROOTS)
    g.dynamic = {f: imports.dynamic_sites(f, t) for f, t in srcs.items()}
    return g


def import_tables(srcs: dict[str, str]) -> dict:
    """Each file's from-imports (see candidates.Map.import_table)."""
    from . import candidates

    m = candidates.Map.__new__(candidates.Map)
    m.sources, m._import_tables = srcs, {}
    return {f: m.import_table(f) for f in srcs if f.endswith(".py")}


def fingerprint(repo: str, req: dict, seed: str, pyc: str | None = None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(req, fh)
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    # a pycache prefix only helps if it holds this checkout's bytecode (in the
    # image, the pinned checkout keeps its .pyc files in place instead)
    if pyc and os.path.isdir(os.path.join(pyc, repo.lstrip(os.sep))):
        env["PYTHONPYCACHEPREFIX"] = pyc
    else:
        env.pop("PYTHONPYCACHEPREFIX", None)
    try:
        r = subprocess.run([sys.executable, "-m", "blast.fingerprint", repo, fh.name], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        return json.loads(r.stdout) if r.returncode == 0 else None
    finally:
        os.unlink(fh.name)


def base_state(repo: str, pyc: str | None = None):
    """(digests, noisy keys, lines run while importing, modules that failed)
    of the pinned tree, or None."""
    a = fingerprint(repo, {"files": {}, "watch_all": True}, "0", pyc)
    b = fingerprint(repo, {"files": {}}, "1", pyc)
    if a is None or b is None:
        return None
    noise = {k for k in a["fp"] if b["fp"].get(k) != a["fp"][k]} | (set(b["fp"]) ^ set(a["fp"]))
    return a["fp"], noise, {f: set(ls) for f, ls in a["ran"].items()}, set(a["errors"])


def map_cache(map_path: str, out: str) -> None:
    """The decoded test map as a pickle: about half the CPU of decoding the
    JSON at every run."""
    import gzip
    import hashlib
    import json

    raw = open(map_path, "rb").read()
    with open(out, "wb") as fh:
        pickle.dump((hashlib.sha256(raw).hexdigest(), json.loads(gzip.decompress(raw))), fh,
                    protocol=pickle.HIGHEST_PROTOCOL)


def main(repo: str, out: str, map_path: str | None = None) -> None:
    if map_path:
        map_cache(map_path, str(Path(out).with_name("map.pkl")))
    root = Path(repo).resolve()
    srcs = sources(root)
    data = {"repo": str(root), "files": sorted(srcs), "graph": graph(srcs), "state": base_state(str(root)),
            "import_tables": import_tables(srcs)}
    with open(out, "wb") as fh:
        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"prebuilt: {len(srcs)} files, state {'ok' if data['state'] else 'missing'}", file=sys.stderr)


def load(path: str, repo: Path, srcs: dict[str, str]):
    """The prebuilt data, if it was made for this checkout."""
    try:
        with open(path, "rb") as fh:
            data = pickle.load(fh)
    except (OSError, pickle.PickleError, EOFError, AttributeError, ImportError):
        return None
    if data.get("repo") != str(repo.resolve()) or data.get("files") != sorted(srcs):
        return None
    return data


if __name__ == "__main__":
    main(*sys.argv[1:])
