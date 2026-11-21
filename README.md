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
     it from this module (or from one that re-exports it: a star import, an
     import of the name, a package handing out submodule attributes from a
     module-level `__getattr__`), or that can get hold of the module object
     (by importing it or a package above it, or by loading modules
     dynamically) and access the name as an attribute or string. An
     attribute read on a name bound to some other imported module
     (`t.Iterator` with `import typing as t`) doesn't count. A removed
     name read that way at module level or in a class body makes importing
     that file fail: its importers;
   - a removed name still used at import time → the tests that import that
     module (see below);
   - a function line that ran while modules were imported → every test,
     unless importing the patched tree leaves every module- and class-level
     value as it was (`src/blast/fingerprint.py`: one subprocess imports all
     of sqlglot with the patched texts swapped in and digests that state,
     code objects excluded; values that differ between two imports of the
     pinned tree are ignored as noise, and so are names the patch itself adds
     or removes, which the name rules follow). Then the line counts like any
     other. If only lazily loaded modules come out different (a dialect
     whose class body now fails, plus what that changes in eager registries
     and packages), the probe runs again in the pinned and the patched tree
     without those modules and their importers; if the rest then matches,
     the tests that import those modules take the place of every test. The
     same probe decides whether an edit to an eager module that is too
     far-reaching to resolve still needs every test;
   - a test that starts Python subprocesses (`test_lazy_load`) → only when
     the patch touches a module those subprocesses loaded at the pinned
     commit (`data/popen.json`, from `tools/mapper_popen.py`), or anything
     but sqlglot code;
   - comment/format-only edits (identical AST) → nothing, beyond tests that
     read the file or look at code objects (bytecode is unchanged, only line
     numbers move); an encoding-line change counts as code;
   - a patch that changes only one comparison (operator or literal operand)
     → only tests whose recorded operands (`data/cmp`, per test and site)
     could compare differently; a test that never evaluated it is unaffected.
     `==` vs `is` is judged only for literals, identity-equality types and
     members of one enum;
   - a patch that changes only one value of a class-level dict literal →
     tests that read that key's value (`data/keys`) instead of every reader
     of the dict, unless the key was read at import time;
   - a patch that changes only an `except` clause's types → tests that ran
     that header line (it runs only while an exception is being matched);
   - a method added to, removed from or renamed in a generator class →
     tests whose generator (that class or a subclass) looked up the
     expression its `<key>_sql` name dispatches (`data/disp.json.gz`, from a
     pass that swaps the per-class dispatch tables for recording ones), plus
     every line that names the method, plus the handler lookups built from a
     name at run time when an enclosing `isinstance(x, exp.Cls)` guard
     allows the key (read statically from the expression class tree, so
     `connector_sql`'s lookup counts only for `and`/`or`/`xor`). The
     generator finds handlers by method name, so a
     new method changes nothing else;
   - a fixture → tests that opened it; a new data file → tests that list its
     directory;
   - deleted/moved modules, unparseable or unmapped code → every test.

   Resolution has a work budget. Past it, the change falls back to users of
   the file plus users of every file that mentions a touched name.

   **Importer bound.** A changed module that `import sqlglot` does not load
   can only matter to tests that import it. So any "every test" outcome for
   such a file, and any over-budget fallback that is larger, becomes: the
   modules that import it, directly or through others (a static import graph
   of the pinned and the patched tree, module level only), and the tests that
   need one of them. A test needs a module if its test file imports it at the
   top, if it ran its code or created its classes, if it lists its directory,
   or if the module-needs map (`data/imports.json.gz`) says so. That map comes
   from a separate pass (`tools/mapper_imports.py`): sqlglot is imported bare,
   a child per test file imports that file, and a grandchild per test records
   every sqlglot module the test loads or asks for (lazy dialect lookups,
   imports inside functions).
3. **Run or list** (`src/blast/main.py`). From measured CPU costs
   (`data/cpu.json`), each commit either lists its candidates without running
   them or runs them. Very slow tests are always listed without running.
4. **Run** (`src/blast/runner.py`). A forked child runs the tests through
   unittest on a patched copy of sqlglot in `/tmp`:
   - the parent preloads pytest-free dependencies and sqlglot core, so a child
     only re-imports sqlglot when the patch touches a module that is already
     loaded;
   - process pools run in-process, still going through pickle;
   - the parent freezes everything it preloaded out of the garbage
     collector, and children raise its threshold, so the collector runs far
     less often;
   - DuckDB answers recorded at the pinned commit (`data/duck.pkl.gz`) are
     reused when the patched sqlglot sends DuckDB the exact same SQL;
   - a slow test whose process-pool tasks were mapped one by one runs only
     the tasks the change can reach. In the TPC-DS and TPC-H executor tests
     the others yield None, which the test already treats as "not executed".
     In the optimizer's fixture-file tests (`check_file`: simplify,
     qualify_columns, merge_subqueries, optimize, TPC-DS/TPC-H and a few
     more) a skipped task's future raises instead, so the subTest that
     checks that case ends at once and is not counted; each case's check runs
     only with its task. The mapper picks the mode that leaves the
     test passing with no task run;
   - after a failure the rest of that test file is listed without running
     (failures cluster by file). Other files keep running while their
     estimated CPU is below the simulated time listing them would add, given
     the share of files that failed so far; otherwise everything not run is
     listed. A failed subtest ends its test, and the split executor tests'
     pool tasks run only when their results are read (the optimizer's wait
     for all of theirs first, so theirs run at once).
