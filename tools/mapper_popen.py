"""Which sqlglot modules the Python subprocesses of a test load.

The coverage map can't see into subprocesses, so tests that start one
(the map's "popen" list) are candidates for every code change. Run each
of them with a sitecustomize hook on PYTHONPATH that, in every Python
process, records the sqlglot modules loaded by exit; keep the ones from
processes other than the test runner itself. The same hook records every
sqlglot line those processes ran (sys.monitoring, from interpreter start), so
a change inside function bodies none of them ran can't reach them.

cd <sqlglot checkout> && PATH=<venv>/bin:$PATH PYTHONPATH=../../src \
    python ../../tools/mapper_popen.py <map.json.gz> <tests.json> <out.json> [<lines.json>]

Writes {test number: [module files]} and, with lines.json, {test number:
{file: [lines]}}; a test whose subprocesses could not be recorded is left
out of both (and stays a candidate for everything).
"""

import json
import os
import subprocess
import sys
import tempfile

HOOK = """
import atexit, json, os, sys
_lines = {}
_root = os.path.join(os.getcwd(), "sqlglot") + os.sep
def _line(code, ln):
    f = code.co_filename
    if f.startswith(_root):
        _lines.setdefault("sqlglot/" + f[len(_root):].replace(os.sep, "/"), set()).add(ln)
    return sys.monitoring.DISABLE
if os.environ.get("BLAST_POPEN_OUT"):
    sys.monitoring.use_tool_id(4, "popen_lines")
    sys.monitoring.register_callback(4, sys.monitoring.events.LINE, _line)
    sys.monitoring.set_events(4, sys.monitoring.events.LINE)
def _dump():
    out = os.environ.get("BLAST_POPEN_OUT")
    if not out:
        return
    mods = sorted(n for n, m in list(sys.modules.items()) if m is not None and (n == "sqlglot" or n.startswith("sqlglot.")))
    with open(os.path.join(out, f"{os.getpid()}.json"), "w") as fh:
        json.dump({"argv": sys.argv, "mods": mods, "lines": {f: sorted(v) for f, v in _lines.items()}}, fh)
atexit.register(_dump)
"""


def main(map_path, tests_json, out_path, lines_path=None):
    from blast import candidates

    tests = json.load(open(tests_json))["tests"]
    m = candidates.Map(map_path, tests)
    ids = {t["number"]: t["id"] for t in tests}
    files = set()
    for dirpath, _, names in os.walk("sqlglot"):
        files |= {os.path.join(dirpath, n) for n in names if n.endswith(".py")}
    result = {}
    result_lines = {}
    with tempfile.TemporaryDirectory() as hook:
        with open(os.path.join(hook, "sitecustomize.py"), "w") as fh:
            fh.write(HOOK)
        for t in sorted(m.popen):
            f, cls, meth = ids[t].split("::")
            name = f"{f[:-3].replace('/', '.')}.{cls}.{meth}"
            with tempfile.TemporaryDirectory() as out:
                env = dict(os.environ, BLAST_POPEN_OUT=out,
                           PYTHONPATH=os.pathsep.join([hook, os.getcwd(), os.environ.get("PYTHONPATH", "")]))
                r = subprocess.run([sys.executable, "-m", "unittest", name], env=env, capture_output=True, text=True)
                if r.returncode != 0:
                    print(f"{ids[t]} failed, left out:\n{r.stderr[-500:]}", file=sys.stderr)
                    continue
                mods = set()
                lines = {}
                children = 0
                for fn in os.listdir(out):
                    d = json.load(open(os.path.join(out, fn)))
                    if any("unittest" in a for a in d["argv"]):
                        continue  # the test runner itself
                    children += 1
                    mods |= set(d["mods"])
                    for f, ls in d.get("lines", {}).items():
                        lines.setdefault(f, set()).update(ls)
                if not children:
                    print(f"{ids[t]}: no Python subprocess recorded, left out", file=sys.stderr)
                    continue
                paths = set()
                for n in mods:
                    base = n.replace(".", "/")
                    paths |= {p for p in (base + ".py", base + "/__init__.py") if p in files}
                result[str(t)] = sorted(paths)
                result_lines[str(t)] = {f: sorted(v) for f, v in sorted(lines.items())}
                print(f"{ids[t]}: {children} subprocesses, {len(paths)} modules", file=sys.stderr)
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=0)
    if lines_path:
        with open(lines_path, "w") as fh:
            json.dump(result_lines, fh, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:])
