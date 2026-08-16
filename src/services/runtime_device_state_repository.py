"""Durable Workstream 6 device readiness used by cross-device handoff."""
from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine

from src.core.runtime_readiness import RuntimeDeviceState
from src.core.schema_version import CURRENT_EXECUTION_SCHEMA_VERSION
from src.services.state_sync import get_main_device

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


@dataclass(frozen=True)
class RuntimeDeviceRecord:
    device_id: str
    hostname: str
    state: RuntimeDeviceState
    handoff_confirmed: bool
    readiness_generation: int
    confirmed_generation: int
    confirmed_by_lease_epoch: int
    confirmed_at: Optional[datetime]
    updated_at: datetime
    schema_version: int


def _table(metadata: MetaData):
    from sqlalchemy import Table

    return Table(
        "runtime_device_state",
        metadata,
        Column("device_id", String(64), primary_key=True),
        Column("hostname", String(255), nullable=False, server_default=""),
        Column("state", String(32), nullable=False),
        Column(
            "schema_version",
            Integer,
            nullable=False,
            server_default=str(CURRENT_EXECUTION_SCHEMA_VERSION),
        ),
        Column("handoff_confirmed", Boolean, nullable=False, server_default="0"),
        Column("readiness_generation", BigInteger, nullable=False, server_default="0"),
        Column("confirmed_generation", BigInteger, nullable=False, server_default="0"),
        Column(
            "confirmed_by_lease_epoch", BigInteger, nullable=False, server_default="0"
        ),
        Column("confirmed_at", DateTime, nullable=True),
        Column("updated_at", DateTime, nullable=False),
    )


def ensure_runtime_device_state_table(engine: Engine):
    metadata = MetaData()
    table = _table(metadata)
    if engine in _ensured_engines:
        return table
    with _ensure_lock:
        if engine not in _ensured_engines:
            metadata.create_all(engine)
            existing = {
                column["name"]
                for column in inspect(engine).get_columns("runtime_device_state")
            }
            additions = {
                "readiness_generation": "BIGINT NOT NULL DEFAULT 0",
                "confirmed_generation": "BIGINT NOT NULL DEFAULT 0",
                "confirmed_by_lease_epoch": "BIGINT NOT NULL DEFAULT 0",
                "confirmed_at": "DATETIME NULL",
                # Existing rows predate version publication and must remain
                # explicitly unknown (0), never be relabelled as compatible.
                "schema_version": "INTEGER NOT NULL DEFAULT 0",
            }
            with engine.begin() as conn:
                for name, definition in additions.items():
                    if name not in existing:
                        conn.execute(
                            text(
                                f"ALTER TABLE runtime_device_state ADD COLUMN {name} "
                                f"{definition}"
                            )
                        )
            _ensured_engines.add(engine)
    return table


def _server_now(engine: Engine):
    if engine.dialect.name == "mysql":
        return func.utc_timestamp(6)
    return func.current_timestamp()


def _record(row) -> RuntimeDeviceRecord:
    observed = row.updated_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    confirmed_at = row.confirmed_at
    if confirmed_at is not None:
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
        confirmed_at = confirmed_at.astimezone(timezone.utc)
    return RuntimeDeviceRecord(
        device_id=row.device_id,
        hostname=row.hostname,
        state=RuntimeDeviceState(row.state),
        handoff_confirmed=bool(row.handoff_confirmed),
        readiness_generation=int(row.readiness_generation or 0),
        confirmed_generation=int(row.confirmed_generation or 0),
        confirmed_by_lease_epoch=int(row.confirmed_by_lease_epoch or 0),
        confirmed_at=confirmed_at,
        updated_at=observed.astimezone(timezone.utc),
        schema_version=int(row.schema_version or 0),
    )


