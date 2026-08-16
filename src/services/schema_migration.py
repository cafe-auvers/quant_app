"""Resumable execution-schema migration and cutover safety fencing."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    select,
)
from sqlalchemy.engine import Engine

from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.execution_mode import ExecutionLease
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderStatus
from src.core.schema_version import CURRENT_EXECUTION_SCHEMA_VERSION
from src.services.capital_reservation_repository import (
    ensure_capital_reservations_table,
    fetch_reservation,
    invalidate_capital_reservations_table_cache,
    save_reservation_strict,
)
from src.services.execution_order_repository import (
    ensure_execution_orders_table,
    fetch_execution_order,
    invalidate_execution_orders_table_cache,
    record_execution_order,
)
from src.services.execution_lease_protocol import (
    DefaultExecutionLeaseProtocol,
    ExecutionLeaseProtocol,
    LeaseNotCurrentError,
)
from src.services.order_ledger import ORDERS_FILE, load_orders
from src.services.trade_card_repository import (
    LOCAL_TRADE_CARDS_FILE,
    ensure_trade_cards_table,
    get_trade_card,
    invalidate_trade_cards_table_cache,
    insert_trade_card,
    load_local_trade_cards_snapshot,
)
from src.services.capital_allocator import RESERVATIONS_FILE, load_reservations
from src.utils.config import DATA_DIR

SCHEMA_MIGRATION_BACKUP_FILE = DATA_DIR / "execution_schema_migration_backup.json"
_BACKUP_VALUE_TYPE_KEY = "__schema_migration_value_type__"


def _encode_backup_value(value):
    if isinstance(value, datetime):
        return {_BACKUP_VALUE_TYPE_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {_BACKUP_VALUE_TYPE_KEY: "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {_BACKUP_VALUE_TYPE_KEY: "time", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {_BACKUP_VALUE_TYPE_KEY: "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {
            _BACKUP_VALUE_TYPE_KEY: "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }
    return value


def _decode_backup_value(value):
    if not isinstance(value, dict) or _BACKUP_VALUE_TYPE_KEY not in value:
        return value
    value_type = value.get(_BACKUP_VALUE_TYPE_KEY)
    encoded = value.get("value")
    if value_type == "datetime":
        return datetime.fromisoformat(str(encoded))
    if value_type == "date":
        return date.fromisoformat(str(encoded))
    if value_type == "time":
        return time.fromisoformat(str(encoded))
    if value_type == "decimal":
        return Decimal(str(encoded))
    if value_type == "bytes":
        return base64.b64decode(str(encoded))
    raise MigrationBackupIntegrityError(
        f"Unknown migration backup value type {value_type!r}"
    )


class MigrationPhase(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    BACKED_UP = "BACKED_UP"
    MIGRATING = "MIGRATING"
    AWAITING_RECONCILIATION = "AWAITING_RECONCILIATION"
    READY = "READY"
    FAILED = "FAILED"
    FORWARD_RECONCILING = "FORWARD_RECONCILING"
    COMPATIBILITY_READY = "COMPATIBILITY_READY"


class MigrationError(RuntimeError):
    pass


class MigrationBackupIntegrityError(MigrationError):
    pass


class MigrationRollbackForbiddenError(MigrationError):
    pass


class MigrationCutoverOwnershipError(MigrationError):
    pass


class MigrationEntriesBlockedError(MigrationError):
    pass


@dataclass(frozen=True)
class MigrationState:
    source_version: int
    target_version: int
    phase: MigrationPhase
    backup_path: str
    backup_checksum: str
    cutover_device_id: str
    cutover_lease_token: str
    cutover_lease_epoch: int
    reconciliation_complete: bool
    post_migration_broker_mutation_occurred: bool
    version: int


def _table(metadata: MetaData) -> Table:
    return Table(
        "execution_schema_migration",
        metadata,
        Column("singleton_id", Integer, primary_key=True),
        Column("source_version", Integer, nullable=False),
        Column("target_version", Integer, nullable=False),
        Column("phase", String(64), nullable=False),
        Column("backup_path", Text, nullable=False),
        Column("backup_checksum", String(64), nullable=False),
        Column("cutover_device_id", String(64), nullable=False),
        Column("cutover_lease_token", String(255), nullable=False),
        Column("cutover_lease_epoch", Integer, nullable=False),
        Column("reconciliation_complete", Boolean, nullable=False),
        Column("post_migration_broker_mutation_occurred", Boolean, nullable=False),
        Column("last_error", Text, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        Column("version", Integer, nullable=False),
    )


def ensure_schema_migration_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _table(metadata)
    metadata.create_all(engine)
    return table


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _state(row) -> MigrationState:
    return MigrationState(
        source_version=int(row.source_version),
        target_version=int(row.target_version),
        phase=MigrationPhase(row.phase),
        backup_path=row.backup_path,
        backup_checksum=row.backup_checksum,
        cutover_device_id=row.cutover_device_id,
        cutover_lease_token=row.cutover_lease_token,
        cutover_lease_epoch=int(row.cutover_lease_epoch),
        reconciliation_complete=bool(row.reconciliation_complete),
        post_migration_broker_mutation_occurred=bool(
            row.post_migration_broker_mutation_occurred
        ),
        version=int(row.version),
    )


def get_schema_migration_state(engine: Engine) -> MigrationState:
    table = ensure_schema_migration_table(engine)
    with engine.begin() as conn:
        row = conn.execute(
            select(table).where(table.c.singleton_id == 1)
        ).first()
        if row is None:
            now = _now()
            conn.execute(
                table.insert().values(
                    singleton_id=1,
                    source_version=0,
                    target_version=CURRENT_EXECUTION_SCHEMA_VERSION,
                    phase=MigrationPhase.NOT_STARTED.value,
                    backup_path="",
                    backup_checksum="",
                    cutover_device_id="",
                    cutover_lease_token="",
                    cutover_lease_epoch=0,
                    reconciliation_complete=False,
                    post_migration_broker_mutation_occurred=False,
                    last_error="",
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
            )
            row = conn.execute(
                select(table).where(table.c.singleton_id == 1)
            ).one()
    return _state(row)


class SchemaMigrationManager:
    """Backup-first, crash-resumable migration and forward-only cutover."""

    _DATABASE_TABLES = (
        "execution_orders",
        "trade_cards",
        "capital_reservations",
    )

    def __init__(
        self,
        engine: Engine,
        *,
        backup_path: Path = SCHEMA_MIGRATION_BACKUP_FILE,
        legacy_paths: Optional[Iterable[Path]] = None,
        target_version: int = CURRENT_EXECUTION_SCHEMA_VERSION,
        migration_callback: Optional[Callable[[], None]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        lease_protocol: Optional[ExecutionLeaseProtocol] = None,
    ) -> None:
        self.engine = engine
        self.backup_path = Path(backup_path)
        self.local_mutation_marker_path = self.backup_path.with_suffix(
            self.backup_path.suffix + ".broker-mutation"
        )
        self.legacy_paths = tuple(
            Path(item)
            for item in (
                legacy_paths
                if legacy_paths is not None
                else (ORDERS_FILE, LOCAL_TRADE_CARDS_FILE, RESERVATIONS_FILE)
            )
        )
        self.target_version = int(target_version)
        self._migration_callback = migration_callback
        self._fault_hook = fault_hook or (lambda _point: None)
        self._lease_protocol = lease_protocol or DefaultExecutionLeaseProtocol(
            engine=engine
        )
        ensure_schema_migration_table(engine)

    @property
    def state(self) -> MigrationState:
        return get_schema_migration_state(self.engine)

    def _update(self, expected: MigrationState, **values) -> MigrationState:
        table = _table(MetaData())
        values.update(updated_at=_now(), version=expected.version + 1)
        with self.engine.begin() as conn:
            result = conn.execute(
                table.update()
                .where(
                    table.c.singleton_id == 1,
                    table.c.version == expected.version,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise MigrationError("Migration state changed concurrently")
        return self.state

    @staticmethod
    def _artifact_checksum(payload: Dict) -> str:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _require_live_lease(
        self, *, device_id: str, lease_token: str, lease_epoch: int
    ) -> ExecutionLease:
        lease = ExecutionLease(
            device_id=str(device_id or "").strip(),
            lease_token=str(lease_token or "").strip(),
            lease_epoch=int(lease_epoch or 0),
        )
        if not lease.device_id or not lease.lease_token or lease.lease_epoch <= 0:
            raise MigrationCutoverOwnershipError(
                "Migration operation requires an exact positive execution lease"
            )
        if not getattr(self._lease_protocol, "epoch_verified", False):
            raise MigrationCutoverOwnershipError(
                "Migration lease authority cannot verify epochs"
            )
        try:
            self._lease_protocol.require_current(lease)
        except LeaseNotCurrentError as exc:
            raise MigrationCutoverOwnershipError(str(exc)) from exc
        return lease

    def _build_backup_payload(self, source_version: int) -> Dict:
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        database = {}
        metadata = MetaData()
        for table_name in self._DATABASE_TABLES:
            if table_name not in existing_tables:
                database[table_name] = {
                    "existed": False,
                    "column_types": {},
                    "rows": [],
                }
                continue
            reflected = Table(table_name, metadata, autoload_with=self.engine)
            with self.engine.connect() as conn:
                rows = conn.execute(select(reflected)).fetchall()
            database[table_name] = {
                "existed": True,
                "column_types": {
                    column.name: str(column.type) for column in reflected.columns
                },
                "rows": [
                    {
                        key: _encode_backup_value(value)
                        for key, value in row._mapping.items()
                    }
                    for row in rows
                ],
            }
        files = {}
        for path in self.legacy_paths:
            if path.exists():
                files[str(path.resolve(strict=False))] = base64.b64encode(
                    path.read_bytes()
                ).decode("ascii")
        return {
            "artifact_version": 2,
            "source_schema_version": int(source_version),
            "target_schema_version": self.target_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": database,
            "files": files,
        }

    def _write_backup(self, payload: Dict) -> str:
        checksum = self._artifact_checksum(payload)
        envelope = {"payload": payload, "checksum": checksum}
        encoded = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.backup_path.with_suffix(self.backup_path.suffix + ".tmp")
        descriptor = os.open(
            temp_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600
        )
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("Short migration-backup write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp_path, self.backup_path)
        return checksum

    def _read_validated_backup(self) -> Dict:
        try:
            envelope = json.loads(self.backup_path.read_text(encoding="utf-8"))
            payload = envelope["payload"]
            checksum = envelope["checksum"]
        except Exception as exc:
            raise MigrationBackupIntegrityError(
                "Migration backup is missing or unreadable"
            ) from exc
        if checksum != self._artifact_checksum(payload):
            raise MigrationBackupIntegrityError(
                "Migration backup checksum validation failed"
            )
        return payload

    def _migrate_legacy_orders(self) -> None:
        ensure_execution_orders_table(self.engine)
        if not ORDERS_FILE.exists():
            return
        for order in load_orders(ORDERS_FILE):
            if fetch_execution_order(self.engine, order.client_order_id) is not None:
                continue
            terminal = order.status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
            }
            has_exact_identity = bool(order.broker_order_id)
            if not has_exact_identity:
                status = ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE
                identity = BrokerIdentityStatus.AMBIGUOUS
                broker_order_id = ""
            else:
                status = {
                    OrderStatus.ACCEPTED: ExecutionOrderStatus.ACKNOWLEDGED,
                    OrderStatus.WORKING: ExecutionOrderStatus.WORKING,
                    OrderStatus.PARTIALLY_FILLED: ExecutionOrderStatus.PARTIALLY_FILLED,
                    OrderStatus.FILLED: ExecutionOrderStatus.FILLED,
                    OrderStatus.CANCEL_REQUESTED: ExecutionOrderStatus.CANCEL_PENDING,
                    OrderStatus.CANCELLED: ExecutionOrderStatus.CANCELLED,
                    OrderStatus.REJECTED: ExecutionOrderStatus.REJECTED,
                    OrderStatus.EXPIRED: ExecutionOrderStatus.EXPIRED,
                }.get(order.status, ExecutionOrderStatus.WORKING)
                identity = BrokerIdentityStatus.EXACT
                broker_order_id = order.broker_order_id
            record_execution_order(
                self.engine,
                ExecutionOrderRecord(
                    environment=order.environment,
                    account_no=order.account_no,
                    symbol=order.symbol,
                    side=order.side,
                    intent=order.intent,
                    client_order_id=order.client_order_id,
                    broker_order_id=broker_order_id,
                    attempt_group_id=order.attempt_group_id,
                    attempt_number=order.attempt_number,
                    attempt_deadline_at=order.attempt_deadline_at,
                    submitted_quantity=order.quantity_requested,
                    submitted_limit_price=order.limit_price,
                    exchange=order.exchange,
                    execution_policy=order.execution_policy,
                    prepared_at=order.submitted_at,
                    owner_device_id="migration",
                    status=status,
                    filled_quantity=order.filled_quantity,
                    remaining_quantity=order.remaining_quantity,
                    average_fill_price=order.avg_fill_price,
                    cancel_requested_at=order.cancel_requested_at,
                    broker_identity_status=identity,
                    recovery_state=(
                        OrderRecoveryState.NONE
                        if terminal
                        else OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN
                    ),
                    capital_reservation_id=order.capital_reservation_id,
                ),
            )

    def _apply_migration(self) -> None:
        ensure_trade_cards_table(self.engine)
        ensure_capital_reservations_table(self.engine)
        if LOCAL_TRADE_CARDS_FILE.exists():
            for card in load_local_trade_cards_snapshot(LOCAL_TRADE_CARDS_FILE):
                if get_trade_card(
                    self.engine,
                    card.environment,
                    card.account_no,
                    card.symbol,
                ) is None:
                    with self.engine.begin() as conn:
                        insert_trade_card(conn, card)
        if RESERVATIONS_FILE.exists():
            for reservation in load_reservations(RESERVATIONS_FILE):
                if fetch_reservation(
                    self.engine, reservation.reservation_id
                ) is None:
                    save_reservation_strict(self.engine, reservation)
        self._migrate_legacy_orders()
        if self._migration_callback is not None:
            self._migration_callback()

    def _invalidate_dropped_table_cache(self, table_name: str) -> None:
        invalidators = {
            "execution_orders": invalidate_execution_orders_table_cache,
            "trade_cards": invalidate_trade_cards_table_cache,
            "capital_reservations": invalidate_capital_reservations_table_cache,
        }
        invalidator = invalidators.get(table_name)
        if invalidator is not None:
            invalidator(self.engine)

    def prepare_cutover(
        self,
        *,
        device_id: str,
        lease_token: str,
        lease_epoch: int,
        source_version: int = 0,
    ) -> MigrationState:
        lease = self._require_live_lease(
            device_id=device_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
        device_id = lease.device_id
        lease_token = lease.lease_token
        lease_epoch = lease.lease_epoch
        state = self.state
        if state.target_version != self.target_version:
            raise MigrationError("Migration target version does not match this runtime")
        if state.cutover_device_id and (
            state.cutover_device_id != device_id
            or state.cutover_lease_token != lease_token
            or state.cutover_lease_epoch != lease_epoch
        ):
            raise MigrationCutoverOwnershipError(
                "A different exact lease owns the in-progress migration cutover"
            )
        if state.phase == MigrationPhase.READY:
            return state
        if state.phase == MigrationPhase.NOT_STARTED:
            payload = self._build_backup_payload(source_version)
            checksum = self._write_backup(payload)
            self._require_live_lease(
                device_id=device_id,
                lease_token=lease_token,
                lease_epoch=lease_epoch,
            )
            state = self._update(
                state,
                source_version=int(source_version),
                backup_path=str(self.backup_path),
                backup_checksum=checksum,
                cutover_device_id=device_id,
                cutover_lease_token=lease_token,
                cutover_lease_epoch=lease_epoch,
                phase=MigrationPhase.BACKED_UP.value,
            )
            self._fault_hook("after_backup")
        self._read_validated_backup()
        if state.phase in {MigrationPhase.BACKED_UP, MigrationPhase.FAILED}:
            state = self._update(
                state,
                phase=MigrationPhase.MIGRATING.value,
                last_error="",
            )
        if state.phase == MigrationPhase.MIGRATING:
            try:
                self._require_live_lease(
                    device_id=device_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                self._apply_migration()
                self._require_live_lease(
                    device_id=device_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                state = self._update(
                    state,
                    phase=MigrationPhase.AWAITING_RECONCILIATION.value,
                    reconciliation_complete=False,
                )
            except Exception as exc:
                self._update(
                    state,
                    phase=MigrationPhase.FAILED.value,
                    last_error=str(exc),
                )
                raise
            self._fault_hook("after_cutover")
        return state

    def mark_reconciliation_complete(self) -> MigrationState:
        state = self.state
        if state.phase == MigrationPhase.READY:
            return state
        if state.phase != MigrationPhase.AWAITING_RECONCILIATION:
            raise MigrationError(
                "Full broker reconciliation cannot complete before migration cutover"
            )
        return self._update(
            state,
            phase=MigrationPhase.READY.value,
            reconciliation_complete=True,
        )

    def require_entries_ready(self) -> None:
        state = self.state
        if not (
            state.phase == MigrationPhase.READY
            and state.reconciliation_complete
            and state.target_version == self.target_version
        ):
            raise MigrationEntriesBlockedError(
                "New entries remain blocked until post-migration reconciliation completes"
            )

    def _write_local_mutation_marker(self) -> None:
        payload = json.dumps(
            {
                "target_version": self.target_version,
                "marked_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.local_mutation_marker_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.local_mutation_marker_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            if os.write(descriptor, payload) != len(payload):
                raise OSError("Short local broker-mutation marker write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def mark_post_migration_broker_mutation(self) -> None:
        try:
            state = self.state
            if state.post_migration_broker_mutation_occurred:
                return
            self._update(
                state,
                post_migration_broker_mutation_occurred=True,
            )
        except Exception:
            if not self.local_mutation_marker_path.exists():
                self._write_local_mutation_marker()

    def reconcile_local_mutation_marker(self) -> None:
        if not self.local_mutation_marker_path.exists():
            return
        state = self.state
        if not state.post_migration_broker_mutation_occurred:
            self._update(
                state,
                post_migration_broker_mutation_occurred=True,
            )

    def rollback_direct(
        self,
        *,
        device_id: str,
        lease_token: str,
        lease_epoch: int,
    ) -> None:
        self._require_live_lease(
            device_id=device_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
        state = self.state
        if (
            state.cutover_device_id != str(device_id or "").strip()
            or state.cutover_lease_token != str(lease_token or "").strip()
            or state.cutover_lease_epoch != int(lease_epoch or 0)
        ):
            raise MigrationCutoverOwnershipError(
                "Direct rollback requires the exact lease that owns cutover"
            )
        if (
            state.post_migration_broker_mutation_occurred
            or self.local_mutation_marker_path.exists()
        ):
            raise MigrationRollbackForbiddenError(
                "Direct backup restore is forbidden after post-migration broker activity; "
                "take a fresh broker snapshot and reconcile forward"
            )
        payload = self._read_validated_backup()
        self._require_live_lease(
            device_id=device_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
        inspector = inspect(self.engine)
        existing = set(inspector.get_table_names())
        database = payload.get("database", {})
        artifact_version = int(payload.get("artifact_version") or 1)
        with self.engine.begin() as conn:
            for table_name in self._DATABASE_TABLES:
                table_backup = database.get(table_name)
                if table_backup is None:
                    if artifact_version > 1:
                        raise MigrationBackupIntegrityError(
                            f"Migration backup omits table manifest for {table_name!r}"
                        )
                    # V1 omitted tables that did not exist before migration.
                    existed_before = False
                    rows = []
                # Artifact v1 stored only rows from pre-existing tables.
                elif isinstance(table_backup, list):
                    existed_before = True
                    rows = table_backup
                else:
                    existed_before = bool(table_backup.get("existed"))
                    rows = table_backup.get("rows", [])
                if not existed_before:
                    if table_name in existing:
                        reflected = Table(
                            table_name, MetaData(), autoload_with=self.engine
                        )
                        reflected.drop(conn)
                        existing.remove(table_name)
                        self._invalidate_dropped_table_cache(table_name)
                    continue
                if table_name not in existing:
                    raise MigrationBackupIntegrityError(
                        f"Cannot restore pre-existing table {table_name!r}: it is missing"
                    )
                reflected = Table(
                    table_name, MetaData(), autoload_with=self.engine
                )
                conn.execute(reflected.delete())
                if rows:
                    decoded_rows = [
                        {
                            key: _decode_backup_value(value)
                            for key, value in row.items()
                        }
                        for row in rows
                    ]
                    # Backward compatibility for v1 artifacts, which emitted
                    # bare ISO strings for DateTime columns.
                    if artifact_version == 1:
                        for row in decoded_rows:
                            for column in reflected.columns:
                                value = row.get(column.name)
                                if isinstance(column.type, DateTime) and isinstance(
                                    value, str
                                ):
                                    row[column.name] = datetime.fromisoformat(value)
                    conn.execute(reflected.insert(), decoded_rows)
        for raw_path, encoded in payload.get("files", {}).items():
            path = Path(raw_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(encoded)
            descriptor = os.open(
                path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600
            )
            try:
                if os.write(descriptor, content) != len(content):
                    raise OSError("Short migration rollback file write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        current = self.state
        self._update(
            current,
            phase=MigrationPhase.NOT_STARTED.value,
            backup_path="",
            backup_checksum="",
            cutover_device_id="",
            cutover_lease_token="",
            cutover_lease_epoch=0,
            reconciliation_complete=False,
        )

    def reconcile_forward_to_compatibility(
        self,
        *,
        device_id: str,
        lease_token: str,
        lease_epoch: int,
        broker_snapshot_provider: Callable[[], object],
        full_reconciliation: Callable[[object], bool],
        compatibility_transform: Callable[[object], None],
    ) -> MigrationState:
        """Forward-only downgrade after broker activity made restore unsafe.

        The exact live lease is checked before every stage. A fresh broker
        snapshot must reconcile completely before the explicit compatibility
        transform runs; stale backup restoration is never part of this path.
        """

        self._require_live_lease(
            device_id=device_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
        state = self.state
        if not (
            state.post_migration_broker_mutation_occurred
            or self.local_mutation_marker_path.exists()
        ):
            raise MigrationError(
                "Forward compatibility recovery is reserved for post-mutation cutover"
            )
        if state.phase != MigrationPhase.FORWARD_RECONCILING:
            state = self._update(
                state,
                phase=MigrationPhase.FORWARD_RECONCILING.value,
                reconciliation_complete=False,
            )
        snapshot = broker_snapshot_provider()
        if snapshot is None:
            raise MigrationError("Fresh broker snapshot was not available")
        self._require_live_lease(
            device_id=device_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
        if not bool(full_reconciliation(snapshot)):
            raise MigrationError("Full broker reconciliation did not complete")
        self._require_live_lease(
            device_id=device_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
        compatibility_transform(snapshot)
        self._require_live_lease(
            device_id=device_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
        return self._update(
            self.state,
            phase=MigrationPhase.COMPATIBILITY_READY.value,
            reconciliation_complete=True,
        )
