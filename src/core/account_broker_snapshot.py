"""Immutable, account-scoped broker truth used by reconciliation.

One snapshot is fetched once per account and reused for every card/order in
that reconciliation pass.  Completeness is tracked per source because an
unrelated KIS query failure must not turn usable holdings or order evidence
into an all-or-nothing result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import FrozenSet, Optional, Tuple
from uuid import uuid4

from src.core.order_state import BrokerOrderStatusSnapshot


class ReconciliationAction(str, Enum):
    NEW_ENTRY = "NEW_ENTRY"
    CANCEL_KNOWN_ORDER = "CANCEL_KNOWN_ORDER"
    POSITION_QUANTITY_UPDATE = "POSITION_QUANTITY_UPDATE"
    TERMINAL_ORDER_CONCLUSION = "TERMINAL_ORDER_CONCLUSION"
    RESERVED_MOO_RECONCILIATION = "RESERVED_MOO_RECONCILIATION"
    EMERGENCY_SELL_ALL = "EMERGENCY_SELL_ALL"


@dataclass(frozen=True)
class SnapshotCompleteness:
    holdings_complete: bool = False
    open_orders_complete: bool = False
    history_complete: bool = False
    reserved_orders_complete: bool = False
    account_balance_complete: bool = False

    def allows(self, action: ReconciliationAction) -> bool:
        requirements = _ACTION_REQUIREMENTS[action]
        return all(bool(getattr(self, field_name)) for field_name in requirements)


_ACTION_REQUIREMENTS = {
    ReconciliationAction.NEW_ENTRY: (
        "holdings_complete",
        "open_orders_complete",
        "account_balance_complete",
    ),
    ReconciliationAction.CANCEL_KNOWN_ORDER: ("open_orders_complete",),
    ReconciliationAction.POSITION_QUANTITY_UPDATE: ("holdings_complete",),
    ReconciliationAction.TERMINAL_ORDER_CONCLUSION: (
        "holdings_complete",
        "open_orders_complete",
        "history_complete",
    ),
    ReconciliationAction.RESERVED_MOO_RECONCILIATION: (
        "reserved_orders_complete",
    ),
    # Holdings are the only unconditional requirement.  If open-order
    # discovery is incomplete the reducer falls back to broker sellable
    # quantity, or alerts instead of guessing.
    ReconciliationAction.EMERGENCY_SELL_ALL: ("holdings_complete",),
}


@dataclass(frozen=True)
class AccountHoldingSnapshot:
    symbol: str
    quantity: int
    average_price: float = 0.0
    sellable_quantity: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        object.__setattr__(self, "quantity", max(0, int(self.quantity or 0)))
        object.__setattr__(self, "average_price", float(self.average_price or 0.0))
        if self.sellable_quantity is not None:
            object.__setattr__(
                self,
                "sellable_quantity",
                max(0, int(self.sellable_quantity or 0)),
            )


@dataclass(frozen=True)
class AccountBrokerSnapshot:
    environment: str
    account_no: str
    completeness: SnapshotCompleteness
    holdings: Tuple[AccountHoldingSnapshot, ...] = ()
    orders: Tuple[BrokerOrderStatusSnapshot, ...] = ()
    account_buying_power: Optional[float] = None
    account_equity: Optional[float] = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    session_date: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    snapshot_id: str = field(default_factory=lambda: uuid4().hex)
    errors: Tuple[str, ...] = ()
    execution_notice_broker_order_ids: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        observed = self.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "observed_at", observed.astimezone(timezone.utc))
        object.__setattr__(self, "holdings", tuple(self.holdings or ()))
        object.__setattr__(self, "orders", tuple(self.orders or ()))
        object.__setattr__(self, "errors", tuple(str(error) for error in (self.errors or ())))
        object.__setattr__(
            self,
            "execution_notice_broker_order_ids",
            frozenset(
                str(value or "").strip()
                for value in (self.execution_notice_broker_order_ids or ())
                if str(value or "").strip()
            ),
        )

    def holding_for(self, symbol: str) -> Optional[AccountHoldingSnapshot]:
        wanted = str(symbol or "").upper()
        return next((holding for holding in self.holdings if holding.symbol == wanted), None)

    def order_for_broker_id(self, broker_order_id: str) -> Optional[BrokerOrderStatusSnapshot]:
        wanted = str(broker_order_id or "").strip()
        return next((order for order in self.orders if order.broker_order_id == wanted), None)
