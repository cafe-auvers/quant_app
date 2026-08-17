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
from enum import Enum


class MarketDataOutageRiskTier(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


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


def _env_text(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return str(raw).strip() if raw is not None else default


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

# --- Exit order TTL / cancel-confirm / reprice cycle (code review: "a
# partially filled working [Sell All] order could remain open indefinitely
# ... a significant unattended-trading risk"). Mirrors the entry side's
# ENTRY_ATTEMPT_TTL_SECONDS two-phase cancel (request -> await broker
# confirmation) rather than treating a cancel request as an immediate
# cancellation -- see src.services.trading_engine's exit-order
# reconciliation stages.
PARTIAL_EXIT_ATTEMPT_TTL_SECONDS = _env_int("PARTIAL_EXIT_ATTEMPT_TTL_SECONDS", 10)
SELL_ALL_ATTEMPT_TTL_SECONDS = _env_int("SELL_ALL_ATTEMPT_TTL_SECONDS", 5)
EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS = _env_int(
    "EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS", 10
)

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
AMBIGUOUS_SUBMISSION_CANDIDATE_WINDOW_SECONDS = _env_int(
    "AMBIGUOUS_SUBMISSION_CANDIDATE_WINDOW_SECONDS", 60
)
MIN_ABSENCE_CONFIRMATION_INTERVAL_SECONDS = _env_int(
    "MIN_ABSENCE_CONFIRMATION_INTERVAL_SECONDS", 60
)
FALLBACK_POSITION_PRICE_POLL_SECONDS = _env_int(
    "FALLBACK_POSITION_PRICE_POLL_SECONDS", 2
)
QUOTE_STALE_AFTER_SECONDS = _env_int("QUOTE_STALE_AFTER_SECONDS", 3)

# --- Production KIS WebSocket market data (Workstream 5) -------------------
# All activation switches fail closed.  In particular, KIS_WS_ENABLED is not
# sufficient on its own: the live protocol matrix must have been completed
# and explicitly acknowledged before the production transport may start.
KIS_WS_ENABLED = _env_bool("KIS_WS_ENABLED", False)
KIS_WS_PROTOCOL_VERIFIED = _env_bool("KIS_WS_PROTOCOL_VERIFIED", False)
KIS_MARKET_DATA_MODE = _env_text("KIS_MARKET_DATA_MODE", "REST_DISPLAY_ONLY").upper()
KIS_MARKET_DATA_FALLBACK_MODE = _env_text(
    "KIS_MARKET_DATA_FALLBACK_MODE", "DISPLAY_ONLY"
).upper()
# Opening ranges continue to use execution-grade KIS minute bars. PR4 does
# not silently switch ORB construction to local tick aggregation.
ORB_FORMATION_SOURCE = _env_text("ORB_FORMATION_SOURCE", "KIS_MINUTE_BARS").upper()
KIS_WS_APPROVAL_KEY_TTL_SECONDS = _env_int(
    "KIS_WS_APPROVAL_KEY_TTL_SECONDS", 23 * 60 * 60
)
KIS_WS_AUTH_MAX_RETRIES = _env_int("KIS_WS_AUTH_MAX_RETRIES", 3)
KIS_WS_RECONNECT_INITIAL_SECONDS = _env_float(
    "KIS_WS_RECONNECT_INITIAL_SECONDS", 1.0
)
KIS_WS_RECONNECT_MAX_SECONDS = _env_float("KIS_WS_RECONNECT_MAX_SECONDS", 30.0)
KIS_WS_RECONNECT_JITTER_SECONDS = _env_float(
    "KIS_WS_RECONNECT_JITTER_SECONDS", 0.5
)
KIS_WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS = _env_float(
    "KIS_WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS", 5.0
)
BROKER_EVENT_STALE_SECONDS = _env_float("BROKER_EVENT_STALE_SECONDS", 3.0)
LOCAL_RECEIVE_STALE_SECONDS = _env_float("LOCAL_RECEIVE_STALE_SECONDS", 3.0)
MAX_MARKET_DATA_QUEUE_DELAY_SECONDS = _env_float(
    "MAX_MARKET_DATA_QUEUE_DELAY_SECONDS", 1.0
)
MAX_BROKER_CLOCK_SKEW_SECONDS = _env_float("MAX_BROKER_CLOCK_SKEW_SECONDS", 5.0)
MAX_FUTURE_BROKER_EVENT_SECONDS = _env_float(
    "MAX_FUTURE_BROKER_EVENT_SECONDS", 1.0
)
# Capacity remains zero until Workstream 0 records the measured KIS limits.
# A guessed unlimited/default capacity would violate INV-20.
KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY = _env_int(
    "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY", 0
)
KIS_WS_TRADE_CHANNEL_CAPACITY = _env_int("KIS_WS_TRADE_CHANNEL_CAPACITY", 0)
KIS_WS_QUOTE_CHANNEL_CAPACITY = _env_int("KIS_WS_QUOTE_CHANNEL_CAPACITY", 0)
KIS_WS_RAW_CAPTURE_ENABLED = _env_bool("KIS_WS_RAW_CAPTURE_ENABLED", False)
# Stable identity used only after a symbol is explicitly assigned to this
# Kanban strategy in the durable ownership table.  It does not activate the
# engine or transfer ownership by itself.
KANBAN_STRATEGY_INSTANCE_ID = _env_text(
    "KANBAN_STRATEGY_INSTANCE_ID", "buyboard-orb-v1"
)

# Existing-position outage policy.  Defaults are deliberately conservative
# and remain inert while BUYBOARD_ENGINE_ENABLED is false.
MARKET_DATA_OUTAGE_GRACE_SECONDS = _env_int(
    "MARKET_DATA_OUTAGE_GRACE_SECONDS", 15
)
MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS = _env_int(
    "MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS", 120
)
MARKET_DATA_OUTAGE_RISK_BUFFER_PCT = _env_float(
    "MARKET_DATA_OUTAGE_RISK_BUFFER_PCT", 0.01
)
MARKET_DATA_OUTAGE_LOSS_THRESHOLD_PCT = _env_float(
    "MARKET_DATA_OUTAGE_LOSS_THRESHOLD_PCT", 0.02
)
MARKET_DATA_OUTAGE_SUPERVISED_HOLD_ONLY = _env_bool(
    "MARKET_DATA_OUTAGE_SUPERVISED_HOLD_ONLY", False
)
MARKET_DATA_OUTAGE_ACCOUNT_RISK_PCT = _env_float(
    "MARKET_DATA_OUTAGE_ACCOUNT_RISK_PCT", 0.01
)
MARKET_DATA_OUTAGE_CONCENTRATION_PCT = _env_float(
    "MARKET_DATA_OUTAGE_CONCENTRATION_PCT", 0.20
)
MARKET_DATA_OUTAGE_STOP_DISTANCE_ATR = _env_float(
    "MARKET_DATA_OUTAGE_STOP_DISTANCE_ATR", 0.5
)
EMERGENCY_EXIT_MAX_REPRICE_ATTEMPTS = _env_int(
    "EMERGENCY_EXIT_MAX_REPRICE_ATTEMPTS", 3
)
# Separate from market-data outage timing by design (INV-24). This bounds
# how long a lease last verified at the canonical database may authorize
# emergency-only mutations while that database remains unreachable.
EMERGENCY_LEASE_ALLOWANCE_SECONDS = _env_float(
    "EMERGENCY_LEASE_ALLOWANCE_SECONDS", 30.0
)

# Workstream 10 / Workstream 0 boundary. These values are inert unless the
# operator explicitly records that the KIS mutation limits were measured and
# verified. Keeping VERIFIED false leaves all new-entry buckets UNKNOWN.
KIS_MUTATION_BUDGET_VERIFIED = _env_bool(
    "KIS_MUTATION_BUDGET_VERIFIED", False
)
KIS_SUBMIT_MUTATION_CAPACITY = _env_int(
    "KIS_SUBMIT_MUTATION_CAPACITY", 0
)
KIS_CANCEL_MUTATION_CAPACITY = _env_int(
    "KIS_CANCEL_MUTATION_CAPACITY", 0
)
KIS_REPLACE_MUTATION_CAPACITY = _env_int(
    "KIS_REPLACE_MUTATION_CAPACITY", 0
)
KIS_MUTATION_BUDGET_WINDOW_SECONDS = _env_float(
    "KIS_MUTATION_BUDGET_WINDOW_SECONDS", 1.0
)

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
