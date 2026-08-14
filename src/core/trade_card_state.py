"""Trade-card state model for the Kanban Buy Board redesign.

This module owns the single authoritative aggregate for the visible Kanban
lifecycle of one (environment, account_no, symbol) trade card, per
``buydashboard_to_kanban.md`` sections 5-7. It is deliberately additive: the
existing ``BuylistItem``/``ExecutionQueueItem`` models in
:mod:`src.core.watchlist` and :mod:`src.core.execution_queue` are untouched
and continue to drive the legacy Buy Dashboard tab. ``TradeCardState`` rows
are built from those models by :mod:`src.services.trade_card_repository`'s
migration function and rendered by the new Buy Board tab.

Field layout follows the spec's four domains kept deliberately separate
(section 179): visible board state, strategy/entry-runtime state, broker
order/position state, and stop-loss state. ``BoardStatus`` alone determines
where a card is rendered; it must never be inferred from whether an order or
broker position actually exists (section 194).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

PRODUCTION_ENVIRONMENT = "PROD"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any, *, default: Optional[datetime] = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value))
    else:
        return default if default is not None else _utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def _finite_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _enum_from_value(value: Any, enum_cls: type[Enum], default: Enum) -> Enum:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).strip().upper())
    except (TypeError, ValueError):
        return default


class BoardStatus(str, Enum):
    """The visible Kanban column. Section 5.1."""

    WATCHLIST = "WATCHLIST"
    BUYLIST = "BUYLIST"
    BUY_TODAY = "BUY_TODAY"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN_POSITION = "OPEN_POSITION"
    PARTIAL_SELL = "PARTIAL_SELL"
    SELL_ALL = "SELL_ALL"
    CLOSED = "CLOSED"


class EntryRuntimeStatus(str, Enum):
    """Badge/internal substate for a card in BUY_TODAY. Section 3.

    These are display substates, not Kanban columns -- never used to decide
    which column a card renders in.
    """

    ORB_FORMING = "ORB_FORMING"
    WAITING_BREAKOUT = "WAITING_BREAKOUT"
    ARMED = "ARMED"
    EXECUTE_READY = "EXECUTE_READY"
    RISK_INVALID = "RISK_INVALID"
    WAITING_FOR_CAPITAL = "WAITING_FOR_CAPITAL"
    RETRY_COOLDOWN = "RETRY_COOLDOWN"
    ORDER_PENDING = "ORDER_PENDING"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    SESSION_COMPLETE = "SESSION_COMPLETE"


class PositionRuntimeStatus(str, Enum):
    """Section 5.3."""

    NONE = "NONE"
    OPEN = "OPEN"
    ENTRY_COMPLETING = "ENTRY_COMPLETING"
    PARTIAL_EXIT_PENDING = "PARTIAL_EXIT_PENDING"
    EXIT_ALL_REQUIRED = "EXIT_ALL_REQUIRED"
    LIQUIDATING = "LIQUIDATING"
    QUEUED_FOR_OPEN = "QUEUED_FOR_OPEN"
    CLOSED = "CLOSED"
    RECONCILING = "RECONCILING"


class StopType(str, Enum):
    """Section 5.2."""

    ORB_LOW = "ORB_LOW"
    BREAKEVEN = "BREAKEVEN"
    MANUAL_PRICE = "MANUAL_PRICE"


def _require_production_environment(environment: str) -> str:
    environment_key = str(environment or PRODUCTION_ENVIRONMENT).strip().upper()
    if environment_key != PRODUCTION_ENVIRONMENT:
        raise ValueError("TradeCardState supports the PROD environment only")
    return environment_key


@dataclass
class TradeCardState:
    """The authoritative aggregate for one (environment, account_no, symbol)
    card. Section 7, extended with the position/stop/exit fields the rest of
    the spec (sections 5.2, 5.3, 16-18) requires a single card to carry.

    ``UNIQUE(environment, account_no, symbol)`` is enforced by
    :mod:`src.services.trade_card_repository`, not by this dataclass -- this
    class only models one row.
    """

    # Identity
    environment: str
    account_no: str
    symbol: str
    name: str = ""

    # Concurrency
    version: int = 1
    kanban_priority: int = 0

    # Visible lifecycle
    board_status: BoardStatus = BoardStatus.WATCHLIST
    previous_board_status: Optional[BoardStatus] = None
    board_status_updated_at: datetime = field(default_factory=_utc_now)
    session_date: Optional[date] = None

    # Membership metadata
    watchlist_member: bool = False
    buylist_member: bool = False
    return_to_buylist_after_close: bool = False

    # Entry plan
    breakout_price: Optional[float] = None
    selected_orb_window: Optional[str] = None
    buffer_pct: float = 0.001
    risk_percent: float = 1.0
    planned_quantity: int = 0
    target_position_quantity: int = 0

    # Frozen ORB values (persisted at first fill; never recalculated after --
    # section 620)
    entry_orb_window: Optional[str] = None
    entry_orb_high: Optional[float] = None
    entry_orb_low: Optional[float] = None
    entry_trigger: Optional[float] = None
    stop_adr: Optional[float] = None

    # Entry runtime (Buy Today substate badge, section 3)
    entry_runtime_status: Optional[EntryRuntimeStatus] = None
    entry_block_reason: str = ""

    # Position / broker-derived state (section 5.3, 231-233)
    position_runtime_status: PositionRuntimeStatus = PositionRuntimeStatus.NONE
    broker_quantity: int = 0
    orderable_quantity: int = 0
    average_entry_price: float = 0.0
    entry_remaining_target_quantity: int = 0

    # Stop-loss state (section 5.2)
    stop_type: Optional[StopType] = None
    active_stop_price: Optional[float] = None
    stop_quantity: int = 0

    # Exit state (sections 17-18)
    exit_all_required: bool = False
    sell_all_at_market_open: bool = False
    # User-requested partial-sell quantity (section 15), stashed on the card
    # when a RequestPartialSell command moves it into PARTIAL_SELL so the
    # position/exit engine (which owns the actual sell submission) has the
    # user's requested size without the board layer submitting orders itself.
    pending_partial_sell_quantity: int = 0

    # Capital (section 22 correlation, not the reservation record itself)
    capital_reservation_id: str = ""

    # Timestamps
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.environment = _require_production_environment(self.environment)
        self.account_no = str(self.account_no or "").strip()
        self.symbol = str(self.symbol or "").strip().upper()
        if not self.symbol:
            raise ValueError("TradeCardState requires a symbol")
        self.name = str(self.name or "")
        self.version = int(self.version or 1)
        self.kanban_priority = int(self.kanban_priority or 0)
        self.board_status = _enum_from_value(
            self.board_status, BoardStatus, BoardStatus.WATCHLIST
        )
        if self.previous_board_status is not None:
            self.previous_board_status = _enum_from_value(
                self.previous_board_status, BoardStatus, BoardStatus.WATCHLIST
            )
        self.board_status_updated_at = _parse_timestamp(self.board_status_updated_at)
        self.session_date = _parse_optional_date(self.session_date)
        self.watchlist_member = bool(self.watchlist_member)
        self.buylist_member = bool(self.buylist_member)
        self.return_to_buylist_after_close = bool(self.return_to_buylist_after_close)
        self.breakout_price = _finite_float(self.breakout_price)
        self.selected_orb_window = (
            str(self.selected_orb_window).strip() if self.selected_orb_window else None
        )
        self.buffer_pct = float(_finite_float(self.buffer_pct, 0.001))
        self.risk_percent = float(_finite_float(self.risk_percent, 1.0))
        self.planned_quantity = int(self.planned_quantity or 0)
        self.target_position_quantity = int(self.target_position_quantity or 0)
        self.entry_orb_window = (
            str(self.entry_orb_window).strip() if self.entry_orb_window else None
        )
        self.entry_orb_high = _finite_float(self.entry_orb_high)
        self.entry_orb_low = _finite_float(self.entry_orb_low)
        self.entry_trigger = _finite_float(self.entry_trigger)
        self.stop_adr = _finite_float(self.stop_adr)
        if self.entry_runtime_status is not None:
            self.entry_runtime_status = _enum_from_value(
                self.entry_runtime_status,
                EntryRuntimeStatus,
                EntryRuntimeStatus.ORB_FORMING,
            )
        self.entry_block_reason = str(self.entry_block_reason or "")
        self.position_runtime_status = _enum_from_value(
            self.position_runtime_status,
            PositionRuntimeStatus,
            PositionRuntimeStatus.NONE,
        )
        self.broker_quantity = int(self.broker_quantity or 0)
        self.orderable_quantity = int(self.orderable_quantity or 0)
        self.average_entry_price = float(_finite_float(self.average_entry_price, 0.0))
        self.entry_remaining_target_quantity = int(
            self.entry_remaining_target_quantity or 0
        )
        if self.stop_type is not None:
            self.stop_type = _enum_from_value(self.stop_type, StopType, StopType.ORB_LOW)
        self.active_stop_price = _finite_float(self.active_stop_price)
        self.stop_quantity = int(self.stop_quantity or 0)
        self.exit_all_required = bool(self.exit_all_required)
        self.sell_all_at_market_open = bool(self.sell_all_at_market_open)
        self.pending_partial_sell_quantity = int(self.pending_partial_sell_quantity or 0)
        self.capital_reservation_id = str(self.capital_reservation_id or "")
        self.created_at = _parse_timestamp(self.created_at)
        self.updated_at = _parse_timestamp(self.updated_at)
        self.warnings = list(self.warnings or [])

    @property
    def card_key(self) -> str:
        """``(environment, account_no, symbol)`` as a single lookup key."""
        return f"{self.environment}:{self.account_no}:{self.symbol}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "environment": self.environment,
            "account_no": self.account_no,
            "symbol": self.symbol,
            "name": self.name,
            "version": self.version,
            "kanban_priority": self.kanban_priority,
            "board_status": self.board_status.value,
            "previous_board_status": (
                self.previous_board_status.value
                if self.previous_board_status is not None
                else None
            ),
            "board_status_updated_at": self.board_status_updated_at.isoformat(),
            "session_date": (
                self.session_date.isoformat() if self.session_date else None
            ),
            "watchlist_member": self.watchlist_member,
            "buylist_member": self.buylist_member,
            "return_to_buylist_after_close": self.return_to_buylist_after_close,
            "breakout_price": self.breakout_price,
            "selected_orb_window": self.selected_orb_window,
            "buffer_pct": self.buffer_pct,
            "risk_percent": self.risk_percent,
            "planned_quantity": self.planned_quantity,
            "target_position_quantity": self.target_position_quantity,
            "entry_orb_window": self.entry_orb_window,
            "entry_orb_high": self.entry_orb_high,
            "entry_orb_low": self.entry_orb_low,
            "entry_trigger": self.entry_trigger,
            "stop_adr": self.stop_adr,
            "entry_runtime_status": (
                self.entry_runtime_status.value
                if self.entry_runtime_status is not None
                else None
            ),
            "entry_block_reason": self.entry_block_reason,
            "position_runtime_status": self.position_runtime_status.value,
            "broker_quantity": self.broker_quantity,
            "orderable_quantity": self.orderable_quantity,
            "average_entry_price": self.average_entry_price,
            "entry_remaining_target_quantity": self.entry_remaining_target_quantity,
            "stop_type": self.stop_type.value if self.stop_type is not None else None,
            "active_stop_price": self.active_stop_price,
            "stop_quantity": self.stop_quantity,
            "exit_all_required": self.exit_all_required,
            "sell_all_at_market_open": self.sell_all_at_market_open,
            "pending_partial_sell_quantity": self.pending_partial_sell_quantity,
            "capital_reservation_id": self.capital_reservation_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeCardState":
        return cls(
            environment=str(data.get("environment", PRODUCTION_ENVIRONMENT)),
            account_no=str(data.get("account_no", "")),
            symbol=str(data.get("symbol", "")),
            name=str(data.get("name", "")),
            version=int(data.get("version", 1) or 1),
            kanban_priority=int(data.get("kanban_priority", 0) or 0),
            board_status=data.get("board_status", BoardStatus.WATCHLIST),
            previous_board_status=data.get("previous_board_status"),
            board_status_updated_at=data.get("board_status_updated_at"),
            session_date=data.get("session_date"),
            watchlist_member=bool(data.get("watchlist_member", False)),
            buylist_member=bool(data.get("buylist_member", False)),
            return_to_buylist_after_close=bool(
                data.get("return_to_buylist_after_close", False)
            ),
            breakout_price=data.get("breakout_price"),
            selected_orb_window=data.get("selected_orb_window"),
            buffer_pct=data.get("buffer_pct", 0.001),
            risk_percent=data.get("risk_percent", 1.0),
            planned_quantity=int(data.get("planned_quantity", 0) or 0),
            target_position_quantity=int(
                data.get("target_position_quantity", 0) or 0
            ),
            entry_orb_window=data.get("entry_orb_window"),
            entry_orb_high=data.get("entry_orb_high"),
            entry_orb_low=data.get("entry_orb_low"),
            entry_trigger=data.get("entry_trigger"),
            stop_adr=data.get("stop_adr"),
            entry_runtime_status=data.get("entry_runtime_status"),
            entry_block_reason=str(data.get("entry_block_reason", "")),
            position_runtime_status=data.get(
                "position_runtime_status", PositionRuntimeStatus.NONE
            ),
            broker_quantity=int(data.get("broker_quantity", 0) or 0),
            orderable_quantity=int(data.get("orderable_quantity", 0) or 0),
            average_entry_price=data.get("average_entry_price", 0.0),
            entry_remaining_target_quantity=int(
                data.get("entry_remaining_target_quantity", 0) or 0
            ),
            stop_type=data.get("stop_type"),
            active_stop_price=data.get("active_stop_price"),
            stop_quantity=int(data.get("stop_quantity", 0) or 0),
            exit_all_required=bool(data.get("exit_all_required", False)),
            sell_all_at_market_open=bool(data.get("sell_all_at_market_open", False)),
            pending_partial_sell_quantity=int(
                data.get("pending_partial_sell_quantity", 0) or 0
            ),
            capital_reservation_id=str(data.get("capital_reservation_id", "")),
            created_at=data.get("created_at") or _utc_now(),
            updated_at=data.get("updated_at") or _utc_now(),
            warnings=list(data.get("warnings", []) or []),
        )
