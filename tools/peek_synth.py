"""Candidates for unlabeled synthetic commits, with selection time."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blast import candidates, patch
R, S = Path(sys.argv[1]), Path(sys.argv[2])
only = sys.argv[3].split(",") if len(sys.argv) > 3 else None
tests = json.load(open("blast-radius-sample/sample/tests.json"))["tests"]
m = candidates.Map("data/map.json.gz", tests)
for f in list(R.glob("sqlglot/**/*.py")) + list(R.glob("tests/**/*.py")):
    m.sources[str(f.relative_to(R))] = f.read_text()
kinds = json.load(open(S / "kinds.json"))
for p in sorted((S / "commits").glob("*.patch")):
    if only and p.stem not in only:
        continue
    diffs = patch.parse(p.read_text()); old, new = {}, {}
    for fd in diffs:
        src = (R / fd.old_path).read_text(); old[fd.old_path] = src; new[fd.new_path] = patch.apply(src, fd)
    print(p.stem, kinds[p.stem], diffs[0].path, "...", end=" ", flush=True)
    t = time.process_time(); sel = candidates.select(diffs, old, new, m); dt = time.process_time() - t
    print("ALL" if sel.everything else len(sel.tests), f"{dt:.2f}s", [n[:70] for n in sel.notes][:3], flush=True)
