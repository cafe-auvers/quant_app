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
from typing import Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

from src.utils.config import DATA_DIR


OUTBOUND_CHANGE_PULSE_FILE = DATA_DIR / "coordination_change_outbound.json"
INBOUND_CHANGE_PULSE_FILE = DATA_DIR / "coordination_change_inbound.json"

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
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
    last_remote_event_id: str = ""
    remote_generation: int = 0
    table_generations: dict[str, int] = field(default_factory=dict)
    notifications_available: bool = False


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


def install_coordination_change_tracking(engine: Engine) -> None:
    """Install one transaction-coalesced DML observer on a coordination engine."""

    with _lock:
        if engine in _tracked_engines:
            return
        _tracked_engines.add(engine)
        _states.setdefault(engine, _EnginePulseState())

    def after_cursor_execute(
        connection, cursor, statement, parameters, context, executemany
    ) -> None:
        del cursor, parameters, context, executemany
        table = _changed_table(statement)
        if not table or _ignore_write(statement, table):
            return
        connection.info.setdefault("coordination_change_tables", set()).add(table)

    def on_commit(connection) -> None:
        tables = connection.info.pop("coordination_change_tables", set())
        if tables:
            _mark_local_change(engine, tables)

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

    if engine is None:
        return ""
    with _lock:
        state = _states.setdefault(engine, _EnginePulseState())
        if state.pending_generation <= state.acknowledged_generation:
            return ""
        if (
            state.staged_event_id
            and state.staged_generation == state.pending_generation
        ):
            return state.staged_event_id
        prefix = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(device_id or "device"))[:64]
        state.staged_generation = state.pending_generation
        state.staged_event_id = f"{prefix}:{uuid.uuid4().hex}"
        return state.staged_event_id


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
        state.staged_event_id = ""
        state.staged_generation = 0
        return True


def mark_remote_coordination_change(
    engine: Optional[Engine], event_id: str
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
        state.remote_generation += 1
        # A received event must not be sent back to its origin.
        return True


def set_change_notifications_available(
    engine: Optional[Engine], available: bool
) -> None:
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


def _valid_event_id(event_id: str) -> bool:
    return bool(_EVENT_ID_RE.fullmatch(str(event_id or "")))


def _read_event(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    event_id = str(payload.get("event_id") or "")
    return event_id if _valid_event_id(event_id) else ""


def _write_event(path: Path, event_id: str) -> bool:
    if not _valid_event_id(event_id):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_id": event_id,
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


def publish_outbound_change_pulse(event_id: str) -> bool:
    """Publish a PC-originated token for the listener's PING response."""

    return _write_event(OUTBOUND_CHANGE_PULSE_FILE, event_id)


def read_outbound_change_pulse() -> str:
    return _read_event(OUTBOUND_CHANGE_PULSE_FILE)


def record_inbound_change_pulse(event_id: str) -> bool:
    """Record a laptop-originated token for the PC main process."""

    return _write_event(INBOUND_CHANGE_PULSE_FILE, event_id)


def read_inbound_change_pulse() -> str:
    return _read_event(INBOUND_CHANGE_PULSE_FILE)
