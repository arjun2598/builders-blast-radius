"""From a parsed patch to the set of tests that could possibly notice it.

Every rule here errs toward including tests: the runner then executes the
candidates and only the ones that really fail are reported. Missing a
candidate costs 300 s; an extra candidate costs only its CPU time.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
from dataclasses import dataclass, field

from . import cmpcheck
from .patch import FileDiff


@dataclass
class Selection:
    tests: set[int] = field(default_factory=set)
    everything: bool = False  # can't reason about it: every test is a candidate
    notes: list[str] = field(default_factory=list)


class Map:
    def __init__(self, path: str, tests: list[dict]):
        with gzip.open(path, "rt") as fh:
            m = json.load(fh)
        self.lines = m["lines"]
        self.imports = {f: set(ls) for f, ls in m["import"].items()}
        self.users = m["users"]
        self.opens = m["opens"]
        self.lists = m["lists"]
        self.popen = set(m["popen"])
        self.classes = m.get("classes", {})
        self.hooks = m.get("hooks", {})
        self.subtasks = {int(k): v for k, v in m.get("subtasks", {}).items()}
        self.cmp: cmpcheck.CmpMap | None = None  # comparison operands, set by the caller
        self.keys: cmpcheck.KeyMap | None = None  # dict keys read, set by the caller
        attrs = m.get("attrs", {"sets": [], "attr": {}, "import": {}})
        self.attr_sets, self.attr_index, self.attr_import = attrs["sets"], attrs["attr"], attrs["import"]
        self.tests = tests
        self._file_users: dict[str, set[int]] = {}
        self.sources: dict[str, str] = {}  # filled by the caller: repo .py texts
        self.by_file: dict[str, list[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])

    def all_subtasks(self) -> set[int]:
        return {10_000_000 + n * 1000 + i for n, k in self.subtasks.items() for i in range(k)}

    def file_users(self, f: str) -> set[int]:
        """Tests that ran a line of f or instantiated a class defined in it."""
        if f not in self._file_users:
            out = set(self.users.get(f, ()))
            prefix = f + "::"
            for k, v in self.classes.items():
                if k.startswith(prefix):
                    out.update(v)
            self._file_users[f] = out
        return self._file_users[f]

    def attr_readers(self, key: str) -> set[int]:
        i = self.attr_index.get(key)
        return set(self.attr_sets[i]) if i is not None else set()

    def line_tests(self, f: str, lines) -> set[int]:
        entry = self.lines.get(f)
        if not entry:
            return set()
        idx, sets = entry["line"], entry["sets"]
        out: set[int] = set()
        seen: set[int] = set()
        for l in lines:
            k = idx.get(str(l))
            if k is not None and k not in seen:
                seen.add(k)
                out.update(sets[k])
        return out


class Scopes:
    """Where each line of a module sits: innermost function body (runtime) or
    innermost class body (import-time, but owned by that class)."""

    def __init__(self, tree: ast.AST):
        self.funcs: list[tuple[int, int, int]] = []  # (start, end, header line or 0)
        self.classes: list[tuple[int, int, str]] = []  # (start, end, qualname)
        self._walk(tree, "")

    def _walk(self, node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                q = prefix + child.name
                self.classes.append((child.lineno, child.end_lineno, q))
                self._walk(child, q + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.append((child.body[0].lineno, child.end_lineno, 0))
                self._walk(child, prefix + child.name + ".<locals>.")
            else:
                if isinstance(child, ast.Lambda):
                    # the lambda's first line also holds the code around it
                    self.funcs.append((child.lineno, child.end_lineno, child.lineno))
                self._walk(child, prefix)

    @staticmethod
    def _inner(spans, line):
        best = None
        for span in spans:
            if span[0] <= line <= span[1] and (best is None or span[1] - span[0] < best[1] - best[0]):
                best = span
        return best

    def func(self, line):
        return self._inner(self.funcs, line)

    def cls(self, line):
        return self._inner(self.classes, line)


def _assigned_names(tree: ast.AST, lines: set[int]) -> set[str]:
    """Names bound by the module/class-level statements that cover lines."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(node.lineno <= l <= (node.end_lineno or node.lineno) for l in lines):
            continue
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.add(n.id)
    return names


