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
    app_state,
    event_journal,
    state_sync,
    trade_card_repository,
    trading_state,
)  # noqa: E402  (needs sys.path set up above)
from src.core import execution_config  # noqa: E402


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
def _isolate_trade_card_snapshot(monkeypatch, tmp_path):
    """Never let SQLite-backed tests rewrite the operator's recovery board."""

    monkeypatch.setenv(
        "OPERATIONAL_DB_PATH",
        str(tmp_path / "kanban_operational.sqlite3"),
    )
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    monkeypatch.setattr(
        app_state,
        "KANBAN_STATE_METADATA_FILE",
        tmp_path / "kanban_state_metadata.json",
    )


@pytest.fixture(autouse=True)
def _isolate_local_device_identity(monkeypatch, tmp_path):
    """Never let ownership tests replace the workstation's durable UUID.

    Several state-sync helpers persist the effective local role as part of a
    normal claim/demotion. Tests deliberately exercise those helpers with
    identities such as ``laptop-id``; without a session-wide redirect, an
    otherwise isolated test can strand the real execution lease on the next
    application restart.
    """

    monkeypatch.setattr(
        state_sync,
        "LOCAL_DEVICE_ROLE_FILE",
        tmp_path / "device_role.json",
    )


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


@pytest.fixture
def authorized_full_live(monkeypatch):
    """Explicit production-mutation opt-in for gateway characterization tests."""

    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "FULL_LIVE")
    monkeypatch.setattr(execution_config, "KIS_MUTATION_BUDGET_VERIFIED", True)
    monkeypatch.setattr(execution_config, "KIS_SUBMIT_MUTATION_CAPACITY", 10)
    monkeypatch.setattr(execution_config, "KIS_CANCEL_MUTATION_CAPACITY", 10)
    monkeypatch.setattr(execution_config, "KIS_REPLACE_MUTATION_CAPACITY", 10)
    monkeypatch.setattr(execution_config, "KIS_MUTATION_MIN_SPACING_SECONDS", 0.2)
    monkeypatch.setattr(execution_config, "KIS_WS_ENABLED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_PROTOCOL_VERIFIED", True)
    monkeypatch.setattr(execution_config, "KIS_MARKET_DATA_MODE", "WEBSOCKET")
