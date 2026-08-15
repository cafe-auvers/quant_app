"""Durable persistence for :class:`~src.core.execution_order_record.ExecutionOrderRecord`.

``docs/kanban_production_readiness.md``, Workstream 2 (A1), revision 3.2:
"``ExecutionOrderRecord``... need[s] real, restart-surviving tables..., not
merely in-memory dataclasses -- INV-1... [is] only actually guaranteed once
these survive a crash." Follows the same blob-plus-key-columns pattern
:mod:`src.services.trade_card_repository` already uses: natural-key/status
columns for querying and optimistic concurrency, one JSON ``payload``
column for the full record.

Every write primitive here accepts an already-open ``Connection`` so a
caller (the execution gateway, Workstream 3) can compose it into a single
transaction alongside the command journal
(:mod:`src.services.execution_command_repository`) and a capital
reservation, satisfying A1's atomicity requirement. Convenience wrappers
that open and commit their own transaction are provided for callers that
don't need cross-table atomicity.

This module is purely a repository: it does not decide what to submit,
validate gateway gates, or call the broker.
"""
from __future__ import annotations

import json
import logging
import threading
import weakref
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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

from src.core.execution_order_record import (
    AdoptedOrderPermission,
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    OrderOrigin,
)
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderIntent, OrderSide

logger = logging.getLogger(__name__)

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


class DuplicateExecutionOrderError(RuntimeError):
    """A row for this ``client_order_id`` already exists (A1/A5's
    idempotency guarantee applied to the order record itself)."""


class BrokerIdentityConflictError(RuntimeError):
    """Another *different* local record already holds this exact
    ``broker_order_id`` as ``EXACT`` -- a genuine ownership conflict, never
    silently allowed (mirrors :func:`~src.core.execution_order_record.mark_broker_identity_exact`'s
    same-record contradiction check, extended here across records)."""


class ExecutionOrderNotFoundError(RuntimeError):
    pass


class ExecutionOrderVersionConflictError(RuntimeError):
    """Optimistic-concurrency conflict -- another writer already changed
    this row. Callers must reload and retry, never blindly overwrite."""


def _get_execution_orders_table(metadata: MetaData) -> Table:
    return Table(
        "execution_orders",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("client_order_id", String(160), nullable=False),
        Column("environment", String(10), nullable=False),
        Column("account_no", String(32), nullable=False),
        Column("symbol", String(20), nullable=False),
        Column("status", String(32), nullable=False),
        Column("origin", String(24), nullable=False),
        Column("broker_identity_status", String(32), nullable=False),
        Column("broker_order_id", String(64), nullable=False, server_default=""),
        Column("recovery_state", String(40), nullable=False),
        Column("version", BigInteger, nullable=False, server_default="1"),
        Column("payload", Text(length=16_777_215), nullable=False),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint("client_order_id", name="uq_execution_orders_client_order_id"),
    )


def ensure_execution_orders_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_execution_orders_table(metadata)
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


def _record_to_payload(record: ExecutionOrderRecord) -> Dict[str, Any]:
    payload = {
        "environment": record.environment,
        "account_no": record.account_no,
        "symbol": record.symbol,
        "side": record.side.value,
        "intent": record.intent.value,
        "client_order_id": record.client_order_id,
        "broker_order_id": record.broker_order_id,
        "attempt_group_id": record.attempt_group_id,
        "attempt_number": record.attempt_number,
        "submitted_quantity": record.submitted_quantity,
        "submitted_limit_price": record.submitted_limit_price,
        "exchange": record.exchange,
        "execution_policy": record.execution_policy,
        "prepared_at": record.prepared_at,
        "submission_started_at": record.submission_started_at,
        "acknowledged_at": record.acknowledged_at,
        "market_session_date": record.market_session_date,
        "owner_device_id": record.owner_device_id,
        "lease_token": record.lease_token,
        "lease_epoch": record.lease_epoch,
        "status": record.status.value,
        "filled_quantity": record.filled_quantity,
        "remaining_quantity": record.remaining_quantity,
        "average_fill_price": record.average_fill_price,
        "cancel_requested_at": record.cancel_requested_at,
        "last_broker_seen_at": record.last_broker_seen_at,
        "last_reconciled_at": record.last_reconciled_at,
        "origin": record.origin.value,
        "broker_identity_status": record.broker_identity_status.value,
        "recovery_state": record.recovery_state.value,
        "adoption_permissions": sorted(perm.value for perm in record.adoption_permissions),
        "replaces_execution_order_id": record.replaces_execution_order_id,
        "adopted_from_external_order_id": record.adopted_from_external_order_id,
        "capital_reservation_id": record.capital_reservation_id,
        "raw_submission_hash": record.raw_submission_hash,
        "version": record.version,
    }
    return payload


