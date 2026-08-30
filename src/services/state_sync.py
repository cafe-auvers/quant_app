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
from datetime import date, datetime, timezone
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
    case,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.core.execution_config import (
    COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS,
)
from src.infrastructure.database.coordination_engine import coordination_read_connection
from src.services.runtime_status import MAIN_APP_PROCESS, heartbeat_row_is_stale
from src.utils.config import DATA_DIR, get_env_value
from src.utils.market_calendar import current_or_next_nyse_session_date
from src.utils.storage import load_json, save_json

logger = logging.getLogger(__name__)

WATCHLIST_KEY = "watchlist"
BUYLIST_KEY = "buylist"
TRADE_PLANS_KEY = "trade_plans"
# Cross-machine handoff needs the execution queue too -- a valid automated
# entry today must be queue-backed (legacy ACTIVE entry automation is
# retired), so without syncing this a device that takes over main-device
# status can receive a buylist item showing EXECUTE_READY with no matching
# queue item at all. Dynamic per-item candidate/status data synced this way
# is never trusted directly -- the handoff-reconciliation path re-validates
# it against fresh intraday data before resuming auto-submission.
EXECUTION_QUEUE_KEY = "execution_queue"
# Strategy inputs must travel with the plan.  Without these rows the laptop
# and PC can evaluate the same frozen watch/buy/queue documents under different
# ORB bounds or scanner definitions.
SCANNER_SETUPS_KEY = "scanner_setups"
SETTINGS_KEY = "settings"
MAIN_DEVICE_KEY = "__main_device__"
OPERATOR_CONTROL_KEY = "__operator_control__"
LIVE_TRADING_CONTROL_KEY = "__live_trading_control__"

SYNCED_STATE_KEYS = (
    WATCHLIST_KEY,
    BUYLIST_KEY,
    TRADE_PLANS_KEY,
    EXECUTION_QUEUE_KEY,
    SCANNER_SETUPS_KEY,
    SETTINGS_KEY,
)
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
    lease_token: str = ""
    lease_epoch: int = 0


@dataclass(frozen=True)
class OwnershipResult:
    success: bool
    main_device: Optional[MainDevice] = None
    error: str = ""


@dataclass(frozen=True)
class LiveTradingControl:
    """Shared operator kill-switch state, independent of Main ownership."""

    enabled: bool
    revision: int
    updated_at: datetime
    updated_by_host: str = ""
    updated_by_device: str = ""
    session_date: Optional[date] = None
    runtime_commit_sha: str = ""


@dataclass(frozen=True)
class LiveTradingControlResult:
    success: bool
    control: Optional[LiveTradingControl] = None
    error: str = ""


def _live_trading_control_from_state(state: RemoteState) -> LiveTradingControl:
    enabled = state.payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError(
            "Shared live-trading control has an invalid enabled value."
        )
    raw_session_date = str(state.payload.get("session_date") or "").strip()
    session_date = None
    if raw_session_date:
        try:
            session_date = date.fromisoformat(raw_session_date)
        except ValueError as exc:
            raise ValueError(
                "Shared live-trading control has an invalid session date."
            ) from exc
    return LiveTradingControl(
        enabled=enabled,
        revision=state.revision,
        updated_at=state.updated_at,
        updated_by_host=state.updated_by_host,
        updated_by_device=state.updated_by_device,
        session_date=session_date,
        runtime_commit_sha=str(
            state.payload.get("runtime_commit_sha") or ""
        ).strip().lower(),
    )


def live_trading_control_block_reason(
    control: LiveTradingControl,
    *,
    now: Optional[datetime] = None,
    runtime_commit_sha: Optional[str] = None,
) -> str:
    """Explain why a durable live switch is ineffective for this session."""

    if not control.enabled:
        return "shared live-trading control is disabled"
    expected_session = current_or_next_nyse_session_date(now)
    if control.session_date is None:
        return "shared live-trading control predates session-scoped arming"
    if control.session_date != expected_session:
        return (
            f"shared live-trading control is armed for {control.session_date}, "
            f"not {expected_session}"
        )
    expected_commit = str(
        runtime_commit_sha
        if runtime_commit_sha is not None
        else get_env_value("KIS_RUNTIME_COMMIT_SHA", "") or ""
    ).strip().lower()
    if expected_commit and control.runtime_commit_sha != expected_commit:
        return "shared live-trading control was armed for a different release"
    return ""


def live_trading_control_is_effective(
    control: LiveTradingControl,
    *,
    now: Optional[datetime] = None,
    runtime_commit_sha: Optional[str] = None,
) -> bool:
    return not live_trading_control_block_reason(
        control,
        now=now,
        runtime_commit_sha=runtime_commit_sha,
    )


@dataclass(frozen=True)
class OperatorControl:
    """The device allowed to create future manual operator commands.

    A locked control has no owner.  This state is deliberately independent
    from :class:`MainDevice`, which remains the execution owner.
    """

    device_id: str
    hostname: str
    locked: bool
    revision: int
    updated_at: datetime
    updated_by_host: str = ""
    updated_by_device: str = ""
    previous_device_id: str = ""
    previous_hostname: str = ""


@dataclass(frozen=True)
class OperatorControlResult:
    success: bool
    control: Optional[OperatorControl] = None
    error: str = ""


