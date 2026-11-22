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


_tasks = {"n": 0, "only": None, "raise": False}  # pool tasks submitted so far; run only this one


class SkippedTask(Exception):
    """Result of a pool task skipped in split mode, for tests that use every
    result inside a subTest (fixture loops): that subTest fails with this
    and the others go on."""


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
        n = _tasks["n"]
        _tasks["n"] = n + 1
        if _tasks["only"] is not None and n != _tasks["only"]:
            # skipped task, see mapper_fork split mode
            if _tasks["raise"]:
                fut.set_exception(SkippedTask())
            else:
                fut.set_result(None)
            return fut
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
_modstack: list = []  # modules being executed right now
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
        # at import time, what matters is which module is being built: derived
        # values end up on its classes (e.g. a dialect copying tokenizer flags)
        s.add((self.idx, cls if _key or not _modstack else ("MOD", _modstack[-1])))
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
        _modstack.append(module.__name__)
        try:
            orig(self, module)
        finally:
            _modstack.pop()
        if module.__name__.split(".")[0] == "sqlglot":
            _wrap_attrs(module)

    SourceFileLoader.exec_module = exec_module


if os.environ.get("MAP_ATTRS", "1") == "1":
    _install_import_hook()


# ---- comparison operands ---------------------------------------------------
# With MAP_CMP=1 every single comparison `a OP b` in sqlglot is compiled as
# __blast_cmp__(site, OP, a, b), which records per test what the operands
# were (literal values; otherwise type and whether its == is identity) and
# returns a OP b. An edit to one comparison can then be checked against what
# each test actually compared (see candidates.cmp_filter).

import ast as _ast  # noqa: E402
import builtins as _builtins  # noqa: E402
import operator as _op  # noqa: E402

