"""Conservative order reconciliation using KIS account snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core.order_state import (
    BrokerOrder,
    BrokerOrderStatusSnapshot,
    OPEN_ORDER_STATUSES,
    OrderSide,
    OrderStatus,
    is_open_status,
)
from src.services.order_ledger import ORDERS_FILE, load_orders, save_orders


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


def reconcile_order_with_broker_snapshot(
    order: BrokerOrder,
    snapshot: BrokerOrderStatusSnapshot,
) -> BrokerOrder:
    """Update one local order from direct broker order-status evidence."""
    if snapshot.client_order_id and not order.client_order_id:
        order.client_order_id = snapshot.client_order_id
    if snapshot.broker_order_id and not order.broker_order_id:
        order.broker_order_id = snapshot.broker_order_id
    if snapshot.raw_response:
        order.raw_status_response = snapshot.to_dict()

    if snapshot.status == OrderStatus.FILLED:
        order.status = OrderStatus.FILLED
        filled = snapshot.filled_quantity or order.quantity_requested
        order.filled_quantity = max(0, int(filled))
        order.remaining_quantity = 0
        if snapshot.avg_fill_price > 0:
            order.avg_fill_price = snapshot.avg_fill_price
    elif snapshot.status == OrderStatus.PARTIALLY_FILLED:
        order.status = OrderStatus.PARTIALLY_FILLED
        order.filled_quantity = max(0, int(snapshot.filled_quantity or order.filled_quantity))
        if snapshot.remaining_quantity > 0:
            order.remaining_quantity = int(snapshot.remaining_quantity)
        else:
            order.remaining_quantity = max(0, order.quantity_requested - order.filled_quantity)
        if snapshot.avg_fill_price > 0:
            order.avg_fill_price = snapshot.avg_fill_price
    elif snapshot.status in {OrderStatus.WORKING, OrderStatus.ACCEPTED}:
        order.status = snapshot.status
        if snapshot.filled_quantity > 0:
            order.filled_quantity = int(snapshot.filled_quantity)
        if snapshot.remaining_quantity > 0:
            order.remaining_quantity = int(snapshot.remaining_quantity)
    elif snapshot.status in {
        OrderStatus.CANCEL_REQUESTED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }:
        order.status = snapshot.status
        if snapshot.filled_quantity > 0:
            order.filled_quantity = int(snapshot.filled_quantity)
        if snapshot.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}:
            order.remaining_quantity = max(0, int(snapshot.remaining_quantity or 0))
    elif snapshot.status == OrderStatus.UNKNOWN:
        if order.status != OrderStatus.UNKNOWN_SUBMISSION_STATE:
            order.status = order.status

    if snapshot.limit_price > 0 and order.limit_price <= 0:
        order.limit_price = snapshot.limit_price
    if order.remaining_quantity <= 0 and order.status in {OrderStatus.ACCEPTED, OrderStatus.WORKING}:
        order.remaining_quantity = max(0, order.quantity_requested - order.filled_quantity)
    order.touch()
    return order


def _date_window_for_order(order: BrokerOrder) -> tuple[str, str]:
    now = datetime.now(timezone.utc).date()
    try:
        submitted = datetime.fromisoformat(str(order.submitted_at).replace("Z", "+00:00"))
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        start = (submitted.astimezone(timezone.utc).date() - timedelta(days=2)).strftime("%Y%m%d")
    except (TypeError, ValueError):
        start = (now - timedelta(days=14)).strftime("%Y%m%d")
    return start, (now + timedelta(days=1)).strftime("%Y%m%d")


def _select_snapshot_for_order(
    order: BrokerOrder,
    snapshots: List[BrokerOrderStatusSnapshot],
) -> Optional[BrokerOrderStatusSnapshot]:
    if not snapshots:
        return None
    priority = {
        OrderStatus.FILLED: 100,
        OrderStatus.CANCELLED: 95,
        OrderStatus.REJECTED: 90,
        OrderStatus.EXPIRED: 85,
        OrderStatus.PARTIALLY_FILLED: 70,
        OrderStatus.CANCEL_REQUESTED: 60,
        OrderStatus.WORKING: 50,
        OrderStatus.ACCEPTED: 45,
        OrderStatus.UNKNOWN: 0,
    }
    if order.broker_order_id:
        matches = [snapshot for snapshot in snapshots if snapshot.broker_order_id == order.broker_order_id]
        if matches:
            return max(matches, key=lambda snapshot: priority.get(snapshot.status, 0))
    matches = [snapshot for snapshot in snapshots if snapshot.symbol == order.symbol and snapshot.side == order.side]
    if matches:
        return max(matches, key=lambda snapshot: priority.get(snapshot.status, 0))
    return max(snapshots, key=lambda snapshot: priority.get(snapshot.status, 0))


def query_and_reconcile_unresolved_orders(
    *,
    environment: Optional[str] = None,
    account_no: Optional[str] = None,
    symbol: Optional[str] = None,
    path: Path = ORDERS_FILE,
) -> List[BrokerOrder]:
    """Query KIS for unresolved local orders and persist broker-backed updates."""
    from src.api import kis_order

    environment_key = str(environment or "").upper()
    account_key = str(account_no or "")
    symbol_key = str(symbol or "").upper()
    orders = load_orders(path)
    updated_orders: List[BrokerOrder] = []

    for order in orders:
        if not is_open_status(order.status):
            continue
        if environment_key and order.environment != environment_key:
            continue
        if account_key and str(order.account_no or "") != account_key:
            continue
        if symbol_key and order.symbol != symbol_key:
            continue

        start, end = _date_window_for_order(order)
        try:
            snapshots = kis_order.query_overseas_order(
                environment=order.environment,
                account_no=order.account_no,
                symbol=order.symbol,
                broker_order_id=order.broker_order_id,
                client_order_id=order.client_order_id,
                side=order.side.value,
                start_date=start,
                end_date=end,
                exchange=order.exchange or "NASD",
            )
            snapshot = _select_snapshot_for_order(order, snapshots)
            if snapshot is None:
                continue
            reconcile_order_with_broker_snapshot(order, snapshot)
            updated_orders.append(order)
            save_orders(orders, path)
        except Exception as exc:
            note = f"Broker order query failed: {exc}"
            order.error_message = f"{order.error_message}; {note}" if order.error_message else note
            order.touch()
            save_orders(orders, path)
            continue

    return updated_orders


def cancel_and_reconcile_order(
    client_order_id: str,
    *,
    path: Path = ORDERS_FILE,
) -> BrokerOrder:
    """Cancel one known broker-side open order and persist the local status."""
    from src.api import kis_order

    orders = load_orders(path)
    target: Optional[BrokerOrder] = None
    for order in orders:
        if order.client_order_id == str(client_order_id or ""):
            target = order
            break
    if target is None:
        raise ValueError(f"Order {client_order_id} was not found")
    if not is_open_status(target.status):
        raise ValueError(f"Order {client_order_id} is already {target.status.value}")
    if not target.broker_order_id:
        raise ValueError("broker_order_id is required before canceling a KIS order")

    quantity = target.remaining_quantity or max(0, target.quantity_requested - target.filled_quantity)
    if quantity <= 0:
        quantity = target.quantity_requested
    snapshot = kis_order.cancel_overseas_order(
        environment=target.environment,
        account_no=target.account_no,
        symbol=target.symbol,
        broker_order_id=target.broker_order_id,
        quantity=quantity,
        side=target.side.value,
        exchange=target.exchange or "NASD",
    )
    reconcile_order_with_broker_snapshot(target, snapshot)
    save_orders(orders, path)
    return target
