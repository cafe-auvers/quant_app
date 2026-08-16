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

Exact broker-order identity uniqueness is enforced by a real database
``UNIQUE`` constraint on ``broker_identity_key`` (revision 3.2) -- every
``DiscoveredExternalOrder`` always has a real ``broker_order_id``
(enforced by the dataclass itself), so this column is always non-null,
unlike ``execution_orders.broker_identity_key`` which is only populated
once identity is confirmed EXACT. This closes the same
"reconciliation sweep runs twice and discovers the same real broker order
under two different ``external_order_id``s" race the ``execution_orders``
table's own ``broker_identity_key`` constraint closes for the app's own
orders -- see :mod:`src.services.execution_order_repository`'s module
docstring for the full race-condition rationale.
"""
from __future__ import annotations

import json
import logging
import threading
import weakref
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
from src.core.execution_order_record import (
    AdoptedOrderPermission,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    compute_broker_identity_key,
)
from src.core.order_state import OrderSide
from src.services.execution_order_repository import insert_execution_order

logger = logging.getLogger(__name__)

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


class DuplicateExternalOrderError(RuntimeError):
    """A row for this ``external_order_id`` already exists."""


class DuplicateBrokerOrderDiscoveryError(RuntimeError):
    """This exact real broker order (``environment``/``account_no``/
    ``broker_order_id``) was already discovered and stored under a
    *different* ``external_order_id`` -- a second, independent discovery
    of the same broker order (e.g. two overlapping reconciliation sweeps)
    must never create a second row; backed by a real database ``UNIQUE``
    constraint on ``broker_identity_key``, not only an application-level
    check."""


class ExternalOrderNotFoundError(RuntimeError):
    pass


class ExternalOrderVersionConflictError(RuntimeError):
    """Optimistic-concurrency conflict -- another writer (e.g. a
    concurrent adoption attempt, or reconciliation marking this
    ``DISMISSED_TERMINAL``) already changed this row."""


class ImmutableFieldChangedError(RuntimeError):
    """``environment``/``account_no``/``symbol``/``broker_order_id``
    changed between the read and the write of the same
    ``external_order_id`` (revision 3.2) -- these identify *which real
    broker order* this permanent audit record describes, and must never
    silently drift."""


class ActiveExternalOrderFenceError(RuntimeError):
    """A still-unowned broker order durably fences this account/symbol."""


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
        # Always non-null: every DiscoveredExternalOrder always carries a
        # real broker_order_id (see the dataclass's own __post_init__).
        Column("broker_identity_key", String(200), nullable=False),
        Column("disposition", String(32), nullable=False),
        Column("version", BigInteger, nullable=False, server_default="1"),
        Column("payload", Text(length=16_777_215), nullable=False),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint("external_order_id", name="uq_discovered_external_orders_external_order_id"),
        UniqueConstraint(
            "broker_identity_key", name="uq_discovered_external_orders_broker_identity_key"
        ),
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


def _identity_key(order: DiscoveredExternalOrder) -> str:
    return compute_broker_identity_key(order.environment, order.account_no, order.broker_order_id)


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


def _diagnose_and_raise_integrity_error(
    conn: Connection, order: DiscoveredExternalOrder, exc: IntegrityError
) -> None:
    """Turns a raw ``IntegrityError`` into the specific, typed exception it
    actually represents. Always raises; never returns normally."""
    table = _get_discovered_external_orders_table(MetaData())
    existing_by_external_id = conn.execute(
        select(table.c.id).where(table.c.external_order_id == order.external_order_id)
    ).first()
    if existing_by_external_id is not None:
        raise DuplicateExternalOrderError(
            f"DiscoveredExternalOrder for external_order_id={order.external_order_id!r} already exists"
        ) from exc

    existing_by_identity = conn.execute(
        select(table.c.external_order_id).where(table.c.broker_identity_key == _identity_key(order))
    ).first()
    if existing_by_identity is not None:
        raise DuplicateBrokerOrderDiscoveryError(
            f"broker_order_id={order.broker_order_id!r} was already discovered as "
            f"external_order_id={existing_by_identity.external_order_id!r}"
        ) from exc

    raise exc


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
                broker_identity_key=_identity_key(order),
                disposition=order.disposition.value,
                version=order.version,
                payload=json.dumps(_order_to_payload(order), separators=(",", ":")),
                updated_at=_server_now(engine),
            )
        )
    except IntegrityError as exc:
        _diagnose_and_raise_integrity_error(conn, order, exc)
    return order


def update_discovered_external_order(
    conn: Connection, order: DiscoveredExternalOrder, *, expected_version: int
) -> DiscoveredExternalOrder:
    """Optimistic-concurrency update -- two revision-3.2 corrections over
    the original version, mirroring
    :func:`src.services.execution_order_repository.update_execution_order`:
    ``order.version`` is assigned the new value only *after* the update is
    confirmed to have actually applied, and
    ``environment``/``account_no``/``symbol``/``broker_order_id`` are
    treated as immutable identity fields.
    """
    table = _get_discovered_external_orders_table(MetaData())
    engine = conn.engine

    current = conn.execute(
        select(
            table.c.environment, table.c.account_no, table.c.symbol,
            table.c.broker_order_id, table.c.version,
        ).where(table.c.external_order_id == order.external_order_id)
    ).first()
    if current is None:
        raise ExternalOrderNotFoundError(
            f"No DiscoveredExternalOrder for external_order_id={order.external_order_id!r}"
        )
    if (
        current.environment != order.environment
        or current.account_no != order.account_no
        or current.symbol != order.symbol
        or current.broker_order_id != order.broker_order_id
    ):
        raise ImmutableFieldChangedError(
            f"external_order_id={order.external_order_id!r}: environment/account_no/symbol/"
            "broker_order_id must not change after creation"
        )

    next_version = int(expected_version) + 1
    # Build the persisted payload with the *new* version already in it --
    # order.version is only mutated below, once the write is confirmed to
    # have applied, so it must not be read here.
    payload_dict = _order_to_payload(order)
    payload_dict["version"] = next_version
    try:
        result = conn.execute(
            table.update()
            .where(
                table.c.external_order_id == order.external_order_id,
                table.c.version == int(expected_version),
            )
            .values(
                disposition=order.disposition.value,
                version=next_version,
                payload=json.dumps(payload_dict, separators=(",", ":")),
                updated_at=_server_now(engine),
            )
        )
    except IntegrityError as exc:
        _diagnose_and_raise_integrity_error(conn, order, exc)
        raise  # unreachable -- _diagnose_and_raise_integrity_error always raises

    if result.rowcount == 0:
        raise ExternalOrderVersionConflictError(
            f"external_order_id={order.external_order_id!r} version conflict "
            f"(expected {expected_version}, stored {current.version})"
        )
    order.version = next_version
    return order


def get_discovered_external_order(
    conn: Connection, external_order_id: str
) -> Optional[DiscoveredExternalOrder]:
    table = _get_discovered_external_orders_table(MetaData())
    row = conn.execute(
        select(table).where(table.c.external_order_id == external_order_id)
    ).first()
    return _row_to_order(row) if row is not None else None


def get_discovered_external_order_by_broker_id(
    conn: Connection,
    *,
    environment: str,
    account_no: str,
    broker_order_id: str,
) -> Optional[DiscoveredExternalOrder]:
    table = _get_discovered_external_orders_table(MetaData())
    row = conn.execute(
        select(table).where(
            table.c.broker_identity_key
            == compute_broker_identity_key(
                environment, account_no, broker_order_id
            )
        )
    ).first()
    return _row_to_order(row) if row is not None else None


def require_no_active_unowned_external_order(
    conn: Connection,
    *,
    environment: str,
    account_no: str,
    symbol: str,
) -> None:
    """Execution-boundary fence; call inside the mutation transaction."""
    table = _get_discovered_external_orders_table(MetaData())
    terminal_statuses = {
        ExecutionOrderStatus.FILLED,
        ExecutionOrderStatus.CANCELLED,
        ExecutionOrderStatus.EXPIRED,
        ExecutionOrderStatus.REJECTED,
    }
    rows = conn.execute(
        select(table).where(
            table.c.environment == str(environment or "").upper(),
            table.c.account_no == str(account_no or ""),
            table.c.symbol == str(symbol or "").upper(),
            table.c.disposition
            == ExternalOrderDisposition.DISCOVERED_UNOWNED.value,
        )
    ).fetchall()
    active = None
    for row in rows:
        candidate = _row_to_order(row)
        if candidate.broker_status not in terminal_statuses:
            active = candidate
            break
    if active is not None:
        raise ActiveExternalOrderFenceError(
            f"{environment}/{account_no}/{symbol} is fenced by active unowned "
            f"broker_order_id={active.broker_order_id!r} "
            f"(external_order_id={active.external_order_id!r}); wait for a terminal "
            "broker observation or explicitly adopt the order"
        )


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


def save_discovered_external_order(
    engine: Engine,
    order: DiscoveredExternalOrder,
    *,
    expected_version: int,
) -> DiscoveredExternalOrder:
    ensure_discovered_external_orders_table(engine)
    with engine.begin() as conn:
        return update_discovered_external_order(
            conn, order, expected_version=expected_version
        )


def list_discovered_external_orders_for_account(
    engine: Engine,
    *,
    environment: str,
    account_no: str,
) -> list[DiscoveredExternalOrder]:
    """Return the permanent A4b audit records for one account."""
    table = ensure_discovered_external_orders_table(engine)
    with engine.begin() as conn:
        rows = conn.execute(
            select(table)
            .where(
                table.c.environment == str(environment or "").upper(),
                table.c.account_no == str(account_no or ""),
            )
            .order_by(table.c.id.asc())
        ).fetchall()
    return [_row_to_order(row) for row in rows]


def list_discovered_external_orders(
    engine: Engine, *, environment: Optional[str] = None
) -> list[DiscoveredExternalOrder]:
    """Return external-order audit rows, optionally for one environment."""
    table = ensure_discovered_external_orders_table(engine)
    statement = select(table)
    if environment is not None:
        statement = statement.where(
            table.c.environment == str(environment or "").upper()
        )
    with engine.begin() as conn:
        rows = conn.execute(statement.order_by(table.c.id.asc())).fetchall()
    return [_row_to_order(row) for row in rows]


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
