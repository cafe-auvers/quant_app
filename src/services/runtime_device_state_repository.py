"""Durable Workstream 6 device readiness used by cross-device handoff."""
from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, func, select
from sqlalchemy.engine import Engine

from src.core.runtime_readiness import RuntimeDeviceState

_ensured_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()
_ensure_lock = threading.Lock()


@dataclass(frozen=True)
class RuntimeDeviceRecord:
    device_id: str
    hostname: str
    state: RuntimeDeviceState
    handoff_confirmed: bool
    updated_at: datetime


def _table(metadata: MetaData):
    from sqlalchemy import Table

    return Table(
        "runtime_device_state",
        metadata,
        Column("device_id", String(64), primary_key=True),
        Column("hostname", String(255), nullable=False, server_default=""),
        Column("state", String(32), nullable=False),
        Column("handoff_confirmed", Boolean, nullable=False, server_default="0"),
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
    return RuntimeDeviceRecord(
        device_id=row.device_id,
        hostname=row.hostname,
        state=RuntimeDeviceState(row.state),
        handoff_confirmed=bool(row.handoff_confirmed),
        updated_at=observed.astimezone(timezone.utc),
    )


def save_runtime_device_state(
    engine: Engine,
    *,
    device_id: str,
    hostname: str,
    state: RuntimeDeviceState,
    handoff_confirmed: bool = False,
) -> RuntimeDeviceRecord:
    """Upsert one device's readiness state.

    Entering any state other than STANDBY_READY clears an earlier handoff
    confirmation, so a stale confirmation cannot authorize a later shutdown.
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
        confirmed = bool(handoff_confirmed)
        if (
            existing is not None
            and state == RuntimeDeviceState.STANDBY_READY
            and existing.state == RuntimeDeviceState.STANDBY_READY.value
            and existing.handoff_confirmed
        ):
            # A standby heartbeat must not erase the outgoing owner's
            # confirmation while the handoff is in progress.
            confirmed = True
        confirmed = bool(confirmed and state == RuntimeDeviceState.STANDBY_READY)
        values = {
            "hostname": str(hostname or ""),
            "state": state.value,
            "handoff_confirmed": confirmed,
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


def confirm_standby_handoff(engine: Engine, *, device_id: str) -> bool:
    """Confirm a specifically observed STANDBY_READY successor."""

    table = ensure_runtime_device_state_table(engine)
    with engine.begin() as conn:
        result = conn.execute(
            table.update()
            .where(table.c.device_id == str(device_id or ""))
            .where(table.c.state == RuntimeDeviceState.STANDBY_READY.value)
            .values(handoff_confirmed=True, updated_at=_server_now(engine))
        )
    return result.rowcount == 1


def get_runtime_device_state(
    engine: Engine, *, device_id: str
) -> Optional[RuntimeDeviceRecord]:
    table = ensure_runtime_device_state_table(engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(table.c.device_id == str(device_id or ""))
        ).first()
    return _record(row) if row is not None else None


def find_standby_successor(
    engine: Engine,
    *,
    excluding_device_id: str,
    max_age_seconds: float = 60.0,
    now: Optional[datetime] = None,
    require_confirmed: bool = False,
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
        if 0.0 <= age <= float(max_age_seconds):
            return record
    return None


def find_confirmed_standby_successor(
    engine: Engine,
    *,
    excluding_device_id: str,
    max_age_seconds: float = 60.0,
    now: Optional[datetime] = None,
) -> Optional[RuntimeDeviceRecord]:
    return find_standby_successor(
        engine,
        excluding_device_id=excluding_device_id,
        max_age_seconds=max_age_seconds,
        now=now,
        require_confirmed=True,
    )