def save_runtime_device_state(
    engine: Engine,
    *,
    device_id: str,
    hostname: str,
    state: RuntimeDeviceState,
    handoff_confirmed: bool = False,
    schema_version: int = CURRENT_EXECUTION_SCHEMA_VERSION,
) -> RuntimeDeviceRecord:
    """Upsert one device's readiness state.

    A transition into STANDBY_READY advances ``readiness_generation`` exactly
    once. Later heartbeats preserve that generation and any confirmation for
    it. Leaving STANDBY_READY clears confirmation, so recovery necessarily
    publishes a new generation after another final reconciliation.
    """

    device_id = str(device_id or "").strip()
    if not device_id:
        raise ValueError("runtime device state requires device_id")
    state = state if isinstance(state, RuntimeDeviceState) else RuntimeDeviceState(state)
    table = ensure_runtime_device_state_table(engine)
    with engine.begin() as conn:
        existing = conn.execute(
            select(table).where(table.c.device_id == device_id)
        ).first()
        prior_generation = int(existing.readiness_generation or 0) if existing else 0
        staying_ready = bool(
            existing is not None
            and state == RuntimeDeviceState.STANDBY_READY
            and existing.state == RuntimeDeviceState.STANDBY_READY.value
        )
        generation = (
            prior_generation
            if staying_ready
            else prior_generation + 1
            if state == RuntimeDeviceState.STANDBY_READY
            else prior_generation
        )
        if staying_ready:
            confirmed = bool(existing.handoff_confirmed)
            confirmed_generation = int(existing.confirmed_generation or 0)
            confirmed_by_lease_epoch = int(
                existing.confirmed_by_lease_epoch or 0
            )
            confirmed_at = existing.confirmed_at
        else:
            confirmed = bool(
                handoff_confirmed and state == RuntimeDeviceState.STANDBY_READY
            )
            confirmed_generation = generation if confirmed else 0
            confirmed_by_lease_epoch = 0
            confirmed_at = _server_now(engine) if confirmed else None
        values = {
            "hostname": str(hostname or ""),
            "state": state.value,
            "schema_version": int(schema_version),
            "handoff_confirmed": confirmed,
            "readiness_generation": generation,
            "confirmed_generation": confirmed_generation,
            "confirmed_by_lease_epoch": confirmed_by_lease_epoch,
            "confirmed_at": confirmed_at,
            "updated_at": _server_now(engine),
        }
        if existing is None:
            conn.execute(table.insert().values(device_id=device_id, **values))
        else:
            conn.execute(
                table.update().where(table.c.device_id == device_id).values(**values)
            )
        row = conn.execute(select(table).where(table.c.device_id == device_id)).first()
    return _record(row)


def confirm_standby_handoff(
    engine: Engine,
    *,
    device_id: str,
    readiness_generation: int,
    outgoing_lease_epoch: int,
    max_age_seconds: float = 60.0,
    now: Optional[datetime] = None,
) -> bool:
    """Confirm one fresh readiness generation without refreshing its heartbeat."""

    table = ensure_runtime_device_state_table(engine)
    with engine.begin() as conn:
        row = conn.execute(
            select(table)
            .where(table.c.device_id == str(device_id or ""))
            .with_for_update()
        ).first()
        if row is None:
            return False
        record = _record(row)
        reference = now or datetime.now(timezone.utc)
        age = (reference - record.updated_at).total_seconds()
        expected_generation = int(readiness_generation or 0)
        expected_epoch = int(outgoing_lease_epoch or 0)
        if not (
            record.state == RuntimeDeviceState.STANDBY_READY
            and expected_generation > 0
            and record.readiness_generation == expected_generation
            and expected_epoch > 0
            and 0.0 <= age <= float(max_age_seconds)
        ):
            return False
        result = conn.execute(
            table.update()
            .where(table.c.device_id == str(device_id or ""))
            .where(table.c.state == RuntimeDeviceState.STANDBY_READY.value)
            .where(table.c.readiness_generation == expected_generation)
            .values(
                handoff_confirmed=True,
                confirmed_generation=expected_generation,
                confirmed_by_lease_epoch=expected_epoch,
                confirmed_at=_server_now(engine),
            )
        )
    return result.rowcount == 1


