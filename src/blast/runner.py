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
# lazy: run a task when its result is first asked for, so a test that
# checks results in order and fails early (with failfast) skips the rest.
_tasks: dict = {"n": 0, "only": None, "lazy": False, "raise": False}


def _call(blob):
    fn, args, kwargs = pickle.loads(blob)
    return pickle.loads(pickle.dumps(fn(*args, **kwargs)))


def _lazy_future(blob):
    from concurrent.futures import Future

    class LazyFuture(Future):
        _blob = blob

        def _force(self):
            if self._blob is not None:
                b, self._blob = self._blob, None
                try:
                    self.set_result(_call(b))
                except BaseException as e:  # noqa: BLE001
                    self.set_exception(e)

        def result(self, timeout=None):
            self._force()
            return super().result(timeout)

        def exception(self, timeout=None):
            self._force()
            return super().exception(timeout)

    return LazyFuture()


class SkippedTask(Exception):
    """Result of a pool task the map says can't see the change, for tests
    that use each result inside a subTest: that subTest ends with this and
    counts for nothing; the others run as usual."""


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
            if _tasks["raise"]:
                fut.set_exception(SkippedTask())
            else:
                fut.set_result(None)
            return fut
        try:
            blob = pickle.dumps((fn, args, kwargs))
            if _tasks["lazy"]:
                return _lazy_future(blob)
            fut.set_result(_call(blob))
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


CONTINUE_SHARE = float(os.environ.get("BLAST_CONTINUE", "1.0"))


def _pack_cost(ms: list[int]) -> float:
    """Simulated seconds of listing tests of these durations (ms), packed
    greedily on the best number of workers (2 s each, at most 8)."""
    if not ms:
        return 0.0
    order = sorted(ms, reverse=True)
    best = None
    for k in range(1, min(8, len(order)) + 1):
        loads = [0] * k
        for x in order:
            loads[loads.index(min(loads))] += x
        c = max(loads) + 2000 * k
        best = c if best is None or c < best else best
    return best / 1000


class Result(unittest.TestResult):
    def __init__(self, ids: dict, timeouts: dict, stop_first: bool, subsets: dict | None = None,
                 lazy: set | None = None, raising: set | None = None):
        super().__init__()
        self.subsets = subsets or {}
        self.lazy = lazy or set()
        self.raising = raising or set()  # nodeids whose skipped tasks raise SkippedTask
        # a failed subtest ends its test at once: the test already counts as
        # failed, the rest of it would only cost CPU
        self.failfast = stop_first
        self.ids = ids  # id(TestCase) -> nodeid
        self.timeouts = timeouts
        self.stop_first = stop_first
        self.costs: dict = {}  # nodeid -> (estimated CPU s, simulated ms)
        self.listed: list[int] = []  # simulated ms of tests listed without running
        self.file_failed: set[str] = set()  # test files with a failure: the rest of each is listed
        self.stop_file = False
        self.stop_all = False
        self.bad: set[str] = set()
        self.done: set[str] = set()
        self.class_errors: list[str] = []

    def _fail(self, test):
        nid = self.ids.get(id(test))
        if nid is None:  # class/module fixture error holder
            self.class_errors.append(str(test))
        else:
            self.bad.add(nid)
        if not self.stop_first:
            return
        if nid is None:
            self.stop()
            return
        self.file_failed.add(nid.split("::", 1)[0])
        self.stop_file = True  # the rest of this file is listed, not run
        if self._stop_all(nid):
            self.stop_all = True
        self.stop()

    def _stop_all(self, nid) -> bool:
        """After a failure (whose file's remaining tests get listed): also
        stop, listing every other test not run yet, or keep running the other
        files? Failures cluster by file, so other files pass at the rate seen
        across files so far. Listing them costs the simulated time they add
        on top of what is listed already; running costs their CPU."""
        if not self.costs:
            return True
        here = nid.split("::", 1)[0]
        left = [n for n in self.costs if n not in self.done and n != nid and n.split("::", 1)[0] != here
                and n.split("::", 1)[0] not in self.file_failed]
        if not left:
            return False
        same = [n for n in self.costs if n not in self.done and n.split("::", 1)[0] in self.file_failed]
        cpu_left = sum(self.costs[n][0] for n in left)
        listed = [self.costs[n][1] for n in self.bad | set(same) if n in self.costs] + self.listed
        extra = _pack_cost(listed + [self.costs[n][1] for n in left]) - _pack_cost(listed)
        # start from 4 imagined clean files
        files_done = {n.split("::", 1)[0] for n in self.done} | self.file_failed
        rate = len(self.file_failed) / (len(files_done) + 4)
        if os.environ.get("BLAST_DEBUG_STOP"):
            print(f"stop? {nid} left={len(left)} cpu_left={cpu_left:.2f} extra={extra:.2f} rate={rate:.2f} "
                  f"listed={len(listed)}", file=sys.__stderr__)
        return cpu_left >= CONTINUE_SHARE * extra * (1 - rate)

    def startTest(self, test):
        super().startTest(test)
        sub = self.subsets.get(self.ids.get(id(test)))
        _tasks["n"], _tasks["only"] = 0, (set(sub) if sub is not None else None)
        _tasks["lazy"] = self.ids.get(id(test)) in self.lazy
        _tasks["raise"] = sub is not None and self.ids.get(id(test)) in self.raising
        # skipped tasks fail their subTests on purpose: fail-fast would end
        # the test at the first of them
        self.failfast = self.stop_first and not _tasks["raise"]
        signal.setitimer(signal.ITIMER_REAL, self.timeouts.get(self.ids.get(id(test)), 30.0))

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        _tasks["n"], _tasks["only"], _tasks["lazy"], _tasks["raise"] = 0, None, False, False
        self.failfast = self.stop_first
        nid = self.ids.get(id(test))
        if nid:
            self.done.add(nid)
        super().stopTest(test)

    def addError(self, test, err):
        self._fail(test)

    def addFailure(self, test, err):
        self._fail(test)

    def addSubTest(self, test, subtest, err):
        if err is not None and not (_tasks["raise"] and issubclass(err[0], SkippedTask)):
            self.failfast = self.stop_first  # a real failure ends the test again
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
        subsets: dict[str, list[int]] | None = None, duck: str | None = None,
        lazy: set[str] | None = None, costs: dict | None = None, listed: list[int] | None = None,
        raising: set[str] | None = None) -> None:
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
    if sys.pycache_prefix:
        os.environ["PYTHONPYCACHEPREFIX"] = sys.pycache_prefix  # subprocesses use the bytecode too
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
    result = Result(ids, timeouts, stop_first, subsets, lazy, raising)
    result.costs = costs or {}
    result.listed = list(listed or ())
    failed: set[str] = set()
    for path, classes in order.items():
        if result.stop_all:
            break
        if result.shouldStop:  # only this file was given up on
            result.shouldStop = False
            result.stop_file = False
        if path in result.file_failed:
            continue
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
