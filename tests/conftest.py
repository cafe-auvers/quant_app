import sys
from pathlib import Path

import pytest


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
    monkeypatch.setattr(
        event_journal,
        "EVENT_JOURNAL_FILE",
        tmp_path / "event_journal.jsonl",
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