def _payload_to_record(payload: Dict[str, Any]) -> ExecutionOrderRecord:
    return ExecutionOrderRecord(
        environment=payload["environment"],
        account_no=payload["account_no"],
        symbol=payload["symbol"],
        side=OrderSide(payload["side"]),
        intent=OrderIntent(payload["intent"]),
        client_order_id=payload["client_order_id"],
        broker_order_id=payload.get("broker_order_id", ""),
        attempt_group_id=payload.get("attempt_group_id", ""),
        attempt_number=payload.get("attempt_number", 1),
        submitted_quantity=payload.get("submitted_quantity", 0),
        submitted_limit_price=payload.get("submitted_limit_price", 0.0),
        exchange=payload.get("exchange", ""),
        execution_policy=payload.get("execution_policy", ""),
        prepared_at=payload.get("prepared_at", ""),
        submission_started_at=payload.get("submission_started_at"),
        acknowledged_at=payload.get("acknowledged_at"),
        market_session_date=payload.get("market_session_date"),
        owner_device_id=payload.get("owner_device_id", ""),
        lease_token=payload.get("lease_token", ""),
        lease_epoch=payload.get("lease_epoch", 0),
        status=ExecutionOrderStatus(payload["status"]),
        filled_quantity=payload.get("filled_quantity", 0),
        remaining_quantity=payload.get("remaining_quantity", 0),
        average_fill_price=payload.get("average_fill_price", 0.0),
        cancel_requested_at=payload.get("cancel_requested_at"),
        last_broker_seen_at=payload.get("last_broker_seen_at"),
        last_reconciled_at=payload.get("last_reconciled_at"),
        origin=OrderOrigin(payload["origin"]),
        broker_identity_status=BrokerIdentityStatus(payload["broker_identity_status"]),
        recovery_state=OrderRecoveryState(payload["recovery_state"]),
        adoption_permissions=frozenset(
            AdoptedOrderPermission(p) for p in payload.get("adoption_permissions", [])
        ),
        replaces_execution_order_id=payload.get("replaces_execution_order_id", ""),
        adopted_from_external_order_id=payload.get("adopted_from_external_order_id", ""),
        capital_reservation_id=payload.get("capital_reservation_id", ""),
        raw_submission_hash=payload.get("raw_submission_hash", ""),
        version=payload.get("version", 1),
    )


def _row_to_record(row) -> ExecutionOrderRecord:
    return _payload_to_record(json.loads(row.payload))


# --- shared-transaction primitives (revision 3.2) ---------------------------


def _check_broker_identity_conflict(conn: Connection, record: ExecutionOrderRecord) -> None:
    if record.broker_identity_status != BrokerIdentityStatus.EXACT or not record.broker_order_id:
        return
    table = _get_execution_orders_table(MetaData())
    existing = conn.execute(
        select(table.c.client_order_id).where(
            table.c.broker_order_id == record.broker_order_id,
            table.c.broker_identity_status == BrokerIdentityStatus.EXACT.value,
            table.c.client_order_id != record.client_order_id,
        )
    ).first()
    if existing is not None:
        raise BrokerIdentityConflictError(
            f"broker_order_id={record.broker_order_id!r} is already EXACT on "
            f"client_order_id={existing.client_order_id!r} -- cannot also claim it on "
            f"{record.client_order_id!r}"
        )


