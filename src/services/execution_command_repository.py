"""Durable command idempotency ledger.

``docs/kanban_production_readiness.md``, Workstream 2 (A5), signed off in
that document's revision 3.1, amended by revision 3.2: "A unique constraint
on the idempotency key must prevent duplicate submit, cancel, and replace
operations after restart or device handoff." (INV-15).

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

Every write primitive here (``insert_command``/``update_command_response``)
accepts an already-open SQLAlchemy ``Connection`` (revision 3.2) so a
caller -- the execution gateway, once it exists (Workstream 3) -- can
compose this table's write into a single transaction alongside
``execution_orders`` and a capital reservation, satisfying A1's atomicity
requirement. ``record_command``/``update_command_response`` remain as
convenience wrappers that open and commit their own transaction, for
callers (and this module's own tests) that don't need cross-table
atomicity.
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
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from src.utils.redaction import hash_redacted_payload, redact_payload

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


class CommandResponseConflictError(RuntimeError):
    """The compare-and-set guard on :func:`update_command_response` failed
    (revision 3.2): the stored row was no longer ``status='REQUESTED'`` at
    ``expected_version``, so this write did not apply. A broker response
    must never silently overwrite an already-recorded response -- once the
    row is ACKNOWLEDGED/FAILED, that outcome is final; a caller retrying a
    stale write is a bug and must find out, not fail open and clobber the
    real outcome."""


def _redact_broker_response(response: Any, *, account_no: str) -> Dict[str, Any]:
    """Runs a raw broker response through the codebase's shared,
    dependency-neutral redaction utility (:mod:`src.utils.redaction`)
    before it is ever persisted (revision 3.2) -- see
    :func:`src.core.discovered_external_order._redact_raw_response` for
    the same pattern applied to a discovered order's own payload.
    """
    if not isinstance(response, dict):
        response = {"raw": response}
    return redact_payload(response, account_no=account_no)


@dataclass
class ExecutionCommand:
    idempotency_key: str
    command_type: str  # "submit" | "cancel" | "replace"
    environment: str
    account_no: str
    symbol: str
    lease_epoch: int
    owner_device_id: str = ""
    lease_token: str = ""
    target_broker_order_id: str = ""
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # PRE_BROKER_ABORTED is a definitive local outcome: the command was
    # journaled, but a final mutable gate failed before any broker call.
    status: str = "REQUESTED"  # -> ACKNOWLEDGED | FAILED | AMBIGUOUS | PRE_BROKER_ABORTED
    redacted_response: Dict[str, Any] = field(default_factory=dict)
    response_hash: str = ""
    command_id: Optional[int] = None
    version: int = 1
    # Workstream 9 (PR2): which frontend/caller issued this command --
    # src.core.execution_mode.ExecutionSource's value, or "" for a command
    # journaled before this field existed / by a caller that hasn't been
    # migrated to attribute a source yet. Purely observational -- nothing
    # in this module gates on it.
    source: str = ""

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
        self.owner_device_id = str(self.owner_device_id or "")
        self.lease_token = str(self.lease_token or "")
        self.target_broker_order_id = str(self.target_broker_order_id or "")
        self.status = str(self.status or "REQUESTED").upper()
        # Always redact, not only when the value isn't already a dict --
        # a caller passing an unredacted dict directly must not bypass
        # redaction just because it's already the right container type.
        # Idempotent when the value has already been redacted (e.g.
        # reconstructing from a stored, already-redacted row).
        self.redacted_response = _redact_broker_response(
            self.redacted_response, account_no=self.account_no
        )
        self.response_hash = str(self.response_hash or "")
        self.version = int(self.version or 1)
        self.source = str(self.source or "")


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
        Column("owner_device_id", String(64), nullable=False, server_default=""),
        Column("lease_token", String(128), nullable=False, server_default=""),
        Column("target_broker_order_id", String(64), nullable=False, server_default=""),
        Column("requested_at", DateTime, nullable=False),
        Column("status", String(24), nullable=False, server_default="REQUESTED"),
        Column("redacted_response", Text(length=16_777_215), nullable=False, server_default="{}"),
        Column("response_hash", String(64), nullable=False, server_default=""),
        Column("version", BigInteger, nullable=False, server_default="1"),
        Column("source", String(32), nullable=False, server_default=""),
        UniqueConstraint("idempotency_key", name="uq_execution_commands_idempotency_key"),
    )


def ensure_execution_commands_table(engine: Engine) -> Table:
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
    """Raises on a blank or malformed timestamp (revision 3.2) rather than
    silently substituting ``now()`` -- a corrupted or truncated
    ``requested_at`` is a data-integrity problem the caller needs to find
    out about, not a routine gap papered over with the current time (which
    would misrepresent when the command was actually requested, an audit
    field B4a's whole point is to get right)."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("requested_at must not be blank")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"requested_at={value!r} is not a valid ISO-8601 timestamp") from exc


def _row_to_command(row) -> ExecutionCommand:
    try:
        redacted_response = json.loads(row.redacted_response or "{}")
    except (TypeError, ValueError):
        redacted_response = {}
    return ExecutionCommand(
        idempotency_key=row.idempotency_key,
        command_type=row.command_type,
        environment=row.environment,
        account_no=row.account_no,
        symbol=row.symbol,
        lease_epoch=row.lease_epoch,
        owner_device_id=row.owner_device_id,
        lease_token=row.lease_token,
        target_broker_order_id=row.target_broker_order_id,
        requested_at=row.requested_at.isoformat() if row.requested_at else "",
        status=row.status,
        redacted_response=redacted_response,
        response_hash=row.response_hash,
        command_id=row.id,
        version=row.version,
        source=getattr(row, "source", "") or "",
    )


# --- shared-transaction primitives (revision 3.2) ---------------------------


def insert_command(conn: Connection, command: ExecutionCommand) -> ExecutionCommand:
    """B4a: the mandatory command journal write, made *before* any broker
    call. Takes an already-open ``Connection`` so a caller can compose
    this write into a larger atomic transaction (A1). Raises
    :class:`DuplicateCommandError` on a repeated ``idempotency_key`` --
    the caller must not then call the broker a second time.
    """
    table = _get_execution_commands_table(MetaData())
    try:
        result = conn.execute(
            table.insert().values(
                idempotency_key=command.idempotency_key,
                command_type=command.command_type,
                environment=command.environment,
                account_no=command.account_no,
                symbol=command.symbol,
                lease_epoch=command.lease_epoch,
                owner_device_id=command.owner_device_id,
                lease_token=command.lease_token,
                target_broker_order_id=command.target_broker_order_id,
                requested_at=_parse_dt(command.requested_at),
                status=command.status,
                redacted_response=json.dumps(command.redacted_response, separators=(",", ":")),
                response_hash=command.response_hash,
                version=command.version,
                source=command.source,
            )
        )
    except IntegrityError as exc:
        raise DuplicateCommandError(
            f"Command with idempotency_key={command.idempotency_key!r} was already recorded"
        ) from exc
    command.command_id = result.inserted_primary_key[0]
    return command


def update_command_response(
    conn: Connection,
    idempotency_key: str,
    *,
    status: str,
    broker_response: Dict[str, Any],
    expected_version: Optional[int] = None,
) -> ExecutionCommand:
    """B4b: the post-call broker-response persist. Takes an already-open
    ``Connection`` for the same composability reason as
    :func:`insert_command`. A failure here must never be treated as
    license to retry the broker call (INV-23) -- that rule is enforced by
    the execution gateway (Workstream 3), not this repository; this
    function only records the outcome once the gateway has already
    decided not to retry.

    Two revision-3.2 corrections over the original version:

    - ``account_no`` for redaction is looked up from the command's *own*
      stored row, never trusted from an optional caller-supplied
      parameter -- a caller that forgets (or gets it wrong) must not be
      able to silently defeat redaction of its own command's response.
    - The update is a compare-and-set: it only applies while the stored
      row is still ``status='REQUESTED'`` at ``expected_version``,
      incrementing the version on success. A second write to an
      already-ACKNOWLEDGED/FAILED row -- whether a stale retry, a race, or
      a bug -- must fail loudly (:class:`CommandResponseConflictError`)
      rather than fail open and overwrite a real, already-recorded
      outcome. ``expected_version`` defaults to the command's own
      ``version=1`` construction default when omitted, matching the
      common single-write case.
    """
    table = _get_execution_commands_table(MetaData())
    current = conn.execute(
        select(table.c.account_no, table.c.status, table.c.version).where(
            table.c.idempotency_key == idempotency_key
        )
    ).first()
    if current is None:
        raise CommandNotFoundError(f"No command found for idempotency_key={idempotency_key!r}")

    guard_version = int(current.version if expected_version is None else expected_version)
    redacted = _redact_broker_response(broker_response or {}, account_no=current.account_no)
    response_hash = hash_redacted_payload(redacted)
    next_version = guard_version + 1
    result = conn.execute(
        table.update()
        .where(
            table.c.idempotency_key == idempotency_key,
            table.c.status == "REQUESTED",
            table.c.version == guard_version,
        )
        .values(
            status=str(status or "").upper(),
            redacted_response=json.dumps(redacted, separators=(",", ":")),
            response_hash=response_hash,
            version=next_version,
        )
    )
    if result.rowcount == 0:
        raise CommandResponseConflictError(
            f"idempotency_key={idempotency_key!r} response update rejected -- expected "
            f"status=REQUESTED and version={guard_version}, found status={current.status!r} "
            f"version={current.version}"
        )
    row = conn.execute(select(table).where(table.c.idempotency_key == idempotency_key)).first()
    return _row_to_command(row)


def get_command(conn: Connection, idempotency_key: str) -> Optional[ExecutionCommand]:
    table = _get_execution_commands_table(MetaData())
    row = conn.execute(select(table).where(table.c.idempotency_key == idempotency_key)).first()
    return _row_to_command(row) if row is not None else None


# --- standalone convenience wrappers (own transaction each) -----------------


def record_command(engine: Engine, command: ExecutionCommand) -> ExecutionCommand:
    """Convenience wrapper around :func:`insert_command` that opens and
    commits its own transaction, for callers that don't need cross-table
    atomicity (e.g. tests, or a command that genuinely has no reservation/
    order-record counterpart)."""
    ensure_execution_commands_table(engine)
    with engine.begin() as conn:
        return insert_command(conn, command)


def get_command_by_idempotency_key(engine: Engine, idempotency_key: str) -> Optional[ExecutionCommand]:
    ensure_execution_commands_table(engine)
    with engine.begin() as conn:
        return get_command(conn, idempotency_key)


def record_command_response(
    engine: Engine,
    idempotency_key: str,
    *,
    status: str,
    broker_response: Dict[str, Any],
    expected_version: Optional[int] = None,
) -> ExecutionCommand:
    """Convenience wrapper around :func:`update_command_response` that
    opens and commits its own transaction."""
    ensure_execution_commands_table(engine)
    with engine.begin() as conn:
        return update_command_response(
            conn,
            idempotency_key,
            status=status,
            broker_response=broker_response,
            expected_version=expected_version,
        )
