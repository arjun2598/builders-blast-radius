"""Record DuckDB answers for the tests that use DuckDB (see blast/duckcache.py).

cd <sqlglot checkout> && SKIP_INTEGRATION=0 PYTHONPATH=.:../../src \
    python ../../tools/duck_record.py <out duck.pkl.gz> <test module>...
"""

import concurrent.futures
import importlib
import os
import sys
import unittest

from blast import duckcache, runner


def main(out, *modules):
    concurrent.futures.ProcessPoolExecutor = runner.SyncExecutor
    duckcache.install(os.getcwd(), record=True)
    suite = unittest.TestSuite()
    for m in modules:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(m)))
    result = unittest.TestResult()
    suite.run(result)
    if not result.wasSuccessful():
        raise SystemExit(f"tests failed while recording: {(result.failures + result.errors)[:1]}")
    duckcache.save(out)
    print(f"recorded {len(duckcache._store)} results, {os.path.getsize(out)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
