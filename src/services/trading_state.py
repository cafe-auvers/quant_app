"""Global live-trading kill switch.

``TRADING_ENABLED`` starts DISABLED on every process launch by design: this
state is deliberately in-memory only, with no persistence to ``settings.json``
or any other file, so a forgotten "left it on" from a previous session can
never silently carry forward into the next one. The in-process toggle (driven
by the UI) is the only way to turn it on.

An explicit falsy, blank, or unrecognized ``TRADING_ENABLED`` value in the
environment or ``.env`` is a one-way lock. Recognized truthy values merely
permit the UI toggle; they never turn trading on by themselves. This lets a dev
clone with inherited PROD credentials remain administratively disarmed while
ensuring a configuration typo also fails closed.
"""
from __future__ import annotations

from src.utils.config import get_env_value

_FALSY_ENV_VALUES = {"0", "false", "no", "off"}
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}

_trading_enabled = False


class TradingDisabledError(RuntimeError):
    """Raised when an order reaches a submission boundary while disarmed."""

    def __init__(self, environment: str, symbol: str) -> None:
        self.environment = str(environment or "").upper()
        self.symbol = str(symbol or "").upper()
        super().__init__(
            f"Live trading is disabled (kill switch off); refused to submit "
            f"{self.environment} order for {self.symbol}. Enable trading from "
            "the toolbar before submitting orders."
        )


def is_trading_locked_disabled() -> bool:
    """Whether machine configuration forbids enabling order submission.

    An unset value permits the per-session UI toggle. A recognized truthy value
    also permits the toggle but never enables it automatically. Blank, falsy,
    and malformed values fail closed so a typo cannot silently remove the
    administrative lock.
    """
    value = get_env_value("TRADING_ENABLED")
    if value is None:
        return False
    normalized = value.strip().lower()
    return normalized in _FALSY_ENV_VALUES or normalized not in _TRUTHY_ENV_VALUES


def is_trading_enabled() -> bool:
    """Whether guarded order submission is currently permitted."""
    global _trading_enabled
    if is_trading_locked_disabled():
        # Do not retain an armed in-memory state behind an administrative lock:
        # removing/changing the env value later must still require a fresh UI
        # confirmation.
        _trading_enabled = False
        return False
    return _trading_enabled


def set_trading_enabled(enabled: bool) -> bool:
    """Set the in-process kill-switch toggle.

    Returns the resulting effective state -- if TRADING_ENABLED is locked off
    via the environment, this always returns False even when ``enabled=True``.
    """
    global _trading_enabled
    _trading_enabled = bool(enabled) and not is_trading_locked_disabled()
    return is_trading_enabled()


def require_trading_enabled(environment: str, symbol: str) -> None:
    """Fail closed at an order-submission boundary."""
    if not is_trading_enabled():
        raise TradingDisabledError(environment=environment, symbol=symbol)


def reset_trading_state_for_tests() -> None:
    """Test-only helper: restore the disabled-by-default in-process state."""
    global _trading_enabled
    _trading_enabled = False
