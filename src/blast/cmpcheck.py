"""Edits to a single comparison, checked against what tests compared.

The mapper's MAP_CMP pass recorded, per test and per comparison site, each
distinct (left operand, right operand, left is right, result) it evaluated.
Operands are literal values (None, bool, int, float, short str/bytes, tuples
of those) or, otherwise, a type name plus whether that type's == is plain
identity. When a patch changes nothing but one comparison (its operator, or a
literal operand), a test whose every recorded evaluation gives the same result
under the new comparison runs exactly as before; so does a test that never
evaluated it. Anything not provably the same counts as changed.
"""

from __future__ import annotations

import ast
import gzip
import json
import operator as op
import os

_OPS = {"Eq": op.eq, "NotEq": op.ne, "Lt": op.lt, "LtE": op.le, "Gt": op.gt, "GtE": op.ge,
        "Is": op.is_, "IsNot": op.is_not, "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b}
_SINGLETONS = (None, True, False, Ellipsis)
UNKNOWN = object()


class Edit:
    def __init__(self, old: ast.Compare, new: ast.Compare):
        self.old, self.new = old, new
        self.op_old = type(old.ops[0]).__name__
        self.op_new = type(new.ops[0]).__name__
        # a side whose source changed must be a literal on both versions
        self.left = self._side(old.left, new.left)
        self.right = self._side(old.comparators[0], new.comparators[0])

    @staticmethod
    def _side(a, b):
        if ast.dump(a) == ast.dump(b):
            return None  # unchanged: use what was observed
        return ("lit", ast.literal_eval(b))  # raises if not a literal

    @property
    def site(self) -> tuple[int, int]:
        return self.old.lineno, self.old.col_offset


def _diffs(a, b, parents, out, limit=2):
    if len(out) >= limit:
        return
    if type(a) is not type(b):
        out.append((a, b, parents))
        return
    if isinstance(a, ast.AST):
        for f in a._fields:
            va, vb = getattr(a, f, None), getattr(b, f, None)
            if isinstance(va, list) and isinstance(vb, list):
                if len(va) != len(vb):
                    out.append((a, b, parents))
                    return
                for x, y in zip(va, vb):
                    _diffs(x, y, parents + [a], out, limit)
            elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                _diffs(va, vb, parents + [a], out, limit)
            elif va != vb or type(va) is not type(vb):
                out.append((a, b, parents))
                return
    elif a != b:
        out.append((a, b, parents))


def find_edit(old_src: str, new_src: str, old_tree: ast.AST, new_tree: ast.AST) -> Edit | None:
    """The single comparison the patch changes, if that is all it changes and
    no line moved."""
    if old_src.count("\n") != new_src.count("\n"):
        return None
    out: list = []
    _diffs(old_tree, new_tree, [], out)
    if len(out) != 1:
        return None
    a, _, parents = out[0]
    chain = parents + [a]
    old_cmp = next((n for n in reversed(chain) if isinstance(n, ast.Compare)), None)
    if old_cmp is None or len(old_cmp.ops) != 1:
        return None
    # the matching node in the new tree: same position in the same walk
    new_cmp = _counterpart(old_tree, new_tree, old_cmp)
    if not isinstance(new_cmp, ast.Compare) or len(new_cmp.ops) != 1:
        return None
    try:
        e = Edit(old_cmp, new_cmp)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if e.op_new not in _OPS or e.op_old not in _OPS:
        return None
    return e


def _counterpart(old_tree, new_tree, node):
    for a, b in zip(ast.walk(old_tree), ast.walk(new_tree)):
        if a is node:
            return b
    return None


def _value(k):
    """An observed operand back as a value, or UNKNOWN for objects."""
    if k[0] == "v":
        try:
            return ast.literal_eval(k[1])
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return UNKNOWN
    return UNKNOWN


def _ideq(k) -> bool:
    return k[0] == "t" and bool(k[2])


def changes(e: Edit, o) -> bool:
    """Could this observation compare differently after the edit?"""
    lk, rk, same, res = o
    if res is None:  # not a bool result: no claim
        return True
    lv = e.left[1] if e.left else _value(lk)
    rv = e.right[1] if e.right else _value(rk)
    lobj = lv is UNKNOWN
    robj = rv is UNKNOWN
    new = e.op_new
    try:
        if new in ("Is", "IsNot"):
            if not lobj and not robj and (any(lv is s for s in _SINGLETONS) or any(rv is s for s in _SINGLETONS)):
                ident = lv is rv
            elif not e.left and not e.right:
                ident = bool(same)  # same operands as observed
            elif (lobj and not robj) or (robj and not lobj):
                ident = False  # an object vs a freshly written literal
            else:
                return True
            r = ident if new == "Is" else not ident
        elif not lobj and not robj:
            r = _OPS[new](lv, rv)
        elif new in ("Eq", "NotEq"):
            # == between an object whose type has identity equality and a
            # literal or another such object falls back to identity
            objs = [k for k, is_obj in ((lk, lobj), (rk, robj)) if is_obj]
            same_enum = (lobj and robj and not e.left and not e.right and len(lk) > 3 and len(rk) > 3
                         and lk[3] and rk[3] and lk[1] == rk[1])
            if not same_enum and not all(_ideq(k) for k in objs):
                return True
            ident = False if (e.left or e.right) else bool(same)
            r = ident if new == "Eq" else not ident
        elif new in ("In", "NotIn") and lobj and not robj and _ideq(lk) and isinstance(rv, (tuple, frozenset, set, list)) \
                and all(v is None or type(v) in (bool, int, float, str, bytes) for v in rv):
            r = (new == "NotIn")  # an identity-hashed object is in no literal container
        else:
            return True
    except Exception:  # noqa: BLE001
        return True
    return type(r) is not bool or r != res


class CmpMap:
    def __init__(self, root: str):
        self.root = root
        self._files: dict = {}
        try:
            self.mapped = set(json.load(open(os.path.join(root, "tests.json"))))
        except (OSError, ValueError):
            self.mapped = set()

    def file(self, path: str):
        if path not in self._files:
            p = os.path.join(self.root, path.replace("/", "__") + ".json.gz")
            try:
                with gzip.open(p, "rt") as fh:
                    self._files[path] = json.load(fh)
            except (OSError, ValueError):
                self._files[path] = None
        return self._files[path]


def narrow(e: Edit, path: str, cm: CmpMap):
    """(tests whose comparisons may change, mapped tests that are safe), or
    None when the edit can't be judged (import-time evaluations change, no
    data)."""
    data = cm.file(path)
    if data is None or not cm.mapped:
        return None
    site = data.get(f"{path}:{e.site[0]}:{e.site[1]}")
    if site is None:
        return set(), set(cm.mapped)  # evaluated by no test, and not at import
    obs = site["o"]
    imp = site["i"]
    if imp == "many" or (imp and any(changes(e, obs[i]) for i in imp)):
        return None
    flips = [changes(e, o) for o in obs]
    hit = set(site["n"])
    for idx, tests in site["g"]:
        if any(flips[i] for i in idx):
            hit.update(tests)
    return hit, set(cm.mapped) - hit


# ---- dictionary entries ---------------------------------------------------

def find_dict_entry_edit(old_src: str, new_src: str, old_tree: ast.AST, new_tree: ast.AST):
    """(class qualname, attribute, key repr) when the patch changes nothing but
    the value of one entry of a class-level dict literal with a literal key."""
    if old_src.count("\n") != new_src.count("\n"):
        return None
    out: list = []
    _diffs(old_tree, new_tree, [], out)
    if len(out) != 1:
        return None
    a, _, parents = out[0]
    chain = parents + [a]
    for i, node in enumerate(chain):
        if not isinstance(node, ast.Dict) or i + 1 >= len(chain):
            continue
        child = chain[i + 1]
        if not any(child is v for v in node.values):
            return None  # a key changed: membership and iteration change
        j = next(j for j, v in enumerate(node.values) if v is child)
        key = node.keys[j]
        if key is None:  # **spread
            return None
        try:
            krepr = repr(ast.literal_eval(key))
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return None
        # the dict must be the whole value of a plain class-level assignment
        if i < 2 or not isinstance(chain[i - 1], (ast.Assign, ast.AnnAssign)) or not isinstance(chain[i - 2], ast.ClassDef):
            return None
        st = chain[i - 1]
        targets = st.targets if isinstance(st, ast.Assign) else [st.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return None
        qual = ".".join(n.name for n in chain[:i - 1] if isinstance(n, ast.ClassDef))
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in chain[:i - 1]):
            return None
        return qual, targets[0].id, krepr
    return None


class KeyMap(CmpMap):
    pass


def key_readers(km: KeyMap, path: str, qual: str, attr: str, krepr: str, attr_readers: set):
    """Tests that may read the value under krepr, or None when unknown (not
    recorded, or read while modules were imported)."""
    data = km.file(path)
    if data is None or not km.mapped:
        return None
    e = data.get(f"{path}::{qual}::{attr}")
    if e is None:
        return None
    imp = e["i"]
    if imp == "many" or krepr in imp or "*" in imp:
        return None
    return set(e["k"].get(krepr, ())) | set(e["a"]) | set(e["n"]) | (attr_readers - km.mapped)


# ---- exception handlers ---------------------------------------------------

def find_handler_edit(old_src: str, new_src: str, old_tree: ast.AST, new_tree: ast.AST):
    """(first line, last line) of the `except ...:` header when the patch
    changes nothing but that handler's exception types. Matching evaluates a
    handler's types only when an exception reaches it, so only tests that ran
    the header line can see the change."""
    if old_src.count("\n") != new_src.count("\n"):
        return None
    out: list = []
    _diffs(old_tree, new_tree, [], out)
    if len(out) != 1:
        return None
    a, _, parents = out[0]
    chain = parents + [a]
    for i, node in enumerate(chain):
        if isinstance(node, ast.ExceptHandler) and i + 1 < len(chain):
            if node.type is None or chain[i + 1] is not node.type:
                return None
            return node.lineno, node.type.end_lineno
    return None


# ---- and/or -----------------------------------------------------------------
# The MAP_BOOL pass recorded, per test and per `and`/`or` used only for its
# truth, each distinct vector of operand truths (1, 0, or None = skipped by
# short-circuiting). An edit that only regroups the same operands with
# and/or (a swapped operator) leaves a test as it was when, on every vector it
# saw, the new expression has the same truth, needs no operand the old one
# skipped, and skips no operand the old one evaluated unless that operand is
# free of side effects (`self._match(...)` consumes tokens).

class BoolEdit:
    def __init__(self, old: ast.BoolOp, formula, pure: list[bool]):
        self.old, self.formula, self.pure = old, formula, pure

    @property
    def site(self) -> tuple[int, int]:
        return self.old.lineno, self.old.col_offset


_PURE_CALLS = {"isinstance", "issubclass", "len", "bool", "type", "hasattr", "callable"}
_PURE_METHODS = {"get", "startswith", "endswith", "isdigit", "isupper", "islower", "isidentifier"}


def _pure(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom, ast.Lambda)):
            return False
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in _PURE_CALLS:
                continue
            if isinstance(f, ast.Attribute) and f.attr in _PURE_METHODS:
                continue
            return False
    return True


