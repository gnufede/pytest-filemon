"""pytest-filemon: file-level test impact analysis (TIA) for pytest.

Records which repository files each test imports — every import statement,
cached or not, through ``builtins.__import__`` and
``importlib.import_module`` wraps — stores the data as JSON under
``.pytest_filemon/``, and uses file fingerprints to select only the tests
whose dependency files changed.  No ``sys.monitoring``, no bytecode
analysis: works on any Python that runs pytest.  The block-level sibling
is ``pytest-mortadelo``.
"""

__version__ = "0.1.0"
