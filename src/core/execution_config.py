"""Central execution-engine configuration. ``buydashboard_to_kanban.md``
section 28.

Single source of truth for the new Kanban entry/position/EOD engine's timing
constants, instead of scattering literals across UI files the way the
legacy Buy Dashboard does today (e.g. ``buylist/constants.py``'s
``STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT``). Every value is env-var overridable
the same way that module already does it, for paper/controlled-live tuning
without a code change.
"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# --- Entry attempt lifetime and retry (section 393-399) ---------------------
ENTRY_ATTEMPT_TTL_SECONDS = _env_int("ENTRY_ATTEMPT_TTL_SECONDS", 15)
ENTRY_RETRY_COOLDOWN_SECONDS = _env_int("ENTRY_RETRY_COOLDOWN_SECONDS", 3)
MAX_ENTRY_ATTEMPTS_PER_SYMBOL_PER_MINUTE = _env_int(
    "MAX_ENTRY_ATTEMPTS_PER_SYMBOL_PER_MINUTE", 4
)

# --- Exit (Sell All / Partial Sell) retry backoff (code review finding P1-4) -
# A liquidation must retry aggressively (an open position with no working
# sell order is real, ongoing risk) but a rejected/erroring submission must
# not be resubmitted on literally every 1-second heartbeat tick.
EXIT_RETRY_COOLDOWN_SECONDS = _env_int("EXIT_RETRY_COOLDOWN_SECONDS", 5)

# Discount applied to the last trade price when no live bid is available to
# build a marketable SELL limit (code review finding P0-3's production sell
# adapter). Mirrors the existing legacy Buy Dashboard's
# ``src.ui.buylist.constants.STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT`` -- same
# number, re-declared here rather than importing across the services/ui
# boundary.
SELL_MARKETABLE_DISCOUNT_PCT = _env_float("SELL_MARKETABLE_DISCOUNT_PCT", 0.005)

# --- One-second engine cadence (section 771-806) -----------------------------
ENGINE_HEARTBEAT_SECONDS = _env_int("ENGINE_HEARTBEAT_SECONDS", 1)
PENDING_ORDER_RECONCILIATION_SECONDS = _env_int(
    "PENDING_ORDER_RECONCILIATION_SECONDS", 2
)
UNKNOWN_ORDER_RECONCILIATION_SECONDS = _env_int(
    "UNKNOWN_ORDER_RECONCILIATION_SECONDS", 1
)
ACTIVE_ACCOUNT_REFRESH_SECONDS = _env_int("ACTIVE_ACCOUNT_REFRESH_SECONDS", 5)
IDLE_ACCOUNT_REFRESH_SECONDS = _env_int("IDLE_ACCOUNT_REFRESH_SECONDS", 20)
FULL_RECONCILIATION_SECONDS = _env_int("FULL_RECONCILIATION_SECONDS", 60)
FALLBACK_POSITION_PRICE_POLL_SECONDS = _env_int(
    "FALLBACK_POSITION_PRICE_POLL_SECONDS", 2
)
QUOTE_STALE_AFTER_SECONDS = _env_int("QUOTE_STALE_AFTER_SECONDS", 3)

# --- End of day (section 505-511) -------------------------------------------
EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE = _env_int(
    "EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE", 60
)

# --- Breakeven stop (section 632-644) ---------------------------------------
# Account-specific (commission + tax + slippage estimate); the spec marks
# this ``ACCOUNT_SPECIFIC`` rather than giving a default. 15 bps is a
# conservative placeholder covering typical US overseas-brokerage round-trip
# commission -- must be reviewed against the real account's fee schedule
# before any live breakeven stop is placed off of it.
BREAKEVEN_BUFFER_BPS = _env_float("BREAKEVEN_BUFFER_BPS", 15.0)


def is_buyboard_engine_enabled() -> bool:
    """Fail-closed cutover flag for the new Kanban entry/position/EOD engine.

    Defaults to ``False``: while unset, the new engine services must never
    submit, cancel, or reprice a broker order, mirroring how
    :mod:`src.services.trading_state`'s kill switch fails closed. The legacy
    Buy Dashboard's 60-second monitor loop remains the live, authoritative
    trading path regardless of this flag -- flipping it on is a deliberate,
    separate, user-directed step taken only after paper/controlled-live
    validation (spec section 1076-1082's Phase 7), never a side effect of
    deploying this code.
    """
    return _env_bool("BUYBOARD_ENGINE_ENABLED", False)
