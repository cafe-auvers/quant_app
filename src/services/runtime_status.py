"""Database-backed runtime heartbeats for processes on shared-data devices."""
from __future__ import annotations

import datetime as dt
import os
import platform
import threading
import weakref
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


MAIN_APP_PROCESS = "main.py"
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 60

_ensured_engines: weakref.WeakSet[Engine] = weakref.WeakSet()
_ensure_lock = threading.Lock()


@dataclass(frozen=True)
class RuntimeProcessStatus:
    hostname: str
    process_name: str
    observed: bool
    active: bool
    pid: Optional[int] = None
    heartbeat_at: Optional[dt.datetime] = None
    age_seconds: Optional[float] = None


def _normalized_hostname(hostname: str) -> str:
    return str(hostname or "").strip().lower()


def _get_runtime_status_table(metadata: MetaData) -> Table:
    return Table(
        "app_runtime_status",
        metadata,
        Column("hostname", String(128), primary_key=True),
        Column("process_name", String(64), primary_key=True),
        Column("pid", Integer),
        Column("active", Boolean, nullable=False),
        Column("started_at", DateTime, nullable=False),
        Column("heartbeat_at", DateTime, nullable=False),
    )


def _ensure_runtime_status_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_runtime_status_table(metadata)
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


def database_server_hostname(engine: Engine) -> str:
    """Return the host name of the MySQL server that owns the shared data."""
    if engine.dialect.name == "mysql":
        with engine.connect() as conn:
            hostname = conn.execute(text("SELECT @@hostname")).scalar()
        return _normalized_hostname(str(hostname or ""))
    return _normalized_hostname(platform.node())


def record_runtime_heartbeat(
    engine: Engine,
    *,
    process_name: str = MAIN_APP_PROCESS,
    hostname: Optional[str] = None,
    pid: Optional[int] = None,
) -> None:
    """Mark a local process active using the database server's clock."""
    hostname = _normalized_hostname(hostname or platform.node())
    process_name = str(process_name or "").strip().lower()
    pid = int(pid if pid is not None else os.getpid())
    if not hostname or not process_name:
        raise ValueError("Runtime heartbeat requires a hostname and process name")

    table = _ensure_runtime_status_table(engine)
    with engine.begin() as conn:
        row = conn.execute(
            select(table.c.pid, table.c.active).where(
                table.c.hostname == hostname,
                table.c.process_name == process_name,
            )
        ).first()
        values = {
            "pid": pid,
            "active": True,
            "heartbeat_at": _server_now(engine),
        }
        if row is None:
            conn.execute(
                table.insert().values(
                    hostname=hostname,
                    process_name=process_name,
                    started_at=_server_now(engine),
                    **values,
                )
            )
        else:
            if int(row.pid or 0) != pid or not bool(row.active):
                values["started_at"] = _server_now(engine)
            conn.execute(
                table.update()
                .where(
                    table.c.hostname == hostname,
                    table.c.process_name == process_name,
                )
                .values(**values)
            )


def mark_runtime_process_stopped(
    engine: Engine,
    *,
    process_name: str = MAIN_APP_PROCESS,
    hostname: Optional[str] = None,
    pid: Optional[int] = None,
) -> None:
    """Mark a process stopped without overwriting a newer process instance."""
    hostname = _normalized_hostname(hostname or platform.node())
    process_name = str(process_name or "").strip().lower()
    pid = int(pid if pid is not None else os.getpid())
    if not hostname or not process_name:
        return

    table = _ensure_runtime_status_table(engine)
    with engine.begin() as conn:
        conn.execute(
            table.update()
            .where(
                table.c.hostname == hostname,
                table.c.process_name == process_name,
                table.c.pid == pid,
            )
            .values(active=False, heartbeat_at=_server_now(engine))
        )


def get_runtime_process_status(
    engine: Engine,
    hostname: str,
    *,
    process_name: str = MAIN_APP_PROCESS,
    max_age_seconds: int = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
) -> RuntimeProcessStatus:
    """Read whether a process heartbeat is present and still fresh."""
    hostname = _normalized_hostname(hostname)
    process_name = str(process_name or "").strip().lower()
    if not hostname or not process_name:
        return RuntimeProcessStatus(hostname, process_name, False, False)

    table = _ensure_runtime_status_table(engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(
                table.c.hostname == hostname,
                table.c.process_name == process_name,
            )
        ).first()
        server_now = conn.execute(select(_server_now(engine))).scalar()

    if row is None:
        return RuntimeProcessStatus(hostname, process_name, False, False)

    heartbeat_at = row.heartbeat_at
    if heartbeat_at is None or server_now is None:
        age_seconds = None
        is_fresh = False
    else:
        age_seconds = max(0.0, (server_now - heartbeat_at).total_seconds())
        is_fresh = age_seconds <= max(0, int(max_age_seconds))
    return RuntimeProcessStatus(
        hostname=hostname,
        process_name=process_name,
        observed=True,
        active=bool(row.active) and is_fresh,
        pid=int(row.pid) if row.pid is not None else None,
        heartbeat_at=heartbeat_at,
        age_seconds=age_seconds,
    )


def safe_mark_runtime_process_stopped(engine: Optional[Engine]) -> None:
    """Best-effort shutdown marker for UI teardown paths."""
    if engine is None:
        return
    try:
        mark_runtime_process_stopped(engine)
    except (OSError, SQLAlchemyError, TypeError, ValueError):
        return
