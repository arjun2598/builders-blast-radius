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

from . import cmpcheck, refactor
from .patch import FileDiff


@dataclass
class Selection:
    tests: set[int] = field(default_factory=set)
    everything: bool = False  # can't reason about it: every test is a candidate
    notes: list[str] = field(default_factory=list)
    import_probe: dict = field(default_factory=dict)  # path -> function lines run at import time
    pair_files: set = field(default_factory=set)  # fixtures narrowed to the pool tasks of changed pairs


def load_map(path: str, cache: str | None = None) -> dict:
    """The decoded map: from cache (a pickle written at image build by
    blast.prebuild, used only if made from these exact bytes) or the JSON."""
    raw = open(path, "rb").read()
    if cache:
        import hashlib
        import pickle

        try:
            with open(cache, "rb") as fh:
                sha, m = pickle.load(fh)
            if sha == hashlib.sha256(raw).hexdigest():
                return m
        except (OSError, pickle.PickleError, EOFError, ValueError, TypeError):
            pass
    return json.loads(gzip.decompress(raw))


class Map:
    def __init__(self, path: str, tests: list[dict], cache: str | None = None):
        m = load_map(path, cache)
        self.lines = m["lines"]
        self.imports = {f: set(ls) for f, ls in m["import"].items()}
        self.users = m["users"]
        self.opens = m["opens"]
        self.lists = m["lists"]
        self.popen = set(m["popen"])  # of the commit being selected (see popen_for)
        self.popen_all = set(self.popen)  # tests that start subprocesses
        self._ctx_cache: dict = {}  # path -> Ctx of the pinned source
        self._import_tables: dict = {}  # path -> import_table of the pinned source (prebuilt)
        self.popen_mods: dict[int, set[str]] = {}  # their subprocesses' sqlglot modules, set by load_popen
        self.classes = m.get("classes", {})
        self.hooks = m.get("hooks", {})
        self.subtasks = {int(k): v for k, v in m.get("subtasks", {}).items()}
        self.patched: dict | None = None  # path -> patched text, of the commit being selected
        self.subtask_raise = set(m.get("subtask_raise", ()))  # skipped tasks raise SkippedTask (see runner)
        self.cmp: cmpcheck.CmpMap | None = None  # comparison operands, set by the caller
        self.bool: cmpcheck.CmpMap | None = None  # and/or operand truth values, set by the caller
        self.req: cmpcheck.ReqMap | None = None  # validations missing each argument, set by the caller
        self.keys: cmpcheck.KeyMap | None = None  # dict keys read, set by the caller
        self.needs: dict | None = None  # module file -> tests that imported it, set by load_needs
        self.needs_mapped: set[int] = set()
        self.disp: dict | None = None  # generator dispatch map, set by load_disp
        self.import_ok: set[str] = set()  # files whose import-time runs were checked to leave the same state
        attrs = m.get("attrs", {"sets": [], "attr": {}, "import": {}})
        self.attr_sets, self.attr_index, self.attr_import = attrs["sets"], attrs["attr"], attrs["import"]
        self.tests = tests
        self._file_users: dict[str, set[int]] = {}
        self.sources: dict[str, str] = {}  # filled by the caller: repo .py texts
        self.by_file: dict[str, list[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])

    def expr_keys(self) -> dict[str, set[str]]:
        """Expression class name -> keys of it and its subclasses, read
        statically from sqlglot/expressions (a class's key is its lowercased
        name)."""
        if getattr(self, "_expr_keys", None) is None:
            bases: dict[str, set[str]] = {}
            for f, text in self.sources.items():
                if not f.startswith("sqlglot/expressions/"):
                    continue
                for n in ast.parse(text).body:
                    if isinstance(n, ast.ClassDef):
                        bases[n.name] = {b.id if isinstance(b, ast.Name) else b.attr for b in n.bases
                                         if isinstance(b, (ast.Name, ast.Attribute))}
            subs: dict[str, set[str]] = {c: {c} for c in bases}
            changed = True
            while changed:
                changed = False
                for c, bs in bases.items():
                    for b in bs:
                        if b in subs and not subs[c] <= subs[b]:
                            subs[b] |= subs[c]
                            changed = True
            self._expr_keys = {c: {x.lower() for x in v} for c, v in subs.items()}
        return self._expr_keys

    def dyn_handler_sites(self) -> list:
        """(file, line, keys or None) for each getattr(..., f"..._sql") in
        sqlglot: keys are what an enclosing isinstance(x, exp.Cls) guard
        allows, None when unguarded."""
        if getattr(self, "_dyn_sites", None) is None:
            out = []
            for f, text in self.sources.items():
                if not f.startswith("sqlglot/") or "getattr" not in text or "_sql" not in text:
                    continue
                tree = ast.parse(text)
                parents: dict = {}
                for n in ast.walk(tree):
                    for ch in ast.iter_child_nodes(n):
                        parents[ch] = n
                for n in ast.walk(tree):
                    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr"
                            and len(n.args) >= 2 and isinstance(n.args[1], ast.JoinedStr)):
                        continue
                    tail = n.args[1].values[-1] if n.args[1].values else None
                    if not (isinstance(tail, ast.Constant) and str(tail.value).endswith("_sql")):
                        continue
                    keys = None
                    p = parents.get(n)
                    while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        t = p.test if isinstance(p, ast.If) else None
                        if isinstance(t, ast.Call) and isinstance(t.func, ast.Name) and t.func.id == "isinstance" \
                                and len(t.args) == 2 and isinstance(t.args[1], (ast.Attribute, ast.Name)):
                            cname = t.args[1].attr if isinstance(t.args[1], ast.Attribute) else t.args[1].id
                            if cname in self.expr_keys():
                                keys = self.expr_keys()[cname]
                                break
                        p = parents.get(p)
                    out.append((f, n.lineno, keys))
            self._dyn_sites = out
        return self._dyn_sites

    def load_disp(self, path: str) -> None:
        try:
            with gzip.open(path, "rt") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        d["mapped"] = set(d["mapped"])
        d["mro"] = {g: set(b) for g, b in d["mro"].items()}
        self.disp = d

    def import_table(self, f: str):
        """(modules f star-imports, {name: modules f imports it from}) of the
        pinned source; None stands for a module that can't be resolved."""
        if f not in self._import_tables:
            star, src, bound = set(), {}, set()
            try:
                tree = ast.parse(self.sources[f])
            except SyntaxError:
                star.add(None)
                tree = None
            for node in ast.walk(tree) if tree is not None else ():
                if isinstance(node, ast.ImportFrom):
                    mod = _abs_module(f, node)
                    for a in node.names:
                        if a.name == "*":
                            star.add(mod)
                        else:
                            src.setdefault(a.asname or a.name, set()).add(mod)
                            bound.add(f"{mod}.{a.name}" if mod else None)
                elif isinstance(node, ast.Import):
                    bound |= {a.name for a in node.names}
            self._import_tables[f] = (star, src, bound)
        return self._import_tables[f]

    def names_module(self, f: str, owners: set[str]) -> bool:
        """Can code in f get hold of one of these module objects: by
        importing it, by importing a package above it and reading the
        submodule attribute, or by loading modules dynamically?"""
        text = self.sources.get(f, "")
        if not f.endswith(".py") or _modname(f) in owners or _DYNAMIC_MODULE.search(text):
            return True
        _, _, bound = self.import_table(f)
        if None in bound or bound & owners:
            return True
        for o in owners:
            parts = o.split(".")
            for i in range(1, len(parts)):
                if ".".join(parts[:i]) in bound and re.search(rf"\.{re.escape(parts[i])}\b", text):
                    return True
        return False

    def load_popen(self, path: str) -> None:
        try:
            with open(path) as fh:
                self.popen_mods = {int(k): set(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError):
            self.popen_mods = {}

    def popen_for(self, changed: list[str]) -> set[int]:
        """Tests with subprocesses that can see the change: the coverage map
        can't follow them, so all of them unless the change is sqlglot code
        their subprocesses don't load (recorded at the pinned commit, plus
        whatever those modules import after the patch)."""
        if not self.popen_mods or not all(f.startswith("sqlglot/") and f.endswith(".py") for f in changed):
            return set(self.popen_all)
        g, og = self.import_graph(), self.old_import_graph()
        if g.bad or og.bad:
            return set(self.popen_all)
        out = set()
        for t in self.popen_all:
            mods = self.popen_mods.get(t)
            if mods is None or (g.closure(mods) | og.closure(mods)) & set(changed):
                out.add(t)
        return out

    def load_popen_lines(self, path: str) -> None:
        try:
            with open(path) as fh:
                self.popen_lines = {int(k): {f: set(v) for f, v in d.items()} for k, d in json.load(fh).items()}
        except (OSError, ValueError):
            self.popen_lines = {}

    def popen_untouched(self, t: int, diffs: list["FileDiff"], old: dict, new: dict) -> bool:
        """True when every change sits in bodies of functions that test t's
        subprocesses never ran at the pinned commit (data/popen_lines.json).
        A function that is never called runs none of its body, so such a
        change can't reach them. Headers, decorators, defaults, docstrings and
        anything outside a function run when the def does, so they count."""
        lines = getattr(self, "popen_lines", {}).get(t)
        if lines is None:
            return False
        for fd in diffs:
            if (fd.old_path != fd.new_path or not fd.path.startswith("sqlglot/") or not fd.path.endswith(".py")
                    or not old.get(fd.path) or not new.get(fd.path) or fd.binary):
                return False
            safe = _unrun_bodies(old[fd.path], fd.changed_old_lines(), lines.get(fd.path, set()))
            if safe is None:
                return False
            added = fd.changed_new_lines()
            new_lines = new[fd.path].splitlines()
            if any(n > len(new_lines) or new_lines[n - 1].rstrip("\r") != text for n, text in added.items()):
                return False
            got = _unrun_bodies(new[fd.path], set(added), None)
            if got is None or not got <= safe:
                return False
        return True

    def load_pairs(self, path: str) -> None:
        """data/pairs.json: test id -> {fixture: "none" | "tasks"}."""
        try:
            with open(path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        by_id = {t["id"]: t["number"] for t in self.tests}
        kinds: dict = {}
        for nid, files in d.items():
            if nid in by_id:
                for f, k in files.items():
                    kinds.setdefault(f, {})[by_id[nid]] = k
        self.pair_kinds = kinds

    def load_needs(self, path: str) -> None:
        try:
            with gzip.open(path, "rt") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        self.needs_mapped = set(d.pop("__mapped__", ()))
        self.needs = d

    @property
    def package_walkers(self) -> set[int]:
        """Tests in test files that walk packages or directories for modules
        (pkgutil, os.walk, glob): directory listings through the import
        system aren't recorded as listings."""
        if not hasattr(self, "_walkers"):
            pat = re.compile(r"walk_packages|iter_modules|os\.walk|\bglob\(|\.rglob\(|listdir|scandir")
            self._walkers = {n for f, ns in self.by_file.items()
                             if pat.search(self.sources.get(f, "")) for n in ns}
        return self._walkers

    @property
    def code_readers(self) -> set[int]:
        """Tests in files that look at code objects, frames or source text,
        where line numbers could show."""
        if not hasattr(self, "_code_readers"):
            pat = re.compile(r"__code__|co_\w+|getsource|f_lineno|tb_lineno|linecache|inspect\.|traceback\.extract")
            out: set[int] = set()
            for f, nums in self.by_file.items():
                if pat.search(self.sources.get(f, "")) or f not in self.sources:
                    out.update(nums)
            self._code_readers = out
        return self._code_readers

    def import_graph(self):
        """Static import graph of the patched tree (patched texts replace the
        pinned ones for the changed files)."""
        if getattr(self, "_graph", None) is None:
            from . import imports

            og = self.old_import_graph()
            srcs = dict(self.sources)
            changed = set()
            for f, text in getattr(self, "new_sources", {}).items():
                if f.endswith(".py") and f.startswith(("sqlglot/", "tests/")):
                    srcs[f] = "" if text is None else text  # gone: importers still point at it
                    changed.add(f)
            g = imports.Graph(srcs, og, changed)
            g.eager = g.closure(imports.EAGER_ROOTS)
            g.dynamic = {f: (imports.dynamic_sites(f, t) if f in changed or f not in og.dynamic else og.dynamic[f])
                         for f, t in srcs.items()}
            self._graph = g
        return self._graph

    def old_import_graph(self):
        if getattr(self, "_old_graph", None) is None:
            from . import prebuild

            self._old_graph = prebuild.graph(self.sources)
        return self._old_graph

    def import_breakers(self, f: str) -> set[int] | None:
        """Tests that import f, directly or through other modules, before or
        after the patch: the only tests that can see anything f does, or
        fail when importing it raises. None when that is every test (f is
        imported by `import sqlglot`)."""
        g = self.import_graph()
        og = self.old_import_graph()
        if g.bad or og.bad:
            return None
        imp = g.importers(f) | og.importers(f)
        if imp & (g.eager | og.eager):
            return None
        out = set(self.popen)
        for x in imp:
            if x.startswith("sqlglot/"):
                out |= self.file_users(x)
                out |= _parent_lists(x, self)
            else:
                out |= set(self.by_file.get(x, ()))
        if self.needs is not None:
            # recorded: the modules each test imported or asked for while it ran
            for x in imp:
                out.update(self.needs.get(x, ()))
            out |= {t["number"] for t in self.tests} - self.needs_mapped
            return out
        for x, sites in g.sites.items():
            for line, tgt in sites:
                if tgt & imp:
                    out |= self.line_tests(x, [line]) if x.startswith("sqlglot/") else set(self.by_file.get(x, ()))
        # dynamic imports that may name one of those modules
        names = {x[:-3].replace("/", ".").removesuffix(".__init__") for x in imp}
        for x, sites in g.dynamic.items():
            lines = [l for ls, pre in sites if pre is None or any(n.startswith(p) for p in pre for n in names) for l in ls]
            if lines:
                out |= self.line_tests(x, lines) if x.startswith("sqlglot/") else set(self.by_file.get(x, ()))
        return out

    @staticmethod
    def drop_tasks_of(tests: set[int], mapped, kept: set[int]) -> set[int]:
        """Tests minus the single pool tasks of mapped tests that were judged
        unaffected as a whole (not in kept): a task does part of its test's
        work, so it can't see what the whole test didn't."""
        return {t for t in tests
                if t < 10_000_000 or t in mapped or (n := (t - 10_000_000) // 1000) in kept or n not in mapped}

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


def _unrun_bodies(src: str, changed: set[int], ran: set[int] | None) -> set[str] | None:
    """Qualified names of the innermost functions whose bodies hold every
    changed line, or None if a line is outside every body. With ran (lines
    that ran), also None if any def of such a name ran a body line."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    defs: list[tuple[str, int, int, int]] = []  # qualname, body start, end, def start

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + ch.name
                body = ch.body
                first = body[0]
                start = first.lineno
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    start = first.end_lineno + 1  # the docstring is stored when the def runs
                defs.append((q, start, ch.end_lineno or ch.lineno, ch.lineno))
                walk(ch, q + ".<locals>.")
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    out = set()
    for ln in changed:
        inner = None
        for d in defs:
            if d[3] <= ln <= d[2] and (inner is None or d[3] >= inner[3]):
                inner = d
        if inner is None or not inner[1] <= ln <= inner[2]:
            return None
        out.add(inner[0])
    if ran is not None:
        for q, start, end, _ in defs:
            if q in out and any(start <= l <= end for l in ran):
                return None
    return out


def select(diffs: list[FileDiff], old: dict, new: dict, m: Map) -> Selection:
    """old/new map path -> text (None if missing)."""
    m.patched = new
    sel = Selection()
    m.new_sources = {fd.new_path or fd.old_path: (new.get(fd.new_path) if fd.new_path else None) for fd in diffs}
    if fd_old := [fd.old_path for fd in diffs if fd.old_path and fd.old_path != fd.new_path]:
        for p in fd_old:
            m.new_sources[p] = None
    m._graph = None
    m.popen = m.popen_for(list(m.new_sources))
    for t in sorted(m.popen):
        try:
            if m.popen_untouched(t, diffs, old, new):
                m.popen.discard(t)
                sel.notes.append(f"subprocesses of {t} never ran the changed bodies")
        except Exception:  # noqa: BLE001
            pass
    for fd in diffs:
        sub = Selection()
        _select_file(fd, old.get(fd.old_path) if fd.old_path else None,
                     new.get(fd.new_path) if fd.new_path else None, m, sub)
        if sub.everything and _bounded_by_imports(fd, m):
            hit = m.import_breakers(fd.path)
            if hit is not None:
                sub.everything = False
                sub.tests |= hit
                sub.notes.append(f"bounded by importers of {fd.path}: {len(hit)} tests")
        sel.tests |= sub.tests
        sel.notes += sub.notes
        for k, v in sub.import_probe.items():
            sel.import_probe.setdefault(k, set()).update(v)
        sel.pair_files |= sub.pair_files
        if sub.everything:
            sel.everything = True
            break
    return sel


_FILTER_DEF = '''def _filter_comments(s):
    return "\\n".join([line for line in s.splitlines() if line and not line.startswith("--")])
'''


def _fixture_lines_dropped(fd: FileDiff, old_src, new_src, m: Map) -> bool:
    """A .sql fixture edit that changes only lines the fixture loaders drop.
    Holds only while tests/helpers.py (unchanged by the patch) reads every
    fixture through _filter_comments and no other test code opens .sql files."""
    path = fd.path
    if not (path.startswith("tests/fixtures/") and path.endswith(".sql")) or fd.binary \
            or fd.old_path != fd.new_path or old_src is None or new_src is None:
        return False
    if "tests/helpers.py" in (m.patched or {}) or "tests/helpers.py" in m.new_sources:
        return False
    helpers = m.sources.get("tests/helpers.py") or ""
    if _FILTER_DEF not in helpers or helpers.count("open(") != 2 \
            or helpers.count("_filter_comments(f.read())") != 2:
        return False
    others = [f for f, t in m.sources.items() if f.startswith("tests/") and f != "tests/helpers.py"
              and t and any(".sql" in ln and ("open(" in ln or "read_text" in ln or "read_bytes" in ln)
                            for ln in t.split("\n"))]
    if others:
        return False

    def kept(text):
        return "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))

    return kept(old_src) == kept(new_src)


_PAIR_FUNCS = ("_filter_comments", "_extract_meta", "load_sql_fixture_pairs")


def _pinned_pairs(helpers_src: str, text: str):
    """The (meta, sql, expected) pairs the pinned tests/helpers.py loader
    yields for text: its own three functions, run on the text."""
    import ast
    import io

    tree = ast.parse(helpers_src)
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in _PAIR_FUNCS]
    if sorted(d.name for d in defs) != sorted(_PAIR_FUNCS):
        raise ValueError("loader not found")
    ns = {"os": os, "FIXTURES_DIR": "", "open": lambda *a, **k: io.StringIO(text)}
    exec(compile(ast.Module(body=defs, type_ignores=[]), "tests/helpers.py", "exec"), ns)  # noqa: S102
    return list(ns["load_sql_fixture_pairs"]("x"))


def _fixture_pairs_narrowed(fd: FileDiff, old_src, new_src, m: Map, sel: Selection) -> bool:
    """A .sql fixture edit that keeps the file's pair count, while the test
    code is as at the pinned commit. data/pairs.json (tools/pair_map.py)
    says, per test that opened the file: "none" (it never read a pair, only
    its class setup loaded them) or "tasks" (one pool task per pair, task k
    for pair k). Such a test can see only the changed pairs, through their
    tasks. Tests not in the map, and every other reader, are kept whole."""
    path = fd.path
    kinds = getattr(m, "pair_kinds", {}).get(path)
    if not kinds or not (path.startswith("tests/fixtures/") and path.endswith(".sql")) or fd.binary \
            or fd.old_path != fd.new_path or old_src is None or new_src is None or fd.old_mode != fd.new_mode:
        return False
    if any(f.endswith(".py") and not f.startswith("sqlglot/") for f in m.new_sources):
        return False  # the map was made with the pinned test code
    helpers = m.sources.get("tests/helpers.py")
    if not helpers:
        return False
    try:
        old_p, new_p = _pinned_pairs(helpers, old_src), _pinned_pairs(helpers, new_src)
    except Exception:  # noqa: BLE001  (a class setup that loads it would fail too)
        return False
    if len(old_p) != len(new_p):
        return False
    changed = {i for i, (a, b) in enumerate(zip(old_p, new_p)) if a != b}
    out: set[int] = set()
    for t in m.opens.get(path, ()):
        parent = (t - 10_000_000) // 1000 if t >= 10_000_000 else t
        kind = kinds.get(parent)
        if kind == "none":
            continue
        if kind == "tasks":
            if t >= 10_000_000:
                if t % 1000 in changed:
                    out.add(t)
            else:
                out |= {10_000_000 + t * 1000 + i for i in changed}
            continue
        out.add(t)
    sel.tests |= out
    sel.pair_files.add(path)
    sel.notes.append(f"fixture pairs {sorted(changed)[:10]} of {len(old_p)} changed in {path}: {len(out)} candidates")
    return True


def _bounded_by_imports(fd: FileDiff, m: Map) -> bool:
    """A changed library module whose only way to matter is being imported:
    an existing .py file under sqlglot/ (not a package __init__, which its
    submodules import implicitly, nor a new or renamed file)."""
    return (fd.old_path is not None and fd.old_path == fd.new_path and fd.path.endswith(".py")
            and fd.path.startswith("sqlglot/") and not fd.path.endswith("__init__.py")
            and fd.path in m.sources and not fd.binary)


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

    if _fixture_pairs_narrowed(fd, old_src, new_src, m, sel):
        return

    if _fixture_lines_dropped(fd, old_src, new_src, m):
        # the test helpers read .sql fixtures through _filter_comments, which
        # drops blank lines and lines starting with "--": nothing they return
        # changed
        sel.notes.append(f"fixture comments/blank lines only {path}")
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
        sel.tests.update(_parent_lists(path, m))
        sel.tests.update(m.popen)
        sel.tests.update(m.package_walkers)
        stem = os.path.basename(path)[:-3]
        named = re.compile(rf"\b{re.escape(stem)}\b")
        others = [t for f, t in list(m.sources.items()) + list((m.patched or {}).items())
                  if f != path and t is not None and f.endswith(".py")]
        if stem != "__init__" and os.path.basename(os.path.dirname(path)) != stem \
                and not any(named.search(t) for t in others):
            # named by no code: only what walks the package (tests that list
            # its directory) or runs Python in a subprocess can load it
            sel.notes.append(f"new module {path} named nowhere")
            return
        sel.tests.update(m.users.get(pkg_init, ()))
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
        hit = None if path.endswith("__init__.py") or not path.startswith("sqlglot/") else m.import_breakers(path)
        if hit is None:
            sel.everything = True
            sel.notes.append(f"module removed/moved {path}")
        else:
            sel.tests |= hit | m.file_users(path)
            sel.notes.append(f"module removed/moved {path}: {len(hit)} tests import it")
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
        # only comments / formatting changed: the bytecode is identical and
        # only line numbers move. Tests see line numbers only through code
        # objects or the source text, and the tests that read this file are
        # already selected above. An encoding cookie on line 1-2 changes how
        # the whole file decodes, so that counts as code.
        cookie = re.compile(r"^[ \t\f]*#.*?coding[:=]")
        heads = (old_src.split("\n")[:2], new_src.split("\n")[:2])
        if any(cookie.match(l) for h in heads for l in h) and heads[0] != heads[1]:
            sel.tests |= m.file_users(path)
            sel.notes.append(f"encoding line changed {path}")
            return
        sel.tests |= m.code_readers & m.file_users(path)
        sel.notes.append(f"comment/format only {path}")
        return

    try:
        new_tree = ast.parse(new_src)
    except SyntaxError:  # handled above, kept for safety
        sel.everything = True
        return
    if path.endswith(".py"):
        # a refactor proven from the two ASTs (renamed locals or private
        # names, reordered defs or imports): as for a comment-only edit
        sources = dict(m.sources)
        for f, text in (m.patched or {}).items():
            if f != path and text is not None:
                sources[f + "\0new"] = text
        lambdas = refactor.lambda_edits(tree, new_tree)
        if lambdas:
            # a line that also runs while importing (where the lambda is
            # created) is credited to no test: the edit must be on lambda
            # lines only calls run
            touched = fd.changed_old_lines()
            at_import = m.imports.get(path, set())
            for lam in lambdas:
                span = set(range(lam.lineno, (lam.end_lineno or lam.lineno) + 1))
                inside = touched & span
                if not inside or inside & at_import or lam.lineno in inside:
                    lambdas = None
                    break
        if lambdas:
            # only the bodies of lambdas defined at module or class level
            # changed: they run when called, and every call runs their lines
            for lam in lambdas:
                sel.tests |= m.line_tests(path, range(lam.lineno + 1, (lam.end_lineno or lam.lineno) + 1))
            sel.tests |= m.code_readers & m.file_users(path)
            sel.notes.append(f"lambda bodies {path}: {[l.lineno for l in lambdas]}")
            return
        defaults = refactor.default_edits(old_src, new_src)
        if defaults:
            # only literal parameter defaults changed: they matter to calls
            # that leave the argument out, and every call runs the body
            # (unless the body also runs while importing, which no test is
            # credited with, or shares a line with the signature)
            at_import = m.imports.get(path, set())
            spans = []
            for fn in defaults:
                first, last = fn.body[0].lineno, fn.end_lineno or fn.body[0].lineno
                sig_end = max([fn.lineno] + [getattr(n, "end_lineno", fn.lineno) or fn.lineno
                                             for n in [*fn.args.defaults, *fn.args.kw_defaults] if n is not None])
                if first <= sig_end or any(l in at_import for l in range(first, last + 1)):
                    spans = None
                    break
                spans.append((first, last))
            if spans:
                for first, last in spans:
                    sel.tests |= m.line_tests(path, range(first, last + 1))
                sel.tests |= m.code_readers & m.file_users(path)
                sel.tests |= m.popen
                sel.notes.append(f"parameter defaults {path}: {[fn.name for fn in defaults]}")
                return
        same = refactor.equivalent(tree, new_tree, path, sources)
        if same == "probe" and path.startswith("sqlglot/") and path in m.import_graph().eager \
                and path not in m.import_ok:
            # sqlglot imports reordered in a module `import sqlglot` loads:
            # same if the patched import leaves the same state
            sel.everything = True
            sel.import_probe.setdefault(path, set())
            sel.notes.append(f"imports reordered {path}")
            return
        if same == "same" or (same == "probe" and path in m.import_ok):
            sel.tests |= m.code_readers & m.file_users(path)
            sel.notes.append(f"refactor only {path}")
            return
    edit = None
    if m.cmp is not None and path.startswith("sqlglot/"):
        try:
            edit = cmpcheck.find_edit(old_src, new_src, tree, new_tree)
        except Exception:  # noqa: BLE001
            edit = None
    bool_edit = None
    if edit is None and m.bool is not None and path.startswith("sqlglot/"):
        try:
            bool_edit = cmpcheck.find_bool_edit(old_src, new_src, tree, new_tree)
        except Exception:  # noqa: BLE001
            bool_edit = None
    flag_edit = None
    if edit is None and bool_edit is None and m.req is not None and path.startswith("sqlglot/"):
        try:
            flag_edit = cmpcheck.find_flag_edit(old_src, new_src, tree, new_tree)
        except Exception:  # noqa: BLE001
            flag_edit = None
    res = Resolver(m, sel)
    if m.keys is not None and path.startswith("sqlglot/"):
        try:
            res.dict_edit = cmpcheck.find_dict_entry_edit(old_src, new_src, tree, new_tree)
        except Exception:  # noqa: BLE001
            res.dict_edit = None
    ctx = res.ctx(path, old_src)
    try:
        new_tree._blast_lines = new_src.split("\n")
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
        g = m.import_graph()
        if g.bad:
            sel.everything = True
            return
        if (path in g.eager or path in m.old_import_graph().eager) and path not in m.import_ok:
            # loaded by `import sqlglot`: the change may break or alter that
            # import, which every test goes through, unless the import-state
            # probe finds the patched import leaves the same state
            sel.everything = True
            if path.startswith("sqlglot/"):
                sel.import_probe.setdefault(path, set())
            sel.notes.append(f"{path} is imported by sqlglot itself: every test")
            return
        if _bounded_by_imports(fd, m):
            hit = m.import_breakers(path)
            if hit is not None and len(hit) < len(sel.tests):
                sel.tests = hit | set(m.opens.get(path, ()))
                sel.notes.append(f"importers of {path} instead: {len(hit)} tests")
    if edit is not None:
        _narrow_compare(fd, edit, m, sel)
    elif bool_edit is not None:
        _narrow_bool(fd, bool_edit, m, sel)
    elif flag_edit is not None:
        _narrow_flag(fd, flag_edit, m, sel)
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
    sel.tests = m.drop_tasks_of(base - safe, m.cmp.mapped, base - safe) | set(m.opens.get(fd.path, ()))
    sel.notes.append(f"comparison edit {fd.path}:{edit.site[0]} {edit.op_old}->{edit.op_new}: "
                     f"{len(base)} -> {len(sel.tests)}")

def _narrow_flag(fd: FileDiff, edit, m: Map, sel: Selection) -> None:
    """The file's only change flips one arg_types required flag: keep the
    tests that validated an instance of that class (or a subclass) without
    that argument, plus unmapped tests and tests that read the file."""
    qual, arg, was, now = edit
    cname = fd.path[:-3].replace("/", ".").removesuffix(".__init__") + ":" + qual
    if was == now:
        r = (set(), set(m.req.mapped))  # same required_args
    else:
        r = m.req.narrow(cname, arg)
    if r is None:
        sel.notes.append(f"arg_types flag {qual}.{arg} validated while importing")
        return
    hit, safe = r
    if sel.everything:
        base = {t["number"] for t in m.tests} | m.all_subtasks()
        sel.everything = False
    else:
        base = set(sel.tests)
    sel.tests = m.drop_tasks_of(base - safe, m.req.mapped, base - safe) | set(m.opens.get(fd.path, ()))
    sel.notes.append(f"arg_types flag {qual}.{arg} {was}->{now}: {len(base)} -> {len(sel.tests)}")


def _narrow_bool(fd: FileDiff, edit, m: Map, sel: Selection) -> None:
    """As _narrow_compare, for a file whose only change rewrites one and/or
    over the same operands: keep the tests whose recorded operand truth
    values give a different result, or skip an operand with side effects."""
    r = cmpcheck.narrow_bool(edit, fd.path, m.bool)
    if r is None:
        sel.notes.append(f"and/or edit {fd.path}:{edit.site[0]} not judged")
        return
    hit, safe = r
    if sel.everything:
        base = {t["number"] for t in m.tests} | m.all_subtasks()
        sel.everything = False
    else:
        base = set(sel.tests)
    sel.tests = m.drop_tasks_of(base - safe, m.bool.mapped, base - safe) | set(m.opens.get(fd.path, ()))
    sel.notes.append(f"and/or edit {fd.path}:{edit.site[0]}: {len(base)} -> {len(sel.tests)}")


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
    new_lines = getattr(new_tree, "_blast_lines", None)
    in_string: set[int] = set()
    if new_lines is not None:
        for n in ast.walk(new_tree):
            if isinstance(n, (ast.Constant, ast.JoinedStr)) and n.end_lineno and n.end_lineno > n.lineno:
                in_string.update(range(n.lineno, n.end_lineno + 1))
    for line in added:
        if new_lines is not None and 0 < line <= len(new_lines) and line not in in_string:
            text = new_lines[line - 1].strip()
            if not text or text.startswith("#"):
                continue  # a blank or comment line outside any string: no code
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
            new_node = new_st.innermost(line)
            if node is not None and new_node is not None and not hasattr(new_node, "body"):
                # a simple statement replacing simple statements removed right
                # after the anchor: it runs where they ran, and their removed
                # lines are resolved already
                run, r = [], (node.end_lineno or node.lineno) + 1
                while r in removed or (not run and 0 < r <= len(ctx.lines) and r < line + 50
                                       and _blank_or_comment(ctx.lines[r - 1])):
                    if r in removed:
                        run.append(r)
                    r += 1
                if run and all((x := ctx.st.innermost(q)) is not None and not hasattr(x, "body") for q in run):
                    continue
            if ctx.scopes.cls(old_line) is not None and ctx.scopes.func(old_line) is None:
                new_node = new_st.innermost(line)
                t = _targets(new_node) if new_node is not None else None
                qual = ctx.scopes.cls(old_line)[2]
                if t and t <= removed_bound.get(qual, set()):
                    continue  # same attributes re-bound: a value change, already resolved
                res.class_insert(path, ctx, old_line, _class_body_stmt(new_tree, line))
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


def _blank_or_comment(text: str) -> bool:
    t = text.strip()
    return not t or t.startswith("#")


def _class_body_stmt(tree: ast.AST, line: int):
    """The statement directly in a class body (innermost class) that holds
    `line`, or None."""
    best = None
    stack = list(tree.body)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.ClassDef) and n.lineno <= line <= n.end_lineno:
            for st in n.body:
                start = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])
                if start <= line <= st.end_lineno:
                    best = st
                    if isinstance(st, ast.ClassDef):
                        stack.append(st)
        elif isinstance(n, (ast.If, ast.Try)) and n.lineno <= line <= n.end_lineno:
            stack.extend(n.body)
    return best


_HARMLESS_DECORATORS = {"staticmethod", "classmethod", "unsupported_args"}


def _plain_method(node) -> bool:
    """A def whose decorators (run while the class is created) only wrap it."""
    for d in node.decorator_list:
        f = d.func if isinstance(d, ast.Call) else d
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name not in _HARMLESS_DECORATORS:
            return False
    return True


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


def _modname(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def _abs_module(path: str, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    pkg = _modname(path) if path.endswith("__init__.py") else _modname(path).rpartition(".")[0]
    parts = pkg.split(".")
    if node.level - 1 > len(parts):
        return None
    base = ".".join(parts[: len(parts) - (node.level - 1)])
    return f"{base}.{node.module}" if node.module else base


_STAR_IMPORT = re.compile(r"^\s*from\s+\S+\s+import\s+\*", re.M)
_LAZY_PACKAGE = re.compile(r"^def __getattr__\(|import_module\(", re.M)

# ways to reach a module object without naming it
_DYNAMIC_MODULE = re.compile(r"import_module|__import__|sys\.modules|globals\(\)|vars\(|pkgutil|importlib")


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
        self.import_lines: dict[str, list[int]] = {}  # local alias -> lines of its `from` imports
        self.import_src: dict[str, set] = {}  # local alias -> modules it is imported from (None: unknown)
        self.star = False
        self.star_src: set = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                src_mod = _abs_module(path, node)
                for a in node.names:
                    if a.name == "*":
                        self.star = True
                        self.star_src.add(src_mod)
                    else:
                        self.imports_from[a.asname or a.name] = a.name
                        self.import_lines.setdefault(a.asname or a.name, []).append(node.lineno)
                        self.import_src.setdefault(a.asname or a.name, set()).add(src_mod)

    def foreign_attr_lines(self, name: str, owners: set[str]) -> set[int]:
        """Lines whose every `X.name` read is on a name X bound by importing
        a module that is neither one of `owners` nor a package above one."""
        if not hasattr(self, "_mod_alias"):
            self._mod_alias: dict[str, set] = {}
            for n in ast.walk(self.tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        self._mod_alias.setdefault(a.asname or a.name.split(".")[0], set()).add(
                            a.name if a.asname else a.name.split(".")[0])
                elif isinstance(n, ast.ImportFrom):
                    mod = _abs_module(self.path, n)
                    for a in n.names:
                        self._mod_alias.setdefault(a.asname or a.name, set()).add(f"{mod}.{a.name}" if mod else None)
                elif isinstance(n, (ast.Name, ast.arg)) and isinstance(getattr(n, "ctx", ast.Store()), ast.Store):
                    # rebound elsewhere: can't tell what it holds
                    self._mod_alias.setdefault(n.id if isinstance(n, ast.Name) else n.arg, set()).add(None)
        def related(mods):
            return any(x is None or any(o == x or o.startswith(x + ".") for o in owners) for x in mods)
        ok: dict[int, bool] = {}
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Attribute) and n.attr == name:
                v = n.value
                foreign = isinstance(v, ast.Name) and v.id in self._mod_alias and not related(self._mod_alias[v.id])
                ok[n.lineno] = ok.get(n.lineno, True) and foreign
        return {l for l, f in ok.items() if f}

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
MAX_SECONDS = float(os.environ.get("BLAST_RESOLVE_SECONDS", "1.0"))


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
        self._cache: dict = {}
        self.touched: set[str] = set()  # module/class-level names followed so far
        self._lines = 0
        import time

        self._deadline = time.process_time() + MAX_SECONDS

    def import_break(self, f: str, why: str, fctx=None, aliases=()) -> bool:
        """Importing f now raises. Select whatever that breaks; False (and
        every test) when that can't be bounded."""
        if fctx is not None:
            lines = [l for al in aliases for l in fctx.import_lines.get(al, ())]
            if lines and all(fctx.scopes.func(l) is not None for l in lines):
                # only imported inside functions: fails when those lines run
                hit = self.m.line_tests(f, lines) if f.startswith("sqlglot/") else set(self.m.by_file.get(f, ()))
                self.sel.tests |= hit
                self.sel.notes.append(f"{why} (in functions): {len(hit)} tests")
                return True
        hit = self.m.import_breakers(f)
        if hit is None:
            self.sel.everything = True
            self.sel.notes.append(why)
            return False
        self.sel.tests |= hit
        self.sel.notes.append(f"{why}: {len(hit)} tests import it")
        return True

    def exporters(self, path: str, name: str) -> set[str]:
        """Modules through which `name` of module `path` can be reached: the
        module itself, those that star-import it or import the name from one
        of these (re-exports), and packages above them that hand out
        submodule attributes from a module-level __getattr__."""
        key = ("exporters", path, name)
        if key in self._cache:
            return self._cache[key]
        m = self.m
        word = re.compile(rf"\b{re.escape(name)}\b")
        files = [f for f, t in m.sources.items() if f.endswith(".py") and (_STAR_IMPORT.search(t) or word.search(t))]
        lazy = [f for f, t in m.sources.items() if f.endswith("__init__.py") and _LAZY_PACKAGE.search(t)]
        out = {_modname(path)}
        grew = True
        while grew:
            grew = False
            for f in lazy:
                pkg = _modname(f)
                if pkg not in out and any(o.startswith(pkg + ".") for o in out):
                    out.add(pkg)
                    grew = True
            for f in files:
                mod = _modname(f)
                if mod in out:
                    continue
                star_src, src, _ = m.import_table(f)
                if star_src & (out | {None}) or src.get(name, set()) & (out | {None}):
                    out.add(mod)
                    grew = True
        self._cache[key] = out
        return out

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
            # contexts of pinned sources are kept across commits (read-only)
            shared = self.m._ctx_cache if text == self.m.sources.get(path) else {}
            if path not in shared:
                try:
                    shared[path] = Ctx(path, text)
                except SyntaxError:
                    return None
            self._ctx[path] = shared[path]
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
        if span is not None and span[2] != line and line in m.imports.get(path, ()) and path not in m.import_ok:
            # this function ran while modules were being imported: what it
            # computed (or raised) there is shared by every test, and the map
            # credits import-time work to no test. The caller may check that
            # importing the patched tree leaves the same state (import_ok).
            sel.everything = True
            sel.import_probe.setdefault(path, set()).add(line)
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
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and _plain_method(stmt):
            # a method's header changed or it went away: whatever could see
            # the old method (its new version, if any, comes as an added line)
            hit = self.method_users(path, qual, stmt.name)
            if hit is not None:
                hit |= self.m.line_tests(path, range(stmt.lineno, stmt.end_lineno + 1))
                self.sel.tests |= hit
                self.sel.notes.append(f"method {qual}.{stmt.name} changed or removed: {len(hit)} tests")
                return
        names = _targets(stmt)
        if names is None:
            return self.class_fallback(path, qual, f"class-level {type(stmt).__name__}")
        self.class_attrs(path, ctx, cnode, qual, names, {id(stmt)}, depth)

    def class_insert(self, path, ctx: Ctx, old_line, new_node=None) -> None:
        """A statement was added to a class body: it may define a new
        attribute that shadows a base class one. Readers can't be told apart,
        so use every user of the class, unless it is a method added to a
        generator class (see method_users)."""
        c = ctx.scopes.cls(old_line)
        if isinstance(new_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _plain_method(new_node):
            hit = self.method_users(path, c[2], new_node.name)
            if hit is not None:
                self.sel.tests |= hit
                self.sel.notes.append(f"method {c[2]}.{new_node.name} added: {len(hit)} tests")
                return
        if new_node is not None and refactor.inert_member(new_node) and self._inert_class(path, c[2]):
            name = refactor.member_name(new_node)
            new_text = (self.m.patched or {}).get(path)
            sources = dict(self.m.sources)
            for f, text in (self.m.patched or {}).items():
                if f != path and text is not None:
                    sources[f + "\0new"] = text
            if name and new_text and refactor.invisible_name(name, path, new_text, sources):
                # a new member nothing names: no code can read it
                self.sel.tests |= self.m.code_readers & self.m.file_users(path)
                self.sel.notes.append(f"new member {c[2]}.{name} named nowhere else")
                return
        self.class_fallback(path, c[2], "statement added to class body")

    def _inert_class(self, path: str, qual: str) -> bool:
        """No creation hook of the class walks its namespace generically."""
        generic = re.compile(r"__dict__|\bdir\(|\bvars\(|getmembers|inspect\.|\.items\(\)")
        key = f"{path}::{qual}"
        if key not in self.m.hooks and path not in self.m.import_graph().eager:
            return False  # hooks are recorded for classes the mapper had loaded only
        for f, s0, e0 in self.m.hooks.get(key, []):
            hctx = self.ctx(f)
            if hctx is None or generic.search("\n".join(hctx.lines[s0 - 1:e0])):
                return False
        return True

    def method_users(self, path: str, qual: str, name: str) -> set[int] | None:
        """Tests that can see a method `name` appear in, or vanish from,
        generator class `qual`: those whose generators (that class or a
        subclass) looked up the expression a `<key>_sql` name dispatches,
        plus every place that names the method directly. None when the class
        is no generator the dispatch map knows."""
        d = self.m.disp
        if d is None:
            return None
        cls = f"{path[:-3].replace('/', '.').removesuffix('.__init__')}:{qual}"
        if cls not in d["mro"]:
            return None
        out = set(self.m.popen) | ({t["number"] for t in self.m.tests} - d["mapped"])
        if name.endswith("_sql") and not name.startswith("_"):
            key = name[:-4]
            for g, bases in d["mro"].items():
                if cls in bases:
                    out.update(d["by"].get(g, {}).get(key, ()))
            # handlers looked up by a name built at run time, where an
            # isinstance guard around the lookup doesn't rule this key out
            for f, line, keys in self.m.dyn_handler_sites():
                if keys is None or key in keys:
                    out |= self.m.line_tests(f, [line])
        # direct uses: attribute access, getattr strings, super() calls
        ref = re.compile(rf"\.{re.escape(name)}\b|[\"']{re.escape(name)}[\"']")
        for f, text in self.m.sources.items():
            if name not in text:
                continue
            lines = [i for i, l in enumerate(text.split("\n"), 1) if ref.search(l)]
            if not lines:
                continue
            if f.startswith("sqlglot/"):
                out |= self.m.line_tests(f, lines)
            else:
                out |= set(self.m.by_file.get(f, ()))
        out |= set(self.m.opens.get(path, ()))
        return out

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
                    narrowed = m.drop_tasks_of(narrowed, m.keys.mapped, narrowed)
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
                if path.startswith("sqlglot/") and not any(
                        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
                        for n in ast.walk(stmt)):
                    # an if/try/for at module level that defines no code: all
                    # it can do is set module-level values (and import), which
                    # the import-state probe compares. If the patched import
                    # leaves them as they were, the statement did the same.
                    if path in self.m.import_ok:
                        return
                    self.sel.everything = True
                    self.sel.import_probe.setdefault(path, set()).add(stmt.lineno)
                    self.sel.notes.append(f"module-level {type(stmt).__name__} {path}:{stmt.lineno} run at import")
                    return
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
                if self.import_break(path, f"{path} uses removed name {name} at import"):
                    continue
                raise OverBudget()
            # inside the file: every statement that reads the name
            for l in ctx.name_refs(name):
                node = ctx.top_stmt(l)
                if node is not None and id(node) in skip:
                    continue
                self.line(path, ctx, l, depth + 1)
            # other files: imports of the name, star-imports, attribute and
            # string access, from this module or one that re-exports the name
            word = re.compile(rf"\b{re.escape(name)}\b")
            attr = re.compile(rf"\.{re.escape(name)}\b|[\"']{re.escape(name)}[\"']")
            dot = re.compile(rf"\.{re.escape(name)}\b")
            owners = self.exporters(path, name)
            for f, text in m.sources.items():
                if f == path or not word.search(text):
                    continue
                reach = m.names_module(f, owners)
                if f.endswith(".py"):
                    # cheap checks first: most files that use the word don't
                    # import it from here and can't name this module
                    star_src, src, _ = m.import_table(f)
                    imported = any(src.get(al, set()) & (owners | {None}) for al in src)
                    if not (imported or star_src & (owners | {None}) or reach):
                        continue
                fctx = self.ctx(f)
                if fctx is None:
                    continue
                aliases = {al for al, orig in fctx.imports_from.items() if orig == name
                           and fctx.import_src.get(al, {None}) & (owners | {None})}
                star = fctx.star and bool(fctx.star_src & (owners | {None}))
                if name in self.removed_names and aliases:
                    # `from x import name` of a name that is gone fails at import
                    if self.import_break(f, f"{f} imports removed name {name}", fctx, aliases):
                        continue
                    raise OverBudget()
                if star:
                    aliases.add(name)
                for al in aliases:
                    for l in fctx.name_refs(al):
                        self.line(f, fctx, l, depth + 1)
                if not reach:
                    continue
                broke = False
                foreign = fctx.foreign_attr_lines(name, owners)
                for i, l in enumerate(fctx.lines, 1):
                    if not attr.search(l):
                        continue
                    if i in foreign and not re.search(rf"[\"']{re.escape(name)}[\"']", l):
                        continue  # e.g. t.Iterator for typing's Iterator
                    if name in self.removed_names and fctx.scopes.func(i) is None and (dot.search(l) or "getattr" in l):
                        # read while f is imported (module level, class body):
                        # importing f now fails
                        if not self.import_break(f, f"{f} reads removed name {name} at import", fctx):
                            raise OverBudget()
                        broke = True
                        break
                    self.line(f, fctx, i, depth + 1)
                if broke:
                    continue
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
