"""Map every test in its own forked process, so runtime caches filled by one
test (dispatch tables, memoised lookups) can't hide what another test needs.

The parent installs the tracing from mapper.py, imports all of sqlglot and the
test modules once, then forks a child per test. Each child runs its test
(class fixtures included) through unittest and dumps what it saw.

cd <sqlglot checkout> && SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src \
    python ../../tools/mapper_fork.py <tests.json> <out dir> [jobs]
"""

import importlib
import json
import os
import sys
import time
import unittest

os.environ.setdefault("MAP_OUT", "/dev/null")
import mapper  # noqa: E402  installs tracing, attribute wrapping, sync executor


def main(tests_json, out, jobs="8", split=""):
    """split="1": instead of whole tests, map each process-pool task of the
    slow tests separately (see split_tests)."""
    jobs = int(jobs)
    os.makedirs(out, exist_ok=True)
    sys.path.insert(0, os.getcwd())
    tests = json.load(open(tests_json))["tests"]
    mapper.pytest_configure(None)  # import all sqlglot modules, wrap __init__s
    modules = {}
    for t in tests:
        mod = t["file"][:-3].replace("/", ".")
        if mod not in modules:
            modules[mod] = importlib.import_module(mod)
    # import-time state, shared by every child
    mapper._switch("")
    mapper._key = ""
    base = dict(mapper.sections_dump())
    base["hooks"] = mapper._hooks()
    base["attr_names"] = [[mapper._mod_file(m), q, a] for m, q, a in mapper._attr_names]
    with open(os.path.join(out, "base.json"), "w") as fh:
        json.dump(base, fh)

    queue = list(tests)
    if split:
        queue = split_tests(tests, modules, out, jobs)
    running, failed = {}, []
    t0 = time.time()
    done = 0
    while queue or running:
        while queue and len(running) < jobs:
            t = queue.pop(0)
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    code = run_one(t, modules, out)
                except BaseException:  # noqa: BLE001
                    import traceback

                    traceback.print_exc()
                    code = 3
                os._exit(code)
            running[pid] = (t, time.time())
        pid, status = os.waitpid(-1, os.WNOHANG)
        if pid == 0:
            now = time.time()
            for p, (t, st) in list(running.items()):
                if now - st > 900:
                    os.kill(p, 9)
            time.sleep(0.01)
            continue
        t, _ = running.pop(pid)
        done += 1
        if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
            failed.append(t["id"])
        if done % 100 == 0:
            print(f"{done}/{len(tests)} in {time.time() - t0:.0f}s", flush=True)
    print(f"done {done} in {time.time() - t0:.0f}s, failed: {failed[:10]} ({len(failed)})", flush=True)


SPLIT_MIN_SECONDS = float(os.environ.get("MAP_SPLIT_MIN", "3.0"))
# comma-separated test numbers: split only these (added to an existing split dir)
SPLIT_ONLY = {int(x) for x in os.environ.get("MAP_SPLIT_ONLY", "").split(",") if x}


class _Tolerant(unittest.TestResult):
    """Passes a test whose only failures are subTests that got a skipped
    task's result (SkippedTask); never stops at the first one."""

    def addSubTest(self, test, subtest, err):
        if err is not None and err[0].__name__ == "SkippedTask":
            return
        super().addSubTest(test, subtest, err)


def split_tests(tests, modules, out, jobs):
    """For each slow test, count the pool tasks it submits (running none of
    them), then queue one job per task in which only that task runs; the
    others yield None. A test that passes with every task skipped and whose
    tasks are mapped this way can later be run with only the tasks a change
    reaches. Writes n<number>.json with the count and whether that held."""
    jobs_out = []
    for t in tests:
        if (SPLIT_ONLY and t["number"] not in SPLIT_ONLY) or (not SPLIT_ONLY and t["seconds"] < SPLIT_MIN_SECONDS):
            continue
        info = None
        # skipped tasks give None if the test copes with that, else raise
        # SkippedTask into the subTest that uses the result
        for mode in ("none", "raise"):
            pid = os.fork()
            if pid == 0:
                mapper._tasks["only"] = -1
                mapper._tasks["raise"] = mode == "raise"
                ok = run_one(dict(t, mode=mode), modules, out, dump=False) == 0
                with open(os.path.join(out, f"n{t['number']:05d}.json"), "w") as fh:
                    json.dump({"ntasks": mapper._tasks["n"], "ok": ok, "mode": mode}, fh)
                os._exit(0)
            os.waitpid(pid, 0)
            info = json.load(open(os.path.join(out, f"n{t['number']:05d}.json")))
            if info["ok"]:
                break
        print(t["id"], info, flush=True)
        if info["ok"] and 2 <= info["ntasks"] < 1000:
            jobs_out += [dict(t, task=i, mode=info["mode"]) for i in range(info["ntasks"])]
    return jobs_out


def run_one(t, modules, out, dump=True):
    nodeid = t["id"]
    task = t.get("task")
    if task is not None:
        mapper._tasks["only"] = task
    if t.get("mode") == "raise":
        mapper._tasks["raise"] = True
    path, cls_name, meth = nodeid.split("::")
    module = modules[path[:-3].replace("/", ".")]
    # drop what the parent recorded so this child dumps only its own test
    for d in (mapper._hits, mapper._events, mapper._types, mapper._reads, mapper._cmp, mapper._keys, mapper._disp):
        d.clear()
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    mapper._switch(nodeid + (f"#task{task}" if task is not None else "") + "\0c")
    suite = unittest.TestSuite([getattr(module, cls_name)(meth)])
    result = _Tolerant() if t.get("mode") == "raise" else unittest.TestResult()
    suite.run(result)
    mapper._switch("")
    if not dump:
        return 0 if result.wasSuccessful() else 4
    data = mapper.sections_dump()
    data["ok"] = result.wasSuccessful()
    name = f"t{t['number']:05d}" if task is None else f"p{t['number']:05d}_{task:04d}"
    with open(os.path.join(out, name + ".json"), "w") as fh:
        json.dump(data, fh)
    return 0 if result.wasSuccessful() else 4


if __name__ == "__main__":
    main(*sys.argv[1:])
