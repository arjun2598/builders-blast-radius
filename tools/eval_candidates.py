"""Offline check: for every sample commit, are all failing tests candidates,
and how much CPU would running the candidates cost?

python tools/eval_candidates.py <sqlglot checkout> <sample dir> <map>
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blast import candidates, patch  # noqa: E402


def main(repo, sample, map_path):
    repo, sample = Path(repo), Path(sample)
    tests = json.load(open(sample / "tests.json"))["tests"]
    secs = {t["number"]: t["seconds"] for t in tests}
    answers = json.load(open(sample / "answers.json"))["commits"]
    m = candidates.Map(map_path, tests)
    cmp_dir = os.environ.get("BLAST_CMPMAP", str(Path(map_path).parent / "cmp"))
    if os.path.isdir(cmp_dir):
        from blast import cmpcheck

        m.cmp = cmpcheck.CmpMap(cmp_dir)
    key_dir = os.environ.get("BLAST_KEYMAP", str(Path(map_path).parent / "keys"))
    if os.path.isdir(key_dir):
        from blast import cmpcheck

        m.keys = cmpcheck.KeyMap(key_dir)
    m.load_needs(os.environ.get("BLAST_NEEDS", str(Path(map_path).parent / "imports.json.gz")))
    m.load_disp(os.environ.get("BLAST_DISP", str(Path(map_path).parent / "disp.json.gz")))
    for f in list(repo.glob("sqlglot/**/*.py")) + list(repo.glob("tests/**/*.py")):
        m.sources[str(f.relative_to(repo))] = f.read_text()
    total = 0.0
    for p in sorted((sample / "commits").glob("*.patch")):
        if p.stem not in answers:
            continue
        diffs = patch.parse(p.read_text())
        old, new = {}, {}
        for fd in diffs:
            src = None
            if fd.old_path and (repo / fd.old_path).exists():
                src = (repo / fd.old_path).read_text()
                old[fd.old_path] = src
            if fd.new_path:
                new[fd.new_path] = patch.apply(src, fd)
        import time as _t
        t0 = _t.process_time()
        sel = candidates.select(diffs, old, new, m)
        dt = _t.process_time() - t0
        cand = set(secs) if sel.everything else {t for t in sel.tests if t in secs}
        ans = answers[p.stem]
        missed = set(ans["failing"]) - cand
        cpu = sum(secs[t] for t in cand)
        total += cpu
        print(f"{p.stem} {ans['kind']:8} fail={len(ans['failing']):3} cand={len(cand):5} "
              f"cpu={cpu:6.1f} sel={dt:5.2f}s {'MISS ' + str(sorted(missed)) if missed else 'ok'} {sel.notes}")
    print(f"total candidate seconds {total:.1f}")


if __name__ == "__main__":
    main(*sys.argv[1:])
