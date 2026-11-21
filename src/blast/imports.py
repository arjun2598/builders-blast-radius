"""Module-level import graph of the repo, read statically from the sources.

When a patch makes a module fail while it is imported (a name it uses at
import time is gone), only code that imports it, directly or through other
modules, can fail. Tests reach a lazily loaded module either through
imports at the top of their test file or by running its code.
"""

from __future__ import annotations

import ast
import os
import re

# imported by `import sqlglot` at the pinned commit (checked when the map is
# rebuilt; the static walk below adds whatever these import in turn)
EAGER_ROOTS = ("sqlglot/__init__.py",)


def _is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _module_path(mod: str, files: set[str]) -> str | None:
    p = mod.replace(".", "/")
    if p + ".py" in files:
        return p + ".py"
    if p + "/__init__.py" in files:
        return p + "/__init__.py"
    return None


def _package(path: str) -> str:
    d = os.path.dirname(path)
    return d.replace("/", ".")


def lazy_tables(sources: dict[str, str]) -> dict[str, dict]:
    """Packages that hand out names from a module-level __getattr__ (lazy
    loading): package module name -> {name: files it may load, "*": every
    file any name may load}. Read from the package's dict literals
    (name -> module path or submodule name) and its TYPE_CHECKING imports;
    an f-string import under the package's own prefix may load any of its
    submodules."""
    files = set(sources)
    out: dict[str, dict] = {}
    for f, src in sources.items():
        if not f.endswith("__init__.py") or "def __getattr__" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        if not any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in tree.body):
            continue
        pkg = f[: -len("/__init__.py")].replace("/", ".")
        table: dict[str, set[str]] = {}
        everything: set[str] = set()

        def target(sv: str) -> str | None:
            return _module_path(sv, files) or _module_path(f"{pkg}.{sv}", files)

        for n in ast.walk(tree):
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        ts = {t for c in ast.walk(v) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                              for t in [target(c.value)] if t}
                        if ts:
                            table.setdefault(k.value, set()).update(ts)
                            everything |= ts
            elif isinstance(n, ast.If) and _is_type_checking(n.test):
                for st in ast.walk(n):
                    if isinstance(st, ast.ImportFrom) and st.module and not st.level:
                        t = _module_path(st.module, files)
                        if t:
                            for a in st.names:
                                table.setdefault(a.asname or a.name, set()).add(t)
                            everything.add(t)
            elif isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) == "import_module" \
                    and n.args and isinstance(n.args[0], ast.JoinedStr):
                head = "".join(str(v.value) for v in n.args[0].values[:1] if isinstance(v, ast.Constant))
                if head.startswith(pkg + "."):
                    everything |= {x for x in files if x.startswith(pkg.replace(".", "/") + "/") and x.endswith(".py")}
        # names the package binds itself never reach __getattr__
        for st in tree.body:
            names = []
            if isinstance(st, (ast.Assign, ast.AnnAssign)):
                for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                    names += [x.id for x in ast.walk(t) if isinstance(x, ast.Name)]
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(st.name)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                names += [a.asname or a.name.split(".")[0] for a in st.names if a.name != "*"]
            for x in names:
                table[x] = set()
        table["*"] = everything
        out[pkg] = table
    return out


