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
from typing import Optional

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, UniqueConstraint, func, select
from sqlalchemy.engine import Connection, Engine

from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership

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
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(
                table.c.environment == environment, table.c.account_no == account_no,
                table.c.symbol == symbol,
            )
        ).first()
    if row is None:
        return ExecutionOwnership(environment=environment, account_no=account_no, symbol=symbol)
    return _row_to_ownership(row)


def assign_ownership(engine: Engine, ownership: ExecutionOwnership) -> ExecutionOwnership:
    """Explicit, audited ownership transfer (H1/E1's "an EXPLICIT
    ownership transfer back to LEGACY... never an implicit fallback").
    Upserts by ``(environment, account_no, symbol)``; not optimistic-
    concurrency-guarded on its own version since an ownership transfer is
    a rare, explicit, single-actor administrative action, not a
    high-contention write path like an order record."""
    table = ensure_execution_ownership_table(engine)
    with engine.begin() as conn:
        existing = conn.execute(
            select(table.c.id, table.c.version).where(
                table.c.environment == ownership.environment, table.c.account_no == ownership.account_no,
                table.c.symbol == ownership.symbol,
            )
        ).first()
        values = dict(
            owner=ownership.owner.value, strategy_instance_id=ownership.strategy_instance_id,
            assigned_by=ownership.assigned_by, updated_at=_server_now(engine),
        )
        if existing is None:
            conn.execute(
                table.insert().values(
                    environment=ownership.environment, account_no=ownership.account_no,
                    symbol=ownership.symbol, version=1, **values,
                )
            )
        else:
            conn.execute(
                table.update()
                .where(table.c.id == existing.id)
                .values(version=existing.version + 1, **values)
            )
    return ownership