@dataclass(frozen=True)
class CoordinationStatusSnapshot:
    """Small shared-control snapshot fetched with one database statement."""

    live_trading: LiveTradingControlResult
    operator_control: OperatorControlResult
    state_revisions: Dict[str, int]


@dataclass(frozen=True)
class PlanPublishResult:
    success: bool
    revisions: Dict[str, int] = None
    verified_at: Optional[datetime] = None
    execution_owner_hostname: str = ""
    execution_owner_heartbeat_fresh: bool = False
    error: str = ""

    def __post_init__(self) -> None:
        if self.revisions is None:
            object.__setattr__(self, "revisions", {})


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


def _get_operator_control_audit_table(metadata: MetaData) -> Table:
    return Table(
        "operator_control_audit",
        metadata,
        Column("revision", BigInteger, primary_key=True),
        Column("previous_device_id", String(64), nullable=False, server_default=""),
        Column("previous_hostname", String(128), nullable=False, server_default=""),
        Column("new_device_id", String(64), nullable=False, server_default=""),
        Column("new_hostname", String(128), nullable=False, server_default=""),
        Column("locked", BigInteger, nullable=False, server_default=text("1")),
        Column("updated_by_host", String(128), nullable=False, server_default=""),
        Column("updated_by_device", String(64), nullable=False, server_default=""),
        Column("updated_at", DateTime, nullable=False),
    )