def _maybe_cosmetic(fd: FileDiff) -> bool:
    """Cheap pre-check: could this diff be comments/whitespace only? Lines
    with quotes are kept whole (a '#' may sit inside a string)."""
    def norm(lines):
        out = []
        for l in lines:
            if "'" not in l and '"' not in l:
                l = l.split("#", 1)[0]
            l = "".join(l.split())
            if l:
                out.append(l)
        return "".join(out)

    for h in fd.hunks:
        if norm(l[1:] for l in h.lines if l[:1] == "-") != norm(l[1:] for l in h.lines if l[:1] == "+"):
            return False
    return True


def _strip_docless(src: str) -> str | None:
    try:
        return ast.dump(ast.parse(src), include_attributes=False)
    except SyntaxError:
        return None


def select(diffs: list[FileDiff], old: dict, new: dict, m: Map) -> Selection:
    """old/new map path -> text (None if missing)."""
    sel = Selection()
    for fd in diffs:
        sub = Selection()
        _select_file(fd, old.get(fd.old_path) if fd.old_path else None,
                     new.get(fd.new_path) if fd.new_path else None, m, sub)
        sel.tests |= sub.tests
        sel.notes += sub.notes
        if sub.everything:
            sel.everything = True
            break
    return sel


def _parent_lists(path: str, m: Map) -> set[int]:
    out: set[int] = set()
    d = os.path.dirname(path)
    while True:
        out.update(m.lists.get(d or ".", ()))
        if not d:
            return out
        d = os.path.dirname(d)