def _truth_context(chain: list) -> bool:
    """chain[-1] is a node whose truth alone is used."""
    node = chain[-1]
    for parent in reversed(chain[:-1]):
        if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and parent.test is node:
            return True
        if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
            return True
        if isinstance(parent, ast.comprehension) and any(x is node for x in parent.ifs):
            return True
        if isinstance(parent, ast.BoolOp) and any(x is node for x in parent.values):
            node = parent  # truth used if the enclosing and/or's truth is
            continue
        return False
    return False


def _chain_to(tree, target):
    stack = [(tree, [tree])]
    while stack:
        n, path = stack.pop()
        if n is target:
            return path
        for c in ast.iter_child_nodes(n):
            stack.append((c, path + [c]))
    return None


def find_bool_edit(old_src: str, new_src: str, old_tree: ast.AST, new_tree: ast.AST) -> BoolEdit | None:
    if old_src.count("\n") != new_src.count("\n"):
        return None
    out: list = []
    _diffs(old_tree, new_tree, [], out, limit=1)
    if not out:
        return None  # the masked comparison below rules out other changes
    a, _, parents = out[0]
    chain = parents + [a]
    x = next((n for n in reversed(chain) if isinstance(n, ast.BoolOp)), None)
    if x is None:
        return None
    # the outermost and/or of the new tree starting where x starts
    y = None
    for n in ast.walk(new_tree):
        if isinstance(n, ast.BoolOp) and (n.lineno, n.col_offset) == (x.lineno, x.col_offset):
            y = n
            break
    if y is None or _masked(old_tree, x) != _masked(new_tree, y):
        return None  # something else changed too
    path = _chain_to(old_tree, x)
    if path is None or not _truth_context(path):
        return None
    dumps = [ast.dump(v) for v in x.values]
    pos = [0]

    def formula(node):
        i = pos[0]
        if i < len(dumps) and ast.dump(node) == dumps[i]:
            pos[0] += 1
            return i
        if isinstance(node, ast.BoolOp):
            return (type(node.op).__name__, [formula(v) for v in node.values])
        raise ValueError

    try:
        f = formula(y)
    except (ValueError, RecursionError):
        return None
    if pos[0] != len(dumps):
        return None
    return BoolEdit(x, f, [_pure(v) for v in x.values])