def insert_execution_order(conn: Connection, record: ExecutionOrderRecord) -> ExecutionOrderRecord:
    """A1: the durable ``ExecutionOrderRecord`` write, part of the atomic
    pre-submission transaction (command + reservation + this record).
    Takes an already-open ``Connection``. Raises
    :class:`DuplicateExecutionOrderError` on a repeated ``client_order_id``
    and :class:`BrokerIdentityConflictError` if another record already
    holds this exact ``broker_order_id``.
    """
    _check_broker_identity_conflict(conn, record)
    table = _get_execution_orders_table(MetaData())
    engine = conn.engine
    try:
        conn.execute(
            table.insert().values(
                client_order_id=record.client_order_id,
                environment=record.environment,
                account_no=record.account_no,
                symbol=record.symbol,
                status=record.status.value,
                origin=record.origin.value,
                broker_identity_status=record.broker_identity_status.value,
                broker_order_id=record.broker_order_id,
                recovery_state=record.recovery_state.value,
                version=record.version,
                payload=json.dumps(_record_to_payload(record), separators=(",", ":")),
                updated_at=_server_now(engine),
            )
        )
    except IntegrityError as exc:
        raise DuplicateExecutionOrderError(
            f"ExecutionOrderRecord for client_order_id={record.client_order_id!r} already exists"
        ) from exc
    return record


def update_execution_order(
    conn: Connection, record: ExecutionOrderRecord, *, expected_version: int
) -> ExecutionOrderRecord:
    """Optimistic-concurrency update, mirroring
    :func:`src.services.trade_card_repository.update_trade_card`'s
    pattern. Raises :class:`ExecutionOrderVersionConflictError` if the
    stored version no longer matches ``expected_version``, and
    :class:`BrokerIdentityConflictError` on a cross-record identity
    conflict, same as :func:`insert_execution_order`.
    """
    _check_broker_identity_conflict(conn, record)
    table = _get_execution_orders_table(MetaData())
    engine = conn.engine
    next_version = int(expected_version) + 1
    record.version = next_version
    result = conn.execute(
        table.update()
        .where(
            table.c.client_order_id == record.client_order_id,
            table.c.version == int(expected_version),
        )
        .values(
            status=record.status.value,
            origin=record.origin.value,
            broker_identity_status=record.broker_identity_status.value,
            broker_order_id=record.broker_order_id,
            recovery_state=record.recovery_state.value,
            version=next_version,
            payload=json.dumps(_record_to_payload(record), separators=(",", ":")),
            updated_at=_server_now(engine),
        )
    )
    if result.rowcount == 0:
        table2 = _get_execution_orders_table(MetaData())
        existing = conn.execute(
            select(table2).where(table2.c.client_order_id == record.client_order_id)
        ).first()
        if existing is None:
            raise ExecutionOrderNotFoundError(
                f"No ExecutionOrderRecord for client_order_id={record.client_order_id!r}"
            )
        raise ExecutionOrderVersionConflictError(
            f"client_order_id={record.client_order_id!r} version conflict "
            f"(expected {expected_version}, stored {existing.version})"
        )
    return record


def get_execution_order(conn: Connection, client_order_id: str) -> Optional[ExecutionOrderRecord]:
    table = _get_execution_orders_table(MetaData())
    row = conn.execute(select(table).where(table.c.client_order_id == client_order_id)).first()
    return _row_to_record(row) if row is not None else None


# --- standalone convenience wrappers (own transaction each) -----------------


def record_execution_order(engine: Engine, record: ExecutionOrderRecord) -> ExecutionOrderRecord:
    ensure_execution_orders_table(engine)
    with engine.begin() as conn:
        return insert_execution_order(conn, record)


def save_execution_order(
    engine: Engine, record: ExecutionOrderRecord, *, expected_version: int
) -> ExecutionOrderRecord:
    ensure_execution_orders_table(engine)
    with engine.begin() as conn:
        return update_execution_order(conn, record, expected_version=expected_version)


def fetch_execution_order(engine: Engine, client_order_id: str) -> Optional[ExecutionOrderRecord]:
    ensure_execution_orders_table(engine)
    with engine.begin() as conn:
        return get_execution_order(conn, client_order_id)
