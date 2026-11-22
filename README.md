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
     but sqlglot code. The same pass records the lines those subprocesses
     ran (`data/popen_lines.json`); a change that sits only inside bodies of
     functions they never ran (not headers, defaults, docstrings or module
     code) can't reach them either;
   - comment/format-only edits (identical AST) → nothing, beyond tests that
     read the file or look at code objects (bytecode is unchanged, only line
     numbers move); an encoding-line change counts as code;
   - edits only inside docstrings (the same AST with docstrings blanked) →
     tests that read the file, tests whose file mentions a doc reader
     (`__doc__`, `getdoc`, `doctest`, `pydoc`, `help(`) and tests that ran a
     sqlglot line that does;
   - refactors proven from the two ASTs (`src/blast/refactor.py`) → the same
     as a comment-only edit: a local variable renamed everywhere in its
     function (not a parameter; no `locals()`/`eval`, no f-string `{x=}`); a
     name renamed throughout the file when neither name appears in another
     source file or in a string, and no name-built lookup (`<key>_sql`,
     `getattr` with an f-string prefix) can reach it; runs of plain defs
     reordered (decorators from a harmless list, no default, annotation or
     decorator naming another def of the run); runs of imports reordered
     when no name is bound twice. Reordered imports of sqlglot's own modules
     in a module `import sqlglot` loads are checked with the import-state
     probe (circular imports, registries filled in import order);
   - a new class member that is a literal or a plainly decorated def, named
     nowhere else, in a class whose creation hooks don't walk its namespace
     → nothing (it shadows nothing and nothing reads it); a new module named
     by no code → only tests that walk packages or directories, or start
     subprocesses;
   - edits only inside the bodies of module- or class-level lambdas (outside
     any call) → tests that ran those lambda lines, when the edit is on
     lines the import itself doesn't run (the lambda's first line runs when
     it is created, and import-time lines are credited to no test);
   - a module-level `if`/`try` that binds no name and defines no code, read
     because a name it reads changed → decided by the import-state probe,
     which ignores the names the patch assigns directly (their readers are
     followed by name);
   - a patch that changes only one comparison (operator or literal operand)
     → only tests whose recorded operands (`data/cmp`, per test and site)
     could compare differently; a test that never evaluated it is unaffected.
     `==` vs `is` is judged only for literals, identity-equality types and
     members of one enum, and for `==`/`!=` between a literal of a builtin
     type and an object whose pinned `__eq__` begins by checking
     `type(self) is type(other)` (after an optional `self is other`), with
     `__ne__` the default or `not self.__eq__(other)`, when the edit is in
     neither (Expression's `x == None` is then `x is None`);
   - a patch that only rewrites one `and`/`or` over the same operands, in
     the same order (`a and b` → `a or b`, regrouping) → only tests whose
     recorded operand truth values (`data/bool`, per test and site) give a
     different result, or that would now skip or newly evaluate an operand
     that isn't a plain attribute, name or pure builtin call;
   - a patch that changes only literal parameter defaults → tests that ran
     those functions' bodies (a default is read only by calls that leave the
     argument out), unless a body also runs at import or shares the
     signature's line; a `typing.overload` stub is dropped when defined, so
     its defaults reach no call;
   - a patch that flips one `arg_types` flag → tests that validated an
     instance of that class or a subclass with that argument missing
     (`data/req.json.gz`, from a `MAP_REQ=1` pass wrapping
     `Expression.error_messages`, the only reader of `required_args`);
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
     directory. A change to a `.sql` fixture that only adds, drops or edits
     `--` comment lines selects nothing: the test helpers read those files
     only through `_filter_comments`, which drops such lines (checked from
     the helper's pinned source, and only while no test file opens a `.sql`
     file any other way). An edit to a `.sql` fixture that keeps its number
     of pairs (as the pinned helpers' own loader splits the old and new text)
     reaches, of the tests that opened it, only those that read a changed
     pair. `tools/pair_map.py` runs each such test with the loader's pairs
     tagged and records, per fixture, tests that never read a pair (their
     class setup loaded the file for other tests) and tests that submit one
     pool task per pair, task k for pair k. The first are dropped, the second
     run only the tasks of changed pairs (`data/pairs.json`); every other
     reader runs whole. Only while the test code is the pinned code;
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
   them or runs them, or runs only the tests cheapest per listed second they
   would save when that is clearly cheaper than both. Very slow tests are
   always listed without running.
4. **Run** (`src/blast/runner.py`). A forked child runs the tests through
   unittest on a patched copy of sqlglot in `/tmp`:
   - the parent preloads pytest-free dependencies and sqlglot core, so a child
     only re-imports sqlglot when the patch touches a module that is already
     loaded;
   - process pools run in-process, still going through pickle;
   - the parent freezes everything it preloaded out of the garbage
     collector, and children raise its threshold, so the collector runs far
     less often; a child imports each test module with the collector off and
     then freezes what that import created;
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
   - a slow test with a scaled-down stand-in (`src/blast/proxies.py`) runs
     the stand-in instead: `test_max_nodes` builds the same statement shapes
     and makes the same calls and assertions at a hundredth of the size.
     `tools/proxy_map.py` checks at the pinned commit that the stand-in passes
     and runs every sqlglot line the real test runs (`data/proxy.json`); only
     those stand-ins are used, and the cost model prices the test at the
     stand-in's cost;
   - TestExecutor's class setup reads sixteen TPC CSV files and converts
     every value in pure Python before any of its tests can run. Only the
     test file, the test helpers, the CSV bytes and the standard library
     decide the rows it builds, so the image build runs it once and stores
     them (`python -m blast.xcache`, `/app/xcache.pkl`). That step also
     checks, in fresh children, that the stored path leaves the class in the
     same state: tables, value types, DuckDB views and the statements sent,
     query lists and attributes. A child uses it only when the patch leaves
     all of those inputs alone and adds no top-level module. It still builds
     each Table with the patched class, and creates the DuckDB views at
     first use (still part of the recorded-answer key);
   - after a failure the rest of that test file is listed without running
     (failures cluster by file). Other files keep running while their
     estimated CPU is below the simulated time listing them would add, given
     the share of files that failed so far; otherwise everything not run is
     listed. A failed subtest ends its test, and the split executor tests'
     pool tasks run only when their results are read (the optimizer's wait
     for all of theirs first, so theirs run at once). A slow test that
     usually still passes when a sibling in its file fails (a per-test prior
     from `tools/sibling_prior.py`, `data/sibling.json`) is run anyway when
     that chance times its listed cost outweighs its CPU.
5. **Pack**: what gets reported is split across workers to minimise the
   busiest worker's time plus 2 s per worker. Once every line is written and every
   child reaped, the process exits without the interpreter's teardown.

Children run one at a time while the batch is on schedule: on the scorer's
two CPUs (often two threads of one core) two children at once each burn more
CPU seconds. Past 30 s both slots run.

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

The same pass with `MAP_BOOL=1` (into `../rawbool` and `../rawboolsplit`)
records the truth value of every operand of `and`/`or` in a truth context;
`tools/build_cmp.py` turns it into `data/bool`.

Stage cuts for the TPC executor tasks (`data/stage.json.gz`). Each pool task
runs alone with line tracing. Lines run inside `optimize` (or the test's
`parse_one`) are counted apart from all others: setup, pickling, planning,
Python execution and the DuckDB check. A digest of the optimized
expression is recorded with them, plus the CPU of each task and of its
optimize call. When every function a code change touches ran only inside
`optimize` for a task, the runner runs that task only up to `optimize` and
compares the digest. If it matches, the rest would run as at the pinned
commit, so the task is skipped. Otherwise the task runs in full. If the
first few digests of a big test all differ, the test is given up on and
listed. This is off unless `BLAST_STAGE` points at the map: on the practice
commits it ran tests that failed anyway, adding CPU without saving time:

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../src ../venv/bin/python ../../tools/stage_map.py ../../data/stage.json.gz 7
```

The required-flag map (`MAP_REQ=1` into `../rawreq` and `../rawreqsplit` the
same way, then, from `work/sqlglot`,
`python ../../tools/build_req.py ../rawreq ../rawreqsplit ../../blast-radius-sample/sample/tests.json ../../data/map.json.gz ../../data/req.json.gz`).

The generator dispatch map:

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../tools:../../src MAP_LINES=0 MAP_ATTRS=0 MAP_DISPATCH=1 \
  ../venv/bin/python ../../tools/mapper_fork.py ../../blast-radius-sample/sample/tests.json ../rawdisp 7
cd ../.. && python3 tools/build_disp.py work/rawdisp blast-radius-sample/sample/tests.json data/disp.json.gz
```

The fixture pair map:

```sh
cd work/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=../../src ../venv/bin/python \
  ../../tools/pair_map.py ../../data/map.json.gz ../../blast-radius-sample/sample/tests.json ../../data/pairs.json 6
```

The subprocess module map:

```sh
cd work/sqlglot && PATH=$PWD/../venv/bin:$PATH PYTHONPATH=../../src ../venv/bin/python \
  ../../tools/mapper_popen.py ../../data/map.json.gz ../../blast-radius-sample/sample/tests.json ../../data/popen.json ../../data/popen_lines.json
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
