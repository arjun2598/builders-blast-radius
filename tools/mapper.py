"""pytest plugin that records, per test and per phase, which source lines ran and
which repo files were opened or listed. Used once at the pinned commit to build
the selection map (see build_map.py).

Usage: PYTHONPATH=tools MAP_OUT=dir python -m pytest -p mapper tests
"""

import json
import os
import sys

import pytest

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
ROOT = os.path.realpath(os.getcwd()) + os.sep

_key = ""  # "" = outside any test (imports, collection)
_hits: dict = {}  # key -> {file: set(lines)}
_events: dict = {}  # key -> {"open": set, "list": set, "popen": bool}


def _rel(path):
    try:
        path = os.path.realpath(os.fsdecode(path))
    except Exception:
        return None
    return path[len(ROOT):] if path.startswith(ROOT) else None


_relcache: dict = {}


def _on_line(code, line):
    f = code.co_filename
    rel = _relcache.get(f, 0)
    if rel == 0:
        rel = _relcache[f] = _rel(f) if f and not f.startswith("<") else None
    if rel is not None:
        _hits.setdefault(_key, {}).setdefault(rel, set()).add(line)
    return mon.DISABLE


def _audit(event, args):
    if event == "open":
        target = args[0]
        if isinstance(target, int):
            return
        kind = "open"
    elif event in ("os.listdir", "os.scandir", "glob.glob"):
        target = args[0] if args else "."
        kind = "list"
    elif event == "subprocess.Popen":
        _events.setdefault(_key, {}).setdefault("popen", set()).add("1")
        return
    else:
        return
    rel = _rel(target if target is not None else ".")
    if rel is not None:
        _events.setdefault(_key, {}).setdefault(kind, set()).add(rel)


def _switch(key):
    global _key
    _key = key
    mon.restart_events()


_types: dict = {}  # key -> set of sqlglot types instantiated


class _SyncExecutor:
    """Stands in for ProcessPoolExecutor so work done "in a child process" is
    traced and credited to the test that submitted it. Arguments and results
    still go through pickle, as they would across processes."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        import pickle
        from concurrent.futures import Future

        fut = Future()
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


import concurrent.futures  # noqa: E402

concurrent.futures.ProcessPoolExecutor = _SyncExecutor


def _wrap_inits():
    """Record, per test, every sqlglot class that gets instantiated, so class-
    level edits (attributes, dicts) map to the tests that used the class."""
    import inspect

    seen = set()
    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot") or mod is None:
            continue
        for obj in list(vars(mod).values()):
            stack = [obj]
            while stack:
                cls = stack.pop()
                if not inspect.isclass(cls) or cls in seen:
                    continue
                seen.add(cls)
                if not getattr(cls, "__module__", "").startswith("sqlglot"):
                    continue
                stack.extend(v for v in vars(cls).values() if inspect.isclass(v))
                init = vars(cls).get("__init__")
                if inspect.isfunction(init):
                    def make(orig):
                        def __init__(self, *a, **k):
                            _types.setdefault(_key, set()).add(type(self))
                            return orig(self, *a, **k)

                        __init__.__wrapped__ = orig
                        return __init__

                    try:
                        setattr(cls, "__init__", make(init))
                    except (TypeError, AttributeError):
                        pass


mon.use_tool_id(TOOL, "mapper")
mon.register_callback(TOOL, mon.events.LINE, _on_line)
mon.set_events(TOOL, mon.events.LINE)
sys.addaudithook(_audit)


def pytest_configure(config):
    # Import every sqlglot module up front so module-level lines count as
    # import-time ("") and are never credited to whichever test loaded them.
    import importlib
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    _wrap_inits()


def _class_keys(types):
    out = set()
    for t in types:
        for c in t.__mro__:
            mod = sys.modules.get(c.__module__)
            f = _rel(getattr(mod, "__file__", None) or "") if mod else None
            if f:
                out.add(f + "::" + c.__qualname__)
    return sorted(out)


for _phase, _name in (("s", "setup"), ("c", "call"), ("t", "teardown")):

    def _make(phase):
        @pytest.hookimpl(hookwrapper=True)
        def hook(item, *args):
            _switch(item.nodeid + "\0" + phase)
            yield
            _switch("")

        return hook

    globals()[f"pytest_runtest_{_name}"] = _make(_phase)


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    out = os.environ["MAP_OUT"]
    os.makedirs(out, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    data = {
        "hits": {k: {f: sorted(ls) for f, ls in v.items()} for k, v in _hits.items()},
        "events": {k: {e: sorted(s) for e, s in v.items()} for k, v in _events.items()},
        "types": {k: _class_keys(v) for k, v in _types.items()},
    }
    with open(os.path.join(out, f"{worker}.json"), "w") as fh:
        json.dump(data, fh)
