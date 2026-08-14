"""QThread adapters for durable order submission and reconciliation services."""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrder,
    OrderIntent,
    OrderSide,
)
from src.risk.pre_trade import PreTradeRiskDecision
from src.services.broker import Broker, KisBroker
from src.services.event_journal import record_event
from src.services.order_reconciliation import reconcile_orders_with_snapshot


class KisOrderWorker(QThread):
    """Place a KIS overseas equity order in a background thread."""

    finished_order = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        environment: str,
        symbol: str,
        quantity: int,
        price: float,
        side: str,
        exchange: str = "NASD",
        order_type: str = "limit",
        account_no: Optional[str] = None,
        intent: OrderIntent | str = OrderIntent.UNKNOWN,
        buylist_symbol_key: str = "",
        execution_policy: str = REGULAR_LIMIT_EXECUTION,
        broker: Optional[Broker] = None,
        pre_trade_risk_decision: Optional[PreTradeRiskDecision] = None,
        strategy_id: str = "",
        plan_id: str = "",
        event_recorder: Optional[Callable[..., Any]] = None,
        signal_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self.environment = environment
        self.symbol = symbol
        self.quantity = quantity
        self.price = price
        self.side = side
        self.exchange = exchange
        self.order_type = order_type
        self.account_no = account_no
        self.intent = intent
        self.buylist_symbol_key = buylist_symbol_key
        self.execution_policy = execution_policy
        self.broker = broker
        self.pre_trade_risk_decision = pre_trade_risk_decision
        self.strategy_id = strategy_id
        self.plan_id = plan_id
        self.event_recorder = event_recorder or record_event
        self.signal_payload = signal_payload

    def run(self) -> None:
        try:
            from src.services.order_execution_service import (
                submit_guarded_overseas_order,
            )

            order = submit_guarded_overseas_order(
                environment=self.environment,
                account_no=self.account_no or "",
                symbol=self.symbol,
                side=OrderSide(str(self.side).upper()),
                intent=(
                    self.intent
                    if isinstance(self.intent, OrderIntent)
                    else OrderIntent(str(self.intent).upper())
                ),
                quantity=self.quantity,
                limit_price=self.price,
                exchange=self.exchange,
                execution_policy=self.execution_policy,
                broker=self.broker,
                pre_trade_risk_decision=self.pre_trade_risk_decision,
                strategy_id=self.strategy_id,
                plan_id=self.plan_id,
                event_recorder=self.event_recorder,
                signal_payload=self.signal_payload,
            )
            self.finished_order.emit(order)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class OrderReconciliationWorker(QThread):
    finished_reconciliation = pyqtSignal(list, dict)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        environment: str,
        account_no: str,
        open_orders: List[BrokerOrder],
        previous_snapshot: Optional[dict] = None,
        broker: Optional[Broker] = None,
    ) -> None:
        super().__init__()
        self.environment = environment
        self.account_no = account_no
        self.open_orders = list(open_orders)
        self.previous_snapshot = previous_snapshot
        self.broker = KisBroker() if broker is None else broker

    def run(self) -> None:
        try:
            orders_to_reconcile = self.open_orders
            if any(
                order.execution_policy == RESERVED_MOO_EXECUTION
                for order in self.open_orders
            ):
                from src.services.order_reconciliation import (
                    query_and_reconcile_unresolved_orders,
                )

                direct_updates = query_and_reconcile_unresolved_orders(
                    environment=self.environment,
                    account_no=self.account_no,
                    execution_policy=RESERVED_MOO_EXECUTION,
                    broker=self.broker,
                )
                direct_by_id = {
                    order.client_order_id: order for order in direct_updates
                }
                orders_to_reconcile = [
                    direct_by_id.get(order.client_order_id, order)
                    for order in self.open_orders
                ]

            snapshot = self.broker.get_positions(
                environment=self.environment,
                account_no=self.account_no,
            )
            updated_orders = reconcile_orders_with_snapshot(
                orders_to_reconcile,
                snapshot,
                previous_snapshot=self.previous_snapshot,
            )
            self.finished_reconciliation.emit(updated_orders, snapshot)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class KisOrderQueryWorker(QThread):
    finished_query = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        environment: Optional[str] = None,
        account_no: Optional[str] = None,
        symbol: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        broker: Optional[Broker] = None,
    ) -> None:
        super().__init__()
        self.environment = environment
        self.account_no = account_no
        self.symbol = symbol
        self.broker_order_id = broker_order_id
        self.client_order_id = client_order_id
        self.broker = broker

    def run(self) -> None:
        try:
            from src.services.order_reconciliation import (
                query_and_reconcile_unresolved_orders,
            )

            updated = query_and_reconcile_unresolved_orders(
                environment=self.environment,
                account_no=self.account_no,
                symbol=self.symbol,
                broker=self.broker,
            )
            if self.client_order_id:
                updated = [
                    order
                    for order in updated
                    if order.client_order_id == self.client_order_id
                    or (
                        self.broker_order_id
                        and order.broker_order_id == self.broker_order_id
                    )
                ]
            self.finished_query.emit(updated)
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class KisOrderCancelWorker(QThread):
    finished_cancel = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, client_order_id: str, broker: Optional[Broker] = None) -> None:
        super().__init__()
        self.client_order_id = client_order_id
        self.broker = broker

    def run(self) -> None:
        try:
            from src.services.order_reconciliation import cancel_and_reconcile_order

            order = cancel_and_reconcile_order(self.client_order_id, broker=self.broker)
            self.finished_cancel.emit(order)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