def _get_live_trading_control_audit_table(metadata: MetaData) -> Table:
    return Table(
        "live_trading_control_audit",
        metadata,
        Column("revision", BigInteger, primary_key=True),
        Column("previous_enabled", BigInteger, nullable=False, server_default=text("0")),
        Column("enabled", BigInteger, nullable=False, server_default=text("0")),
        Column("updated_by_host", String(128), nullable=False, server_default=""),
        Column("updated_by_device", String(64), nullable=False, server_default=""),
        Column("updated_at", DateTime, nullable=False),
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


def _ensure_operator_control_audit_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_operator_control_audit_table(metadata)
    metadata.create_all(engine)
    return table


def _ensure_live_trading_control_audit_table(engine: Engine) -> Table:
    metadata = MetaData()
    table = _get_live_trading_control_audit_table(metadata)
    metadata.create_all(engine)
    return table


def ensure_state_sync_tables(engine: Engine) -> None:
    """Public one-time provisioning hook for a coordination-only database."""

    _ensure_state_sync_table(engine)
    _ensure_operator_control_audit_table(engine)
    _ensure_live_trading_control_audit_table(engine)


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
        with coordination_read_connection(engine) as conn:
            row = _select_row(conn, table, state_key)
        if row is None:
            return PullResult(PULL_MISSING)
        return PullResult(PULL_OK, state=_remote_state_from_row(row, state_key))
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("State sync pull failed for %s: %s", state_key, exc)
        return PullResult(PULL_ERROR, error=str(exc))


def get_live_trading_control(
    engine: Optional[Engine],
) -> LiveTradingControlResult:
    """Read the global live-trading control.

    A missing row is a valid, disabled state. This lets existing databases
    upgrade without accidentally authorizing execution; the first explicit
    operator ON action creates the row.
    """

    pulled = pull_state(engine, LIVE_TRADING_CONTROL_KEY)
    if pulled.status == PULL_MISSING:
        return LiveTradingControlResult(
            True,
            LiveTradingControl(
                enabled=False,
                revision=0,
                updated_at=datetime.min,
            ),
        )
    if pulled.status != PULL_OK or pulled.state is None:
        return LiveTradingControlResult(
            False,
            error=pulled.error or "Could not read shared live-trading control.",
        )
    try:
        control = _live_trading_control_from_state(pulled.state)
    except ValueError as exc:
        return LiveTradingControlResult(
            False,
            error=str(exc),
        )
    return LiveTradingControlResult(True, control)


def set_live_trading_control(
    engine: Optional[Engine],
    role: LocalDeviceRole,
    enabled: bool,
    *,
    session_date: Optional[date] = None,
    runtime_commit_sha: Optional[str] = None,
) -> LiveTradingControlResult:
    """Atomically set the global kill switch from either registered device.

    Main ownership intentionally is not required here. Main controls who may
    execute; this row controls whether execution is armed for the deployment.
    The row lock serializes simultaneous operator actions, so the last
    committed action wins and every change advances the durable revision.
    """

    if engine is None:
        return LiveTradingControlResult(
            False,
            error="Shared live-trading database is unavailable.",
        )
    try:
        table = _ensure_state_sync_table(engine)
        audit_table = _ensure_live_trading_control_audit_table(engine)
        with engine.begin() as conn:
            row = _select_row(
                conn,
                table,
                LIVE_TRADING_CONTROL_KEY,
                for_update=True,
            )
            previous_enabled = False
            if row is not None:
                previous_enabled = bool(
                    _decode_payload(
                        row.payload, LIVE_TRADING_CONTROL_KEY
                    ).get("enabled", False)
                )
            revision = int(row.revision or 0) + 1 if row is not None else 1
            armed_session = (
                session_date or current_or_next_nyse_session_date()
            ) if enabled else None
            armed_commit = str(
                runtime_commit_sha
                if runtime_commit_sha is not None
                else get_env_value("KIS_RUNTIME_COMMIT_SHA", "") or ""
            ).strip().lower() if enabled else ""
            payload = {
                "enabled": bool(enabled),
                "session_date": (
                    armed_session.isoformat() if armed_session else ""
                ),
                "runtime_commit_sha": armed_commit,
            }
            values = {
                "payload": json.dumps(
                    payload,
                    separators=(",", ":"),
                ),
                "revision": revision,
                "updated_at": _server_now(engine),
                "updated_by_host": role.hostname,
                "updated_by_device": role.device_id,
            }
            if row is None:
                conn.execute(
                    table.insert().values(
                        state_key=LIVE_TRADING_CONTROL_KEY,
                        **values,
                    )
                )
            else:
                conn.execute(
                    table.update()
                    .where(table.c.state_key == LIVE_TRADING_CONTROL_KEY)
                    .where(table.c.revision == int(row.revision or 0))
                    .values(**values)
                )
            conn.execute(
                audit_table.insert().values(
                    revision=revision,
                    previous_enabled=1 if previous_enabled else 0,
                    enabled=1 if enabled else 0,
                    updated_by_host=role.hostname,
                    updated_by_device=role.device_id,
                    updated_at=_server_now(engine),
                )
            )
            written_row = _select_row(conn, table, LIVE_TRADING_CONTROL_KEY)
        if written_row is None:
            return LiveTradingControlResult(
                False,
                error="Shared live-trading control was not persisted.",
            )
        state = _remote_state_from_row(written_row, LIVE_TRADING_CONTROL_KEY)
        return LiveTradingControlResult(
            True,
            _live_trading_control_from_state(state),
        )
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("Could not update shared live-trading control: %s", exc)
        return LiveTradingControlResult(False, error=str(exc))


def _operator_control_from_state(state: RemoteState) -> OperatorControl:
    locked = bool(state.payload.get("locked", False))
    device_id = str(state.payload.get("device_id") or "").strip()
    hostname = str(state.payload.get("hostname") or "").strip()
    if not locked and not device_id:
        raise ValueError("Operator-control ownership row has no device_id")
    return OperatorControl(
        device_id="" if locked else device_id,
        hostname="" if locked else hostname,
        locked=locked,
        revision=state.revision,
        updated_at=state.updated_at,
        updated_by_host=state.updated_by_host,
        updated_by_device=state.updated_by_device,
        previous_device_id=str(
            state.payload.get("previous_device_id") or ""
        ).strip(),
        previous_hostname=str(
            state.payload.get("previous_hostname") or ""
        ).strip(),
    )


def get_operator_control(engine: Optional[Engine]) -> OperatorControlResult:
    """Read the independent manual-command owner.

    A missing row upgrades fail-closed to ``Locked``; an operator must make
    the first explicit assignment before either machine can submit commands.
    """

    pulled = pull_state(engine, OPERATOR_CONTROL_KEY)
    if pulled.status == PULL_MISSING:
        return OperatorControlResult(
            True,
            OperatorControl(
                device_id="",
                hostname="",
                locked=True,
                revision=0,
                updated_at=datetime.min,
            ),
        )
    if pulled.status != PULL_OK or pulled.state is None:
        return OperatorControlResult(
            False,
            error=pulled.error or "Could not read operator-control ownership.",
        )
    try:
        return OperatorControlResult(
            True,
            control=_operator_control_from_state(pulled.state),
        )
    except (TypeError, ValueError) as exc:
        return OperatorControlResult(False, error=str(exc))


def set_operator_control(
    engine: Optional[Engine],
    updated_by: LocalDeviceRole,
    owner: Optional[LocalDeviceRole],
) -> OperatorControlResult:
    """Atomically assign future manual commands to ``owner`` or lock them.

    The switch never changes execution ownership and never touches an
    already accepted command.  Every real change advances a shared revision
    and appends the before/after assignment to ``operator_control_audit``.
    """

    if engine is None:
        return OperatorControlResult(
            False, error="Shared operator-control database is unavailable."
        )
    try:
        table = _ensure_state_sync_table(engine)
        audit_table = _ensure_operator_control_audit_table(engine)
        with engine.begin() as conn:
            row = _select_row(conn, table, OPERATOR_CONTROL_KEY, for_update=True)
            previous_device_id = ""
            previous_hostname = ""
            previous_locked = True
            if row is not None:
                previous = _operator_control_from_state(
                    _remote_state_from_row(row, OPERATOR_CONTROL_KEY)
                )
                previous_device_id = previous.device_id
                previous_hostname = previous.hostname
                previous_locked = previous.locked

            locked = owner is None
            new_device_id = "" if locked else str(owner.device_id or "").strip()
            new_hostname = "" if locked else str(owner.hostname or "").strip()
            if not locked and not new_device_id:
                return OperatorControlResult(
                    False, error="Operator-control target has no device identity."
                )
            if (
                previous_locked == locked
                and previous_device_id == new_device_id
                and previous_hostname == new_hostname
                and row is not None
            ):
                return OperatorControlResult(
                    True,
                    control=_operator_control_from_state(
                        _remote_state_from_row(row, OPERATOR_CONTROL_KEY)
                    ),
                )

            revision = int(row.revision or 0) + 1 if row is not None else 1
            payload = json.dumps(
                {
                    "device_id": new_device_id,
                    "hostname": new_hostname,
                    "locked": locked,
                    "previous_device_id": previous_device_id,
                    "previous_hostname": previous_hostname,
                },
                separators=(",", ":"),
            )
            values = {
                "payload": payload,
                "revision": revision,
                "updated_at": _server_now(engine),
                "updated_by_host": updated_by.hostname,
                "updated_by_device": updated_by.device_id,
            }
            if row is None:
                conn.execute(
                    table.insert().values(
                        state_key=OPERATOR_CONTROL_KEY,
                        **values,
                    )
                )
            else:
                result = conn.execute(
                    table.update()
                    .where(table.c.state_key == OPERATOR_CONTROL_KEY)
                    .where(table.c.revision == int(row.revision or 0))
                    .values(**values)
                )
                if result.rowcount != 1:
                    return OperatorControlResult(
                        False,
                        error="Operator-control ownership changed before commit.",
                    )
            conn.execute(
                audit_table.insert().values(
                    revision=revision,
                    previous_device_id=previous_device_id,
                    previous_hostname=previous_hostname,
                    new_device_id=new_device_id,
                    new_hostname=new_hostname,
                    locked=1 if locked else 0,
                    updated_by_host=updated_by.hostname,
                    updated_by_device=updated_by.device_id,
                    updated_at=_server_now(engine),
                )
            )
            written = _select_row(conn, table, OPERATOR_CONTROL_KEY)
        if written is None:
            return OperatorControlResult(
                False, error="Operator-control ownership was not persisted."
            )
        return OperatorControlResult(
            True,
            control=_operator_control_from_state(
                _remote_state_from_row(written, OPERATOR_CONTROL_KEY)
            ),
        )
    except (SQLAlchemyError, TypeError, ValueError) as exc:
        logger.info("Could not update operator-control ownership: %s", exc)
        return OperatorControlResult(False, error=str(exc))


def get_synced_state_revisions(
    engine: Optional[Engine],
) -> Dict[str, int]:
    """Return canonical plan revisions without pulling their large payloads."""

    if engine is None:
        return {}
    try:
        table = _ensure_state_sync_table(engine)
        with coordination_read_connection(engine) as conn:
            rows = conn.execute(
                select(table.c.state_key, table.c.revision).where(
                    table.c.state_key.in_(SYNCED_STATE_KEYS)
                )
            ).fetchall()
        revisions = {state_key: 0 for state_key in SYNCED_STATE_KEYS}
        revisions.update(
            {str(row.state_key): int(row.revision or 0) for row in rows}
        )
        return revisions
    except SQLAlchemyError as exc:
        logger.debug("Could not read synchronized state revisions: %s", exc)
        return {}
    except (TypeError, ValueError) as exc:
        logger.info("Could not read synchronized state revisions: %s", exc)
        return {}


def get_coordination_status_snapshot(
    engine: Optional[Engine],
) -> CoordinationStatusSnapshot:
    """Read controls and planning revisions in one compact query.

    Planning payloads can be large, so the SQL projection returns their
    revision only.  Payload text is selected solely for the two small control
    rows.  Missing rows preserve the same fail-closed defaults as the
    individual control readers.
    """

    failed_live = LiveTradingControlResult(
        False,
        error="Could not read shared live-trading control.",
    )
    failed_operator = OperatorControlResult(
        False,
        error="Could not read shared operator-control ownership.",
    )
    if engine is None:
        return CoordinationStatusSnapshot(failed_live, failed_operator, {})

    try:
        table = _ensure_state_sync_table(engine)
        requested_keys = (
            LIVE_TRADING_CONTROL_KEY,
            OPERATOR_CONTROL_KEY,
            *SYNCED_STATE_KEYS,
        )
        control_keys = (LIVE_TRADING_CONTROL_KEY, OPERATOR_CONTROL_KEY)
        with coordination_read_connection(engine) as conn:
            rows = conn.execute(
                select(
                    table.c.state_key,
                    case(
                        (table.c.state_key.in_(control_keys), table.c.payload),
                        else_=None,
                    ).label("control_payload"),
                    table.c.revision,
                    table.c.updated_at,
                    table.c.updated_by_host,
                    table.c.updated_by_device,
                ).where(table.c.state_key.in_(requested_keys))
            ).fetchall()
    except SQLAlchemyError as exc:
        logger.debug("Could not read coordination status snapshot: %s", exc)
        error = str(exc)
        return CoordinationStatusSnapshot(
            LiveTradingControlResult(False, error=error),
            OperatorControlResult(False, error=error),
            {},
        )

    rows_by_key = {str(row.state_key): row for row in rows}
    revisions = {state_key: 0 for state_key in SYNCED_STATE_KEYS}
    for state_key in SYNCED_STATE_KEYS:
        row = rows_by_key.get(state_key)
        if row is not None:
            try:
                revisions[state_key] = int(row.revision or 0)
            except (TypeError, ValueError):
                logger.info("Invalid synchronized revision for %s", state_key)
                revisions[state_key] = 0

    live_row = rows_by_key.get(LIVE_TRADING_CONTROL_KEY)
    if live_row is None:
        live_result = LiveTradingControlResult(
            True,
            LiveTradingControl(False, 0, datetime.min),
        )
    else:
        try:
            live_state = RemoteState(
                payload=_decode_payload(
                    live_row.control_payload,
                    LIVE_TRADING_CONTROL_KEY,
                ),
                revision=int(live_row.revision or 1),
                updated_at=live_row.updated_at,
                updated_by_host=live_row.updated_by_host or "",
                updated_by_device=live_row.updated_by_device or "",
            )
            live_result = LiveTradingControlResult(
                True,
                _live_trading_control_from_state(live_state),
            )
        except (TypeError, ValueError) as exc:
            live_result = LiveTradingControlResult(False, error=str(exc))

    operator_row = rows_by_key.get(OPERATOR_CONTROL_KEY)
    if operator_row is None:
        operator_result = OperatorControlResult(
            True,
            OperatorControl("", "", True, 0, datetime.min),
        )
    else:
        try:
            operator_state = RemoteState(
                payload=_decode_payload(
                    operator_row.control_payload,
                    OPERATOR_CONTROL_KEY,
                ),
                revision=int(operator_row.revision or 1),
                updated_at=operator_row.updated_at,
                updated_by_host=operator_row.updated_by_host or "",
                updated_by_device=operator_row.updated_by_device or "",
            )
            operator_result = OperatorControlResult(
                True,
                control=_operator_control_from_state(operator_state),
            )
        except (TypeError, ValueError) as exc:
            operator_result = OperatorControlResult(False, error=str(exc))

    return CoordinationStatusSnapshot(live_result, operator_result, revisions)


def publish_planning_snapshot(
    engine: Optional[Engine],
    role: LocalDeviceRole,
    payloads: Dict[str, Dict[str, Any]],
    *,
    expected_revisions: Dict[str, int],
    market_is_open: bool,
) -> PlanPublishResult:
    """Atomically publish the complete pre-market plan and strategy inputs.

    This is the narrow exception that lets the Operator Control owner prepare
    tomorrow's plan while the PC remains Execution Owner.  It is disabled
    during the regular session and while any operator command is in flight.
    """

    if engine is None:
        return PlanPublishResult(False, error="Shared planning database is unavailable.")
    if market_is_open:
        return PlanPublishResult(
            False,
            error=(
                "Market is open. Full plan publish is disabled; use Live "
                "Intervention commands instead."
            ),
        )
    if set(payloads) != set(SYNCED_STATE_KEYS):
        return PlanPublishResult(
            False,
            error=(
                "A full plan publish requires watchlist, buylist, trade_plans, "
                "execution_queue, scanner_setups, and settings."
            ),
        )
    try:
        encoded = {
            key: json.dumps(value, default=str, separators=(",", ":"))
            for key, value in payloads.items()
        }
        expected = {key: int(expected_revisions.get(key, 0)) for key in payloads}
        if any(value < 0 for value in expected.values()):
            raise ValueError("Expected revisions must be non-negative")
    except (TypeError, ValueError) as exc:
        return PlanPublishResult(False, error=str(exc))

    try:
        from src.services.operator_commands import (
            OperatorCommandStatus,
            ensure_operator_commands_table,
        )

        table = _ensure_state_sync_table(engine)
        command_table = ensure_operator_commands_table(engine)
        written_revisions: Dict[str, int] = {}
        with engine.begin() as conn:
            operator_row = _select_row(
                conn, table, OPERATOR_CONTROL_KEY, for_update=True
            )
            if operator_row is None:
                return PlanPublishResult(
                    False,
                    error="Operator Control is Locked; assign it before publishing.",
                )
            operator_payload = _decode_payload(
                operator_row.payload, OPERATOR_CONTROL_KEY
            )
            if bool(operator_payload.get("locked", False)):
                return PlanPublishResult(
                    False,
                    error="Operator Control is Locked; assign it before publishing.",
                )
            if str(operator_payload.get("device_id") or "") != role.device_id:
                return PlanPublishResult(
                    False,
                    error="Only the current Operator Control owner may publish the plan.",
                )
            active_statuses = (
                OperatorCommandStatus.PENDING.value,
                OperatorCommandStatus.ACCEPTED.value,
                OperatorCommandStatus.EXECUTING.value,
                OperatorCommandStatus.BROKER_SUBMITTED.value,
                OperatorCommandStatus.PARTIALLY_FILLED.value,
            )
            active_command = conn.execute(
                select(command_table.c.command_id)
                .where(command_table.c.status.in_(active_statuses))
                .limit(1)
            ).first()
            if active_command is not None:
                return PlanPublishResult(
                    False,
                    error="Full plan publish is blocked while an operator command is in flight.",
                )

            rows = {
                key: _select_row(conn, table, key, for_update=True)
                for key in SYNCED_STATE_KEYS
            }
            for key, row in rows.items():
                current_revision = int(row.revision or 0) if row is not None else 0
                if current_revision != expected[key]:
                    return PlanPublishResult(
                        False,
                        revisions={
                            item: int(value.revision or 0) if value is not None else 0
                            for item, value in rows.items()
                        },
                        error=(
                            f"Remote {key} revision changed from {expected[key]} "
                            f"to {current_revision}; refresh before publishing."
                        ),
                    )
            for key, row in rows.items():
                revision = expected[key] + 1
                values = {
                    "payload": encoded[key],
                    "revision": revision,
                    "updated_at": _server_now(engine),
                    "updated_by_host": role.hostname,
                    "updated_by_device": role.device_id,
                }
                if row is None:
                    conn.execute(
                        table.insert().values(state_key=key, **values)
                    )
                else:
                    conn.execute(
                        table.update()
                        .where(table.c.state_key == key)
                        .where(table.c.revision == expected[key])
                        .values(**values)
                    )
                written_revisions[key] = revision

        ownership = get_main_device(engine)
        owner = ownership.main_device if ownership.success else None
        heartbeat_fresh = False
        if owner is not None:
            from src.services.runtime_device_state_repository import (
                get_runtime_device_liveness,
            )

            heartbeat_fresh = get_runtime_device_liveness(
                engine,
                device_id=owner.device_id,
            ).active
        return PlanPublishResult(
            True,
            revisions=written_revisions,
            verified_at=datetime.now(timezone.utc),
            execution_owner_hostname=owner.hostname if owner else "",
            execution_owner_heartbeat_fresh=heartbeat_fresh,
        )
    except (SQLAlchemyError, TypeError, ValueError) as exc:
        logger.info("Could not publish full planning snapshot: %s", exc)
        return PlanPublishResult(False, error=str(exc))


def _main_device_from_state(state: RemoteState) -> MainDevice:
    device_id = str(state.payload.get("device_id") or "").strip()
    hostname = str(state.payload.get("hostname") or "").strip()
    lease_token = str(state.payload.get("lease_token") or "").strip()
    # Existing pre-Workstream-6 ownership rows have no explicit field. Their
    # already-monotonic ownership revision is the safe migration baseline.
    lease_epoch = max(
        int(state.payload.get("lease_epoch") or 0),
        int(state.revision or 0),
    )
    if not device_id:
        raise ValueError("Main-device ownership row has no device_id")
    return MainDevice(
        device_id=device_id,
        hostname=hostname,
        revision=state.revision,
        updated_at=state.updated_at,
        lease_token=lease_token,
        lease_epoch=lease_epoch,
    )


def get_main_device(engine: Optional[Engine]) -> OwnershipResult:
    pulled = pull_state(engine, MAIN_DEVICE_KEY)
    if pulled.status == PULL_MISSING:
        return OwnershipResult(True)
    if pulled.status != PULL_OK or pulled.state is None:
        return OwnershipResult(False, error=pulled.error or "Could not read main-device ownership.")
    # A released lease is retained as a tombstone so its epoch cannot reset
    # to 1 on the next clean claim.  To callers it is still simply unclaimed.
    if not str(pulled.state.payload.get("device_id") or "").strip():
        return OwnershipResult(True)
    try:
        return OwnershipResult(True, main_device=_main_device_from_state(pulled.state))
    except ValueError as exc:
        return OwnershipResult(False, error=str(exc))


def claim_main_device(
    engine: Optional[Engine],
    role: LocalDeviceRole,
    *,
    expected_standby_generation: int = 0,
    standby_max_age_seconds: float = (
        COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
    ),
    require_operator_handoff_clear: bool = False,
) -> OwnershipResult:
    """Atomically make ``role`` the sole remote writer.

    Mints a fresh ``lease_token`` on every claim (manual or bootstrap) so
    ``ExecutionAuthority.require_current_lease`` has something to fence
    order submission against -- a device only trades while its cached token
    still matches the live ownership row.
    """
    if engine is None:
        return OwnershipResult(False, error="State sync database is unavailable.")
    try:
        runtime_table = None
        if int(expected_standby_generation or 0) > 0:
            from src.services.runtime_device_state_repository import (
                ensure_runtime_device_state_table,
            )

            runtime_table = ensure_runtime_device_state_table(engine)
        operator_command_table = None
        nontransferable_statuses: tuple[str, ...] = ()
        if require_operator_handoff_clear:
            from src.services.operator_commands import (
                NONTRANSFERABLE_OPERATOR_COMMAND_STATUSES,
                ensure_operator_commands_table,
            )

            operator_command_table = ensure_operator_commands_table(engine)
            nontransferable_statuses = tuple(
                status.value
                for status in NONTRANSFERABLE_OPERATOR_COMMAND_STATUSES
            )
        table = _ensure_state_sync_table(engine)
        with engine.begin() as conn:
            row = _select_row(conn, table, MAIN_DEVICE_KEY, for_update=True)
            if operator_command_table is not None:
                in_flight = conn.execute(
                    select(
                        operator_command_table.c.command_id,
                        operator_command_table.c.command_type,
                        operator_command_table.c.status,
                    )
                    .where(
                        operator_command_table.c.status.in_(
                            nontransferable_statuses
                        )
                    )
                    .order_by(operator_command_table.c.created_at.asc())
                    .limit(1)
                    .with_for_update()
                ).first()
                if in_flight is not None:
                    return OwnershipResult(
                        False,
                        error=(
                            "Execution-owner switch is paused while operator "
                            f"command {in_flight.command_type} "
                            f"({in_flight.status}) is in flight. Wait for its "
                            "terminal result, then retry."
                        ),
                    )
            if runtime_table is not None:
                from src.services.runtime_device_state_repository import (
                    verify_standby_generation_for_claim,
                )

                ready, error = verify_standby_generation_for_claim(
                    conn,
                    runtime_table,
                    device_id=role.device_id,
                    readiness_generation=expected_standby_generation,
                    max_age_seconds=standby_max_age_seconds,
                )
                if not ready:
                    return OwnershipResult(False, error=error)
            revision = int(row.revision or 1) + 1 if row is not None else 1
            prior_epoch = 0
            if row is not None:
                prior_payload = _decode_payload(row.payload, MAIN_DEVICE_KEY)
                prior_epoch = max(
                    int(prior_payload.get("lease_epoch") or 0),
                    int(row.revision or 0),
                )
            payload_json = json.dumps(
                {
                    "device_id": role.device_id,
                    "hostname": role.hostname,
                    "lease_token": str(uuid.uuid4()),
                    "lease_epoch": prior_epoch + 1,
                },
                separators=(",", ":"),
            )
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


def claim_main_device_if_stale(
    engine: Optional[Engine],
    role: LocalDeviceRole,
    *,
    expected_owner_device_id: str,
    heartbeat_cutoff_seconds: float = (
        COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
    ),
    expected_standby_generation: int = 0,
    standby_max_age_seconds: float = (
        COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
    ),
) -> OwnershipResult:
    """Atomically transfer ownership away from a confirmed-stale owner.

    This is the only safe way to auto-claim on behalf of an unattended
    device (e.g. a PC taking over after the laptop went dark). A naive
    "check heartbeat, then call ``claim_main_device``" sequence has a
    time-of-check/time-of-use gap: the previous owner can reconnect and
    resume its heartbeat between the caller's staleness observation and the
    claim. This function re-verifies both the current owner's identity and
    heartbeat staleness *inside the same row lock* that performs the
    transfer, using the database server's own clock throughout
    (``heartbeat_row_is_stale``), so nothing can change underneath it.

    Mints a fresh ``lease_token`` on success, exactly like ``claim_main_device``.
    """
    if engine is None:
        return OwnershipResult(False, error="State sync database is unavailable.")
    expected_owner_device_id = str(expected_owner_device_id or "").strip()
    if not expected_owner_device_id:
        return OwnershipResult(False, error="No expected owner device to verify against.")
    try:
        runtime_table = None
        if int(expected_standby_generation or 0) > 0:
            from src.services.runtime_device_state_repository import (
                ensure_runtime_device_state_table,
            )

            runtime_table = ensure_runtime_device_state_table(engine)
        table = _ensure_state_sync_table(engine)
        with engine.begin() as conn:
            row = _select_row(conn, table, MAIN_DEVICE_KEY, for_update=True)
            if row is None:
                return OwnershipResult(
                    False,
                    error="Ownership was already released; use claim_main_device for a clean handoff.",
                )
            current_state = _remote_state_from_row(row, MAIN_DEVICE_KEY)
            current_owner = _main_device_from_state(current_state)
            if current_owner.device_id != expected_owner_device_id:
                return OwnershipResult(
                    False,
                    error=(
                        "Ownership changed since it was observed stale "
                        f"(now {current_owner.device_id!r}, expected {expected_owner_device_id!r})."
                    ),
                )
            from src.services.runtime_device_state_repository import (
                runtime_device_row_is_stale,
            )

            runtime_stale = runtime_device_row_is_stale(
                conn,
                engine,
                device_id=current_owner.device_id,
                max_age_seconds=heartbeat_cutoff_seconds,
            )
            if runtime_stale is None:
                runtime_stale = heartbeat_row_is_stale(
                    conn,
                    engine,
                    current_owner.hostname,
                    process_name=MAIN_APP_PROCESS,
                    max_age_seconds=heartbeat_cutoff_seconds,
                )
            if not runtime_stale:
                return OwnershipResult(
                    False,
                    error="Previous owner's heartbeat is fresh again; not claiming.",
                )
            if runtime_table is not None:
                from src.services.runtime_device_state_repository import (
                    verify_standby_generation_for_claim,
                )

                ready, error = verify_standby_generation_for_claim(
                    conn,
                    runtime_table,
                    device_id=role.device_id,
                    readiness_generation=expected_standby_generation,
                    max_age_seconds=standby_max_age_seconds,
                )
                if not ready:
                    return OwnershipResult(False, error=error)

            payload_json = json.dumps(
                {
                    "device_id": role.device_id,
                    "hostname": role.hostname,
                    "lease_token": str(uuid.uuid4()),
                    "lease_epoch": max(
                        current_owner.lease_epoch,
                        int(row.revision or 0),
                    )
                    + 1,
                },
                separators=(",", ":"),
            )
            current_revision = int(row.revision or 1)
            conn.execute(
                table.update()
                .where(table.c.state_key == MAIN_DEVICE_KEY)
                .where(table.c.revision == current_revision)
                .values(
                    payload=payload_json,
                    revision=current_revision + 1,
                    updated_at=_server_now(engine),
                    updated_by_host=role.hostname,
                    updated_by_device=role.device_id,
                )
            )
            written_row = _select_row(conn, table, MAIN_DEVICE_KEY)
        if written_row is None:
            return OwnershipResult(False, error="Stale-owner claim was not persisted.")
        state = _remote_state_from_row(written_row, MAIN_DEVICE_KEY)
        return OwnershipResult(True, main_device=_main_device_from_state(state))
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("Could not atomically claim stale main-device ownership: %s", exc)
        return OwnershipResult(False, error=str(exc))


def claim_main_device_if_unclaimed(
    engine: Optional[Engine],
    role: LocalDeviceRole,
    *,
    expected_standby_generation: int = 0,
    standby_max_age_seconds: float = (
        COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
    ),
) -> OwnershipResult:
    """Atomically claim ownership only if the row is still genuinely missing.

    The clean-handoff counterpart to ``claim_main_device_if_stale``: after a
    device observes "no owner" (a released row), something else could still
    have claimed it in the gap before this call actually runs. Re-checking
    inside the same row lock that performs the insert closes that race the
    same way ``claim_main_device_if_stale`` closes it for the stale-heartbeat
    case -- a blind ``claim_main_device`` call here would silently steal the
    lease back from whatever legitimately claimed it in between.
    """
    if engine is None:
        return OwnershipResult(False, error="State sync database is unavailable.")
    try:
        runtime_table = None
        if int(expected_standby_generation or 0) > 0:
            from src.services.runtime_device_state_repository import (
                ensure_runtime_device_state_table,
            )

            runtime_table = ensure_runtime_device_state_table(engine)
        table = _ensure_state_sync_table(engine)
        with engine.begin() as conn:
            row = _select_row(conn, table, MAIN_DEVICE_KEY, for_update=True)
            if row is not None and str(
                _decode_payload(row.payload, MAIN_DEVICE_KEY).get("device_id") or ""
            ).strip():
                return OwnershipResult(
                    False,
                    error="Ownership was claimed by another device before this claim ran.",
                )
            if runtime_table is not None:
                from src.services.runtime_device_state_repository import (
                    verify_standby_generation_for_claim,
                )

                ready, error = verify_standby_generation_for_claim(
                    conn,
                    runtime_table,
                    device_id=role.device_id,
                    readiness_generation=expected_standby_generation,
                    max_age_seconds=standby_max_age_seconds,
                )
                if not ready:
                    return OwnershipResult(False, error=error)
            prior_epoch = 0
            prior_revision = 0
            if row is not None:
                prior_payload = _decode_payload(row.payload, MAIN_DEVICE_KEY)
                prior_epoch = int(prior_payload.get("lease_epoch") or 0)
                prior_revision = int(row.revision or 0)
            payload_json = json.dumps(
                {
                    "device_id": role.device_id,
                    "hostname": role.hostname,
                    "lease_token": str(uuid.uuid4()),
                    "lease_epoch": max(prior_epoch, prior_revision) + 1,
                },
                separators=(",", ":"),
            )
            values = {
                "payload": payload_json,
                "revision": prior_revision + 1 if row is not None else 1,
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
                    .where(table.c.revision == prior_revision)
                    .values(**values)
                )
            written_row = _select_row(conn, table, MAIN_DEVICE_KEY)
        if written_row is None:
            return OwnershipResult(False, error="Unclaimed-row claim was not persisted.")
        state = _remote_state_from_row(written_row, MAIN_DEVICE_KEY)
        return OwnershipResult(True, main_device=_main_device_from_state(state))
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("Could not atomically claim unclaimed main-device ownership: %s", exc)
        return OwnershipResult(False, error=str(exc))


def release_main_device(
    engine: Optional[Engine],
    role: LocalDeviceRole,
    *,
    expected_lease_token: str,
    expected_lease_epoch: int,
) -> OwnershipResult:
    """Release ownership iff the exact caller-held lease still owns the row.

    The blank-device payload is an intentional tombstone. ``get_main_device``
    exposes it as unclaimed, while the next claimant can still mint an epoch
    strictly greater than the released lease.  Deleting this row would allow
    a clean handoff to reuse epoch 1 after every release.
    """
    if engine is None:
        return OwnershipResult(False, error="State sync database is unavailable.")
    try:
        expected_token = str(expected_lease_token or "").strip()
        expected_epoch = int(expected_lease_epoch or 0)
        if not expected_token or expected_epoch <= 0:
            return OwnershipResult(
                False,
                error="An exact positive lease epoch and nonblank lease token are required.",
            )
        table = _ensure_state_sync_table(engine)
        with engine.begin() as conn:
            row = _select_row(conn, table, MAIN_DEVICE_KEY, for_update=True)
            if row is None:
                return OwnershipResult(True)
            current_state = _remote_state_from_row(row, MAIN_DEVICE_KEY)
            current_owner = _main_device_from_state(current_state)
            if current_owner.device_id != role.device_id:
                # Not ours to release -- ownership already moved on.
                return OwnershipResult(True)
            if current_owner.lease_token != expected_token:
                return OwnershipResult(
                    False,
                    error="Main-device lease token changed before release.",
                )
            if current_owner.lease_epoch != expected_epoch:
                return OwnershipResult(
                    False,
                    error="Main-device lease epoch changed before release.",
                )
            tombstone = json.dumps(
                {
                    "device_id": "",
                    "hostname": "",
                    "lease_token": "",
                    "lease_epoch": current_owner.lease_epoch,
                },
                separators=(",", ":"),
            )
            result = conn.execute(
                table.update()
                .where(table.c.state_key == MAIN_DEVICE_KEY)
                .where(table.c.revision == current_state.revision)
                .values(
                    payload=tombstone,
                    revision=current_state.revision + 1,
                    updated_at=_server_now(engine),
                    updated_by_host=role.hostname,
                    updated_by_device=role.device_id,
                )
            )
            if result.rowcount != 1:
                return OwnershipResult(
                    False,
                    error="Main-device lease changed before release committed.",
                )
        return OwnershipResult(True)
    except (SQLAlchemyError, ValueError, TypeError) as exc:
        logger.info("Could not release main-device ownership: %s", exc)
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
