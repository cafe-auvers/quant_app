"""Append-only human operator requests consumed by the execution owner.

The table in this module is intentionally separate from
``execution_commands``.  The latter is the broker-boundary idempotency ledger;
this table records human intent before the execution owner validates or
applies it.  A laptop can therefore request a live board action without ever
writing canonical card state or calling KIS itself.
"""
from __future__ import annotations

import json
import threading
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.services.state_sync import (
    MAIN_DEVICE_KEY,
    OPERATOR_CONTROL_KEY,
    LocalDeviceRole,
    _decode_payload,
    _ensure_state_sync_table,
    _server_now,
)


class OperatorCommandType(str, Enum):
    ADD_BUY_TODAY = "ADD_BUY_TODAY"
    CANCEL_ENTRY = "CANCEL_ENTRY"
    SELL_PARTIAL = "SELL_PARTIAL"
    SELL_ALL = "SELL_ALL"
    MOVE_STOP_BREAKEVEN = "MOVE_STOP_BREAKEVEN"
    MOVE_STOP_MANUAL_PRICE = "MOVE_STOP_MANUAL_PRICE"
    SET_PARTIAL_SELL_QUANTITY = "SET_PARTIAL_SELL_QUANTITY"
    LOCK_OPERATOR_CONTROL = "LOCK_OPERATOR_CONTROL"
    UNLOCK_OPERATOR_CONTROL = "UNLOCK_OPERATOR_CONTROL"


class OperatorCommandStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    BROKER_SUBMITTED = "BROKER_SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_OPERATOR_COMMAND_STATUSES = frozenset(
    {
        OperatorCommandStatus.REJECTED,
        OperatorCommandStatus.FILLED,
        OperatorCommandStatus.COMPLETED,
        OperatorCommandStatus.FAILED,
        OperatorCommandStatus.CANCELLED,
        OperatorCommandStatus.EXPIRED,
    }
)

# A PENDING request is transferable: after the Main lease moves, only the new
# Execution Owner can claim it. Once accepted, the executor identity is
# durable, so an ownership transfer would strand the command mid-lifecycle.
NONTRANSFERABLE_OPERATOR_COMMAND_STATUSES = frozenset(
    {
        OperatorCommandStatus.ACCEPTED,
        OperatorCommandStatus.EXECUTING,
        OperatorCommandStatus.BROKER_SUBMITTED,
        OperatorCommandStatus.PARTIALLY_FILLED,
    }
)


class OperatorCommandError(RuntimeError):
    pass


class OperatorControlNotOwnedError(OperatorCommandError):
    pass


class ExecutionOwnerMismatchError(OperatorCommandError):
    pass


class OperatorCommandTransitionError(OperatorCommandError):
    pass


@dataclass(frozen=True)
class OperatorCommandRecord:
    command_id: str
    idempotency_key: str
    command_type: OperatorCommandType
    symbol: str
    payload: Dict[str, Any]
    status: OperatorCommandStatus
    requested_by_device: str
    requested_by_host: str
    created_at: datetime
    accepted_at: Optional[datetime] = None
    executing_at: Optional[datetime] = None
    broker_submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    error_message: str = ""
    executor_device_id: str = ""
    executor_host: str = ""
    broker_order_id: str = ""
    state_before_hash: str = ""
    state_after_hash: str = ""


@dataclass(frozen=True)
class OperatorCommandInsertResult:
    command: OperatorCommandRecord
    created: bool


_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


