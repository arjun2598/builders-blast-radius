"""Edits that leave a module's behaviour as it was, proven from the two ASTs.

`equivalent(old_tree, new_tree, path, sources)` says whether the patched
file computes the same as the pinned one up to:

- local variables renamed consistently inside one function (not its
  parameters, which callers may pass by keyword);
- names renamed consistently throughout the file when neither the old nor
  the new name appears in any other source file or in a string here, and no
  name-built lookup (`getattr(x, f"_parse_{...}")`, `<key>_sql` dispatch) can
  reach them;
- runs of plain function definitions reordered among themselves (module
  level or class body), when no decorator or default of the run refers to a
  name the run defines;
- runs of import statements reordered, and names reordered inside one
  `from x import a, b`.

Returns None when not proven, "same" when proven, and "probe" when the only
difference is the order of imports of sqlglot's own modules: import order
can matter there (circular imports, registries filled in import order), so
the caller checks it with the import-state probe.

What still sees a difference: code objects (line numbers, local names) and
the source text, which the caller charges as for a comment-only edit.
"""

from __future__ import annotations

import ast
import re

_SAFE_DECORATORS = {"staticmethod", "classmethod", "property", "overload", "abstractmethod",
                    "cache", "lru_cache", "cached_property", "wraps"}
_DYNAMIC = {"locals", "vars", "eval", "exec", "globals"}
_NAME_FIELDS = {
    ast.Name: ("id",), ast.FunctionDef: ("name",), ast.AsyncFunctionDef: ("name",),
    ast.ClassDef: ("name",), ast.arg: ("arg",), ast.ExceptHandler: ("name",),
    ast.alias: ("asname",), ast.Attribute: ("attr",), ast.keyword: ("arg",),
}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_CANON_FIELDS = frozenset(("body", "orelse", "finalbody", "names"))
_ATTR_KINDS = (ast.Attribute, ast.keyword)


class _Mismatch(Exception):
    pass


def equivalent(old: ast.Module, new: ast.Module, path: str, sources: dict) -> str | None:
    try:
        pairs: list = []  # (old name, new name, outermost function of old or None, node type)
        _walk(old, new, None, pairs)
    except (_Mismatch, RecursionError):
        return None
    verdict = "same"
    if _import_order(old) != _import_order(new):
        verdict = "probe"
    by_old: dict = {}
    for o, n, fn, kind in pairs:
        by_old.setdefault(o, set()).add((n, fn, kind))
    renamed = {o for o, n, _, _ in pairs if o != n}
    if not renamed:
        return verdict
    # one new name per old name, across the whole file: an old name kept in
    # some places and renamed in others (or two names merged) is no rename
    # (attribute and keyword names are a namespace of their own)
    to_new: dict = {}
    for o, targets in by_old.items():
        for n, fn, kind in targets:
            to_new.setdefault((o, id(fn), kind in _ATTR_KINDS), set()).add(n)
    if any(len(v) != 1 for v in to_new.values()):
        return None
    back: dict = {}
    for (o, f, ns), v in to_new.items():
        back.setdefault((next(iter(v)), f, ns), set()).add(o)
    if any(len(v) != 1 for v in back.values()):
        return None
    for o in renamed:
        targets = {t for t in by_old[o] if t[0] != o}
        if len({n for n, _, _ in targets}) != 1:
            return None
        n = next(iter(targets))[0]
        fns = {fn for _, fn, _ in targets}
        local_kinds = all(k in (ast.Name, ast.arg, ast.ExceptHandler, ast.alias) for _, _, k in targets)
        if local_kinds and len(fns) == 1 and None not in fns and _local_rename(next(iter(fns)), o, n):
            continue
        if _private_rename(old, new, o, n, path, sources):
            continue
        return None
    return verdict


# -- canonical order ---------------------------------------------------------

