"""Cross-process sleep-readiness signal for the PC's guarded-sleep automation.

Windows Task Scheduler / PowerShell cannot inspect a running Qt process
directly, so this writes a small JSON snapshot that the PowerShell-side
``scripts/Invoke-GuardedSleep.ps1`` guard reads before allowing the PC to
suspend (S3). Written every 30s by a QTimer in ``MainWindow``, alongside the
existing 15s ``state_sync_timer``.

``safe_to_sleep`` is false only when this device is main *and* something is
actually in flight -- a pull-only device (the common case, since only the
device holding the main-device lease trades live) is always safe to sleep
regardless of what the synced buylist shows, since it isn't the one acting
on it.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Dict

from src.core.execution_queue import HANDOFF_MONITORABLE_STATUSES
from src.services.order_ledger import find_open_orders
from src.utils.config import DATA_DIR
from src.utils.storage import save_json

SLEEP_READINESS_FILE = DATA_DIR / "sleep_readiness.json"


def _in_flight_prod_symbol_count(buylist_manager: Any) -> int:
    if buylist_manager is None:
        return 0
    return sum(
        1
        for item in getattr(buylist_manager, "items", [])
        if str(getattr(item, "environment", "") or "").upper() == "PROD"
        and str(getattr(item, "monitoring_status", "") or "").upper()
        in HANDOFF_MONITORABLE_STATUSES
    )


def build_sleep_readiness_snapshot(main_window: Any) -> Dict[str, Any]:
    """Pure function: compute the snapshot dict from a MainWindow-like object.

    Kept separate from the file write so it's directly unit-testable against
    plain attribute-bearing objects, the same way the rest of this handoff
    feature's tests use ``MainWindow.__new__(MainWindow)`` / ``SimpleNamespace``.
    """
    role = getattr(main_window, "state_sync_role", None)
    is_main_device = bool(role is not None and getattr(role, "is_main", False))
    in_flight_count = _in_flight_prod_symbol_count(
        getattr(main_window, "buylist_manager", None)
    )
    try:
        open_orders = find_open_orders(getattr(main_window, "order_ledger", None) or [])
    except Exception:
        # A ledger read problem must not make sleep look artificially safe.
        open_orders = [True]
    handoff_worker = getattr(main_window, "handoff_reconciliation_worker", None)
    is_running = getattr(handoff_worker, "isRunning", None)
    reconciliation_running = bool(handoff_worker is not None and callable(is_running) and is_running())

    safe_to_sleep = not (
        is_main_device
        and (in_flight_count > 0 or bool(open_orders) or reconciliation_running)
    )

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pid": os.getpid(),
        "is_main_device": is_main_device,
        "in_flight_prod_symbol_count": in_flight_count,
        "has_open_broker_orders": bool(open_orders),
        "handoff_reconciliation_in_progress": reconciliation_running,
        "safe_to_sleep": safe_to_sleep,
    }


def write_sleep_readiness_snapshot(
    main_window: Any, *, path: Path = SLEEP_READINESS_FILE
) -> Dict[str, Any]:
    """Best-effort write -- a failure here must never block or crash the app."""
    snapshot = build_sleep_readiness_snapshot(main_window)
    try:
        save_json(path, snapshot)
    except Exception:
        pass
    return snapshot
