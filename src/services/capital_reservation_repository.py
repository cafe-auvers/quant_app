"""Central (MySQL) persistence for :class:`~src.core.capital_reservation.CapitalReservation`
rows. ``buydashboard_to_kanban.md`` section 22; code review finding P1-12.

The local JSON ledger :mod:`src.services.capital_allocator` already uses
(file-lock-guarded, same pattern as :mod:`src.services.order_ledger`) does
not synchronize between devices -- a PC and a laptop have separate
filesystems, so each could believe it has the account's full buying power
available and both submit at once. This module gives capital reservations
the same database-backed, cross-device-visible home
:mod:`src.services.trade_card_repository` already gives trade cards, using
the identical SQLAlchemy ``Table`` + ``metadata.create_all`` pattern
(:func:`src.services.state_sync._ensure_state_sync_table`'s idiom).

``capital_allocator.py`` treats this as optional: every function there
accepts ``engine: Optional[Engine] = None``. When an engine is supplied, the
database becomes the authoritative source for the availability check and
every reservation write commits here before updating the local JSON mirror
(kept as the offline/recovery fallback, per section 23's "JSON files may
remain for migration, backup, and recovery"). When no engine is supplied
(the default, and every existing test), behavior is unchanged from before
this module existed -- purely local-JSON.

``insert_reservation`` (Workstream 3, PR2, A1) is a second, distinct write
path added alongside the above, not a replacement for it: the guarded
execution gateway's atomic pre-submission transaction ("insert
``ExecutionCommand``, create capital reservation, create ``PREPARED``
``ExecutionOrderRecord``, all in one transaction") needs a reservation write
that (a) takes an already-open ``Connection`` so it can be composed with
``execution_command_repository.insert_command``/
``execution_order_repository.insert_execution_order`` into one commit, and
(b) raises on failure rather than ``save_reservation``'s compatibility
best-effort log-and-continue behavior -- a reservation that silently failed
to write would leave the "atomic" transaction not actually atomic. The
allocator uses ``save_reservation_strict`` so CAS conflicts repair the local
mirror from the authoritative row instead of persisting stale state.
"""
from __future__ import annotations

import logging
import threading
import weakref
from typing import Iterable, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.core.capital_reservation import CapitalReservation, CapitalReservationStatus

logger = logging.getLogger(__name__)

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()

_ACTIVE_STATUS_VALUES = [
    CapitalReservationStatus.RESERVED.value,
    CapitalReservationStatus.PARTIALLY_CONSUMED.value,
]


def _get_capital_reservations_table(metadata: MetaData) -> Table:
    return Table(
        "capital_reservations",
        metadata,
        Column("reservation_id", String(64), primary_key=True),
        Column("environment", String(10), nullable=False),
        Column("account_no", String(32), nullable=False),
        Column("symbol", String(20), nullable=False),
        Column("attempt_group_id", String(64), nullable=False),
        Column("requested_notional", Float, nullable=False),
        Column("remaining_reserved_notional", Float, nullable=False),
        Column("status", String(24), nullable=False),
        Column("version", Integer, nullable=False, server_default="1"),
        Column("created_at", DateTime, nullable=False),
        Column("released_at", DateTime, nullable=True),
        Column("absence_count", Integer, nullable=False, server_default="0"),
        Column("last_absence_snapshot_id", String(64), nullable=True),
        Column("last_absence_observed_at", String(64), nullable=True),
        Column("last_absence_session_date", String(16), nullable=True),
        Column("updated_at", DateTime, nullable=False),
    )


def ensure_capital_reservations_table(engine: Engine) -> Table:
    """Public, idempotent table-creation entry point (Workstream 3, PR2) --
    matches ``execution_order_repository.ensure_execution_orders_table``'s
    naming/pattern. Callers composing :func:`insert_reservation`/
    :func:`update_reservation` into their own transaction must call this
    *before* opening it: those two primitives intentionally do not ensure
    the table themselves, since ``metadata.create_all`` opens its own
    connection from the engine's pool, which for a file-based SQLite
    database can contend with a transaction already open on ``conn``."""
    return _ensure_table(engine)


def _ensure_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_capital_reservations_table(metadata)
    if engine in _ensured_engines:
        return table
    with _ensure_lock:
        if engine in _ensured_engines:
            return table
        metadata.create_all(engine)
        _ensure_pr3_columns(engine)
        _ensured_engines.add(engine)
    return table


