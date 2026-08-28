"""Dynamic ORB execution queue workflow.

This module owns the strategy/workflow state for turning watchlist ORB plans
into one execution queue item per environment and symbol. UI layers should render these objects
and call order services only after user review.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

from src.risk.orb_position import (
    calculate_orb_position_values,
    is_orb_position_plan_valid,
    score_orb_position_recommendation,
    validate_orb_position_values,
)
from src.strategy import MarketSnapshot, PortfolioSnapshot
from src.strategy.orb import ORBStrategy, ORBStrategyConfig, market_local_index

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc_timestamp(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

SUPPORTED_ORB_WINDOWS = ("1m", "5m", "30m")
DEFAULT_ORB_BUFFER_PCT = 0.001
DEFAULT_UPGRADE_MARGIN = 0.0
PRODUCTION_ENVIRONMENT = "PROD"


def _require_production_environment(environment: str = PRODUCTION_ENVIRONMENT) -> str:
    environment_key = str(environment or PRODUCTION_ENVIRONMENT).strip().upper()
    if environment_key != PRODUCTION_ENVIRONMENT:
        raise ValueError("Execution queue supports the PROD environment only")
    return environment_key


def queue_key(symbol: str, environment: str = PRODUCTION_ENVIRONMENT) -> str:
    return f"{_require_production_environment(environment)}:{str(symbol or '').upper()}"


def _split_queue_key(key: str) -> tuple[str, str]:
    raw = str(key or "").upper()
    if ":" in raw:
        environment, symbol = raw.split(":", 1)
        return environment, symbol
    return "", raw


class ExecutionQueueStatus(str, Enum):
    WATCHING = "WATCHING"
    ORB_FORMING = "ORB_FORMING"
    WAITING_BREAKOUT = "WAITING_BREAKOUT"
    ARMED = "ARMED"
    EXECUTE_READY = "EXECUTE_READY"
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    UNKNOWN_SUBMISSION_STATE = "UNKNOWN_SUBMISSION_STATE"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    # A valid trigger fired but account capital was reserved elsewhere at the
    # moment it fired (buydashboard_to_kanban.md section 9.4, 384-389). Kept
    # as a distinct status rather than folded into ``resolve_queue_status``'s
    # existing candidate-based resolution (section 24's "expose the capital
    # result separately") -- callers that own capital reservation (the new
    # entry-attempt engine) set/clear this explicitly; it is never derived
    # from ORB candidate state alone.
    WAITING_FOR_CAPITAL = "WAITING_FOR_CAPITAL"


UNKNOWN_SUBMISSION_ORDER_STATUS_VALUES = {
    "UNKNOWN",
    "UNKNOWN_SUBMISSION_STATE",
    "AMBIGUOUS",
    "TIMEOUT",
    "NETWORK_ERROR",
}

# An execution-queue row is symbol-scoped, so changing its account detaches
# every account-sized candidate and any order identity stored on the row.  A
# terminal no-fill result can be discarded during a safe reassignment; a
# possibly-live order or confirmed fill must stay bound to its original
# account until broker reconciliation resolves it elsewhere.
_ACCOUNT_REASSIGNMENT_BLOCKING_QUEUE_STATUSES = {
    ExecutionQueueStatus.ORDER_PENDING,
    ExecutionQueueStatus.ORDER_SUBMITTED,
    ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE,
    ExecutionQueueStatus.FILLED,
}
_ACCOUNT_REASSIGNMENT_BLOCKING_ORDER_STATUSES = (
    UNKNOWN_SUBMISSION_ORDER_STATUS_VALUES
    | {
        "PENDING",
        "SUBMITTING",
        "ORDER_PENDING",
        "SUBMITTED",
        "ACCEPTED",
        "WORKING",
        "ORDER_SUBMITTED",
        "CANCEL_REQUESTED",
        "PARTIALLY_FILLED",
        "FILLED",
    }
)
_ACCOUNT_REASSIGNMENT_SAFE_TERMINAL_ORDER_STATUSES = {
    "REJECTED",
    "CANCELLED",
    "CANCELED",
    "EXPIRED",
}

NON_PRE_ENTRY_BUYLIST_STATUSES = {
    "BOUGHT",
    "BUY_PARTIAL",
    "FILLED",
    "SOLD",
    "SELL_SUBMITTED",
    "PARTIAL_EXIT_SUBMITTED",
    "PARTIAL_EXIT_RESERVED",
    "SELL_RESERVED",
}

# --- Handoff-safety status sets (cross-machine main-device takeover) -------
# Purpose-built, non-overlapping categories -- deliberately more precise
# than one broad "in-flight" set, since these statuses mean materially
# different things: a working broker order is not the same risk as a held
# position, which is not the same as an unconfirmed pre-entry trigger.
# Reused by both the existing monitor cycle (buylist/monitoring.py's
# _skip_statuses) and the handoff-reconciliation path so the two can never
# silently drift apart.

# A broker order may or may not have actually reached KIS -- must always be
# reconciled against the broker directly before anything else touches it.
BROKER_UNCERTAIN_STATUSES = {
    ExecutionQueueStatus.ORDER_PENDING.value,
    ExecutionQueueStatus.ORDER_SUBMITTED.value,
    ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE.value,
    "BUY_SUBMITTED",
    "BUY_PARTIAL",
    "SELL_SUBMITTED",
    "PARTIAL_EXIT_SUBMITTED",
    "SELL_RESERVED",
    "PARTIAL_EXIT_RESERVED",
}

# A live position exists and needs stop-loss monitoring.
POSITION_HOLDING_STATUSES = {"BOUGHT"}

# An unconfirmed pre-entry strategy trigger -- no broker order necessarily
# exists yet. A synced EXECUTE_READY here must never be trusted directly;
# it has to be re-evaluated against fresh intraday data and a fresh risk
# approval before an entry can fire on a newly-main device.
PRE_ENTRY_QUEUE_STATUSES = {
    ExecutionQueueStatus.ORB_FORMING.value,
    ExecutionQueueStatus.WAITING_BREAKOUT.value,
    ExecutionQueueStatus.ARMED.value,
    ExecutionQueueStatus.EXECUTE_READY.value,
}

# Everything a device taking over main-device status must reset runtime-only
# pending flags for and reconcile against the broker before resuming
# auto-submission. Deliberately excludes legacy "ACTIVE" -- monitoring
# already refuses legacy ACTIVE auto-buy (see monitoring.py), and the
# handoff path must not resurrect it.
HANDOFF_MONITORABLE_STATUSES = (
    BROKER_UNCERTAIN_STATUSES | POSITION_HOLDING_STATUSES | PRE_ENTRY_QUEUE_STATUSES
)

class OrbCandidateStatus(str, Enum):
    NOT_AVAILABLE = "NOT_AVAILABLE"
    FORMING = "FORMING"
    WAITING_BREAKOUT = "WAITING_BREAKOUT"
    RISK_INVALID = "RISK_INVALID"
    VALID = "VALID"
    EXECUTE_READY = "EXECUTE_READY"
    REJECTED = "REJECTED"


def _enum_from_value(enum_cls, value, default):
    if isinstance(value, enum_cls):
        return value
    raw = str(value.value if isinstance(value, Enum) else value or default.value)
    key = raw.split(".")[-1].upper()
    try:
        return enum_cls(key)
    except ValueError:
        return default


@dataclass
class OrbCandidate:
    symbol: str
    window: str
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    breakout_price: Optional[float] = None
    breakout_trigger: Optional[float] = None
    entry_trigger: Optional[float] = None
    current_price: Optional[float] = None
    # Calendar date of the newest source bar in America/New_York.  Queue
    # refresh time cannot prove that cached minute bars belong to today's
    # session, so execution persists the source identity explicitly.
    source_session_date: Optional[str] = None
    stop_loss: Optional[float] = None
    shares: int = 0
    capital_percent: float = 0.0
    stop_loss_percent: float = 0.0
    stop_adr: Optional[float] = None
    risk_percent: float = 0.0
    score: float = 0.0
    status: OrbCandidateStatus = OrbCandidateStatus.NOT_AVAILABLE
    valid: bool = False
    # Explicit proof that this window is permanently unusable for the current
    # published plan.  Status alone is insufficient: REJECTED also represents
    # duplicate-order protection, while RISK_INVALID can mean that price or
    # sizing equity was temporarily unavailable.  Legacy rows intentionally
    # default to False so they cannot hide a Buy Today card without a fresh
    # evaluation that had known positive sizing equity.
    terminal_rejection: bool = False
    warnings: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrbCandidate":
        payload = dict(data)
        payload["status"] = _enum_from_value(
            OrbCandidateStatus,
            payload.get("status"),
            OrbCandidateStatus.NOT_AVAILABLE,
        )
        payload["warnings"] = list(payload.get("warnings", []))
        return cls(**payload)


@dataclass
class ExecutionQueueItem:
    symbol: str
    environment: str = PRODUCTION_ENVIRONMENT
    # The queue remains keyed by environment+symbol for compatibility, but
    # the account used for risk sizing is persisted so a candidate can never
    # be silently applied to a different account.
    account_no: str = ""
    name: str = ""
    breakout_price: Optional[float] = None
    current_price: Optional[float] = None
    candidates: Dict[str, OrbCandidate] = field(default_factory=dict)
    selected_window: Optional[str] = None
    selected_candidate: Optional[OrbCandidate] = None
    status: ExecutionQueueStatus = ExecutionQueueStatus.WATCHING
    locked: bool = False
    locked_reason: Optional[str] = None
    manual_window_lock: bool = False
    order_status: Optional[str] = None
    order_id: Optional[str] = None
    last_updated: datetime = field(default_factory=_utc_now)
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.environment = _require_production_environment(self.environment)
        self.account_no = str(self.account_no or "").strip()
        self.last_updated = _parse_utc_timestamp(self.last_updated)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "environment": self.environment,
            "account_no": self.account_no,
            "name": self.name,
            "breakout_price": self.breakout_price,
            "current_price": self.current_price,
            "candidates": {
                key: candidate.to_dict() for key, candidate in self.candidates.items()
            },
            "selected_window": self.selected_window,
            "selected_candidate": (
                self.selected_candidate.to_dict() if self.selected_candidate else None
            ),
            "status": self.status.value,
            "locked": self.locked,
            "locked_reason": self.locked_reason,
            "manual_window_lock": self.manual_window_lock,
            "order_status": self.order_status,
            "order_id": self.order_id,
            "last_updated": self.last_updated.isoformat(),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionQueueItem":
        candidates = {
            str(key): OrbCandidate.from_dict(value)
            for key, value in dict(data.get("candidates", {})).items()
        }
        selected_raw = data.get("selected_candidate")
        selected = (
            OrbCandidate.from_dict(selected_raw)
            if isinstance(selected_raw, dict)
            else None
        )
        last_updated_raw = data.get("last_updated")
        try:
            last_updated = (
                _parse_utc_timestamp(last_updated_raw)
                if last_updated_raw
                else _utc_now()
            )
        except ValueError:
            last_updated = _utc_now()
        return cls(
            symbol=str(data.get("symbol", "")).upper(),
            environment=str(data.get("environment") or PRODUCTION_ENVIRONMENT).upper(),
            account_no=str(data.get("account_no") or "").strip(),
            name=str(data.get("name", "")),
            breakout_price=_optional_float(data.get("breakout_price")),
            current_price=_optional_float(data.get("current_price")),
            candidates=candidates,
            selected_window=data.get("selected_window"),
            selected_candidate=selected,
            status=_enum_from_value(
                ExecutionQueueStatus,
                data.get("status"),
                ExecutionQueueStatus.WATCHING,
            ),
            locked=bool(data.get("locked", False)),
            locked_reason=data.get("locked_reason"),
            manual_window_lock=bool(data.get("manual_window_lock", False)),
            order_status=data.get("order_status"),
            order_id=data.get("order_id"),
            last_updated=last_updated,
            warnings=list(data.get("warnings", [])),
        )


@dataclass
class QueueDisplayState:
    symbol: str
    name: str
    display_status: str
    entry_price: float = 0.0
    breakout_price: Optional[float] = None
    stop_loss: float = 0.0
    current_price: float = 0.0
    planned_shares: int = 0
    capital_percent: float = 0.0
    risk_percent: float = 0.0
    stop_adr: Optional[float] = None
    selected_window: str = ""
    warnings: List[str] = field(default_factory=list)
    trade_plan: str = ""


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _saved_orb_selection(item: Any) -> tuple[Optional[str], Optional[float], Optional[float]]:
    """Return the durable Watchlist ORB choice, when it is safe to apply.

    A watchlist plan is a user decision, not merely a display preference.  Its
    selected window and risk/buffer inputs must therefore survive a queue
    refresh instead of being replaced by the current auto-ranked candidate.
    """
    raw_plan = getattr(item, "selected_orb_plan", None)
    if not isinstance(raw_plan, dict):
        return None, None, None

    window = str(raw_plan.get("window", "") or "").strip()
    if window not in SUPPORTED_ORB_WINDOWS:
        return None, None, None

    def _finite_number(key: str, *, positive: bool = False) -> Optional[float]:
        try:
            value = float(raw_plan.get(key))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or (positive and value <= 0):
            return None
        return value

    buffer_pct = _finite_number("buffer_pct")
    if buffer_pct is not None and buffer_pct < 0:
        buffer_pct = None
    return window, _finite_number("risk_percent", positive=True), buffer_pct


def _status_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").split(".")[-1].upper()


def is_pre_entry_execution_queue_item(item: Any) -> bool:
    """Return True for buylist rows whose pre-entry state is queue-backed."""
    if item is None:
        return False
    status = _status_text(
        getattr(item, "monitoring_status", None) or getattr(item, "status", "")
    )
    if status in NON_PRE_ENTRY_BUYLIST_STATUSES:
        return False
    method = str(getattr(item, "breakout_method", "") or "").lower()
    return method.startswith("execution_queue")


def build_queue_display_state(
    queue_item: ExecutionQueueItem,
    buylist_item: Optional[Any] = None,
) -> QueueDisplayState:
    """Project queue state into display-only values for pre-entry dashboard rows."""
    candidate = getattr(queue_item, "selected_candidate", None)
    if candidate is None:
        selected_window = str(getattr(queue_item, "selected_window", "") or "")
        if selected_window:
            candidate = getattr(queue_item, "candidates", {}).get(selected_window)
    if candidate is None:
        _display_priority = {
            OrbCandidateStatus.WAITING_BREAKOUT: 0,
            OrbCandidateStatus.RISK_INVALID: 1,
            OrbCandidateStatus.REJECTED: 2,
        }
        _displayable = [
            c
            for c in getattr(queue_item, "candidates", {}).values()
            if c.status in _display_priority
        ]
        if _displayable:
            candidate = min(
                _displayable, key=lambda c: (_display_priority[c.status], -c.score)
            )

    symbol = str(
        getattr(queue_item, "symbol", "") or getattr(buylist_item, "symbol", "") or ""
    ).upper()
    name = str(
        getattr(queue_item, "name", "") or getattr(buylist_item, "name", "") or symbol
    )
    selected_window = str(
        getattr(candidate, "window", "")
        or getattr(queue_item, "selected_window", "")
        or ""
    )

    entry_price = (
        _optional_float(getattr(candidate, "entry_trigger", None))
        or _optional_float(getattr(candidate, "orb_high", None))
        or _optional_float(getattr(buylist_item, "entry_price", None))
        or 0.0
    )
    breakout_price = (
        _optional_float(getattr(candidate, "breakout_price", None))
        or _optional_float(getattr(queue_item, "breakout_price", None))
        or _optional_float(getattr(buylist_item, "breakout_price", None))
    )
    stop_loss = (
        _optional_float(getattr(candidate, "stop_loss", None))
        or _optional_float(getattr(buylist_item, "stop_loss", None))
        or 0.0
    )
    current_price = (
        _optional_float(getattr(queue_item, "current_price", None))
        or _optional_float(getattr(candidate, "current_price", None))
        or _optional_float(getattr(buylist_item, "current_price", None))
        or 0.0
    )
    planned_shares = int(getattr(candidate, "shares", None) or 0)
    capital_percent = (
        _optional_float(getattr(candidate, "capital_percent", None))
        or _optional_float(getattr(buylist_item, "position_percent", None))
        or 0.0
    )
    stop_adr = _optional_float(getattr(candidate, "stop_adr", None)) or _optional_float(
        getattr(buylist_item, "stop_adr", None)
    )

    warnings: List[str] = list(getattr(queue_item, "warnings", []) or [])
    if candidate is not None:
        warnings.extend(list(getattr(candidate, "warnings", []) or []))
        reason = str(getattr(candidate, "reason", "") or "")
        if reason and not bool(getattr(candidate, "valid", False)):
            warnings.append(reason)
    elif getattr(queue_item, "candidates", None):
        for window, cand in queue_item.candidates.items():
            reason = str(getattr(cand, "reason", "") or "")
            if reason:
                warnings.append(f"{window}: {reason}")
    display_status = _status_text(getattr(queue_item, "status", ""))
    if display_status == ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE.value:
        warnings.insert(0, "UNKNOWN SUBMISSION - RECONCILE BEFORE RETRY")
    warnings = list(dict.fromkeys(warnings))

    trade_plan = str(getattr(buylist_item, "trade_plan", "") or "")
    if selected_window and entry_price > 0 and planned_shares > 0:
        trade_plan = f"ORB {selected_window}: buy {planned_shares} @ {entry_price:.2f}"

    risk_percent = (
        float(getattr(candidate, "risk_percent", 0.0) or 0.0) * 100.0
        if candidate
        else 0.0
    )

    return QueueDisplayState(
        symbol=symbol,
        name=name,
        display_status=display_status,
        entry_price=entry_price,
        breakout_price=breakout_price,
        stop_loss=stop_loss,
        current_price=current_price,
        planned_shares=planned_shares,
        capital_percent=capital_percent,
        risk_percent=risk_percent,
        stop_adr=stop_adr,
        selected_window=selected_window,
        warnings=warnings,
        trade_plan=trade_plan,
    )


def _candidate_unavailable(
    symbol: str,
    window: str,
    status: OrbCandidateStatus,
    reason: str,
    *,
    source_session_date: Optional[str] = None,
) -> OrbCandidate:
    return OrbCandidate(
        symbol=symbol.upper(),
        window=window,
        source_session_date=source_session_date,
        status=status,
        valid=False,
        warnings=[reason],
        reason=reason,
    )


def _has_known_positive_sizing_equity(account_size: Any) -> bool:
    try:
        equity = float(account_size)
    except (TypeError, ValueError):
        return False
    return math.isfinite(equity) and equity > 0


def _intraday_source_session_date(intraday: pd.DataFrame) -> Optional[str]:
    """Return the newest bar's U.S.-market session date.

    Naive KIS/cache indexes are normalized by the same compatibility helper
    used by the ORB strategy itself.  If an index cannot be trusted, the
    candidate remains unlabelled and runtime execution fails closed until a
    refresh provides explicit current-session provenance.
    """

    try:
        local_index = market_local_index(intraday.sort_index().index)
        if local_index is None or local_index.empty:
            return None
        return local_index[-1].date().isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def _missing_orb_range_state(
    intraday: pd.DataFrame,
    window: str,
) -> tuple[OrbCandidateStatus, str]:
    """Explain why a supported ORB range could not be calculated.

    A completed window with later current-session bars but no 09:30 bar is a
    data failure, not a window that is still forming.  Keeping those cases
    separate prevents a missing opening bar from leaving the 1m plan in
    ``FORMING`` for the rest of the session.
    """

    window_minutes = {"1m": 1, "5m": 5, "30m": 30}.get(window)
    if window_minutes is None:
        return OrbCandidateStatus.NOT_AVAILABLE, f"unsupported ORB window {window}"
    if "High" not in intraday.columns or "Low" not in intraday.columns:
        return (
            OrbCandidateStatus.NOT_AVAILABLE,
            f"{window} ORB high/low market data is unavailable",
        )

    try:
        local_index = market_local_index(intraday.sort_index().index)
        if local_index is None or local_index.empty:
            raise ValueError("invalid intraday timestamp index")
        session_start = local_index[-1].normalize() + pd.Timedelta(
            hours=9, minutes=30
        )
        session_index = local_index[
            local_index.normalize() == session_start.normalize()
        ]
        window_end = session_start + pd.Timedelta(minutes=window_minutes)
    except (AttributeError, TypeError, ValueError):
        return OrbCandidateStatus.NOT_AVAILABLE, "intraday timestamps are invalid"

    if session_index.empty or session_index[-1] < window_end:
        return OrbCandidateStatus.FORMING, "ORB window has not completed"
    if not (session_index == session_start).any():
        return (
            OrbCandidateStatus.NOT_AVAILABLE,
            f"09:30 opening bar is unavailable for the completed {window} ORB window",
        )
    return (
        OrbCandidateStatus.NOT_AVAILABLE,
        f"completed {window} ORB window contains invalid high/low market data",
    )


def calculate_position_values(
    account_size: float,
    risk_percent: float,
    entry_price: float,
    stop_price: float,
    adr_percent: Optional[float] = None,
) -> Dict[str, Any]:
    sizing = calculate_orb_position_values(
        account_size=account_size,
        risk_percent=risk_percent,
        entry_price=entry_price,
        stop_price=stop_price,
        adr_percent=adr_percent,
    )
    return {
        "shares": int(sizing["shares"]),
        "investment": float(sizing["investment"]),
        "capital_percent": float(sizing["capital_percent"]),
        "stop_loss_percent": float(sizing["stop_loss_percent"]),
        "sl_adr": sizing["sl_adr"],
        "risk_per_share": float(sizing["risk_per_share"]),
    }


def validate_position_values(
    sizing: Dict[str, Any], adr_percent: Optional[float]
) -> List[str]:
    return validate_orb_position_values(sizing, adr_percent)


def score_orb_candidate(sizing: Dict[str, Any], risk_percent: float) -> float:
    return score_orb_position_recommendation(sizing, risk_percent)


def build_orb_candidate(
    *,
    symbol: str,
    window: str,
    intraday: pd.DataFrame,
    breakout_price: Optional[float],
    current_price: Optional[float],
    account_size: float,
    risk_percent: float,
    adr_percent: Optional[float] = None,
    stop_loss: Optional[float] = None,
    buffer_pct: float = DEFAULT_ORB_BUFFER_PCT,
    duplicate_pending_order: bool = False,
    lock_risk_percent: bool = False,
) -> OrbCandidate:
    symbol = str(symbol or "").upper()
    has_sizing_equity = _has_known_positive_sizing_equity(account_size)
    if window not in SUPPORTED_ORB_WINDOWS:
        return _candidate_unavailable(
            symbol,
            window,
            OrbCandidateStatus.NOT_AVAILABLE,
            f"unsupported ORB window {window}",
        )
    if intraday is None or intraday.empty:
        return _candidate_unavailable(
            symbol, window, OrbCandidateStatus.NOT_AVAILABLE, "intraday data missing"
        )

    source_session_date = _intraday_source_session_date(intraday)

    breakout = _optional_float(breakout_price)
    price = _optional_float(current_price)
    strategy_evaluation = ORBStrategy(
        ORBStrategyConfig(window=window, buffer_pct=buffer_pct)
    ).evaluate(
        MarketSnapshot(
            symbol=symbol,
            current_price=price,
            bars=intraday,
            metadata={"breakout_price": breakout},
        ),
        PortfolioSnapshot(equity=account_size),
    )
    orb_range = strategy_evaluation.orb_range
    if orb_range is None:
        status, reason = _missing_orb_range_state(intraday, window)
        return _candidate_unavailable(
            symbol,
            window,
            status,
            reason,
            source_session_date=source_session_date,
        )

    orb_high = float(orb_range.high)
    orb_low = float(orb_range.low)
    candidate_stop = _optional_float(stop_loss) or orb_low
    warnings: List[str] = []

    if breakout is None or breakout <= 0:
        warnings.append("Manual breakout price is required")
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            breakout_trigger=None,
            entry_trigger=orb_high,
            current_price=price,
            source_session_date=source_session_date,
            stop_loss=candidate_stop,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            warnings=warnings,
            reason=warnings[0],
        )

    if duplicate_pending_order:
        warnings.append("Duplicate pending/submitted order exists for symbol")
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            current_price=price,
            source_session_date=source_session_date,
            stop_loss=candidate_stop,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            warnings=warnings,
            reason=warnings[0],
        )

    if price is None or price <= 0:
        warnings.append("Current price is unavailable")

    entry_signal = strategy_evaluation.entry
    if entry_signal is None:
        return _candidate_unavailable(
            symbol,
            window,
            OrbCandidateStatus.FORMING,
            "ORB window has not completed",
            source_session_date=source_session_date,
        )
    breakout_trigger = float(entry_signal.breakout_trigger)
    entry_trigger = float(entry_signal.entry_trigger)

    if entry_signal.signal == "orb_high_below_breakout_trigger":
        reason = (
            f"ORB high {orb_high:.2f} has not cleared breakout trigger "
            f"{breakout_trigger:.2f}"
        )
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            breakout_trigger=breakout_trigger,
            entry_trigger=entry_trigger,
            current_price=price,
            source_session_date=source_session_date,
            stop_loss=candidate_stop,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            terminal_rejection=(
                has_sizing_equity and price is not None and price > 0
            ),
            warnings=[reason],
            reason=reason,
        )

    if candidate_stop <= 0 or candidate_stop >= entry_trigger:
        warnings.append("Stop loss must be below entry trigger")

    # Auto-select the best valid risk% (same cases as watchlist scoreboard),
    # so execution queue sizing matches what the watchlist displays.
    _risk_cases = (
        [risk_percent]
        if lock_risk_percent
        else sorted(
            {
                0.0025,
                0.005,
                0.0075,
                0.01,
                0.0125,
                0.015,
                0.0175,
                0.02,
                risk_percent,
            }
        )
    )
    _best_risk = risk_percent
    _best_sizing: Optional[Dict[str, Any]] = None
    _best_score = -1.0
    if not warnings:
        for _rc in _risk_cases:
            _s = calculate_position_values(
                account_size=account_size,
                risk_percent=_rc,
                entry_price=entry_trigger,
                stop_price=candidate_stop,
                adr_percent=adr_percent,
            )
            if is_orb_position_plan_valid(_s, adr_percent):
                _sc = score_orb_candidate(_s, _rc)
                if _sc > _best_score:
                    _best_score = _sc
                    _best_sizing = _s
                    _best_risk = _rc

    sizing = (
        _best_sizing
        if _best_sizing is not None
        else calculate_position_values(
            account_size=account_size,
            risk_percent=risk_percent,
            entry_price=entry_trigger,
            stop_price=candidate_stop,
            adr_percent=adr_percent,
        )
    )
    risk_percent = _best_risk
    warnings.extend(validate_position_values(sizing, adr_percent))
    score = score_orb_candidate(sizing, risk_percent)

    if warnings:
        terminal_rejection = (
            has_sizing_equity
            and price is not None
            and price > 0
        )
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            breakout_trigger=breakout_trigger,
            entry_trigger=entry_trigger,
            current_price=price,
            source_session_date=source_session_date,
            stop_loss=candidate_stop,
            shares=int(sizing.get("shares", 0) or 0),
            capital_percent=float(sizing.get("capital_percent", 0.0) or 0.0),
            stop_loss_percent=float(sizing.get("stop_loss_percent", 0.0) or 0.0),
            stop_adr=sizing.get("sl_adr"),
            risk_percent=risk_percent,
            score=score,
            status=OrbCandidateStatus.RISK_INVALID,
            valid=False,
            terminal_rejection=terminal_rejection,
            warnings=warnings,
            reason="; ".join(warnings),
        )

    # Below ORB high, the strategy intentionally has no actionable Signal:
    # the queue now uses the structurally valid plan to place a resting
    # limit at that high.  Once price is already above the high, however,
    # allow_entry and the emitted Signal must agree; fail closed if an
    # internal strategy fault produces only half of that decision.
    if entry_signal.allow_entry and strategy_evaluation.signal is None:
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            breakout_trigger=breakout_trigger,
            entry_trigger=entry_trigger,
            current_price=price,
            source_session_date=source_session_date,
            stop_loss=candidate_stop,
            shares=int(sizing["shares"]),
            capital_percent=float(sizing["capital_percent"]),
            stop_loss_percent=float(sizing["stop_loss_percent"]),
            stop_adr=sizing.get("sl_adr"),
            risk_percent=risk_percent,
            score=score,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            warnings=["ORB strategy did not emit an entry signal"],
            reason="ORB strategy did not emit an entry signal",
        )

    return OrbCandidate(
        symbol=symbol,
        window=window,
        orb_high=orb_high,
        orb_low=orb_low,
        breakout_price=breakout,
        breakout_trigger=breakout_trigger,
        entry_trigger=entry_trigger,
        current_price=price,
        source_session_date=source_session_date,
        stop_loss=candidate_stop,
        shares=int(sizing["shares"]),
        capital_percent=float(sizing["capital_percent"]),
        stop_loss_percent=float(sizing["stop_loss_percent"]),
        stop_adr=sizing.get("sl_adr"),
        risk_percent=risk_percent,
        score=score,
        status=OrbCandidateStatus.EXECUTE_READY,
        valid=True,
        warnings=[],
        reason="Ready to place resting limit at ORB high",
    )


def select_best_orb_candidate(
    candidates: Dict[str, OrbCandidate],
    current_selected_window: Optional[str],
    locked: bool,
    upgrade_margin: float = DEFAULT_UPGRADE_MARGIN,
) -> Optional[OrbCandidate]:
    if locked:
        return (
            candidates.get(current_selected_window or "")
            if current_selected_window
            else None
        )

    valid_candidates = [
        candidate for candidate in candidates.values() if candidate.valid
    ]
    if not valid_candidates:
        return None

    best_candidate = max(valid_candidates, key=lambda candidate: candidate.score)
    if not current_selected_window:
        return best_candidate

    current_candidate = candidates.get(current_selected_window)
    if current_candidate is None or not current_candidate.valid:
        return best_candidate

    if (
        best_candidate.window != current_candidate.window
        and best_candidate.score > current_candidate.score
        and best_candidate.score
        >= current_candidate.score + max(0.0, float(upgrade_margin or 0.0))
    ):
        return best_candidate
    return current_candidate


def resolve_queue_status(
    candidates: Dict[str, OrbCandidate],
    selected_candidate: Optional[OrbCandidate],
    *,
    locked: bool = False,
    order_status: Optional[str] = None,
) -> ExecutionQueueStatus:
    normalized_order_status = str(order_status or "").upper()
    if locked:
        if normalized_order_status in {"FILLED", "PARTIALLY_FILLED"}:
            return ExecutionQueueStatus.FILLED
        if normalized_order_status in UNKNOWN_SUBMISSION_ORDER_STATUS_VALUES:
            return ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE
        if normalized_order_status in {
            "SUBMITTED",
            "ACCEPTED",
            "WORKING",
            "ORDER_SUBMITTED",
            "CANCEL_REQUESTED",
        }:
            return ExecutionQueueStatus.ORDER_SUBMITTED
        if normalized_order_status in {"PENDING", "SUBMITTING", "ORDER_PENDING"}:
            return ExecutionQueueStatus.ORDER_PENDING
    if not candidates:
        return ExecutionQueueStatus.WATCHING
    if selected_candidate is not None and selected_candidate.valid:
        return ExecutionQueueStatus.EXECUTE_READY

    statuses = {candidate.status for candidate in candidates.values()}
    # Only block on ORB_FORMING when non-1m windows are still forming.
    # The 1m window uses a single opening bar and can lag due to data staleness;
    # if 5m/30m have already progressed, treat the 1m FORMING as stale data.
    forming_windows = {
        w for w, c in candidates.items() if c.status == OrbCandidateStatus.FORMING
    }
    if forming_windows and forming_windows != {"1m"}:
        return ExecutionQueueStatus.ORB_FORMING
    if any(status == OrbCandidateStatus.WAITING_BREAKOUT for status in statuses):
        return ExecutionQueueStatus.ARMED
    if statuses and all(
        status in {OrbCandidateStatus.REJECTED, OrbCandidateStatus.RISK_INVALID}
        for status in statuses
    ):
        return ExecutionQueueStatus.REJECTED
    return ExecutionQueueStatus.WATCHING


class ExecutionQueueManager:
    """Stateful manager for one execution queue row per environment and symbol."""

    def __init__(self, upgrade_margin: float = DEFAULT_UPGRADE_MARGIN) -> None:
        self.upgrade_margin = upgrade_margin
        self.items: Dict[str, ExecutionQueueItem] = {}

    def get_item(
        self,
        symbol: str,
        environment: str = PRODUCTION_ENVIRONMENT,
        *,
        legacy_fallback: bool = True,
    ) -> Optional[ExecutionQueueItem]:
        key = queue_key(symbol, environment)
        return self.items.get(key)

    @staticmethod
    def _account_reassignment_block_reason(
        item: ExecutionQueueItem,
    ) -> Optional[str]:
        order_status = str(item.order_status or "").strip().upper()
        if item.status in _ACCOUNT_REASSIGNMENT_BLOCKING_QUEUE_STATUSES:
            return item.status.value
        if order_status in _ACCOUNT_REASSIGNMENT_BLOCKING_ORDER_STATUSES:
            return order_status
        if (
            item.order_id
            and order_status
            not in _ACCOUNT_REASSIGNMENT_SAFE_TERMINAL_ORDER_STATUSES
        ):
            return order_status or "unresolved order identity"
        return None

    @staticmethod
    def _reset_for_account_reassignment(item: ExecutionQueueItem) -> None:
        """Discard facts that were computed or locked for another account."""

        item.candidates = {}
        item.selected_window = None
        item.selected_candidate = None
        item.status = ExecutionQueueStatus.WATCHING
        item.locked = False
        item.locked_reason = None
        item.manual_window_lock = False
        item.order_status = None
        item.order_id = None
        item.warnings = []

    def upsert_item(
        self,
        *,
        symbol: str,
        environment: str = PRODUCTION_ENVIRONMENT,
        account_no: Optional[str] = None,
        name: str = "",
        breakout_price: Optional[float] = None,
        current_price: Optional[float] = None,
        candidates: Optional[Dict[str, OrbCandidate]] = None,
        warnings: Optional[Iterable[str]] = None,
    ) -> ExecutionQueueItem:
        symbol_key = str(symbol or "").upper()
        environment_key = _require_production_environment(environment)
        item_key = queue_key(symbol_key, environment_key)
        existing = self.items.get(item_key)
        if existing is None:
            existing = ExecutionQueueItem(
                symbol=symbol_key,
                environment=environment_key,
                account_no=str(account_no or "").strip(),
                name=name,
            )
            self.items[item_key] = existing

        existing.symbol = symbol_key
        existing.environment = environment_key
        if account_no is not None:
            requested_account = str(account_no or "").strip()
            if requested_account != existing.account_no:
                blocked_by = self._account_reassignment_block_reason(existing)
                if blocked_by:
                    raise ValueError(
                        f"Cannot reassign {symbol_key} execution queue from "
                        f"account {existing.account_no or '<unassigned>'} to "
                        f"{requested_account or '<unassigned>'}: "
                        f"{blocked_by} requires broker reconciliation"
                    )
                self._reset_for_account_reassignment(existing)
                existing.account_no = requested_account
        existing.name = name or existing.name
        existing.breakout_price = breakout_price
        existing.current_price = current_price
        if candidates is not None:
            existing.candidates = {
                key: value
                for key, value in candidates.items()
                if key in SUPPORTED_ORB_WINDOWS
            }
        existing.warnings = list(warnings or [])
        existing.last_updated = _utc_now()

        if existing.manual_window_lock and existing.selected_window:
            selected = (
                existing.candidates.get(existing.selected_window)
                or existing.selected_candidate
            )
            existing.selected_candidate = selected
        elif existing.locked and existing.selected_candidate is not None:
            selected = existing.selected_candidate
        else:
            selected = select_best_orb_candidate(
                existing.candidates,
                existing.selected_window,
                existing.locked,
                upgrade_margin=self.upgrade_margin,
            )
            existing.selected_candidate = selected
            existing.selected_window = (
                selected.window if selected else existing.selected_window
            )

        existing.status = resolve_queue_status(
            existing.candidates,
            selected,
            locked=existing.locked,
            order_status=existing.order_status,
        )
        return existing

    def build_or_update_from_watchlist_item(
        self,
        item: Any,
        intraday_by_window: Dict[str, pd.DataFrame],
        *,
        current_price: Optional[float],
        account_size: float,
        risk_percent: float,
        environment: str = PRODUCTION_ENVIRONMENT,
        account_no: str = "",
        adr_percent: Optional[float] = None,
        buffer_pct: float = DEFAULT_ORB_BUFFER_PCT,
        duplicate_pending_order: bool = False,
        force_buffer_pct: bool = False,
    ) -> ExecutionQueueItem:
        symbol = str(getattr(item, "symbol", "")).upper()
        previous = self.get_item(symbol, environment)
        account_changed = bool(
            previous is not None
            and str(account_no or "").strip() != previous.account_no
        )
        breakout_price = _optional_float(getattr(item, "breakout_price", None))
        stop_loss = _optional_float(getattr(item, "stop_loss", None))
        selected_window, selected_risk_percent, selected_buffer_pct = (
            _saved_orb_selection(item)
        )
        if account_changed:
            # A legacy/manual selection is symbol-scoped, not account-scoped.
            # Reusing its risk/window lock would immediately undo the safe
            # reset performed by ``upsert_item`` for the new account.
            selected_window = None
            selected_risk_percent = None
            selected_buffer_pct = None
        candidates = {}
        for window in SUPPORTED_ORB_WINDOWS:
            use_saved_selection = window == selected_window
            candidates[window] = build_orb_candidate(
                symbol=symbol,
                window=window,
                intraday=intraday_by_window.get(window, pd.DataFrame()),
                breakout_price=breakout_price,
                current_price=current_price,
                account_size=account_size,
                risk_percent=(
                    selected_risk_percent
                    if use_saved_selection and selected_risk_percent is not None
                    else risk_percent
                ),
                adr_percent=adr_percent,
                stop_loss=stop_loss,
                buffer_pct=(
                    buffer_pct
                    if force_buffer_pct
                    else (
                        selected_buffer_pct
                        if use_saved_selection
                        and selected_buffer_pct is not None
                        else buffer_pct
                    )
                ),
                duplicate_pending_order=duplicate_pending_order,
                lock_risk_percent=(
                    use_saved_selection and selected_risk_percent is not None
                ),
            )
        queue_item = self.upsert_item(
            symbol=symbol,
            environment=environment,
            account_no=account_no,
            name=str(getattr(item, "name", "") or symbol),
            breakout_price=breakout_price,
            current_price=current_price,
            candidates=candidates,
        )
        if selected_window:
            selected_candidate = queue_item.candidates.get(selected_window)
            if selected_candidate is not None:
                queue_item.locked = True
                queue_item.manual_window_lock = True
                if not queue_item.order_status:
                    queue_item.locked_reason = "Watchlist ORB plan selection"
                queue_item.selected_window = selected_window
                queue_item.selected_candidate = selected_candidate
                queue_item.status = resolve_queue_status(
                    queue_item.candidates,
                    selected_candidate,
                    locked=True,
                    order_status=queue_item.order_status,
                )
        return queue_item

    def mark_order_submitted(
        self,
        symbol: str,
        order_id: str = "",
        order_status: str = "SUBMITTED",
        environment: str = PRODUCTION_ENVIRONMENT,
    ) -> None:
        item = self.get_item(symbol, environment)
        if item is None:
            raise KeyError(queue_key(symbol, environment))
        item.locked = True
        item.locked_reason = "Order submitted"
        item.order_status = order_status
        item.order_id = order_id or item.order_id
        item.status = resolve_queue_status(
            item.candidates,
            item.selected_candidate,
            locked=True,
            order_status=order_status,
        )
        item.last_updated = _utc_now()

    def mark_order_failed(
        self,
        symbol: str,
        order_status: str = "REJECTED",
        environment: str = PRODUCTION_ENVIRONMENT,
    ) -> None:
        item = self.get_item(symbol, environment)
        if item is None:
            return
        item.locked = False
        item.locked_reason = None
        item.order_status = order_status
        item.order_id = None
        item.status = resolve_queue_status(item.candidates, item.selected_candidate)
        item.last_updated = _utc_now()

    def mark_order_filled(
        self,
        symbol: str,
        order_id: str = "",
        order_status: str = "FILLED",
        environment: str = PRODUCTION_ENVIRONMENT,
    ) -> None:
        item = self.get_item(symbol, environment)
        if item is None:
            return
        item.locked = True
        item.locked_reason = "Order filled"
        item.order_status = order_status
        item.order_id = order_id or item.order_id
        item.status = ExecutionQueueStatus.FILLED
        item.last_updated = _utc_now()

    def has_pending_or_submitted_order(
        self,
        symbol: str,
        environment: str = PRODUCTION_ENVIRONMENT,
    ) -> bool:
        item = self.get_item(symbol, environment)
        if item is None:
            return False
        return item.status in {
            ExecutionQueueStatus.ORDER_PENDING,
            ExecutionQueueStatus.ORDER_SUBMITTED,
            ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE,
        }

    def clear_unknown_submission_state(
        self,
        symbol: str,
        environment: str = PRODUCTION_ENVIRONMENT,
    ) -> bool:
        item = self.get_item(symbol, environment)
        if item is None or item.status != ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE:
            return False
        item.locked = False
        item.locked_reason = None
        item.order_status = None
        item.order_id = None
        item.status = resolve_queue_status(item.candidates, item.selected_candidate)
        item.last_updated = _utc_now()
        return True

    def values(self) -> List[ExecutionQueueItem]:
        return list(self.items.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upgrade_margin": self.upgrade_margin,
            "items": {
                queue_key(
                    item.symbol or _split_queue_key(key)[1], item.environment
                ): item.to_dict()
                for key, item in self.items.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        *,
        on_rejected: Optional[Callable[[int, Any, Exception], None]] = None,
    ) -> "ExecutionQueueManager":
        try:
            upgrade_margin = float(
                data.get("upgrade_margin", DEFAULT_UPGRADE_MARGIN)
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Rejected execution queue upgrade margin: %s", exc)
            if on_rejected is not None:
                on_rejected(
                    0,
                    {"field": "upgrade_margin", "value": data.get("upgrade_margin")},
                    exc,
                )
            upgrade_margin = DEFAULT_UPGRADE_MARGIN
        manager = cls(upgrade_margin=upgrade_margin)
        raw_items = data.get("items", {})
        if not isinstance(raw_items, dict):
            error = TypeError("items must be an object keyed by environment and symbol")
            logger.warning("Rejected execution queue items container: %s", error)
            if on_rejected is not None:
                on_rejected(0, {"field": "items", "value": raw_items}, error)
            return manager
        for index, (raw_key, item_data) in enumerate(
            raw_items.items()
        ):
            if not isinstance(item_data, dict):
                error = TypeError("record is not an object")
                logger.warning("Rejected execution queue record %r: %s", raw_key, error)
                if on_rejected is not None:
                    on_rejected(index, {"key": raw_key, "value": item_data}, error)
                continue
            key_environment, key_symbol = _split_queue_key(str(raw_key))
            item_environment = (
                str(item_data.get("environment") or key_environment).strip().upper()
            )
            if (
                key_environment != PRODUCTION_ENVIRONMENT
                or item_environment != PRODUCTION_ENVIRONMENT
            ):
                continue
            try:
                item = ExecutionQueueItem.from_dict(item_data)
            except Exception as exc:
                logger.warning("Rejected execution queue record %r: %s", raw_key, exc)
                if on_rejected is not None:
                    on_rejected(index, {"key": raw_key, "value": item_data}, exc)
                continue
            if not item.symbol:
                item.symbol = key_symbol
            item.symbol = str(item.symbol or "").upper()
            item.environment = PRODUCTION_ENVIRONMENT
            manager.items[queue_key(item.symbol, item.environment)] = item
        return manager
