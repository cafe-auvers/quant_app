"""Central execution-engine configuration. ``buydashboard_to_kanban.md``
section 28.

Single source of truth for the new Kanban entry/position/EOD engine's timing
constants, instead of scattering literals across UI files. Non-secret values
come from ``config/runtime.json`` plus ``config/runtime.local.json``; startup
installs them into the process before this module resolves its typed values.
Explicit OS-level variables remain useful for isolated tests and emergency
deployment overrides without putting operational configuration in ``.env``.
"""
from __future__ import annotations

import math
import os
from enum import Enum
from typing import Optional


class MarketDataOutageRiskTier(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


_configuration_issues: dict[str, str] = {}
_entry_configuration_keys: set[str] = set()


def _record_configuration_issue(
    name: str,
    reason: str,
    *,
    entry_boundary: bool,
) -> None:
    # Never retain the raw override value: even though the settings parsed
    # in this module are non-secret, diagnostics should remain safe to display.
    _configuration_issues[name] = reason
    if entry_boundary:
        _entry_configuration_keys.add(name)


def _clear_configuration_issue(name: str) -> None:
    _configuration_issues.pop(name, None)
    _entry_configuration_keys.discard(name)


def configuration_issues() -> tuple[str, ...]:
    """Return sanitized invalid-override diagnostics for Health/UI surfaces."""

    return tuple(
        f"{name}: {_configuration_issues[name]}"
        for name in sorted(_configuration_issues)
    )


def entry_configuration_issues() -> tuple[str, ...]:
    """Return invalid overrides that must block exposure-increasing BUYs."""

    return tuple(
        f"{name}: {_configuration_issues[name]}"
        for name in sorted(_entry_configuration_keys)
        if name in _configuration_issues
    )


def _env_int(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    entry_boundary: bool = False,
) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        _clear_configuration_issue(name)
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        _record_configuration_issue(
            name,
            f"must be an integer; using safe default {default}",
            entry_boundary=entry_boundary,
        )
        return default
    if minimum is not None and value < minimum:
        _record_configuration_issue(
            name,
            f"must be at least {minimum}; using safe default {default}",
            entry_boundary=entry_boundary,
        )
        return default
    if maximum is not None and value > maximum:
        _record_configuration_issue(
            name,
            f"must be at most {maximum}; using safe default {default}",
            entry_boundary=entry_boundary,
        )
        return default
    _clear_configuration_issue(name)
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    minimum_exclusive: bool = False,
    entry_boundary: bool = False,
) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        _clear_configuration_issue(name)
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        _record_configuration_issue(
            name,
            f"must be numeric; using safe default {default}",
            entry_boundary=entry_boundary,
        )
        return default
    if not math.isfinite(value):
        _record_configuration_issue(
            name,
            f"must be finite; using safe default {default}",
            entry_boundary=entry_boundary,
        )
        return default
    below_minimum = minimum is not None and (
        value <= minimum if minimum_exclusive else value < minimum
    )
    if below_minimum:
        comparator = "greater than" if minimum_exclusive else "at least"
        _record_configuration_issue(
            name,
            f"must be {comparator} {minimum}; using safe default {default}",
            entry_boundary=entry_boundary,
        )
        return default
    if maximum is not None and value > maximum:
        _record_configuration_issue(
            name,
            f"must be at most {maximum}; using safe default {default}",
            entry_boundary=entry_boundary,
        )
        return default
    _clear_configuration_issue(name)
    return value


