"""Conservative order reconciliation using KIS account snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.core.order_state import (
    RESERVED_MOO_EXECUTION,
    BrokerOrder,
    BrokerOrderStatusSnapshot,
    OPEN_ORDER_STATUSES,
    OrderSide,
    OrderStatus,
    is_open_status,
)
from src.services.order_ledger import ORDERS_FILE, load_orders, mutate_order
from src.services.broker import Broker, KisBroker


def _to_float(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


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


def _requested_quantity(order: BrokerOrder) -> int:
    return max(0, int(_to_float(order.quantity_requested)))


def _known_filled_quantity(order: BrokerOrder) -> int:
    return max(0, min(int(_to_float(order.filled_quantity)), _requested_quantity(order)))


def _mark_fill(
    order: BrokerOrder,
    filled_quantity: int,
    avg_fill_price: float,
    snapshot: Dict[str, Any],
) -> None:
    """Persist a cumulative fill without ever reducing known broker evidence."""
    requested = _requested_quantity(order)
    proposed = max(0, min(int(filled_quantity or 0), requested))
    filled_quantity = max(_known_filled_quantity(order), proposed)
    order.filled_quantity = filled_quantity
    order.remaining_quantity = max(0, requested - filled_quantity)
    if avg_fill_price > 0:
        order.avg_fill_price = avg_fill_price
    order.raw_status_response = snapshot
    if requested > 0 and filled_quantity >= requested:
        order.status = OrderStatus.FILLED
    elif filled_quantity > 0:
        order.status = OrderStatus.PARTIALLY_FILLED
    elif order.status in {OrderStatus.ACCEPTED, OrderStatus.SUBMITTING, OrderStatus.CREATED, OrderStatus.UNKNOWN}:
        order.status = OrderStatus.WORKING
    order.touch()


def _mark_working_if_unfilled(order: BrokerOrder, snapshot: Dict[str, Any]) -> None:
    """Record fresh holdings evidence without weakening a partial/unknown state."""
    if order.status in {
        OrderStatus.ACCEPTED,
        OrderStatus.SUBMITTING,
        OrderStatus.CREATED,
        OrderStatus.UNKNOWN,
    }:
        order.status = OrderStatus.WORKING
    order.raw_status_response = snapshot
    order.touch()


def _order_sort_key(order: BrokerOrder) -> str:
    """Allocate ambiguous holdings deltas in FIFO order.

    Python's stable sort preserves ledger/input order for orders sharing the
    same legacy second-resolution submission timestamp.
    """
    return str(order.submitted_at or "")


def _apply_broker_fill_progress(
    order: BrokerOrder,
    snapshot: BrokerOrderStatusSnapshot,
    *,
    terminal: bool = False,
    assume_full_when_missing: bool = False,
) -> int:
    """Merge direct broker fill evidence without erasing a known partial fill."""
    requested = _requested_quantity(order)
    known_filled = _known_filled_quantity(order)
    reported_filled = max(0, min(int(snapshot.filled_quantity or 0), requested))
    if assume_full_when_missing and reported_filled <= 0:
        reported_filled = requested
    filled = max(known_filled, reported_filled)
    order.filled_quantity = filled

    reported_remaining = max(0, int(snapshot.remaining_quantity or 0))
    if terminal:
        # For a partial terminal order, retain the cancelled/unfilled remainder
        # for auditability; a zero-fill cancellation has no remaining live qty.
        order.remaining_quantity = max(0, requested - filled) if filled > 0 else 0
    elif reported_remaining <= 0:
        order.remaining_quantity = max(0, requested - filled)
    else:
        order.remaining_quantity = min(max(0, requested - filled), reported_remaining)
    avg_fill_price = _to_float(snapshot.avg_fill_price)
    if avg_fill_price > 0:
        order.avg_fill_price = avg_fill_price
    return filled


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
    reconciled = list(orders)
    open_by_symbol_and_side: Dict[tuple[str, OrderSide], List[BrokerOrder]] = {}
    for order in reconciled:
        if order.status not in OPEN_ORDER_STATUSES:
            continue
        open_by_symbol_and_side.setdefault((order.symbol, order.side), []).append(order)

    for (symbol, side), grouped_orders in open_by_symbol_and_side.items():
        grouped_orders = sorted(grouped_orders, key=_order_sort_key)
        current_qty, current_avg, _ = _holding_for_symbol(snapshot, symbol)

        if previous_snapshot is None:
            # With no baseline, holdings alone cannot distinguish this order
            # from a pre-existing/manual position.  Preserve an already known
            # partial fill, but do not manufacture a new fill.  The sole
            # historical exception is a single known partial sell whose entire
            # position has disappeared.
            if (
                side == OrderSide.SELL
                and len(grouped_orders) == 1
                and current_qty <= 0
                and _known_filled_quantity(grouped_orders[0]) > 0
            ):
                _mark_fill(
                    grouped_orders[0],
                    _requested_quantity(grouped_orders[0]),
                    current_avg,
                    snapshot,
                )
                continue
            for order in grouped_orders:
                if _known_filled_quantity(order) > 0:
                    _mark_fill(order, _known_filled_quantity(order), current_avg, snapshot)
                else:
                    _mark_working_if_unfilled(order, snapshot)
            continue

        previous_qty, _, _ = _holding_for_symbol(previous_snapshot, symbol)
        if side == OrderSide.BUY:
            confirmed_delta = max(0, int(round(current_qty - previous_qty)))
        else:
            confirmed_delta = max(0, int(round(previous_qty - current_qty)))

        # A holdings delta is aggregate evidence, not evidence that every open
        # order for the same symbol filled.  Allocate it once across the FIFO
        # open orders so the total locally confirmed quantity never exceeds the
        # broker-observed position change.
        unallocated_delta = confirmed_delta
        for order in grouped_orders:
            known_filled = _known_filled_quantity(order)
            available_quantity = max(0, _requested_quantity(order) - known_filled)
            allocated_quantity = min(available_quantity, unallocated_delta)
            if allocated_quantity > 0:
                _mark_fill(
                    order,
                    known_filled + allocated_quantity,
                    current_avg,
                    snapshot,
                )
                unallocated_delta -= allocated_quantity
            elif known_filled > 0:
                _mark_fill(order, known_filled, current_avg, snapshot)
            else:
                _mark_working_if_unfilled(order, snapshot)

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
        filled = _apply_broker_fill_progress(
            order,
            snapshot,
            terminal=True,
            assume_full_when_missing=True,
        )
        # A malformed broker row that claims FILLED but reports a lower
        # quantity must not invent the missing shares locally.
        order.status = (
            OrderStatus.FILLED
            if filled >= _requested_quantity(order)
            else OrderStatus.PARTIALLY_FILLED
        )
    elif snapshot.status == OrderStatus.PARTIALLY_FILLED:
        _apply_broker_fill_progress(order, snapshot)
        order.status = (
            OrderStatus.PARTIALLY_FILLED
            if order.filled_quantity > 0
            else OrderStatus.WORKING
        )
    elif snapshot.status in {OrderStatus.WORKING, OrderStatus.ACCEPTED}:
        _apply_broker_fill_progress(order, snapshot)
        # A broad working response can omit fills.  Retain the stronger local
        # evidence rather than downgrading a known partial order to WORKING.
        order.status = (
            OrderStatus.PARTIALLY_FILLED
            if order.filled_quantity > 0
            else snapshot.status
        )
    elif snapshot.status == OrderStatus.CANCEL_REQUESTED:
        _apply_broker_fill_progress(order, snapshot)
        order.status = OrderStatus.CANCEL_REQUESTED
    elif snapshot.status in {
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }:
        # A terminal cancellation/rejection can still contain confirmed shares.
        # Keep both the terminal state and the partial fill so downstream
        # position application can safely apply the executed quantity once.
        _apply_broker_fill_progress(order, snapshot, terminal=True)
        order.status = snapshot.status
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
        return None
    matches = [snapshot for snapshot in snapshots if snapshot.symbol == order.symbol and snapshot.side == order.side]
    if matches:
        return max(matches, key=lambda snapshot: priority.get(snapshot.status, 0))
    return max(snapshots, key=lambda snapshot: priority.get(snapshot.status, 0))


def query_and_reconcile_unresolved_orders(
    *,
    environment: Optional[str] = None,
    account_no: Optional[str] = None,
    symbol: Optional[str] = None,
    execution_policy: Optional[str] = None,
    path: Path = ORDERS_FILE,
    broker: Optional[Broker] = None,
) -> List[BrokerOrder]:
    """Query the broker for unresolved local orders and persist updates."""
    broker = KisBroker() if broker is None else broker

    environment_key = str(environment or "").upper()
    account_key = str(account_no or "")
    symbol_key = str(symbol or "").upper()
    execution_policy_key = str(execution_policy or "").upper()
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
        if execution_policy_key and order.execution_policy != execution_policy_key:
            continue

        start, end = _date_window_for_order(order)
        try:
            snapshots = broker.get_order(
                environment=order.environment,
                account_no=order.account_no,
                is_reserved=order.execution_policy == RESERVED_MOO_EXECUTION,
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
            updated = mutate_order(
                order.client_order_id,
                lambda current, value=snapshot: reconcile_order_with_broker_snapshot(
                    current, value
                ),
                path=path,
            )
            if updated is not None:
                updated_orders.append(updated)
        except Exception as exc:
            note = f"Broker order query failed: {exc}"

            def record_query_failure(current: BrokerOrder) -> BrokerOrder:
                current.error_message = (
                    f"{current.error_message}; {note}"
                    if current.error_message
                    else note
                )
                current.touch()
                return current

            mutate_order(order.client_order_id, record_query_failure, path=path)
            continue

    return updated_orders


def cancel_and_reconcile_order(
    client_order_id: str,
    *,
    path: Path = ORDERS_FILE,
    broker: Optional[Broker] = None,
) -> BrokerOrder:
    """Cancel one known broker-side open order and persist the local status."""
    broker = KisBroker() if broker is None else broker

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
    if target.execution_policy == RESERVED_MOO_EXECUTION:
        try:
            submitted = datetime.fromisoformat(
                str(target.submitted_at).replace("Z", "+00:00")
            )
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            reservation_date = submitted.astimezone(
                ZoneInfo("Asia/Seoul")
            ).strftime("%Y%m%d")
        except (TypeError, ValueError):
            reservation_date = datetime.now(ZoneInfo("Asia/Seoul")).strftime(
                "%Y%m%d"
            )
        snapshot = broker.cancel_order(
            environment=target.environment,
            account_no=target.account_no,
            is_reserved=True,
            broker_order_id=target.broker_order_id,
            reservation_date=reservation_date,
        )
        snapshot.symbol = target.symbol
    else:
        snapshot = broker.cancel_order(
            environment=target.environment,
            account_no=target.account_no,
            is_reserved=False,
            symbol=target.symbol,
            broker_order_id=target.broker_order_id,
            quantity=quantity,
            side=target.side.value,
            exchange=target.exchange or "NASD",
        )
    updated = mutate_order(
        target.client_order_id,
        lambda current: reconcile_order_with_broker_snapshot(current, snapshot),
        path=path,
    )
    if updated is None:
        raise ValueError(f"Order {client_order_id} was not found after cancellation")
    return updated
