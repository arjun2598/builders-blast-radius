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

import ast
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
NEEDS = Path(os.environ.get("BLAST_NEEDS", "/app/data/imports.json.gz"))
POPEN = Path(os.environ.get("BLAST_POPEN", "/app/data/popen.json"))
PREBUILT = Path(os.environ.get("BLAST_PREBUILT", "/app/prebuilt.pkl"))
DISP = Path(os.environ.get("BLAST_DISP", "/app/data/disp.json.gz"))
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
        if sel.everything and sel.import_probe and all(p.startswith("sqlglot/") for p in sel.import_probe):
            differ = import_state_diff(c, sel.import_probe, m)
            if differ is not None:
                m.import_ok = set(sel.import_probe)
                try:
                    sel2 = candidates.select(diffs, old, new, m)
                finally:
                    m.import_ok = set()
                # only code that imports a module whose state changed can see
                # the change (an eager one is imported by every test)
                for f in sorted(differ):
                    users = m.import_breakers(f) if f else None
                    if users is None:
                        sel2.everything = True
                        break
                    sel2.tests |= users
                if not differ or not sel2.everything:
                    sel = sel2
                log(c.id, "import-time runs change", sorted(differ) or "nothing", "->",
                    "everything" if sel.everything else len(sel.tests))
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


_origin_state: dict = {}


def _fingerprint(req: dict, seed: str):
    from . import prebuild

    try:
        return prebuild.fingerprint(str(REPO.resolve()), req, seed, PYC)
    except Exception as e:  # noqa: BLE001
        log("fingerprint failed:", repr(e))
        return None


CHILD_RSS_MB = float(os.environ.get("BLAST_CHILD_MB", "1400"))
_PAGE_MB = os.sysconf("SC_PAGE_SIZE") / 2**20 if hasattr(os, "sysconf") else 4 / 1024


def _rss_mb(pid: int) -> float:
    """Resident memory of a child (Linux; 0 where /proc is missing). Pages
    shared with the parent since the fork count too, so the limit leaves
    room for them."""
    try:
        with open(f"/proc/{pid}/statm") as fh:
            return int(fh.read().split()[1]) * _PAGE_MB
    except (OSError, ValueError, IndexError):
        return 0.0


def _module_file(name: str, m: candidates.Map, c: "Commit") -> str:
    base = name.replace(".", "/")
    for f in (base + ".py", base + "/__init__.py"):
        if f in m.sources or c.new_files.get(f) is not None:
            return f
    return ""  # unknown: treated as imported by everything


def import_state_diff(c: "Commit", probe: dict, m: candidates.Map) -> set[str] | None:
    """Files of the sqlglot modules whose module- or class-level values (or
    whether they import at all) differ when the patched tree is imported,
    compared with the pinned tree; None when that can't be told, or when the
    probed lines are not really run by that import (but by some test
    module's). Values that differ between two imports of the pinned tree are
    noise no test can rely on."""
    if "base" not in _origin_state:
        from . import prebuild

        _origin_state["base"] = prebuild.base_state(str(REPO.resolve()), PYC)
    base = _origin_state["base"]
    if base is None:
        return None
    fp0, noise, ran0, err0 = base
    if any(not set(ls) <= ran0.get(f, set()) for f, ls in probe.items()):
        log(c.id, "probed lines not run by importing sqlglot")
        return None
    files = {f: t for f, t in c.new_files.items() if f.endswith(".py")}
    if len(files) != len(c.new_files):
        return None
    r = _fingerprint({"files": files}, "0")
    if r is None:
        return None
    # names the patch itself binds or unbinds (a removed import, a renamed
    # function or method) are followed by the resolver's name rules; whatever
    # reads one while importing fails there or differs elsewhere
    noise = noise | _renamed_keys(c, m)
    mods = _differing(fp0, err0, r, noise)
    if not mods:
        return set()
    log(c.id, "import-time state differs:", sorted(mods)[:8])
    out = {_module_file(n, m, c) for n in mods}
    # A module that registers itself somewhere eager (a dialect in the
    # dialect registry, a submodule on its package) changes eager state
    # only for tests that import it. Import everything but the lazily
    # loaded modules that differ and their importers, in the pinned and
    # the patched tree: if that leaves the same state, the change is
    # confined to those modules' importers.
    g, og = m.import_graph(), m.old_import_graph()
    eager = g.eager | og.eager
    lazy = {f for f in out if f and f not in eager}
    if not lazy or "" in out:
        return out
    importers = set().union(*(g.importers(f) | og.importers(f) for f in lazy))
    if importers & eager:
        return out
    skip = sorted(f[:-3].replace("/", ".").removesuffix(".__init__") for f in importers if f.startswith("sqlglot/"))
    a = _fingerprint({"files": {}, "skip": skip}, "0")
    b = _fingerprint({"files": files, "skip": skip}, "0")
    if a is None or b is None or set(skip) & (set(a["loaded"]) | set(b["loaded"])):
        return out
    if _differing(a["fp"], set(a["errors"]), b, noise):
        return out
    log(c.id, "without", skip, "the import-time state is the same")
    return lazy


