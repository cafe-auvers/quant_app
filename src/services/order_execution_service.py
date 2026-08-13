"""Durable guarded order submission for KIS overseas orders."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.api import kis_order
from src.core.order_state import (
    REGULAR_LIMIT_EXECUTION,
    RESERVED_MOO_EXECUTION,
    BrokerOrder,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.services.broker import Broker, KisBroker
from src.services.order_ledger import ORDERS_FILE, reserve_order_if_no_matching_open, upsert_order
from src.services import trading_state

logger = logging.getLogger(__name__)


class DuplicateOpenOrderError(RuntimeError):
    """Raised when a matching unresolved broker order already exists."""

    def __init__(self, order: BrokerOrder) -> None:
        self.order = order
        super().__init__(
            f"Open {order.side.value} {order.intent.value} order already exists for "
            f"{order.symbol} in {order.environment} account {order.account_no}. "
            "Reconcile or cancel it before submitting another order."
        )


class TradingDisabledError(RuntimeError):
    """Raised when a guarded order submission is attempted while the live-trading
    kill switch is off. Independent of PROD/SIM environment selection -- this
    gate applies to every guarded submission, not just live PROD orders."""

    def __init__(self, environment: str, symbol: str) -> None:
        self.environment = environment
        self.symbol = symbol
        super().__init__(
            f"Live trading is disabled (kill switch off); refused to submit "
            f"{environment} order for {symbol}. Enable trading from the toolbar "
            "before submitting orders."
        )


def _extract_broker_order_id(response: Dict[str, Any]) -> Optional[str]:
    candidates = (
        "OVRS_RSVN_ODNO",
        "ovrs_rsvn_odno",
        "ODNO",
        "odno",
        "order_no",
        "ORD_NO",
    )

    def walk(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            for key in candidates:
                if value.get(key):
                    return str(value[key])
            for item in value.values():
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(response)


def submit_guarded_overseas_order(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    side: OrderSide,
    intent: OrderIntent,
    quantity: int,
    limit_price: float,
    exchange: str = "NASD",
    execution_policy: str = REGULAR_LIMIT_EXECUTION,
    allow_duplicate: bool = False,
    path: Path = ORDERS_FILE,
    broker: Optional[Broker] = None,
) -> BrokerOrder:
    """Submit a KIS order with a durable local idempotency guard.

    This function records local intent before touching the KIS API. A returned
    ACCEPTED order means only that KIS received the order request; it never
    implies a broker fill or local position change.

    ``broker`` defaults to ``KisBroker()`` (the real KIS API) -- this state
    machine itself has no broker-specific knowledge and doesn't change based
    on which ``Broker`` implementation runs underneath it.
    """
    broker = broker or KisBroker()
    if not trading_state.is_trading_enabled():
        raise TradingDisabledError(
            environment=str(environment or "").upper(),
            symbol=str(symbol or "").upper(),
        )
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")

    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    symbol = str(symbol or "").upper()
    side = side if isinstance(side, OrderSide) else OrderSide(str(side).upper())
    intent = intent if isinstance(intent, OrderIntent) else OrderIntent(str(intent).upper())
    execution_policy = str(
        execution_policy or REGULAR_LIMIT_EXECUTION
    ).strip().upper()
    if execution_policy not in {
        REGULAR_LIMIT_EXECUTION,
        RESERVED_MOO_EXECUTION,
    }:
        raise ValueError(f"Unsupported execution_policy={execution_policy!r}")
    if execution_policy == RESERVED_MOO_EXECUTION:
        if environment != "PROD" or side != OrderSide.SELL:
            raise ValueError("Reserved MOO execution requires a PROD sell order")
        limit_price = 0.0
    elif limit_price <= 0:
        raise ValueError(f"limit_price must be positive, got {limit_price}")

    order = BrokerOrder.create(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        side=side,
        intent=intent,
        quantity_requested=quantity,
        limit_price=limit_price,
        exchange=exchange,
        status=OrderStatus.CREATED,
        execution_policy=execution_policy,
        buylist_symbol_key=f"{environment}:{account_no}:{symbol}",
    )
    match = reserve_order_if_no_matching_open(
        order,
        allow_duplicate=allow_duplicate,
        path=path,
    )
    if match is not None:
        raise DuplicateOpenOrderError(match)

    # Once we are about to issue the non-idempotent broker request, a process
    # crash or lost response must be treated as potentially submitted.  This
    # blocks a retry until reconciliation checks the broker.
    order.status = OrderStatus.UNKNOWN_SUBMISSION_STATE
    order.touch()
    upsert_order(order, path=path)

    try:
        response = broker.submit_order(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            exchange=exchange,
            execution_policy=execution_policy,
        )
    except Exception as exc:
        if kis_order.is_ambiguous_order_submission_error(exc):
            order.status = OrderStatus.UNKNOWN_SUBMISSION_STATE
            logger.warning(
                "KIS guarded order submission result unknown for %s %s %s account %s: %s",
                environment,
                side.value,
                symbol,
                account_no or "<unknown>",
                exc,
            )
        else:
            order.status = OrderStatus.REJECTED
        order.error_message = str(exc)
        order.touch()
        upsert_order(order, path=path)
        return order

    order.status = OrderStatus.ACCEPTED
    order.broker_order_id = _extract_broker_order_id(response) or ""
    order.raw_submit_response = response
    order.filled_quantity = 0
    order.remaining_quantity = order.quantity_requested
    order.touch()
    upsert_order(order, path=path)
    return order