CMP_CAP = 64  # distinct observations per test and site; beyond that "many"
_cmp: dict = {}  # key -> {site: set of observations, or None = too many}
_OPS = {"Eq": _op.eq, "NotEq": _op.ne, "Lt": _op.lt, "LtE": _op.le, "Gt": _op.gt, "GtE": _op.ge,
        "Is": _op.is_, "IsNot": _op.is_not, "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b}
_SCALARS = (type(None), bool, int, float, str, bytes)
_okcache: dict = {}


def _okey(x):
    t = type(x)
    if t in _SCALARS and (t not in (str, bytes) or len(x) <= 120):
        return ("v", repr(x))
    if t is tuple and len(x) <= 16 and all(type(e) in _SCALARS for e in x) and len(repr(x)) <= 400:
        return ("v", repr(x))
    k = _okcache.get(t)
    if k is None:
        import enum

        ideq = getattr(t, "__eq__", None) is object.__eq__ and getattr(t, "__ne__", None) is object.__ne__
        # enum members of one class: distinct members have distinct values,
        # so == between them is identity even for str/int mixins
        is_enum = isinstance(t, type) and issubclass(t, enum.Enum) and t.__eq__ in (object.__eq__, enum.Enum.__eq__, str.__eq__, int.__eq__)
        k = _okcache[t] = ("t", f"{t.__module__}.{t.__qualname__}", ideq, is_enum)
    return k


def _bc(site, op, a, b):
    res = _OPS[op](a, b)
    per = _cmp.get(_key)
    if per is None:
        per = _cmp[_key] = {}
    obs = per.get(site, 0)
    if obs is not None:
        if obs == 0:
            obs = per[site] = set()
        try:
            obs.add((_okey(a), _okey(b), a is b, res if type(res) is bool else None))
        except Exception:  # noqa: BLE001
            per[site] = None
        else:
            if len(obs) > CMP_CAP:
                per[site] = None
    return res


class _CmpRewrite(_ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = _ast.Call(func=_ast.Name("__blast_cmp__", _ast.Load()),
                         args=[_ast.Constant(site), _ast.Constant(type(node.ops[0]).__name__),
                               node.left, node.comparators[0]], keywords=[])
        return _ast.copy_location(call, node)


# With MAP_BOOL=1 every `and`/`or` whose value is only used for its truth
# (if/while/assert tests, ternary tests, `not`, comprehension filters, and
# operands of such) records per evaluation which operands came out true (1),
# false (0) or were skipped by short-circuiting (None), in the same
# per-test, per-site structure as the comparisons. Operand i is wrapped as
# __blast_bb__(site, i, n, operand) and the whole as __blast_bend__(site, ...),
# which keeps short-circuiting and walrus bindings; evaluations in progress
# are told apart by the frame evaluating them.

_bool_active: dict = {}


def _bb(site, i, n, v):
    k = (site, id(sys._getframe(1)))
    if i == 0:
        vec = _bool_active[k] = [None] * n
    else:
        vec = _bool_active.get(k)
        if vec is None:
            return v
    try:
        vec[i] = 1 if v else 0
    except Exception:  # noqa: BLE001
        _bool_active.pop(k, None)
    return v


def _bend(site, v):
    vec = _bool_active.pop((site, id(sys._getframe(1))), None)
    per = _cmp.get(_key)
    if per is None:
        per = _cmp[_key] = {}
    obs = per.get(site, 0)
    if obs is not None:
        if obs == 0:
            obs = per[site] = set()
        if vec is None:
            per[site] = None  # lost track (exception inside an operand): no claim
        else:
            obs.add(tuple(vec))
            if len(obs) > CMP_CAP:
                per[site] = None
    return v


class _BoolRewrite(_ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def _truth(self, node):
        if isinstance(node, _ast.BoolOp):
            node.values = [self._truth(self.visit(v)) if isinstance(v, _ast.BoolOp) else self.visit(v)
                           for v in node.values]
            site = f"{self.rel}:{node.lineno}:{node.col_offset}"
            n = len(node.values)
            node.values = [_ast.copy_location(_ast.Call(
                func=_ast.Name("__blast_bb__", _ast.Load()),
                args=[_ast.Constant(site), _ast.Constant(i), _ast.Constant(n), v], keywords=[]), v)
                for i, v in enumerate(node.values)]
            return _ast.copy_location(_ast.Call(func=_ast.Name("__blast_bend__", _ast.Load()),
                                                args=[_ast.Constant(site), node], keywords=[]), node)
        return self.visit(node)

    def visit_If(self, node):
        node.test = self._truth(node.test)
        node.body = [self.visit(x) for x in node.body]
        node.orelse = [self.visit(x) for x in node.orelse]
        return node

    visit_While = visit_If

    def visit_IfExp(self, node):
        node.test = self._truth(node.test)
        node.body, node.orelse = self.visit(node.body), self.visit(node.orelse)
        return node

    def visit_Assert(self, node):
        node.test = self._truth(node.test)
        if node.msg is not None:
            node.msg = self.visit(node.msg)
        return node

    def visit_UnaryOp(self, node):
        if isinstance(node.op, _ast.Not):
            node.operand = self._truth(node.operand)
            return node
        return self.generic_visit(node)

    def visit_comprehension(self, node):
        node.target, node.iter = self.visit(node.target), self.visit(node.iter)
        node.ifs = [self._truth(x) for x in node.ifs]
        return node


def _install_cmp(bool_mode=False):
    import tempfile
    from importlib.machinery import SourceFileLoader

    _builtins.__blast_cmp__ = _bc
    _builtins.__blast_bb__ = _bb
    _builtins.__blast_bend__ = _bend
    sys.pycache_prefix = tempfile.mkdtemp(prefix="cmp_pyc")  # never reuse plain bytecode
    orig = SourceFileLoader.source_to_code

    def source_to_code(self, data, path, *a, **k):
        rel = _rel(path) if isinstance(path, str) else None
        if not rel or not rel.startswith("sqlglot/"):
            return orig(self, data, path, *a, **k)
        tree = (_BoolRewrite if bool_mode else _CmpRewrite)(rel).visit(_ast.parse(data, path))
        _ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)

    SourceFileLoader.source_to_code = source_to_code


if os.environ.get("MAP_CMP") == "1":
    _install_cmp()
elif os.environ.get("MAP_BOOL") == "1":
    _install_cmp(bool_mode=True)


# ---- dictionary keys -------------------------------------------------------
# With MAP_KEYS=1 every class-level dict with literal keys is swapped (after
# its module runs) for a dict subclass that records, per test, which keys'
# values were read; reads that see every value (values(), items(), ==, copies
# through those) count as "*". Key-only reads (iteration, `in`, len) don't
# depend on values and aren't recorded.

KEY_CAP = 4000
_keys: dict = {}  # key -> {dict id: set of key reprs, or None = too many}


def _krepr(k):
    t = type(k)
    if t in _SCALARS or (t is tuple and all(type(e) in _SCALARS for e in k)):
        return repr(k)
    return None


def _kread(d, k):
    per = _keys.get(_key)
    if per is None:
        per = _keys[_key] = {}
    s = per.get(d._bk, 0)
    if s is None:
        return
    if s == 0:
        s = per[d._bk] = set()
    r = "*" if k is _ALL else _krepr(k)
    s.add(r if r is not None else "*")
    if len(s) > KEY_CAP:
        per[d._bk] = None


class _KeyDict(dict):
    __slots__ = ("_bk",)

    def __getitem__(self, k):
        _kread(self, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _kread(self, k)
        return dict.get(self, k, default)

    def pop(self, k, *a):
        _kread(self, k)
        return dict.pop(self, k, *a)

    def setdefault(self, k, default=None):
        _kread(self, k)
        return dict.setdefault(self, k, default)

    def __iter__(self):  # makes dict(d) / {**d} go through keys() + __getitem__
        return dict.__iter__(self)

    def _all(self):
        _kread(self, _ALL)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def copy(self):
        self._all()
        return dict(dict.items(self))

    def popitem(self):
        self._all()
        return dict.popitem(self)

    def __eq__(self, other):
        self._all()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._all()
        return dict.__ne__(self, other)

    __hash__ = None

    def __or__(self, other):
        self._all()
        return dict.__or__(self, other)

    def __ror__(self, other):
        self._all()
        return dict.__ror__(self, other)

    def __repr__(self):
        self._all()
        return dict.__repr__(self)

    def __reduce_ex__(self, proto):
        self._all()
        return (dict, (dict(dict.items(self)),))


class _AllKeys:
    pass


_ALL = _AllKeys()


def _wrap_keys(module):
    import enum
    import inspect

    for obj in list(vars(module).values()):
        stack = [obj]
        while stack:
            cls = stack.pop()
            if not inspect.isclass(cls) or cls.__module__ != module.__name__ or "_blast_keys" in cls.__dict__:
                continue
            stack.extend(v for v in vars(cls).values() if inspect.isclass(v))
            if issubclass(cls, enum.Enum):
                continue
            for name, value in list(vars(cls).items()):
                if type(value) is not dict or name.startswith("__") or not value or len(value) > 20000:
                    continue
                if any(_krepr(k) is None for k in value):
                    continue
                d = _KeyDict(value)
                d._bk = f"{_rel(module.__file__) if getattr(module, '__file__', None) else module.__name__}" \
                        f"::{cls.__qualname__}::{name}"
                try:
                    setattr(cls, name, d)
                except (TypeError, AttributeError):
                    pass
            try:
                cls._blast_keys = True
            except (TypeError, AttributeError):
                pass


def _install_keys():
    from importlib.machinery import SourceFileLoader

    orig = SourceFileLoader.exec_module

    def exec_module(self, module):
        orig(self, module)
        if module.__name__.split(".")[0] == "sqlglot":
            _wrap_keys(module)

    SourceFileLoader.exec_module = exec_module


if os.environ.get("MAP_KEYS") == "1":
    _install_keys()

# With MAP_DISPATCH=1 the generator's per-class dispatch tables (expression
# class -> handler, built from TRANSFORMS and the *_sql method names) record
# which expression keys each generator class looked up, hits and misses
# alike: that is what a new, removed or renamed *_sql method can change.

_disp: dict = {}  # key -> set of (generator class "module:qualname", expression key)
_disp_mro: dict = {}  # generator class -> its bases ("module:qualname"), itself first


def _cname(c):
    return f"{c.__module__}:{c.__qualname__}"


class _DispDict(dict):
    __slots__ = ("_cls",)

    def get(self, k, default=None):
        try:
            _disp.setdefault(_key, set()).add((self._cls, getattr(k, "key", None) or k.__name__.lower()))
        except Exception:  # noqa: BLE001
            pass
        return dict.get(self, k, default)


def _install_dispatch():
    import sqlglot.generator as g

    orig = g._build_dispatch

    def wrap(cls, d):
        w = _DispDict(d)
        w._cls = _cname(cls)
        _disp_mro[w._cls] = [_cname(b) for b in cls.__mro__ if b is not object]
        return w

    def build(cls):
        return wrap(cls, orig(cls))

    g._build_dispatch = build
    for cls, d in list(g._DISPATCH_CACHE.items()):
        g._DISPATCH_CACHE[cls] = wrap(cls, d)


mon.use_tool_id(TOOL, "mapper")
mon.register_callback(TOOL, mon.events.LINE, _on_line)
if os.environ.get("MAP_LINES", "1") == "1":
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
    if os.environ.get("MAP_DISPATCH") == "1":
        _install_dispatch()


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
    if isinstance(c, tuple):
        return "MOD::" + (_mod_file(c[1]) or c[1])
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
    data = {"worker": worker, **sections_dump(with_names=True)}
    try:
        data["hooks"] = _hooks()
    except Exception as e:  # noqa: BLE001
        print("mapper: hooks failed", repr(e), file=sys.stderr)
        data["hooks"] = {}
    with open(os.path.join(out, f"{worker}.json"), "w") as fh:
        json.dump(data, fh)


def sections_dump(with_names=False):
    sections = {
        "hits": lambda: {k: {f: sorted(ls) for f, ls in v.items()} for k, v in _hits.items()},
        "events": lambda: {k: {e: sorted(s) for e, s in v.items()} for k, v in _events.items()},
        "types": lambda: {k: _class_keys(v) for k, v in _types.items()},
        **({"attr_names": lambda: [[_mod_file(m), q, a] for m, q, a in _attr_names]} if with_names else {}),
        "reads": lambda: {k: sorted({(i, _cls_key(c)) for i, c in v}) for k, v in _reads.items()},
        "cmp": lambda: {k: {s: (list(o) if o is not None else None) for s, o in v.items()}
                        for k, v in _cmp.items()},
        "keys": lambda: {k: {d: (sorted(o) if o is not None else None) for d, o in v.items()}
                         for k, v in _keys.items()},
        "disp": lambda: {k: sorted(map(list, v)) for k, v in _disp.items()},
        "disp_mro": lambda: dict(_disp_mro),
    }
    data = {}
    for name, fn in sections.items():
        try:
            data[name] = fn()
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            data[name] = {} if name != "attr_names" else []
            data.setdefault("errors", []).append(name)
    return data
