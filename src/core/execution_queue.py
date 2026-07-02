"""Dynamic ORB execution queue workflow.

This module owns the strategy/workflow state for turning watchlist ORB plans
into one execution queue item per symbol. UI layers should render these objects
and call order services only after user review.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from src.core.orb import calculate_orb_range, evaluate_orb_entry_signal
from src.core.position_sizer import PositionSizer


SUPPORTED_ORB_WINDOWS = ("1m", "5m", "30m")
DEFAULT_ORB_BUFFER_PCT = 0.001
DEFAULT_UPGRADE_MARGIN = 5.0
MIN_CAPITAL_PERCENT = 10.0
MAX_CAPITAL_PERCENT = 30.0
MIN_STOP_ADR = 15.0
MAX_STOP_ADR = 66.0


class ExecutionQueueStatus(str, Enum):
    WATCHING = "WATCHING"
    ORB_FORMING = "ORB_FORMING"
    WAITING_BREAKOUT = "WAITING_BREAKOUT"
    ARMED = "ARMED"
    EXECUTE_READY = "EXECUTE_READY"
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


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
    stop_loss: Optional[float] = None
    shares: int = 0
    capital_percent: float = 0.0
    stop_loss_percent: float = 0.0
    stop_adr: Optional[float] = None
    risk_percent: float = 0.0
    score: float = 0.0
    status: OrbCandidateStatus = OrbCandidateStatus.NOT_AVAILABLE
    valid: bool = False
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
    name: str = ""
    breakout_price: Optional[float] = None
    current_price: Optional[float] = None
    candidates: Dict[str, OrbCandidate] = field(default_factory=dict)
    selected_window: Optional[str] = None
    selected_candidate: Optional[OrbCandidate] = None
    status: ExecutionQueueStatus = ExecutionQueueStatus.WATCHING
    locked: bool = False
    locked_reason: Optional[str] = None
    order_status: Optional[str] = None
    order_id: Optional[str] = None
    last_updated: datetime = field(default_factory=datetime.now)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "breakout_price": self.breakout_price,
            "current_price": self.current_price,
            "candidates": {key: candidate.to_dict() for key, candidate in self.candidates.items()},
            "selected_window": self.selected_window,
            "selected_candidate": self.selected_candidate.to_dict() if self.selected_candidate else None,
            "status": self.status.value,
            "locked": self.locked,
            "locked_reason": self.locked_reason,
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
        selected = OrbCandidate.from_dict(selected_raw) if isinstance(selected_raw, dict) else None
        last_updated_raw = data.get("last_updated")
        try:
            last_updated = datetime.fromisoformat(last_updated_raw) if last_updated_raw else datetime.now()
        except ValueError:
            last_updated = datetime.now()
        return cls(
            symbol=str(data.get("symbol", "")).upper(),
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
            order_status=data.get("order_status"),
            order_id=data.get("order_id"),
            last_updated=last_updated,
            warnings=list(data.get("warnings", [])),
        )


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_unavailable(symbol: str, window: str, status: OrbCandidateStatus, reason: str) -> OrbCandidate:
    return OrbCandidate(
        symbol=symbol.upper(),
        window=window,
        status=status,
        valid=False,
        warnings=[reason],
        reason=reason,
    )


def calculate_position_values(
    account_size: float,
    risk_percent: float,
    entry_price: float,
    stop_price: float,
    adr_percent: Optional[float] = None,
) -> Dict[str, Any]:
    if account_size <= 0 or risk_percent <= 0 or entry_price <= 0 or stop_price <= 0:
        return {
            "shares": 0,
            "investment": 0.0,
            "capital_percent": 0.0,
            "stop_loss_percent": 0.0,
            "sl_adr": None,
            "risk_per_share": 0.0,
        }

    risk_per_share = max(0.0, entry_price - stop_price)
    if risk_per_share <= 0:
        return {
            "shares": 0,
            "investment": 0.0,
            "capital_percent": 0.0,
            "stop_loss_percent": 0.0,
            "sl_adr": None,
            "risk_per_share": 0.0,
        }

    sizer = PositionSizer(account_size=account_size, max_risk_per_trade=risk_percent)
    sizing = sizer.size_risk_based(entry_price=entry_price, stop_loss_price=stop_price, risk_percent=risk_percent)
    stop_loss_percent = risk_per_share / entry_price * 100.0
    sl_adr = stop_loss_percent / adr_percent * 100.0 if adr_percent and adr_percent > 0 else None
    return {
        "shares": int(sizing.shares),
        "investment": float(sizing.dollar_amount),
        "capital_percent": float(sizing.percent_of_account * 100.0),
        "stop_loss_percent": float(stop_loss_percent),
        "sl_adr": sl_adr,
        "risk_per_share": float(risk_per_share),
    }


def validate_position_values(sizing: Dict[str, Any], adr_percent: Optional[float]) -> List[str]:
    warnings: List[str] = []
    shares = int(sizing.get("shares", 0) or 0)
    capital_percent = float(sizing.get("capital_percent", 0.0) or 0.0)
    stop_loss_percent = float(sizing.get("stop_loss_percent", 0.0) or 0.0)
    stop_adr = sizing.get("sl_adr")

    if shares < 1:
        warnings.append("Position size calculation resulted in 0 shares")
    if capital_percent < MIN_CAPITAL_PERCENT:
        warnings.append(f"Capital allocation ({capital_percent:.2f}%) is below {MIN_CAPITAL_PERCENT:.0f}%")
    if capital_percent >= MAX_CAPITAL_PERCENT:
        warnings.append(f"Capital allocation ({capital_percent:.2f}%) exceeds {MAX_CAPITAL_PERCENT:.0f}%")
    if adr_percent is not None and adr_percent > 0 and stop_loss_percent >= adr_percent:
        warnings.append(f"Stop loss % ({stop_loss_percent:.2f}%) is wider than ADR ({adr_percent:.2f}%)")
    if stop_adr is not None and (float(stop_adr) < MIN_STOP_ADR or float(stop_adr) > MAX_STOP_ADR):
        warnings.append(f"Stop/ADR ({float(stop_adr):.0f}%) is outside {MIN_STOP_ADR:.0f}-{MAX_STOP_ADR:.0f}%")
    return warnings


def score_orb_candidate(sizing: Dict[str, Any], risk_percent: float) -> float:
    stop_adr = sizing.get("sl_adr")
    if stop_adr is None:
        return 0.0
    capital_percent = float(sizing.get("capital_percent", 0.0) or 0.0)
    stop_adr_score = max(0.0, 100.0 - abs(float(stop_adr) - 65.0) * 3.0)
    capital_score = max(0.0, 100.0 - abs(capital_percent - 17.5) * 4.0)
    risk_score = max(0.0, 100.0 - float(risk_percent) * 100.0 * 25.0)
    return round((stop_adr_score * 0.45) + (capital_score * 0.40) + (risk_score * 0.15), 1)


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
) -> OrbCandidate:
    symbol = str(symbol or "").upper()
    if window not in SUPPORTED_ORB_WINDOWS:
        return _candidate_unavailable(symbol, window, OrbCandidateStatus.NOT_AVAILABLE, f"unsupported ORB window {window}")
    if intraday is None or intraday.empty:
        return _candidate_unavailable(symbol, window, OrbCandidateStatus.NOT_AVAILABLE, "intraday data missing")

    orb_range = calculate_orb_range(symbol, intraday, window)
    if orb_range is None:
        return _candidate_unavailable(symbol, window, OrbCandidateStatus.FORMING, "ORB window has not completed")

    orb_high = float(orb_range.high)
    orb_low = float(orb_range.low)
    breakout = _optional_float(breakout_price)
    price = _optional_float(current_price)
    candidate_stop = _optional_float(stop_loss) or orb_low
    warnings: List[str] = []

    if duplicate_pending_order:
        warnings.append("Duplicate pending/submitted order exists for symbol")
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            current_price=price,
            stop_loss=candidate_stop,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            warnings=warnings,
            reason=warnings[0],
        )

    if breakout is None or breakout <= 0:
        warnings.append("Manual breakout price is required")
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            current_price=price,
            stop_loss=candidate_stop,
            status=OrbCandidateStatus.REJECTED,
            valid=False,
            warnings=warnings,
            reason=warnings[0],
        )
    if price is None or price <= 0:
        warnings.append("Current price is unavailable")

    entry_signal = evaluate_orb_entry_signal(
        orb_high=orb_high,
        orb_low=orb_low,
        breakout_price=breakout,
        current_price=price or 0.0,
        buffer_pct=buffer_pct,
    )
    breakout_trigger = float(entry_signal.breakout_trigger)
    entry_trigger = float(entry_signal.entry_trigger)

    if candidate_stop <= 0 or candidate_stop >= entry_trigger:
        warnings.append("Stop loss must be below entry trigger")

    sizing = calculate_position_values(
        account_size=account_size,
        risk_percent=risk_percent,
        entry_price=entry_trigger,
        stop_price=candidate_stop,
        adr_percent=adr_percent,
    )
    warnings.extend(validate_position_values(sizing, adr_percent))
    score = score_orb_candidate(sizing, risk_percent)

    if warnings:
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            breakout_trigger=breakout_trigger,
            entry_trigger=entry_trigger,
            current_price=price,
            stop_loss=candidate_stop,
            shares=int(sizing.get("shares", 0) or 0),
            capital_percent=float(sizing.get("capital_percent", 0.0) or 0.0),
            stop_loss_percent=float(sizing.get("stop_loss_percent", 0.0) or 0.0),
            stop_adr=sizing.get("sl_adr"),
            risk_percent=risk_percent,
            score=score,
            status=OrbCandidateStatus.RISK_INVALID,
            valid=False,
            warnings=warnings,
            reason="; ".join(warnings),
        )

    if not entry_signal.allow_entry:
        reason = "Waiting for price to clear entry trigger"
        return OrbCandidate(
            symbol=symbol,
            window=window,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_price=breakout,
            breakout_trigger=breakout_trigger,
            entry_trigger=entry_trigger,
            current_price=price,
            stop_loss=candidate_stop,
            shares=int(sizing["shares"]),
            capital_percent=float(sizing["capital_percent"]),
            stop_loss_percent=float(sizing["stop_loss_percent"]),
            stop_adr=sizing.get("sl_adr"),
            risk_percent=risk_percent,
            score=score,
            status=OrbCandidateStatus.WAITING_BREAKOUT,
            valid=False,
            warnings=[],
            reason=reason,
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
        reason="Ready to execute",
    )


def select_best_orb_candidate(
    candidates: Dict[str, OrbCandidate],
    current_selected_window: Optional[str],
    locked: bool,
    upgrade_margin: float = DEFAULT_UPGRADE_MARGIN,
) -> Optional[OrbCandidate]:
    if locked:
        return candidates.get(current_selected_window or "") if current_selected_window else None

    valid_candidates = [candidate for candidate in candidates.values() if candidate.valid]
    if not valid_candidates:
        return None

    best_candidate = max(valid_candidates, key=lambda candidate: candidate.score)
    if not current_selected_window:
        return best_candidate

    current_candidate = candidates.get(current_selected_window)
    if current_candidate is None or not current_candidate.valid:
        return best_candidate

    if best_candidate.window != current_candidate.window and best_candidate.score >= current_candidate.score + upgrade_margin:
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
        if normalized_order_status in {"SUBMITTED", "ACCEPTED", "WORKING", "ORDER_SUBMITTED"}:
            return ExecutionQueueStatus.ORDER_SUBMITTED
        if normalized_order_status in {"PENDING", "SUBMITTING", "ORDER_PENDING"}:
            return ExecutionQueueStatus.ORDER_PENDING
    if not candidates:
        return ExecutionQueueStatus.WATCHING
    if selected_candidate is not None and selected_candidate.valid:
        return ExecutionQueueStatus.EXECUTE_READY

    statuses = {candidate.status for candidate in candidates.values()}
    if any(status == OrbCandidateStatus.FORMING for status in statuses):
        return ExecutionQueueStatus.ORB_FORMING
    if any(status == OrbCandidateStatus.WAITING_BREAKOUT for status in statuses):
        return ExecutionQueueStatus.ARMED
    if statuses and all(status in {OrbCandidateStatus.REJECTED, OrbCandidateStatus.RISK_INVALID} for status in statuses):
        return ExecutionQueueStatus.REJECTED
    return ExecutionQueueStatus.WATCHING


class ExecutionQueueManager:
    """Stateful manager for one execution queue row per symbol."""

    def __init__(self, upgrade_margin: float = DEFAULT_UPGRADE_MARGIN) -> None:
        self.upgrade_margin = upgrade_margin
        self.items: Dict[str, ExecutionQueueItem] = {}

    def upsert_item(
        self,
        *,
        symbol: str,
        name: str = "",
        breakout_price: Optional[float] = None,
        current_price: Optional[float] = None,
        candidates: Optional[Dict[str, OrbCandidate]] = None,
        warnings: Optional[Iterable[str]] = None,
    ) -> ExecutionQueueItem:
        symbol_key = str(symbol or "").upper()
        existing = self.items.get(symbol_key)
        if existing is None:
            existing = ExecutionQueueItem(symbol=symbol_key, name=name)
            self.items[symbol_key] = existing

        existing.name = name or existing.name
        existing.breakout_price = breakout_price
        existing.current_price = current_price
        if candidates is not None:
            existing.candidates = {key: value for key, value in candidates.items() if key in SUPPORTED_ORB_WINDOWS}
        existing.warnings = list(warnings or [])
        existing.last_updated = datetime.now()

        if existing.locked and existing.selected_candidate is not None:
            selected = existing.selected_candidate
        else:
            selected = select_best_orb_candidate(
                existing.candidates,
                existing.selected_window,
                existing.locked,
                upgrade_margin=self.upgrade_margin,
            )
            existing.selected_candidate = selected
            existing.selected_window = selected.window if selected else existing.selected_window

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
        adr_percent: Optional[float] = None,
        buffer_pct: float = DEFAULT_ORB_BUFFER_PCT,
        duplicate_pending_order: bool = False,
    ) -> ExecutionQueueItem:
        symbol = str(getattr(item, "symbol", "")).upper()
        breakout_price = _optional_float(getattr(item, "breakout_price", None))
        stop_loss = _optional_float(getattr(item, "stop_loss", None))
        candidates = {
            window: build_orb_candidate(
                symbol=symbol,
                window=window,
                intraday=intraday_by_window.get(window, pd.DataFrame()),
                breakout_price=breakout_price,
                current_price=current_price,
                account_size=account_size,
                risk_percent=risk_percent,
                adr_percent=adr_percent,
                stop_loss=stop_loss,
                buffer_pct=buffer_pct,
                duplicate_pending_order=duplicate_pending_order,
            )
            for window in SUPPORTED_ORB_WINDOWS
        }
        return self.upsert_item(
            symbol=symbol,
            name=str(getattr(item, "name", "") or symbol),
            breakout_price=breakout_price,
            current_price=current_price,
            candidates=candidates,
        )

    def mark_order_submitted(self, symbol: str, order_id: str = "", order_status: str = "SUBMITTED") -> None:
        item = self.items[str(symbol or "").upper()]
        item.locked = True
        item.locked_reason = "Order submitted"
        item.order_status = order_status
        item.order_id = order_id or item.order_id
        item.status = resolve_queue_status(item.candidates, item.selected_candidate, locked=True, order_status=order_status)
        item.last_updated = datetime.now()

    def mark_order_failed(self, symbol: str, order_status: str = "REJECTED") -> None:
        item = self.items.get(str(symbol or "").upper())
        if item is None:
            return
        item.locked = False
        item.locked_reason = None
        item.order_status = order_status
        item.order_id = None
        item.status = resolve_queue_status(item.candidates, item.selected_candidate)
        item.last_updated = datetime.now()

    def mark_order_filled(self, symbol: str, order_id: str = "", order_status: str = "FILLED") -> None:
        item = self.items.get(str(symbol or "").upper())
        if item is None:
            return
        item.locked = True
        item.locked_reason = "Order filled"
        item.order_status = order_status
        item.order_id = order_id or item.order_id
        item.status = ExecutionQueueStatus.FILLED
        item.last_updated = datetime.now()

    def has_pending_or_submitted_order(self, symbol: str) -> bool:
        item = self.items.get(str(symbol or "").upper())
        if item is None:
            return False
        return item.status in {ExecutionQueueStatus.ORDER_PENDING, ExecutionQueueStatus.ORDER_SUBMITTED}

    def values(self) -> List[ExecutionQueueItem]:
        return list(self.items.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upgrade_margin": self.upgrade_margin,
            "items": {symbol: item.to_dict() for symbol, item in self.items.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionQueueManager":
        manager = cls(upgrade_margin=float(data.get("upgrade_margin", DEFAULT_UPGRADE_MARGIN)))
        manager.items = {
            str(symbol).upper(): ExecutionQueueItem.from_dict(item_data)
            for symbol, item_data in dict(data.get("items", {})).items()
        }
        return manager