def _canon_field(node, field: str, value):
    """A field's value in canonical order: import runs and def runs sorted
    (statement lists), imported names sorted. Nothing is modified."""
    if field in ("body", "orelse", "finalbody"):
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            return _canon_body(value, isinstance(node, (ast.Module, ast.ClassDef)))
    elif field == "names" and (isinstance(node, ast.ImportFrom) or (isinstance(node, ast.Import) and len(value) > 1)):
        return sorted(value, key=lambda a: (a.name, a.asname or ""))
    return value


def _canon_body(stmts: list, defs_movable: bool) -> list:
    out, run, kind = [], [], None

    def flush():
        nonlocal run
        if kind == "import":
            bound = [(a.asname or a.name).split(".")[0] for s in run for a in s.names]
            if len(bound) == len(set(bound)) and not any(a.name == "*" for s in run for a in s.names):
                run.sort(key=lambda s: ast.dump(s))  # the last binding of a name wins
        elif kind == "def" and _movable(run):
            run.sort(key=lambda s: s.name)  # stable: same-name defs keep their order
        out.extend(run)
        run = []

    for s in stmts:
        k = "import" if isinstance(s, (ast.Import, ast.ImportFrom)) and not (
            isinstance(s, ast.ImportFrom) and s.module == "__future__") else \
            "def" if defs_movable and isinstance(s, _FUNCS) else None
        if k != kind:
            flush()
            kind = k
        if k is None:
            out.append(s)
        else:
            run.append(s)
    flush()
    return out


def _movable(run: list) -> bool:
    names = {s.name for s in run}
    for s in run:
        for d in s.decorator_list:
            base = d.func if isinstance(d, ast.Call) else d
            last = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else None
            if last not in _SAFE_DECORATORS:
                return False
        args = s.args.posonlyargs + s.args.args + s.args.kwonlyargs + [a for a in (s.args.vararg, s.args.kwarg) if a]
        evaluated = list(s.decorator_list) + list(s.args.defaults) + [d for d in s.args.kw_defaults if d] + \
            [a.annotation for a in args if a.annotation is not None] + ([s.returns] if s.returns else [])
        for e in evaluated:
            if any(isinstance(x, ast.Name) and x.id in names for x in ast.walk(e)):
                return False
    return True


def _import_order(tree: ast.Module) -> list:
    """The module-level sqlglot imports in order (relative ones too)."""
    out = []
    for s in ast.walk(tree):
        if isinstance(s, ast.ImportFrom) and (s.level or (s.module or "").split(".")[0] == "sqlglot"):
            out.append((s.level, s.module))
        elif isinstance(s, ast.Import):
            out += [(0, a.name) for a in s.names if a.name.split(".")[0] == "sqlglot"]
    return out


# -- parallel walk -------------------------------------------------------------

def _walk(a, b, fn, pairs) -> None:
    if type(a) is not type(b):
        raise _Mismatch
    if isinstance(a, list):
        if len(a) != len(b):
            raise _Mismatch
        for x, y in zip(a, b):
            _walk(x, y, fn, pairs)
        return
    if not isinstance(a, ast.AST):
        if a != b:
            raise _Mismatch
        return
    if fn is None and isinstance(a, _FUNCS):
        fn = a
    names = _NAME_FIELDS.get(type(a), ())
    for field in a._fields:
        x, y = getattr(a, field, None), getattr(b, field, None)
        if field in _CANON_FIELDS:
            x, y = _canon_field(a, field, x), _canon_field(b, field, y)
        if field in names:
            if x != y:
                if not (isinstance(x, str) and isinstance(y, str)):
                    raise _Mismatch
                pairs.append((x, y, fn, type(a)))
            elif isinstance(x, str):
                pairs.append((x, x, fn, type(a)))  # kept: o -> o rules out renaming o elsewhere
            continue
        if isinstance(a, (ast.Global, ast.Nonlocal)) and x != y:
            raise _Mismatch
        _walk(x, y, fn, pairs)