def _select_file(fd: FileDiff, old_src, new_src, m: Map, sel: Selection) -> None:
    path = fd.path
    is_py = path.endswith(".py")
    relevant = path.startswith(("sqlglot/", "tests/", "sqlglotc/")) or path in m.opens
    if not relevant:
        sel.notes.append(f"ignored {path}")
        return

    # Anything that reads the file, or lists its directory when it appears,
    # disappears or moves.
    for p in {fd.old_path, fd.new_path} - {None}:
        sel.tests.update(m.opens.get(p, ()))
    if fd.old_path != fd.new_path or fd.binary or fd.old_mode != fd.new_mode:
        for p in {fd.old_path, fd.new_path} - {None}:
            sel.tests.update(_parent_lists(p, m))
        sel.tests.update(m.popen)

    if not is_py:
        known = any(p in m.opens for p in (fd.old_path, fd.new_path) if p)
        if fd.old_path is None:
            sel.notes.append(f"new data file {path}")  # only directory listers can see it
            return
        if not known and path.startswith(("sqlglot/", "tests/")):
            sel.everything = True
            sel.notes.append(f"unmapped data file {path}")
        return

    if old_src is None or fd.old_path is None:
        # a new module: only reachable via imports in other changed files,
        # package walkers or subprocesses
        pkg_init = os.path.join(os.path.dirname(path), "__init__.py")
        sel.tests.update(m.users.get(pkg_init, ()))
        sel.tests.update(_parent_lists(path, m))
        sel.tests.update(m.popen)
        sel.notes.append(f"new module {path}")
        return

    known = fd.old_path in m.lines or fd.old_path in m.imports
    if not known:
        if path.startswith("tests/") and path in m.by_file:
            sel.tests.update(m.by_file[path])
        elif path.startswith("sqlglot/"):
            sel.everything = True
            sel.notes.append(f"unmapped module {path}")
        return

    if new_src is None or (fd.new_path and fd.new_path != fd.old_path):
        # deleted or moved module: every importer breaks, directly or not
        sel.everything = True
        sel.notes.append(f"module removed/moved {path}")
        return

    try:
        tree = ast.parse(old_src)
    except SyntaxError:
        sel.everything = True
        return
    try:
        compile(new_src, path, "exec", dont_inherit=True, flags=ast.PyCF_ONLY_AST)
        new_dump = _strip_docless(new_src) if _maybe_cosmetic(fd) else ""
    except SyntaxError:
        new_dump = None
    if new_dump is None:
        # the patched file does not compile: anything importing it fails,
        # including lazily loaded modules nobody traced by line
        sel.everything = True
        sel.notes.append(f"syntax error {path}")
        return
    if _maybe_cosmetic(fd) and _strip_docless(old_src) == new_dump:
        # only comments / formatting changed: runtime code is identical, but
        # line numbers move, so still run what executes the touched functions
        scopes = Scopes(tree)
        for line in fd.changed_old_lines():
            span = scopes.func(line)
            if span:
                sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
        sel.notes.append(f"comment/format only {path}")
        return

    try:
        new_tree = ast.parse(new_src)
    except SyntaxError:  # handled above, kept for safety
        sel.everything = True
        return
    edit = None
    if m.cmp is not None and path.startswith("sqlglot/"):
        try:
            edit = cmpcheck.find_edit(old_src, new_src, tree, new_tree)
        except Exception:  # noqa: BLE001
            edit = None
    res = Resolver(m, sel)
    if m.keys is not None and path.startswith("sqlglot/"):
        try:
            res.dict_edit = cmpcheck.find_dict_entry_edit(old_src, new_src, tree, new_tree)
        except Exception:  # noqa: BLE001
            res.dict_edit = None
    ctx = res.ctx(path, old_src)
    try:
        _resolve_change(fd, tree, new_tree, ctx, res, m, sel)
    except OverBudget:
        if sel.everything:
            return
        # too far-reaching to follow precisely: users of the file, and users
        # of every file that mentions a name the change touched
        sel.tests |= m.file_users(path)
        names = res.touched | res.removed_names | res.added_names | _direct_names(fd, ctx)
        if names:
            pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
            for f, text in m.sources.items():
                if f != path and pat.search(text):
                    sel.tests |= m.file_users(f)
        sel.notes.append(f"resolver over budget for {path}: broad fallback")
    if edit is not None:
        _narrow_compare(fd, edit, m, sel)
    elif path.startswith("sqlglot/"):
        try:
            span = cmpcheck.find_handler_edit(old_src, new_src, tree, new_tree)
        except Exception:  # noqa: BLE001
            span = None
        if span is not None and not any(l in m.imports.get(path, ()) for l in range(span[0], span[1] + 1)):
            before = len(sel.tests) if not sel.everything else len(m.tests)
            sel.everything = False
            sel.tests = m.line_tests(path, range(span[0], span[1] + 1)) | set(m.opens.get(path, ()))
            sel.notes.append(f"handler types {path}:{span[0]}: {before} -> {len(sel.tests)}")
    sel.tests |= m.popen


def _narrow_compare(fd: FileDiff, edit, m: Map, sel: Selection) -> None:
    """The file's only change is one comparison: keep the tests whose
    recorded evaluations could come out differently, plus tests the operand
    pass didn't map and tests that read the source file."""
    r = cmpcheck.narrow(edit, fd.path, m.cmp)
    if r is None:
        sel.notes.append(f"comparison edit {fd.path}:{edit.site[0]} not judged")
        return
    hit, safe = r
    if sel.everything:
        base = {t["number"] for t in m.tests} | m.all_subtasks()
        sel.everything = False
    else:
        base = set(sel.tests)
    sel.tests = (base - safe) | set(m.opens.get(fd.path, ()))
    sel.notes.append(f"comparison edit {fd.path}:{edit.site[0]} {edit.op_old}->{edit.op_new}: "
                     f"{len(base)} -> {len(sel.tests)}")