def _table(metadata: MetaData) -> Table:
    return Table(
        "operator_commands",
        metadata,
        Column("command_id", String(64), primary_key=True),
        Column("idempotency_key", String(128), nullable=False, unique=True),
        Column("command_type", String(48), nullable=False),
        Column("symbol", String(32), nullable=False, server_default=""),
        Column("payload_json", Text(length=16_777_215), nullable=False),
        Column("status", String(32), nullable=False),
        Column("requested_by_device", String(64), nullable=False),
        Column("requested_by_host", String(128), nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("accepted_at", DateTime),
        Column("executing_at", DateTime),
        Column("broker_submitted_at", DateTime),
        Column("completed_at", DateTime),
        Column("rejected_at", DateTime),
        Column("failed_at", DateTime),
        # MySQL 5.7 and older MariaDB releases reject defaults on TEXT/BLOB
        # columns (error 1101).  Inserts write the empty value explicitly.
        Column("error_message", Text, nullable=False),
        Column("executor_device_id", String(64), nullable=False, server_default=""),
        Column("executor_host", String(128), nullable=False, server_default=""),
        Column("broker_order_id", String(128), nullable=False, server_default=""),
        Column("state_before_hash", String(64), nullable=False, server_default=""),
        Column("state_after_hash", String(64), nullable=False, server_default=""),
    )


def ensure_operator_commands_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _table(metadata)
    if engine in _ensured_engines:
        return table
    with _ensure_lock:
        if engine not in _ensured_engines:
            metadata.create_all(engine)
            existing = {
                column["name"]
                for column in inspect(engine).get_columns("operator_commands")
            }
            additions = {
                "broker_order_id": "VARCHAR(128) NOT NULL DEFAULT ''",
                "state_before_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
                "state_after_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
                "completed_at": "DATETIME NULL",
            }
            with engine.begin() as conn:
                for name, definition in additions.items():
                    if name not in existing:
                        conn.execute(
                            text(
                                f"ALTER TABLE operator_commands ADD COLUMN {name} "
                                f"{definition}"
                            )
                        )
            _ensured_engines.add(engine)
    return table


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record(row) -> OperatorCommandRecord:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, ValueError) as exc:
        raise OperatorCommandError(
            f"Operator command {row.command_id!r} has invalid payload JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise OperatorCommandError(
            f"Operator command {row.command_id!r} payload is not an object"
        )
    return OperatorCommandRecord(
        command_id=str(row.command_id),
        idempotency_key=str(row.idempotency_key),
        command_type=OperatorCommandType(row.command_type),
        symbol=str(row.symbol or "").upper(),
        payload=payload,
        status=OperatorCommandStatus(row.status),
        requested_by_device=str(row.requested_by_device or ""),
        requested_by_host=str(row.requested_by_host or ""),
        created_at=_aware(row.created_at) or datetime.min.replace(tzinfo=timezone.utc),
        accepted_at=_aware(row.accepted_at),
        executing_at=_aware(row.executing_at),
        broker_submitted_at=_aware(row.broker_submitted_at),
        completed_at=_aware(row.completed_at),
        rejected_at=_aware(row.rejected_at),
        failed_at=_aware(row.failed_at),
        error_message=str(row.error_message or ""),
        executor_device_id=str(row.executor_device_id or ""),
        executor_host=str(row.executor_host or ""),
        broker_order_id=str(row.broker_order_id or ""),
        state_before_hash=str(row.state_before_hash or ""),
        state_after_hash=str(row.state_after_hash or ""),
    )


def _current_owner_device_id(conn, state_table: Table, state_key: str) -> str:
    row = conn.execute(
        select(state_table)
        .where(state_table.c.state_key == state_key)
        .with_for_update()
    ).first()
    if row is None:
        return ""
    payload = _decode_payload(row.payload, state_key)
    if state_key == OPERATOR_CONTROL_KEY and bool(payload.get("locked", False)):
        return ""
    return str(payload.get("device_id") or "").strip()


