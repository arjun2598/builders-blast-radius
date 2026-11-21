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
from dataclasses import dataclass, field

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
        self.tests = tests
        self._file_users: dict[str, set[int]] = {}
        self.sources: dict[str, str] = {}  # filled by the caller: repo .py texts
        self.by_file: dict[str, list[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])

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


def _strip_docless(src: str) -> str | None:
    try:
        return ast.dump(ast.parse(src), include_attributes=False)
    except SyntaxError:
        return None


def select(diffs: list[FileDiff], old: dict, new: dict, m: Map) -> Selection:
    """old/new map path -> text (None if missing)."""
    sel = Selection()
    for fd in diffs:
        _select_file(fd, old.get(fd.old_path) if fd.old_path else None,
                     new.get(fd.new_path) if fd.new_path else None, m, sel)
        if sel.everything:
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

    if new_src is None:  # deleted module: everything that touched it
        sel.tests.update(m.users.get(path, ()))
        sel.tests.update(m.popen)
        return

    try:
        tree = ast.parse(old_src)
    except SyntaxError:
        sel.everything = True
        return
    new_dump = _strip_docless(new_src)
    if new_dump is None:
        # the patched file does not compile: anything importing it fails,
        # including lazily loaded modules nobody traced by line
        sel.everything = True
        sel.notes.append(f"syntax error {path}")
        return
    if _strip_docless(old_src) == new_dump:
        # only comments / formatting changed: runtime code is identical, but
        # line numbers move, so still run what executes the touched functions
        scopes = Scopes(tree)
        for line in fd.changed_old_lines():
            span = scopes.func(line)
            if span:
                sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
        sel.notes.append(f"comment/format only {path}")
        return

    scopes = Scopes(tree)
    try:
        new_tree = ast.parse(new_src)
    except SyntaxError:  # handled above, kept for safety
        sel.everything = True
        return
    old_st, new_st = Stmts(tree), Stmts(new_tree)
    removed, added, new_to_old = _line_maps(fd)

    module_lines: set[int] = set()

    def resolve_old(line: int) -> None:
        """Tests that could notice a change at this old line."""
        span = scopes.func(line)
        if span is not None and span[2] == 0 or (span is not None and span[2] != line):
            if STATEMENT_LEVEL:
                node = old_st.innermost(line)
                if node is not None and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                        and old_st.in_function(node):
                    sel.tests |= m.line_tests(path, old_st.reach_lines(node))
                    return
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and line > old_st.header_end(node):
                    sel.tests |= m.line_tests(path, old_st.reach_lines(node.body[0]))
                    return
            sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
            return
        if span is not None:  # first line of a lambda: also the code around it
            sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
        c = scopes.cls(line)
        if c is not None:
            _class_users(path, c, m, sel)
        else:
            module_lines.add(line)

    for line in removed:
        resolve_old(line)
    for line in added:
        anchor = _anchor(new_st, line, added, new_to_old)
        if anchor is None:
            module_lines.add(1)
            continue
        kind, old_line = anchor
        if kind == "line":
            node = old_st.innermost(old_line)
            if node is not None and not hasattr(node, "body") and any(
                node.lineno <= r <= node.end_lineno for r in removed
            ):
                continue  # an edited simple statement: its removed lines already cover it

            resolve_old(old_line)
        else:  # first statement of a function body: whoever calls the function
            span = scopes.func(old_line)
            if span is not None:
                sel.tests |= m.line_tests(path, range(span[0], span[1] + 1))
            else:
                resolve_old(old_line)

    if module_lines:
        sel.tests |= m.file_users(path)
        names = _assigned_names(tree, module_lines)
        sel.notes.append(f"module-level {path} {sorted(names)[:5]}")
        _importers(path, names, m, sel)
    sel.tests |= m.popen


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


def _class_users(path: str, cls: tuple, m: Map, sel: Selection) -> None:
    start, end, qual = cls
    users = set(m.classes.get(f"{path}::{qual}", ()))
    # classmethods / staticmethods run without an instance
    users |= m.line_tests(path, range(start, end + 1))
    if not users:
        # never instantiated nor executed: fall back to the whole file's users
        users = m.file_users(path)
        sel.notes.append(f"unused class {qual}, file fallback")
    sel.tests |= users


def _importers(path: str, names: set[str], m: Map, sel: Selection) -> None:
    """Module-level names are often imported elsewhere: add users of every
    mapped file that mentions one of them."""
    if not names or not m.sources:
        return
    import re

    pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
    for f, text in m.sources.items():
        if f != path and pat.search(text):
            sel.tests |= m.file_users(f)
