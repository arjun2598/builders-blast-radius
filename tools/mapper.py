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
        outer = _key
        if outer and os.environ.get("MAP_TASKS") == "1":
            # credit this task separately: <test>\0<phase>\0task<n>
            self._n = getattr(self, "_n", -1) + 1
            _switch(outer + f"\0task{self._n}")
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        finally:
            if outer and os.environ.get("MAP_TASKS") == "1":
                _switch(outer)
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


# ---- class attribute reads -------------------------------------------------
# Every plain data value in a sqlglot class body (flags, dicts, sets, ...) is
# swapped for a descriptor that records, per test, "attribute A owned by class
# C was read through class K". Wrapping happens right after each module runs,
# so classes created later (subclasses in other modules) are recorded reading
# their bases' attributes at import time too.

_attr_names: list = []  # idx -> (module, qualname, attr)
_reads: dict = {}  # key -> set of (idx, lookup class)


class _Attr:
    __slots__ = ("value", "idx")

    def __init__(self, value, idx):
        self.value = value
        self.idx = idx

    def __get__(self, obj, cls=None):
        s = _reads.get(_key)
        if s is None:
            s = _reads[_key] = set()
        s.add((self.idx, cls))
        return self.value


def _wrap_attrs(module):
    import enum
    import inspect

    for obj in list(vars(module).values()):
        stack = [obj]
        while stack:
            cls = stack.pop()
            if not inspect.isclass(cls) or cls.__module__ != module.__name__ or "_blast_wrapped" in cls.__dict__:
                continue
            stack.extend(v for v in vars(cls).values() if inspect.isclass(v))
            if issubclass(cls, enum.Enum):
                continue
            slots = set(getattr(cls, "__slots__", ()) or ())
            for name, value in list(vars(cls).items()):
                if name.startswith("__") or name in slots or inspect.isclass(value) or callable(value) \
                        or hasattr(type(value), "__get__") or isinstance(value, _Attr):
                    continue
                _attr_names.append((module.__name__, cls.__qualname__, name))
                try:
                    setattr(cls, name, _Attr(value, len(_attr_names) - 1))
                except (TypeError, AttributeError):
                    _attr_names.pop()
            try:
                cls._blast_wrapped = True
            except (TypeError, AttributeError):
                pass


def _install_import_hook():
    from importlib.machinery import SourceFileLoader

    orig = SourceFileLoader.exec_module

    def exec_module(self, module):
        orig(self, module)
        if module.__name__.split(".")[0] == "sqlglot":
            _wrap_attrs(module)

    SourceFileLoader.exec_module = exec_module


if os.environ.get("MAP_ATTRS", "1") == "1":
    _install_import_hook()


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


def _mod_file(modname):
    mod = sys.modules.get(modname)
    return _rel(getattr(mod, "__file__", None) or "") if mod else None


_ck: dict = {}


def _cls_key(c):
    k = _ck.get(c)
    if k is None:
        try:
            f = _mod_file(c.__module__) if isinstance(c.__module__, str) else None
            k = (f or str(c.__module__)) + "::" + str(getattr(c, "__qualname__", c))
        except Exception:  # noqa: BLE001
            k = "?::" + repr(c)
        _ck[c] = k
    return k


def _hooks():
    """For each sqlglot class: source spans of code that runs when it or a
    subclass is created (__init_subclass__ in its MRO, its metaclass
    methods). Edits to attributes those mention can't be traced by reads."""
    import inspect

    out = {}
    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot") or mod is None:
            continue
        for obj in list(vars(mod).values()):
            if not inspect.isclass(obj) or obj.__module__ != name:
                continue
            spans = []
            owners = [k for k in obj.__mro__ if "__init_subclass__" in k.__dict__] + \
                     [m for m in type(obj).__mro__ if m.__module__.startswith("sqlglot")]
            for k in owners:
                for fn in (k.__dict__.values() if k in type(obj).__mro__ else [k.__dict__["__init_subclass__"]]):
                    fn = getattr(fn, "__func__", fn)
                    if inspect.isfunction(fn):
                        try:
                            lines, start = inspect.getsourcelines(fn)
                            f = _rel(inspect.getsourcefile(fn) or "")
                            if f:
                                spans.append([f, start, start + len(lines) - 1])
                        except (OSError, TypeError):
                            pass
            if spans:
                out[_cls_key(obj)] = spans
    return out


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session):
    mon.set_events(TOOL, 0)
    out = os.environ["MAP_OUT"]
    os.makedirs(out, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    sections = {
        "hits": lambda: {k: {f: sorted(ls) for f, ls in v.items()} for k, v in _hits.items()},
        "events": lambda: {k: {e: sorted(s) for e, s in v.items()} for k, v in _events.items()},
        "types": lambda: {k: _class_keys(v) for k, v in _types.items()},
        "attr_names": lambda: [[_mod_file(m), q, a] for m, q, a in _attr_names],
        "reads": lambda: {k: sorted({(i, _cls_key(c)) for i, c in v}) for k, v in _reads.items()},
    }
    data = {"worker": worker}
    for name, fn in sections.items():
        try:
            data[name] = fn()
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            data[name] = {} if name != "attr_names" else []
            data.setdefault("errors", []).append(name)
    try:
        data["hooks"] = _hooks()
    except Exception as e:  # noqa: BLE001
        print("mapper: hooks failed", repr(e), file=sys.stderr)
        data["hooks"] = {}
    with open(os.path.join(out, f"{worker}.json"), "w") as fh:
        json.dump(data, fh)
