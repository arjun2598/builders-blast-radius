"""Stage cuts for pool tasks.

Some slow tests hand each query to a pool task that runs it in stages: the
query is optimized first, and the much slower plan and execution stages read
only the optimized expression. The map (tools/stage_map.py) records, for each
task at the pinned commit, which lines ran outside the optimize call (parse,
execution, result checks) and a digest of the expression the optimize call
returned.

When every function a change touches ran only inside the optimize call for a
task, the task's outcome hinges on that expression alone. The runner then
runs the task only until the call returns. An equal digest means the rest
runs as at the pinned commit, where it passed, so the task is skipped. Any
other digest lets the task run in full.
"""

from __future__ import annotations

import ast
import bisect
import hashlib


def digest(e) -> str:
    """Everything later stages can read from an expression tree: node
    classes, args, types, comments and meta."""
    h = hashlib.blake2b(digest_size=16)
    up = h.update
    stack = [e]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            up(b"[%d" % len(x))
            stack.extend(reversed(x))
            continue
        args = getattr(x, "args", None)
        if isinstance(args, dict) and hasattr(x, "_type"):
            up(b"(" + type(x).__name__.encode())
            if x.comments:
                up(repr(x.comments).encode())
            if x._meta:
                up(repr(sorted(x._meta.items(), key=lambda kv: kv[0])).encode())
            if x._type is not None:
                up(b"T")
                stack.append(x._type)
            for k in sorted(args):
                v = args[k]
                if v is None or v is False or (isinstance(v, list) and not v):
                    continue
                up(b"," + k.encode() + b"=")
                stack.append(v)
            continue
        up(b"|" + type(x).__name__.encode() + b":" + repr(x).encode())
    return h.hexdigest()


def changed_functions(src: str, lines) -> list[tuple[int, int]] | None:
    """(first, last) line of the innermost function around each changed
    line, or None if a line sits outside every function."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans.append((start, node.end_lineno or node.lineno))
    out = set()
    for ln in lines:
        inner = None
        for s, e in spans:
            if s <= ln <= e and (inner is None or e - s < inner[1] - inner[0]):
                inner = (s, e)
        if inner is None:
            return None
        out.add(inner)
    return sorted(out)


def cuttable(task: dict, changed: dict[str, list[tuple[int, int]]]) -> bool:
    """task["post"]: file -> sorted lines run outside the cut. True when no
    changed function has a line there and at least one ran in the cut."""
    post, inside = task["post"], task["cut"]
    hit = False
    for f, spans in changed.items():
        for lines, flag in ((post.get(f, ()), False), (inside.get(f, ()), True)):
            for s, e in spans:
                i = bisect.bisect_left(lines, s)
                if i < len(lines) and lines[i] <= e:
                    if not flag:
                        return False
                    hit = True
    return hit
