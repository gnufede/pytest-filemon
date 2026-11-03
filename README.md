# pytest-filemon

**File-level test impact analysis (TIA)** for pytest — Filemón, the file-level
partner of [pytest-mortadelo](https://github.com/gnufede/pytest-mortadelo)
(block-level).  Both work for the T.I.A. — here: Test Impact Analysis.

| | pytest-filemon |
|---|---|
| dependency storage | local JSON under `.pytest_filemon/` (not versioned; copy the directory to share it) |
| dependency unit | **whole files**: any change to a dependency file re-runs its dependents |
| dependency collection | wraps `builtins.__import__` and `importlib.import_module` — **every import statement, cached imports included**; no code instrumentation |
| import-time dependencies | everything imported before tests start is a dependency of *every* test; modules imported before arming are claimed, and their ASTs backfilled as possible dependencies |
| change detection | **file fingerprints** (size, mtime, content hash) — git only for new-file questions; `git diff` is the fallback |
| Python support | **3.9+** — no `sys.monitoring`, no bytecode analysis; anything that runs pytest |

Requires a git repository.

## Usage

```bash
pip install pytest-filemon

pytest --filemon              # first run: everything runs, dependencies are recorded
pytest --filemon              # second run: only tests whose dependency files changed
```

Options:

| option | effect |
|---|---|
| `--filemon` | enable recording + selection |
| `--filemon-force` | run all tests, but still record dependencies |
| `--filemon-clean` | discard the stored data before running |
| `PYTEST_FILEMON=1` | same as `--filemon` (env form; also enables before conftest loading) |
| `-p no:filemon` | disable the plugin entirely |

If nothing changed and nothing failed, **zero tests run and pytest exits
with code 0**: deselecting everything is a successful selection, not an
error.  (A genuinely empty collection keeps pytest's own exit code 5.)

## How it works

### The import wraps

The recorder wraps `builtins.__import__` — the single choke point every
`import` and `from ... import ...` statement runs through, **hit or miss**
— and `importlib.import_module`.  Each executed statement is resolved to
the repository files it names (the imported module, and its parent
packages, which execute at import too) and recorded as a dependency of
whatever is running:

- **import phase** (before collection: pytest plugins, conftest imports)
  → dependencies of *every* test.  Modules already imported at arming
  time have no statements left to see, so they are claimed from
  `sys.modules`, and their full ASTs are backfilled: every import
  statement in them — including dead branches — whose target is present
  in `sys.modules` becomes a dependency.  Backfill deliberately
  over-approximates what ran before the recorder existed.
- **collect phase** (test module imports) → a test module's own imports
  belong to its tests; a test module imported by pytest's machinery is a
  dependency of its own tests; everything else is shared by every test.
- **test phase** → attributed to the running test's node id.

The wraps see **cached** imports — an import that hits `sys.modules`
fires no code event anywhere, but the statement still runs through
`__import__`, which is what makes whole-file dependencies complete
without any instrumentation: to call into a module, a test must execute
an import statement for it.  This is also why filemon needs none of
mortadelo's extra machinery (the bytecode import scan, the fixture
closure, the `lru_cache` wrap): at file granularity, the plain import
edges subsume all three.

**Subprocess boundaries (always on):** the functions that create
processes are wrapped (`subprocess.Popen`, `os.system`,
`os.fork`/`posix_spawn`/`exec*`, `_posixsubprocess.fork_exec` — which
also covers `multiprocessing`).  Coverage cannot follow a child
process, so when a test spawns one the recorder **stops collecting for
that test, writes no dependencies for it, and lists it in the data's
`always` key: it re-runs whenever anything changed** (reported as `N
always re-run (spawn subprocesses)`); a rerun with *provably nothing
changed* skips them like everything else via the no-change fast path.

The active configuration file (`pytest.ini` / `pyproject.toml` / ...)
is claimed as a shared dependency: a config change selects everything.

### Storage and selection

```
.pytest_filemon/
├── data.json   {"version":1, "files":[...], "tests":{nodeid:[file-index...]}, "import_deps":[...], "failed":[...], "always":[...]}
├── meta.json   {"version":1, "base_commit":..., "created":..., "failed":[...], "nodeids":[...], "dep_files":[...]}
└── files.json  {"v":1, "at":ns, "files":{file:[size, mtime_ns, hash]}, "dirs":{dir:[subdirs]}}
```

One dataset for the whole repository; each file replaced atomically.  The
change detection (fingerprints: stat first, hash on doubt, racy-mtime
protection, new-file detection through one scoped `git ls-files`) is
shared code with pytest-mortadelo — see its README for the details.

A test runs when it has no recorded data (new), failed in the recorded
run, one of its dependency files is in `changed`, a shared import-time
dependency changed, or it is a spawner and anything changed.  Selectors
(`-k`, `-m`, `--lf`, explicit `file.py::test` arguments) disable
selection entirely: an explicitly requested but unaffected test must
never be silently skipped.

## File granularity, honestly

Filemon re-runs *every* dependent of a changed file, even for a one-line
edit in a branch the test never touched — that is the trade: no
instrumentation, any Python, tiny overhead.  What it does **not** see:

- **Stored / dynamic code paths**: a test only depends on files it (or
  its module at collection, or a conftest in its chain) *imports*.  A
  test that calls `stored_functions[0]()` — a function defined in
  `lib.py` and appended to a list at import time — records a dependency
  on `lib.py` only if that import happened in its own chain.  When the
  list was filled by *another test module's* import, the edge belongs to
  that module's tests alone and this test records nothing: edit
  `lib.py`, and this test is silently skipped.
- **`.so` artifacts are files too**: `import fast` resolves the
  extension module's path — a rebuild re-runs its importers.  `ctypes`
  dlopen without an import statement is not seen (mortadelo's world had
  the same limit).
- **Data files** (fixtures, configs beyond the active pytest ini): not
  dependencies — nothing executes them.
- **Backfill is an over-approximation**: before the recorder armed,
  imports in dead branches of already-loaded modules count as
  dependencies once their target is in `sys.modules`.  Post-arm events
  record only executed statements.
- **State-dispatched tests** (env vars, the clock, randomness): the
  fundamental limit of any TIA.

For block-level precision (function/class granularity, stale-block
intersection, module skeletons), use
[pytest-mortadelo](https://github.com/gnufede/pytest-mortadelo).

## Development

The repo is managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync            # create/update .venv from uv.lock (dev tools included)
uv run pytest      # run the test suite (syncs automatically if needed)
uv run ruff check src tests
```

The test suite uses `pytester` subprocess runs against real git
repositories, so it needs network-free local git only.

### Live demo

`demo/sqlalchemy_demo.py` runs a live overhead + edit→deselect benchmark
against sqlalchemy's ORM suite (2,480 tests over 12 files), with only
comment or no-op edits — nothing breaks, everything is restored:

```bash
python3 demo/sqlalchemy_demo.py [path-to-sqlalchemy-checkout]
```

If the checkout has no `.venv*` virtualenv, the demo creates
`<checkout>/.venv` and installs sqlalchemy, pytest and this plugin
editable into it. Set `SQLALCHEMY_PYTHON` to use an existing interpreter.

Pytest output is streamed live, each edit is shown as a colored
`git diff`, and the demo waits for Enter between steps, so it can be
driven as a live walkthrough.

It shows the record overhead, the no-change fast path (0 tests, exit 0,
under a second), and where the deselect boundary sits — see the script
header for details.

## Layout

```
src/pytest_filemon/
├── plugin.py      # pytest hooks, lifecycle, options
├── imports.py     # the import wraps: __import__ / import_module + backfill
├── fingerprint.py # [size, mtime_ns, hash] snapshots and change detection
├── selection.py   # stored-data parsing + test selection
├── store.py       # .pytest_filemon/ JSON storage
└── util.py        # path normalization
```