def _env_bool(name: str, default: bool, *, fail_closed: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        _clear_configuration_issue(name)
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        _clear_configuration_issue(name)
        return True
    if normalized in {"0", "false", "no", "off"}:
        _clear_configuration_issue(name)
        return False
    _record_configuration_issue(
        name,
        "must be true/false, yes/no, on/off, or 1/0; using fail-closed value",
        entry_boundary=False,
    )
    return False if fail_closed else default


def _env_text(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return str(raw).strip() if raw is not None else default


# --- Entry attempt lifetime and retry (section 393-399) ---------------------
ENTRY_ATTEMPT_TTL_SECONDS = _env_int("ENTRY_ATTEMPT_TTL_SECONDS", 15, minimum=1)
ENTRY_RETRY_COOLDOWN_SECONDS = _env_int(
    "ENTRY_RETRY_COOLDOWN_SECONDS", 3, minimum=0
)
MAX_ENTRY_ATTEMPTS_PER_SYMBOL_PER_MINUTE = _env_int(
    "MAX_ENTRY_ATTEMPTS_PER_SYMBOL_PER_MINUTE", 4, minimum=1
)

# --- Exit (Sell All / Partial Sell) retry backoff (code review finding P1-4) -
# A liquidation must retry aggressively (an open position with no working
# sell order is real, ongoing risk) but a rejected/erroring submission must
# not be resubmitted on literally every 1-second heartbeat tick.
EXIT_RETRY_COOLDOWN_SECONDS = _env_int(
    "EXIT_RETRY_COOLDOWN_SECONDS", 5, minimum=0
)

# --- Exit order TTL / cancel-confirm / reprice cycle (code review: "a
# partially filled working [Sell All] order could remain open indefinitely
# ... a significant unattended-trading risk"). Mirrors the entry side's
# ENTRY_ATTEMPT_TTL_SECONDS two-phase cancel (request -> await broker
# confirmation) rather than treating a cancel request as an immediate
# cancellation -- see src.services.trading_engine's exit-order
# reconciliation stages.
PARTIAL_EXIT_ATTEMPT_TTL_SECONDS = _env_int(
    "PARTIAL_EXIT_ATTEMPT_TTL_SECONDS", 10, minimum=1
)
SELL_ALL_ATTEMPT_TTL_SECONDS = _env_int(
    "SELL_ALL_ATTEMPT_TTL_SECONDS", 5, minimum=1
)
EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS = _env_int(
    "EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS", 10, minimum=1
)

# Discount applied to the last trade price when no live bid is available to
# build a marketable SELL limit (code review finding P0-3's production sell
# adapter). Mirrors the existing legacy Buy Dashboard's
# ``src.ui.buylist.constants.STOP_LOSS_SELL_LIMIT_DISCOUNT_PCT`` -- same
# number, re-declared here rather than importing across the services/ui
# boundary.
SELL_MARKETABLE_DISCOUNT_PCT = _env_float(
    "SELL_MARKETABLE_DISCOUNT_PCT", 0.005, minimum=0.0, maximum=1.0
)

# --- One-second engine cadence (section 771-806) -----------------------------
ENGINE_HEARTBEAT_SECONDS = _env_int("ENGINE_HEARTBEAT_SECONDS", 1, minimum=1)
# Published in runtime readiness so a newly deployed client refuses to share
# the coordination store with a still-running pre-budget peer. This is a
# protocol identity, not an operator-overridable setting.
COORDINATION_RU_PROFILE = "operator-executor-sync-v8"
# The market/ORB loop above stays at one second. These independent cadences
# cap Internet coordination traffic without delaying broker-boundary fencing.
COORDINATION_ACTIVE_CARD_POLL_SECONDS = max(
    180.0,
    _env_float(
        "COORDINATION_ACTIVE_CARD_POLL_SECONDS", 180.0, minimum=180.0
    ),
)
COORDINATION_STANDBY_CARD_POLL_SECONDS = max(
    300.0,
    _env_float(
        "COORDINATION_STANDBY_CARD_POLL_SECONDS", 300.0, minimum=300.0
    ),
)
# The runtime-device heartbeat is itself a real coordination-store write, and
# every safety-critical mutation independently proves database/lease state.
# A separate no-op write every three minutes is sufficient to detect a
# connection that is readable but not writable. Runtime readiness writes and
# every safety-critical mutation independently prove the write path sooner.
COORDINATION_DATABASE_PROBE_SECONDS = max(
    180.0,
    _env_float("COORDINATION_DATABASE_PROBE_SECONDS", 180.0, minimum=180.0),
)
COORDINATION_LEASE_POLL_SECONDS = max(
    20.0,
    _env_float("COORDINATION_LEASE_POLL_SECONDS", 20.0, minimum=20.0),
)
# Runtime liveness is the one routine TiDB write that remains while a device
# is otherwise idle. Local/Tailscale change pulses handle five-second peer
# synchronization, so TiDB only needs a coarse four-minute publication.
# Every safety-critical mutation still proves its database/lease state at the
# action boundary and publishes immediately when runtime details change.
COORDINATION_DEVICE_HEARTBEAT_SECONDS = max(
    240.0,
    _env_float(
        "COORDINATION_DEVICE_HEARTBEAT_SECONDS", 240.0, minimum=240.0
    ),
)
# The stale-owner and standby-readiness fences must always exceed the write
# cadence. Sixty seconds of delivery/scheduling margin prevents an ordinary
# delayed heartbeat from authorizing an unsafe failover.
COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS = max(
    COORDINATION_DEVICE_HEARTBEAT_SECONDS + 60.0,
    _env_float(
        "COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS",
        300.0,
        minimum=COORDINATION_DEVICE_HEARTBEAT_SECONDS + 60.0,
    ),
)
COORDINATION_OWNERSHIP_PROOF_SECONDS = max(
    30.0,
    _env_float("COORDINATION_OWNERSHIP_PROOF_SECONDS", 30.0, minimum=30.0),
)
COORDINATION_ALERT_POLL_SECONDS = max(
    90.0,
    _env_float("COORDINATION_ALERT_POLL_SECONDS", 90.0, minimum=90.0),
)
# Liveness monitoring belongs to the external watchdog, not the SQL
# coordination store.  The runtime checks this cadence from its local
# one-second loop and publishes without waiting for the TiDB alert poll.
# Successful webhook traffic is cheap enough to be frequent; only compact
# audit evidence is written to TiDB, once per hour or on a status transition.
EXTERNAL_WATCHDOG_HEARTBEAT_SECONDS = max(
    1.0,
    _env_float("EXTERNAL_WATCHDOG_HEARTBEAT_SECONDS", 5.0, minimum=1.0),
)
EXTERNAL_WATCHDOG_TIDB_AUDIT_SECONDS = max(
    3600.0,
    _env_float(
        "EXTERNAL_WATCHDOG_TIDB_AUDIT_SECONDS", 3600.0, minimum=3600.0
    ),
)
# Once a critical alert has been delivered successfully, the durable incident
# remains open until an operator acknowledges it. Re-sending the same alert
# every five minutes created hundreds of delivery rows per incident without
# improving liveness. Keep a six-hour reminder while failed deliveries retain
# their independent exponential retry schedule.
EXTERNAL_ALERT_ACK_REMINDER_SECONDS = max(
    3600.0,
    _env_float(
        "EXTERNAL_ALERT_ACK_REMINDER_SECONDS", 21600.0, minimum=3600.0
    ),
)
# Operator commands are the hottest remaining coordination read while the US
# regular session is open.  Production measurements near 20 RU/s were
# consistent with the remaining empty-queue lookup's one-second cadence. A
# later production sample still measured 17--18 RU/s with the three-second
# floor. Twenty seconds is now the non-overridable minimum required by the
# 7--9 RU/s cluster budget. Local controls are immediate; only a command
# created on the other device waits for this fallback poll.
# The local market/stop loop and broker-boundary lease proof are independent.
COORDINATION_OPERATOR_COMMAND_POLL_SECONDS = max(
    20.0,
    _env_float(
        "COORDINATION_OPERATOR_COMMAND_POLL_SECONDS", 20.0, minimum=20.0
    ),
)
COORDINATION_OFF_HOURS_POLL_SECONDS = max(
    300.0,
    _env_float("COORDINATION_OFF_HOURS_POLL_SECONDS", 300.0, minimum=300.0),
)

# Manual planning/command synchronization follows the control topology rather
# than the market clock.  When Operator Control and Execution Ownership are on
# different devices, both sides check the shared canonical revisions at this
# cadence.  The hot loop is stopped when control is locked or both roles are on
# the same device.  Keep this protocol value exact so two deployed peers agree
# about the maximum command-delivery delay.
COORDINATION_SPLIT_ROLE_SYNC_SECONDS = 5.0

# UI projection/display polling is not an execution boundary. Local changes
# invalidate immediately and owner activation force-loads canonical state.
COORDINATION_STATE_SYNC_SECONDS = max(
    180.0,
    _env_float("COORDINATION_STATE_SYNC_SECONDS", 180.0, minimum=180.0),
)
COORDINATION_BOARD_PROJECTION_SECONDS = max(
    180.0,
    _env_float(
        "COORDINATION_BOARD_PROJECTION_SECONDS", 180.0, minimum=180.0
    ),
)
# When the existing Tailscale listener confirms change-pulse protocol v2/v3,
# unchanged display/card/command reads use this disaster-recovery fallback.
# Normal cross-device changes arrive as local tokens and reconcile at once.
COORDINATION_REMOTE_FALLBACK_SECONDS = max(
    3600.0,
    _env_float(
        "COORDINATION_REMOTE_FALLBACK_SECONDS", 3600.0, minimum=3600.0
    ),
)
PENDING_ORDER_RECONCILIATION_SECONDS = max(
    2, _env_int("PENDING_ORDER_RECONCILIATION_SECONDS", 2, minimum=2)
)
UNKNOWN_ORDER_RECONCILIATION_SECONDS = max(
    1, _env_int("UNKNOWN_ORDER_RECONCILIATION_SECONDS", 1, minimum=1)
)
ACTIVE_ACCOUNT_REFRESH_SECONDS = _env_int(
    "ACTIVE_ACCOUNT_REFRESH_SECONDS", 5, minimum=1
)
IDLE_ACCOUNT_REFRESH_SECONDS = _env_int(
    "IDLE_ACCOUNT_REFRESH_SECONDS", 20, minimum=1
)
FULL_RECONCILIATION_SECONDS = _env_int(
    "FULL_RECONCILIATION_SECONDS", 60, minimum=1
)
# Broker truth still refreshes every minute. The relational comparison side
# is process-local between canonical writes and is force-refreshed from TiDB
# periodically, avoiding the same three unchanged table reads every minute.
COORDINATION_RECONCILIATION_CACHE_SECONDS = max(
    300.0,
    _env_float(
        "COORDINATION_RECONCILIATION_CACHE_SECONDS", 900.0, minimum=300.0
    ),
)
# Broker status/fill/recovery transitions are persisted immediately.  When an
# exact working order is observed with no semantic change, its durable
# last-seen audit timestamp is coalesced; terminal rows need no periodic touch.
DURABLE_ORDER_OBSERVATION_SECONDS = max(
    3600, _env_int("DURABLE_ORDER_OBSERVATION_SECONDS", 3600, minimum=3600)
)
AMBIGUOUS_SUBMISSION_CANDIDATE_WINDOW_SECONDS = _env_int(
    "AMBIGUOUS_SUBMISSION_CANDIDATE_WINDOW_SECONDS", 60, minimum=1
)
MIN_ABSENCE_CONFIRMATION_INTERVAL_SECONDS = _env_int(
    "MIN_ABSENCE_CONFIRMATION_INTERVAL_SECONDS", 60, minimum=1
)
FALLBACK_POSITION_PRICE_POLL_SECONDS = _env_int(
    "FALLBACK_POSITION_PRICE_POLL_SECONDS", 2, minimum=1
)
QUOTE_STALE_AFTER_SECONDS = _env_int("QUOTE_STALE_AFTER_SECONDS", 3, minimum=1)

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
    "KIS_WS_APPROVAL_KEY_TTL_SECONDS", 23 * 60 * 60, minimum=1
)
KIS_WS_AUTH_MAX_RETRIES = _env_int("KIS_WS_AUTH_MAX_RETRIES", 3, minimum=1)
KIS_WS_RECONNECT_INITIAL_SECONDS = _env_float(
    "KIS_WS_RECONNECT_INITIAL_SECONDS", 1.0, minimum=0.0
)
KIS_WS_RECONNECT_MAX_SECONDS = _env_float(
    "KIS_WS_RECONNECT_MAX_SECONDS",
    max(30.0, KIS_WS_RECONNECT_INITIAL_SECONDS),
    minimum=KIS_WS_RECONNECT_INITIAL_SECONDS,
)
KIS_WS_RECONNECT_JITTER_SECONDS = _env_float(
    "KIS_WS_RECONNECT_JITTER_SECONDS", 0.5, minimum=0.0
)
KIS_WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS = _env_float(
    "KIS_WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS",
    5.0,
    minimum=0.0,
    minimum_exclusive=True,
)
BROKER_EVENT_STALE_SECONDS = _env_float(
    "BROKER_EVENT_STALE_SECONDS", 3.0, minimum=0.0, minimum_exclusive=True
)
LOCAL_RECEIVE_STALE_SECONDS = _env_float(
    "LOCAL_RECEIVE_STALE_SECONDS", 3.0, minimum=0.0, minimum_exclusive=True
)
MAX_MARKET_DATA_QUEUE_DELAY_SECONDS = _env_float(
    "MAX_MARKET_DATA_QUEUE_DELAY_SECONDS", 1.0, minimum=0.0
)
MAX_BROKER_CLOCK_SKEW_SECONDS = _env_float(
    "MAX_BROKER_CLOCK_SKEW_SECONDS", 5.0, minimum=0.0
)
MAX_FUTURE_BROKER_EVENT_SECONDS = _env_float(
    "MAX_FUTURE_BROKER_EVENT_SECONDS", 1.0, minimum=0.0
)
# Capacity remains zero until Workstream 0 records the measured KIS limits.
# A guessed unlimited/default capacity would violate INV-20.
KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY = _env_int(
    "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY", 0, minimum=0
)
# Deprecated as live KIS limits: credentialed WS0 evidence proved one
# aggregate pool. These remain only for compatibility with older diagnostics;
# production composition derives both candidate sets from TOTAL capacity.
KIS_WS_TRADE_CHANNEL_CAPACITY = _env_int(
    "KIS_WS_TRADE_CHANNEL_CAPACITY", 0, minimum=0
)
KIS_WS_QUOTE_CHANNEL_CAPACITY = _env_int(
    "KIS_WS_QUOTE_CHANNEL_CAPACITY", 0, minimum=0
)
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
    "MARKET_DATA_OUTAGE_GRACE_SECONDS", 15, minimum=0
)
MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS = _env_int(
    "MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS", 120, minimum=0
)
MARKET_DATA_OUTAGE_RISK_BUFFER_PCT = _env_float(
    "MARKET_DATA_OUTAGE_RISK_BUFFER_PCT", 0.01, minimum=0.0, maximum=1.0
)
MARKET_DATA_OUTAGE_LOSS_THRESHOLD_PCT = _env_float(
    "MARKET_DATA_OUTAGE_LOSS_THRESHOLD_PCT", 0.02, minimum=0.0, maximum=1.0
)
MARKET_DATA_OUTAGE_SUPERVISED_HOLD_ONLY = _env_bool(
    "MARKET_DATA_OUTAGE_SUPERVISED_HOLD_ONLY", False
)
MARKET_DATA_OUTAGE_ACCOUNT_RISK_PCT = _env_float(
    "MARKET_DATA_OUTAGE_ACCOUNT_RISK_PCT", 0.01, minimum=0.0, maximum=1.0
)
MARKET_DATA_OUTAGE_CONCENTRATION_PCT = _env_float(
    "MARKET_DATA_OUTAGE_CONCENTRATION_PCT", 0.20, minimum=0.0, maximum=1.0
)
MARKET_DATA_OUTAGE_STOP_DISTANCE_ATR = _env_float(
    "MARKET_DATA_OUTAGE_STOP_DISTANCE_ATR", 0.5, minimum=0.0
)
EMERGENCY_EXIT_MAX_REPRICE_ATTEMPTS = _env_int(
    "EMERGENCY_EXIT_MAX_REPRICE_ATTEMPTS", 3, minimum=1
)
# Separate from market-data outage timing by design (INV-24). This bounds
# how long a lease last verified at the canonical database may authorize
# emergency-only mutations while that database remains unreachable.
EMERGENCY_LEASE_ALLOWANCE_SECONDS = _env_float(
    "EMERGENCY_LEASE_ALLOWANCE_SECONDS", 30.0, minimum=0.0
)

