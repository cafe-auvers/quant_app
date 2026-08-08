"""Conflict-safe cross-machine synchronization for user-managed app state.

Only the device holding the shared ``main device`` ownership record may push.
Other devices are pull-only.  State rows use a monotonically increasing server
revision, and every update is conditional on the revision last absorbed by the
writer.  This prevents a stale device or an in-flight save from overwriting a
newer remote copy.
"""
from __future__ import annotations

import json
import logging
import platform
import threading
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.utils.config import DATA_DIR
from src.utils.storage import load_json, save_json

logger = logging.getLogger(__name__)

WATCHLIST_KEY = "watchlist"
BUYLIST_KEY = "buylist"
TRADE_PLANS_KEY = "trade_plans"
MAIN_DEVICE_KEY = "__main_device__"

SYNCED_STATE_KEYS = (WATCHLIST_KEY, BUYLIST_KEY, TRADE_PLANS_KEY)
LOCAL_DEVICE_ROLE_FILE = DATA_DIR / "device_role.json"

PULL_OK = "ok"
PULL_MISSING = "missing"
PULL_ERROR = "error"

PUSH_WRITTEN = "written"
PUSH_CONFLICT = "conflict"
PUSH_NOT_MAIN = "not_main"
PUSH_ERROR = "error"

_ensured_engines: weakref.WeakSet[Engine] = weakref.WeakSet()
_ensure_lock = threading.Lock()


@dataclass(frozen=True)
class LocalDeviceRole:
    device_id: str
    hostname: str
    is_main: bool = False


@dataclass(frozen=True)
class RemoteState:
    payload: Dict[str, Any]
    revision: int
    updated_at: datetime
    updated_by_host: str
    updated_by_device: str


@dataclass(frozen=True)
class PullResult:
    status: str
    state: Optional[RemoteState] = None
    error: str = ""


@dataclass(frozen=True)
class PushResult:
    status: str
    revision: Optional[int] = None
    updated_at: Optional[datetime] = None
    error: str = ""


@dataclass(frozen=True)
class MainDevice:
    device_id: str
    hostname: str
    revision: int
    updated_at: datetime


@dataclass(frozen=True)
class OwnershipResult:
    success: bool
    main_device: Optional[MainDevice] = None
    error: str = ""


def _device_role_path(path: Path | None = None) -> Path:
    return Path(path) if path is not None else LOCAL_DEVICE_ROLE_FILE


def load_local_device_role(path: Path | None = None) -> LocalDeviceRole:
    """Load this machine's unshared identity and desired role.

    A copied role file is detected by its hostname and reset to a new,
    pull-only identity.  This keeps a manually copied data directory from
    accidentally creating two devices with the same writer identity.
    """
    role_path = _device_role_path(path)
    hostname = platform.node().strip() or "unknown-device"
    data = load_json(role_path, {})
    stored_hostname = str(data.get("hostname") or "").strip()
    device_id = str(data.get("device_id") or "").strip()
    copied_from_another_host = bool(stored_hostname and stored_hostname != hostname)

    if not device_id or copied_from_another_host:
        role = LocalDeviceRole(
            device_id=str(uuid.uuid4()),
            hostname=hostname,
            is_main=False,
        )
        save_local_device_role(role, path=role_path)
        return role

    role = LocalDeviceRole(
        device_id=device_id,
        hostname=hostname,
        is_main=bool(data.get("is_main", False)),
    )
    if stored_hostname != hostname:
        save_local_device_role(role, path=role_path)
    return role


def save_local_device_role(
    role: LocalDeviceRole,
    *,
    path: Path | None = None,
) -> None:
    save_json(
        _device_role_path(path),
        {
            "device_id": role.device_id,
            "hostname": role.hostname,
            "is_main": bool(role.is_main),
        },
    )


def set_local_device_main(
    role: LocalDeviceRole,
    is_main: bool,
    *,
    path: Path | None = None,
) -> LocalDeviceRole:
    updated = LocalDeviceRole(
        device_id=role.device_id,
        hostname=role.hostname,
        is_main=bool(is_main),
    )
    save_local_device_role(updated, path=path)
    return updated


