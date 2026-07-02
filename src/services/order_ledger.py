"""Persistent local order ledger for submitted broker orders."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Optional

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus, is_open_status
from src.utils.storage import load_json, save_json


ORDERS_FILE = Path("data/orders.json")


def load_orders(path: Path = ORDERS_FILE) -> List[BrokerOrder]:
    data = load_json(path, {"orders": []})
    raw_orders = data.get("orders", data) if isinstance(data, dict) else data
    if not isinstance(raw_orders, list):
        return []

    orders: List[BrokerOrder] = []
    for raw_order in raw_orders:
        if not isinstance(raw_order, dict):
            continue
        try:
            order = BrokerOrder.from_dict(raw_order)
        except (TypeError, ValueError):
            continue
        if order.client_order_id:
            orders.append(order)
    return orders


def save_orders(orders: Iterable[BrokerOrder], path: Path = ORDERS_FILE) -> None:
    save_json(path, {"orders": [order.to_dict() for order in orders]})


def load_order_ledger(path: Path = ORDERS_FILE) -> List[BrokerOrder]:
    return load_orders(path)


def save_order_ledger(orders: Iterable[BrokerOrder], path: Path = ORDERS_FILE) -> None:
    save_orders(orders, path)


def append_order(order: BrokerOrder, path: Path = ORDERS_FILE) -> BrokerOrder:
    orders = load_orders(path)
    if any(existing.client_order_id == order.client_order_id for existing in orders):
        return upsert_order(order, path=path)
    orders.append(order)
    save_orders(orders, path)
    return order


def upsert_order(order: BrokerOrder, path: Path = ORDERS_FILE) -> BrokerOrder:
    orders = load_orders(path)
    for index, existing in enumerate(orders):
        if existing.client_order_id == order.client_order_id:
            order.touch()
            orders[index] = order
            save_orders(orders, path)
            return order
    order.touch()
    orders.append(order)
    save_orders(orders, path)
    return order


def update_order(
    order_or_id: BrokerOrder | str,
    updates: Optional[dict] = None,
    path: Path = ORDERS_FILE,
    **fields: Any,
) -> Optional[BrokerOrder]:
    orders = load_orders(path)
    updated: Optional[BrokerOrder] = None
    if isinstance(order_or_id, BrokerOrder):
        return upsert_order(order_or_id, path=path)

    client_order_id = str(order_or_id or "")
    merged_fields = dict(updates or {})
    merged_fields.update(fields)
    for order in orders:
        if order.client_order_id != client_order_id:
            continue
        for field_name, value in merged_fields.items():
            if field_name == "status":
                value = value if isinstance(value, OrderStatus) else OrderStatus(str(value).upper())
            elif field_name == "side":
                value = value if isinstance(value, OrderSide) else OrderSide(str(value).upper())
            elif field_name == "intent":
                value = value if isinstance(value, OrderIntent) else OrderIntent(str(value).upper())
            setattr(order, field_name, value)
        order.touch()
        updated = order
        break
    if updated is not None:
        save_orders(orders, path)
    return updated


def find_order(client_order_id: str, path: Path = ORDERS_FILE) -> Optional[BrokerOrder]:
    client_order_id = str(client_order_id or "")
    return next((order for order in load_orders(path) if order.client_order_id == client_order_id), None)


def find_open_orders(
    orders: Optional[Iterable[BrokerOrder]] = None,
    environment: str = "",
    account_no: str = "",
    symbol: Optional[str] = None,
    side: Optional[OrderSide | str] = None,
    intent: Optional[OrderIntent | str] = None,
    path: Path = ORDERS_FILE,
) -> List[BrokerOrder]:
    if isinstance(orders, str):
        environment, account_no, orders = orders, environment, None

    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    symbol = str(symbol or "").upper()
    side_value = None
    if side is not None:
        side_value = side if isinstance(side, OrderSide) else OrderSide(str(side).upper())
    intent_value = None
    if intent is not None:
        intent_value = intent if isinstance(intent, OrderIntent) else OrderIntent(str(intent).upper())

    matches: List[BrokerOrder] = []
    source_orders = list(orders) if orders is not None else load_orders(path)
    for order in source_orders:
        if not is_open_status(order.status):
            continue
        if environment and order.environment != environment:
            continue
        if account_no and str(order.account_no or "") != account_no:
            continue
        if symbol and order.symbol != symbol:
            continue
        if side_value is not None and order.side != side_value:
            continue
        if intent_value is not None and order.intent != intent_value:
            continue
        matches.append(order)
    return matches


def has_open_order(
    environment: str,
    account_no: str,
    symbol: str,
    side: Optional[OrderSide | str] = None,
    intent: Optional[OrderIntent | str] = None,
    path: Path = ORDERS_FILE,
) -> bool:
    return bool(
        find_open_orders(
            environment=environment,
            account_no=account_no,
            symbol=symbol,
            side=side,
            intent=intent,
            path=path,
        )
    )


def has_open_order_for_buylist_item(
    environment: str,
    account_no: str,
    symbol: str,
    side: Optional[OrderSide | str] = None,
    path: Path = ORDERS_FILE,
) -> bool:
    return has_open_order(environment, account_no, symbol, side=side, path=path)
