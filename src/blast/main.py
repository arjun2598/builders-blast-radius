"""Entry point: one JSON line per commit on stdout, logs on stderr.

Pipeline per commit:
  1. parse the patch and work out candidate tests from the map (candidates.py)
  2. slow candidates are reported without running them (running costs as
     much CPU as just listing them, and they often fail)
  3. the rest run in a forked child against a patched copy of sqlglot
     (runner.py); only the ones that fail are reported
  4. reported tests are packed onto workers to minimise busiest + 2 s/worker
Anything that goes wrong falls back to reporting every candidate unrun.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import candidates, patch

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
REPO = Path(os.environ.get("BLAST_REPO", "/opt/sqlglot"))
MAP = Path(os.environ.get("BLAST_MAP", "/app/data/map.json.gz"))
PYC = os.environ.get("BLAST_PYC", "/opt/pyc")
TMP = Path(os.environ.get("BLAST_TMP", "/tmp"))
SLOTS = int(os.environ.get("BLAST_SLOTS", "2"))
CPU_TABLE = Path(os.environ.get("BLAST_CPU", "/app/data/cpu.json"))
DUCK = Path(os.environ.get("BLAST_DUCK", "/app/data/duck.pkl.gz"))
CMP = Path(os.environ.get("BLAST_CMPMAP", "/app/data/cmp"))
KEYS = Path(os.environ.get("BLAST_KEYMAP", "/app/data/keys"))
SUB_BASE = 10_000_000  # map numbers of single pool tasks, see build_map.py
SLOW = float(os.environ.get("BLAST_SLOW", "5.0"))  # CPU s: report unrun at/above this
CPU_SCALE = float(os.environ.get("BLAST_CPU_SCALE", "1.2"))  # scorer box vs where cpu.json was measured
BASE_CPU = 0.08  # fork, result file, bookkeeping per commit
IMPORT_CPU = 0.45  # re-importing sqlglot core after a code change
P_FAIL = 0.6  # chance a commit with candidates breaks something
WORKER_COST_MS = 2000
START = time.monotonic()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- packing

def pack(tests: list[int], ms: dict[int, int]) -> list[list[int]]:
    """Choose a worker count and an LPT split minimising max load + 2 s each."""
    if not tests:
        return []
    order = sorted(tests, key=lambda t: -ms[t])
    best, best_cost = None, None
    for k in range(1, min(8, len(order)) + 1):
        loads = [0] * k
        workers: list[list[int]] = [[] for _ in range(k)]
        for t in order:
            i = loads.index(min(loads))
            workers[i].append(t)
            loads[i] += ms[t]
        cost = max(loads) + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best, best_cost = workers, cost
    return best  # type: ignore[return-value]


# ---------------------------------------------------------------- work trees

class Slot:
    """A private, patchable copy of the sqlglot checkout under /tmp. It is
    copied once and restored file by file after each commit."""

    def __init__(self, i: int):
        self.path = TMP / f"w{i}"
        if not self.path.exists():
            shutil.copytree(REPO, self.path, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "posts", "docs", "CHANGELOG.md"))
        self.dirty: list[str] = []

    def apply(self, files: dict[str, str | None]) -> None:
        for rel, text in files.items():
            p = self.path / rel
            self.dirty.append(rel)
            if text is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)

    def restore(self) -> None:
        for rel in self.dirty:
            src, dst = REPO / rel, self.path / rel
            if src.exists():
                shutil.copy2(src, dst)
            elif dst.exists():
                dst.unlink()
        self.dirty = []


# ---------------------------------------------------------------- main

class Commit:
    def __init__(self, cid: str, text: str):
        self.id = cid
        self.text = text
        self.report: set[int] = set()  # tests reported without running
        self.run: list[int] = []  # tests to execute
        self.new_files: dict[str, str | None] = {}
        self.ok = True
        self.subsets: dict[int, list[int]] = {}  # test -> the only pool tasks to run
        self.duck = False  # recorded DuckDB answers still valid


def prepare(c: Commit, m: candidates.Map, all_tests: list[dict], cm: "CostModel") -> None:
    secs = {t["number"]: t["seconds"] for t in all_tests}
    try:
        diffs = patch.parse(c.text)
        old, new = {}, {}
        for fd in diffs:
            src = None
            if fd.old_path:
                p = REPO / fd.old_path
                if p.is_file():
                    src = p.read_text()
                old[fd.old_path] = src
            if fd.new_path:
                new[fd.new_path] = patch.apply(src, fd)
            if fd.old_path and fd.old_path != fd.new_path:
                c.new_files[fd.old_path] = None
            if fd.new_path:
                c.new_files[fd.new_path] = new[fd.new_path]
            if fd.binary:
                raise patch.ApplyError("binary patch")
        sel = candidates.select(diffs, old, new, m)
        cands = set(secs) if sel.everything else set(sel.tests)
        if sel.notes:
            log(c.id, "notes:", "; ".join(sel.notes))
        # Single pool tasks of split tests. Only trusted when the change is
        # sqlglot code alone: test code and data are what the split was
        # mapped with.
        tasks: dict[int, set[int]] = {}
        for t in [t for t in cands if t >= SUB_BASE]:
            cands.discard(t)
            p = (t - SUB_BASE) // 1000
            tasks.setdefault(p, set()).add(t % 1000)
            cands.add(p)
        changed = list(c.new_files)
        c.duck = all(f.endswith(".py") for f in changed)
        if not sel.everything and all(f.startswith("sqlglot/") and f.endswith(".py") for f in changed):
            for p, idx in tasks.items():
                if p in m.subtasks and len(idx) < m.subtasks[p]:
                    c.subsets[p] = sorted(idx)
    except Exception as e:  # noqa: BLE001
        log(c.id, "prepare failed, running everything:", repr(e))
        cands = set(secs)
        c.ok = False
    if not c.ok:
        c.report = set(cands)
        return
    c.report, c.run = cm.decide(cands, c.new_files, c.subsets, c.duck)
    for t in list(c.subsets):
        if t not in c.run:
            del c.subsets[t]
    if c.subsets:
        log(c.id, "tasks:", {t: len(v) for t, v in c.subsets.items()})


class CostModel:
    """Estimates what running tests costs in CPU, from per-test, per-class
    and per-module CPU measured at the pinned commit (data/cpu.json)."""

    def __init__(self, tests: list[dict], path: Path):
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.secs = {t["number"]: t["seconds"] for t in tests}
        self.id = {t["number"]: t["id"] for t in tests}
        try:
            d = json.loads(path.read_text())
            self.cpu = {int(k): v * CPU_SCALE for k, v in d["test"].items()}
            self.cls = {k: v * CPU_SCALE for k, v in d["class"].items()}
            self.mod = {k: v * CPU_SCALE for k, v in d["module"].items()}
            # split tests: [fixed, per task] CPU with DuckDB answers reused
            self.sub = {int(k): (v["fixed"] * CPU_SCALE, [x * CPU_SCALE for x in v["task"]])
                        for k, v in d.get("sub", {}).items()}
            self.duck = {int(k): v * CPU_SCALE for k, v in d.get("duck", {}).items()}
        except Exception as e:  # noqa: BLE001
            log("no cpu table, using recorded seconds:", repr(e))
            self.cpu, self.cls, self.mod, self.sub, self.duck = {}, {}, {}, {}, {}
        self.subsets: dict[int, list[int]] = {}  # of the commit being costed
        self.use_duck = False

    def test_cpu(self, t: int) -> float:
        if t in self.subsets and t in self.sub:
            fixed, per = self.sub[t]
            return fixed + sum(per[i] for i in self.subsets[t] if i < len(per))
        cpu = self.cpu.get(t, self.secs[t] * 1.5)
        if self.use_duck and t in self.duck:
            return min(cpu, self.duck[t])
        return cpu

    def run_cpu(self, tests, purge: bool) -> float:
        if not tests:
            return 0.0
        classes = {self.id[t].rsplit("::", 1)[0] for t in tests}
        files = {c.split("::")[0] for c in classes}
        return (BASE_CPU + (IMPORT_CPU if purge else 0.0)
                + sum(self.test_cpu(t) for t in tests)
                + sum(self.cls.get(c, 0.0) for c in classes)
                + sum(self.mod.get(f, 0.05) for f in files))

    def commit_cpu(self, c: "Commit") -> float:
        self.subsets, self.use_duck = c.subsets, c.duck
        return self.run_cpu(c.run, True)

    def pack_cost(self, tests) -> float:
        workers = pack(sorted(tests), self.ms)
        if not workers:
            return 0.0
        return (max(sum(self.ms[t] for t in w) for w in workers) + WORKER_COST_MS * len(workers)) / 1000

    def decide(self, cands: set[int], changed: dict, subsets=None, duck=False) -> tuple[set[int], list[int]]:
        """Split candidates into (report unrun, run). Tests whose CPU exceeds
        what listing them would cost go unrun; then pick the cheaper of
        listing everything vs. running the rest and reporting what fails."""
        self.subsets, self.use_duck = subsets or {}, duck
        if not cands:
            return set(), []
        slow = {t for t in cands if self.test_cpu(t) >= max(SLOW, self.secs[t])}
        rest = cands - slow
        purge = any(f.endswith(".py") for f in changed)
        list_all = self.pack_cost(cands)
        expect_fail = (P_FAIL * (WORKER_COST_MS / 1000 + 0.2)) if not slow else 0.3
        run_rest = self.run_cpu(rest, purge) + self.pack_cost(slow) + expect_fail
        if not rest or list_all <= run_rest:
            return set(cands), []
        order = sorted(rest, key=lambda t: (self.test_cpu(t), t))
        return slow, order


def main() -> None:
    # Only answer lines may reach stdout: point fd 1 at stderr for everything
    # else (imports, children, stray prints) and keep a private handle.
    sys.stdout.flush()
    answer_fd = os.dup(1)
    os.dup2(2, 1)
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    secs = {t["number"]: t["seconds"] for t in tests}
    ids = {t["number"]: t["id"] for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    m = candidates.Map(str(MAP), tests)
    if CMP.is_dir():
        from . import cmpcheck

        m.cmp = cmpcheck.CmpMap(str(CMP))
    if KEYS.is_dir():
        from . import cmpcheck

        m.keys = cmpcheck.KeyMap(str(KEYS))
    for f in list(REPO.glob("sqlglot/**/*.py")) + list(REPO.glob("tests/**/*.py")):
        m.sources[str(f.relative_to(REPO))] = f.read_text()

    commits = [Commit(p.stem, p.read_text()) for p in sorted((DATA / "commits").glob("*.patch"))]
    only = os.environ.get("BLAST_ONLY")
    if only:  # debugging aid
        commits = [c for c in commits if c.id in only.split(",")]
    # 3 min for ~40 commits in practice, 4.5 min for ~60 in the final
    budget = float(os.environ.get("BLAST_BUDGET", 150 if len(commits) <= 45 else 235))
    child_cpu = [0.0]
    last_ru = [0.0]
    cm = CostModel(tests, CPU_TABLE)
    for c in commits:
        prepare(c, m, tests, cm)

    # warm the parent with heavy third-party imports so forked children share them
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, "1")
    for mod in ("unittest", "unittest.mock", "duckdb", "numpy", "pandas", "pandas.testing"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            pass
    sys.path.insert(0, str(REPO))
    try:
        import sqlglot  # noqa: F401  core modules, shared by every child
    except Exception as e:  # noqa: BLE001
        log("sqlglot preload failed:", repr(e))
    from . import runner

    if DUCK.exists() and any(c.duck and c.run for c in commits):
        try:
            from . import duckcache

            duckcache.install(str(REPO), str(DUCK))  # loaded once, shared by children
        except Exception as e:  # noqa: BLE001
            log("duck cache failed:", repr(e))

    # Everything loaded so far lives as long as the run: keep the collector
    # from re-scanning it (and children from un-sharing its pages).
    import gc

    gc.collect()
    gc.freeze()
    setup_cpu = time.process_time()
    slots = [Slot(i) for i in range(SLOTS)]
    free = list(range(SLOTS))
    queue = sorted((c for c in commits if c.run), key=cm.commit_cpu)
    running: dict[int, tuple[Commit, int, float, float]] = {}  # pid -> (commit, slot, start, limit)

    def finish(c: Commit, slot: int, ok: bool) -> None:
        res = TMP / f"res_{c.id}.json"
        failed = None
        if ok and res.exists():
            try:
                failed = {by_id[n] for n in json.loads(res.read_text()) if n in by_id}
            except Exception:  # noqa: BLE001
                failed = None
        if failed is None:
            log(c.id, "run failed, reporting all candidates")
            c.report |= set(c.run)
        else:
            c.report |= failed
            if failed:
                log(c.id, "failed:", " ".join(ids[t] for t in sorted(failed))[:600])
        res.unlink(missing_ok=True)
        slots[slot].restore()
        free.append(slot)

    while queue or running:
        elapsed = time.monotonic() - START
        while queue and free:
            c = queue.pop(0)
            est = cm.commit_cpu(c) * 1.3 + 1
            if elapsed + est / SLOTS > budget:
                log(c.id, f"over budget at {elapsed:.0f}s, reporting candidates unrun")
                c.report |= set(c.run)
                continue
            slot = free.pop()
            slots[slot].apply(c.new_files)
            timeouts = {ids[t]: max(10.0, secs[t] * 5 + 5) for t in c.run}
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    sys.pycache_prefix = PYC
                    runner.run(str(slots[slot].path), str(REPO.resolve()), list(c.new_files),
                               [ids[t] for t in c.run], timeouts, str(TMP / f"res_{c.id}.json"),
                               subsets={ids[t]: v for t, v in c.subsets.items()},
                               duck=str(DUCK) if c.duck and DUCK.exists() else None,
                               lazy={ids[t] for t in m.subtasks if t in ids})
                except BaseException as e:  # noqa: BLE001
                    log(c.id, "child error", repr(e))
                    code = 1
                os._exit(code)
            running[pid] = (c, slot, time.monotonic(), est * 2 + 20)
        if not running:
            continue
        pid, status, ru = os.wait4(-1, os.WNOHANG)
        if pid:
            child_cpu[0] += ru.ru_utime + ru.ru_stime
            last_ru[0] = ru.ru_utime + ru.ru_stime
        if pid == 0:
            now = time.monotonic()
            for p, (c, slot, t0, limit) in list(running.items()):
                if now - t0 > limit or now - START > budget + 15:
                    log(c.id, "child too slow, killing")
                    os.kill(p, 9)
            time.sleep(0.005)
            continue
        c, slot, t0, _ = running.pop(pid)
        ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        finish(c, slot, ok)
        log(f"{c.id}: ran {len(c.run)} in {time.monotonic() - t0:.2f}s cpu {last_ru[0]:.2f}s "
            f"(est {cm.commit_cpu(c):.2f}s) -> report {len(c.report)}")

    out = []
    for c in commits:
        out.append(json.dumps({"commit": c.id, "workers": pack(sorted(c.report), ms)}))
    data = ("\n".join(out) + ("\n" if out else "")).encode()
    while data:
        data = data[os.write(answer_fd, data):]
    import resource

    me = resource.getrusage(resource.RUSAGE_SELF)
    log(f"done in {time.monotonic() - START:.1f}s; cpu parent {me.ru_utime + me.ru_stime:.2f}s "
        f"(setup {setup_cpu:.2f}s) children {child_cpu[0]:.2f}s")


if __name__ == "__main__":
    main()
