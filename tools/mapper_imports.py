"""Which sqlglot modules each test needs imported.

The coverage pass imports all of sqlglot before forking, so it can't see
which lazily loaded modules (dialects, parsers, typing tables...) a test
pulls in. Here the parent imports only `sqlglot` itself; a child per test
file then imports that test module, and a grandchild per test runs it and
records every sqlglot module it imported or asked for (import statements
and importlib.import_module calls, cached or not). Modules a test file
imports at its top are left to the static import graph, which credits them
to every test in the file.

cd <sqlglot checkout> && SKIP_INTEGRATION=0 MAP_LINES=0 MAP_ATTRS=0 \
    PYTHONPATH=../../tools:../../src python ../../tools/mapper_imports.py <tests.json> <out dir> [jobs]

Writes <out dir>/t<number>.json = {"mods": [module names], "ok": bool}.
"""

import builtins
import importlib
import json
import os
import sys
import time
import unittest

os.environ.setdefault("MAP_OUT", "/dev/null")
os.environ["MAP_LINES"] = "0"
os.environ["MAP_ATTRS"] = "0"
import mapper  # noqa: E402,F401  sync process pool: pool work runs in the test's process

_asked: set = set()


def _install():
    orig_import = builtins.__import__
    orig_im = importlib.import_module

    def imp(name, globals=None, locals=None, fromlist=(), level=0):
        mod = orig_import(name, globals, locals, fromlist, level)
        try:
            if level:
                pkg = (globals or {}).get("__package__") or ""
                base = pkg.rsplit(".", level - 1)[0] if level > 1 else pkg
                full = f"{base}.{name}" if name else base
            else:
                full = name
            if full.startswith("sqlglot"):
                _asked.add(full)
                for n in fromlist or ():
                    if n != "*":
                        _asked.add(f"{full}.{n}")
        except Exception:  # noqa: BLE001
            pass
        return mod

    def import_module(name, package=None):
        mod = orig_im(name, package)
        try:
            _asked.add(mod.__name__)
        except Exception:  # noqa: BLE001
            pass
        return mod

    builtins.__import__ = imp
    importlib.import_module = import_module


def main(tests_json, out, jobs="6"):
    jobs = int(jobs)
    os.makedirs(out, exist_ok=True)
    sys.path.insert(0, os.getcwd())
    tests = json.load(open(tests_json))["tests"]
    import sqlglot  # noqa: F401  the eager set every test shares

    by_file: dict = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t)
    t0 = time.time()
    done = 0
    failed = []
    for f, group in by_file.items():
        pid = os.fork()
        if pid == 0:
            os._exit(run_file(f, group, out, jobs))
        _, status = os.waitpid(pid, 0)
        if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
            failed.append(f)
        done += len(group)
        print(f"{done}/{len(tests)} in {time.time() - t0:.0f}s {f}", flush=True)
    print(f"done in {time.time() - t0:.0f}s, failed files: {failed}", flush=True)


def run_file(f, group, out, jobs):
    module = importlib.import_module(f[:-3].replace("/", "."))
    _install()
    running = {}
    queue = list(group)
    while queue or running:
        while queue and len(running) < jobs:
            t = queue.pop(0)
            pid = os.fork()
            if pid == 0:
                code = 3
                try:
                    code = run_one(t, module, out)
                finally:
                    os._exit(code)
            running[pid] = t
        pid, _ = os.waitpid(-1, 0)
        running.pop(pid, None)
    return 0


def run_one(t, module, out):
    before = set(sys.modules)
    _asked.clear()
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    _, cls_name, meth = t["id"].split("::")
    result = unittest.TestResult()
    unittest.TestSuite([getattr(module, cls_name)(meth)]).run(result)
    new = {m for m in set(sys.modules) - before if m.startswith("sqlglot")}
    mods = sorted(new | {m for m in _asked if m in sys.modules or m.rpartition(".")[0] in sys.modules})
    with open(os.path.join(out, f"t{t['number']:05d}.json"), "w") as fh:
        json.dump({"mods": mods, "ok": result.wasSuccessful()}, fh)
    return 0


if __name__ == "__main__":
    main(*sys.argv[1:])