# -- renames -------------------------------------------------------------------

def _idents(node) -> list[tuple[str, ast.AST]]:
    out = []
    for x in ast.walk(node):
        for f in _NAME_FIELDS.get(type(x), ()):
            v = getattr(x, f, None)
            if isinstance(v, str):
                out.append((v, x))
        if isinstance(x, (ast.Global, ast.Nonlocal)):
            out += [(n, x) for n in x.names]
        if isinstance(x, ast.alias):
            out.append((x.name.split(".")[0], x))
    return out


def _strings(node) -> str:
    return "\n".join(x.value for x in ast.walk(node) if isinstance(x, ast.Constant) and isinstance(x.value, str))


def _local_rename(fn, o: str, n: str) -> bool:
    """o -> n renamed inside function fn (old tree), o a local of fn."""
    params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    params |= {a.arg for a in (fn.args.vararg, fn.args.kwarg) if a is not None}
    if o in params:
        return False
    ids = _idents(fn)
    if any(v == n for v, _ in ids):
        return False  # the new name already meant something here
    if any(isinstance(x, ast.Attribute) and v == o for v, x in ids):
        return False
    if any(isinstance(x, (ast.Global, ast.Nonlocal)) and o in x.names for x in ast.walk(fn)):
        return False
    if any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id in _DYNAMIC for x in ast.walk(fn)):
        return False
    if re.search(rf"\b{re.escape(o)}\b", _strings(fn)):
        return False
    # bound in fn's own scope (not only in a nested one): then every read of
    # it in fn and its nested scopes meant that local
    return any(isinstance(x, ast.Name) and x.id == o and isinstance(x.ctx, ast.Store) for x in _own_scope(fn)) or \
        any(isinstance(x, ast.ExceptHandler) and x.name == o for x in _own_scope(fn)) or \
        any(isinstance(x, ast.alias) and (x.asname or x.name.split(".")[0]) == o for x in _own_scope(fn))


def _own_scope(fn):
    stack = list(fn.body)
    while stack:
        x = stack.pop()
        yield x
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        stack.extend(ast.iter_child_nodes(x))


_DYN_LOOKUP = re.compile(r"""(?:getattr|hasattr)\(\s*[^,()]+,\s*f?["']([A-Za-z_]\w*)?(?:\{|["'])""")


def _private_rename(old, new, o: str, n: str, path: str, sources: dict) -> bool:
    """o -> n throughout the file, both names invisible from anywhere else."""
    if o.startswith("__") or n.startswith("__") or o.endswith("_sql") or n.endswith("_sql"):
        return False
    for x in ast.walk(old):  # namespaces read as dicts would see the new name
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and (
                x.func.id in ("locals", "globals", "eval", "exec") or (x.func.id == "vars" and not x.args)):
            return False
    if any(v == n for v, _ in _idents(old)) or any(v == o for v, _ in _idents(new)):
        return False  # not a rename of every occurrence
    word = re.compile(rf"\b(?:{re.escape(o)}|{re.escape(n)})\b")
    if word.search(_strings(old)) or word.search(_strings(new)):
        return False
    for f, text in sources.items():
        if f == path or not f.endswith(".py"):
            continue
        if word.search(text):
            return False
    for f, text in sources.items():
        if not f.endswith(".py"):
            continue
        for p in _DYN_LOOKUP.findall(text):
            if p and (o.startswith(p) or n.startswith(p)):
                return False
    return True


def invisible_name(name: str, path: str, new_text: str, sources: dict) -> bool:
    """`name` occurs once in the patched file (its new definition) and in no
    other source: nothing can read it, and it shadows nothing. Names that
    name-built lookups could reach (`<key>_sql`, getattr with an f-string
    prefix, dunders) don't count."""
    if name.startswith("__") or name.endswith("_sql"):
        return False
    word = re.compile(rf"(?<![\w.]){re.escape(name)}\b|\.{re.escape(name)}\b")
    if len(word.findall(new_text)) != 1:
        return False
    for f, text in sources.items():
        if f != path and name in text and re.search(rf"\b{re.escape(name)}\b", text):
            return False
    for f, text in sources.items():
        if f.endswith(".py"):
            for p in _DYN_LOOKUP.findall(text):
                if p and name.startswith(p):
                    return False
    return True


