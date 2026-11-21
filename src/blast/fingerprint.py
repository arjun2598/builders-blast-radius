"""What importing sqlglot leaves behind, as one digest per name.

python -m blast.fingerprint <repo> <request.json>

request.json: {"files": {rel path: new text or null}, "watch": {rel path: [lines]}}

Imports every sqlglot module (patched texts replace the files named in
"files"; the rest load from <repo> as usual, cached bytecode included), then
prints JSON {"fp": {"<module>:<name>": digest}, "ran": {rel path: [lines of
"watch" that executed]}, "errors": [modules that failed to import]}.

The digest of a value covers everything import-time code could have
computed into it: literals, containers, object state, function defaults and
closure cells. It leaves out code objects, so a patch that only changes what
a function does when called gives the same digests unless running it while
importing produced different values.
"""

from __future__ import annotations

import enum
import functools
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import pkgutil
import re
import sys
import types

_SKIP = {"__file__", "__cached__", "__spec__", "__loader__", "__builtins__", "__path__", "__package__",
         "__doc__", "__dict__", "__weakref__", "__annotations__", "__annotate__", "__firstlineno__",
         "__static_attributes__"}


class _Overlay(importlib.abc.MetaPathFinder):
    def __init__(self, repo: str, files: dict):
        self.repo = repo
        self.files = files

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("sqlglot"):
            return None
        base = fullname.replace(".", "/")
        for rel, is_pkg in ((base + ".py", False), (base + "/__init__.py", True)):
            if rel in self.files:
                text = self.files[rel]
                if text is None:
                    return None if not os.path.exists(os.path.join(self.repo, rel)) else _gone(fullname)
                full = os.path.join(self.repo, rel)
                loader = _TextLoader(fullname, full, text)
                spec = importlib.machinery.ModuleSpec(fullname, loader, origin=full, is_package=is_pkg)
                if is_pkg:
                    spec.submodule_search_locations = [os.path.dirname(full)]
                spec.has_location = True
                return spec
        return None


def _gone(fullname):
    raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)


class _TextLoader(importlib.abc.Loader):
    def __init__(self, name, path, text):
        self.name, self.path, self.text = name, path, text

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.path
        exec(compile(self.text, self.path, "exec", dont_inherit=True), module.__dict__)


class Digest:
    def __init__(self):
        self.memo: dict[int, str] = {}
        self.keep: list = []  # holds objects so ids stay unique
        self.stack: set[int] = set()

    def __call__(self, obj) -> str:
        i = id(obj)
        if i in self.memo:
            return self.memo[i]
        if i in self.stack:
            return "cycle"
        self.stack.add(i)
        try:
            s = self._canon(obj)
        finally:
            self.stack.discard(i)
        d = hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        self.memo[i] = d
        self.keep.append(obj)
        return d

    def _canon(self, o) -> str:
        t = type(o)
        if o is None or t in (bool, int, float, complex, str, bytes):
            return f"{t.__name__}:{o!r}"
        if isinstance(o, type):
            return f"C:{getattr(o, '__module__', '')}.{getattr(o, '__qualname__', o.__name__)}"
        if isinstance(o, types.ModuleType):
            return f"M:{o.__name__}"
        if isinstance(o, enum.Enum):
            return f"E:{type(o).__module__}.{type(o).__qualname__}.{o.name}"
        if isinstance(o, types.FunctionType):
            parts = [f"F:{o.__module__}.{o.__qualname__}", self(o.__defaults__), self(o.__kwdefaults__)]
            if o.__closure__:
                for c in o.__closure__:
                    try:
                        parts.append(self(c.cell_contents))
                    except ValueError:
                        parts.append("empty")
            if o.__dict__:
                parts.append(self(dict(o.__dict__)))
            return "|".join(parts)
        if isinstance(o, (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
                          types.MethodWrapperType, types.ClassMethodDescriptorType, types.GetSetDescriptorType,
                          types.MemberDescriptorType)):
            return f"BF:{getattr(o, '__module__', '')}.{getattr(o, '__qualname__', repr(o))}"
        if isinstance(o, types.MethodType):
            return f"BM:{self(o.__self__)}.{o.__func__.__qualname__}|{self(o.__func__)}"
        if isinstance(o, functools.partial):
            return f"P:{self(o.func)}|{self(o.args)}|{self(o.keywords)}"
        if isinstance(o, (staticmethod, classmethod)):
            return f"{t.__name__}:{self(o.__func__)}"
        if isinstance(o, property):
            return f"prop:{self(o.fget)}|{self(o.fset)}|{self(o.fdel)}"
        if isinstance(o, dict):
            # sorted: dicts built from sets of classes come out in a different
            # order on every run, so no test can depend on that order
            return f"{t.__qualname__}{{" + ",".join(sorted(f"{self(k)}:{self(v)}" for k, v in list(o.items()))) + "}"
        if isinstance(o, (list, tuple)):
            return f"{t.__qualname__}[" + ",".join(self(x) for x in list(o)) + "]"
        if isinstance(o, (set, frozenset)):
            return f"{t.__qualname__}(" + ",".join(sorted(self(x) for x in list(o))) + ")"
        if isinstance(o, re.Pattern):
            return f"re:{o.pattern!r}:{o.flags}"
        parts = [f"O:{t.__module__}.{t.__qualname__}"]
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict):
            parts.append(self(dict(d)))
        for k in getattr(t, "__slots__", ()) if isinstance(getattr(t, "__slots__", ()), (tuple, list)) else ():
            if hasattr(o, k):
                parts.append(f"{k}={self(getattr(o, k))}")
        if len(parts) == 1:
            # opaque C object: fall back to its repr without addresses
            parts.append(re.sub(r" at 0x[0-9a-f]+", "", repr(o)))
        return "|".join(parts)


