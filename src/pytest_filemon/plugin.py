"""pytest-filemon: file-level test impact analysis.

Records which repository files each test imports (every import statement,
cached or not, via the ``builtins.__import__`` and
``importlib.import_module`` wraps) and selects only tests whose
dependency files changed.  Dependencies are stored as JSON under
``.pytest_filemon/`` in the repository; ``rm -rf .pytest_filemon`` resets
the cache.  The block-level sibling is ``pytest-mortadelo``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .fingerprint import build_snapshot
from .imports import ImportMonitor
from .selection import Graph, load_graph, select_tests
from .store import Store
from .util import git_root, to_repo_rel

_ENV_TRUTHY = {"1", "true", "yes", "on"}

_DATA_VERSION = 1  # must match selection.py


def _env_enabled() -> bool:
    return os.environ.get("PYTEST_FILEMON", "").strip().lower() in _ENV_TRUTHY


def _selectors_active(config: pytest.Config) -> bool:
    """Whether test selectors (-k, -m, --lf, explicit node ids) are in play.

    When they are, selection is disabled entirely: an explicitly
    requested but unaffected test must not be silently deselected."""
    opts = config.option
    if getattr(opts, "keyword", None) or getattr(opts, "markexpr", None):
        return True
    if getattr(opts, "lf", None) or getattr(opts, "failedfirst", None):
        return True
    return any("::" in arg for arg in getattr(config, "args", []))


# ---------------------------------------------------------------- options


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("filemon", "file-level test impact analysis")
    group.addoption(
        "--filemon",
        action="store_true",
        default=False,
        help="Enable filemon: record per-test import dependencies and "
        "select only tests whose dependency files changed (file fingerprints).",
    )
    group.addoption(
        "--filemon-force",
        action="store_true",
        default=False,
        help="Run all tests, but still record dependencies.",
    )
    group.addoption(
        "--filemon-clean",
        action="store_true",
        default=False,
        help="Discard the stored data before running.",
    )


# ---------------------------------------------------------------- helpers


def _fast_conditions(store, meta):
    """``(changed, fast, pure)`` from meta.json alone.

    If no file in ``dep_files`` changed, HEAD did not move, and nothing
    failed, no recorded dependency can be stale and the whole recording
    machinery is not needed at all.
    """
    if meta is None:
        return None, False, False
    dep_files = meta.get("dep_files")
    if not isinstance(dep_files, list) or meta.get("failed"):
        return None, False, False
    changed = store.changed_files(meta.get("base_commit"))
    if changed is None or changed:
        return changed, False, False
    if store.head() != meta.get("base_commit"):
        return changed, False, False
    return changed, True, True


def _shared_git_root(config, cwd: Path) -> Path | None:
    root = getattr(config, "_filemon_git_root", None)
    if root is not None:
        return root
    root = git_root(cwd)
    if root is not None:
        config._filemon_git_root = root
    return root


def _early_probe_pure_fast(args, invocation_args, early_config) -> bool:
    """Whether the run can skip the monitor entirely (no-change rerun)."""
    if "--filemon-force" in args or "--filemon-clean" in args:
        return False
    for flag in ("--lf", "--ff", "-k", "-m"):
        if flag in args:
            return False
    root = _shared_git_root(early_config, Path.cwd())
    if root is None:
        return False
    store = Store(root)
    _, fast, pure = _fast_conditions(store, store.load_meta())
    return fast and pure


# ---------------------------------------------------------------- hooks


def pytest_load_initial_conftests(early_config, parser, args):
    """Arm the import wraps before conftest loading (import-time tracking)."""
    if ("--filemon" in args or _env_enabled()) and not _early_probe_pure_fast(
        args, early_config.invocation_params.args, early_config
    ):
        root = _shared_git_root(early_config, Path.cwd())
        if root is not None:
            monitor = ImportMonitor(root)
            if monitor.activate():
                early_config._filemon_early_monitor = monitor
    return (yield)


# ---------------------------------------------------------------- state


class FilemonState:
    """Per-session recording and selection state."""

    def __init__(self) -> None:
        self.enabled = False
        self.requested = False
        self.root: Path | None = None
        self.store: Store | None = None
        self.monitor: ImportMonitor | None = None
        self.graph: Graph | None = None
        self.selection = None
        self.failed: set[str] = set()
        self.finished: set[str] = set()
        self.item_map: dict[str, str | None] = {}
        self.module_files: set[str] = set()
        self.conftest_deps: set[str] = set()
        self.notes: list[str] = []
        self._meta = None
        self.fast = False
        self.fast_pure = False
        self.fast_recorded: set[str] = set()
        self.changed: set[str] | None = None

    # -- setup ----------------------------------------------------------

    def setup(self, config: pytest.Config, early_monitor: ImportMonitor | None) -> None:
        self.requested = True
        self.root = _shared_git_root(config, config.rootpath)
        if self.root is None:
            self.notes.append("filemon: not a git repository; running and recording nothing")
            self._drop_early_monitor(early_monitor)
            return
        self.store = Store(self.root)
        self._collect_conftest_deps(config)
        if config.option.filemon_clean:
            self.store.delete()
            self.notes.append("filemon: cleared stored data")
        meta = self.store.load_meta()
        fast = self._try_fast_path(config, meta)
        if not fast:
            self.graph = load_graph(self.store.load_data(), meta)
        if not (fast and self.fast_pure):
            self._activate_late(config, early_monitor)

    def _try_fast_path(self, config: pytest.Config, meta) -> bool:
        """Decide the run without reading data.json."""
        self._meta = meta
        if meta is None or config.option.filemon_force:
            return False
        if _selectors_active(config):
            return False
        changed, fast, pure = _fast_conditions(self.store, meta)
        if changed is not None:
            self.changed = changed
        if not fast:
            return False
        nodeids = [n for n in meta.get("nodeids", []) if isinstance(n, str)]
        self.fast_recorded = set(nodeids)
        self.fast = True
        self.fast_pure = pure
        self.notes.append("filemon: no recorded dependency changed; fast path")
        return True

    def _activate_late(self, config, early_monitor):
        """Arm the wraps now (recovery path when the early probe skipped)."""
        self.monitor = early_monitor
        if self.monitor is None or not self.monitor.active:
            self.monitor = ImportMonitor(self.root)
            self.monitor.activate()

    def _drop_early_monitor(self, monitor):
        if monitor is not None:
            monitor.deactivate()

    def _collect_conftest_deps(self, config: pytest.Config) -> None:
        assert self.root is not None
        for plugin in config.pluginmanager.get_plugins():
            filename = getattr(plugin, "__file__", None)
            if filename and os.path.basename(filename) == "conftest.py":
                rel = to_repo_rel(filename, self.root)
                if rel:
                    self.conftest_deps.add(rel)
        # The active config file: parsed before the earliest hook, so no
        # import ever sees the read.  Any change re-runs everything.
        inifile = getattr(config, "inifile", None)
        if inifile is not None:
            rel = to_repo_rel(os.fspath(inifile), self.root)
            if rel:
                self.conftest_deps.add(rel)

    # -- collection / selection -------------------------------------------

    def plan(self, config: pytest.Config) -> None:
        """Compute the changed set before collection starts."""
        if not self.enabled or self.store is None or self.graph is None:
            return
        if config.option.filemon_force or _selectors_active(config):
            return
        try:
            self.changed = self.store.changed_files(self.graph.base_commit)
        except Exception as e:
            self.notes.append(f"filemon: could not compute changes ({e!r}); running everything")

    def repo_rel(self, path) -> str | None:
        assert self.root is not None
        if self.monitor is not None:
            return self.monitor.repo_rel(os.fspath(path))
        return to_repo_rel(path, self.root)

    def remember_items(self, items: list[pytest.Item]) -> None:
        assert self.root is not None
        rel_of: dict = {}
        for item in items:
            path = item.path
            if path not in rel_of:
                rel_of[path] = to_repo_rel(path, self.root)
            rel = rel_of[path]
            self.item_map[item.nodeid] = rel
            if rel:
                self.module_files.add(rel)

    def select(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        """Select tests among the collected items."""
        assert self.store is not None
        collected = [i.nodeid for i in items]
        if self.fast:
            recorded = self.fast_recorded
            self.selection = _Selection(
                [n for n in collected if n not in recorded],
                [n for n in collected if n in recorded],
                "no recorded dependency changed; fast path",
            )
        elif self.graph is None:
            self.selection = _Selection(collected, [], "no stored data; running everything")
            return
        elif config.option.filemon_force:
            self.selection = _Selection(collected, [], "--filemon-force: running everything")
            return
        elif _selectors_active(config):
            self.selection = _Selection(collected, [], "selectors present: running everything")
            return
        else:
            self.selection = select_tests(collected, self.graph, self.changed)
        sel = self.selection
        if sel.deselected:
            selected_set = set(sel.selected)
            keep = [i for i in items if i.nodeid in selected_set]
            drop = [i for i in items if i.nodeid not in selected_set]
            if drop:
                config.hook.pytest_deselected(items=drop)
                items[:] = keep

    # -- recording ---------------------------------------------------------

    def record_report(self, report) -> None:
        if report.failed:
            self.failed.add(report.nodeid)
        if report.when in ("call", "teardown"):
            self.finished.add(report.nodeid)

    def finish(self, config: pytest.Config, interrupted: bool = False) -> None:
        if self.store is None or self.root is None:
            return
        if self.fast and not self.finished and not self.failed:
            self.notes.append("filemon: nothing ran; stored data left unchanged")
            return
        if self.fast and self.monitor is None:
            self._store_failures_only()
            return
        monitor = self.monitor
        if monitor is not None:
            monitor.phase = "done"
            monitor.current_nodeid = None
        if (
            monitor is not None
            and not self.fast
            and self.graph is not None
            and not self.finished
            and not self.failed
            and not interrupted
        ):
            self.notes.append("filemon: nothing ran; stored data left unchanged")
            return
        if self.fast:
            self.graph = load_graph(self.store.load_data(), self._meta)
        if monitor is None:
            return
        old_graph = self.graph
        import_files, module_collect = self._shared_files(monitor, old_graph)
        tests = self._merge_tests(monitor, module_collect, old_graph, interrupted)
        always = self._marks(monitor, tests, old_graph)
        data, meta, dep_files = _data_documents(
            self.store, tests, import_files, self.failed, always
        )
        self._save(config, monitor, data, meta, dep_files, bool(always))

    def _store_failures_only(self) -> None:
        data = self.store.load_data() or {
            "version": _DATA_VERSION, "files": [], "tests": {}, "import_deps": [],
        }
        data["failed"] = sorted(self.failed)
        meta = dict(self._meta or {})
        meta.update(
            failed=sorted(self.failed),
            base_commit=self.store.head(),
            created=datetime.now(timezone.utc).isoformat(),
        )
        self.store.save(data, meta)
        self.notes.append("filemon: fast path; failure list updated")

    def _shared_files(self, monitor, old_graph):
        """``(shared import files, per-test-module imported files)``."""
        module_collect: dict[str, set[str]] = {}
        import_files: set[str] = set(monitor.import_deps)
        for caller, imported in monitor.collect_deps:
            if caller is not None and caller in self.module_files:
                module_collect.setdefault(caller, set()).add(imported)
            elif imported in self.module_files:
                # A test module imported by pytest's machinery: its own
                # file belongs to its tests.
                module_collect.setdefault(imported, set()).add(imported)
            else:
                import_files.add(imported)
        for rel in self.conftest_deps:
            import_files.add(rel)
        if old_graph is not None:
            import_files |= old_graph.import_deps
        return import_files, module_collect

    def _merge_tests(self, monitor, module_collect, old_graph, interrupted):
        old_tests = old_graph.tests if old_graph is not None else {}
        tests: dict[str, set[str]] = {}
        for nodeid, module_rel in self.item_map.items():
            own = monitor.test_deps.get(nodeid)
            if own is not None and nodeid in self.finished:
                extra = module_collect.get(module_rel, set()) if module_rel else set()
                tests[nodeid] = own | extra if extra else own
            elif nodeid in old_tests:
                tests[nodeid] = set(old_tests[nodeid])
        for nodeid, old_deps in old_tests.items():
            if nodeid not in tests and nodeid in self.item_map:
                tests[nodeid] = set(old_deps)
        # Early stop (-x, --maxfail, interrupt): tests that were *selected*
        # but never ran keep no fresh dependencies, so they must re-run.
        # Correctly deselected tests are NOT failed -- comparing against
        # every collected test would mark them and poison the failure list.
        selected = (
            set(self.selection.selected)
            if self.selection is not None
            else set(self.item_map)
        )
        if interrupted or (self.finished and len(self.finished) < len(selected)):
            for nodeid in selected:
                if nodeid not in self.finished:
                    tests.setdefault(nodeid, set())
                    self.failed.add(nodeid)
        for nodeid in monitor.spawn_tests:
            if nodeid in self.item_map:
                tests[nodeid] = set()
        return tests

    def _marks(self, monitor, tests, old_graph):
        alive = set(tests) | set(self.item_map)
        observed = self.finished
        old_always = old_graph.always if old_graph is not None else set()
        return (set(monitor.spawn_tests) | (old_always - observed)) & alive

    def _save(self, config, monitor, data, meta, dep_files, spawners):
        """Write the JSON files (with fingerprints) to .pytest_filemon/."""
        tracked = self.store.tracked_files(30000) if spawners else None
        snapshot = build_snapshot(
            self.root, dep_files,
            previous=self.store.load_files(),
            extra_files=tracked,
        )
        self.store.save(data, meta, files=snapshot)


def _data_documents(store, tests, import_files, failed, always):
    """``(data, meta, dep_files)`` for the .pytest_filemon/ files."""
    distinct: set[str] = set(import_files)
    for deps in tests.values():
        distinct.update(deps)
    dep_files = set(distinct)
    ordered = sorted(distinct)
    lookup = {f: i for i, f in enumerate(ordered)}.__getitem__
    data = {
        "version": _DATA_VERSION,
        "files": ordered,
        "tests": {nid: sorted(map(lookup, tests[nid])) for nid in sorted(tests)},
        "import_deps": sorted(map(lookup, import_files)),
        "failed": sorted(failed),
        "always": sorted(always),
    }
    meta = {
        "version": 1,
        "base_commit": store.head(),
        "created": datetime.now(timezone.utc).isoformat(),
        "failed": sorted(failed),
        "nodeids": sorted(tests),
        "dep_files": sorted(dep_files),
        "always": sorted(always),
    }
    return data, meta, dep_files


class _Selection:
    """Outcome of the selection for one run."""

    __slots__ = ("deselected", "reason", "selected")

    def __init__(self, selected, deselected, reason=""):
        self.selected = selected
        self.deselected = deselected
        self.reason = reason


# ---------------------------------------------------------------- hooks


def pytest_configure(config: pytest.Config) -> None:
    option = config.option
    enabled = bool(getattr(option, "filemon", False)) or _env_enabled()
    early_monitor = getattr(config, "_filemon_early_monitor", None)
    config._filemon_early_monitor = None
    state = FilemonState()
    config._filemon_state = state
    if not enabled:
        if early_monitor is not None:
            early_monitor.deactivate()
        return
    state.enabled = True
    state.setup(config, early_monitor)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    state = _state_from(config)
    # store is None when the run is outside a git repository: recording is
    # disabled entirely and every test simply runs.
    if state is None or not state.enabled or state.store is None:
        return
    state.remember_items(items)
    state.select(config, items)


@pytest.hookimpl(wrapper=True)
def pytest_collection(session):
    state = _state_from(session.config)
    if state is not None:
        try:
            state.plan(session.config)
        except Exception as e:
            state.notes.append(f"filemon: planning failed ({e!r}); running everything")
    if state is not None and state.monitor is not None:
        state.monitor.phase = "collect"
    result = yield
    if state is not None and state.monitor is not None:
        state.monitor.phase = "test"
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    state = _state_from(item.config)
    if state is not None and state.monitor is not None:
        state.monitor.phase = "test"
        state.monitor.current_nodeid = item.nodeid
    result = yield
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    state = _state_from(item.config)
    if state is not None and state.enabled and report is not None:
        state.record_report(report)
    return report


def pytest_sessionfinish(session, exitstatus):
    state = _state_from(session.config)
    if state is None or not state.enabled:
        return
    # A run whose selection deselected everything is a *successful*
    # selection -- nothing the change affects needs to run -- not pytest's
    # "no tests collected" (5).  Only rewrite the status when the empty run
    # is our doing: a genuinely empty collection keeps exit 5, and a run
    # under -k/-m (where we never deselect) keeps whatever pytest decides.
    if (
        exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED
        and state.selection is not None
        and state.selection.deselected
    ):
        session.exitstatus = pytest.ExitCode.OK
    try:
        state.finish(session.config, interrupted=exitstatus == pytest.ExitCode.INTERRUPTED)
    except Exception as e:
        state.notes.append(f"filemon: failed to store dependency data: {e!r}")


def pytest_unconfigure(config):
    state = _state_from(config)
    early_monitor = getattr(config, "_filemon_early_monitor", None)
    if early_monitor is not None:
        early_monitor.deactivate()
        config._filemon_early_monitor = None
    if state is not None and state.monitor is not None:
        state.monitor.deactivate()


def pytest_report_header(config):
    state = _state_from(config)
    if state is None or not state.enabled or state.store is None:
        return None
    return f"filemon: enabled, data in {state.store.dir}"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    state = _state_from(config)
    if state is None or not state.requested:
        return
    reporter = terminalreporter
    reporter.write_sep("=", "filemon")
    if state.store is not None:
        reporter.write_line(f"data: {state.store.dir}")
    if state.selection is not None:
        sel = state.selection
        reporter.write_line(
            f"selected {len(sel.selected)}/{len(sel.selected) + len(sel.deselected)} tests: {sel.reason}"
        )
    for note in state.notes:
        reporter.write_line(note)


def _state_from(config) -> FilemonState | None:
    return getattr(config, "_filemon_state", None)
