"""Background workers for shared planning and execution ownership control."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from src.core import execution_config
from src.core.runtime_readiness import RuntimeDeviceState
from src.services.app_state import (
    StateReconcileResult,
    activate_device_as_main,
    auto_claim_main_device_if_stale,
    publish_trading_plan,
    reconcile_state_with_remote,
)
from src.services.runtime_status import record_runtime_heartbeat
from src.services.state_sync import (
    LocalDeviceRole,
    get_coordination_status_snapshot,
    live_trading_control_is_effective,
    set_live_trading_control,
    set_operator_control,
)
from src.utils.device_identity import runtime_device_kind

logger = logging.getLogger(__name__)


class StateSyncWorker(QThread):
    """Reconcile shared state without blocking the Qt event loop."""

    completed = pyqtSignal(object, int)

    def __init__(
        self,
        engine,
        role: LocalDeviceRole,
        save_lock: threading.Lock,
        *,
        activate: bool = False,
        ownership_only_when_main: bool = False,
        generation: int = 0,
        auto_claim: bool = False,
        expected_owner_device_id: str = "",
        expected_standby_generation: int = 0,
        require_runtime_ready_claim: bool = False,
        metadata_path=None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.role = role
        self.save_lock = save_lock
        self.activate = activate
        self.ownership_only_when_main = ownership_only_when_main
        self.generation = int(generation)
        self.auto_claim = auto_claim
        self.expected_owner_device_id = expected_owner_device_id
        self.expected_standby_generation = int(expected_standby_generation or 0)
        self.require_runtime_ready_claim = bool(require_runtime_ready_claim)
        self.metadata_path = metadata_path

    def run(self) -> None:
        try:
            if self.auto_claim:
                result = auto_claim_main_device_if_stale(
                    self.engine,
                    self.role,
                    expected_owner_device_id=self.expected_owner_device_id,
                    save_lock=self.save_lock,
                    expected_standby_generation=self.expected_standby_generation,
                    metadata_path=self.metadata_path,
                )
            elif self.activate:
                result = activate_device_as_main(
                    self.engine,
                    self.role,
                    save_lock=self.save_lock,
                    expected_standby_generation=self.expected_standby_generation,
                    metadata_path=self.metadata_path,
                )
            else:
                result = reconcile_state_with_remote(
                    self.engine,
                    self.role,
                    save_lock=self.save_lock,
                    ownership_only_when_main=self.ownership_only_when_main,
                    allow_unprepared_claim=not self.require_runtime_ready_claim,
                    metadata_path=self.metadata_path,
                )
        except Exception as exc:
            logger.exception("State sync worker failed")
            result = StateReconcileResult(
                errors=[f"State sync failed: {exc}"],
                local_role=self.role,
            )
        coordination_status = get_coordination_status_snapshot(self.engine)
        control_result = coordination_status.live_trading
        if control_result.success and control_result.control is not None:
            result.live_trading_enabled = live_trading_control_is_effective(
                control_result.control
            )
            result.live_trading_revision = control_result.control.revision
        else:
            result.live_trading_error = (
                control_result.error or "Could not read shared live-trading control."
            )
        operator_result = coordination_status.operator_control
        if operator_result.success:
            result.operator_control = operator_result.control
        else:
            result.operator_control_error = (
                operator_result.error
                or "Could not read shared operator-control ownership."
            )
        if not result.state_revisions:
            result.state_revisions = coordination_status.state_revisions
        try:
            from src.services.operator_commands import list_operator_commands
            from src.services.runtime_device_state_repository import (
                list_runtime_device_states,
            )

            result.runtime_devices = list_runtime_device_states(self.engine)
            result.operator_commands = list_operator_commands(self.engine, limit=10)
        except Exception:
            logger.debug("Could not read runtime device/command status", exc_info=True)
            result.runtime_devices = []
            result.operator_commands = []
        result.last_verified_at = dt.datetime.now(dt.timezone.utc)
        self.completed.emit(result, self.generation)


class LiveTradingControlWorker(QThread):
    """Persist one global kill-switch action without blocking the Qt thread."""

    completed = pyqtSignal(object)

    def __init__(self, engine, role: LocalDeviceRole, enabled: bool) -> None:
        super().__init__()
        self.engine = engine
        self.role = role
        self.enabled = bool(enabled)

    def run(self) -> None:
        self.completed.emit(
            set_live_trading_control(
                self.engine,
                self.role,
                self.enabled,
            )
        )


@dataclass(frozen=True)
class ControlOwnerUpdate:
    control: str
    success: bool
    target_label: str
    result: object = None
    error: str = ""


def control_runtime_identity_available(
    record, *, now: Optional[dt.datetime] = None
) -> bool:
    """Accept only a fresh runtime identity that can participate in control."""

    state = getattr(record, "state", "")
    state_value = str(getattr(state, "value", state) or "").upper()
    if state_value not in {
        RuntimeDeviceState.STARTING.value,
        RuntimeDeviceState.STANDBY.value,
        RuntimeDeviceState.STANDBY_READY.value,
        RuntimeDeviceState.ACTIVE.value,
    }:
        return False
    updated_at = getattr(record, "updated_at", None)
    if not isinstance(updated_at, dt.datetime):
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=dt.timezone.utc)
    reference = now or dt.datetime.now(dt.timezone.utc)
    age_seconds = (reference - updated_at.astimezone(dt.timezone.utc)).total_seconds()
    return (
        -5.0
        <= age_seconds
        <= execution_config.COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
    )


def control_target_role_from_records(
    records, target_label: str
) -> Optional[LocalDeviceRole]:
    candidates = [
        record
        for record in records
        if control_runtime_identity_available(record)
        if runtime_device_kind(record.hostname, record.details) == target_label
    ]
    if not candidates:
        return None
    record = max(candidates, key=lambda item: item.updated_at)
    return LocalDeviceRole(
        device_id=record.device_id,
        hostname=record.hostname,
        is_main=False,
    )


class ControlOwnerWorker(QThread):
    """Switch either owner without blocking the Qt event loop."""

    completed = pyqtSignal(object)

    def __init__(
        self,
        engine,
        initiated_by: LocalDeviceRole,
        *,
        control: str,
        target: Optional[LocalDeviceRole],
        target_label: str,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.initiated_by = initiated_by
        self.control = str(control)
        self.target = target
        self.target_label = str(target_label)

    def run(self) -> None:
        try:
            target = self.target
            if target is None and self.target_label != "Locked":
                from src.services.runtime_device_state_repository import (
                    list_runtime_device_states,
                )

                records = list_runtime_device_states(self.engine)
                target = control_target_role_from_records(
                    records,
                    self.target_label,
                )
                if target is None:
                    identity_exists = any(
                        runtime_device_kind(record.hostname, record.details)
                        == self.target_label
                        for record in records
                    )
                    error = (
                        f"The {self.target_label} runtime is registered, but its "
                        "heartbeat is stale or its process state is not eligible. "
                        "Keep main.py running and verify Buy Board readiness."
                        if identity_exists
                        else (
                            f"No {self.target_label} runtime identity is registered "
                            "in shared coordination. Start main.py on that device."
                        )
                    )
                    self.completed.emit(
                        ControlOwnerUpdate(
                            control=self.control,
                            success=False,
                            target_label=self.target_label,
                            error=error,
                        )
                    )
                    return
            if target is not None and target.device_id == self.initiated_by.device_id:
                record_runtime_heartbeat(
                    self.engine,
                    hostname=target.hostname,
                )
            if self.control == "operator":
                result = set_operator_control(
                    self.engine,
                    self.initiated_by,
                    target,
                )
                success = bool(result.success)
                error = str(result.error or "")
            else:
                from src.services.control_ownership import switch_execution_owner

                if target is None:
                    raise ValueError("Execution Owner cannot be Locked")
                result = switch_execution_owner(
                    self.engine,
                    initiated_by=self.initiated_by,
                    target_device_id=target.device_id,
                )
                success = bool(result.success)
                error = str(result.error or "")
        except Exception as exc:
            logger.exception("Control-owner switch failed")
            result = None
            success = False
            error = str(exc)
        self.completed.emit(
            ControlOwnerUpdate(
                control=self.control,
                success=success,
                target_label=self.target_label,
                result=result,
                error=error,
            )
        )


class PlanPublishWorker(QThread):
    """Publish a copied planning snapshot and verify its shared revisions."""

    completed = pyqtSignal(object)

    def __init__(
        self,
        engine,
        role: LocalDeviceRole,
        payloads: tuple,
        execution_queue: Dict[str, Any],
        *,
        metadata_path,
        market_is_open: bool,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.role = role
        self.payloads = payloads
        self.execution_queue = dict(execution_queue)
        self.metadata_path = metadata_path
        self.market_is_open = bool(market_is_open)

    def run(self) -> None:
        watchlist, buylist, trade_plans = self.payloads[:3]
        self.completed.emit(
            publish_trading_plan(
                self.engine,
                self.role,
                watchlist,
                buylist,
                trade_plans,
                self.execution_queue,
                market_is_open=self.market_is_open,
                metadata_path=self.metadata_path,
            )
        )


# Compatibility names for callers that imported the former main-window helpers.
_control_runtime_identity_available = control_runtime_identity_available
_control_target_role_from_records = control_target_role_from_records


__all__ = [
    "ControlOwnerUpdate",
    "ControlOwnerWorker",
    "LiveTradingControlWorker",
    "PlanPublishWorker",
    "StateSyncWorker",
    "control_runtime_identity_available",
    "control_target_role_from_records",
]