# Workstream 10 / Workstream 0 boundary. These values are inert unless the
# operator explicitly records that the KIS mutation limits were measured and
# verified. Keeping VERIFIED false leaves all new-entry buckets UNKNOWN.
KIS_MUTATION_BUDGET_VERIFIED = _env_bool(
    "KIS_MUTATION_BUDGET_VERIFIED", False
)
KIS_SUBMIT_MUTATION_CAPACITY = _env_int(
    "KIS_SUBMIT_MUTATION_CAPACITY", 0, minimum=0
)
KIS_CANCEL_MUTATION_CAPACITY = _env_int(
    "KIS_CANCEL_MUTATION_CAPACITY", 0, minimum=0
)
KIS_REPLACE_MUTATION_CAPACITY = _env_int(
    "KIS_REPLACE_MUTATION_CAPACITY", 0, minimum=0
)
KIS_MUTATION_BUDGET_WINDOW_SECONDS = _env_float(
    "KIS_MUTATION_BUDGET_WINDOW_SECONDS",
    1.0,
    minimum=0.0,
    minimum_exclusive=True,
)
# KIS applies a shared per-second transaction allowance across endpoints. Keep
# headroom below the nominal bucket totals for token, account, order-history,
# display, and mutation traffic sharing this process.
KIS_REQUEST_MIN_SPACING_SECONDS = max(
    0.0, _env_float("KIS_REQUEST_MIN_SPACING_SECONDS", 0.1, minimum=0.0)
)
# Process-wide spacing is an independent upper bound across endpoint buckets.
# The controlled-live default is deliberately slower than the bucket totals;
# a strategy cannot override it for a burst.
KIS_MUTATION_MIN_SPACING_SECONDS = _env_float(
    "KIS_MUTATION_MIN_SPACING_SECONDS", 0.2, minimum=0.0
)
# One means no scheduler-level retry, including a clean pre-acceptance rate
# refusal. The workflow may make a later, freshly reconciled decision with a
# new deterministic identity; the scheduler never loops during the pilot.
KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS = _env_int(
    "KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS", 1, minimum=1
)

