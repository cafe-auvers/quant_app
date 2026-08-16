"""Order lifecycle models for broker-submitted orders."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderIntent(str, Enum):
    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    MOMENTUM_EXIT = "MOMENTUM_EXIT"
    PARTIAL_TAKE_PROFIT = "PARTIAL_TAKE_PROFIT"
    MANUAL_EXIT = "MANUAL_EXIT"
    UNKNOWN = "UNKNOWN"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WORKING = "WORKING"
    UNKNOWN_SUBMISSION_STATE = "UNKNOWN_SUBMISSION_STATE"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


REGULAR_LIMIT_EXECUTION = "REGULAR_LIMIT"
RESERVED_MOO_EXECUTION = "RESERVED_MOO"


OPEN_ORDER_STATUSES = {
    OrderStatus.CREATED,
    OrderStatus.SUBMITTING,
    OrderStatus.ACCEPTED,
    OrderStatus.WORKING,
    OrderStatus.UNKNOWN_SUBMISSION_STATE,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.CANCEL_REQUESTED,
    OrderStatus.UNKNOWN,
}

CLOSED_ORDER_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _sanitize_id_part(value: Any) -> str:
    text = str(value or "").strip().upper()
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in text).strip("-") or "UNKNOWN"


def generate_client_order_id(
    environment: str,
    account_no: str,
    symbol: str,
    side: OrderSide | str,
    intent: OrderIntent | str,
    timestamp: Optional[dt.datetime] = None,
) -> str:
    """Generate a readable, unique local idempotency key for an order intent.

    Windows clock resolution can yield the same microsecond timestamp for a
    burst of orders.  Include an independent nonce so two locally reserved
    orders never overwrite each other in the durable ledger.
    """
    ts = timestamp or dt.datetime.now(dt.timezone.utc)
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
    side_value = _enum_value(side, OrderSide, OrderSide.BUY).value
    intent_value = _enum_value(intent, OrderIntent, OrderIntent.UNKNOWN).value
    return "-".join([
        _sanitize_id_part(environment),
        _sanitize_id_part(account_no),
        _sanitize_id_part(symbol),
        side_value,
        intent_value,
        ts.strftime("%Y%m%dT%H%M%S%f"),
        uuid4().hex[:16].upper(),
    ])


def new_client_order_id(
    environment: str = "",
    account_no: str = "",
    symbol: str = "",
    side: OrderSide | str = OrderSide.BUY,
    intent: OrderIntent | str = OrderIntent.UNKNOWN,
    timestamp: Optional[dt.datetime] = None,
) -> str:
    return generate_client_order_id(environment, account_no, symbol, side, intent, timestamp)


def _enum_value(value: Any, enum_cls: type[Enum], default: Enum) -> Enum:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).upper())
    except (TypeError, ValueError):
        return default


def is_open_status(status: OrderStatus | str) -> bool:
    return _enum_value(status, OrderStatus, OrderStatus.UNKNOWN) in OPEN_ORDER_STATUSES


def is_closed_status(status: OrderStatus | str) -> bool:
    return _enum_value(status, OrderStatus, OrderStatus.UNKNOWN) in CLOSED_ORDER_STATUSES


@dataclass
class BrokerOrderStatusSnapshot:
    environment: str
    account_no: str
    symbol: str
    broker_order_id: str = ""
    client_order_id: str = ""
    side: OrderSide = OrderSide.BUY
    status: OrderStatus = OrderStatus.UNKNOWN
    quantity_requested: int = 0
    filled_quantity: int = 0
    remaining_quantity: int = 0
    avg_fill_price: float = 0.0
    limit_price: float = 0.0
    raw_response: Dict[str, Any] = field(default_factory=dict)
    # Normalized broker-side submission time.  KIS mapping remains empty
    # until Workstream 0 proves the real field; reconciliation must never
    # substitute checked_at (query time) for this value.
    submitted_at: str = ""
    checked_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        self.broker_order_id = str(self.broker_order_id or "")
        self.client_order_id = str(self.client_order_id or "")
        self.side = _enum_value(self.side, OrderSide, OrderSide.BUY)
        self.status = _enum_value(self.status, OrderStatus, OrderStatus.UNKNOWN)
        self.quantity_requested = int(float(self.quantity_requested or 0))
        self.filled_quantity = int(float(self.filled_quantity or 0))
        self.remaining_quantity = int(float(self.remaining_quantity or 0))
        self.avg_fill_price = float(self.avg_fill_price or 0.0)
        self.limit_price = float(self.limit_price or 0.0)
        if not isinstance(self.raw_response, dict):
            self.raw_response = {"raw": self.raw_response}
        self.submitted_at = str(self.submitted_at or "")
        self.checked_at = str(self.checked_at or utc_now_iso())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "environment": self.environment,
            "account_no": self.account_no,
            "symbol": self.symbol,
            "broker_order_id": self.broker_order_id,
            "client_order_id": self.client_order_id,
            "side": self.side.value,
            "status": self.status.value,
            "quantity_requested": self.quantity_requested,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "avg_fill_price": self.avg_fill_price,
            "limit_price": self.limit_price,
            "raw_response": self.raw_response,
            "submitted_at": self.submitted_at,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrokerOrderStatusSnapshot":
        return cls(
            environment=str(data.get("environment", "")),
            account_no=str(data.get("account_no", "")),
            symbol=str(data.get("symbol", "")),
            broker_order_id=str(data.get("broker_order_id", "")),
            client_order_id=str(data.get("client_order_id", "")),
            side=_enum_value(data.get("side"), OrderSide, OrderSide.BUY),
            status=_enum_value(data.get("status"), OrderStatus, OrderStatus.UNKNOWN),
            quantity_requested=int(float(data.get("quantity_requested", 0) or 0)),
            filled_quantity=int(float(data.get("filled_quantity", 0) or 0)),
            remaining_quantity=int(float(data.get("remaining_quantity", 0) or 0)),
            avg_fill_price=float(data.get("avg_fill_price", 0.0) or 0.0),
            limit_price=float(data.get("limit_price", 0.0) or 0.0),
            raw_response=data.get("raw_response") if isinstance(data.get("raw_response"), dict) else {},
            submitted_at=str(data.get("submitted_at") or ""),
            checked_at=str(data.get("checked_at") or utc_now_iso()),
        )


@dataclass
class BrokerOrderDiscoveryResult:
    """Completeness-aware account-wide broker order discovery.

    An empty snapshot list is only trustworthy when every required source is
    complete.  Handoff code must fail closed on any incomplete flag or error;
    this prevents a partial KIS outage from looking like "no open orders".
    """

    snapshots: List[BrokerOrderStatusSnapshot] = field(default_factory=list)
    open_orders_complete: bool = False
    history_complete: bool = False
    reserved_orders_complete: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(
            self.open_orders_complete
            and self.history_complete
            and self.reserved_orders_complete
            and not self.errors
        )


@dataclass
class BrokerOrder:
    client_order_id: str
    environment: str
    account_no: str
    symbol: str
    side: OrderSide
    intent: OrderIntent
    quantity_requested: int
    limit_price: float
    exchange: str
    status: OrderStatus
    execution_policy: str = REGULAR_LIMIT_EXECUTION
    broker_order_id: str = ""
    submitted_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    filled_quantity: int = 0
    applied_filled_quantity: int = 0
    remaining_quantity: int = 0
    avg_fill_price: float = 0.0
    raw_submit_response: Optional[Dict[str, Any]] = None
    raw_status_response: Optional[Dict[str, Any]] = None
    error_message: str = ""
    buylist_key: str = ""
    buylist_symbol_key: str = ""

    # --- Entry-attempt / capital-reservation extension ----------------
    # buydashboard_to_kanban.md section 446-469: extends BrokerOrder rather
    # than replacing it. All new fields default to a value that makes an
    # order that never went through the attempt engine behave exactly as it
    # did before this extension -- every existing caller/test is unaffected.
    attempt_group_id: str = ""  # all retries belonging to one planned entry
    attempt_number: int = 1  # 1, 2, 3, ...
    attempt_deadline_at: Optional[str] = None  # 15s application deadline (ISO8601)
    retry_eligible: bool = True  # whether the strategy may attempt again
    replaces_client_order_id: str = ""  # previous cancelled attempt
    capital_reservation_id: str = ""  # links this order to reserved capital
    cancel_requested_at: Optional[str] = None
    terminal_reason: str = ""  # filled, EOD, user cancelled, risk invalid, ...

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        self.side = _enum_value(self.side, OrderSide, OrderSide.BUY)
        self.intent = _enum_value(self.intent, OrderIntent, OrderIntent.UNKNOWN)
        self.status = _enum_value(self.status, OrderStatus, OrderStatus.UNKNOWN)
        self.execution_policy = (
            str(self.execution_policy or REGULAR_LIMIT_EXECUTION).strip().upper()
        )
        self.quantity_requested = int(self.quantity_requested or 0)
        self.limit_price = float(self.limit_price or 0.0)
        self.exchange = str(self.exchange or "").upper()
        self.filled_quantity = int(self.filled_quantity or 0)
        self.applied_filled_quantity = int(self.applied_filled_quantity or 0)
        if self.remaining_quantity in (None, 0) and self.filled_quantity == 0:
            self.remaining_quantity = self.quantity_requested
        else:
            self.remaining_quantity = int(self.remaining_quantity or 0)
        self.avg_fill_price = float(self.avg_fill_price or 0.0)
        self.buylist_key = str(self.buylist_key or self.buylist_symbol_key or f"{self.environment}:{self.account_no}:{self.symbol}")
        self.buylist_symbol_key = str(self.buylist_symbol_key or self.buylist_key or self.symbol)
        self.attempt_group_id = str(self.attempt_group_id or "")
        self.attempt_number = int(self.attempt_number or 1)
        self.attempt_deadline_at = (
            str(self.attempt_deadline_at) if self.attempt_deadline_at else None
        )
        self.retry_eligible = bool(self.retry_eligible)
        self.replaces_client_order_id = str(self.replaces_client_order_id or "")
        self.capital_reservation_id = str(self.capital_reservation_id or "")
        self.cancel_requested_at = (
            str(self.cancel_requested_at) if self.cancel_requested_at else None
        )
        self.terminal_reason = str(self.terminal_reason or "")

    @classmethod
    def create(
        cls,
        *,
        environment: str,
        account_no: str,
        symbol: str,
        side: OrderSide | str,
        intent: OrderIntent | str = OrderIntent.UNKNOWN,
        quantity_requested: int,
        limit_price: float,
        exchange: str = "NASD",
        status: OrderStatus | str = OrderStatus.CREATED,
        execution_policy: str = REGULAR_LIMIT_EXECUTION,
        buylist_symbol_key: str = "",
        attempt_group_id: str = "",
        attempt_number: int = 1,
        attempt_deadline_at: Optional[str] = None,
        capital_reservation_id: str = "",
        replaces_client_order_id: str = "",
    ) -> "BrokerOrder":
        return cls(
            client_order_id=generate_client_order_id(
                environment,
                account_no or "",
                symbol,
                _enum_value(side, OrderSide, OrderSide.BUY),
                _enum_value(intent, OrderIntent, OrderIntent.UNKNOWN),
            ),
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            side=_enum_value(side, OrderSide, OrderSide.BUY),
            intent=_enum_value(intent, OrderIntent, OrderIntent.UNKNOWN),
            quantity_requested=quantity_requested,
            limit_price=limit_price,
            exchange=exchange,
            status=_enum_value(status, OrderStatus, OrderStatus.CREATED),
            execution_policy=execution_policy,
            remaining_quantity=int(quantity_requested or 0),
            buylist_key=buylist_symbol_key or f"{str(environment or '').upper()}:{account_no or ''}:{str(symbol).upper()}",
            buylist_symbol_key=buylist_symbol_key or f"{str(environment or '').upper()}:{account_no or ''}:{str(symbol).upper()}",
            attempt_group_id=attempt_group_id,
            attempt_number=attempt_number,
            attempt_deadline_at=attempt_deadline_at,
            capital_reservation_id=capital_reservation_id,
            replaces_client_order_id=replaces_client_order_id,
        )

    def is_open(self) -> bool:
        return self.status in OPEN_ORDER_STATUSES

    @property
    def quantity(self) -> int:
        return self.quantity_requested

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "environment": self.environment,
            "account_no": self.account_no,
            "symbol": self.symbol,
            "side": self.side.value,
            "intent": self.intent.value,
            "quantity_requested": self.quantity_requested,
            "limit_price": self.limit_price,
            "exchange": self.exchange,
            "status": self.status.value,
            "execution_policy": self.execution_policy,
            "broker_order_id": self.broker_order_id,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "filled_quantity": self.filled_quantity,
            "applied_filled_quantity": self.applied_filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "avg_fill_price": self.avg_fill_price,
            "raw_submit_response": self.raw_submit_response,
            "raw_status_response": self.raw_status_response,
            "error_message": self.error_message,
            "buylist_key": self.buylist_key,
            "buylist_symbol_key": self.buylist_symbol_key,
            "attempt_group_id": self.attempt_group_id,
            "attempt_number": self.attempt_number,
            "attempt_deadline_at": self.attempt_deadline_at,
            "retry_eligible": self.retry_eligible,
            "replaces_client_order_id": self.replaces_client_order_id,
            "capital_reservation_id": self.capital_reservation_id,
            "cancel_requested_at": self.cancel_requested_at,
            "terminal_reason": self.terminal_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrokerOrder":
        return cls(
            client_order_id=str(data.get("client_order_id", "")),
            environment=str(data.get("environment", "")),
            account_no=str(data.get("account_no", "")),
            symbol=str(data.get("symbol", "")),
            side=_enum_value(data.get("side"), OrderSide, OrderSide.BUY),
            intent=_enum_value(data.get("intent"), OrderIntent, OrderIntent.UNKNOWN),
            quantity_requested=int(float(data.get("quantity_requested", 0) or 0)),
            limit_price=float(data.get("limit_price", 0.0) or 0.0),
            exchange=str(data.get("exchange", "")),
            status=_enum_value(data.get("status"), OrderStatus, OrderStatus.UNKNOWN),
            execution_policy=str(
                data.get("execution_policy") or REGULAR_LIMIT_EXECUTION
            ),
            broker_order_id=str(data.get("broker_order_id", "")),
            submitted_at=str(data.get("submitted_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            filled_quantity=int(float(data.get("filled_quantity", 0) or 0)),
            applied_filled_quantity=int(float(data.get("applied_filled_quantity", 0) or 0)),
            remaining_quantity=int(float(data.get("remaining_quantity", 0) or 0)),
            avg_fill_price=float(data.get("avg_fill_price", 0.0) or 0.0),
            raw_submit_response=data.get("raw_submit_response"),
            raw_status_response=data.get("raw_status_response"),
            error_message=str(data.get("error_message", "")),
            buylist_key=str(data.get("buylist_key", "")),
            buylist_symbol_key=str(data.get("buylist_symbol_key", "")),
            attempt_group_id=str(data.get("attempt_group_id", "")),
            attempt_number=int(data.get("attempt_number", 1) or 1),
            attempt_deadline_at=data.get("attempt_deadline_at"),
            retry_eligible=bool(data.get("retry_eligible", True)),
            replaces_client_order_id=str(data.get("replaces_client_order_id", "")),
            capital_reservation_id=str(data.get("capital_reservation_id", "")),
            cancel_requested_at=data.get("cancel_requested_at"),
            terminal_reason=str(data.get("terminal_reason", "")),
        )
