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

Exact broker-order identity uniqueness is enforced by a real database
``UNIQUE`` constraint on ``broker_identity_key`` (revision 3.2), not only
an application-level pre-check -- a "SELECT then INSERT" check alone is a
race between two concurrent transactions that can both pass the check and
commit duplicate claims; only the database itself can make that atomic.
The pre-check in :func:`insert_execution_order`/:func:`update_execution_order`
remains as a fast, friendly failure for the common (non-racing) case, but
the constraint is the actual authority -- see
:func:`_diagnose_and_raise_integrity_error`, which turns a raw
``IntegrityError`` from either path into the same typed exception.

This module is purely a repository: it does not decide what to submit,
validate gateway gates, or call the broker.
"""
from __future__ import annotations

import json
import logging
import threading
import weakref
from typing import Any, Dict, List, Optional

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
    TERMINAL_EXECUTION_ORDER_STATUSES,
    compute_broker_identity_key,
    validate_consistency,
)
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderIntent, OrderSide

logger = logging.getLogger(__name__)

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


def invalidate_execution_orders_table_cache(engine: Engine) -> None:
    """Forget an ensure result after migration rollback drops the table."""

    with _ensure_lock:
        _ensured_engines.discard(engine)


class DuplicateExecutionOrderError(RuntimeError):
    """A row for this ``client_order_id`` already exists (A1/A5's
    idempotency guarantee applied to the order record itself)."""


class BrokerIdentityConflictError(RuntimeError):
    """Another *different* local record already holds this exact
    ``broker_order_id`` as ``EXACT`` -- a genuine ownership conflict, never
    silently allowed (mirrors :func:`~src.core.execution_order_record.mark_broker_identity_exact`'s
    same-record contradiction check, extended here across records). Backed
    by a real database ``UNIQUE`` constraint on ``broker_identity_key``,
    not only an application-level check -- see the module docstring."""


class DuplicateAdoptionError(RuntimeError):
    """Another *different* local record already claims adoption from this
    exact ``DiscoveredExternalOrder`` -- two adoptions of the same external
    order must never both succeed."""


class ExecutionOrderNotFoundError(RuntimeError):
    pass


class ExecutionOrderVersionConflictError(RuntimeError):
    """Optimistic-concurrency conflict -- another writer already changed
    this row. Callers must reload and retry, never blindly overwrite."""


class ImmutableFieldChangedError(RuntimeError):
    """``environment``/``account_no``/``symbol`` changed between the read
    and the write of the same ``client_order_id`` (revision 3.2) -- these
    are treated as immutable identity fields specifically so the indexed
    relational columns and the JSON payload can never silently diverge.
    A record that genuinely needs to move symbol/account is a new record
    (a new ``client_order_id``), not a mutation of an existing one."""


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
        # NULL (not "") whenever broker_identity_status != EXACT, so many
        # non-EXACT rows can coexist under the UNIQUE constraint below --
        # SQL NULL is never equal to anything, including another NULL, for
        # uniqueness purposes, unlike an empty string.
        Column("broker_identity_key", String(200), nullable=True),
        Column("recovery_state", String(40), nullable=False),
        # NULL (not "") whenever this record was not created by adoption --
        # same NULL-vs-empty-string reasoning as broker_identity_key.
        Column("adopted_from_external_order_id", String(64), nullable=True),
        Column("version", BigInteger, nullable=False, server_default="1"),
        Column("payload", Text(length=16_777_215), nullable=False),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint("client_order_id", name="uq_execution_orders_client_order_id"),
        UniqueConstraint("broker_identity_key", name="uq_execution_orders_broker_identity_key"),
        UniqueConstraint(
            "adopted_from_external_order_id", name="uq_execution_orders_adopted_from_external_order_id"
        ),
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


def _identity_key_or_none(record: ExecutionOrderRecord) -> Optional[str]:
    if record.broker_identity_status != BrokerIdentityStatus.EXACT or not record.broker_order_id:
        return None
    return compute_broker_identity_key(record.environment, record.account_no, record.broker_order_id)


def _adoption_source_or_none(record: ExecutionOrderRecord) -> Optional[str]:
    return record.adopted_from_external_order_id or None


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
        "attempt_deadline_at": record.attempt_deadline_at,
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
        "absence_count": record.absence_count,
        "last_absence_snapshot_id": record.last_absence_snapshot_id,
        "last_absence_observed_at": record.last_absence_observed_at,
        "last_absence_session_date": record.last_absence_session_date,
        "last_absence_broker_order_id": record.last_absence_broker_order_id,
        "last_absence_holding_quantity": record.last_absence_holding_quantity,
        "origin": record.origin.value,
        "broker_identity_status": record.broker_identity_status.value,
        "recovery_state": record.recovery_state.value,
        "recovery_candidate_broker_order_ids": list(
            record.recovery_candidate_broker_order_ids
        ),
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
        attempt_deadline_at=payload.get("attempt_deadline_at"),
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
        absence_count=payload.get("absence_count", 0),
        last_absence_snapshot_id=payload.get("last_absence_snapshot_id", ""),
        last_absence_observed_at=payload.get("last_absence_observed_at"),
        last_absence_session_date=payload.get("last_absence_session_date"),
        last_absence_broker_order_id=payload.get(
            "last_absence_broker_order_id", ""
        ),
        last_absence_holding_quantity=payload.get(
            "last_absence_holding_quantity"
        ),
        origin=OrderOrigin(payload["origin"]),
        broker_identity_status=BrokerIdentityStatus(payload["broker_identity_status"]),
        recovery_state=OrderRecoveryState(payload["recovery_state"]),
        recovery_candidate_broker_order_ids=tuple(
            payload.get("recovery_candidate_broker_order_ids", ())
        ),
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
    """Fast, friendly pre-check for the common (non-racing) case -- the
    database's own UNIQUE constraint on ``broker_identity_key`` is what
    actually makes this race-safe; see :func:`_diagnose_and_raise_integrity_error`.
    """
    key = _identity_key_or_none(record)
    if key is None:
        return
    table = _get_execution_orders_table(MetaData())
    existing = conn.execute(
        select(table.c.client_order_id).where(
            table.c.broker_identity_key == key,
            table.c.client_order_id != record.client_order_id,
        )
    ).first()
    if existing is not None:
        raise BrokerIdentityConflictError(
            f"broker_order_id={record.broker_order_id!r} is already EXACT on "
            f"client_order_id={existing.client_order_id!r} -- cannot also claim it on "
            f"{record.client_order_id!r}"
        )


def _diagnose_and_raise_integrity_error(
    conn: Connection, record: ExecutionOrderRecord, exc: IntegrityError
) -> None:
    """Turns a raw ``IntegrityError`` from a failed insert/update into the
    specific, typed exception it actually represents -- the real,
    race-safe enforcement (the pre-checks above only catch the common
    non-racing case). Always raises; never returns normally.
    """
    table = _get_execution_orders_table(MetaData())
    existing_by_client_id = conn.execute(
        select(table.c.id).where(table.c.client_order_id == record.client_order_id)
    ).first()
    if existing_by_client_id is not None:
        raise DuplicateExecutionOrderError(
            f"ExecutionOrderRecord for client_order_id={record.client_order_id!r} already exists"
        ) from exc

    key = _identity_key_or_none(record)
    if key is not None:
        existing_by_identity = conn.execute(
            select(table.c.client_order_id).where(table.c.broker_identity_key == key)
        ).first()
        if existing_by_identity is not None:
            raise BrokerIdentityConflictError(
                f"broker_order_id={record.broker_order_id!r} is already EXACT on "
                f"client_order_id={existing_by_identity.client_order_id!r}"
            ) from exc

    adoption_source = _adoption_source_or_none(record)
    if adoption_source is not None:
        existing_by_adoption = conn.execute(
            select(table.c.client_order_id).where(
                table.c.adopted_from_external_order_id == adoption_source
            )
        ).first()
        if existing_by_adoption is not None:
            raise DuplicateAdoptionError(
                f"external_order_id={adoption_source!r} was already adopted as "
                f"client_order_id={existing_by_adoption.client_order_id!r}"
            ) from exc

    raise exc


def insert_execution_order(conn: Connection, record: ExecutionOrderRecord) -> ExecutionOrderRecord:
    """A1: the durable ``ExecutionOrderRecord`` write, part of the atomic
    pre-submission transaction (command + reservation + this record).
    Takes an already-open ``Connection``. Raises
    :class:`DuplicateExecutionOrderError` on a repeated ``client_order_id``,
    :class:`BrokerIdentityConflictError` if another record already holds
    this exact ``broker_order_id``, or :class:`DuplicateAdoptionError` if
    another record already claims this exact adoption source.
    """
    validate_consistency(record)
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
                broker_identity_key=_identity_key_or_none(record),
                recovery_state=record.recovery_state.value,
                adopted_from_external_order_id=_adoption_source_or_none(record),
                version=record.version,
                payload=json.dumps(_record_to_payload(record), separators=(",", ":")),
                updated_at=_server_now(engine),
            )
        )
    except IntegrityError as exc:
        _diagnose_and_raise_integrity_error(conn, record, exc)
    return record


def update_execution_order(
    conn: Connection, record: ExecutionOrderRecord, *, expected_version: int
) -> ExecutionOrderRecord:
    """Optimistic-concurrency update, mirroring
    :func:`src.services.trade_card_repository.update_trade_card`'s
    pattern -- with two revision-3.2 corrections that pattern didn't have:

    - ``record.version`` is assigned the new value only *after* the update
      is confirmed to have actually applied (``rowcount == 1``) -- not
      before, which would leave the caller's in-memory object claiming a
      version the database never actually stored on a conflict.
    - ``environment``/``account_no``/``symbol`` are treated as immutable:
      compared against the currently-stored row before the write, raising
      :class:`ImmutableFieldChangedError` rather than letting the indexed
      relational columns and the JSON payload silently diverge.
    """
    validate_consistency(record)
    table = _get_execution_orders_table(MetaData())
    engine = conn.engine

    current = conn.execute(
        select(table.c.environment, table.c.account_no, table.c.symbol, table.c.version).where(
            table.c.client_order_id == record.client_order_id
        )
    ).first()
    if current is None:
        raise ExecutionOrderNotFoundError(
            f"No ExecutionOrderRecord for client_order_id={record.client_order_id!r}"
        )
    if (
        current.environment != record.environment
        or current.account_no != record.account_no
        or current.symbol != record.symbol
    ):
        raise ImmutableFieldChangedError(
            f"client_order_id={record.client_order_id!r}: environment/account_no/symbol "
            "must not change after creation"
        )
    if int(current.version) != int(expected_version):
        raise ExecutionOrderVersionConflictError(
            f"client_order_id={record.client_order_id!r} version conflict "
            f"(expected {expected_version}, stored {current.version})"
        )

    _check_broker_identity_conflict(conn, record)
    next_version = int(expected_version) + 1
    # Build the persisted payload with the *new* version already in it --
    # the version column and the JSON payload's own "version" field must
    # never disagree, so this must not read record.version (which is only
    # updated below, once the write is confirmed to have applied).
    payload_dict = _record_to_payload(record)
    payload_dict["version"] = next_version
    try:
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
                broker_identity_key=_identity_key_or_none(record),
                recovery_state=record.recovery_state.value,
                adopted_from_external_order_id=_adoption_source_or_none(record),
                version=next_version,
                payload=json.dumps(payload_dict, separators=(",", ":")),
                updated_at=_server_now(engine),
            )
        )
    except IntegrityError as exc:
        _diagnose_and_raise_integrity_error(conn, record, exc)
        raise  # unreachable -- _diagnose_and_raise_integrity_error always raises

    if result.rowcount == 0:
        raise ExecutionOrderVersionConflictError(
            f"client_order_id={record.client_order_id!r} version conflict "
            f"(row changed concurrently after version {expected_version} was read)"
        )
    record.version = next_version
    return record


def get_execution_order(conn: Connection, client_order_id: str) -> Optional[ExecutionOrderRecord]:
    table = _get_execution_orders_table(MetaData())
    row = conn.execute(select(table).where(table.c.client_order_id == client_order_id)).first()
    return _row_to_record(row) if row is not None else None


def find_active_unlinked_adopted_order(
    conn: Connection,
    *,
    environment: str,
    account_no: str,
    symbol: str,
    allowed_client_order_id: str = "",
) -> Optional[ExecutionOrderRecord]:
    """Return an active adopted order that must still fence mutations.

    Adoption establishes a durable local audit record; it does not prove
    that an otherwise-unlinked broker order is safe to ignore.  The one
    exception is a mutation whose target is that exact adopted order (for
    example an explicitly permitted cancel).  Its normal permission and
    identity gates still run before this repository fence is consulted.
    """

    table = _get_execution_orders_table(MetaData())
    terminal = [status.value for status in TERMINAL_EXECUTION_ORDER_STATUSES]
    statement = select(table).where(
        table.c.environment == str(environment or "").upper(),
        table.c.account_no == str(account_no or ""),
        table.c.symbol == str(symbol or "").upper(),
        table.c.origin == OrderOrigin.USER_ADOPTED.value,
        table.c.status.not_in(terminal),
    )
    allowed = str(allowed_client_order_id or "").strip()
    if allowed:
        statement = statement.where(table.c.client_order_id != allowed)
    row = conn.execute(
        statement.order_by(table.c.id.asc()).limit(1).with_for_update()
    ).first()
    return _row_to_record(row) if row is not None else None


def find_active_execution_order_for_scope(
    conn: Connection,
    *,
    environment: str,
    account_no: str,
    symbol: str,
) -> Optional[ExecutionOrderRecord]:
    """Return any nonterminal execution lifecycle for one symbol scope."""

    table = _get_execution_orders_table(MetaData())
    terminal = [status.value for status in TERMINAL_EXECUTION_ORDER_STATUSES]
    row = conn.execute(
        select(table)
        .where(
            table.c.environment == str(environment or "").upper(),
            table.c.account_no == str(account_no or ""),
            table.c.symbol == str(symbol or "").upper(),
            table.c.status.not_in(terminal),
        )
        .order_by(table.c.id.asc())
        .limit(1)
        .with_for_update()
    ).first()
    return _row_to_record(row) if row is not None else None


# --- standalone convenience wrappers ----------------------------------------


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
    # A read must not create a committing transaction.  On the shared TiDB
    # coordination store, ``engine.begin()`` emitted a billed COMMIT for each
    # broker reconciliation lookup even though no row was changed.
    with engine.connect() as conn:
        return get_execution_order(conn, client_order_id)


def list_execution_orders_for_card(
    engine: Engine,
    *,
    environment: str,
    account_no: str,
    symbol: str,
) -> List[ExecutionOrderRecord]:
    """Return durable guarded orders for one card, newest records first."""
    table = ensure_execution_orders_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            select(table)
            .where(
                table.c.environment == str(environment or "").upper(),
                table.c.account_no == str(account_no or ""),
                table.c.symbol == str(symbol or "").upper(),
            )
            .order_by(table.c.id.desc())
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def list_execution_orders_for_account(
    engine: Engine,
    *,
    environment: str,
    account_no: str,
) -> List[ExecutionOrderRecord]:
    """Return every durable order in one account for a single C1 pass."""
    table = ensure_execution_orders_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            select(table)
            .where(
                table.c.environment == str(environment or "").upper(),
                table.c.account_no == str(account_no or ""),
            )
            .order_by(table.c.id.asc())
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def list_execution_orders(
    engine: Engine, *, environment: Optional[str] = None
) -> List[ExecutionOrderRecord]:
    """Return all durable orders, optionally scoped to one environment."""

    table = ensure_execution_orders_table(engine)
    statement = select(table)
    if environment is not None:
        statement = statement.where(
            table.c.environment == str(environment or "").upper()
        )
    with engine.connect() as conn:
        rows = conn.execute(statement.order_by(table.c.id.asc())).fetchall()
    return [_row_to_record(row) for row in rows]