def _get_state_sync_table(metadata: MetaData) -> Table:
    return Table(
        "app_state_sync",
        metadata,
        Column("state_key", String(40), primary_key=True),
        Column("payload", Text(length=16_777_215)),
        Column("revision", BigInteger, nullable=False, server_default=text("1")),
        Column("updated_at", DateTime, nullable=False),
        Column("updated_by_host", String(128)),
        Column("updated_by_device", String(64)),
    )


def _ensure_state_sync_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_state_sync_table(metadata)
    if engine in _ensured_engines:
        return table

    with _ensure_lock:
        if engine in _ensured_engines:
            return table
        metadata.create_all(engine)
        columns = {
            column["name"] for column in inspect(engine).get_columns("app_state_sync")
        }
        migrations = []
        if "revision" not in columns:
            migrations.append(
                "ALTER TABLE app_state_sync "
                "ADD COLUMN revision BIGINT NOT NULL DEFAULT 1"
            )
        if "updated_by_device" not in columns:
            migrations.append(
                "ALTER TABLE app_state_sync "
                "ADD COLUMN updated_by_device VARCHAR(64)"
            )
        for statement in migrations:
            with engine.begin() as conn:
                conn.execute(text(statement))
        _ensured_engines.add(engine)
    return table


def _server_now(engine: Engine):
    if engine.dialect.name == "mysql":
        return func.utc_timestamp(6)
    return func.current_timestamp()


def _select_row(
    conn: Connection,
    table: Table,
    state_key: str,
    *,
    for_update: bool = False,
):
    statement = select(table).where(table.c.state_key == state_key)
    if for_update:
        statement = statement.with_for_update()
    return conn.execute(statement).first()


def _decode_payload(raw_payload: Any, state_key: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw_payload) if raw_payload else None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"State sync payload for {state_key} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"State sync payload for {state_key} is not an object")
    return payload


def _remote_state_from_row(row, state_key: str) -> RemoteState:
    return RemoteState(
        payload=_decode_payload(row.payload, state_key),
        revision=int(row.revision or 1),
        updated_at=row.updated_at,
        updated_by_host=row.updated_by_host or "",
        updated_by_device=row.updated_by_device or "",
    )


def pull_state(engine: Optional[Engine], state_key: str) -> PullResult:
    """Fetch a row while keeping missing data distinct from read failures."""
    if engine is None:
        return PullResult(PULL_ERROR, error="State sync database is unavailable.")
    try:
        table = _ensure_state_sync_table(engine)
        with engine.connect() as conn:
            row = _select_row(conn, table, state_key)
        if row is None:
            return PullResult(PULL_MISSING)
        return PullResult(PULL_OK, state=_remote_state_from_row(row, state_key))
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("State sync pull failed for %s: %s", state_key, exc)
        return PullResult(PULL_ERROR, error=str(exc))


def _main_device_from_state(state: RemoteState) -> MainDevice:
    device_id = str(state.payload.get("device_id") or "").strip()
    hostname = str(state.payload.get("hostname") or "").strip()
    if not device_id:
        raise ValueError("Main-device ownership row has no device_id")
    return MainDevice(
        device_id=device_id,
        hostname=hostname,
        revision=state.revision,
        updated_at=state.updated_at,
    )


def get_main_device(engine: Optional[Engine]) -> OwnershipResult:
    pulled = pull_state(engine, MAIN_DEVICE_KEY)
    if pulled.status == PULL_MISSING:
        return OwnershipResult(True)
    if pulled.status != PULL_OK or pulled.state is None:
        return OwnershipResult(False, error=pulled.error or "Could not read main-device ownership.")
    try:
        return OwnershipResult(True, main_device=_main_device_from_state(pulled.state))
    except ValueError as exc:
        return OwnershipResult(False, error=str(exc))