def _bound_names(src: str | None) -> set[str] | None:
    """Top-level names of a module and its classes' own names, as
    "name" and "Class.name"."""
    if src is None:
        return set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None

    def names(body, prefix):
        out = set()
        for st in body:
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                out |= {prefix + (a.asname or a.name.split(".")[0]) for a in st.names if a.name != "*"}
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(prefix + st.name)
            elif isinstance(st, ast.ClassDef):
                out.add(prefix + st.name)
                if not prefix:
                    out |= names(st.body, st.name + ".")
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                    out |= {prefix + n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        return out

    return names(tree.body, "")


def _renamed_keys(c: "Commit", m: candidates.Map) -> set[str]:
    out = set()
    for f, new_src in c.new_files.items():
        if not (f.startswith("sqlglot/") and f.endswith(".py")):
            continue
        a, b = _bound_names(m.sources.get(f)), _bound_names(new_src)
        if a is None or b is None:
            continue
        mod = f[:-3].replace("/", ".").removesuffix(".__init__")
        out |= {f"{mod}:{n}" for n in a ^ b}
    return out


def _differing(fp0: dict, err0: set, r: dict, noise: set) -> set[str]:
    """Module names whose digests or import errors differ."""
    keys = (set(fp0) | set(r["fp"])) - noise
    return (set(r["errors"]) ^ err0) | {k.split(":", 1)[0] for k in keys if fp0.get(k) != r["fp"].get(k)}


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
    m = candidates.Map(str(MAP), tests, cache=str(PREBUILT.with_name("map.pkl")))
    if CMP.is_dir():
        from . import cmpcheck

        m.cmp = cmpcheck.CmpMap(str(CMP))
    if KEYS.is_dir():
        from . import cmpcheck

        m.keys = cmpcheck.KeyMap(str(KEYS))
    m.load_needs(str(NEEDS))
    m.load_popen(str(POPEN))
    m.load_disp(str(DISP))
    for f in list(REPO.glob("sqlglot/**/*.py")) + list(REPO.glob("tests/**/*.py")):
        m.sources[str(f.relative_to(REPO))] = f.read_text()
    from . import prebuild

    pre = prebuild.load(str(PREBUILT), REPO, m.sources)
    if pre is not None:
        m._old_graph = pre["graph"]
        if pre.get("state") is not None:
            _origin_state["base"] = pre["state"]
        m._import_tables = dict(pre.get("import_tables") or {})
    else:
        log("no prebuilt data for this checkout")

    commits = [Commit(p.stem, p.read_text()) for p in sorted((DATA / "commits").glob("*.patch"))]
    only = os.environ.get("BLAST_ONLY")
    if only:  # debugging aid
        commits = [c for c in commits if c.id in only.split(",")]
    # 3 min for ~40 commits in practice, 4.5 min for ~60 in the final
    budget = float(os.environ.get("BLAST_BUDGET", 150 if len(commits) <= 45 else 235))
    child_cpu = [0.0]
    last_mem = [0.0]
    peak_mb = [0.0]
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
                               # raise-mode tests wait() on all futures before reading
                               # them: a lazy one would never finish
                               lazy={ids[t] for t in m.subtasks if t in ids and t not in m.subtask_raise},
                               raising={ids[t] for t in m.subtask_raise if t in ids},
                               costs={ids[t]: (cm.test_cpu(t), ms[t]) for t in c.run},
                               listed=[ms[t] for t in c.report])
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
            peak_mb[0] = ru.ru_maxrss / (2**20 if sys.platform == "darwin" else 1024)
        if pid == 0:
            now = time.monotonic()
            check_mem = now - last_mem[0] > 0.2
            if check_mem:
                last_mem[0] = now
            for p, (c, slot, t0, limit) in list(running.items()):
                if now - t0 > limit or now - START > budget + 15:
                    log(c.id, "child too slow, killing")
                    os.kill(p, 9)
                elif check_mem and _rss_mb(p) > CHILD_RSS_MB:
                    # a runaway test: losing this commit's run (its candidates
                    # get listed) beats the container being killed at 4 GB
                    log(c.id, f"child over {CHILD_RSS_MB} MB, killing")
                    os.kill(p, 9)
            time.sleep(0.005)
            continue
        c, slot, t0, _ = running.pop(pid)
        ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        finish(c, slot, ok)
        log(f"{c.id}: ran {len(c.run)} in {time.monotonic() - t0:.2f}s cpu {last_ru[0]:.2f}s "
            f"(est {cm.commit_cpu(c):.2f}s) peak {peak_mb[0]:.0f} MB -> report {len(c.report)}")

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