def inert_member(node) -> bool:
    """A class-body statement whose only effect is binding one name: a plain
    assignment of a literal, or a def with harmless decorators."""
    if isinstance(node, _FUNCS):
        return _movable([node]) and not any(
            isinstance(x, ast.Name) for d in node.args.defaults + [k for k in node.args.kw_defaults if k]
            for x in ast.walk(d))
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        value = node.value
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        value = node.value
    else:
        return False
    try:
        ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def member_name(node) -> str | None:
    if isinstance(node, _FUNCS):
        return node.name
    if isinstance(node, ast.Assign):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign):
        return node.target.id
    return None


def lambda_edits(old: ast.Module, new: ast.Module) -> list | None:
    """The lambdas of the pinned tree whose bodies alone differ, when that is
    the file's only difference and each sits in a module- or class-level
    statement outside any call (so nothing calls it while importing):
    calling them is the only way to see the change. None otherwise."""
    changed: list = []

    def walk(a, b, in_call: bool) -> None:
        if type(a) is not type(b):
            raise _Mismatch
        if isinstance(a, list):
            if len(a) != len(b):
                raise _Mismatch
            for x, y in zip(a, b):
                walk(x, y, in_call)
            return
        if not isinstance(a, ast.AST):
            if a != b:
                raise _Mismatch
            return
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.dump(a) != ast.dump(b):
                raise _Mismatch  # function bodies have their own line rules
            return
        if isinstance(a, ast.Lambda) and ast.dump(a.body) != ast.dump(b.body):
            if in_call or ast.dump(a.args) != ast.dump(b.args):
                raise _Mismatch
            changed.append(a)
            return
        for field in a._fields:
            walk(getattr(a, field, None), getattr(b, field, None),
                 in_call or isinstance(a, (ast.Call, ast.Decorator if hasattr(ast, "Decorator") else ast.Call)))

    try:
        walk(old, new, False)
    except (_Mismatch, RecursionError):
        return None
    return changed or None


def _funcs(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _literal(node) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        return _literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal(e) for e in node.elts)
    return False


def default_edits(old_src: str, new_src: str) -> list | None:
    """Functions of old_src whose parameter defaults are all that differ in
    new_src, every changed default a literal on both sides. A default is
    evaluated once, where the def runs, and read only by calls that leave
    the argument out, which run the body. None unless that is the only
    change."""
    if old_src.count("\n") != new_src.count("\n"):
        return None
    try:
        a, b = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return None
    fa, fb = _funcs(a), _funcs(b)
    if len(fa) != len(fb):
        return None
    changed = []
    for x, y in zip(fa, fb):
        dx = [*x.args.defaults, *x.args.kw_defaults]
        dy = [*y.args.defaults, *y.args.kw_defaults]
        if len(dx) != len(dy) or [d is None for d in dx] != [d is None for d in dy]:
            return None
        diff = [(p, q) for p, q in zip(dx, dy) if p is not None and ast.dump(p) != ast.dump(q)]
        if not diff:
            continue
        if not all(_literal(p) and _literal(q) for p, q in diff):
            return None
        changed.append(x)
    if not changed:
        return None
    for t in (a, b):
        for f in _funcs(t):
            f.args.defaults = [ast.Constant(0) for _ in f.args.defaults]
            f.args.kw_defaults = [None if d is None else ast.Constant(0) for d in f.args.kw_defaults]
    if ast.dump(a) != ast.dump(b):
        return None
    return changed
