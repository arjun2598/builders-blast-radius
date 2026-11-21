# Blast Radius selector

For each sqlglot patch: find the tests that could notice it, run the affordable
ones against the patched code, and report only those that fail.

## How it works

1. **Map** (`data/map.json.gz`, built once at the pinned commit by
   `tools/mapper_fork.py` + `tools/build_map.py`, one forked process per test). For each test it records:
   - which source lines ran (`sys.monitoring`), with work sent to process
     pools traced in-process;
   - which sqlglot classes were instantiated (for class-level edits);
   - which files were opened or listed (fixtures);
   - which lines run at import time.
   The mapper also records which class attributes each test reads: every
   plain class-level value is wrapped in a recording descriptor during
   mapping, and reads made while other classes are being created count too.
2. **Candidates** (`src/blast/candidates.py`). The changed code is located with
   the AST, and a resolver follows it across files:
   - an edited statement inside a function → tests that reached that
     statement;
   - an inserted statement → tests that reached its previous sibling or its
     parent;
   - a class attribute edit → tests that read that attribute, plus users of
     classes that read it while being created, plus code elsewhere that names
     it as `Class.ATTR`. If the class's creation hooks (metaclass,
     `__init_subclass__`) mention it, or the statement isn't a plain
     assignment → every user of the class;
   - a module-level name edit (constant, import, function header) → every
     statement that reads the name, in its own file and in files that import
     it or access it as an attribute or string;
   - a removed name still used at import time → every test (imports break);
   - comment/format-only edits (identical AST) → only the touched functions;
   - a fixture → tests that opened it; a new data file → tests that list its
     directory;
   - deleted/moved modules, unparseable or unmapped code → every test.

   Resolution has a work budget. Past it, the change falls back to users of
   the file plus users of every file that mentions a touched name.
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

Recall check on synthetic edits beyond the sample kinds. Random mutations of the
pinned commit are labeled by running the full suite, then failures are
confirmed serially:

```sh
python3 tools/synth.py gen work/sqlglot work/synth 40 7
python3 tools/synth.py label $PWD/work/sqlglot $PWD/work/synth $PWD/work/venv/bin/python
python3 tools/eval_candidates.py work/sqlglot work/synth data/map.json.gz
```

Rebuild the map after changing the mapper:

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src \
  ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawfork 6
cd ../.. && python3 tools/build_map.py work/rawfork blast-radius-sample/sample/tests.json data/map.json.gz
```

Each test is mapped in its own forked process (about 3 min), so caches filled
by one test (the generator's dispatch table, memoized lookups) can't hide
from the map what another test depends on. The older in-process pytest
plugin is still there (`-p mapper`) but under-reports for exactly this reason.
