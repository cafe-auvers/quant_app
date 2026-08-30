"""Durable persistence for H1's per-``(environment, account_no, symbol)``
:class:`~src.core.execution_ownership.ExecutionOwnership` assignment
(Workstream 9).

Same SQLAlchemy ``Table`` + ``metadata.create_all`` pattern as every other
repository in this program. No row for a given key means H2's default
applies (``LEGACY``) -- :func:`get_ownership` returns that default
in-memory rather than requiring a caller to special-case "row absent".
"""
from __future__ import annotations

import logging
import threading
import weakref
from typing import List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.engine import Connection, Engine

from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.infrastructure.database.coordination_engine import coordination_read_connection

logger = logging.getLogger(__name__)

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


class OwnershipVersionConflictError(RuntimeError):
    """Optimistic-concurrency conflict on an ownership reassignment --
    another writer already changed this row."""


def _get_execution_ownership_table(metadata: MetaData) -> Table:
    return Table(
        "execution_ownership",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("environment", String(10), nullable=False),
        Column("account_no", String(32), nullable=False),
        Column("symbol", String(20), nullable=False),
        Column("owner", String(16), nullable=False, server_default=ExecutionOwner.LEGACY.value),
        Column("strategy_instance_id", String(64), nullable=False, server_default=""),
        Column("assigned_by", String(64), nullable=False, server_default=""),
        Column("version", Integer, nullable=False, server_default="1"),
        Column("updated_at", DateTime, nullable=False),
        UniqueConstraint(
            "environment", "account_no", "symbol", name="uq_execution_ownership_env_account_symbol"
        ),
    )


def ensure_execution_ownership_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_execution_ownership_table(metadata)
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


def _row_to_ownership(row) -> ExecutionOwnership:
    return ExecutionOwnership(
        environment=row.environment, account_no=row.account_no, symbol=row.symbol,
        owner=row.owner, strategy_instance_id=row.strategy_instance_id,
        assigned_by=row.assigned_by, assigned_at=row.updated_at.isoformat() if row.updated_at else None,
        version=row.version,
    )


def get_ownership(engine: Engine, *, environment: str, account_no: str, symbol: str) -> ExecutionOwnership:
    """H2's default (``LEGACY``, no assignment row) when nothing has ever
    been explicitly assigned for this key."""
    table = ensure_execution_ownership_table(engine)
    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    symbol = str(symbol or "").upper()
    with coordination_read_connection(engine) as conn:
        row = conn.execute(
            select(table).where(
                table.c.environment == environment, table.c.account_no == account_no,
                table.c.symbol == symbol,
            )
        ).first()
    if row is None:
        return ExecutionOwnership(environment=environment, account_no=account_no, symbol=symbol)
    return _row_to_ownership(row)


def list_execution_ownership(
    engine: Engine, *, environment: Optional[str] = None
) -> List[ExecutionOwnership]:
    """Bulk-read ownership for projections and emergency-proof refreshes."""

    table = ensure_execution_ownership_table(engine)
    statement = select(table)
    if environment:
        statement = statement.where(
            table.c.environment == str(environment or "").upper()
        )
    with coordination_read_connection(engine) as conn:
        rows = conn.execute(statement).fetchall()
    return [_row_to_ownership(row) for row in rows]


def get_ownership_in_transaction(
    conn: Connection,
    *,
    environment: str,
    account_no: str,
    symbol: str,
    for_update: bool = False,
) -> ExecutionOwnership:
    """Read an ownership assignment in the caller's transaction.

    Workflow intent persistence uses ``for_update=True`` so an ownership
    transfer cannot race between the UI fence and the card CAS write.
    """
    table = _get_execution_ownership_table(MetaData())
    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    symbol = str(symbol or "").upper()
    statement = select(table).where(
        table.c.environment == environment,
        table.c.account_no == account_no,
        table.c.symbol == symbol,
    )
    if for_update:
        statement = statement.with_for_update()
    row = conn.execute(statement).first()
    if row is None:
        return ExecutionOwnership(
            environment=environment, account_no=account_no, symbol=symbol
        )
    return _row_to_ownership(row)


def assign_ownership_in_transaction(
    conn: Connection,
    ownership: ExecutionOwnership,
    *,
    expected_version: Optional[int] = None,
) -> ExecutionOwnership:
    """Assign ownership inside a caller-owned atomic workflow transaction.

    This is used when a durable UI intent and the ownership transfer are one
    semantic operation (for example Buylist -> Buy Today).  ``expected_version``
    may be supplied when the caller rendered a concrete ownership revision;
    the default H2 LEGACY state has version 0 and no row.
    """

    table = _get_execution_ownership_table(MetaData())
    current_row = conn.execute(
        select(table).where(
            table.c.environment == ownership.environment,
            table.c.account_no == ownership.account_no,
            table.c.symbol == ownership.symbol,
        ).with_for_update()
    ).first()
    current_version = int(current_row.version) if current_row is not None else 0
    if expected_version is not None and int(expected_version) != current_version:
        raise OwnershipVersionConflictError(
            f"Expected ownership version {expected_version} for "
            f"{ownership.environment}:{ownership.account_no}:{ownership.symbol}, "
            f"stored version is {current_version}"
        )

    next_version = current_version + 1
    values = {
        "owner": ownership.owner.value,
        "strategy_instance_id": ownership.strategy_instance_id,
        "assigned_by": ownership.assigned_by,
        "version": next_version,
        "updated_at": _server_now(conn.engine),
    }
    if current_row is None:
        conn.execute(
            table.insert().values(
                environment=ownership.environment,
                account_no=ownership.account_no,
                symbol=ownership.symbol,
                **values,
            )
        )
    else:
        conn.execute(
            table.update().where(table.c.id == current_row.id).values(**values)
        )
    return ExecutionOwnership(
        environment=ownership.environment,
        account_no=ownership.account_no,
        symbol=ownership.symbol,
        owner=ownership.owner,
        strategy_instance_id=ownership.strategy_instance_id,
        assigned_by=ownership.assigned_by,
        version=next_version,
    )


def assign_ownership(engine: Engine, ownership: ExecutionOwnership) -> ExecutionOwnership:
    """Explicit, audited ownership transfer (H1/E1's "an EXPLICIT
    ownership transfer back to LEGACY... never an implicit fallback").
    Upserts by ``(environment, account_no, symbol)``; not optimistic-
    concurrency-guarded on its own version since an ownership transfer is
    a rare, explicit, single-actor administrative action, not a
    high-contention write path like an order record."""
    ensure_execution_ownership_table(engine)
    with engine.begin() as conn:
        return assign_ownership_in_transaction(conn, ownership)
