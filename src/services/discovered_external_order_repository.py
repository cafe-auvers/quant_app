"""Durable persistence for
:class:`~src.core.discovered_external_order.DiscoveredExternalOrder`.

``docs/kanban_production_readiness.md``, Workstream 2 (A4b), revision 3.2:
"``DiscoveredExternalOrder``... need[s] real, restart-surviving tables...,
not merely in-memory dataclasses -- INV-22 [is] only actually guaranteed
once these survive a crash." Same blob-plus-key-columns pattern as
:mod:`src.services.execution_order_repository` and
:mod:`src.services.trade_card_repository`.

:func:`adopt_external_order_in_db` is the durable counterpart to
:func:`src.core.discovered_external_order.adopt_external_order` (which is
still the pure business-logic function this module calls) -- it wraps the
whole adoption (verify still ``DISCOVERED_UNOWNED``, insert the new
``execution_orders`` row, update the ``discovered_external_orders`` row's
``disposition``) in one atomic transaction, per revision 3.2's explicit
requirement that adoption never commit one half without the other.
"""
from __future__ import annotations

import json
import logging
import threading
import weakref
from datetime import datetime
from typing import Any, Dict, FrozenSet, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from src.core.discovered_external_order import (
    DiscoveredExternalOrder,
    ExternalOrderDisposition,
    adopt_external_order,
)
from src.core.execution_order_record import AdoptedOrderPermission, ExecutionOrderRecord, ExecutionOrderStatus
from src.core.order_state import OrderSide
from src.services.execution_order_repository import insert_execution_order

logger = logging.getLogger(__name__)

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


class DuplicateExternalOrderError(RuntimeError):
    """A row for this ``external_order_id`` already exists."""


class ExternalOrderNotFoundError(RuntimeError):
    pass


class ExternalOrderVersionConflictError(RuntimeError):
    """Optimistic-concurrency conflict -- another writer (e.g. a
    concurrent adoption attempt, or reconciliation marking this
    ``DISMISSED_TERMINAL``) already changed this row."""


def _get_discovered_external_orders_table(metadata: MetaData) -> Table:
    return Table(
        "discovered_external_orders",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("external_order_id", String(64), nullable=False),
        Column("broker_order_id", String(64), nullable=False),
        Column("environment", String(10), nullable=False),
        Column("account_no", String(32), nullable=False),
        Column("symbol", String(20), nullable=False),
        Column("disposition", String(32), nullable=False),
        Column("version", BigInteger, nullable=False, server_default="1"),
        Column("payload", Text(length=16_777_215), nullable=False),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint("external_order_id", name="uq_discovered_external_orders_external_order_id"),
    )


def ensure_discovered_external_orders_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_discovered_external_orders_table(metadata)
    if engine in _ensured_engines:
        return table
    with _ensure_lock:
        if engine in _ensured_engines:
            return table
        metadata.create_all(engine)
        _ensured_engines.add(engine)
    return table


def _server_now(engine: Engine):
    if engine.dialect.name == "mysql":
        return func.utc_timestamp(6)
    return func.current_timestamp()


# --- (de)serialization --------------------------------------------------


def _order_to_payload(order: DiscoveredExternalOrder) -> Dict[str, Any]:
    return {
        "environment": order.environment,
        "account_no": order.account_no,
        "symbol": order.symbol,
        "side": order.side.value,
        "broker_order_id": order.broker_order_id,
        "quantity_requested": order.quantity_requested,
        "filled_quantity": order.filled_quantity,
        "limit_price": order.limit_price,
        "broker_status": order.broker_status.value,
        "external_order_id": order.external_order_id,
        "discovered_at": order.discovered_at,
        "disposition": order.disposition.value,
        "adopted_at": order.adopted_at,
        "adopted_by": order.adopted_by,
        "redacted_response": order.redacted_response,
        "response_hash": order.response_hash,
        "version": order.version,
    }


def _payload_to_order(payload: Dict[str, Any]) -> DiscoveredExternalOrder:
    order = DiscoveredExternalOrder(
        environment=payload["environment"],
        account_no=payload["account_no"],
        symbol=payload["symbol"],
        side=OrderSide(payload["side"]),
        broker_order_id=payload["broker_order_id"],
        quantity_requested=payload.get("quantity_requested", 0),
        filled_quantity=payload.get("filled_quantity", 0),
        limit_price=payload.get("limit_price", 0.0),
        broker_status=ExecutionOrderStatus(payload.get("broker_status", ExecutionOrderStatus.WORKING.value)),
        redacted_response=payload.get("redacted_response") or {},
    )
    order.external_order_id = payload["external_order_id"]
    order.discovered_at = payload.get("discovered_at", order.discovered_at)
    order.disposition = ExternalOrderDisposition(payload["disposition"])
    order.adopted_at = payload.get("adopted_at")
    order.adopted_by = payload.get("adopted_by", "")
    order.response_hash = payload.get("response_hash", "")
    order.version = payload.get("version", 1)
    return order


def _row_to_order(row) -> DiscoveredExternalOrder:
    return _payload_to_order(json.loads(row.payload))


# --- shared-transaction primitives -------------------------------------