def module_imports(path: str, src: str, files: set[str], sites: list | None = None,
                   lazy: dict | None = None) -> set[str] | None:
    """Files that importing `path` imports at module level (class bodies
    included, function bodies and TYPE_CHECKING blocks not). None if the
    source does not parse. Imports inside functions go to `sites` as
    (line, files) pairs: they run whenever the line does."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    out: set[str] = set()
    if sites is not None:
        # imports are statements: visit statement lists only, not expressions
        def stmts(body, in_fn):
            for st in body:
                if isinstance(st, (ast.Import, ast.ImportFrom)):
                    if in_fn:
                        tgt = _targets(st, path, files)
                        if tgt:
                            sites.append((st.lineno, tgt))
                    continue
                inner = in_fn or isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))
                for f in ("body", "orelse", "finalbody"):
                    sub = getattr(st, f, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                        stmts(sub, inner)
                for h in getattr(st, "handlers", ()) or ():
                    stmts(h.body, inner)
                for c in getattr(st, "cases", ()) or ():
                    stmts(c.body, inner)

        stmts(tree.body, False)

    def add_mod(mod: str):
        parts = mod.split(".")
        for i in range(1, len(parts) + 1):  # parent packages run first
            f = _module_path(".".join(parts[:i]), files)
            if f:
                out.add(f)

    def walk(body):
        for st in body:
            if isinstance(st, ast.Import):
                for a in st.names:
                    add_mod(a.name)
            elif isinstance(st, ast.ImportFrom):
                if st.level:
                    base = _package(path) if not path.endswith("__init__.py") else _package(path)
                    base = ".".join(base.split(".")[: len(base.split(".")) - (st.level - 1)])
                    mod = base + ("." + st.module if st.module else "")
                else:
                    mod = st.module or ""
                add_mod(mod)
                for a in st.names:
                    sub = _module_path(f"{mod}.{a.name}", files) or _module_path(f"{mod}.{a.name.lower()}", files)
                    if sub:
                        out.add(sub)
                    elif lazy and mod in lazy and a.name != "*":
                        # handed out by the package's __getattr__
                        out.update(lazy[mod][a.name] if a.name in lazy[mod] else lazy[mod]["*"])
            elif isinstance(st, ast.If):
                if not _is_type_checking(st.test):
                    walk(st.body)
                walk(st.orelse)
            elif isinstance(st, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                walk(st.body)
                for h in st.handlers:
                    walk(h.body)
                walk(st.orelse)
                walk(st.finalbody)
            elif isinstance(st, (ast.With, ast.ClassDef)):
                walk(st.body)

    walk(tree.body)
    out.discard(path)
    return out


def _targets(st, path: str, files: set[str]) -> set[str]:
    out: set[str] = set()

    def add_mod(mod: str):
        parts = mod.split(".")
        for i in range(1, len(parts) + 1):
            f = _module_path(".".join(parts[:i]), files)
            if f:
                out.add(f)

    if isinstance(st, ast.Import):
        for a in st.names:
            add_mod(a.name)
        return out
    if st.level:
        base = _package(path)
        base = ".".join(base.split(".")[: len(base.split(".")) - (st.level - 1)])
        mod = base + ("." + st.module if st.module else "")
    else:
        mod = st.module or ""
    add_mod(mod)
    for a in st.names:
        sub = _module_path(f"{mod}.{a.name}", files) or _module_path(f"{mod}.{a.name.lower()}", files)
        if sub:
            out.add(sub)
    return out


class Graph:
    def __init__(self, sources: dict[str, str], base: "Graph | None" = None, changed=()):
        """With base: a copy of base where only `changed` files (and, if the
        set of files differs, everything) are read again."""
        self.files = set(sources)
        self.deps: dict[str, set[str]] = {}
        self.bad = False
        self.sites: dict[str, list] = {}  # file -> [(line, imported files)] inside functions
        self.lazy = lazy_tables(sources)
        redo = set(sources) if base is None or base.files != self.files or getattr(base, "lazy", None) != self.lazy \
            else set(changed)
        for f, src in sources.items():
            if f not in redo and f in base.deps:
                self.deps[f], self.sites[f] = base.deps[f], base.sites[f]
                continue
            self.sites[f] = []
            d = module_imports(f, src, self.files, self.sites[f], self.lazy)
            if d is None:
                self.bad = True
                d = set()
            self.deps[f] = d
        if base is not None and base.bad and not redo >= set(sources):
            self.bad = True
        self.rdeps: dict[str, set[str]] = {}
        for f, ds in self.deps.items():
            for d in ds:
                self.rdeps.setdefault(d, set()).add(f)

    def closure(self, roots) -> set[str]:
        seen, todo = set(), list(roots)
        while todo:
            f = todo.pop()
            if f in seen:
                continue
            seen.add(f)
            todo.extend(self.deps.get(f, ()))
        return seen

    def importers(self, f: str) -> set[str]:
        """f and every module whose import imports f."""
        seen, todo = set(), [f]
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            todo.extend(self.rdeps.get(x, ()))
        return seen


_DOTTED = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_]\w*)+$")


def dynamic_sites(path: str, src: str) -> list:
    """(line, prefixes) for each importlib.import_module / __import__ call:
    the dotted-name prefixes the module it loads must start with, or None
    when anything is possible."""
    if "import_module" not in src and "__import__" not in src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [([i], None) for i, l in enumerate(src.split("\n"), 1) if "import_module" in l or "__import__" in l]
    modname = path[:-3].replace("/", ".").removesuffix(".__init__")
    pkg = modname if path.endswith("__init__.py") else modname.rpartition(".")[0]
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and _DOTTED.match(n.value)}
    # each call counts as its whole enclosing function: the cache check
    # guarding a lazy import runs even when the import itself is skipped
    spans = [(f.lineno, f.end_lineno) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
        if name not in ("import_module", "__import__") or not n.args:
            continue
        a = n.args[0]
        pre = None
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            pre = {a.value}
        elif isinstance(a, ast.JoinedStr):
            head = ""
            for v in a.values:
                if isinstance(v, ast.Constant):
                    head += str(v.value)
                elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id == "__name__":
                    head += modname
                elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id == "__package__":
                    head += pkg
                else:
                    break
            pre = {head} if head.strip(".") else None
        elif literals and path.startswith("sqlglot/") and not (len(n.args) > 1 or n.keywords):
            pre = literals  # a name from this file's own table of modules
        if pre is not None and any(p.startswith(".") for p in pre):
            pre = None  # relative to a package argument
        inner = min((sp for sp in spans if sp[0] <= n.lineno <= sp[1]), key=lambda sp: sp[1] - sp[0], default=None)
        # a load whose target is known (a family of modules) is typically a
        # lazy loader behind a cache check that skips it once loaded: count
        # the whole function. An open-ended one counts where it runs.
        whole = inner is not None and pre is not None
        out.append((range(inner[0], inner[1] + 1) if whole else range(n.lineno, n.end_lineno + 1), pre))
    return out
