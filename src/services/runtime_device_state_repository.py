"""Durable Workstream 6 device readiness used by cross-device handoff."""
from __future__ import annotations

import json
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Text,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine

from src.core.runtime_readiness import RuntimeDeviceState
from src.core.schema_version import CURRENT_EXECUTION_SCHEMA_VERSION
from src.infrastructure.database.coordination_engine import (
    coordination_autocommit_connection,
    coordination_read_connection,
)
from src.services.state_sync import get_main_device
from src.services.runtime_status import get_runtime_process_status

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
    details: Dict[str, Any] = field(default_factory=dict)


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
        # MySQL 5.7 and older MariaDB releases reject defaults on TEXT/BLOB
        # columns (error 1101).  Save paths always publish this JSON
        # explicitly, while readers already treat NULL/blank legacy values as
        # an empty object.
        Column("details_json", Text(length=16_777_215), nullable=True),
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
                # Keep the compatibility migration nullable.  Adding a
                # NOT NULL TEXT column to a populated table is not portable,
                # and every subsequent upsert writes a concrete JSON object.
                "details_json": "TEXT NULL",
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
    try:
        details = json.loads(getattr(row, "details_json", "{}") or "{}")
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        details = {}
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
        details=details,
    )


def save_runtime_device_state(
    engine: Engine,
    *,
    device_id: str,
    hostname: str,
    state: RuntimeDeviceState,
    handoff_confirmed: bool = False,
    schema_version: int = CURRENT_EXECUTION_SCHEMA_VERSION,
    details: Optional[Dict[str, Any]] = None,
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
        existing_details: Dict[str, Any] = {}
        if existing is not None:
            try:
                decoded_details = json.loads(existing.details_json or "{}")
                if isinstance(decoded_details, dict):
                    existing_details = decoded_details
            except (TypeError, ValueError):
                existing_details = {}
        values = {
            "hostname": str(hostname or ""),
            "state": state.value,
            "schema_version": int(schema_version),
            "handoff_confirmed": confirmed,
            "readiness_generation": generation,
            "confirmed_generation": confirmed_generation,
            "confirmed_by_lease_epoch": confirmed_by_lease_epoch,
            "confirmed_at": confirmed_at,
            "details_json": json.dumps(
                (
                    dict(details)
                    if details is not None
                    else existing_details
                ),
                default=str,
                separators=(",", ":"),
            ),
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


def refresh_runtime_device_state(
    engine: Engine,
    *,
    device_id: str,
    hostname: str,
    state: RuntimeDeviceState,
    schema_version: int = CURRENT_EXECUTION_SCHEMA_VERSION,
    details: Optional[Dict[str, Any]] = None,
    heartbeat_only: bool = False,
) -> bool:
    """Refresh an unchanged readiness row with one UPDATE statement.

    State transitions still use :func:`save_runtime_device_state` because
    they must calculate/read back the handoff generation.  Normal ACTIVE or
    STANDBY_READY heartbeats already know that generation and must not spend
    three cloud requests selecting the same row before and after every touch.
    A missing or concurrently changed row returns ``False`` so the caller can
    fall back to the full transition path.
    """

    device_id = str(device_id or "").strip()
    if not device_id:
        raise ValueError("runtime device state requires device_id")
    state = state if isinstance(state, RuntimeDeviceState) else RuntimeDeviceState(state)
    table = ensure_runtime_device_state_table(engine)
    values = {"updated_at": _server_now(engine)}
    if not heartbeat_only:
        values.update(
            hostname=str(hostname or ""),
            schema_version=int(schema_version),
            details_json=json.dumps(
                dict(details) if details is not None else {},
                default=str,
                separators=(",", ":"),
            ),
        )
    # This unchanged-state heartbeat is one conditional UPDATE and is atomic
    # on its own.  Autocommit prevents a second billed COMMIT statement while
    # transition paths continue to use save_runtime_device_state's explicit
    # transaction and read-back generation checks.
    predicates = [
        table.c.device_id == device_id,
        table.c.state == state.value,
    ]
    if heartbeat_only:
        # A copied/stale local cache must not keep a row fresh under the wrong
        # identity or schema.  A mismatch returns False and makes the caller
        # take the full transition/read-back path.
        predicates.extend(
            (
                table.c.hostname == str(hostname or ""),
                table.c.schema_version == int(schema_version),
            )
        )
    with coordination_autocommit_connection(engine) as conn:
        result = conn.execute(
            table.update()
            .where(*predicates)
            .values(**values)
        )
    return result.rowcount == 1


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
    with coordination_read_connection(engine) as conn:
        row = conn.execute(
            select(table).where(table.c.device_id == str(device_id or ""))
        ).first()
    return _record(row) if row is not None else None


def list_runtime_device_states(engine: Engine) -> list[RuntimeDeviceRecord]:
    """Return the latest readiness publication for every known device."""

    table = ensure_runtime_device_state_table(engine)
    with coordination_read_connection(engine) as conn:
        rows = conn.execute(select(table).order_by(table.c.hostname.asc())).fetchall()
    return [_record(row) for row in rows]


def require_compatible_runtime_schema(
    engine: Engine,
    *,
    device_id: str,
    schema_version: int = CURRENT_EXECUTION_SCHEMA_VERSION,
    lease_engine: Optional[Engine] = None,
    max_age_seconds: float = 60.0,
    now: Optional[datetime] = None,
) -> None:
    """Refuse startup while any genuinely live runtime is incompatible.

    Runtime-state rows are historical observations, not execution authority.
    A crashed device can leave ``ACTIVE`` behind indefinitely; once a fenced
    failover grants the lease to another device, that abandoned row must no
    longer block startup.  Fresh non-owner standbys still share canonical
    state, though, and therefore must participate in mixed-version exclusion.
    """

    table = ensure_runtime_device_state_table(engine)
    ownership = get_main_device(lease_engine or engine)
    if not ownership.success:
        raise RuntimeError(
            "Runtime schema compatibility could not verify the current "
            f"execution lease: {ownership.error}"
        )
    current_owner = ownership.main_device
    live_states = {
        RuntimeDeviceState.STARTING.value,
        RuntimeDeviceState.STANDBY.value,
        RuntimeDeviceState.STANDBY_READY.value,
        RuntimeDeviceState.ACTIVE.value,
        RuntimeDeviceState.SHUTTING_DOWN.value,
    }
    with coordination_read_connection(engine) as conn:
        conflicting_rows = conn.execute(
            select(table).where(
                table.c.device_id != str(device_id or ""),
                table.c.state.in_(live_states),
                table.c.schema_version != int(schema_version),
            )
        ).fetchall()
        server_now = conn.execute(select(_server_now(engine))).scalar()
    reference = now or server_now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)
    for row in conflicting_rows:
        conflicting = _record(row)
        age = (reference - conflicting.updated_at).total_seconds()
        row_is_fresh = 0.0 <= age <= float(max_age_seconds)
        is_current_owner = bool(
            current_owner is not None
            and conflicting.device_id == current_owner.device_id
        )
        owner_process_is_fresh = False
        if is_current_owner and not row_is_fresh:
            owner_process_is_fresh = get_runtime_process_status(
                lease_engine or engine,
                conflicting.hostname,
                max_age_seconds=max(0, int(max_age_seconds)),
            ).active
        if not row_is_fresh and not owner_process_is_fresh:
            continue
        authority = (
            "current lease holder"
            if is_current_owner
            else "fresh running peer"
        )
        raise RuntimeError(
            "Runtime schema mismatch: device "
            f"{conflicting.device_id} is a {authority} in "
            f"{conflicting.state.value} on schema {conflicting.schema_version}, "
            f"while this runtime requires {schema_version}"
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
    with coordination_read_connection(engine) as conn:
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
