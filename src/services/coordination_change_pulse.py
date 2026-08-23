"""Process-local dirty pulses and Tailscale change-token persistence.

The one-second runtime loop may inspect this module freely: all hot-path
operations are in-memory or local-file reads.  TiDB is contacted by callers
only after a local or remote generation changes.  Cross-device delivery uses
the already deployed PC remote-control listener; the listener reads/writes the
small files below and never opens a database connection.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

from src.utils.config import DATA_DIR


OUTBOUND_CHANGE_PULSE_FILE = DATA_DIR / "coordination_change_outbound.json"
INBOUND_CHANGE_PULSE_FILE = DATA_DIR / "coordination_change_inbound.json"

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_DML_TABLE_RE = re.compile(
    r"^\s*(?:INSERT\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+[`\"]?([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_IGNORED_TABLES = {
    # Liveness/audit rows do not change projections, commands, ownership, or
    # broker truth and must not create a cross-device reconciliation pulse.
    "app_runtime_status",
    "application_heartbeat_attempts",
    "external_alert_delivery_attempts",
    "external_alert_incidents",
    "external_alert_spool_imports",
}


@dataclass
class _EnginePulseState:
    generation: int = 0
    pending_generation: int = 0
    acknowledged_generation: int = 0
    staged_generation: int = 0
    staged_event_id: str = ""
    staged_tables: tuple[str, ...] = ()
    staged_table_generations: dict[str, int] = field(default_factory=dict)
    last_remote_event_id: str = ""
    remote_generation: int = 0
    table_generations: dict[str, int] = field(default_factory=dict)
    local_table_generations: dict[str, int] = field(default_factory=dict)
    acknowledged_local_table_generations: dict[str, int] = field(
        default_factory=dict
    )
    notifications_available: bool = False
    remote_peer_confirmed_off: bool = False


@dataclass(frozen=True)
class CoordinationChangeEvent:
    """Non-secret cross-device token plus its affected SQL tables.

    An empty table tuple means the sender used the older untyped protocol.
    Receivers must conservatively invalidate every coordination cache for
    that event.
    """

    event_id: str = ""
    tables: tuple[str, ...] = ()


_lock = threading.RLock()
_states: "weakref.WeakKeyDictionary[Engine, _EnginePulseState]" = (
    weakref.WeakKeyDictionary()
)
_tracked_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()


def _state(engine: Engine) -> _EnginePulseState:
    with _lock:
        return _states.setdefault(engine, _EnginePulseState())


def _changed_table(statement: str) -> str:
    match = _DML_TABLE_RE.match(str(statement or ""))
    return str(match.group(1) if match else "").lower()


def _normalize_tables(tables: Optional[Iterable[str]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(table or "").strip().lower()
                for table in (tables or ())
                if _TABLE_NAME_RE.fullmatch(str(table or "").strip())
            }
        )
    )


def _ignore_write(statement: str, table: str) -> bool:
    if table in _IGNORED_TABLES:
        return True
    if table != "runtime_device_state":
        return False
    normalized = " ".join(str(statement or "").lower().replace("`", "").split())
    if not normalized.startswith("update ") or " set " not in normalized:
        return False
    set_clause = normalized.split(" set ", 1)[1].split(" where ", 1)[0]
    columns = {
        assignment.split("=", 1)[0].strip().split(".")[-1]
        for assignment in set_clause.split(",")
        if "=" in assignment
    }
    return columns == {"updated_at"}


def _mark_local_change(engine: Engine, tables: set[str]) -> None:
    with _lock:
        state = _states.setdefault(engine, _EnginePulseState())
        state.generation += 1
        state.pending_generation = state.generation
        for table in tables:
            state.table_generations[table] = (
                int(state.table_generations.get(table, 0)) + 1
            )
            state.local_table_generations[table] = (
                int(state.local_table_generations.get(table, 0)) + 1
            )


def install_coordination_change_tracking(
    engine: Engine,
    *,
    pulse_engine: Optional[Engine] = None,
    autocommit: bool = False,
) -> None:
    """Install one transaction-coalesced DML observer on a coordination engine."""

    target_engine = pulse_engine or engine
    with _lock:
        if engine in _tracked_engines:
            return
        _tracked_engines.add(engine)
        _states.setdefault(target_engine, _EnginePulseState())

    def after_cursor_execute(
        connection, cursor, statement, parameters, context, executemany
    ) -> None:
        del parameters, context, executemany
        table = _changed_table(statement)
        # A write-path probe such as ``UPDATE ... WHERE 1 = 0`` proves
        # connectivity but changes no shared state. Treating it as a dirty
        # event used to wake every card/command/reconciliation reader on the
        # other device.
        if cursor.rowcount == 0 or not table or _ignore_write(statement, table):
            return
        if autocommit:
            _mark_local_change(target_engine, {table})
            return
        connection.info.setdefault("coordination_change_tables", set()).add(table)

    def on_commit(connection) -> None:
        tables = connection.info.pop("coordination_change_tables", set())
        if tables:
            _mark_local_change(target_engine, tables)

    def on_rollback(connection) -> None:
        connection.info.pop("coordination_change_tables", None)

    event.listen(engine, "after_cursor_execute", after_cursor_execute)
    event.listen(engine, "commit", on_commit)
    event.listen(engine, "rollback", on_rollback)


def coordination_change_generation(engine: Optional[Engine]) -> int:
    if engine is None:
        return 0
    with _lock:
        return int(_states.setdefault(engine, _EnginePulseState()).generation)


def coordination_table_change_generation(
    engine: Optional[Engine], tables: set[str]
) -> tuple[int, tuple[tuple[str, int], ...]]:
    """Return a stable token for only the requested relational tables."""

    if engine is None:
        return (0, ())
    normalized = sorted(str(table or "").lower() for table in tables if table)
    with _lock:
        state = _states.setdefault(engine, _EnginePulseState())
        return (
            int(state.remote_generation),
            tuple(
                (table, int(state.table_generations.get(table, 0)))
                for table in normalized
            ),
        )


def stage_local_change_event(
    engine: Optional[Engine], *, device_id: str
) -> str:
    """Return a stable event id until the current dirty batch is acknowledged."""

    return stage_local_coordination_change(engine, device_id=device_id).event_id


def stage_local_coordination_change(
    engine: Optional[Engine], *, device_id: str
) -> CoordinationChangeEvent:
    """Stage one stable, table-scoped event for cross-device delivery."""

    if engine is None:
        return CoordinationChangeEvent()
    with _lock:
        state = _states.setdefault(engine, _EnginePulseState())
        if state.pending_generation <= state.acknowledged_generation:
            return CoordinationChangeEvent()
        if (
            state.staged_event_id
            and state.staged_generation == state.pending_generation
        ):
            return CoordinationChangeEvent(
                state.staged_event_id, state.staged_tables
            )
        prefix = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(device_id or "device"))[:64]
        state.staged_generation = state.pending_generation
        state.staged_event_id = f"{prefix}:{uuid.uuid4().hex}"
        state.staged_tables = tuple(
            sorted(
                table
                for table, generation in state.local_table_generations.items()
                if generation
                > int(state.acknowledged_local_table_generations.get(table, 0))
            )
        )
        state.staged_table_generations = {
            table: int(state.local_table_generations.get(table, 0))
            for table in state.staged_tables
        }
        return CoordinationChangeEvent(
            state.staged_event_id, state.staged_tables
        )


def acknowledge_local_change_event(
    engine: Optional[Engine], event_id: str
) -> bool:
    if engine is None or not event_id:
        return False
    with _lock:
        state = _states.setdefault(engine, _EnginePulseState())
        if event_id != state.staged_event_id:
            return False
        state.acknowledged_generation = max(
            state.acknowledged_generation, state.staged_generation
        )
        for table, generation in state.staged_table_generations.items():
            state.acknowledged_local_table_generations[table] = max(
                int(state.acknowledged_local_table_generations.get(table, 0)),
                int(generation),
            )
        state.staged_event_id = ""
        state.staged_generation = 0
        state.staged_tables = ()
        state.staged_table_generations = {}
        return True


def mark_remote_coordination_change(
    engine: Optional[Engine],
    event_id: str,
    *,
    tables: Optional[Iterable[str]] = None,
) -> bool:
    """Advance the local generation once for a newly observed remote event."""

    if engine is None or not _valid_event_id(event_id):
        return False
    with _lock:
        state = _states.setdefault(engine, _EnginePulseState())
        if event_id == state.last_remote_event_id:
            return False
        state.last_remote_event_id = event_id
        state.generation += 1
        normalized_tables = _normalize_tables(tables)
        if normalized_tables:
            for table in normalized_tables:
                state.table_generations[table] = (
                    int(state.table_generations.get(table, 0)) + 1
                )
        else:
            # Untyped v2 events retain the safe, broad invalidation path.
            state.remote_generation += 1
        # A received event must not be sent back to its origin.
        return True


def set_change_notifications_available(
    engine: Optional[Engine], available: bool
) -> None:
    """Record that routine remote polling may use the missed-event fallback.

    This is true either when the listener can deliver change tokens or when
    the peer is confirmed offline and therefore cannot originate a change.
    The separate peer-off flag suppresses even the missed-event fallback in
    the latter case. Callers clear both immediately when the peer returns.
    """

    if engine is None:
        return
    with _lock:
        _states.setdefault(engine, _EnginePulseState()).notifications_available = bool(
            available
        )


def change_notifications_available(engine: Optional[Engine]) -> bool:
    if engine is None:
        return False
    with _lock:
        return bool(
            _states.setdefault(engine, _EnginePulseState()).notifications_available
        )


def set_remote_peer_confirmed_off(
    engine: Optional[Engine], confirmed_off: bool
) -> None:
    """Record locally observed peer-off state without contacting TiDB."""

    if engine is None:
        return
    with _lock:
        _states.setdefault(engine, _EnginePulseState()).remote_peer_confirmed_off = (
            bool(confirmed_off)
        )


def remote_peer_confirmed_off(engine: Optional[Engine]) -> bool:
    if engine is None:
        return False
    with _lock:
        return bool(
            _states.setdefault(
                engine, _EnginePulseState()
            ).remote_peer_confirmed_off
        )


def _valid_event_id(event_id: str) -> bool:
    return bool(_EVENT_ID_RE.fullmatch(str(event_id or "")))


def _read_event(path: Path) -> CoordinationChangeEvent:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return CoordinationChangeEvent()
    event_id = str(payload.get("event_id") or "")
    if not _valid_event_id(event_id):
        return CoordinationChangeEvent()
    return CoordinationChangeEvent(
        event_id=event_id,
        tables=_normalize_tables(payload.get("tables") or ()),
    )


def _write_event(
    path: Path,
    event_id: str,
    *,
    tables: Optional[Iterable[str]] = None,
) -> bool:
    if not _valid_event_id(event_id):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_id": event_id,
        "tables": list(_normalize_tables(tables)),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def publish_outbound_change_pulse(
    event_id: str, *, tables: Optional[Iterable[str]] = None
) -> bool:
    """Publish a PC-originated token for the listener's PING response."""

    return _write_event(OUTBOUND_CHANGE_PULSE_FILE, event_id, tables=tables)


def read_outbound_change_event() -> CoordinationChangeEvent:
    return _read_event(OUTBOUND_CHANGE_PULSE_FILE)


def read_outbound_change_pulse() -> str:
    return read_outbound_change_event().event_id


def record_inbound_change_pulse(
    event_id: str, *, tables: Optional[Iterable[str]] = None
) -> bool:
    """Record a laptop-originated token for the PC main process."""

    return _write_event(INBOUND_CHANGE_PULSE_FILE, event_id, tables=tables)


def read_inbound_change_event() -> CoordinationChangeEvent:
    return _read_event(INBOUND_CHANGE_PULSE_FILE)


def read_inbound_change_pulse() -> str:
    return read_inbound_change_event().event_id
