"""Durable operator choice and approved-owner default initialization."""
from datetime import datetime, timezone
import re
from sqlalchemy.exc import SQLAlchemyError

from src.core.execution_config import COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS
from src.core.release_identity import require_approved_release_identity
from src.services.runtime_device_state_repository import get_runtime_device_state
from src.services.state_sync import (
    LiveTradingControlResult, get_live_trading_control, get_main_device,
    set_live_trading_control,
)
from src.services.trading_state import is_trading_locked_disabled


def _approved_owner(engine, *, require_ready=False):
    owner = get_main_device(engine)
    if not owner.success or owner.main_device is None:
        raise ValueError("No verified execution owner is available.")
    main = owner.main_device
    record = get_runtime_device_state(engine, device_id=main.device_id)
    if record is None:
        raise ValueError("The execution owner has no runtime identity.")
    stamp = record.updated_at.replace(tzinfo=timezone.utc) if record.updated_at.tzinfo is None else record.updated_at
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if not -5 <= age <= COORDINATION_DEVICE_HEARTBEAT_MAX_AGE_SECONDS:
        raise ValueError("The execution owner's runtime identity is stale.")
    details = record.details
    commit = str(details.get("repository_commit_sha") or "")
    release_id = str(details.get("release_id") or "")
    if not re.fullmatch(r"[0-9a-f]{40}:[0-9a-f]{64}", release_id) or not release_id.startswith(commit + ":"):
        raise ValueError("The execution owner has no approved release identity.")
    if require_ready and (
        record.state.value != "ACTIVE" or not all(details.get(k) is True for k in (
            "main_py_alive", "db_connected", "kis_ready", "executor_ready",
            "order_reconciliation_ready", "command_consumer_ready",
        ))
    ):
        raise ValueError("The execution owner is not ready for default initialization.")
    return main, commit


def set_operator_live_trading_control(engine, role, enabled):
    """An observer arms the executor's release, never its own checkout."""
    if not enabled:
        return set_live_trading_control(engine, role, False)
    if is_trading_locked_disabled():
        return LiveTradingControlResult(False, error="This machine is administratively locked.")
    try:
        _owner, commit = _approved_owner(engine)
    except SQLAlchemyError:
        return LiveTradingControlResult(False, error="Shared execution-owner control is unavailable.")
    except (ValueError, RuntimeError) as exc:
        return LiveTradingControlResult(False, error=str(exc))
    return set_live_trading_control(engine, role, True, runtime_commit_sha=commit)


def initialize_default_live_trading_control(engine, role):
    """Create persistent ON once for an approved, ready Main with no choice.

    Every existing choice, including OFF, survives startup and handoff. The
    revision check prevents a concurrent operator OFF from being overwritten.
    """
    saved = get_live_trading_control(engine)
    if not saved.success or saved.control is None or saved.control.revision:
        return saved
    if is_trading_locked_disabled():
        return LiveTradingControlResult(False, error="Administrative lock preserves OFF.")
    try:
        owner, commit = _approved_owner(engine, require_ready=True)
        identity = require_approved_release_identity()
        if owner.device_id != role.device_id or commit != identity.repository_head:
            raise ValueError("Only the approved, ready execution owner may initialize Live Trading.")
    except SQLAlchemyError:
        return LiveTradingControlResult(False, error="Shared execution-owner control is unavailable.")
    except (ValueError, RuntimeError) as exc:
        return LiveTradingControlResult(False, error=str(exc))
    result = set_live_trading_control(
        engine, role, True, runtime_commit_sha=commit, expected_revision=0,
    )
    return result if result.success else get_live_trading_control(engine)