def _ensure_pr3_columns(engine: Engine) -> None:
    """Add PR3 evidence and optimistic-version fields to a PR2 table."""
    existing = {
        column["name"]
        for column in inspect(engine).get_columns("capital_reservations")
    }
    definitions = {
        "version": "INTEGER NOT NULL DEFAULT 1",
        "absence_count": "INTEGER NOT NULL DEFAULT 0",
        "last_absence_snapshot_id": "VARCHAR(64) NULL",
        "last_absence_observed_at": "VARCHAR(64) NULL",
        "last_absence_session_date": "VARCHAR(16) NULL",
    }
    missing = [name for name in definitions if name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for name in missing:
            conn.execute(
                text(
                    f"ALTER TABLE capital_reservations ADD COLUMN {name} "
                    f"{definitions[name]}"
                )
            )


def _server_now(engine: Engine):
    if engine.dialect.name == "mysql":
        return func.utc_timestamp(6)
    return func.current_timestamp()


def _row_to_reservation(row) -> CapitalReservation:
    return CapitalReservation(
        reservation_id=row.reservation_id,
        environment=row.environment,
        account_no=row.account_no,
        symbol=row.symbol,
        attempt_group_id=row.attempt_group_id,
        requested_notional=row.requested_notional,
        remaining_reserved_notional=row.remaining_reserved_notional,
        status=row.status,
        version=row.version,
        created_at=row.created_at,
        released_at=row.released_at,
        absence_count=row.absence_count,
        last_absence_snapshot_id=row.last_absence_snapshot_id or "",
        last_absence_observed_at=row.last_absence_observed_at,
        last_absence_session_date=row.last_absence_session_date,
    )


def list_active_reservations(
    engine: Optional[Engine], *, environment: str, account_no: str
) -> List[CapitalReservation]:
    """Every RESERVED/PARTIALLY_CONSUMED row for this account -- the same
    set :func:`src.core.capital_reservation.available_for_new_entries`
    subtracts from buying power, but read from the database so every device
    sees the same in-flight reservations.

    Deliberately does *not* catch ``SQLAlchemyError`` (code review finding
    P1-1): this is the availability *read* path a new entry's capital check
    depends on. Silently returning ``[]`` on a database outage would make
    every reservation another device is holding invisible, which is exactly
    the double-spend this table exists to prevent -- a caller that wants
    cross-device coordination must find out the read failed and block the
    new entry (fail closed), not quietly fall back to trusting the local
    ledger alone as if it were still authoritative.
    :func:`src.services.capital_allocator.reserve_capital_for_entry` is the
    caller that matters here; it does not catch this either, and
    :class:`src.services.entry_attempt_manager.EntryAttemptManager` catches
    it only to turn it into a blocked/cooldown attempt for *this* symbol,
    never into "treat capital as available."
    """
    if engine is None:
        return []
    table = _ensure_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            select(table).where(
                table.c.environment == str(environment or "").upper(),
                table.c.account_no == str(account_no or ""),
                table.c.status.in_(_ACTIVE_STATUS_VALUES),
            )
        ).fetchall()
    return [_row_to_reservation(row) for row in rows]


def fetch_reservation(
    engine: Optional[Engine], reservation_id: str
) -> Optional[CapitalReservation]:
    if engine is None:
        return None
    table = _ensure_table(engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.reservation_id == str(reservation_id or ""))
        ).first()
    return _row_to_reservation(row) if row is not None else None


def save_reservation_strict(
    engine: Engine, reservation: CapitalReservation
) -> CapitalReservation:
    """Authoritative insert-or-CAS-update used before a local mirror write.

    Unlike :func:`save_reservation`, conflicts and database failures are
    propagated. This prevents a caller from assuming its stale mutation
    succeeded and then making that stale object authoritative in JSON.
    """
    table = _ensure_table(engine)
    with engine.begin() as conn:
        existing = conn.execute(
            select(table.c.reservation_id).where(
                table.c.reservation_id == reservation.reservation_id
            )
        ).first()
        if existing is None:
            insert_reservation(conn, reservation)
        else:
            update_reservation(
                conn,
                reservation,
                expected_version=reservation.version,
            )
    return reservation


def save_reservation(engine: Optional[Engine], reservation: CapitalReservation) -> None:
    """Backward-compatible best-effort wrapper around the strict API.

    Safety-critical allocator paths use :func:`save_reservation_strict`
    and write their local mirror only after its CAS succeeds.
    """
    if engine is None:
        return
    try:
        save_reservation_strict(engine, reservation)
    except (SQLAlchemyError, CapitalReservationVersionConflictError) as exc:
        logger.warning("capital_reservation_repository: save_reservation failed: %s", exc)


class DuplicateReservationError(RuntimeError):
    """A row for this ``reservation_id`` already exists -- ``reservation_id``
    is a fresh UUID per :meth:`CapitalReservation.create`, so this should
    only ever fire on a genuine caller bug (re-using a reservation object
    across two submission attempts), never in ordinary operation."""


class InsufficientAvailableCapitalError(RuntimeError):
    """The authoritative transaction cannot reserve the requested notional."""


class CapitalReservationVersionConflictError(RuntimeError):
    """A reservation changed after the caller read its version."""


# --- shared-transaction primitive (Workstream 3, PR2) -----------------------


