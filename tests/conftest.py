import os
import sys
from pathlib import Path

import pytest

# Force the headless Qt platform for the whole test session before any test
# module gets a chance to construct a QApplication. Individual test files
# (e.g. test_buyboard_runtime_worker.py) previously set this themselves with
# os.environ.setdefault(...), which only worked by accident of pytest's
# alphabetical collection order importing that file (and therefore setting
# this) before other UI test files ran -- a standalone run of one of those
# other files (or any reordering) would construct QApplication against the
# real native platform instead, where calling most QWidget methods on a
# MainWindow.__new__() test double (whose C++ base was never initialized)
# is undefined behavior rather than a catchable Python exception -- up to
# and including a hard process-crashing access violation.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Unit tests must start from the repository's fail-closed execution defaults,
# regardless of a developer workstation's production .env. Tests that exercise
# guarded execution opt in explicitly with a mode override or monkeypatch.
# Set these before importing any application module because execution_config
# snapshots the controlled-live mode at import time.
os.environ["BUYBOARD_ENGINE_ENABLED"] = "false"
os.environ["KIS_LIVE_EXECUTION_MODE"] = "DISABLED"


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.services import (
    event_journal,
    trading_state,
)  # noqa: E402  (needs sys.path set up above)


@pytest.fixture(autouse=True)
def _isolate_event_journal(monkeypatch, tmp_path):
    """Never let UI/lifecycle tests append synthetic events to local runtime data."""
    event_journal.reset_event_journal_runtime_status_for_tests()
    monkeypatch.setattr(
        event_journal,
        "EVENT_JOURNAL_FILE",
        tmp_path / "event_journal.jsonl",
    )
    yield
    event_journal.reset_event_journal_runtime_status_for_tests()


@pytest.fixture(autouse=True)
def _reset_trading_state(monkeypatch):
    """Keep every test disarmed unless it explicitly opts into submission."""
    # Do not let a developer's real .env decide unit-test behavior.
    monkeypatch.setattr(
        trading_state,
        "get_env_value",
        lambda key, default=None: None,
    )
    trading_state.reset_trading_state_for_tests()
    yield
    trading_state.reset_trading_state_for_tests()


@pytest.fixture
def trading_enabled():
    """Explicit opt-in for tests that must cross a submission boundary."""
    assert trading_state.set_trading_enabled(True) is True
    yield
