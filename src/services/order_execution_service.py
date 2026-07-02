"""Durable guarded order submission for KIS overseas orders."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.api import kis_order
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.services.order_ledger import ORDERS_FILE, append_order, has_open_order, upsert_order

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


def _find_matching_open_order(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    side: OrderSide,
    intent: OrderIntent,
    path: Path,
) -> Optional[BrokerOrder]:
    from src.services.order_ledger import find_open_orders

    matches = find_open_orders(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        side=side,
        intent=intent,
        path=path,
    )
    return matches[0] if matches else None


def _extract_broker_order_id(response: Dict[str, Any]) -> Optional[str]:
    candidates = ("ODNO", "odno", "order_no", "ORD_NO")

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
    allow_duplicate: bool = False,
    path: Path = ORDERS_FILE,
) -> BrokerOrder:
    """Submit a KIS order with a durable local idempotency guard.

    This function records local intent before touching the KIS API. A returned
    ACCEPTED order means only that KIS received the order request; it never
    implies a broker fill or local position change.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    if limit_price <= 0:
        raise ValueError(f"limit_price must be positive, got {limit_price}")

    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    symbol = str(symbol or "").upper()
    side = side if isinstance(side, OrderSide) else OrderSide(str(side).upper())
    intent = intent if isinstance(intent, OrderIntent) else OrderIntent(str(intent).upper())

    if not allow_duplicate:
        match = _find_matching_open_order(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            side=side,
            intent=intent,
            path=path,
        )
        if match is not None:
            raise DuplicateOpenOrderError(match)

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
        buylist_symbol_key=f"{environment}:{account_no}:{symbol}",
    )
    append_order(order, path=path)

    order.status = OrderStatus.SUBMITTING
    order.touch()
    upsert_order(order, path=path)

    try:
        response = kis_order.place_overseas_order(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            quantity=quantity,
            price=limit_price,
            side=side.value.lower(),
            exchange=exchange,
            order_type="limit",
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
