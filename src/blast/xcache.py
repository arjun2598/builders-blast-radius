"""TestExecutor's class setup from rows stored at image build.

tests/test_executor.py's setUpClass reads sixteen TPC-H and TPC-DS CSV files
and converts every value in pure Python, about a second of CPU before any of
its tests can run. The rows it builds depend only on the test file, the test
helpers (the schemas and the fixture folder), the CSV bytes and the standard
library, not on sqlglot. So the image build runs it once on the pinned
commit and stores the rows (python -m blast.xcache <repo> <out>). The same
step checks, each in a fresh child, that the stored path leaves the class
in the same state as the real setup: the same tables (columns and rows,
value types included), DuckDB views, query lists and attributes.

A child uses the stored rows only when the patch leaves all of those inputs
alone (see usable). It still builds each Table with the patched class.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import sys
import time

FILE = "tests/test_executor.py"
CLASS = "TestExecutor"
NODE = FILE + "::" + CLASS
INPUTS = ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py")
DATA_DIRS = ("tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")
SETS = (("tpch_conn", "tpch_tables", "DIR_TPCH", "TPCH_SCHEMA"),
        ("tpcds_conn", "tpcds_tables", "DIR_TPCDS", "TPCDS_SCHEMA"))
VIEW_SQL = """
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """


def _input_files(repo: str) -> list[str]:
    out = [f for f in INPUTS if os.path.exists(os.path.join(repo, f))]
    for d in DATA_DIRS:
        p = os.path.join(repo, d)
        if os.path.isdir(p):
            out += sorted(d + n for n in os.listdir(p))
    return out


def digest(repo: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    for f in _input_files(repo):
        h.update(f.encode() + b"\0")
        with open(os.path.join(repo, f), "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def usable(changed) -> bool:
    """The patch touches none of the setup's inputs, and adds no top-level
    module (it could shadow csv, ast or gzip)."""
    for f in changed:
        if f in INPUTS or f.startswith(DATA_DIRS) or "/" not in f:
            return False
        if not f.startswith(("sqlglot/", "tests/")):
            return False
    return True


class _Lazy:
    """A connection whose pending views run before its first use."""

    def __init__(self, raw):
        self._raw = raw
        self._pending: list[str] = []

    def defer(self, sql: str) -> None:
        self._pending.append(sql)

    def _flush(self) -> None:
        while self._pending:
            self._raw.execute(self._pending.pop(0))

    def __getattr__(self, name):
        if name != "close":
            self._flush()
        return getattr(self._raw, name)

    def __enter__(self):
        self._flush()
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)


def _fast_setup(cls, rows) -> None:
    import duckdb

    te = sys.modules[cls.__module__]
    for conn_name, tables_name, dir_name, schema_name in SETS:
        conn = duckdb.connect()
        if not hasattr(conn, "defer"):
            conn = _Lazy(conn)
        setattr(cls, conn_name, conn)
        tables: dict = {}
        setattr(cls, tables_name, tables)
        directory = getattr(te, dir_name)
        for table, columns in getattr(te, schema_name).items():
            file_name = f"{directory}{table}.csv.gz"
            # verified at build to succeed on these files: run at first use
            conn.defer(VIEW_SQL.format(table=table, file_name=file_name, columns=columns))
            tables[table] = te.Table(columns=columns, rows=rows[tables_name][table])
    cls.cache = {}
    cls.tpch_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    cls.tpcds_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))


def install(cls, path: str) -> bool:
    """Swap cls.setUpClass for the stored-rows path (rows load on first use)."""
    if getattr(cls, "_blast_xcache", False):
        return True
    real = cls.__dict__.get("setUpClass")
    if real is None:
        return False

    def setUpClass(klass):
        try:
            with open(path, "rb") as fh:
                data = pickle.load(fh)
        except Exception:  # noqa: BLE001  no stored rows: the real setup
            return real.__func__(klass)
        _fast_setup(klass, data["rows"])

    cls.setUpClass = classmethod(setUpClass)
    cls._blast_xcache = True
    return True


def load_meta(path: str, repo: str):
    """(stored-path CPU seconds) if the store was built for this checkout."""
    try:
        with open(path + ".meta", "rb") as fh:
            meta = pickle.load(fh)
    except Exception:  # noqa: BLE001
        return None
    if meta.get("digest") != digest(repo):
        return None
    return meta


# --- build ---------------------------------------------------------------

def _state(cls) -> bytes:
    """Everything setUpClass leaves on the class, as bytes."""
    out = {}
    for conn_name, tables_name, _, _ in SETS:
        conn = getattr(cls, conn_name)
        out[conn_name] = conn.execute(
            "SELECT view_name, sql FROM duckdb_views() WHERE NOT internal ORDER BY view_name").fetchall()
        out[tables_name] = [(k, list(t.columns), [tuple((type(v).__name__, v) for v in r) for r in t.rows])
                            for k, t in getattr(cls, tables_name).items()]
    out["cache"] = cls.cache
    out["sqls"] = (cls.tpch_sqls, cls.tpcds_sqls)
    out["attrs"] = sorted(k for k in vars(cls) if not k.startswith("_blast"))
    return pickle.dumps(out, protocol=pickle.HIGHEST_PROTOCOL)


def _child(repo: str, fn) -> bytes:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 0
        try:
            os.chdir(repo)
            sys.path.insert(0, repo)
            os.environ["SKIP_INTEGRATION"] = "0"
            import importlib

            cls = getattr(importlib.import_module(FILE[:-3].replace("/", ".")), CLASS)
            out = fn(cls)
            while out:
                out = out[os.write(w, out):]
        except BaseException:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            code = 1
        os._exit(code)
    os.close(w)
    data = b""
    while chunk := os.read(r, 1 << 20):
        data += chunk
    _, status = os.waitpid(pid, 0)
    if status:
        raise RuntimeError("child failed")
    return data


def build(repo: str, out: str) -> None:
    repo = os.path.abspath(repo)
    out = os.path.abspath(out)
    for f in (out, out + ".meta"):
        if os.path.exists(f):
            os.unlink(f)

    def real(cls):
        import duckdb

        orig = duckdb.connect
        sent: list = []

        class Rec:
            def __init__(self, raw):
                self._raw = raw
                sent.append([])
                self._log = sent[-1]

            def execute(self, sql, *args):
                self._log.append((sql, args))
                return self._raw.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._raw, name)

        duckdb.connect = lambda *a, **k: Rec(orig(*a, **k))
        cls.setUpClass()
        duckdb.connect = orig
        for conn_name, _, _, _ in SETS:
            setattr(cls, conn_name, getattr(cls, conn_name)._raw)
        rows = {tn: {k: t.rows for k, t in getattr(cls, tn).items()} for _, tn, _, _ in SETS}
        return pickle.dumps((rows, (sent, _state(cls))), protocol=pickle.HIGHEST_PROTOCOL)

    rows, want = pickle.loads(_child(repo, real))
    with open(out, "wb") as fh:
        pickle.dump({"rows": rows}, fh, protocol=pickle.HIGHEST_PROTOCOL)

    def fast(cls):
        install(cls, out)
        t0 = time.process_time()
        cls.setUpClass()
        cpu = time.process_time() - t0
        sent = [[(q, ()) for q in getattr(cls, cn)._pending] for cn, _, _, _ in SETS]
        for conn_name, _, _, _ in SETS:
            getattr(cls, conn_name)._flush()
            setattr(cls, conn_name, getattr(cls, conn_name)._raw)
        return pickle.dumps((cpu, (sent, _state(cls))), protocol=pickle.HIGHEST_PROTOCOL)

    cpu, got = pickle.loads(_child(repo, fast))
    if got != want:
        ws, wst = want
        gs, gst = got
        a, b = pickle.loads(wst), pickle.loads(gst)
        print("xcache: sent equal", ws == gs, [k for k in a if a[k] != b.get(k)], file=sys.stderr)
        if ws != gs:
            print(repr(ws[0][0])[:300], "\n", repr(gs[0][0])[:300], file=sys.stderr)
        os.unlink(out)
        print("xcache: stored path differs from the real setup, not used", file=sys.stderr)
        return
    with open(out + ".meta", "wb") as fh:
        pickle.dump({"digest": digest(repo), "cpu": cpu}, fh)
    print(f"xcache: {sum(len(v) for t in rows.values() for v in t.values())} rows, setup cpu {cpu:.2f}s",
          file=sys.stderr)


if __name__ == "__main__":
    build(*sys.argv[1:])
