"""Map the stages of each pool task of the TPC executor tests (see
blast.stagecut).

Each task runs alone in a forked child, as in mapper_fork's split mode.
Lines that run inside the cut function (sqlglot.executor's optimize) or
inside the test's parse_one calls go to "cut". Every other sqlglot line that
runs goes to "post": the parent's fixtures, pickling, table setup, planning,
execution and the DuckDB check. A line that runs in both is "post". The
digest of the expression the cut returned is recorded too. Then an untraced
pass times each task and its cut.

cd <sqlglot checkout> && SKIP_INTEGRATION=0 PYTHONPATH=../../src \
    python ../../tools/stage_map.py <out.json.gz> [jobs]
"""

import gzip
import json
import os
import sys
import time
import unittest

os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, os.getcwd())

import concurrent.futures  # noqa: E402

from blast import runner, stagecut  # noqa: E402

concurrent.futures.ProcessPoolExecutor = runner.SyncExecutor

import sqlglot.executor as sx  # noqa: E402
import tests.test_executor as te  # noqa: E402

TESTS = ["test_execute_tpch", "test_execute_tpcds"]
CLS = "TestExecutor"
FILE = "tests/test_executor.py"
CUT = "sqlglot.executor:optimize"
ROOT = os.path.join(os.getcwd(), "sqlglot") + os.sep

state = {"depth": 0, "digests": [], "timing": None, "t0": 0.0}
real_optimize = sx.optimize
real_parse = te.parse_one


def _phase_change():
    if sys.monitoring.get_tool(TOOL):
        sys.monitoring.restart_events()


def optimize(*a, **k):
    state["depth"] += 1
    _phase_change()
    t0 = time.process_time()
    try:
        out = real_optimize(*a, **k)
    finally:
        state["depth"] -= 1
        if state["timing"] is not None:
            state["timing"].append(time.process_time() - t0)
        _phase_change()
    state["digests"].append(stagecut.digest(out))
    return out


def parse_one(*a, **k):
    state["depth"] += 1
    _phase_change()
    try:
        return real_parse(*a, **k)
    finally:
        state["depth"] -= 1
        _phase_change()


sx.optimize = optimize
te.parse_one = parse_one

TOOL = 3
sets = {"cut": set(), "post": set()}


def _line(code, ln):
    f = code.co_filename
    if f.startswith(ROOT):
        sets["cut" if state["depth"] else "post"].add((f, ln))
    return sys.monitoring.DISABLE


def _count(meth):
    pid = os.fork()
    if pid == 0:
        runner._tasks.update({"n": 0, "only": set(), "raise": False, "lazy": False})
        unittest.TestSuite([getattr(te, CLS)(meth)]).run(unittest.TestResult())
        os._exit(min(runner._tasks["n"], 250))
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status)


def _one(meth, i, path):
    pid = os.fork()
    if pid:
        return pid
    code = 0
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        sys.monitoring.use_tool_id(TOOL, "stage_map")
        sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, _line)
        sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE)
        runner._tasks.update({"n": 0, "only": {i}, "raise": False, "lazy": False})
        res = unittest.TestResult()
        unittest.TestSuite([getattr(te, CLS)(meth)]).run(res)
        sys.monitoring.set_events(TOOL, 0)
        rel = lambda f: "sqlglot/" + f[len(ROOT):].replace(os.sep, "/")  # noqa: E731
        post: dict = {}
        cut: dict = {}
        for f, ln in sets["post"]:
            post.setdefault(rel(f), set()).add(ln)
        for f, ln in sets["cut"]:
            if (f, ln) not in sets["post"]:
                cut.setdefault(rel(f), set()).add(ln)
        out = {"ok": res.wasSuccessful() and not res.skipped, "digests": state["digests"],
               "post": {f: sorted(v) for f, v in post.items()}, "cut": {f: sorted(v) for f, v in cut.items()}}
        with open(path, "w") as fh:
            json.dump(out, fh)
    except BaseException:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        code = 3
    os._exit(code)


def _timing(meth, n):
    """CPU of each task alone and of its cut, untraced, in one child."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        times = []
        orig = runner._call

        def timed(blob, want=None):
            state["timing"] = cut = []
            t0 = time.process_time()
            try:
                return orig(blob, want)
            finally:
                times.append((time.process_time() - t0, sum(cut)))
                state["timing"] = None

        runner._call = timed
        runner._tasks.update({"n": 0, "only": None, "raise": False, "lazy": False})
        unittest.TestSuite([getattr(te, CLS)(meth)]).run(unittest.TestResult())
        os.write(w, json.dumps(times).encode())
        os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 1 << 16):
        data += chunk
    os.waitpid(pid, 0)
    return json.loads(data)


def main(out, jobs="6"):
    jobs = int(jobs)
    tmp = out + ".d"
    os.makedirs(tmp, exist_ok=True)
    result = {"cut": CUT, "tests": {}}
    for meth in TESTS:
        n = _count(meth)
        print(meth, "tasks", n, flush=True)
        queue = list(range(n))
        running = {}
        t0 = time.time()
        while queue or running:
            while queue and len(running) < jobs:
                i = queue.pop(0)
                running[_one(meth, i, f"{tmp}/{meth}_{i:04d}.json")] = i
            pid, status = os.wait()
            i = running.pop(pid)
            if status:
                print("task failed", meth, i, status, flush=True)
        print(meth, "mapped in", round(time.time() - t0), "s", flush=True)
        times = _timing(meth, n)
        tasks = []
        for i in range(n):
            try:
                d = json.load(open(f"{tmp}/{meth}_{i:04d}.json"))
            except OSError:
                d = None
            if not d or not d["ok"] or len(d["digests"]) != 1 or i >= len(times):
                tasks.append(None)
                continue
            tasks.append({"digest": d["digests"][0], "post": d["post"], "cut": d["cut"],
                          "cpu": times[i][0], "cpu_cut": times[i][1]})
        result["tests"][f"{FILE}::{CLS}::{meth}"] = tasks
        print(meth, "usable", sum(t is not None for t in tasks), "of", n,
              "cpu", round(sum(t[0] for t in times), 2), "cut", round(sum(t[1] for t in times), 2), flush=True)
    with gzip.open(out, "wt") as fh:
        json.dump(result, fh, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:])
