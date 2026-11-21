"""Memoised DuckDB queries.

Several slow tests compare sqlglot's output with what DuckDB computes for the
same query. DuckDB isn't under test: for the same statement on the same data
it gives the same answer. So the answers are recorded once at the pinned
commit (tools/duck_record.py) and reused when a patched sqlglot produces the
exact same SQL text; anything else runs in DuckDB for real.

A result is keyed by the statement plus every statement that ran on that
connection before it (the views it reads), with the checkout path removed.
Only results fetched with fetchdf() are cached, and only for read-only
statements, so a cache hit never skips a side effect.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import pickle

_READ = ("SELECT", "WITH", "(", "FROM", "VALUES")
_store: dict[str, object] = {}
_record = False
_roots: list[str] = []
hits = misses = 0


def _norm(sql: str) -> str:
    for r in _roots:
        sql = sql.replace(r, "<ROOT>")
    return sql


class _Result:
    def __init__(self, conn: "_Conn", sql: str, args, key: str | None):
        self._conn, self._sql, self._args, self._key = conn, sql, args, key
        self._real = None

    def _run(self):
        if self._real is None:
            self._real = self._conn._raw.execute(self._sql, *self._args)
        return self._real

    def fetchdf(self):
        global hits, misses
        if self._key is not None and self._key in _store and not _record:
            hits += 1
            return _store[self._key].copy()  # type: ignore[attr-defined]
        misses += 1
        df = self._run().fetchdf()
        if self._key is not None and _record:
            _store[self._key] = df.copy()
        return df

    df = fetchdf

    def __getattr__(self, name):
        return getattr(self._run(), name)


class _Conn:
    def __init__(self, raw):
        self._raw = raw
        self._hist = hashlib.sha1()

    def execute(self, sql, *args):
        if isinstance(sql, str) and not args and sql.lstrip().upper().startswith(_READ):
            h = self._hist.copy()
            h.update(_norm(sql).encode())
            return _Result(self, sql, args, h.hexdigest())
        if isinstance(sql, str):
            self._hist.update(b"\0" + _norm(sql).encode() + repr(args).encode())
        else:
            self._hist.update(b"\0?")  # unknown statement: nothing after it can hit
            self._hist.update(os.urandom(8))
        return self._raw.execute(sql, *args)

    def __getattr__(self, name):
        # anything else (relation API, cursor, ...) poisons the history
        self._hist.update(os.urandom(8))
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)


def install(root: str, path: str | None = None, record: bool = False) -> None:
    """Wrap duckdb.connect(). With path, load recorded results from it."""
    global _record
    import duckdb

    _record = record
    _roots[:] = sorted({os.path.realpath(root), os.path.abspath(root)}, key=len, reverse=True)
    if path and not record and not _store:
        with gzip.open(path, "rb") as fh:
            _store.update(pickle.load(fh))
    orig = getattr(duckdb.connect, "_blast_orig", duckdb.connect)

    def connect(*a, **k):
        if a or k:  # files or settings: leave alone
            return orig(*a, **k)
        raw = orig()
        try:
            raw.execute("SET threads TO 1")  # same answers, less CPU
        except Exception:  # noqa: BLE001
            pass
        return _Conn(raw)

    connect._blast_orig = orig  # type: ignore[attr-defined]
    duckdb.connect = connect


def save(path: str) -> None:
    with gzip.open(path, "wb", compresslevel=6) as fh:
        pickle.dump(_store, fh, protocol=pickle.HIGHEST_PROTOCOL)


def uninstall() -> None:
    import duckdb

    duckdb.connect = getattr(duckdb.connect, "_blast_orig", duckdb.connect)
