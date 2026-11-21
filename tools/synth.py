"""Synthetic commits for recall testing: random, generic mutations of the
pinned sqlglot, labeled by running the full suite on each.

python tools/synth.py gen   <repo> <out dir> <n> [seed]
python tools/synth.py label <repo> <out dir> <venv python>   (slow: full suite per commit)

Never used by the selector itself; only to check it misses nothing on kinds of
edit the sample set doesn't show.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3)
    body = "".join(lines)
    if body and not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{path} b/{path}\n" + body


OPS = [(" == ", " != "), (" != ", " == "), (" and ", " or "), (" or ", " and "), (" < ", " <= "),
       (" > ", " >= "), (" is None", " is not None"), (" is not None", " is None"), ("True", "False"),
       ("False", "True"), (" + 1", " - 1"), (" - 1", " + 1"), ("(args, 0)", "(args, 1)"),
       ("(args, 1)", "(args, 0)"), (" not ", " "), ("[0]", "[-1]"), ("[1:]", "[2:]")]


def _func_lines(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(range(n.body[0].lineno, n.end_lineno + 1))
    return set(out)


def _class_attr_lines(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            for s in n.body:
                if isinstance(s, (ast.Assign, ast.AnnAssign)) and s.lineno == s.end_lineno:
                    out.append(s.lineno)
    return out


def _module_assign_lines(tree):
    return [s.lineno for s in tree.body if isinstance(s, (ast.Assign, ast.AnnAssign)) and s.lineno == s.end_lineno]


def mutate(repo: Path, rng: random.Random):
    pys = [p for p in repo.glob("sqlglot/**/*.py") if p.stat().st_size > 2000]
    kinds = os.environ.get("SYNTH_KINDS", "op,op,op,op,classattr,classattr,module,delete,insert,"
                                          "comment,docstring,fixture,testfile,rename,import").split(",")
    kind = rng.choice(kinds)
    if kind == "fixture":
        fx = rng.choice([p for p in repo.glob("tests/fixtures/**/*.sql")])
        old = fx.read_text()
        lines = old.split("\n")
        idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--"))]
        i = rng.choice(idx)
        lines[i] = re.sub(r"\b(\w)", lambda m: m.group(1).upper() if rng.random() < 0.5 else m.group(1), lines[i], count=1)
        if lines[i] == old.split("\n")[i]:
            lines[i] = lines[i] + " "
        return kind, str(fx.relative_to(repo)), old, "\n".join(lines)
    if kind == "testfile":
        tf = rng.choice(list(repo.glob("tests/**/test_*.py")))
        old = tf.read_text()
        strs = [m for m in re.finditer(r'"SELECT [^"\n]{5,80}"', old)]
        if not strs:
            return None
        m = rng.choice(strs)
        new = old[:m.start()] + m.group(0).replace("SELECT", "SELECT DISTINCT", 1) + old[m.end():]
        return kind, str(tf.relative_to(repo)), old, new
    f = rng.choice(pys)
    old = f.read_text()
    tree = ast.parse(old)
    lines = old.split("\n")
    rel = str(f.relative_to(repo))
    if kind == "op":
        cands = [(i, a, b) for i in _func_lines(tree) for a, b in OPS if a in lines[i - 1] and not lines[i - 1].lstrip().startswith("#")]
        if not cands:
            return None
        i, a, b = rng.choice(cands)
        lines[i - 1] = lines[i - 1].replace(a, b, 1)
    elif kind == "classattr":
        cands = _class_attr_lines(tree)
        if not cands:
            return None
        i = rng.choice(cands)
        l = lines[i - 1]
        for a, b in [("True", "False"), ("False", "True"), ("= {", "= {**{}, "), ("= [", "= [*[], ")]:
            if a in l:
                l = l.replace(a, b, 1)
                break
        else:
            l2 = re.sub(r"(\d+)", lambda m: str(int(m.group(1)) + 1), l, count=1)
            if l2 == l:
                return None
            l = l2
        if a == "= {" and l != lines[i - 1]:
            # an empty-unpack is a no-op; remove the first dict entry instead
            l = lines[i - 1]
            l = re.sub(r"\{\s*[^,{}]+:\s*[^,{}]+,\s*", "{", l, count=1)
        lines[i - 1] = l
    elif kind == "module":
        cands = _module_assign_lines(tree)
        if not cands:
            return None
        i = rng.choice(cands)
        l = lines[i - 1]
        l2 = re.sub(r"(\d+)", lambda m: str(int(m.group(1)) + 1), l, count=1)
        if l2 == l:
            l2 = l.replace("True", "False", 1) if "True" in l else l.replace('"', '"x', 1)
        if l2 == l:
            return None
        lines[i - 1] = l2
    elif kind == "delete":
        cands = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.Expr, ast.Return))
                 and n.lineno == n.end_lineno and n.lineno in _func_lines(tree)]
        if not cands:
            return None
        n = rng.choice(cands)
        indent = lines[n.lineno - 1][: len(lines[n.lineno - 1]) - len(lines[n.lineno - 1].lstrip())]
        lines[n.lineno - 1] = indent + "pass"
    elif kind == "insert":
        cands = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.Return)) and n.lineno in _func_lines(tree)]
        if not cands:
            return None
        n = rng.choice(cands)
        indent = lines[n.lineno - 1][: len(lines[n.lineno - 1]) - len(lines[n.lineno - 1].lstrip())]
        lines.insert(n.lineno - 1, indent + rng.choice(["_unused = None", "assert True", "pass"]))
    elif kind == "comment":
        i = rng.randrange(1, len(lines))
        indent = lines[i - 1][: len(lines[i - 1]) - len(lines[i - 1].lstrip())]
        lines.insert(i - 1, indent + "# note: keep in sync")
    elif kind == "docstring":
        docs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(n)
                and isinstance(n.body[0], ast.Expr)]
        if not docs:
            return None
        n = rng.choice(docs).body[0]
        lines[n.lineno - 1] = lines[n.lineno - 1].replace('"""', '"""Note. ', 1)
    elif kind == "rename":
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("__")]
        if not funcs:
            return None
        n = rng.choice(funcs)
        lines[n.lineno - 1] = lines[n.lineno - 1].replace(f"def {n.name}(", f"def {n.name}_v2(", 1)
    elif kind == "import":
        imps = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.lineno == n.end_lineno and len(n.names) > 1]
        if not imps:
            return None
        n = rng.choice(imps)
        name = n.names[-1].asname or n.names[-1].name
        lines[n.lineno - 1] = re.sub(r",\s*" + re.escape(n.names[-1].name) + r"(\s+as\s+\w+)?\s*$", "", lines[n.lineno - 1])
    new = "\n".join(lines)
    if new == old:
        return None
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return kind, rel, old, new


def gen(repo, out, n, seed=0):
    repo, out = Path(repo), Path(out)
    rng = random.Random(int(seed))
    (out / "commits").mkdir(parents=True, exist_ok=True)
    kinds = {}
    made = 0
    while made < int(n):
        r = mutate(repo, rng)
        if r is None:
            continue
        kind, rel, old, new = r
        cid = f"s{int(seed):02d}{made:03d}"
        (out / "commits" / f"{cid}.patch").write_text(_diff(rel, old, new))
        kinds[cid] = kind
        made += 1
    (out / "kinds.json").write_text(json.dumps(kinds, indent=1))


PLUGIN = '''
import json, os
_failed = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _failed.add("COLLECT::" + report.nodeid)
def pytest_unconfigure(config):
    json.dump(sorted(_failed), open(os.environ["FAIL_OUT"], "w"))
'''


def label(repo, out, python):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from blast import patch

    repo, out = Path(repo), Path(out)
    tests = json.loads((Path(__file__).resolve().parents[1] / "blast-radius-sample/sample/tests.json").read_text())
    shutil.copy(Path(__file__).resolve().parents[1] / "blast-radius-sample/sample/tests.json", out / "tests.json")
    by_id = {t["id"]: t["number"] for t in tests["tests"]}
    by_file: dict = {}
    for t in tests["tests"]:
        by_file.setdefault(t["file"], []).append(t["number"])
    kinds = json.loads((out / "kinds.json").read_text())
    ans_path = out / "answers.json"
    answers = json.loads(ans_path.read_text())["commits"] if ans_path.exists() else {}
    tmp = Path(tempfile.mkdtemp(prefix="synth"))
    plug = tmp / "failplug.py"
    plug.write_text(PLUGIN)
    for p in sorted((out / "commits").glob("*.patch")):
        if p.stem in answers and not os.environ.get("RECONFIRM"):
            continue
        work = tmp / "repo"
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(repo, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "posts"))
        for fd in patch.parse(p.read_text()):
            src = (work / fd.old_path).read_text() if fd.old_path and (work / fd.old_path).exists() else None
            new = patch.apply(src, fd)
            if new is None:
                (work / fd.old_path).unlink()
            else:
                (work / fd.new_path).write_text(new)
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{tmp}", FAIL_OUT=str(tmp / "f.json"))
        subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "failplug", "-n", "8",
                        "--timeout", "120", "tests"], cwd=work, env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        failed = json.loads((tmp / "f.json").read_text())
        # confirm serially: under parallel load the slowest tests can time out
        real = [n for n in failed if not n.startswith("COLLECT::")]
        if real:
            subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "failplug",
                            "-p", "no:xdist", "--timeout", "600", *sorted({n.split("[")[0] for n in real})],
                           cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            again = set(json.loads((tmp / "f.json").read_text()))
            failed = [n for n in failed if n.startswith("COLLECT::") or n in again]
        nums = set()
        for nid in failed:
            if nid.startswith("COLLECT::"):
                nums.update(by_file.get(nid[9:], []))
            elif nid in by_id:
                nums.add(by_id[nid])
        answers[p.stem] = {"failing": sorted(nums), "kind": kinds[p.stem]}
        ans_path.write_text(json.dumps({"commits": answers}))
        print(p.stem, kinds[p.stem], len(nums), flush=True)


if __name__ == "__main__":
    {"gen": gen, "label": label}[sys.argv[1]](*sys.argv[2:])
