"""Global live-trading kill switch.

``TRADING_ENABLED`` starts DISABLED on every process launch by design: this
state is deliberately in-memory only, with no persistence to ``settings.json``
or any other file, so a forgotten "left it on" from a previous session can
never silently carry forward into the next one. The in-process toggle (driven
by the UI) is the only way to turn it on.

An explicit ``TRADING_ENABLED`` value of ``0``/``false``/``no``/``off`` in the
environment or ``.env`` is a one-way lock: it can force trading permanently off
for a given machine/process (e.g. a dev clone that inherited PROD credentials
and must never be allowed to submit live orders), but it can never force
trading on -- only the in-process toggle can do that, and only for the
lifetime of the current process.
"""
from __future__ import annotations

from src.utils.config import get_env_value

_FALSY_ENV_VALUES = {"0", "false", "no", "off"}

_trading_enabled = False


def is_trading_locked_disabled() -> bool:
    """True when TRADING_ENABLED is explicitly falsy in the environment/.env."""
    value = get_env_value("TRADING_ENABLED")
    if value is None:
        return False
    return value.strip().lower() in _FALSY_ENV_VALUES


def is_trading_enabled() -> bool:
    """Whether guarded order submission is currently permitted."""
    if is_trading_locked_disabled():
        return False
    return _trading_enabled


def set_trading_enabled(enabled: bool) -> bool:
    """Set the in-process kill-switch toggle.

    Returns the resulting effective state -- if TRADING_ENABLED is locked off
    via the environment, this always returns False even when ``enabled=True``.
    """
    global _trading_enabled
    _trading_enabled = bool(enabled)
    return is_trading_enabled()


def reset_trading_state_for_tests() -> None:
    """Test-only helper: restore the disabled-by-default in-process state."""
    global _trading_enabled
    _trading_enabled = False