def claim_main_device(
    engine: Optional[Engine],
    role: LocalDeviceRole,
) -> OwnershipResult:
    """Atomically make ``role`` the sole remote writer."""
    if engine is None:
        return OwnershipResult(False, error="State sync database is unavailable.")
    payload_json = json.dumps(
        {"device_id": role.device_id, "hostname": role.hostname},
        separators=(",", ":"),
    )
    try:
        table = _ensure_state_sync_table(engine)
        with engine.begin() as conn:
            row = _select_row(conn, table, MAIN_DEVICE_KEY, for_update=True)
            revision = int(row.revision or 1) + 1 if row is not None else 1
            values = {
                "payload": payload_json,
                "revision": revision,
                "updated_at": _server_now(engine),
                "updated_by_host": role.hostname,
                "updated_by_device": role.device_id,
            }
            if row is None:
                conn.execute(
                    table.insert().values(state_key=MAIN_DEVICE_KEY, **values)
                )
            else:
                conn.execute(
                    table.update()
                    .where(table.c.state_key == MAIN_DEVICE_KEY)
                    .values(**values)
                )
            written_row = _select_row(conn, table, MAIN_DEVICE_KEY)
        if written_row is None:
            return OwnershipResult(False, error="Main-device claim was not persisted.")
        state = _remote_state_from_row(written_row, MAIN_DEVICE_KEY)
        return OwnershipResult(True, main_device=_main_device_from_state(state))
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("Could not claim main-device ownership: %s", exc)
        return OwnershipResult(False, error=str(exc))


def push_state(
    engine: Optional[Engine],
    state_key: str,
    payload: Dict[str, Any],
    *,
    device_id: str,
    expected_revision: int,
) -> PushResult:
    """Conditionally write one state row if this device still owns the lease."""
    if engine is None:
        return PushResult(PUSH_ERROR, error="State sync database is unavailable.")
    if state_key not in SYNCED_STATE_KEYS:
        return PushResult(PUSH_ERROR, error=f"Unsupported synced state key: {state_key}")
    if not device_id:
        return PushResult(PUSH_NOT_MAIN, error="This device has no sync identity.")
    try:
        expected_revision = int(expected_revision)
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        payload_json = json.dumps(payload, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        return PushResult(PUSH_ERROR, error=str(exc))

    try:
        table = _ensure_state_sync_table(engine)
        with engine.begin() as conn:
            owner_row = _select_row(conn, table, MAIN_DEVICE_KEY, for_update=True)
            if owner_row is None:
                return PushResult(PUSH_NOT_MAIN, error="No main device is active.")
            owner_payload = _decode_payload(owner_row.payload, MAIN_DEVICE_KEY)
            owner_device_id = str(owner_payload.get("device_id") or "").strip()
            if owner_device_id != device_id:
                return PushResult(
                    PUSH_NOT_MAIN,
                    error="Another device owns main-device synchronization.",
                )

            current_row = _select_row(conn, table, state_key, for_update=True)
            current_revision = int(current_row.revision or 1) if current_row else 0
            if current_revision != expected_revision:
                return PushResult(
                    PUSH_CONFLICT,
                    revision=current_revision,
                    error=(
                        f"Remote {state_key} revision changed from "
                        f"{expected_revision} to {current_revision}."
                    ),
                )

            new_revision = current_revision + 1
            values = {
                "payload": payload_json,
                "revision": new_revision,
                "updated_at": _server_now(engine),
                "updated_by_host": platform.node() or "",
                "updated_by_device": device_id,
            }
            if current_row is None:
                conn.execute(table.insert().values(state_key=state_key, **values))
            else:
                result = conn.execute(
                    table.update()
                    .where(table.c.state_key == state_key)
                    .where(table.c.revision == current_revision)
                    .values(**values)
                )
                if result.rowcount != 1:
                    return PushResult(
                        PUSH_CONFLICT,
                        revision=current_revision,
                        error=f"Remote {state_key} changed during the save.",
                    )
            written_row = _select_row(conn, table, state_key)

        if written_row is None:
            return PushResult(PUSH_ERROR, error=f"Remote {state_key} write disappeared.")
        written = _remote_state_from_row(written_row, state_key)
        return PushResult(
            PUSH_WRITTEN,
            revision=written.revision,
            updated_at=written.updated_at,
        )
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("State sync push failed for %s: %s", state_key, exc)
        return PushResult(PUSH_ERROR, error=str(exc))