def submit_operator_command(
    engine: Engine,
    requester: LocalDeviceRole,
    command_type: OperatorCommandType | str,
    *,
    symbol: str = "",
    payload: Optional[Dict[str, Any]] = None,
    idempotency_key: str,
    command_id: str = "",
) -> OperatorCommandInsertResult:
    """Insert one PENDING request iff ``requester`` owns operator control.

    A duplicate idempotency key returns the original row with ``created=False``
    and never inserts a second request.
    """

    if engine is None:
        raise OperatorCommandError("Operator command database is unavailable")
    command_type = (
        command_type
        if isinstance(command_type, OperatorCommandType)
        else OperatorCommandType(str(command_type))
    )
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key:
        raise ValueError("Operator command requires an idempotency key")
    command_id = str(command_id or uuid.uuid4().hex).strip()
    payload_json = json.dumps(
        dict(payload or {}), default=str, separators=(",", ":")
    )
    table = ensure_operator_commands_table(engine)
    state_table = _ensure_state_sync_table(engine)

    try:
        with engine.begin() as conn:
            existing = conn.execute(
                select(table).where(table.c.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                return OperatorCommandInsertResult(_record(existing), False)
            owner_device_id = _current_owner_device_id(
                conn, state_table, OPERATOR_CONTROL_KEY
            )
            if not owner_device_id:
                raise OperatorControlNotOwnedError(
                    "Operator Control is Locked; no new manual commands are accepted."
                )
            if owner_device_id != requester.device_id:
                raise OperatorControlNotOwnedError(
                    "This device is not the current Operator Control owner."
                )
            conflict_types = {
                OperatorCommandType.SELL_PARTIAL: {
                    OperatorCommandType.SELL_PARTIAL,
                    OperatorCommandType.SELL_ALL,
                    OperatorCommandType.SET_PARTIAL_SELL_QUANTITY,
                },
                OperatorCommandType.SELL_ALL: {
                    OperatorCommandType.SELL_PARTIAL,
                    OperatorCommandType.SELL_ALL,
                    OperatorCommandType.SET_PARTIAL_SELL_QUANTITY,
                },
                OperatorCommandType.SET_PARTIAL_SELL_QUANTITY: {
                    OperatorCommandType.SELL_PARTIAL,
                    OperatorCommandType.SELL_ALL,
                    OperatorCommandType.SET_PARTIAL_SELL_QUANTITY,
                },
                OperatorCommandType.MOVE_STOP_BREAKEVEN: {
                    OperatorCommandType.MOVE_STOP_BREAKEVEN,
                    OperatorCommandType.MOVE_STOP_MANUAL_PRICE,
                },
                OperatorCommandType.MOVE_STOP_MANUAL_PRICE: {
                    OperatorCommandType.MOVE_STOP_BREAKEVEN,
                    OperatorCommandType.MOVE_STOP_MANUAL_PRICE,
                },
            }.get(command_type, {command_type})
            active_statuses = {
                OperatorCommandStatus.PENDING.value,
                OperatorCommandStatus.ACCEPTED.value,
                OperatorCommandStatus.EXECUTING.value,
                OperatorCommandStatus.BROKER_SUBMITTED.value,
                OperatorCommandStatus.PARTIALLY_FILLED.value,
            }
            conflict = conn.execute(
                select(table.c.command_id)
                .where(table.c.symbol == str(symbol or "").strip().upper())
                .where(
                    table.c.command_type.in_(
                        [item.value for item in conflict_types]
                    )
                )
                .where(table.c.status.in_(active_statuses))
                .limit(1)
            ).first()
            if conflict is not None:
                raise OperatorCommandError(
                    f"A conflicting operator command is already active for "
                    f"{str(symbol or '').strip().upper() or 'this scope'}."
                )
            conn.execute(
                table.insert().values(
                    command_id=command_id,
                    idempotency_key=idempotency_key,
                    command_type=command_type.value,
                    symbol=str(symbol or "").strip().upper(),
                    payload_json=payload_json,
                    status=OperatorCommandStatus.PENDING.value,
                    requested_by_device=requester.device_id,
                    requested_by_host=requester.hostname,
                    created_at=_server_now(engine),
                    error_message="",
                )
            )
            row = conn.execute(
                select(table).where(table.c.command_id == command_id)
            ).first()
        return OperatorCommandInsertResult(_record(row), True)
    except IntegrityError:
        with engine.connect() as conn:
            existing = conn.execute(
                select(table).where(table.c.idempotency_key == idempotency_key)
            ).first()
        if existing is not None:
            return OperatorCommandInsertResult(_record(existing), False)
        raise


def claim_next_operator_command(
    engine: Engine,
    executor: LocalDeviceRole,
) -> Optional[OperatorCommandRecord]:
    """Atomically accept the oldest pending request on the execution owner."""

    table = ensure_operator_commands_table(engine)
    state_table = _ensure_state_sync_table(engine)
    # Most heartbeats have no human command to consume. Avoid requiring a
    # coordination row merely to prove an empty queue (important during
    # startup/recovery and for compatibility runtimes); a command that lands
    # after this observation is picked up on the next heartbeat.
    with engine.connect() as conn:
        pending_exists = conn.execute(
            select(table.c.command_id)
            .where(table.c.status == OperatorCommandStatus.PENDING.value)
            .limit(1)
        ).first()
    if pending_exists is None:
        return None
    with engine.begin() as conn:
        owner_device_id = _current_owner_device_id(conn, state_table, MAIN_DEVICE_KEY)
        if owner_device_id != executor.device_id:
            raise ExecutionOwnerMismatchError(
                "Only the current Execution Owner may process operator commands."
            )
        statement = (
            select(table)
            .where(table.c.status == OperatorCommandStatus.PENDING.value)
            .order_by(table.c.created_at.asc(), table.c.command_id.asc())
            .limit(1)
            .with_for_update()
        )
        row = conn.execute(statement).first()
        if row is None:
            return None
        result = conn.execute(
            table.update()
            .where(table.c.command_id == row.command_id)
            .where(table.c.status == OperatorCommandStatus.PENDING.value)
            .values(
                status=OperatorCommandStatus.ACCEPTED.value,
                accepted_at=_server_now(engine),
                executor_device_id=executor.device_id,
                executor_host=executor.hostname,
            )
        )
        if result.rowcount != 1:
            return None
        accepted = conn.execute(
            select(table).where(table.c.command_id == row.command_id)
        ).first()
    return _record(accepted)


def start_operator_command(
    engine: Engine,
    executor: LocalDeviceRole,
    command_id: str,
) -> OperatorCommandRecord:
    """Move an accepted command to EXECUTING without rechecking its requester."""

    table = ensure_operator_commands_table(engine)
    state_table = _ensure_state_sync_table(engine)
    with engine.begin() as conn:
        owner_device_id = _current_owner_device_id(conn, state_table, MAIN_DEVICE_KEY)
        if owner_device_id != executor.device_id:
            raise ExecutionOwnerMismatchError(
                "Execution ownership changed before the operator command ran."
            )
        result = conn.execute(
            table.update()
            .where(table.c.command_id == str(command_id))
            .where(table.c.status == OperatorCommandStatus.ACCEPTED.value)
            .where(table.c.executor_device_id == executor.device_id)
            .values(
                status=OperatorCommandStatus.EXECUTING.value,
                executing_at=_server_now(engine),
            )
        )
        if result.rowcount != 1:
            raise OperatorCommandTransitionError(
                "Operator command is not accepted by this execution owner."
            )
        row = conn.execute(
            select(table).where(table.c.command_id == str(command_id))
        ).first()
    return _record(row)


def finish_operator_command(
    engine: Engine,
    executor: LocalDeviceRole,
    command_id: str,
    status: OperatorCommandStatus | str,
    *,
    error_message: str = "",
    broker_order_id: str = "",
    state_before_hash: str = "",
    state_after_hash: str = "",
) -> OperatorCommandRecord:
    """Persist the executor's validated outcome for one claimed request."""

    status = (
        status
        if isinstance(status, OperatorCommandStatus)
        else OperatorCommandStatus(str(status))
    )
    allowed = {
        OperatorCommandStatus.REJECTED,
        OperatorCommandStatus.BROKER_SUBMITTED,
        OperatorCommandStatus.FILLED,
        OperatorCommandStatus.PARTIALLY_FILLED,
        OperatorCommandStatus.COMPLETED,
        OperatorCommandStatus.FAILED,
        OperatorCommandStatus.CANCELLED,
        OperatorCommandStatus.EXPIRED,
    }
    if status not in allowed:
        raise OperatorCommandTransitionError(
            f"Cannot finish an operator command as {status.value}."
        )
    timestamp_column = {
        OperatorCommandStatus.REJECTED: "rejected_at",
        OperatorCommandStatus.BROKER_SUBMITTED: "broker_submitted_at",
        OperatorCommandStatus.FAILED: "failed_at",
    }.get(status, "completed_at")
    values: Dict[str, Any] = {
        "status": status.value,
        timestamp_column: _server_now(engine),
        "error_message": str(error_message or ""),
        "broker_order_id": str(broker_order_id or ""),
        "state_before_hash": str(state_before_hash or ""),
        "state_after_hash": str(state_after_hash or ""),
    }
    source_statuses = {
        OperatorCommandStatus.EXECUTING.value,
    }
    if status in {
        OperatorCommandStatus.PARTIALLY_FILLED,
        OperatorCommandStatus.FILLED,
        OperatorCommandStatus.FAILED,
        OperatorCommandStatus.CANCELLED,
        OperatorCommandStatus.EXPIRED,
    }:
        source_statuses.update(
            {
                OperatorCommandStatus.BROKER_SUBMITTED.value,
                OperatorCommandStatus.PARTIALLY_FILLED.value,
            }
        )
    table = ensure_operator_commands_table(engine)
    with engine.begin() as conn:
        result = conn.execute(
            table.update()
            .where(table.c.command_id == str(command_id))
            .where(table.c.status.in_(source_statuses))
            .where(table.c.executor_device_id == executor.device_id)
            .values(**values)
        )
        if result.rowcount != 1:
            raise OperatorCommandTransitionError(
                "Operator command is not executing on this device."
            )
        row = conn.execute(
            select(table).where(table.c.command_id == str(command_id))
        ).first()
    return _record(row)


def get_operator_command(
    engine: Engine, command_id: str
) -> Optional[OperatorCommandRecord]:
    table = ensure_operator_commands_table(engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.command_id == str(command_id))
        ).first()
    return _record(row) if row is not None else None


def list_operator_commands(
    engine: Engine,
    *,
    statuses: Optional[Iterable[OperatorCommandStatus | str]] = None,
    limit: int = 100,
) -> list[OperatorCommandRecord]:
    table = ensure_operator_commands_table(engine)
    statement = select(table).order_by(
        table.c.created_at.desc(), table.c.command_id.desc()
    )
    if statuses is not None:
        values = [
            item.value if isinstance(item, OperatorCommandStatus) else str(item)
            for item in statuses
        ]
        statement = statement.where(table.c.status.in_(values))
    statement = statement.limit(max(1, int(limit)))
    with engine.connect() as conn:
        rows = conn.execute(statement).fetchall()
    return [_record(row) for row in rows]
