"""Durable command idempotency ledger.

``docs/kanban_production_readiness.md``, Workstream 2 (A5), signed off in
that document's revision 3.1: "A unique constraint on the idempotency key
must prevent duplicate submit, cancel, and replace operations after
restart or device handoff." (INV-15).

Follows the same pattern :mod:`src.services.trade_card_repository` already
uses for its own table (a SQLAlchemy ``Table`` + idempotent
``metadata.create_all``, one row per command with a real ``UNIQUE``
constraint) rather than a whole-blob JSON payload.

This is the mandatory command journal Workstream 3's execution gateway
(B4a) writes to *before* every broker call -- see that module's own
docstring for the full failure-domain split (mandatory journal vs.
broker-response persistence vs. supplementary audit log). This module is
purely a repository: it does not decide what commands to issue, validate
gateway gates, or call the broker.
"""
from __future__ import annotations

import json
import logging
import threading
import weakref
from dataclasses import dataclass, field
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
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


class DuplicateCommandError(RuntimeError):
    """Raised when a command with an already-recorded ``idempotency_key``
    is submitted again -- A5's guarantee. The caller (the execution
    gateway) must treat this as "already handled, do not call the broker
    a second time," never retry the underlying broker action from scratch.
    """


class CommandNotFoundError(RuntimeError):
    """No command exists for the requested ``idempotency_key``."""


@dataclass
class ExecutionCommand:
    idempotency_key: str
    command_type: str  # "submit" | "cancel" | "replace"
    environment: str
    account_no: str
    symbol: str
    lease_epoch: int
    target_broker_order_id: str = ""
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "REQUESTED"  # REQUESTED -> ACKNOWLEDGED | FAILED (B4b's post-call persist)
    broker_response: Dict[str, Any] = field(default_factory=dict)
    command_id: Optional[int] = None
    version: int = 1

    def __post_init__(self) -> None:
        self.idempotency_key = str(self.idempotency_key or "").strip()
        if not self.idempotency_key:
            raise ValueError("ExecutionCommand requires a non-blank idempotency_key")
        self.command_type = str(self.command_type or "").strip().lower()
        if self.command_type not in ("submit", "cancel", "replace"):
            raise ValueError(f"Unknown command_type: {self.command_type!r}")
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        self.lease_epoch = int(self.lease_epoch or 0)
        self.target_broker_order_id = str(self.target_broker_order_id or "")
        self.status = str(self.status or "REQUESTED").upper()
        if not isinstance(self.broker_response, dict):
            self.broker_response = {"raw": self.broker_response}
        self.version = int(self.version or 1)


def _get_execution_commands_table(metadata: MetaData) -> Table:
    return Table(
        "execution_commands",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("idempotency_key", String(160), nullable=False),
        Column("command_type", String(16), nullable=False),
        Column("environment", String(10), nullable=False),
        Column("account_no", String(32), nullable=False),
        Column("symbol", String(20), nullable=False),
        Column("lease_epoch", BigInteger, nullable=False, server_default="0"),
        Column("target_broker_order_id", String(64), nullable=False, server_default=""),
        Column("requested_at", DateTime, nullable=False),
        Column("status", String(24), nullable=False, server_default="REQUESTED"),
        Column("broker_response", Text(length=16_777_215), nullable=False, server_default="{}"),
        Column("version", BigInteger, nullable=False, server_default="1"),
        UniqueConstraint("idempotency_key", name="uq_execution_commands_idempotency_key"),
    )


def _ensure_execution_commands_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_execution_commands_table(metadata)
    if engine in _ensured_engines:
        return table
    with _ensure_lock:
        if engine in _ensured_engines:
            return table
        metadata.create_all(engine)
        _ensured_engines.add(engine)
    return table


def _parse_dt(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _row_to_command(row) -> ExecutionCommand:
    try:
        broker_response = json.loads(row.broker_response or "{}")
    except (TypeError, ValueError):
        broker_response = {}
    return ExecutionCommand(
        idempotency_key=row.idempotency_key,
        command_type=row.command_type,
        environment=row.environment,
        account_no=row.account_no,
        symbol=row.symbol,
        lease_epoch=row.lease_epoch,
        target_broker_order_id=row.target_broker_order_id,
        requested_at=row.requested_at.isoformat() if row.requested_at else "",
        status=row.status,
        broker_response=broker_response,
        command_id=row.id,
        version=row.version,
    )


def record_command(engine: Engine, command: ExecutionCommand) -> ExecutionCommand:
    """B4a: the mandatory command journal write, made *before* any broker
    call. Raises :class:`DuplicateCommandError` on a repeated
    ``idempotency_key`` -- the caller must not then call the broker a
    second time.
    """
    table = _ensure_execution_commands_table(engine)
    try:
        with engine.begin() as conn:
            result = conn.execute(
                table.insert().values(
                    idempotency_key=command.idempotency_key,
                    command_type=command.command_type,
                    environment=command.environment,
                    account_no=command.account_no,
                    symbol=command.symbol,
                    lease_epoch=command.lease_epoch,
                    target_broker_order_id=command.target_broker_order_id,
                    requested_at=_parse_dt(command.requested_at),
                    status=command.status,
                    broker_response=json.dumps(command.broker_response, separators=(",", ":")),
                    version=command.version,
                )
            )
    except IntegrityError as exc:
        raise DuplicateCommandError(
            f"Command with idempotency_key={command.idempotency_key!r} was already recorded"
        ) from exc
    command.command_id = result.inserted_primary_key[0]
    return command


def get_command_by_idempotency_key(engine: Engine, idempotency_key: str) -> Optional[ExecutionCommand]:
    table = _ensure_execution_commands_table(engine)
    with engine.begin() as conn:
        row = conn.execute(
            select(table).where(table.c.idempotency_key == idempotency_key)
        ).first()
    return _row_to_command(row) if row is not None else None


def update_command_response(
    engine: Engine,
    idempotency_key: str,
    *,
    status: str,
    broker_response: Dict[str, Any],
) -> ExecutionCommand:
    """B4b: the post-call broker-response persist. A failure here must
    never be treated as license to retry the broker call (INV-23) -- that
    rule is enforced by the execution gateway (Workstream 3), not this
    repository; this function only records the outcome once the gateway
    has already decided not to retry.
    """
    table = _ensure_execution_commands_table(engine)
    with engine.begin() as conn:
        result = conn.execute(
            table.update()
            .where(table.c.idempotency_key == idempotency_key)
            .values(
                status=str(status or "").upper(),
                broker_response=json.dumps(broker_response or {}, separators=(",", ":")),
            )
        )
        if result.rowcount == 0:
            raise CommandNotFoundError(
                f"No command found for idempotency_key={idempotency_key!r}"
            )
        row = conn.execute(
            select(table).where(table.c.idempotency_key == idempotency_key)
        ).first()
    return _row_to_command(row)
