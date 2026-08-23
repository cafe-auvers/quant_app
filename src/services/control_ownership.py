"""Independent execution-owner and operator-owner coordination helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import select

from src.core.execution_config import (
    COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS,
)
from src.core.runtime_readiness import RuntimeDeviceState
from src.infrastructure.database.coordination_engine import coordination_read_connection
from src.services.runtime_device_state_repository import (
    RuntimeDeviceRecord,
    get_runtime_device_state,
    runtime_row_owns_process_liveness,
)
from src.services.runtime_status import get_runtime_process_status
from src.services.state_sync import (
    LocalDeviceRole,
    MainDevice,
    claim_main_device,
    get_main_device,
    get_synced_state_revisions,
)
from src.utils.device_identity import DEVICE_KIND_LAPTOP, runtime_device_kind


@dataclass(frozen=True)
class ExecutorReadinessCheck:
    ready: bool
    reasons: Tuple[str, ...] = ()
    record: Optional[RuntimeDeviceRecord] = None


@dataclass(frozen=True)
class ExecutionOwnerSwitchResult:
    success: bool
    execution_owner: Optional[MainDevice] = None
    error: str = ""
    readiness: Optional[ExecutorReadinessCheck] = None


def _server_reference_time(engine) -> datetime:
    from src.services.runtime_device_state_repository import _server_now

    with coordination_read_connection(engine) as conn:
        value = conn.execute(select(_server_now(engine))).scalar()
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def check_executor_readiness(
    engine,
    *,
    target_device_id: str,
    max_age_seconds: float = COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS,
) -> ExecutorReadinessCheck:
    """Fail-closed readiness check for an execution-owner target."""

    if engine is None:
        return ExecutorReadinessCheck(False, ("Shared database is unavailable.",))
    record = get_runtime_device_state(engine, device_id=target_device_id)
    if record is None:
        return ExecutorReadinessCheck(
            False,
            ("Target has not published runtime readiness.",),
        )
    reasons = []
    reference = _server_reference_time(engine)
    age = (reference - record.updated_at).total_seconds()
    if age < 0 or age > float(max_age_seconds):
        reasons.append(f"{record.hostname} readiness heartbeat is stale ({age:.1f}s).")

    if not runtime_row_owns_process_liveness(record):
        heartbeat = get_runtime_process_status(
            engine,
            record.hostname,
            max_age_seconds=max(0, int(max_age_seconds)),
        )
        if not heartbeat.active:
            if heartbeat.age_seconds is None:
                reasons.append(f"{record.hostname} main.py heartbeat is missing.")
            else:
                reasons.append(
                    f"{record.hostname} main.py heartbeat is older than "
                    f"{max(0, int(max_age_seconds))} seconds."
                )

    if record.state != RuntimeDeviceState.STANDBY_READY:
        reasons.append(
            f"{record.hostname} runtime is {record.state.value}, not STANDBY_READY."
        )

    details = record.details
    if not details:
        reasons.append(f"{record.hostname} has not published executor readiness details.")
    checks = (
        ("main_py_alive", "main.py is not ready"),
        ("db_connected", "MySQL read/write connection is not healthy"),
        ("kis_ready", "KIS session is not ready"),
        ("account_environment_ready", "broker account/environment is not ready"),
        ("market_data_ready", "realtime/quote provider is not ready"),
        ("command_consumer_ready", "operator command consumer is not active"),
        ("order_reconciliation_ready", "order reconciliation is not active"),
        ("state_revisions_current", "latest synchronized state revisions are not absorbed"),
        ("no_stale_local_state", "stale local-only state is present"),
        ("executor_ready", "executor readiness aggregate is false"),
    )
    for key, message in checks:
        if details and not bool(details.get(key, False)):
            reasons.append(f"{record.hostname}: {message}.")

    remote_revisions = get_synced_state_revisions(engine)
    revision_fields = {
        "watchlist": "latest_watchlist_revision",
        "buylist": "latest_buylist_revision",
        "trade_plans": "latest_trade_plans_revision",
        "execution_queue": "latest_execution_queue_revision",
    }
    for state_key, detail_key in revision_fields.items():
        remote_revision = int(remote_revisions.get(state_key, 0) or 0)
        absorbed_revision = int(details.get(detail_key, 0) or 0)
        if remote_revision != absorbed_revision:
            reasons.append(
                f"{record.hostname} has {state_key} revision {absorbed_revision}; "
                f"canonical revision is {remote_revision}."
            )

    power_state = str(details.get("power_state") or "UNKNOWN").upper()
    if power_state in {"SLEEPING", "STANDBY", "SUSPENDING", "SHUTTING_DOWN"}:
        reasons.append(f"{record.hostname} power state is {power_state}.")
    if (
        runtime_device_kind(record.hostname, details) == DEVICE_KIND_LAPTOP
        and not bool(details.get("sleep_blocker_active", False))
    ):
        reasons.append(f"{record.hostname} laptop sleep blocker is not active.")

    return ExecutorReadinessCheck(
        ready=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        record=record,
    )


def switch_execution_owner(
    engine,
    *,
    initiated_by: LocalDeviceRole,
    target_device_id: str,
    max_age_seconds: float = COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS,
) -> ExecutionOwnerSwitchResult:
    """Assign execution ownership only after the target passes every gate."""

    del initiated_by  # retained in the API for UI/audit call-site clarity
    current = get_main_device(engine)
    if not current.success:
        return ExecutionOwnerSwitchResult(False, error=current.error)
    if current.main_device is not None and (
        current.main_device.device_id == str(target_device_id or "")
    ):
        return ExecutionOwnerSwitchResult(True, execution_owner=current.main_device)

    readiness = check_executor_readiness(
        engine,
        target_device_id=str(target_device_id or ""),
        max_age_seconds=max_age_seconds,
    )
    if not readiness.ready or readiness.record is None:
        reason = readiness.reasons[0] if readiness.reasons else "Target is not ready."
        owner = current.main_device.hostname if current.main_device else "unassigned"
        return ExecutionOwnerSwitchResult(
            False,
            execution_owner=current.main_device,
            error=f"Cannot switch executor. Reason: {reason} Execution ownership remains on {owner}.",
            readiness=readiness,
        )

    target = LocalDeviceRole(
        device_id=readiness.record.device_id,
        hostname=readiness.record.hostname,
        is_main=False,
    )
    claimed = claim_main_device(
        engine,
        target,
        expected_standby_generation=readiness.record.readiness_generation,
        standby_max_age_seconds=max_age_seconds,
        require_operator_handoff_clear=True,
    )
    if not claimed.success:
        return ExecutionOwnerSwitchResult(
            False,
            execution_owner=current.main_device,
            error=claimed.error or "Execution-owner switch did not commit.",
            readiness=readiness,
        )
    return ExecutionOwnerSwitchResult(
        True,
        execution_owner=claimed.main_device,
        readiness=readiness,
    )
