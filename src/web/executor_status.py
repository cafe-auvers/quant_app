"""Read executor heartbeat projections without constructing an execution service."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.core.execution_config import (
    COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS,
    COORDINATION_RU_PROFILE,
)


def _datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, dt.datetime):
        value = dt.datetime.fromisoformat(str(value))
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def executor_status(connection, main: dict[str, Any]) -> dict[str, Any]:
    hostname = str(main.get("hostname") or "").strip().lower()
    device_id = str(main.get("device_id") or "").strip()
    result = {
        "executor": "UNKNOWN",
        "executor_hostname": hostname,
        "executor_state": "UNKNOWN",
        "executor_reason": "No executor heartbeat is available",
        "executor_heartbeat_age_seconds": None,
    }
    now_query = "SELECT UTC_TIMESTAMP(6)" if connection.dialect.name == "mysql" else "SELECT CURRENT_TIMESTAMP"
    now = _datetime(connection.execute(text(now_query)).scalar())
    runtime = None
    if device_id:
        try:
            runtime = connection.execute(
                text("SELECT hostname, state, updated_at, details_json FROM runtime_device_state WHERE device_id=:device_id LIMIT 1"),
                {"device_id": device_id},
            ).mappings().first()
        except DBAPIError as exc:
            # Older deployments can lack the canonical heartbeat table.
            missing = getattr(exc.orig, "args", (None,))[0] == 1146 or "no such table: runtime_device_state" in str(exc.orig)
            if not missing:
                raise
    details = {}
    if runtime is not None:
        try:
            details = json.loads(runtime["details_json"] or "{}")
        except (ValueError, TypeError):
            pass
    if isinstance(details, dict) and "main_py_alive" in details and details.get("coordination_ru_profile") == COORDINATION_RU_PROFILE:
        stamp = _datetime(runtime["updated_at"])
        age = max(0.0, (now - stamp).total_seconds()) if now and stamp else None
        state = str(runtime["state"] or "UNKNOWN").upper()
        alive = bool(details["main_py_alive"]) and state not in {"FAILED", "STOPPED"}
        fresh = age is not None and age <= COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
        result.update(
            executor_hostname=str(runtime["hostname"] or hostname),
            executor_state=state,
            executor_heartbeat_age_seconds=round(age, 1) if age is not None else None,
        )
        if not fresh or not alive:
            result.update(executor="STALE", executor_reason="Executor heartbeat is stale" if not fresh else "Executor process is stopped")
        elif state == "ACTIVE" and details.get("executor_ready") is False:
            result.update(executor="BLOCKED", executor_reason=str(details.get("executor_not_ready_reason") or "Executor readiness checks have not passed"))
        elif state in {"STARTING", "STANDBY", "STANDBY_READY", "SHUTTING_DOWN"}:
            result.update(executor=state, executor_reason=str(details.get("executor_not_ready_reason") or f"Executor is {state.lower().replace('_', ' ')}"))
        elif state == "ACTIVE":
            result.update(executor="HEALTHY", executor_reason="Executor heartbeat is current")
        else:
            result.update(executor_reason="Executor state is unknown")
        return result
    if hostname:
        legacy = connection.execute(
            text("SELECT active, heartbeat_at FROM app_runtime_status WHERE hostname=:hostname AND process_name='main.py' LIMIT 1"),
            {"hostname": hostname},
        ).mappings().first()
        stamp = _datetime(legacy["heartbeat_at"]) if legacy else None
        if now and stamp:
            age = max(0.0, (now - stamp).total_seconds())
            healthy = bool(legacy["active"]) and age <= 60.0
            result.update(
                executor="HEALTHY" if healthy else "STALE",
                executor_state="LEGACY",
                executor_reason="Executor heartbeat is current" if healthy else "Executor heartbeat is stale",
                executor_heartbeat_age_seconds=round(age, 1),
            )
    return result