def _class_items(prefix, cls, mod, dg, out, seen):
    if id(cls) in seen:
        return
    seen.add(id(cls))
    for k, v in list(vars(cls).items()):
        if k in _SKIP:
            continue
        key = f"{prefix}.{k}"
        out[key] = dg(v)
        if isinstance(v, type) and v.__module__ == mod and v.__qualname__ == f"{cls.__qualname__}.{k}":
            _class_items(key, v, mod, dg, out, seen)
    out[prefix + ".__mro__"] = dg(tuple(c for c in cls.__mro__))


def snapshot() -> dict:
    dg = Digest()
    out: dict[str, str] = {}
    seen: set = set()
    for name in sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")):
        mod = sys.modules[name]
        if mod is None:
            continue
        for k, v in list(vars(mod).items()):
            if k in _SKIP:
                continue
            out[f"{name}:{k}"] = dg(v)
            if isinstance(v, type) and v.__module__ == name and v.__qualname__ == k:
                _class_items(f"{name}:{k}", v, name, dg, out, seen)
    return out


def main(repo: str, req_path: str) -> None:
    req = json.load(open(req_path))
    files = req.get("files", {})
    watch = {os.path.join(repo, f): set(ls) for f, ls in req.get("watch", {}).items()}
    watch_all = bool(req.get("watch_all"))
    root = os.path.join(repo, "sqlglot") + os.sep
    sys.path.insert(0, repo)
    sys.meta_path.insert(0, _Overlay(repo, files))
    ran: dict[str, set] = {}
    mon = sys.monitoring
    tool = 4
    if watch or watch_all:
        mon.use_tool_id(tool, "fingerprint")

        def on_line(code, line):
            f = code.co_filename
            if watch_all and f.startswith(root):
                ran.setdefault(f, set()).add(line)
                return mon.DISABLE
            if f not in watch:
                return mon.DISABLE
            if line in watch[f]:
                ran.setdefault(f, set()).add(line)
                return mon.DISABLE
            return None

        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.set_events(tool, mon.events.LINE)
    errors = []
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:  # noqa: BLE001
            errors.append(info.name)
    # modules the patch adds are not found by the walk above if their package is new
    for rel, text in files.items():
        if text is not None and rel.startswith("sqlglot/") and rel.endswith(".py"):
            name = rel[:-3].replace("/", ".").removesuffix(".__init__")
            if name not in sys.modules:
                try:
                    importlib.import_module(name)
                except Exception:  # noqa: BLE001
                    errors.append(name)
    if watch or watch_all:
        mon.set_events(tool, 0)
        mon.free_tool_id(tool)
    fp = snapshot()
    rel_ran = {os.path.relpath(f, repo): sorted(ls) for f, ls in ran.items()}
    sys.stdout.write(json.dumps({"fp": fp, "ran": rel_ran, "errors": sorted(errors)}))


if __name__ == "__main__":
    main(*sys.argv[1:])
