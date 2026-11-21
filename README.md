# Blast Radius selector

For each sqlglot patch: find the tests that could notice it, run the affordable
ones against the patched code, and report only those that fail.

## How it works

1. **Map** (`data/map.json.gz`, built once at the pinned commit by
   `tools/mapper.py` + `tools/build_map.py`). For each test it records:
   - which source lines ran (`sys.monitoring`), with work sent to process
     pools traced in-process;
   - which sqlglot classes were instantiated (for class-level edits);
   - which files were opened or listed (fixtures);
   - which lines run at import time.
2. **Candidates** (`src/blast/candidates.py`). The changed code is located with
   the AST:
   - an edited statement inside a function → tests that reached that statement;
   - an inserted statement → tests that reached its previous sibling or its
     parent;
   - an edit in a class body → tests that used the class;
   - a module-level edit → tests that used the file, plus users of any file
     that imports the changed names;
   - comment/format-only edits (identical AST) → only the touched functions;
   - a fixture → tests that opened it;
   - anything unparseable or unmapped → every test.
3. **Run or list** (`src/blast/main.py`). From measured CPU costs
   (`data/cpu.json`), each commit either lists its candidates without running
   them or runs them. Very slow tests are always listed without running.
4. **Run** (`src/blast/runner.py`). A forked child runs the tests through
   unittest on a patched copy of sqlglot in `/tmp`:
   - the parent preloads pytest-free dependencies and sqlglot core, so a child
     only re-imports sqlglot when the patch touches a module that is already
     loaded;
   - process pools run in-process, still going through pickle;
   - it stops at the first failure; the tests it didn't run are listed.
5. **Pack**: what gets reported is split across workers to minimise the
   busiest worker's time plus 2 s per worker.

Any error falls back to listing all candidates, so a crash costs time, never
a miss.

## Commands

```sh
make sample      # docker build + run under scorer limits + score (sample set)
./run_local.sh   # same, natively (needs work/sqlglot + work/venv)
python3 tools/eval_candidates.py work/sqlglot blast-radius-sample/sample data/map.json.gz
```

Rebuild the map after changing the mapper:

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../tools MAP_OUT=../rawmap \
  ../venv/bin/python -m pytest -q -p no:cacheprovider -p mapper -n 4 tests
cd ../.. && python3 tools/build_map.py work/rawmap blast-radius-sample/sample/tests.json data/map.json.gz
```
