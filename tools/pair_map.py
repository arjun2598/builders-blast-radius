"""Which fixture pair each pool task of a test consumed, at the pinned commit.

Tests read .sql fixtures through tests.helpers.load_sql_fixture_pairs, which
yields (meta, sql, expected) per pair. Run every test that opened a .sql
fixture alone, in a forked child, with:
- the loader wrapped so each pair it yields is a tagged tuple; unpacking or
  indexing one records it as read, and as the pair being worked on;
- each process-pool task (blast.runner.SyncExecutor) recording that pair
  when it is submitted;
- open() recording every file opened outside the loader.

For each (test, fixture) this writes one of:
- "none": the test never read a pair of the file and opened it no other way
  (it was opened by its class setup for other tests);
- "tasks": the test submitted exactly one task per pair of the file, task k
  while working on pair k, and opened it no other way: a change to some pairs
  can only reach their tasks, as long as the file keeps its pair count;
and leaves out anything else (the test then runs whole).

cd <sqlglot checkout> && SKIP_INTEGRATION=0 PYTHONPATH=../../src \
    python ../../tools/pair_map.py <map.json.gz> <tests.json> <out.json> [jobs]
"""

import builtins
import json
import os
import sys
import time
import unittest

os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, os.getcwd())
ROOT = os.getcwd() + os.sep

import concurrent.futures  # noqa: E402

from blast import runner  # noqa: E402

concurrent.futures.ProcessPoolExecutor = runner.SyncExecutor

import tests.helpers as helpers  # noqa: E402

state = {"cur": None, "in_loader": 0, "reads": set(), "opens": set(), "tasks": [], "pairs": {}}


def _rel(p):
    try:
        p = os.path.realpath(os.fspath(p))
    except TypeError:
        return None
    return p[len(ROOT):].replace(os.sep, "/") if p.startswith(ROOT) else None


class Pair(tuple):
    tag = None

    def _seen(self):
        state["cur"] = self.tag
        state["reads"].add(self.tag[0])

    def __iter__(self):
        self._seen()
        return tuple.__iter__(self)

    def __getitem__(self, i):
        self._seen()
        return tuple.__getitem__(self, i)


_orig_loader = helpers.load_sql_fixture_pairs


def load_sql_fixture_pairs(filename):
    path = _rel(os.path.join(helpers.FIXTURES_DIR, filename))
    gen = _orig_loader(filename)
    i = 0
    while True:
        state["in_loader"] += 1
        try:
            item = next(gen)
        except StopIteration:
            break
        finally:
            state["in_loader"] -= 1
        p = Pair(item)
        p.tag = (path, i)
        i += 1
        yield p
    state["pairs"][path] = i


helpers.load_sql_fixture_pairs = load_sql_fixture_pairs
_orig_open = builtins.open


def _open(file, *a, **k):
    if not state["in_loader"] and isinstance(file, (str, bytes, os.PathLike)):
        r = _rel(file)
        if r:
            state["opens"].add(r)
    return _orig_open(file, *a, **k)


_orig_submit = runner.SyncExecutor.submit


def submit(self, fn, *args, **kwargs):
    state["tasks"].append(state["cur"])
    return _orig_submit(self, fn, *args, **kwargs)


runner.SyncExecutor.submit = submit


def _one(nodeid, path):
    pid = os.fork()
    if pid:
        return pid
    code = 0
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        import importlib
        import io

        builtins.open = _open
        io.open = _open
        f, cls_name, meth = nodeid.split("::")
        cls = getattr(importlib.import_module(f[:-3].replace("/", ".")), cls_name)
        runner._tasks.update({"n": 0, "only": None, "raise": False, "lazy": False})
        res = unittest.TestResult()
        unittest.TestSuite([cls(meth)]).run(res)
        builtins.open = _orig_open
        io.open = _orig_open
        out = {"ok": res.wasSuccessful() and not res.skipped, "reads": sorted(state["reads"]),
               "opens": sorted(state["opens"]), "tasks": state["tasks"], "pairs": state["pairs"]}
        with _orig_open(path, "w") as fh:
            json.dump(out, fh)
    except BaseException:  # noqa: BLE001
        code = 3
    os._exit(code)


def main(map_path, tests_json, out, jobs="4"):
    from blast import candidates

    jobs = int(jobs)
    tests = json.load(open(tests_json))["tests"]
    m = candidates.Map(map_path, tests)
    ids = {t["number"]: t["id"] for t in tests}
    fixtures = sorted(p for p in m.opens if p.startswith("tests/fixtures/") and p.endswith(".sql"))
    todo = sorted({ids[t] for p in fixtures for t in m.opens[p] if t in ids and ids[t].count("::") == 2})
    print(len(fixtures), "fixtures", len(todo), "tests", flush=True)
    import tempfile

    tmp = tempfile.mkdtemp(prefix="pair_map")
    queue = list(enumerate(todo))
    running = {}
    t0 = time.time()
    while queue or running:
        while queue and len(running) < jobs:
            i, nid = queue.pop(0)
            running[_one(nid, f"{tmp}/{i:05d}.json")] = (i, nid)
        pid, status = os.wait()
        i, nid = running.pop(pid)
        if status:
            print("failed", nid, status, flush=True)
    print("ran in", round(time.time() - t0), "s", flush=True)
    by_id = {v: k for k, v in ids.items()}
    result = {}
    for i, nid in enumerate(todo):
        try:
            d = json.load(open(f"{tmp}/{i:05d}.json"))
        except (OSError, ValueError):
            continue
        if not d["ok"]:
            continue
        entry = {}
        for f in fixtures:
            if by_id[nid] not in m.opens[f] or f in d["opens"]:
                continue  # not opened by it, or opened some other way: runs whole
            if f not in d["reads"]:
                entry[f] = "none"
                continue
            n = d["pairs"].get(f)
            tasks = d["tasks"]
            mine = [t for t in tasks if t is not None and t[0] == f]
            if n and len(tasks) == n and [tuple(t) if t else None for t in tasks] == [(f, k) for k in range(n)]:
                entry[f] = "tasks"
            elif not mine and n is None:
                pass
        if entry:
            result[nid] = entry
    with open(out, "w") as fh:
        json.dump(result, fh, indent=0, sort_keys=True)
    kinds = {}
    for e in result.values():
        for v in e.values():
            kinds[v] = kinds.get(v, 0) + 1
    print("written", len(result), "tests", kinds, flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