def insert_discovered_external_order(
    conn: Connection, order: DiscoveredExternalOrder
) -> DiscoveredExternalOrder:
    table = _get_discovered_external_orders_table(MetaData())
    engine = conn.engine
    try:
        conn.execute(
            table.insert().values(
                external_order_id=order.external_order_id,
                broker_order_id=order.broker_order_id,
                environment=order.environment,
                account_no=order.account_no,
                symbol=order.symbol,
                disposition=order.disposition.value,
                version=order.version,
                payload=json.dumps(_order_to_payload(order), separators=(",", ":")),
                updated_at=_server_now(engine),
            )
        )
    except IntegrityError as exc:
        raise DuplicateExternalOrderError(
            f"DiscoveredExternalOrder for external_order_id={order.external_order_id!r} already exists"
        ) from exc
    return order


def update_discovered_external_order(
    conn: Connection, order: DiscoveredExternalOrder, *, expected_version: int
) -> DiscoveredExternalOrder:
    table = _get_discovered_external_orders_table(MetaData())
    engine = conn.engine
    next_version = int(expected_version) + 1
    order.version = next_version
    result = conn.execute(
        table.update()
        .where(
            table.c.external_order_id == order.external_order_id,
            table.c.version == int(expected_version),
        )
        .values(
            disposition=order.disposition.value,
            version=next_version,
            payload=json.dumps(_order_to_payload(order), separators=(",", ":")),
            updated_at=_server_now(engine),
        )
    )
    if result.rowcount == 0:
        existing = conn.execute(
            select(table).where(table.c.external_order_id == order.external_order_id)
        ).first()
        if existing is None:
            raise ExternalOrderNotFoundError(
                f"No DiscoveredExternalOrder for external_order_id={order.external_order_id!r}"
            )
        raise ExternalOrderVersionConflictError(
            f"external_order_id={order.external_order_id!r} version conflict "
            f"(expected {expected_version}, stored {existing.version})"
        )
    return order


def get_discovered_external_order(
    conn: Connection, external_order_id: str
) -> Optional[DiscoveredExternalOrder]:
    table = _get_discovered_external_orders_table(MetaData())
    row = conn.execute(
        select(table).where(table.c.external_order_id == external_order_id)
    ).first()
    return _row_to_order(row) if row is not None else None


# --- standalone convenience wrappers ------------------------------------


def record_discovered_external_order(
    engine: Engine, order: DiscoveredExternalOrder
) -> DiscoveredExternalOrder:
    ensure_discovered_external_orders_table(engine)
    with engine.begin() as conn:
        return insert_discovered_external_order(conn, order)


def fetch_discovered_external_order(
    engine: Engine, external_order_id: str
) -> Optional[DiscoveredExternalOrder]:
    ensure_discovered_external_orders_table(engine)
    with engine.begin() as conn:
        return get_discovered_external_order(conn, external_order_id)


# --- atomic adoption (revision 3.2) -------------------------------------


def adopt_external_order_in_db(
    engine: Engine,
    external_order_id: str,
    *,
    adopted_by: str,
    permissions: FrozenSet[AdoptedOrderPermission] = frozenset(),
    owner_device_id: str = "",
) -> ExecutionOrderRecord:
    """The durable, atomic counterpart to
    :func:`src.core.discovered_external_order.adopt_external_order`
    (revision 3.2): locks the external-order row (optimistic version
    read + conditional update), verifies it is still
    ``DISCOVERED_UNOWNED``, creates the new ``execution_orders`` row, and
    updates the ``discovered_external_orders`` row's ``disposition`` --
    all inside one transaction, or none of it. Raises
    :class:`ExternalOrderVersionConflictError` if another writer changed
    the row concurrently (e.g. a second adoption attempt, or
    reconciliation marking it ``DISMISSED_TERMINAL``) between the read and
    the write -- the caller must reload and re-decide, never blindly retry
    the same adoption.
    """
    ensure_discovered_external_orders_table(engine)
    ensure_execution_orders_table_lazy(engine)

    with engine.begin() as conn:
        external_order = get_discovered_external_order(conn, external_order_id)
        if external_order is None:
            raise ExternalOrderNotFoundError(
                f"No DiscoveredExternalOrder for external_order_id={external_order_id!r}"
            )
        expected_version = external_order.version

        # The pure business-logic function: validates disposition, builds
        # the new ExecutionOrderRecord, and mutates external_order's own
        # disposition/adopted_at/adopted_by in memory. Nothing is
        # persisted yet -- both writes below happen in this same
        # transaction, or neither does.
        new_record = adopt_external_order(
            external_order,
            adopted_by=adopted_by,
            permissions=permissions,
            owner_device_id=owner_device_id,
        )

        insert_execution_order(conn, new_record)
        update_discovered_external_order(conn, external_order, expected_version=expected_version)

    return new_record


def ensure_execution_orders_table_lazy(engine: Engine) -> None:
    """Avoids a hard import-time dependency loop between the two
    repository modules while still guaranteeing the execution_orders
    table exists before adoption writes to it."""
    from src.services.execution_order_repository import ensure_execution_orders_table

    ensure_execution_orders_table(engine)
