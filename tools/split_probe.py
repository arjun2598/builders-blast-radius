"""Add to cpu.json the CPU of tests when DuckDB answers are reused ("duck")
and, for split tests, the fixed and per-task CPU ("sub").

cd <sqlglot checkout> && SKIP_INTEGRATION=0 PYTHONPATH=.:../../src \
    python ../../tools/split_probe.py <tests.json> <map.json.gz> <duck.pkl.gz> <cpu.json> <module>...
"""

import concurrent.futures
import gzip
import importlib
import json
import os
import sys
import time
import unittest

from blast import duckcache, runner

concurrent.futures.ProcessPoolExecutor = runner.SyncExecutor


class Timed(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.cpu = {}

    def startTest(self, test):
        super().startTest(test)
        self.t0 = time.process_time()

    def stopTest(self, test):
        self.cpu[test.id()] = time.process_time() - self.t0
        super().stopTest(test)


def main(tests_json, map_path, duck, cpu_path, *modules):
    tests = json.load(open(tests_json))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    subtasks = {int(k): v for k, v in json.load(gzip.open(map_path, "rt")).get("subtasks", {}).items()}
    duckcache.install(os.getcwd(), duck)
    task_cpu = []
    orig = runner.SyncExecutor.submit

    def submit(self, fn, *a, **k):
        t0 = time.process_time()
        try:
            return orig(self, fn, *a, **k)
        finally:
            task_cpu.append(time.process_time() - t0)

    runner.SyncExecutor.submit = submit
    duck_cpu, sub = {}, {}
    mods = [importlib.import_module(m) for m in modules]
    import gc

    gc.collect()
    gc.freeze()
    gc.set_threshold(*runner.GC_THRESHOLD)  # as in the runner's children
    for m, mod in zip(modules, mods):
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        res = Timed()
        suite.run(res)
        for tid, cpu in res.cpu.items():
            mod_name, cls, meth = tid.rsplit(".", 2)
            nid = f"{mod_name.replace('.', '/')}.py::{cls}::{meth}"
            if nid in num:
                duck_cpu[num[nid]] = round(cpu, 4)
        for n, k in subtasks.items():
            t = next(t for t in tests if t["number"] == n)
            path, cls, meth = t["id"].split("::")
            if path[:-3].replace("/", ".") != m:
                continue
            task_cpu.clear()
            r = Timed()
            unittest.TestSuite([getattr(mod, cls)(meth)]).run(r)
            per = list(task_cpu)
            assert len(per) == k, (n, len(per), k)
            total = r.cpu[next(iter(r.cpu))]
            sub[n] = {"fixed": round(total - sum(per), 4), "task": [round(x, 4) for x in per]}
            print(t["id"], "total", round(total, 2), "fixed", sub[n]["fixed"], file=sys.stderr)
    d = json.load(open(cpu_path))
    d["duck"] = {str(k): v for k, v in duck_cpu.items()}
    d["sub"] = {str(k): v for k, v in sub.items()}
    json.dump(d, open(cpu_path, "w"))
    old = {int(k): v for k, v in d["test"].items()}
    for n, v in sorted(duck_cpu.items(), key=lambda x: -old.get(x[0], 0))[:8]:
        print(n, "cpu", old.get(n), "-> duck", v, file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
