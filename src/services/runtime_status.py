"""Database-backed runtime heartbeats for processes on shared-data devices."""
from __future__ import annotations

import datetime as dt
import os
import platform
import threading
import weakref
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    case,
    func,
    or_,
    select,
    text,
)
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


def ensure_runtime_status_table(engine: Engine) -> Table:
    """Public provisioning hook for the shared coordination store."""

    return _ensure_runtime_status_table(engine)


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
    # One heartbeat UPDATE is already atomic.  Driver autocommit avoids a
    # separate cloud-billed COMMIT statement for this 30-second liveness
    # touch; multi-statement lifecycle transitions keep explicit transactions.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # The normal heartbeat is one UPDATE, not SELECT + UPDATE.  Besides
        # removing a race between those statements, this avoids one TiDB RU
        # read on every heartbeat from both machines.  ``started_at`` changes
        # only when a genuinely new/restarted process takes over the row.
        now = _server_now(engine)
        result = conn.execute(
            table.update()
            .where(
                table.c.hostname == hostname,
                table.c.process_name == process_name,
            )
            .values(
                pid=pid,
                active=True,
                heartbeat_at=now,
                started_at=case(
                    (
                        or_(
                            table.c.pid.is_(None),
                            table.c.pid != pid,
                            table.c.active.is_(False),
                        ),
                        now,
                    ),
                    else_=table.c.started_at,
                ),
            )
        )
        if result.rowcount == 0:
            conn.execute(
                table.insert().values(
                    hostname=hostname,
                    process_name=process_name,
                    pid=pid,
                    active=True,
                    started_at=now,
                    heartbeat_at=now,
                )
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


def heartbeat_row_is_stale(
    conn,
    engine: Engine,
    hostname: str,
    *,
    process_name: str = MAIN_APP_PROCESS,
    max_age_seconds: int = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
) -> bool:
    """Decide staleness using the caller's own connection/transaction.

    Unlike ``get_runtime_process_status`` (which opens its own short-lived
    connection), this reuses ``conn`` so it can participate in an outer
    transaction -- e.g. the same row lock that atomically transfers
    main-device ownership in ``state_sync.claim_main_device_if_stale``.
    Returns True (safe to treat as stale) when there is no row, the process
    is marked inactive, or the heartbeat age computed from the database
    server's own clock exceeds ``max_age_seconds``.
    """
    hostname = _normalized_hostname(hostname)
    process_name = str(process_name or "").strip().lower()
    if not hostname or not process_name:
        return True
    table = _ensure_runtime_status_table(engine)
    row = conn.execute(
        select(table.c.active, table.c.heartbeat_at).where(
            table.c.hostname == hostname,
            table.c.process_name == process_name,
        )
    ).first()
    if row is None or not bool(row.active):
        return True
    server_now = conn.execute(select(_server_now(engine))).scalar()
    if row.heartbeat_at is None or server_now is None:
        return True
    age_seconds = max(0.0, (server_now - row.heartbeat_at).total_seconds())
    return age_seconds > max(0, int(max_age_seconds))


def safe_mark_runtime_process_stopped(engine: Optional[Engine]) -> None:
    """Best-effort shutdown marker for UI teardown paths."""
    if engine is None:
        return
    try:
        mark_runtime_process_stopped(engine)
    except (OSError, SQLAlchemyError, TypeError, ValueError):
        return
