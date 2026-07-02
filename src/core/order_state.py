"""Order lifecycle models for broker-submitted orders."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


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
    """Generate a readable, unique local idempotency key for an order intent."""
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

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        self.side = _enum_value(self.side, OrderSide, OrderSide.BUY)
        self.intent = _enum_value(self.intent, OrderIntent, OrderIntent.UNKNOWN)
        self.status = _enum_value(self.status, OrderStatus, OrderStatus.UNKNOWN)
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
        buylist_symbol_key: str = "",
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
            remaining_quantity=int(quantity_requested or 0),
            buylist_key=buylist_symbol_key or f"{str(environment or '').upper()}:{account_no or ''}:{str(symbol).upper()}",
            buylist_symbol_key=buylist_symbol_key or f"{str(environment or '').upper()}:{account_no or ''}:{str(symbol).upper()}",
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
        )