def _direct_names(fd: FileDiff, ctx: "Ctx") -> set[str]:
    """Names bound by the module- and class-level statements the diff touches."""
    out: set[str] = set()
    for line in fd.changed_old_lines():
        if ctx.scopes.func(line) is not None:
            continue
        c = ctx.scopes.cls(line)
        node = ctx.class_node(c[2]) if c else None
        stmts = node.body if node is not None else ctx.tree.body
        for st in stmts:
            if st.lineno <= line <= st.end_lineno:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    out.add(st.name)
                else:
                    out |= _targets(st) or set()
        if node is not None:
            out.add(node.name)
    return out


def _resolve_change(fd, tree, new_tree, ctx, res, m, sel) -> None:
    path = fd.path
    new_st = Stmts(new_tree)
    removed, added, new_to_old = _line_maps(fd)
    res.removed_names = _bound_names(tree) - _bound_names(new_tree)
    res.added_names = _bound_names(new_tree) - _bound_names(tree)

    for line in removed:
        res.line(path, ctx, line)
    # names bound by removed class-level statements, per class
    removed_bound: dict[str, set[str]] = {}
    for line in removed:
        c = ctx.scopes.cls(line)
        if c is None or ctx.scopes.func(line) is not None:
            continue
        cnode = ctx.class_node(c[2])
        stmt = next((x for x in (cnode.body if cnode else []) if x.lineno <= line <= x.end_lineno), None)
        t = _targets(stmt) if stmt is not None else None
        if t:
            removed_bound.setdefault(c[2], set()).update(t)
    for line in added:
        anchor = _anchor(new_st, line, added, new_to_old)
        if anchor is None:
            res.module_fallback(path, "insert at module top")
            continue
        kind, old_line = anchor
        if kind == "line":
            node = ctx.st.innermost(old_line)
            if node is not None and not hasattr(node, "body") and any(
                node.lineno <= r <= node.end_lineno for r in removed
            ):
                continue  # an edited simple statement: its removed lines already cover it
            if ctx.scopes.cls(old_line) is not None and ctx.scopes.func(old_line) is None:
                new_node = new_st.innermost(line)
                t = _targets(new_node) if new_node is not None else None
                qual = ctx.scopes.cls(old_line)[2]
                if t and t <= removed_bound.get(qual, set()):
                    continue  # same attributes re-bound: a value change, already resolved
                res.class_insert(path, ctx, old_line)
                continue
            res.line(path, ctx, old_line)
        else:  # first statement of a function body: whoever calls the function
            span = ctx.scopes.func(old_line)
            if span is not None:
                sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
            else:
                res.line(path, ctx, old_line)
    for name in sorted(res.added_names):
        res.module_names(path, ctx, {name}, set(), 0)


def _bound_names(tree: ast.AST) -> set[str]:
    """Names a module binds at top level."""
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                out.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
    return out