def verify_standby_generation_for_claim(
    conn: Connection,
    table,
    *,
    device_id: str,
    readiness_generation: int,
    max_age_seconds: float = 60.0,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Lock and validate the successor generation inside a lease claim."""

    expected_generation = int(readiness_generation or 0)
    if expected_generation <= 0:
        return False, "A positive STANDBY_READY generation is required."
    row = conn.execute(
        select(table)
        .where(table.c.device_id == str(device_id or ""))
        .with_for_update()
    ).first()
    if row is None:
        return False, "No durable runtime readiness row exists for this device."
    record = _record(row)
    if record.state != RuntimeDeviceState.STANDBY_READY:
        return False, f"Runtime device is {record.state.value}, not STANDBY_READY."
    if record.readiness_generation != expected_generation:
        return (
            False,
            "STANDBY_READY generation changed before lease acquisition "
            f"({record.readiness_generation} != {expected_generation}).",
        )
    reference = now or datetime.now(timezone.utc)
    age = (reference - record.updated_at).total_seconds()
    if age < 0.0 or age > float(max_age_seconds):
        return False, f"STANDBY_READY heartbeat is stale ({age:.1f}s)."
    return True, ""


def get_runtime_device_state(
    engine: Engine, *, device_id: str
) -> Optional[RuntimeDeviceRecord]:
    table = ensure_runtime_device_state_table(engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.device_id == str(device_id or ""))
        ).first()
    return _record(row) if row is not None else None


def require_compatible_runtime_schema(
    engine: Engine,
    *,
    device_id: str,
    schema_version: int = CURRENT_EXECUTION_SCHEMA_VERSION,
    lease_engine: Optional[Engine] = None,
) -> None:
    """Refuse startup when the authoritative lease holder is incompatible.

    Runtime-state rows are historical observations, not execution authority.
    A crashed device can leave ``ACTIVE`` behind indefinitely; once a fenced
    failover grants the lease to another device, that abandoned row must no
    longer block startup.  Conversely, an old-schema device that still owns
    the live lease remains a hard conflict even if another row looks newer.
    """

    table = ensure_runtime_device_state_table(engine)
    ownership = get_main_device(lease_engine or engine)
    if not ownership.success:
        raise RuntimeError(
            "Runtime schema compatibility could not verify the current "
            f"execution lease: {ownership.error}"
        )
    current_owner = ownership.main_device
    if current_owner is None or current_owner.device_id == str(device_id or ""):
        return
    live_states = {
        RuntimeDeviceState.STARTING.value,
        RuntimeDeviceState.STANDBY.value,
        RuntimeDeviceState.STANDBY_READY.value,
        RuntimeDeviceState.ACTIVE.value,
        RuntimeDeviceState.SHUTTING_DOWN.value,
    }
    with engine.connect() as conn:
        conflicting = conn.execute(
            select(table.c.device_id, table.c.schema_version, table.c.state).where(
                table.c.device_id == current_owner.device_id,
                table.c.state.in_(live_states),
                table.c.schema_version != int(schema_version),
            )
        ).first()
    if conflicting is not None:
        raise RuntimeError(
            "Runtime schema mismatch: device "
            f"{conflicting.device_id} is {conflicting.state} on schema "
            f"{conflicting.schema_version}, while this runtime requires {schema_version}"
        )


def find_standby_successor(
    engine: Engine,
    *,
    excluding_device_id: str,
    max_age_seconds: float = 60.0,
    now: Optional[datetime] = None,
    require_confirmed: bool = False,
    expected_outgoing_lease_epoch: int = 0,
) -> Optional[RuntimeDeviceRecord]:
    """Return a fresh standby successor; stale rows never authorize release."""

    table = ensure_runtime_device_state_table(engine)
    with engine.connect() as conn:
        statement = (
            select(table)
            .where(table.c.device_id != str(excluding_device_id or ""))
            .where(table.c.state == RuntimeDeviceState.STANDBY_READY.value)
            .order_by(table.c.updated_at.desc())
        )
        if require_confirmed:
            statement = statement.where(table.c.handoff_confirmed.is_(True))
        rows = conn.execute(statement).fetchall()
    reference = now or datetime.now(timezone.utc)
    for row in rows:
        record = _record(row)
        age = (reference - record.updated_at).total_seconds()
        expected_epoch = int(expected_outgoing_lease_epoch or 0)
        confirmation_matches = bool(
            not require_confirmed
            or (
                record.handoff_confirmed
                and record.confirmed_generation == record.readiness_generation
                and expected_epoch > 0
                and record.confirmed_by_lease_epoch == expected_epoch
            )
        )
        if 0.0 <= age <= float(max_age_seconds) and confirmation_matches:
            return record
    return None


def find_confirmed_standby_successor(
    engine: Engine,
    *,
    excluding_device_id: str,
    max_age_seconds: float = 60.0,
    now: Optional[datetime] = None,
    expected_outgoing_lease_epoch: int,
) -> Optional[RuntimeDeviceRecord]:
    return find_standby_successor(
        engine,
        excluding_device_id=excluding_device_id,
        max_age_seconds=max_age_seconds,
        now=now,
        require_confirmed=True,
        expected_outgoing_lease_epoch=expected_outgoing_lease_epoch,
    )
