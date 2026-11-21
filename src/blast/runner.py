"""Runs inside a forked child: executes the given tests in-process against a
patched sqlglot tree and writes which of them failed.

sqlglot's suite is plain unittest, so tests run through unittest directly
(pytest's start-up and assert rewriting cost more than most tests). A test
counts as failed, like pytest would report it, when it fails or errors in any
phase, a subtest fails, it unexpectedly succeeds, its class or module setup
fails, its file can't be imported, it can't be found, or it times out.

After the first failure the remaining tests are not run: the commit already
pays for a worker, and listing a cheap test costs less than running it.
"""

from __future__ import annotations

import importlib
import json
import os
import pickle
import signal
import sys
import unittest


# Pool tasks of the running test: index of the next one, and which to run
# (None = all). A skipped task's future yields None; see Map.subtasks.
_tasks: dict = {"n": 0, "only": None}


class SyncExecutor:
    """Replaces ProcessPoolExecutor: the work runs in this process (no spawned
    interpreters to pay for) but still crosses pickle, so pickling bugs still
    fail."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        n = _tasks["n"]
        _tasks["n"] = n + 1
        if _tasks["only"] is not None and n not in _tasks["only"]:
            fut.set_result(None)
            return fut
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()


GC_THRESHOLD = tuple(int(x) for x in os.environ.get("BLAST_GC", "50000,50,100").split(","))


class TestTimeout(Exception):
    pass


def _alarm(signum, frame):
    # keep firing every second in case something swallows the exception
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    raise TestTimeout("test timed out")


class Result(unittest.TestResult):
    def __init__(self, ids: dict, timeouts: dict, stop_first: bool, subsets: dict | None = None):
        super().__init__()
        self.subsets = subsets or {}
        self.ids = ids  # id(TestCase) -> nodeid
        self.timeouts = timeouts
        self.stop_first = stop_first
        self.bad: set[str] = set()
        self.done: set[str] = set()
        self.class_errors: list[str] = []

    def _fail(self, test):
        nid = self.ids.get(id(test))
        if nid is None:  # class/module fixture error holder
            self.class_errors.append(str(test))
        else:
            self.bad.add(nid)
        if self.stop_first:
            self.stop()

    def startTest(self, test):
        super().startTest(test)
        sub = self.subsets.get(self.ids.get(id(test)))
        _tasks["n"], _tasks["only"] = 0, (set(sub) if sub is not None else None)
        signal.setitimer(signal.ITIMER_REAL, self.timeouts.get(self.ids.get(id(test)), 30.0))

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        _tasks["n"], _tasks["only"] = 0, None
        nid = self.ids.get(id(test))
        if nid:
            self.done.add(nid)
        super().stopTest(test)

    def addError(self, test, err):
        self._fail(test)

    def addFailure(self, test, err):
        self._fail(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._fail(test)

    def addUnexpectedSuccess(self, test):
        self._fail(test)


def _repoint(workdir: str, origin: str) -> None:
    """Make already-imported packages resolve submodules from workdir."""
    for name, mod in list(sys.modules.items()):
        paths = getattr(mod, "__path__", None)
        if not name.startswith(("sqlglot", "tests")) or paths is None:
            continue
        try:
            mod.__path__ = [p.replace(origin, workdir, 1) if p.startswith(origin) else p for p in paths]
        except Exception:  # noqa: BLE001
            pass


def run(workdir: str, origin: str, changed: list[str], nodeids: list[str],
        timeouts: dict[str, float], out_path: str, stop_first: bool = True,
        subsets: dict[str, list[int]] | None = None, duck: str | None = None) -> None:
    """subsets: nodeid -> the only pool tasks to run (the map says no other
    task can see the change). duck: recorded DuckDB answers to reuse."""
    os.chdir(workdir)
    sys.path.insert(0, workdir)
    os.environ["SKIP_INTEGRATION"] = "0"
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = SyncExecutor  # type: ignore[misc]

    changed_mods = {c[:-3].replace("/", ".").removesuffix(".__init__") for c in changed if c.endswith(".py")}
    if any(m in sys.modules for m in changed_mods) or any(not c.startswith(("sqlglot/", "tests/")) for c in changed if c.endswith(".py")):
        for name in [n for n in sys.modules if n == "sqlglot" or n.startswith(("sqlglot.", "tests"))]:
            del sys.modules[name]
    _repoint(workdir, origin)
    if duck:
        from . import duckcache

        try:
            duckcache.install(workdir, duck)
        except Exception as e:  # noqa: BLE001  without answers, DuckDB just runs
            print(f"duck cache unavailable: {e!r}", file=sys.stderr)
            duckcache.uninstall()
    elif "blast.duckcache" in sys.modules:
        sys.modules["blast.duckcache"].uninstall()
    importlib.invalidate_caches()

    # sqlglot allocates many short-lived objects; default thresholds make the
    # cyclic collector a large share of test CPU. Explicit gc.collect() in
    # tests behaves as before.
    import gc

    gc.set_threshold(GC_THRESHOLD[0], *GC_THRESHOLD[1:])

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)  # keep the parent's stdout clean
    signal.signal(signal.SIGALRM, _alarm)

    # group by module and class, keeping the given (cheap-first) order
    order: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for nid in nodeids:
        parts = nid.split("::")
        if len(parts) != 3:
            order.setdefault("", {}).setdefault("", []).append((nid, ""))
            continue
        order.setdefault(parts[0], {}).setdefault(parts[1], []).append((nid, parts[2]))

    ids: dict[int, str] = {}
    result = Result(ids, timeouts, stop_first, subsets)
    failed: set[str] = set()
    for path, classes in order.items():
        if result.shouldStop:
            break
        members = [nid for cl in classes.values() for nid, _ in cl]
        if not path.endswith(".py"):
            failed.update(members)
            continue
        try:
            signal.setitimer(signal.ITIMER_REAL, 60.0)
            module = importlib.import_module(path[:-3].replace("/", "."))
            signal.setitimer(signal.ITIMER_REAL, 0)
        except BaseException as e:  # noqa: BLE001
            signal.setitimer(signal.ITIMER_REAL, 0)
            print(f"import failed {path}: {e!r}", file=sys.stderr)
            failed.update(members)
            if stop_first:
                break
            continue
        suite = unittest.TestSuite()
        for cls_name, tests in classes.items():
            cls = getattr(module, cls_name, None)
            for nid, meth in tests:
                if cls is None or not hasattr(cls, meth):
                    failed.add(nid)
                    continue
                case = cls(meth)
                ids[id(case)] = nid
                suite.addTest(case)
        try:
            suite.run(result)
        except BaseException as e:  # noqa: BLE001
            print(f"suite crashed {path}: {e!r}", file=sys.stderr)
            failed.update(m for m in members if m not in result.done)
            break
        if result.class_errors:
            # setUpClass / tearDownClass / module fixture errors: pytest pins
            # these on the tests around them, so count the whole file as failed
            print(f"fixture errors in {path}: {result.class_errors}", file=sys.stderr)
            failed.update(members)
            result.class_errors.clear()
            if stop_first:
                break

    failed |= result.bad
    # not run (stopped early, fixture error, crash): report them all
    failed |= {nid for nid in nodeids if nid not in result.done}
    with open(out_path, "w") as fh:
        json.dump(sorted(failed), fh)
