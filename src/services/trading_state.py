"""Process projection of the deployment-wide live-trading kill switch.

``TRADING_ENABLED`` remains a per-machine administrative hard lock. When the
desktop app attaches an authoritative provider, the mutable ON/OFF state comes
from the canonical shared database and survives restarts and Main-device
handoffs. Test and standalone callers without a provider retain the small
in-process API.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional

from src.utils.config import get_env_value

_FALSY_ENV_VALUES = {"0", "false", "no", "off"}
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}

_trading_enabled = False
_authoritative_provider: Optional[Callable[[], bool]] = None
_emergency_cached_authorization = ContextVar(
    "emergency_cached_trading_authorization",
    default=False,
)


class TradingDisabledError(RuntimeError):
    """Raised when an order reaches a submission boundary while disarmed."""

    def __init__(
        self,
        environment: str,
        symbol: str,
        *,
        reason: str = "kill switch off",
    ) -> None:
        self.environment = str(environment or "").upper()
        self.symbol = str(symbol or "").upper()
        super().__init__(
            f"Live trading is disabled ({reason}); refused to submit "
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


def set_authoritative_provider(
    provider: Optional[Callable[[], bool]],
) -> None:
    """Install the canonical shared-state reader used at broker boundaries."""

    global _authoritative_provider
    _authoritative_provider = provider


@contextmanager
def allow_cached_emergency_authorization() -> Iterator[None]:
    """Permit last-confirmed ON only inside the bounded emergency path.

    WS11 deliberately allows protective exits/cancels to use its fsynced local
    journal during a canonical-DB outage. Ordinary commands still fail closed
    if the shared kill switch cannot be re-read.
    """

    token = _emergency_cached_authorization.set(True)
    try:
        yield
    finally:
        _emergency_cached_authorization.reset(token)


def require_trading_enabled(environment: str, symbol: str) -> None:
    """Re-read shared state and fail closed at an order boundary."""

    global _trading_enabled
    if is_trading_locked_disabled():
        _trading_enabled = False
        raise TradingDisabledError(
            environment=environment,
            symbol=symbol,
            reason="local administrative lock",
        )
    provider = _authoritative_provider
    if provider is not None:
        try:
            _trading_enabled = bool(provider())
        except Exception as exc:
            if not (
                _emergency_cached_authorization.get()
                and _trading_enabled
            ):
                raise TradingDisabledError(
                    environment=environment,
                    symbol=symbol,
                    reason="shared control unavailable",
                ) from exc
    if not _trading_enabled:
        raise TradingDisabledError(environment=environment, symbol=symbol)


def reset_trading_state_for_tests() -> None:
    """Test-only helper: restore the disabled-by-default in-process state."""
    global _trading_enabled, _authoritative_provider
    _trading_enabled = False
    _authoritative_provider = None
