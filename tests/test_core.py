"""The core loop: record → select → rerun, at file granularity.

Each test creates a tiny git repo with pytester, runs pytest --filemon
via subprocess, edits, and reruns.  The helpers live here because the two
siblings (pytest-filemon, pytest-mortadelo) keep their test suites
self-contained and identical in shape, so a difference in behavior shows
up as a difference in expectations only.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def git(path, *args, check=True):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=check)


def commit(path, msg="commit"):
    git(path, "add", "-A")
    git(path, "commit", "-q", "--allow-empty", "-m", msg)


def run_filemon(pytester, *args):
    return pytester.runpytest_subprocess("--filemon", *args)


def stored_data(path):
    return json.loads((Path(path) / ".pytest_filemon" / "data.json").read_bytes())


@pytest.fixture
def project(pytester):
    tests = pytester.path / "tests"
    tests.mkdir()
    (tests / "conftest.py").touch()
    (tests / "lib.py").write_text("def ping():\n    return 1\n")
    (tests / "other.py").write_text("X = 1\n")
    (tests / "test_a.py").write_text(
        "def test_a():\n    import lib\n    assert lib.ping() == 1\n"
    )
    (tests / "test_b.py").write_text("def test_b():\n    assert 1 + 1 == 2\n")
    (pytester.path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n")
    git(pytester.path, "init", "-q")
    git(pytester.path, "config", "user.email", "t@e.c")
    git(pytester.path, "config", "user.name", "T")
    commit(pytester.path, "initial")
    return pytester


class TestRecord:
    def test_first_run_records_everything(self, project):
        result = run_filemon(project, "-q")
        result.assert_outcomes(passed=2)
        data = stored_data(project.path)
        assert set(data["tests"]) == {
            "tests/test_a.py::test_a", "tests/test_b.py::test_b",
        }
        assert "tests/lib.py" in data["files"]      # imported by test_a
        assert "tests/test_b.py" in data["files"]   # its own module file

    def test_disabled_without_flag(self, project):
        result = project.runpytest_subprocess("-q")
        result.assert_outcomes(passed=2)
        assert not (project.path / ".pytest_filemon").exists()

    def test_pure_rerun_never_activates_the_recorder(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        result = run_filemon(project, "-q")
        assert result.ret == 0  # selection succeeded: nothing needs to run
        result.stdout.fnmatch_lines(["*selected 0/2 tests*"])

    def test_nothing_selected_exits_zero(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        result = run_filemon(project, "-q")
        assert result.ret == 0

    def test_edit_selects_the_importer(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        path = project.path / "tests" / "lib.py"
        path.write_text("def ping():\n    return 1 + int(0)\n")
        result = run_filemon(project, "-v")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*selected 1/2 tests*"])
        result.stdout.fnmatch_lines(["*test_a.py::test_a PASSED*"])

    def test_comment_edit_of_own_file_selects_only_it(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        path = project.path / "tests" / "test_b.py"
        path.write_text("def test_b():\n    assert 1 + 1 == 2  # a comment\n")
        result = run_filemon(project, "-v")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*selected 1/2 tests*"])
        result.stdout.fnmatch_lines(["*test_b.py::test_b PASSED*"])

    def test_edit_of_unimported_file_selects_nothing(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        path = project.path / "tests" / "other.py"
        path.write_text("X = 2\n")
        result = run_filemon(project, "-q")
        assert result.ret == 0
        result.stdout.fnmatch_lines(["*selected 0/2 tests*"])

    def test_new_test_runs(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        (project.path / "tests" / "test_new.py").write_text(
            "def test_new():\n    assert True\n"
        )
        result = run_filemon(project, "-v")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*test_new.py::test_new PASSED*"])

    def test_previously_failed_test_reruns(self, project, monkeypatch):
        monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
        path = project.path / "tests" / "lib.py"
        path.write_text("def ping():\n    return 2\n")
        original_stat = path.stat()
        result = run_filemon(project, "-q")
        result.assert_outcomes(passed=1, failed=1)
        # The string must differ in LENGTH from "return 2": equal-size edits
        # within the same mtime tick leave a stale .pyc (Linux CI).
        path.write_text("def ping():\n    return 1 + int(0)\n")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        result = run_filemon(project, "-q")
        result.assert_outcomes(passed=1)  # test_a (was failed) reruns

    def test_deselected_test_is_not_marked_failed(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        (project.path / "tests" / "lib.py").write_text(
            "def ping():\n    return 1 + int(0)\n"
        )
        run_filemon(project, "-q").assert_outcomes(passed=1)
        data = stored_data(project.path)
        assert data["failed"] == []

    def test_force_runs_all(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        result = run_filemon(project, "-q", "--filemon-force")
        result.assert_outcomes(passed=2)

    def test_clean_discards(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        result = run_filemon(project, "-q", "--filemon-clean")
        result.assert_outcomes(passed=2)
        result.stdout.fnmatch_lines(["*selected 2/2 tests*"])

    def test_genuinely_empty_collection_keeps_exit_five(self, pytester):
        # Without our deselection in play, an empty run keeps pytest's
        # own exit code.
        tests = pytester.path / "tests"
        tests.mkdir()
        (tests / "lib.py").write_text("X = 1\n")
        (pytester.path / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n")
        git(pytester.path, "init", "-q")
        git(pytester.path, "config", "user.email", "t@e.c")
        git(pytester.path, "config", "user.name", "T")
        commit(pytester.path, "initial")
        result = run_filemon(pytester, "-q")
        assert result.ret == 5

    def test_enabled_outside_git_repo_still_runs_tests(self, pytester):
        # --filemon without a git repository: no recording, no selection,
        # no internal error -- the suite just runs.
        (pytester.path / "test_plain.py").write_text(
            "def test_ok():\n    assert True\n"
        )
        result = run_filemon(pytester, "-q")
        result.assert_outcomes(passed=1)
        assert result.ret == 0
        assert not (pytester.path / ".pytest_filemon").exists()


class TestImportDependencies:
    def test_import_time_dep_reruns_everything(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        path = project.path / "tests" / "conftest.py"
        path.write_text("import pathlib\n")
        result = run_filemon(project, "-q")
        result.assert_outcomes(passed=2)
        result.stdout.fnmatch_lines(["*selected 2/2 tests*"])

    def test_cached_import_module_is_recorded(self, project):
        # The sys.modules gap: only the FIRST import loads the module; the
        # second test's importlib.import_module is a plain function call
        # that no code event can see.  The import_module wrap closes it.
        (project.path / "tests" / "test_a.py").write_text(
            "def test_a():\n"
            "    import importlib\n"
            "    lib = importlib.import_module('lib')  # first: loads it\n"
            "    assert lib.ping() == 1\n"
        )
        (project.path / "tests" / "test_b.py").write_text(
            "def test_b():\n"
            "    import importlib\n"
            "    lib = importlib.import_module('lib')  # cached: nothing runs\n"
            "    assert lib.ping() == 1\n"
        )
        run_filemon(project, "-q").assert_outcomes(passed=2)
        data = stored_data(project.path)
        a = data["tests"]["tests/test_a.py::test_a"]
        b = data["tests"]["tests/test_b.py::test_b"]
        files = data["files"]
        lib = files.index("tests/lib.py")
        assert lib in a and lib in b  # both consumers hold the file dep
        # ... and editing lib re-runs both (and both now fail).  The fix
        # must differ in LENGTH: equal-size edits within the same mtime
        # tick leave a stale .pyc.
        (project.path / "tests" / "lib.py").write_text(
            "def ping():\n    return 3 + int(0)\n"
        )
        result = run_filemon(project, "-q")
        result.assert_outcomes(failed=2)

    def test_selector_disables_selection(self, project):
        run_filemon(project, "-q").assert_outcomes(passed=2)
        path = project.path / "tests" / "lib.py"
        path.write_text("def ping():\n    return 1 + int(0)\n")
        result = run_filemon(project, "-q", "-k", "test_b")
        result.assert_outcomes(passed=1)


class TestSpawners:
    def test_spawning_test_always_reruns(self, project):
        (project.path / "tests" / "test_spawn.py").write_text(
            "import subprocess\n\n"
            "def test_spawn():\n"
            "    subprocess.run(['true'])\n"
        )
        run_filemon(project, "-q").assert_outcomes(passed=3)
        data = stored_data(project.path)
        assert "tests/test_spawn.py::test_spawn" in data["always"]
        assert data["tests"]["tests/test_spawn.py::test_spawn"] == []
        # Any change wakes the spawner even though it has no deps on it:
        (project.path / "tests" / "other.py").write_text("X = 2\n")
        result = run_filemon(project, "-q", "-v")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*1 always re-run (spawn subprocesses)*"])
