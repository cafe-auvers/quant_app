"""Persistent local order ledger for submitted broker orders."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, TypeVar

from src.core.order_state import (BrokerOrder, OrderIntent, OrderSide,
                                  OrderStatus, is_open_status)
from src.utils.config import DATA_DIR
from src.utils.file_lock import exclusive_file_lock
from src.utils.storage import save_json

ORDERS_FILE = DATA_DIR / "orders.json"
logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class OrderLedgerCorruptionError(RuntimeError):
    """Raised when existing order-ledger state cannot be trusted."""


# Re-exported under its original private name: this module's lock behavior
# moved to src.utils.file_lock so src.services.capital_allocator can share
# it, but existing callers/tests still reach it as
# ``order_ledger._exclusive_ledger_lock``.
_exclusive_ledger_lock = exclusive_file_lock


def _parse_orders_payload(data: Any, source: Path) -> List[BrokerOrder]:
    if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
        raise OrderLedgerCorruptionError(
            f"Order ledger {source} must contain an 'orders' list"
        )

    orders: List[BrokerOrder] = []
    seen_ids: set[str] = set()
    for index, raw_order in enumerate(data["orders"]):
        if not isinstance(raw_order, dict):
            raise OrderLedgerCorruptionError(
                f"Order ledger {source} record {index} is not an object"
            )
        try:
            client_order_id = str(raw_order.get("client_order_id") or "").strip()
            if not client_order_id:
                raise ValueError("client_order_id is required")
            if client_order_id in seen_ids:
                raise ValueError(f"duplicate client_order_id {client_order_id!r}")
            OrderSide(str(raw_order.get("side") or "").upper())
            OrderIntent(str(raw_order.get("intent") or "").upper())
            OrderStatus(str(raw_order.get("status") or "").upper())
            order = BrokerOrder.from_dict(raw_order)
        except (TypeError, ValueError) as exc:
            identity = raw_order.get("client_order_id") or raw_order.get("symbol") or index
            raise OrderLedgerCorruptionError(
                f"Order ledger {source} has invalid record {identity!r}: {exc}"
            ) from exc
        seen_ids.add(order.client_order_id)
        orders.append(order)
    return orders


def _load_orders_unlocked(path: Path) -> List[BrokerOrder]:
    path = Path(path)
    backup_path = path.with_suffix(path.suffix + ".bak")
    candidates = [path, backup_path]
    existing_candidates = [candidate for candidate in candidates if candidate.exists()]
    if not existing_candidates:
        return []

    failures: list[str] = []
    for candidate in existing_candidates:
        try:
            with candidate.open("r", encoding="utf-8") as file:
                payload = json.load(file)
            orders = _parse_orders_payload(payload, candidate)
        except (OSError, json.JSONDecodeError, OrderLedgerCorruptionError) as exc:
            failures.append(f"{candidate.name}: {exc}")
            continue
        if candidate == backup_path:
            logger.warning(
                "Recovered order ledger from backup %s because the primary ledger "
                "was missing or invalid",
                backup_path,
            )
        return orders

    detail = "; ".join(failures) or "no readable ledger candidates"
    raise OrderLedgerCorruptionError(
        f"Order ledger is present but unreadable; live order submission is blocked. {detail}"
    )


def _save_orders_unlocked(orders: Iterable[BrokerOrder], path: Path) -> None:
    _quarantine_invalid_primary(path)
    save_json(path, {"orders": [order.to_dict() for order in orders]})


def load_orders(path: Path = ORDERS_FILE) -> List[BrokerOrder]:
    with _exclusive_ledger_lock(path):
        return _load_orders_unlocked(path)


def save_orders(orders: Iterable[BrokerOrder], path: Path = ORDERS_FILE) -> None:
    with _exclusive_ledger_lock(path):
        _load_orders_unlocked(path)
        _save_orders_unlocked(orders, path)


def _quarantine_invalid_primary(path: Path) -> Optional[Path]:
    """Move an invalid primary aside before save_json can copy it over a good backup."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            _parse_orders_payload(json.load(file), path)
        return None
    except (OSError, json.JSONDecodeError, OrderLedgerCorruptionError):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            quarantine_path = path.with_name(f"{path.name}.corrupt-{digest}")
            if quarantine_path.exists():
                quarantine_path = path.with_name(
                    f"{path.name}.corrupt-{digest}-{time.time_ns()}"
                )
            path.replace(quarantine_path)
        except OSError as exc:
            raise OrderLedgerCorruptionError(
                f"Could not preserve invalid primary order ledger {path}: {exc}"
            ) from exc
        logger.error(
            "Moved invalid primary order ledger %s to %s before recovery save",
            path,
            quarantine_path,
        )
        return quarantine_path


def load_order_ledger(path: Path = ORDERS_FILE) -> List[BrokerOrder]:
    return load_orders(path)