5. **Pack**: what gets reported is split across workers to minimise the
   busiest worker's time plus 2 s per worker.

Any error falls back to listing all candidates, so a crash costs time, never
a miss. A child whose resident memory passes 1.4 GB is killed the same way,
before the container's 4 GB limit can take the whole run down.

What depends only on the pinned checkout (its import graph, its import-state
fingerprint, each file's import table) is computed when the image is built (`python -m blast.prebuild`,
written to `/app/prebuilt.pkl`) and reused if the checkout matches. The same
step writes the decoded test map as a pickle (`/app/map.pkl`), used only if
its hash matches `data/map.json.gz`, which is cheaper than decoding the JSON at every run.

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

Then map the pool tasks of slow tests one by one, record DuckDB answers and
re-measure CPU (with the runner's GC settings):

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src \
  ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawsplit 6 1
cd ../.. && python3 tools/build_map.py work/rawfork blast-radius-sample/sample/tests.json data/map.json.gz work/rawsplit
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=.:../../src ../venv/bin/python ../../tools/duck_record.py \
  ../../data/duck.pkl.gz tests.test_executor tests.test_optimizer
SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src CPU_OUT=../cpu_raw.json \
  ../venv/bin/python -m pytest -q -p no:cacheprovider -p no:xdist -p cpu_probe tests
cd ../.. && python3 tools/cpu_table.py work/cpu_raw.json blast-radius-sample/sample/tests.json data/cpu.json
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=.:../../src ../venv/bin/python ../../tools/split_probe.py \
  ../../blast-radius-sample/sample/tests.json ../../data/map.json.gz ../../data/duck.pkl.gz ../../data/cpu.json \
  tests.test_executor tests.test_optimizer
```

Comparison operands and dict key reads come from two more forked passes
(sqlglot compiled with comparisons rewritten to record their operands; class
dicts swapped for a key-recording subclass):

```sh
cd work/sqlglot && export SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src MAP_LINES=0 MAP_ATTRS=0
MAP_CMP=1 ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawcmp 7
MAP_CMP=1 ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawcmpsplit 7 1
MAP_KEYS=1 ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawkeys 7
MAP_KEYS=1 ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawkeyssplit 7 1
cd ../.. && python3 tools/build_cmp.py work/rawcmp work/rawcmpsplit blast-radius-sample/sample/tests.json data/map.json.gz data/cmp
python3 tools/build_keys.py work/rawkeys work/rawkeyssplit blast-radius-sample/sample/tests.json data/map.json.gz data/keys
```

The generator dispatch map:

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src MAP_LINES=0 MAP_ATTRS=0 MAP_DISPATCH=1 \
  ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawdisp 7
cd ../.. && python3 tools/build_disp.py work/rawdisp blast-radius-sample/sample/tests.json data/disp.json.gz
```

The subprocess module map:

```sh
cd work/sqlglot && PATH=$PWD/../venv/bin:$PATH PYTHONPATH=../../src ../venv/bin/python \
  ../../tools/mapper_popen.py ../../data/map.json.gz ../../blast-radius-sample/sample/tests.json ../../data/popen.json
```

For native runs, build the prebuilt data once (again after rebuilding the map):
`PYTHONPATH=src work/venv/bin/python -m blast.prebuild $PWD/work/sqlglot work/prebuilt.pkl data/map.json.gz`.

The module-needs map:

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src \
  ../venv/bin/python ../../tools/mapper_imports.py ../../blast-radius-sample/sample/tests.json ../rawimports 7
cd ../.. && python3 tools/build_imports.py work/rawimports work/sqlglot data/imports.json.gz
```

Each test is mapped in its own forked process, so caches filled
by one test (the generator's dispatch table, memoized lookups) can't hide
from the map what another test depends on. The older in-process pytest
plugin is still there (`-p mapper`) but under-reports for exactly this reason.