def insert_reservation(conn: Connection, reservation: CapitalReservation) -> CapitalReservation:
    """The A1 atomic-transaction write: takes an already-open ``Connection``
    so a caller (the execution gateway) can compose it with the command
    journal and order-record writes into one commit, and raises on failure
    instead of :func:`save_reservation`'s best-effort log-and-continue --
    see the module docstring. Callers must call
    :func:`ensure_capital_reservations_table` before opening the
    transaction this participates in -- this function does not ensure the
    table itself (see that function's own docstring)."""
    table = _get_capital_reservations_table(MetaData())
    existing = conn.execute(
        select(table.c.reservation_id).where(table.c.reservation_id == reservation.reservation_id)
    ).first()
    if existing is not None:
        raise DuplicateReservationError(
            f"CapitalReservation {reservation.reservation_id!r} already exists"
        )
    conn.execute(
        table.insert().values(
            reservation_id=reservation.reservation_id,
            environment=reservation.environment,
            account_no=reservation.account_no,
            symbol=reservation.symbol,
            attempt_group_id=reservation.attempt_group_id,
            requested_notional=reservation.requested_notional,
            remaining_reserved_notional=reservation.remaining_reserved_notional,
            status=reservation.status.value,
            version=reservation.version,
            created_at=reservation.created_at,
            released_at=reservation.released_at,
            absence_count=reservation.absence_count,
            last_absence_snapshot_id=(
                reservation.last_absence_snapshot_id or None
            ),
            last_absence_observed_at=reservation.last_absence_observed_at,
            last_absence_session_date=reservation.last_absence_session_date,
            updated_at=_server_now(conn.engine),
        )
    )
    return reservation


def insert_reservation_if_available(
    conn: Connection,
    reservation: CapitalReservation,
    *,
    buying_power: float,
) -> CapitalReservation:
    """Validate account availability and insert within the caller's transaction."""
    table = _get_capital_reservations_table(MetaData())
    if reservation.requested_notional > 0:
        rows = conn.execute(
            select(table.c.remaining_reserved_notional)
            .where(
                table.c.environment == reservation.environment,
                table.c.account_no == reservation.account_no,
                table.c.status.in_(_ACTIVE_STATUS_VALUES),
            )
            .with_for_update()
        ).fetchall()
        reserved = sum(float(row.remaining_reserved_notional or 0.0) for row in rows)
        available = float(buying_power or 0.0) - reserved
        if available < reservation.requested_notional:
            raise InsufficientAvailableCapitalError(
                f"Capital reservation denied for {reservation.environment}/"
                f"{reservation.account_no}/{reservation.symbol}: requested "
                f"{reservation.requested_notional:.2f}, available {available:.2f}"
            )
    return insert_reservation(conn, reservation)


def update_reservation(
    conn: Connection,
    reservation: CapitalReservation,
    *,
    expected_version: int,
) -> CapitalReservation:
    """Companion to :func:`insert_reservation` for the same caller-owned-
    transaction composability -- used by the gateway to release a
    reservation (on a clean rejection) inside the same transaction as the
    order-record/command-response update, rather than a separate best-effort
    write after the fact. Same table-must-already-exist requirement as
    :func:`insert_reservation`."""
    table = _get_capital_reservations_table(MetaData())
    next_version = int(expected_version) + 1
    result = conn.execute(
        table.update()
        .where(
            table.c.reservation_id == reservation.reservation_id,
            table.c.version == int(expected_version),
        )
        .values(
            requested_notional=reservation.requested_notional,
            remaining_reserved_notional=reservation.remaining_reserved_notional,
            status=reservation.status.value,
            version=next_version,
            released_at=reservation.released_at,
            absence_count=reservation.absence_count,
            last_absence_snapshot_id=(
                reservation.last_absence_snapshot_id or None
            ),
            last_absence_observed_at=reservation.last_absence_observed_at,
            last_absence_session_date=reservation.last_absence_session_date,
            updated_at=_server_now(conn.engine),
        )
    )
    if result.rowcount == 0:
        stored = conn.execute(
            select(table.c.version).where(
                table.c.reservation_id == reservation.reservation_id
            )
        ).first()
        if stored is None:
            raise RuntimeError(
                f"CapitalReservation {reservation.reservation_id!r} not found for update"
            )
        raise CapitalReservationVersionConflictError(
            f"reservation_id={reservation.reservation_id!r} version conflict "
            f"(expected {expected_version}, stored {stored.version})"
        )
    reservation.version = next_version
    return reservation


def reconcile_stale_reservations(
    engine: Optional[Engine],
    *,
    environment: str,
    account_no: str,
    open_broker_order_symbols: Iterable[str],
) -> List[CapitalReservation]:
    """Startup/handoff reconciliation (review finding P1-12: "reconcile
    them against KIS orders during startup and main-device handoff").

    Any RESERVED/PARTIALLY_CONSUMED reservation whose symbol has no
    corresponding open broker order is stale -- the process that owned it
    crashed, restarted, or handed off without releasing it. Releases and
    returns every reservation this correction touched.
    """
    if engine is None:
        return []
    open_symbols = {str(symbol or "").upper() for symbol in open_broker_order_symbols}
    released: List[CapitalReservation] = []
    for reservation in list_active_reservations(engine, environment=environment, account_no=account_no):
        if reservation.symbol in open_symbols:
            continue
        reservation.release()
        save_reservation_strict(engine, reservation)
        released.append(reservation)
    return released
