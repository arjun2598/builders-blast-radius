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


def main(tests_json, out, jobs="8"):
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

    running, queue, failed = {}, list(tests), []
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


def run_one(t, modules, out):
    nodeid = t["id"]
    path, cls_name, meth = nodeid.split("::")
    module = modules[path[:-3].replace("/", ".")]
    # drop what the parent recorded so this child dumps only its own test
    for d in (mapper._hits, mapper._events, mapper._types, mapper._reads):
        d.clear()
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    mapper._switch(nodeid + "\0c")
    suite = unittest.TestSuite([getattr(module, cls_name)(meth)])
    result = unittest.TestResult()
    suite.run(result)
    mapper._switch("")
    data = mapper.sections_dump()
    data["ok"] = result.wasSuccessful()
    with open(os.path.join(out, f"t{t['number']:05d}.json"), "w") as fh:
        json.dump(data, fh)
    return 0 if result.wasSuccessful() else 4


if __name__ == "__main__":
    main(*sys.argv[1:])