class Ctx:
    """Parsed view of one source file."""

    def __init__(self, path: str, src: str):
        self.path = path
        self.src = src
        self.tree = ast.parse(src)
        self.scopes = Scopes(self.tree)
        self.st = Stmts(self.tree)
        self.lines = src.split("\n")
        self._names: dict[str, list[int]] | None = None
        self.imports_from: dict[str, str] = {}  # local alias -> imported name
        self.star = False
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == "*":
                        self.star = True
                    else:
                        self.imports_from[a.asname or a.name] = a.name

    def name_refs(self, name: str) -> list[int]:
        """Lines that read the bare name."""
        if self._names is None:
            self._names = {}
            for n in ast.walk(self.tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    self._names.setdefault(n.id, []).append(n.lineno)
        return self._names.get(name, [])

    def top_stmt(self, line: int):
        for node in self.tree.body:
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            if start <= line <= node.end_lineno:
                return node
        return None

    def class_node(self, qual: str):
        node = self.tree
        for part in qual.split("."):
            node = next((c for c in getattr(node, "body", []) if isinstance(c, ast.ClassDef) and c.name == part), None)
            if node is None:
                return None
        return node


MAX_DEPTH = 4
MAX_LINES = int(os.environ.get("BLAST_RESOLVE_LINES", "300"))
MAX_SECONDS = float(os.environ.get("BLAST_RESOLVE_SECONDS", "0.4"))


class OverBudget(Exception):
    pass


class Resolver:
    """Turns "this line changed" into tests, following module-level names and
    class attributes to the code that reads them, across files."""

    def __init__(self, m: Map, sel: Selection):
        self.m = m
        self.sel = sel
        self.dict_edit: tuple[str, str, str] | None = None  # (class, attr, key) of a dict-entry edit
        self._ctx: dict[str, Ctx] = {}
        self.removed_names: set[str] = set()
        self.added_names: set[str] = set()
        self._done: set = set()
        self.touched: set[str] = set()  # module/class-level names followed so far
        self._lines = 0
        import time

        self._deadline = time.process_time() + MAX_SECONDS

    def _tick(self) -> None:
        import time

        self._lines += 1
        if self._lines > MAX_LINES or time.process_time() > self._deadline:
            raise OverBudget()

    def ctx(self, path: str, src: str | None = None) -> Ctx | None:
        if path not in self._ctx:
            text = src if src is not None else self.m.sources.get(path)
            if text is None:
                return None
            try:
                self._ctx[path] = Ctx(path, text)
            except SyntaxError:
                return None
        return self._ctx[path]

    # -- fallbacks -------------------------------------------------------
    def module_fallback(self, path: str, why: str) -> None:
        if ("modfb", path) in self._done:
            return
        self._done.add(("modfb", path))
        self.sel.tests |= self.m.file_users(path)
        self.sel.notes.append(f"file users {path}: {why}")

    def class_fallback(self, path: str, qual: str, why: str) -> None:
        if ("clsfb", path, qual) in self._done:
            return
        self._done.add(("clsfb", path, qual))
        c = self._ctx[path].class_node(qual) if path in self._ctx else None
        users = set(self.m.classes.get(f"{path}::{qual}", ()))
        if c is not None:
            users |= self.m.line_tests(path, range(c.lineno, c.end_lineno + 1))
        if not users:
            users = self.m.file_users(path)
        self.sel.tests |= users
        self.sel.notes.append(f"class users {qual}: {why}")

    # -- dispatch --------------------------------------------------------
    def line(self, path: str, ctx: Ctx, line: int, depth: int = 0) -> None:
        key = ("line", path, line)
        if key in self._done:
            return
        self._done.add(key)
        self._tick()
        m, sel = self.m, self.sel
        span = ctx.scopes.func(line)
        if span is not None and span[2] != line and line in m.imports.get(path, ()):
            # this function ran while modules were being imported: what it
            # computed (or raised) there is shared by every test, and the map
            # credits import-time work to no test
            sel.everything = True
            sel.notes.append(f"function line run at import time {path}:{line}")
            return
        if span is not None and span[2] != line:
            node = ctx.st.innermost(line)
            if STATEMENT_LEVEL and node is not None:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                        and ctx.st.in_function(node):
                    sel.tests |= m.line_tests(path, ctx.st.reach_lines(node))
                    return
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and line > ctx.st.header_end(node):
                    sel.tests |= m.line_tests(path, ctx.st.reach_lines(node.body[0]))
                    return
            sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
            return
        if span is not None:  # first line of a lambda: also the code around it
            sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
        c = ctx.scopes.cls(line)
        if c is not None:
            self.class_line(path, ctx, c, line, depth)
        else:
            self.module_line(path, ctx, line, depth)

    # -- class bodies ----------------------------------------------------
    def class_line(self, path, ctx: Ctx, c, line, depth) -> None:
        start, end, qual = c
        cnode = ctx.class_node(qual)
        if cnode is None or depth > MAX_DEPTH:
            return self.class_fallback(path, qual, "unresolved")
        stmt = next((s for s in cnode.body if s.lineno <= line <= s.end_lineno), None)
        if stmt is None:  # header, bases, decorators, keywords
            return self.class_fallback(path, qual, "class header")
        names = _targets(stmt)
        if names is None:
            return self.class_fallback(path, qual, f"class-level {type(stmt).__name__}")
        self.class_attrs(path, ctx, cnode, qual, names, {id(stmt)}, depth)

    def class_insert(self, path, ctx: Ctx, old_line) -> None:
        """A statement was added to a class body: it may define a new
        attribute that shadows a base class one. Readers can't be told apart,
        so use every user of the class."""
        c = ctx.scopes.cls(old_line)
        self.class_fallback(path, c[2], "statement added to class body")

    def class_attrs(self, path, ctx: Ctx, cnode, qual, names: set[str], seen_stmts: set, depth) -> None:
        m, sel = self.m, self.sel
        # other class-body statements computed from these names change too
        changed = True
        while changed:
            changed = False
            for s in cnode.body:
                if id(s) in seen_stmts or isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                used = {n.id for n in ast.walk(s) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                if used & names:
                    t = _targets(s)
                    if t is None:
                        return self.class_fallback(path, qual, "derived class-level statement")
                    seen_stmts.add(id(s))
                    names |= t
                    changed = True
        self.touched |= names
        hooks = m.hooks.get(f"{path}::{qual}", [])
        last = qual.split(".")[-1]
        for a in sorted(names):
            key = f"{path}::{qual}::{a}"
            if key not in m.attr_index:
                return self.class_fallback(path, qual, f"untracked attribute {a}")
            readers = m.attr_readers(key)
            de = self.dict_edit
            if de is not None and de[0] == qual and de[1] == a:
                narrowed = cmpcheck.key_readers(m.keys, path, qual, a, de[2], readers)
                if narrowed is not None:
                    sel.notes.append(f"dict entry {a}[{de[2][:30]}]: {len(readers)} -> {len(narrowed)} readers")
                    readers = narrowed
            sel.tests |= readers
            for k in m.attr_import.get(key, ()):
                f, _, q = k.partition("::")
                if f == "MOD":  # read while module q was being imported
                    sel.tests |= m.file_users(q)
                    continue
                users = set(m.classes.get(k, ()))
                kctx = self.ctx(f)
                kn = kctx.class_node(q) if kctx else None
                if kn is not None:
                    users |= m.line_tests(f, range(kn.lineno, kn.end_lineno + 1))
                sel.tests |= users
            for f, s0, e0 in hooks:
                hctx = self.ctx(f)
                if hctx and re.search(rf"\b{re.escape(a)}\b", "\n".join(hctx.lines[s0 - 1:e0])):
                    self.class_fallback(path, qual, f"{a} used while creating classes")
                    break
            # textual reads like Base.Tokenizer.KEYWORDS elsewhere (often at import)
            pat = re.compile(rf"\b{re.escape(last)}\.{re.escape(a)}\b")
            for f, text in m.sources.items():
                if pat.search(text) is None:
                    continue
                fctx = self.ctx(f)
                if fctx is None:
                    continue
                for i, l in enumerate(fctx.lines, 1):
                    if pat.search(l) and not (f == path and cnode.lineno <= i <= cnode.end_lineno and
                                              ctx.scopes.func(i) is None):
                        self.line(f, fctx, i, depth + 1)
        sel.notes.append(f"attrs {qual}.{','.join(sorted(names))[:60]}")

    # -- module level ----------------------------------------------------
    def module_line(self, path, ctx: Ctx, line, depth) -> None:
        stmt = ctx.top_stmt(line)
        if stmt is None or depth > MAX_DEPTH:
            return self.module_fallback(path, "unresolved module line")
        if isinstance(stmt, ast.ClassDef):  # decorators
            return self.class_fallback(path, stmt.name, "class decorator")
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {stmt.name}  # header: defaults, decorators, signature
        else:
            names = _targets(stmt)
            if names is None:
                return self.module_fallback(path, f"module-level {type(stmt).__name__}")
        self.module_names(path, ctx, names, {id(stmt)}, depth)

    def module_names(self, path, ctx: Ctx, names: set[str], skip: set, depth) -> None:
        m = self.m
        self.touched |= names
        if depth > MAX_DEPTH or any(n.startswith("__") for n in names):
            return self.module_fallback(path, f"names {sorted(names)[:3]}")
        for name in sorted(names):
            if ("modname", path, name) in self._done:
                continue
            self._done.add(("modname", path, name))
            if name in self.removed_names and any(ctx.scopes.func(l) is None for l in ctx.name_refs(name)):
                # still referenced while the module runs: importing it now fails
                self.sel.everything = True
                self.sel.notes.append(f"{path} uses removed name {name} at import")
                raise OverBudget()
            # inside the file: every statement that reads the name
            for l in ctx.name_refs(name):
                node = ctx.top_stmt(l)
                if node is not None and id(node) in skip:
                    continue
                self.line(path, ctx, l, depth + 1)
            # other files: imports of the name, star-imports, attribute and string access
            word = re.compile(rf"\b{re.escape(name)}\b")
            attr = re.compile(rf"\.{re.escape(name)}\b|[\"']{re.escape(name)}[\"']")
            for f, text in m.sources.items():
                if f == path or not word.search(text):
                    continue
                fctx = self.ctx(f)
                if fctx is None:
                    continue
                aliases = {al for al, orig in fctx.imports_from.items() if orig == name}
                if name in self.removed_names and aliases:
                    # `from x import name` of a name that is gone fails at import
                    self.sel.everything = True
                    self.sel.notes.append(f"{f} imports removed name {name}")
                    raise OverBudget()
                if fctx.star:
                    aliases.add(name)
                for al in aliases:
                    for l in fctx.name_refs(al):
                        self.line(f, fctx, l, depth + 1)
                for i, l in enumerate(fctx.lines, 1):
                    if attr.search(l):
                        self.line(f, fctx, i, depth + 1)
        self.sel.notes.append(f"module names {path} {sorted(names)[:4]}")


def _targets(stmt) -> set[str] | None:
    """Names a simple binding statement sets; None if it isn't one."""
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        if any(a.name == "*" for a in stmt.names):
            return None
        return {(a.asname or a.name).split(".")[0] for a in stmt.names}
    else:
        return None
    out: set[str] = set()
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif not isinstance(n, (ast.Tuple, ast.List, ast.Starred, ast.Load, ast.Store)):
                return None  # attribute / subscript targets
    return out or None


STATEMENT_LEVEL = os.environ.get("BLAST_STATEMENT_LEVEL", "1") == "1"
_BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")


class Stmts:
    """Statement geometry of one module: spans, parents and siblings."""

    def __init__(self, tree: ast.AST):
        self.nodes: list[ast.stmt] = []
        self.parent: dict[int, ast.AST] = {}
        self.block: dict[int, list] = {}
        self.funcs: set[int] = set()  # id of stmts inside some function body
        self._walk(tree, None, False)

    def _children(self, node):
        for field in _BLOCKS:
            for child in getattr(node, field, None) or ():
                if isinstance(child, (ast.excepthandler, ast.match_case)):
                    yield field, child, child.body
                else:
                    yield field, child, None

    def _walk(self, node, parent, in_func):
        for field in _BLOCKS:
            items = getattr(node, field, None) or ()
            for child in items:
                if isinstance(child, (ast.excepthandler, ast.match_case)):
                    for sub in child.body:
                        self._add(sub, node, child.body, in_func)
                else:
                    self._add(child, node, items, in_func)

    def _add(self, child, parent, block, in_func):
        if not isinstance(child, ast.stmt):
            return
        self.nodes.append(child)
        self.parent[id(child)] = parent
        self.block[id(child)] = block
        if in_func:
            self.funcs.add(id(child))
        inner = in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        if isinstance(child, ast.ClassDef):
            inner = False if not in_func else True
        self._walk(child, child, inner)

    def in_function(self, node) -> bool:
        return id(node) in self.funcs

    def innermost(self, line: int):
        best = None
        for n in self.nodes:
            if n.lineno <= line <= (n.end_lineno or n.lineno):
                if best is None or (n.end_lineno - n.lineno) <= (best.end_lineno - best.lineno):
                    best = n
        return best

    @staticmethod
    def header_end(node) -> int:
        return node.body[0].lineno - 1 if getattr(node, "body", None) else node.end_lineno

    def own_lines(self, node) -> set[int]:
        lines = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lines |= {d.lineno for d in node.decorator_list}
        for field in _BLOCKS:
            for child in getattr(node, field, None) or ():
                body = child.body if isinstance(child, (ast.excepthandler, ast.match_case)) else [child]
                for sub in body:
                    lines -= set(range(sub.lineno, (sub.end_lineno or sub.lineno) + 1))
        return lines

    def reach_lines(self, node) -> set[int]:
        """Lines whose execution shows a test reached this statement. A code-
        less header (try:, else:) is reached when its first child runs."""
        lines = self.own_lines(node)
        body = getattr(node, "body", None)
        if body and isinstance(body, list) and isinstance(body[0], ast.stmt):
            lines |= self.own_lines(body[0])
        return lines


def _line_maps(fd: FileDiff):
    """(removed old lines, added new lines, new->old map for kept lines)."""
    removed, added, mapping = set(), set(), {}
    old_pos = new_pos = 1
    for h in fd.hunks:
        new_start = h.old_start + (new_pos - old_pos)  # kept lines between hunks shift uniformly
        while old_pos < h.old_start:
            mapping[new_pos] = old_pos
            old_pos += 1
            new_pos += 1
        for l in h.lines:
            tag = l[:1]
            if tag == " ":
                mapping[new_pos] = old_pos
                old_pos += 1
                new_pos += 1
            elif tag == "-":
                removed.add(old_pos)
                old_pos += 1
            elif tag == "+":
                added.add(new_pos)
                new_pos += 1
    return removed, added, _Tail(mapping, old_pos - new_pos)


class _Tail(dict):
    """new->old for lines after the last hunk shift by a constant."""

    def __init__(self, mapping, delta):
        super().__init__(mapping)
        self.delta = delta
        self.last = max(mapping) if mapping else 0

    def get(self, k, default=None):
        if k in self:
            return self[k]
        return k + self.delta if k > self.last else default


def _anchor(st: Stmts, line: int, added: set[int], new_to_old):
    """For an added new line, the old line whose reach decides who can run it."""
    node = st.innermost(line)
    while node is not None:
        span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        kept = sorted(l for l in span if l not in added and new_to_old.get(l) is not None)
        if kept and not span <= added:
            # an existing statement gained lines: treat as a change to it
            own = [l for l in sorted(st.own_lines(node)) if l in kept]
            return ("line", new_to_old.get((own or kept)[0]))
        # a wholly new statement: reached iff the previous sibling (or parent) is
        block = st.block.get(id(node), [])
        idx = next((i for i, b in enumerate(block) if b is node), 0)
        if idx > 0:
            node = block[idx - 1]
            continue
        parent = st.parent.get(id(node))
        if parent is None or isinstance(parent, ast.Module):
            return None
        kept = [l for l in range(parent.lineno, (parent.end_lineno or parent.lineno) + 1)
                if l not in added and new_to_old.get(l) is not None]
        if not kept:
            node = parent
            continue
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_kept = [l for l in kept if l >= parent.body[0].lineno]
            return ("func", new_to_old.get((body_kept or kept)[-1 if not body_kept else 0]))
        own = [l for l in sorted(st.own_lines(parent)) if l in kept]
        return ("line", new_to_old.get((own or kept)[0]))
    return None