def save_order_ledger(orders: Iterable[BrokerOrder], path: Path = ORDERS_FILE) -> None:
    save_orders(orders, path)


def append_order(order: BrokerOrder, path: Path = ORDERS_FILE) -> BrokerOrder:
    with _exclusive_ledger_lock(path):
        orders = _load_orders_unlocked(path)
        for index, existing in enumerate(orders):
            if existing.client_order_id == order.client_order_id:
                order.touch()
                orders[index] = order
                _save_orders_unlocked(orders, path)
                return order
        orders.append(order)
        _save_orders_unlocked(orders, path)
        return order


def reserve_order_if_no_matching_open(
    order: BrokerOrder,
    *,
    allow_duplicate: bool = False,
    path: Path = ORDERS_FILE,
) -> Optional[BrokerOrder]:
    """Atomically check for a matching open order and reserve a new intent."""
    with _exclusive_ledger_lock(path):
        orders = _load_orders_unlocked(path)
        if not allow_duplicate:
            for existing in orders:
                if (
                    is_open_status(existing.status)
                    and existing.environment == order.environment
                    and str(existing.account_no or "") == str(order.account_no or "")
                    and existing.symbol == order.symbol
                    and existing.side == order.side
                    and existing.intent == order.intent
                ):
                    return existing
        if not any(existing.client_order_id == order.client_order_id for existing in orders):
            orders.append(order)
            _save_orders_unlocked(orders, path)
        return None


def upsert_order(order: BrokerOrder, path: Path = ORDERS_FILE) -> BrokerOrder:
    with _exclusive_ledger_lock(path):
        orders = _load_orders_unlocked(path)
        for index, existing in enumerate(orders):
            if existing.client_order_id == order.client_order_id:
                order.touch()
                orders[index] = order
                _save_orders_unlocked(orders, path)
                return order
        order.touch()
        orders.append(order)
        _save_orders_unlocked(orders, path)
        return order


def mutate_order(
    client_order_id: str,
    mutation: Callable[[BrokerOrder], _T],
    *,
    path: Path = ORDERS_FILE,
) -> Optional[_T]:
    """Apply one in-memory mutation while holding the complete ledger transaction."""
    with _exclusive_ledger_lock(path):
        orders = _load_orders_unlocked(path)
        for order in orders:
            if order.client_order_id != str(client_order_id or ""):
                continue
            result = mutation(order)
            _save_orders_unlocked(orders, path)
            return result
    return None


def merge_orders(
    incoming_orders: Iterable[BrokerOrder],
    *,
    path: Path = ORDERS_FILE,
) -> List[BrokerOrder]:
    """Merge records by client ID without overwriting unrelated concurrent updates."""
    incoming = list(incoming_orders)
    with _exclusive_ledger_lock(path):
        orders = _load_orders_unlocked(path)
        indexes = {order.client_order_id: index for index, order in enumerate(orders)}
        for order in incoming:
            order.touch()
            index = indexes.get(order.client_order_id)
            if index is None:
                indexes[order.client_order_id] = len(orders)
                orders.append(order)
            else:
                orders[index] = order
        _save_orders_unlocked(orders, path)
    return incoming


def update_order(
    order_or_id: BrokerOrder | str,
    updates: Optional[dict] = None,
    path: Path = ORDERS_FILE,
    **fields: Any,
) -> Optional[BrokerOrder]:
    if isinstance(order_or_id, BrokerOrder):
        return upsert_order(order_or_id, path=path)

    client_order_id = str(order_or_id or "")
    merged_fields = dict(updates or {})
    merged_fields.update(fields)

    def apply_updates(order: BrokerOrder) -> BrokerOrder:
        for field_name, value in merged_fields.items():
            if field_name == "status":
                value = value if isinstance(value, OrderStatus) else OrderStatus(str(value).upper())
            elif field_name == "side":
                value = value if isinstance(value, OrderSide) else OrderSide(str(value).upper())
            elif field_name == "intent":
                value = value if isinstance(value, OrderIntent) else OrderIntent(str(value).upper())
            setattr(order, field_name, value)
        order.touch()
        return order

    return mutate_order(client_order_id, apply_updates, path=path)


def clear_unknown_submission_order(
    client_order_id: str,
    *,
    verified: bool = False,
    path: Path = ORDERS_FILE,
) -> Optional[BrokerOrder]:
    """Close an unknown submission only after external broker verification."""
    if not verified:
        raise ValueError("verified=True is required after manual KIS account/order verification")

    def clear(target: BrokerOrder) -> Optional[BrokerOrder]:
        if target.status != OrderStatus.UNKNOWN_SUBMISSION_STATE:
            return None
        target.status = OrderStatus.EXPIRED
        note = "Manually cleared UNKNOWN_SUBMISSION_STATE after KIS verification."
        target.error_message = (
            f"{target.error_message}; {note}" if target.error_message else note
        )
        target.touch()
        return target

    return mutate_order(client_order_id, clear, path=path)


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
