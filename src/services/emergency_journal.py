"""Durable local command journal for bounded database-outage protection.

Every line is checksum-protected and appended under a cross-process lock,
then fsynced before the caller may cross the broker boundary.  The file is
append-only: reconciliation writes a new ``RECONCILED`` marker and a
canonical database mapping rather than mutating old lines in place.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from uuid import uuid4

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
from sqlalchemy.engine import Engine

from src.core.execution_mode import ExecutionLease
from src.core.execution_order_record import (
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    allowed_status_transitions,
    apply_status_transition,
)
from src.core.order_recovery_state import (
    OrderRecoveryState,
    validate_recovery_transition,
)
from src.core.order_state import OrderIntent, OrderSide, OrderStatus
from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus
from src.services import trade_card_repository as trade_card_repo
from src.services.execution_command_repository import (
    ExecutionCommand,
    ensure_execution_commands_table,
    get_command,
    insert_command,
    update_command_response,
)
from src.services.execution_order_repository import (
    ensure_execution_orders_table,
    get_execution_order,
    insert_execution_order,
    update_execution_order,
)
from src.utils.config import DATA_DIR

EMERGENCY_JOURNAL_FILE = DATA_DIR / "emergency_execution_journal.jsonl"
EMERGENCY_JOURNAL_SCHEMA_VERSION = 1
_LOCK_TIMEOUT_SECONDS = 2.0
_STALE_LOCK_SECONDS = 30.0
_THREAD_LOCK = threading.RLock()


class EmergencyJournalError(RuntimeError):
    pass


class EmergencyJournalIntegrityError(EmergencyJournalError):
    pass


class DuplicateEmergencyCommandError(EmergencyJournalError):
    pass


class EmergencyLeaseAllowanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmergencyLeaseSnapshot:
    device_id: str
    lease_token: str
    lease_epoch: int
    verified_at_monotonic: float
    outage_started_monotonic: Optional[float]
    expires_at_monotonic: float


class EmergencyLeaseAllowance:
    """Monotonic, process-local proof for emergency-only outage actions.

    A restart deliberately loses the allowance and therefore fails closed;
    wall-clock changes cannot extend it.
    """

    def __init__(
        self,
        *,
        max_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if float(max_seconds) <= 0:
            raise ValueError("Emergency lease allowance must be positive")
        self._max_seconds = float(max_seconds)
        self._monotonic = monotonic
        self._snapshot: Optional[EmergencyLeaseSnapshot] = None
        self._lock = threading.RLock()

    def record_verified(
        self,
        lease: Optional[ExecutionLease],
        *,
        verified_current: bool,
        handoff_pending: bool,
    ) -> bool:
        with self._lock:
            if (
                not verified_current
                or handoff_pending
                or lease is None
                or not str(lease.device_id or "")
                or not str(lease.lease_token or "")
                or int(lease.lease_epoch or 0) <= 0
            ):
                self._snapshot = None
                return False
            now = self._monotonic()
            self._snapshot = EmergencyLeaseSnapshot(
                device_id=lease.device_id,
                lease_token=lease.lease_token,
                lease_epoch=int(lease.lease_epoch),
                verified_at_monotonic=now,
                outage_started_monotonic=None,
                expires_at_monotonic=now + self._max_seconds,
            )
            return True

    def begin_outage(
        self,
        lease: Optional[ExecutionLease],
        *,
        verified_current: bool,
        handoff_pending: bool,
    ) -> bool:
        """Activate the already-earned allowance without extending it."""

        with self._lock:
            snapshot = self._snapshot
            now = self._monotonic()
            if (
                not verified_current
                or handoff_pending
                or lease is None
                or snapshot is None
                or now >= snapshot.expires_at_monotonic
                or lease.device_id != snapshot.device_id
                or lease.lease_token != snapshot.lease_token
                or int(lease.lease_epoch or 0) != snapshot.lease_epoch
            ):
                self._snapshot = None
                return False
            self._snapshot = EmergencyLeaseSnapshot(
                device_id=snapshot.device_id,
                lease_token=snapshot.lease_token,
                lease_epoch=snapshot.lease_epoch,
                verified_at_monotonic=snapshot.verified_at_monotonic,
                outage_started_monotonic=now,
                expires_at_monotonic=snapshot.expires_at_monotonic,
            )
            return True

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None

    def require_valid(self, lease: Optional[ExecutionLease]) -> EmergencyLeaseSnapshot:
        with self._lock:
            snapshot = self._snapshot
            now = self._monotonic()
            if snapshot is None:
                raise EmergencyLeaseAllowanceError(
                    "No current lease proof is available for the database outage"
                )
            if snapshot.outage_started_monotonic is None:
                raise EmergencyLeaseAllowanceError(
                    "The canonical database outage has not activated emergency mode"
                )
            if now >= snapshot.expires_at_monotonic:
                raise EmergencyLeaseAllowanceError(
                    "The monotonic emergency lease allowance expired"
                )
            if lease is None or (
                lease.device_id != snapshot.device_id
                or lease.lease_token != snapshot.lease_token
                or int(lease.lease_epoch or 0) != snapshot.lease_epoch
            ):
                raise EmergencyLeaseAllowanceError(
                    "The emergency action does not carry the exact cached lease"
                )
            return snapshot

    @property
    def snapshot(self) -> Optional[EmergencyLeaseSnapshot]:
        with self._lock:
            return self._snapshot


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    descriptor: Optional[int] = None
    with _THREAD_LOCK:
        while descriptor is None:
            try:
                descriptor = os.open(
                    lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
            except FileExistsError:
                try:
                    stale = time.time() - lock_path.stat().st_mtime > _STALE_LOCK_SECONDS
                except OSError:
                    stale = False
                if stale:
                    with suppress(OSError):
                        lock_path.unlink()
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for emergency-journal lock: {lock_path}"
                    )
                time.sleep(0.025)
        try:
            yield
        finally:
            os.close(descriptor)
            with suppress(OSError):
                lock_path.unlink()


def _canonical_bytes(record: Dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in record.items() if key != "checksum"}
    return json.dumps(
        unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _checksum(record: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(record)).hexdigest()


def _reconciliation_table(metadata: MetaData) -> Table:
    return Table(
        "emergency_journal_reconciliation",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("journal_id", String(64), nullable=False),
        Column("requested_sequence", BigInteger, nullable=False),
        Column("idempotency_key", String(160), nullable=False),
        Column("request_checksum", String(64), nullable=False),
        Column("payload", Text(length=16_777_215), nullable=False),
        Column("reconciled_at", DateTime, nullable=False),
        UniqueConstraint(
            "journal_id",
            "requested_sequence",
            name="uq_emergency_reconciliation_sequence",
        ),
        UniqueConstraint(
            "journal_id",
            "idempotency_key",
            name="uq_emergency_reconciliation_command",
        ),
    )


def ensure_emergency_reconciliation_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _reconciliation_table(metadata)
    metadata.create_all(engine)
    return table


def _server_now(engine: Engine):
    return func.utc_timestamp(6) if engine.dialect.name == "mysql" else func.current_timestamp()


class EmergencyJournal:
    def __init__(
        self,
        path: Path = EMERGENCY_JOURNAL_FILE,
        *,
        journal_id: str = "",
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.path = Path(path)
        resolved_id = str(journal_id or "").strip()
        if not resolved_id:
            resolved_id = hashlib.sha256(
                str(self.path.resolve(strict=False)).encode("utf-8")
            ).hexdigest()[:32]
        self.journal_id = resolved_id
        self._wall_clock = wall_clock

    def _read_unlocked(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        prior_sequence = 0
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EmergencyJournalIntegrityError(
                    f"Invalid emergency journal JSON at line {line_number}"
                ) from exc
            if not isinstance(entry, dict) or entry.get("checksum") != _checksum(entry):
                raise EmergencyJournalIntegrityError(
                    f"Emergency journal checksum mismatch at line {line_number}"
                )
            sequence = int(entry.get("sequence") or 0)
            if sequence <= prior_sequence:
                raise EmergencyJournalIntegrityError(
                    "Emergency journal sequence is not strictly monotonic"
                )
            prior_sequence = sequence
            entries.append(entry)
        return entries

    def load_entries(self) -> List[Dict[str, Any]]:
        with _exclusive_lock(self.path):
            return self._read_unlocked()

    def _append_unlocked(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        existing: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        sequence = int(existing[-1]["sequence"] if existing else 0) + 1
        record = {
            "schema_version": EMERGENCY_JOURNAL_SCHEMA_VERSION,
            "journal_id": self.journal_id,
            "sequence": sequence,
            "event_id": uuid4().hex,
            "event_type": str(event_type).upper(),
            "created_at": self._wall_clock().astimezone(timezone.utc).isoformat(),
            **payload,
        }
        record["checksum"] = _checksum(record)
        encoded = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError("Short emergency-journal append")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return record

    def _append(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.path):
            existing = self._read_unlocked()
            return self._append_unlocked(event_type, payload, existing=existing)

    def append_requested(
        self,
        *,
        idempotency_key: str,
        command_type: str,
        environment: str,
        account_no: str,
        symbol: str,
        lease: ExecutionLease,
        source: str,
        ownership_proof: Optional[Dict[str, Any]] = None,
        target_broker_order_id: str = "",
        order_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("Emergency command requires idempotency_key")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.path):
            existing = self._read_unlocked()
            if any(
                entry.get("event_type") == "REQUESTED"
                and entry.get("idempotency_key") == key
                for entry in existing
            ):
                raise DuplicateEmergencyCommandError(
                    f"Emergency command {key!r} was already journaled"
                )
            return self._append_unlocked(
                "REQUESTED",
                {
                    "idempotency_key": key,
                    "command_type": str(command_type or "").lower(),
                    "environment": str(environment or "").upper(),
                    "account_no": str(account_no or ""),
                    "symbol": str(symbol or "").upper(),
                    "device_id": lease.device_id,
                    "lease_token": lease.lease_token,
                    "lease_epoch": int(lease.lease_epoch),
                    "source": str(source or ""),
                    "ownership_proof": dict(ownership_proof or {}),
                    "target_broker_order_id": str(target_broker_order_id or ""),
                    "order_payload": dict(order_payload or {}),
                    "reconciled": False,
                },
                existing=existing,
            )

    def append_outcome(
        self,
        *,
        requested_sequence: int,
        idempotency_key: str,
        status: str,
        broker_response: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._append(
            "OUTCOME",
            {
                "requested_sequence": int(requested_sequence),
                "idempotency_key": str(idempotency_key or ""),
                "status": str(status or "").upper(),
                "broker_response": dict(broker_response or {}),
            },
        )

    def append_reconciled(
        self, *, requested_sequence: int, idempotency_key: str
    ) -> Dict[str, Any]:
        return self._append(
            "RECONCILED",
            {
                "requested_sequence": int(requested_sequence),
                "idempotency_key": str(idempotency_key or ""),
            },
        )

    def pending_requests(self) -> List[Dict[str, Any]]:
        entries = self.load_entries()
        reconciled = {
            int(entry.get("requested_sequence") or 0)
            for entry in entries
            if entry.get("event_type") == "RECONCILED"
        }
        return [
            entry
            for entry in entries
            if entry.get("event_type") == "REQUESTED"
            and int(entry["sequence"]) not in reconciled
        ]

    def reconcile_into_canonical(self, engine: Engine) -> int:
        """Fold pending local commands into canonical command state."""

        ensure_execution_commands_table(engine)
        ensure_execution_orders_table(engine)
        trade_card_table = trade_card_repo.ensure_trade_cards_table(engine)
        table = ensure_emergency_reconciliation_table(engine)
        entries = self.load_entries()
        outcomes = {
            int(entry.get("requested_sequence") or 0): entry
            for entry in entries
            if entry.get("event_type") == "OUTCOME"
        }
        reconciled_sequences = {
            int(entry.get("requested_sequence") or 0)
            for entry in entries
            if entry.get("event_type") == "RECONCILED"
        }
        requests = [
            entry
            for entry in entries
            if entry.get("event_type") == "REQUESTED"
            and int(entry["sequence"]) not in reconciled_sequences
        ]
        completed: List[tuple[int, str]] = []
        for request in requests:
            sequence = int(request["sequence"])
            key = str(request["idempotency_key"])
            outcome = outcomes.get(sequence)
            with engine.begin() as conn:
                marker = conn.execute(
                    select(table.c.id).where(
                        table.c.journal_id == self.journal_id,
                        table.c.requested_sequence == sequence,
                    )
                ).first()
                command = get_command(conn, key)
                if command is None:
                    command = insert_command(
                        conn,
                        ExecutionCommand(
                            idempotency_key=key,
                            command_type=request["command_type"],
                            environment=request["environment"],
                            account_no=request["account_no"],
                            symbol=request["symbol"],
                            lease_epoch=int(request["lease_epoch"]),
                            owner_device_id=request["device_id"],
                            lease_token=request["lease_token"],
                            target_broker_order_id=request.get(
                                "target_broker_order_id", ""
                            ),
                            requested_at=request["created_at"],
                            source=request.get("source", ""),
                        ),
                    )
                if outcome is not None and command.status == "REQUESTED":
                    command = update_command_response(
                        conn,
                        key,
                        status=outcome["status"],
                        broker_response=outcome.get("broker_response") or {},
                        expected_version=command.version,
                    )
                order_payload = request.get("order_payload") or {}
                if (
                    request.get("command_type") == "submit"
                    and order_payload.get("client_order_id")
                    and get_execution_order(conn, order_payload["client_order_id"])
                    is None
                ):
                    record = ExecutionOrderRecord(
                        environment=request["environment"],
                        account_no=request["account_no"],
                        symbol=request["symbol"],
                        side=OrderSide(order_payload["side"]),
                        intent=OrderIntent(order_payload["intent"]),
                        client_order_id=order_payload["client_order_id"],
                        attempt_group_id=order_payload.get("attempt_group_id", ""),
                        attempt_number=order_payload.get("attempt_number", 1),
                        submitted_quantity=order_payload.get("quantity", 0),
                        submitted_limit_price=order_payload.get("limit_price", 0.0),
                        remaining_quantity=order_payload.get("quantity", 0),
                        exchange=order_payload.get("exchange", "NASD"),
                        execution_policy=order_payload.get("execution_policy", ""),
                        owner_device_id=request["device_id"],
                        lease_token=request["lease_token"],
                        lease_epoch=request["lease_epoch"],
                    )
                    apply_status_transition(record, ExecutionOrderStatus.SUBMITTING)
                    outcome_status = str(
                        (outcome or {}).get("status") or "AMBIGUOUS"
                    ).upper()
                    broker_response = (outcome or {}).get("broker_response") or {}
                    if outcome_status == "ACKNOWLEDGED" and broker_response.get(
                        "broker_order_id"
                    ):
                        apply_status_transition(
                            record,
                            ExecutionOrderStatus.ACKNOWLEDGED,
                            broker_order_id=broker_response["broker_order_id"],
                        )
                    elif outcome_status == "FAILED":
                        record.remaining_quantity = 0
                        apply_status_transition(record, ExecutionOrderStatus.REJECTED)
                    else:
                        apply_status_transition(
                            record, ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE
                        )
                    insert_execution_order(conn, record)
                elif request.get("command_type") == "cancel":
                    client_order_id = str(order_payload.get("client_order_id") or "")
                    record = (
                        get_execution_order(conn, client_order_id)
                        if client_order_id
                        else None
                    )
                    if record is not None and outcome is not None:
                        outcome_status = str(outcome.get("status") or "").upper()
                        broker_response = outcome.get("broker_response") or {}
                        if outcome_status == "ACKNOWLEDGED":
                            if (
                                record.status != ExecutionOrderStatus.CANCEL_PENDING
                                and ExecutionOrderStatus.CANCEL_PENDING
                                in allowed_status_transitions(record.status)
                            ):
                                apply_status_transition(
                                    record, ExecutionOrderStatus.CANCEL_PENDING
                                )
                            normalized = str(
                                broker_response.get("normalized_status") or ""
                            ).upper()
                            record.filled_quantity = max(
                                record.filled_quantity,
                                int(broker_response.get("filled_quantity") or 0),
                            )
                            record.remaining_quantity = max(
                                0,
                                int(broker_response.get("remaining_quantity") or 0),
                            )
                            if broker_response.get("average_fill_price"):
                                record.average_fill_price = float(
                                    broker_response["average_fill_price"]
                                )
                            target = {
                                OrderStatus.CANCELLED.value: ExecutionOrderStatus.CANCELLED,
                                OrderStatus.FILLED.value: ExecutionOrderStatus.FILLED,
                                OrderStatus.PARTIALLY_FILLED.value: ExecutionOrderStatus.PARTIALLY_FILLED,
                            }.get(normalized)
                            if (
                                target is not None
                                and target in allowed_status_transitions(record.status)
                            ):
                                apply_status_transition(record, target)
                            elif target is None:
                                validate_recovery_transition(
                                    record.recovery_state,
                                    OrderRecoveryState.DISCOVERING,
                                )
                                record.recovery_state = OrderRecoveryState.DISCOVERING
                        elif outcome_status == "AMBIGUOUS":
                            if record.recovery_state == OrderRecoveryState.NONE:
                                validate_recovery_transition(
                                    record.recovery_state,
                                    OrderRecoveryState.DISCOVERING,
                                )
                                record.recovery_state = OrderRecoveryState.DISCOVERING
                        update_execution_order(
                            conn, record, expected_version=record.version
                        )
                if (
                    request.get("command_type") == "submit"
                    and str(order_payload.get("side") or "").upper()
                    == OrderSide.SELL.value
                ):
                    row = conn.execute(
                        select(trade_card_table).where(
                            trade_card_table.c.environment
                            == str(request["environment"]).upper(),
                            trade_card_table.c.account_no == request["account_no"],
                            trade_card_table.c.symbol
                            == str(request["symbol"]).upper(),
                        )
                    ).first()
                    if row is not None:
                        card = trade_card_repo._row_to_card(row)
                        attempt_number = max(
                            1, int(order_payload.get("attempt_number") or 1)
                        )
                        outcome_status = str(
                            (outcome or {}).get("status") or "AMBIGUOUS"
                        ).upper()
                        card.exit_all_required = True
                        card.board_status = BoardStatus.SELL_ALL
                        card.position_runtime_status = (
                            PositionRuntimeStatus.LIQUIDATING
                        )
                        card.exit_attempt_group_id = str(
                            order_payload.get("attempt_group_id") or ""
                        )
                        card.exit_attempt_count = max(
                            int(card.exit_attempt_count or 0), attempt_number
                        )
                        if outcome_status == "FAILED":
                            card.exit_client_order_id = ""
                            card.exit_pending_attempt_number = 0
                            card.exit_submission_unresolved = False
                        else:
                            card.exit_client_order_id = str(
                                order_payload.get("client_order_id") or ""
                            )
                            card.exit_pending_attempt_number = attempt_number
                            card.exit_submission_unresolved = (
                                outcome_status != "ACKNOWLEDGED"
                            )
                        trade_card_repo.update_trade_card_in_transaction(
                            conn, card, expected_version=card.version
                        )
                if marker is None:
                    conn.execute(
                        table.insert().values(
                            journal_id=self.journal_id,
                            requested_sequence=sequence,
                            idempotency_key=key,
                            request_checksum=request["checksum"],
                            payload=json.dumps(
                                {
                                    "request": request,
                                    "outcome": outcome,
                                },
                                separators=(",", ":"),
                            ),
                            reconciled_at=_server_now(engine),
                        )
                    )
            completed.append((sequence, key))
        for sequence, key in completed:
            self.append_reconciled(
                requested_sequence=sequence, idempotency_key=key
            )
        return len(completed)