# Supervised production envelope. DISABLED is an additional one-way fence on
# the Kanban engine even when the administrative and in-session trading
# switches are both armed. CONTROLLED_LIVE permits BUY entries only for
# persisted active Trade Cards and caps each entry command's notional.
# Protective SELL/cancel paths are not constrained by the entry cap. FULL_LIVE
# is a later explicit operational promotion, not a code-path change. Symbols
# deliberately do not live in environment configuration; Buy Today is the
# operator-owned persisted live-stock list.
KIS_LIVE_EXECUTION_MODE = _env_text(
    "KIS_LIVE_EXECUTION_MODE", "DISABLED"
).upper()
KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL = _env_float(
    "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL", 0.0, minimum=0.0
)

# --- Portfolio-level entry governor ---------------------------------------
# These account-wide limits are evaluated from the canonical card set before
# every exposure-increasing entry.  They do not restrict SELL/cancel/recovery
# paths.  Advanced limits whose current runtime input is not yet canonical
# (daily P&L, drawdown, classifications) default to zero/disabled rather than
# fabricating data; PortfolioRiskManager still implements their fail-closed
# behavior for providers that opt in.
PORTFOLIO_MAX_SIMULTANEOUS_POSITIONS = _env_int(
    "PORTFOLIO_MAX_SIMULTANEOUS_POSITIONS",
    30,
    minimum=1,
    maximum=30,
    entry_boundary=True,
)
PORTFOLIO_MAX_TOTAL_OPEN_RISK_FRACTION = _env_float(
    "PORTFOLIO_MAX_TOTAL_OPEN_RISK_FRACTION",
    0.10,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION = _env_float(
    "PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION",
    2.0,
    minimum=0.0,
    maximum=10.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_INCREMENTAL_BUYING_POWER_FRACTION = _env_float(
    "PORTFOLIO_MAX_INCREMENTAL_BUYING_POWER_FRACTION",
    0.0,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_DAILY_LOSS_FRACTION = _env_float(
    "PORTFOLIO_MAX_DAILY_LOSS_FRACTION",
    0.0,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_DRAWDOWN_FRACTION = _env_float(
    "PORTFOLIO_MAX_DRAWDOWN_FRACTION",
    0.0,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_SECTOR_NOTIONAL_FRACTION = _env_float(
    "PORTFOLIO_MAX_SECTOR_NOTIONAL_FRACTION",
    0.0,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_INDUSTRY_NOTIONAL_FRACTION = _env_float(
    "PORTFOLIO_MAX_INDUSTRY_NOTIONAL_FRACTION",
    0.0,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_CORRELATION_GROUP_NOTIONAL_FRACTION = _env_float(
    "PORTFOLIO_MAX_CORRELATION_GROUP_NOTIONAL_FRACTION",
    0.0,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_STRATEGY_NOTIONAL_FRACTION = _env_float(
    "PORTFOLIO_MAX_STRATEGY_NOTIONAL_FRACTION",
    0.0,
    minimum=0.0,
    maximum=1.0,
    entry_boundary=True,
)
PORTFOLIO_MAX_FX_AGE_SECONDS = _env_float(
    "PORTFOLIO_MAX_FX_AGE_SECONDS",
    300.0,
    minimum=0.0,
    minimum_exclusive=True,
    entry_boundary=True,
)

# --- End of day (section 505-511) -------------------------------------------
EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE = _env_int(
    "EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE", 60, minimum=0
)

# --- Breakeven stop (section 632-644) ---------------------------------------
# Account-specific (commission + tax + slippage estimate); the spec marks
# this ``ACCOUNT_SPECIFIC`` rather than giving a default. 15 bps is a
# conservative placeholder covering typical US overseas-brokerage round-trip
# commission -- must be reviewed against the real account's fee schedule
# before any live breakeven stop is placed off of it.
BREAKEVEN_BUFFER_BPS = _env_float(
    "BREAKEVEN_BUFFER_BPS", 15.0, minimum=0.0
)


def is_buyboard_engine_enabled() -> bool:
    """Whether the guarded Kanban entry/position/EOD runtime is available.

    The engine defaults on so the production Buy Board and its protection
    lifecycle do not silently disappear because one deployment variable is
    missing. This is deliberately *not* live-trading authorization.
    ``KIS_LIVE_EXECUTION_MODE=DISABLED``, the shared trading switch, execution
    lease, ownership, reconciliation, market-data, mutation-budget, capital,
    and risk checks remain independent fail-closed broker-boundary fences.
    Setting this flag false is a recovery-only compatibility choice.
    """
    return _env_bool("BUYBOARD_ENGINE_ENABLED", True, fail_closed=True)
