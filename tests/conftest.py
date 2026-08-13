import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.services import trading_state  # noqa: E402  (needs sys.path set up above)


@pytest.fixture(autouse=True)
def _default_trading_enabled_for_tests(monkeypatch):
    """Guarded order submission is gated behind a kill switch that starts
    disabled on every real process launch (src/services/trading_state.py).
    Most tests exercise submission behavior unrelated to the switch itself and
    were written before it existed, so default it to enabled here and restore
    the real disabled-by-default state afterward. Tests targeting the switch
    itself can still call trading_state.set_trading_enabled(False) (or
    monkeypatch the env lock) within the test body to override this.
    """
    monkeypatch.setattr(trading_state, "_trading_enabled", True)
    yield
    trading_state.reset_trading_state_for_tests()
