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
every reservation write mirrors here in addition to the local JSON ledger
(kept as the offline/recovery fallback, per section 23's "JSON files may
remain for migration, backup, and recovery"). When no engine is supplied
(the default, and every existing test), behavior is unchanged from before
this module existed -- purely local-JSON.
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
    MetaData,
    String,
    Table,
    func,
    select,
)
from sqlalchemy.engine import Engine
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
        Column("created_at", DateTime, nullable=False),
        Column("released_at", DateTime, nullable=True),
        Column("updated_at", DateTime, nullable=False),
    )


def _ensure_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_capital_reservations_table(metadata)
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
        created_at=row.created_at,
        released_at=row.released_at,
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


def save_reservation(engine: Optional[Engine], reservation: CapitalReservation) -> None:
    """Insert-or-update (upsert) by ``reservation_id``. Best-effort: a
    database write failure is logged, not raised -- the local JSON ledger
    remains the record of truth for *this* process even if the database
    mirror is temporarily unreachable.
    """
    if engine is None:
        return
    try:
        table = _ensure_table(engine)
        with engine.begin() as conn:
            existing = conn.execute(
                select(table.c.reservation_id).where(
                    table.c.reservation_id == reservation.reservation_id
                )
            ).first()
            values = dict(
                environment=reservation.environment,
                account_no=reservation.account_no,
                symbol=reservation.symbol,
                attempt_group_id=reservation.attempt_group_id,
                requested_notional=reservation.requested_notional,
                remaining_reserved_notional=reservation.remaining_reserved_notional,
                status=reservation.status.value,
                created_at=reservation.created_at,
                released_at=reservation.released_at,
                updated_at=_server_now(engine),
            )
            if existing is None:
                conn.execute(
                    table.insert().values(reservation_id=reservation.reservation_id, **values)
                )
            else:
                conn.execute(
                    table.update()
                    .where(table.c.reservation_id == reservation.reservation_id)
                    .values(**values)
                )
    except SQLAlchemyError as exc:
        logger.warning("capital_reservation_repository: save_reservation failed: %s", exc)


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
        save_reservation(engine, reservation)
        released.append(reservation)
    return released
