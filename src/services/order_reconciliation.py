"""Conservative order reconciliation using KIS account snapshots."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core.order_state import BrokerOrder, OPEN_ORDER_STATUSES, OrderSide, OrderStatus


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _holding_for_symbol(snapshot: Optional[Dict[str, Any]], symbol: str) -> Tuple[float, float, Optional[Dict[str, Any]]]:
    if not isinstance(snapshot, dict):
        return 0.0, 0.0, None

    symbol = symbol.strip().upper()
    best_holding: Optional[Dict[str, Any]] = None
    for section_name in ("domestic", "overseas"):
        section = snapshot.get(section_name)
        if not isinstance(section, dict):
            continue
        for holding in section.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            if str(holding.get("symbol", "")).strip().upper() != symbol:
                continue
            if best_holding is None or _to_float(holding.get("quantity")) > _to_float(best_holding.get("quantity")):
                best_holding = holding

    if best_holding is None:
        return 0.0, 0.0, None
    return (
        _to_float(best_holding.get("quantity")),
        _to_float(best_holding.get("average_price")),
        best_holding,
    )


def _mark_fill(order: BrokerOrder, filled_quantity: int, avg_fill_price: float, snapshot: Dict[str, Any]) -> None:
    filled_quantity = max(0, min(int(filled_quantity), int(order.quantity_requested)))
    order.filled_quantity = filled_quantity
    order.remaining_quantity = max(0, int(order.quantity_requested) - filled_quantity)
    if avg_fill_price > 0:
        order.avg_fill_price = avg_fill_price
    order.raw_status_response = snapshot
    if filled_quantity >= order.quantity_requested:
        order.status = OrderStatus.FILLED
    elif filled_quantity > 0:
        order.status = OrderStatus.PARTIALLY_FILLED
    elif order.status in {OrderStatus.ACCEPTED, OrderStatus.SUBMITTING, OrderStatus.CREATED, OrderStatus.UNKNOWN}:
        order.status = OrderStatus.WORKING
    order.touch()


def reconcile_orders_with_snapshot(
    orders: Iterable[BrokerOrder],
    snapshot: Dict[str, Any],
    previous_snapshot: Optional[Dict[str, Any]] = None,
) -> List[BrokerOrder]:
    """Return orders updated from holdings evidence.

    This intentionally avoids unverified order-status endpoints. If holdings
    evidence is insufficient, the order remains working/unknown instead of being
    marked filled.
    """
    reconciled: List[BrokerOrder] = []
    for order in orders:
        if order.status not in OPEN_ORDER_STATUSES:
            reconciled.append(order)
            continue

        current_qty, current_avg, _ = _holding_for_symbol(snapshot, order.symbol)
        previous_qty, _, _ = _holding_for_symbol(previous_snapshot, order.symbol)
        confirmed_fill = 0

        if previous_snapshot is not None:
            if order.side == OrderSide.BUY:
                confirmed_fill = max(0, int(round(current_qty - previous_qty)))
            elif order.side == OrderSide.SELL:
                confirmed_fill = max(0, int(round(previous_qty - current_qty)))
        else:
            # Conservative no-baseline inference. A full exit is clear when the
            # position disappeared; otherwise keep the order working.
            if order.side == OrderSide.SELL and current_qty <= 0 and order.filled_quantity > 0:
                confirmed_fill = order.quantity_requested
            elif order.filled_quantity > 0:
                confirmed_fill = order.filled_quantity

        if confirmed_fill > 0:
            _mark_fill(order, confirmed_fill, current_avg, snapshot)
        elif order.status in {OrderStatus.ACCEPTED, OrderStatus.SUBMITTING, OrderStatus.CREATED, OrderStatus.UNKNOWN}:
            order.status = OrderStatus.WORKING
            order.raw_status_response = snapshot
            order.touch()

        reconciled.append(order)
    return reconciled