def _masked(tree, target) -> str:
    # mask the node in place, dump, restore
    holder = _chain_to(tree, target)
    if holder is None or len(holder) < 2:
        return ""
    parent = holder[-2]
    for field, value in ast.iter_fields(parent):
        if value is target:
            setattr(parent, field, ast.Constant("__blast_edit__"))
            try:
                return ast.dump(tree)
            finally:
                setattr(parent, field, target)
        if isinstance(value, list):
            for i, v in enumerate(value):
                if v is target:
                    value[i] = ast.Constant("__blast_edit__")
                    try:
                        return ast.dump(tree)
                    finally:
                        value[i] = target
    return ""


def _eval3(f, vec, used: set):
    """Short-circuit truth of formula f over vec (None: unknown), noting
    which operands it evaluates. Returns True/False/None."""
    if isinstance(f, int):
        used.add(f)
        return None if vec[f] is None else bool(vec[f])
    op, parts = f
    for p in parts:
        v = _eval3(p, vec, used)
        if v is None:
            return None
        if (op == "And" and not v) or (op == "Or" and v):
            return v
    return op == "And"


def bool_changes(e: BoolEdit, vec) -> bool:
    if vec is None or len(vec) != len(e.pure):
        return True
    old_op = type(e.old.op).__name__
    old_used: set = set()
    old = _eval3((old_op, list(range(len(vec)))), vec, old_used)
    used: set = set()
    new = _eval3(e.formula, vec, used)
    if old is None or new is None or new != old:
        return True
    return any(not e.pure[i] for i in old_used - used)


def narrow_bool(e: BoolEdit, path: str, bm: CmpMap):
    """As narrow(), for an and/or edit."""
    data = bm.file(path)
    if data is None or not bm.mapped:
        return None
    site = data.get(f"{path}:{e.site[0]}:{e.site[1]}")
    if site is None:
        return set(), set(bm.mapped)
    obs = site["o"]
    imp = site["i"]
    if imp == "many" or (imp and any(bool_changes(e, obs[i]) for i in imp)):
        return None
    flips = [bool_changes(e, o) for o in obs]
    hit = set(site["n"])
    for idx, tests in site["g"]:
        if any(flips[i] for i in idx):
            hit.update(tests)
    return hit, set(bm.mapped) - hit
