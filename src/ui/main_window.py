"""Main application window for the stock dashboard."""

import datetime as dt
import logging
import math
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout,
                             QLabel, QLineEdit, QMainWindow, QMessageBox,
                             QProgressBar, QPushButton, QSizePolicy, QStyle,
                             QSystemTrayIcon, QTabWidget, QTextEdit,
                             QVBoxLayout, QWidget)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None

from src.core.order_state import (BrokerOrder, OrderIntent, OrderSide,
                                  OrderStatus)
from src.core import execution_config
from src.core.runtime_readiness import EngineReadiness, RuntimeDeviceState
from src.core.scanner import StockScanner
from src.core.watchlist import BuylistManager, TradePlanManager, Watchlist
from src.infrastructure.database.mirror_engine import resolve_data_engine
from src.infrastructure.database.coordination_engine import (
    coordination_database_configured,
    init_coordination_engine,
)
from src.infrastructure.database.mirror_freshness import (
    local_mirror_hourly_is_stale, local_mirror_is_stale)
from src.infrastructure.database.operational_engine import (
    init_local_operational_engine,
)
from src.risk.orb_position import configure_orb_settings
from src.services import trading_state
from src.services.app_state import (EXECUTION_QUEUE_FILE, SETTINGS_FILE,
                                    SaveResult, StateReconcileResult,
                                    activate_device_as_main,
                                    auto_claim_main_device_if_stale,
                                    get_state_save_manager, load_buylist_state,
                                    load_chart_drawings_state,
                                    load_scanner_setups_state,
                                    load_tab_options_state,
                                    load_trade_plans_state,
                                    load_watchlist_state,
                                    publish_handoff_snapshot,
                                    publish_trading_plan,
                                    reconcile_state_with_remote,
                                    release_main_device_and_demote,
                                    save_app_state, should_auto_claim_main)
from src.services.cloud_backup import (restore_state_directory,
                                       restore_state_files)
from src.services.execution_authority import ExecutionAuthority, LeaseHandle
from src.services.handoff_reconciliation import reset_runtime_only_order_flags
from src.services.historical_refresh_control import (MODE_1D, MODE_1H,
                                                     is_refresh_running,
                                                     read_status,
                                                     reconcile_stale_status)
from src.services.market_pulse import MarketPulseService
from src.services.order_ledger import (append_order, find_open_orders,
                                       has_open_order, load_order_ledger,
                                       merge_orders, save_order_ledger,
                                       update_order)
from src.services.runtime_status import (
    record_runtime_heartbeat,
    safe_mark_runtime_process_stopped,
)
from src.services.sleep_readiness import write_sleep_readiness_snapshot
from src.services.state_sync import (
    LocalDeviceRole,
    get_coordination_status_snapshot,
    get_live_trading_control,
    load_local_device_role,
    set_operator_control,
    set_live_trading_control,
)
from src.ui.buylist import BuylistMixin
from src.ui.buyboard import BuyboardMixin
from src.ui.charts.controller import ChartsControllerMixin
from src.ui.charts.renderer import ChartsRenderMixin
from src.ui.controllers import (AccountController, BuylistController,
                                BuylistExecutionController,
                                ChartDataController, ScannerController)
from src.ui.dialogs import (BackupEnvDialog, RestoreBackupDialog,
                            RestoreEnvDialog, SettingsDialog)
from src.ui.filter_catalog import (DEFAULT_SCANNER_SETUPS, DEFAULT_SETTINGS,
                                   DEFAULT_TAB_OPTIONS)
from src.ui.health import HealthPanelMixin
from src.ui.mixins.dashboard_mixin import DashboardMixin
from src.ui.mixins.chart_command_routing_mixin import ChartCommandRoutingMixin
from src.ui.mixins.planning_support_mixin import PlanningSupportMixin
from src.ui.mixins.scanner_mixin import ScannerMixin
from src.ui.mixins.sidebar_mixin import SidebarMixin
from src.ui.mixins.watchlist_actions_mixin import WatchlistActionsMixin
from src.ui.market_pulse import MarketPulseMixin
from src.ui.orb_settings_dialog import OrbSettingsDialog
from src.ui.order_workers import HandoffReconciliationWorker
from src.ui.workers import PcRemoteStatusWorker
from src.utils.config import DATA_DIR, ROOT_DIR, get_env_value
from src.utils.data_loader import get_default_universe
from src.utils.device_identity import detect_local_device_kind, runtime_device_kind
from src.utils.intraday_helpers import \
    extract_latest_opening_bar as _extract_latest_opening_bar
from src.utils.market_calendar import (expected_latest_market_data_date,
                                       is_regular_session_open,
                                       seconds_until_nyse_regular_session_open)
from src.utils.storage import load_json, save_json

__all__ = [
    "MainWindow",
    "QTimer",
    "_extract_latest_opening_bar",
    "append_order",
    "find_open_orders",
    "has_open_order",
    "load_order_ledger",
    "merge_orders",
    "save_order_ledger",
    "update_order",
]


logger = logging.getLogger(__name__)

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LOCAL_MIRROR_SYNC_INTERVAL_MS = 15 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
WORKER_SHUTDOWN_TIMEOUT_MS = 30_000
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)

_STANDBY_GATE_LABELS = {
    "startup_reconciliation_complete": "initial broker reconciliation",
    "account_reconciliation_fresh": "fresh broker account snapshot",
    "websocket_connected": "KIS WebSocket connection",
    "critical_trade_subscriptions_acked": "trade subscription acknowledgements",
    "critical_quote_subscriptions_acked": "quote subscription acknowledgements",
    "accumulator_draining_within_budget": "sustained market-data queue delay",
    "database_writable": "local Kanban operational state writable",
}


@dataclass(frozen=True)
class BuyboardReadinessDisplay:
    completed: int
    total: int
    label: str
    tooltip: str
    indeterminate: bool = False


def _format_readiness_eta(seconds: float) -> str:
    remaining = max(0, int(seconds))
    days, remainder = divmod(remaining, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    prefix = f"{days}d " if days else ""
    return f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"


def _live_execution_status_text(enabled: bool) -> str:
    """Describe both the shared switch and the configured broker envelope."""

    switch = "Enabled" if bool(enabled) else "Disabled"
    mode = str(execution_config.KIS_LIVE_EXECUTION_MODE or "DISABLED").upper()
    if mode != "CONTROLLED_LIVE":
        return f"{switch} ({mode})"
    symbols = ",".join(execution_config.KIS_CONTROLLED_LIVE_SYMBOLS) or "none"
    cap = float(execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL or 0.0)
    return f"{switch} (CONTROLLED_LIVE: {symbols}, max ${cap:,.2f}/entry)"


def _per_symbol_quote_guard_detail(
    readiness: EngineReadiness,
    *,
    regular_session_open: bool,
    seconds_until_open: Optional[float],
) -> str:
    if readiness.critical_quotes_fresh:
        return ""
    detail = (
        "Per-symbol execution guard: symbols without a fresh execution-grade "
        "quote remain individually blocked; overall board readiness is unchanged."
    )
    if not regular_session_open and seconds_until_open is not None:
        detail += f" Market opens in {_format_readiness_eta(seconds_until_open)}."
    return detail


def _buyboard_readiness_display(
    readiness: EngineReadiness,
    *,
    device_state: RuntimeDeviceState,
    reconciliation_accounts: Tuple[str, ...] = (),
    regular_session_open: bool = False,
    seconds_until_open: Optional[float] = None,
    auto_claim_enabled: bool = False,
    is_main_device: bool = False,
) -> BuyboardReadinessDisplay:
    """Project the authoritative readiness predicate into operator language."""

    checks = readiness.standby_check_results
    completed = readiness.standby_checks_completed
    total = len(checks)
    blockers = readiness.standby_blockers
    blocked_labels = tuple(_STANDBY_GATE_LABELS[item] for item in blockers)
    quote_guard_detail = _per_symbol_quote_guard_detail(
        readiness,
        regular_session_open=regular_session_open,
        seconds_until_open=seconds_until_open,
    )

    if device_state == RuntimeDeviceState.ACTIVE:
        tooltip_parts = [
            "Startup readiness is latched while the worker remains ACTIVE.",
            "Every broker mutation is revalidated for its account, symbol, and action.",
        ]
        if blocked_labels:
            tooltip_parts.append(
                "Current action guards: " + ", ".join(blocked_labels)
            )
        if quote_guard_detail:
            tooltip_parts.append(quote_guard_detail)
        return BuyboardReadinessDisplay(
            total,
            total,
            f"Buy Board readiness {total}/{total} — ACTIVE; "
            "broker mutations remain guarded by Live Trading",
            " | ".join(tooltip_parts),
        )
    elif reconciliation_accounts:
        accounts = ", ".join(reconciliation_accounts)
        reason = f"final broker reconciliation for {accounts} (ETA unavailable)"
        return BuyboardReadinessDisplay(
            completed,
            total,
            f"Buy Board startup — {reason}",
            "A live broker query is in progress. Its completion time depends on KIS response latency.",
            indeterminate=True,
        )
    elif not readiness.startup_reconciliation_complete:
        reason = "initial broker reconciliation (ETA unavailable)"
        return BuyboardReadinessDisplay(
            completed,
            total,
            f"Buy Board startup — {reason}",
            "Startup cannot become ready until every configured account has been reconciled.",
            indeterminate=True,
        )
    elif len(blocked_labels) > 1:
        reason = f"{len(blocked_labels)} checks pending: " + "; ".join(
            blocked_labels
        )
    elif not readiness.database_writable:
        reason = "waiting for the local Kanban operational store"
    elif not readiness.account_reconciliation_fresh:
        reason = "waiting for a fresh broker account snapshot"
    elif not readiness.websocket_connected:
        reason = "connecting to the KIS WebSocket"
    elif not readiness.critical_trade_subscriptions_acked:
        reason = "waiting for KIS trade-subscription ACKs"
    elif not readiness.critical_quote_subscriptions_acked:
        reason = "waiting for KIS quote-subscription ACKs"
    elif not readiness.accumulator_draining_within_budget:
        reason = "market-data queue missed its drain budget three times"
    elif device_state == RuntimeDeviceState.STANDBY_READY:
        reason = (
            "STANDBY_READY; automatic PC claim is armed"
            if auto_claim_enabled
            else "STANDBY_READY; use this device as Main"
        )
    elif readiness.standby_ready:
        reason = (
            "Main lease held; per-symbol execution guards active"
            if is_main_device
            else "publishing final readiness confirmation"
        )
    else:
        reason = "checking execution dependencies"

    passed_labels = tuple(
        _STANDBY_GATE_LABELS[field_name]
        for field_name, passed in checks
        if passed
    )
    tooltip_parts = []
    if passed_labels:
        tooltip_parts.append("Passed: " + ", ".join(passed_labels))
    if blocked_labels:
        tooltip_parts.append("Waiting: " + ", ".join(blocked_labels))
    if quote_guard_detail:
        tooltip_parts.append(quote_guard_detail)
    tooltip_parts.append(
        "Automatic PC claim is enabled."
        if auto_claim_enabled
        else "Automatic execution-owner claim is disabled."
    )
    return BuyboardReadinessDisplay(
        completed,
        total,
        f"Buy Board readiness {completed}/{total} — {reason}",
        " | ".join(tooltip_parts),
    )


class DatabaseInitWorker(QThread):
    """Use a fast PC connection check, falling back locally only if needed."""

    initialized = pyqtSignal(object, str, object, str)

    def run(self) -> None:
        try:
            # PC schema setup belongs to historical refresh/migration jobs.
            # Dashboard startup needs only a successful connection probe.
            resolution = resolve_data_engine(ensure_pc_schema=False)
            self.initialized.emit(
                resolution.engine, resolution.source, resolution.pc_engine, ""
            )
        except Exception as exc:
            # resolve_data_engine normally returns a "none" resolution on
            # optional-db failures, but the UI must also remain usable if an
            # unexpected driver error escapes it.
            self.initialized.emit(None, "none", None, str(exc))


class CoordinationDatabaseInitWorker(QThread):
    """Connect/provision the tiny Internet coordination store off the UI thread."""

    initialized = pyqtSignal(object, str)

    def run(self) -> None:
        if not coordination_database_configured():
            self.initialized.emit(None, "")
            return
        try:
            engine = init_coordination_engine(ensure_schema=True, raise_on_error=True)
            self.initialized.emit(engine, "")
        except Exception as exc:  # credentials/endpoints must never reach UI logs
            logger.debug(
                "Coordination database initialization failed: %s", type(exc).__name__
            )
            self.initialized.emit(
                None,
                "The configured shared coordination database could not be reached. "
                "Verify its SQL endpoint, TLS CA, username, password, and Internet connection.",
            )


class CoordinationRuntimeHeartbeatWorker(QThread):
    """Publish this local ``main.py`` process to shared coordination."""

    def __init__(self, engine, *, hostname: str, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.hostname = str(hostname or "").strip()

    def run(self) -> None:
        try:
            record_runtime_heartbeat(self.engine, hostname=self.hostname)
        except Exception as exc:  # the independent DB probes own user notices
            logger.debug(
                "Coordination runtime heartbeat failed: %s", type(exc).__name__
            )


@dataclass(frozen=True)
class MarketDataStatusResult:
    """Slow market-cache freshness and watermarks resolved off the GUI thread."""

    engine: object
    latest_daily: object = None
    latest_hourly: object = None
    expected_date: object = None
    daily_is_stale: Optional[bool] = None
    hourly_is_stale: Optional[bool] = None
    error: str = ""


class MarketDataStatusWorker(QThread):
    """Read market-cache watermarks without freezing the dashboard."""

    completed = pyqtSignal(object)

    def __init__(
        self,
        engine,
        tickers: Optional[List[str]] = None,
        hourly_tickers: Optional[List[str]] = None,
        universe_limit: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.tickers = list(tickers or []) or None
        self.hourly_tickers = list(hourly_tickers or []) or None
        self.universe_limit = universe_limit

    def run(self) -> None:
        try:
            from src.infrastructure.database.repositories.market_bars import \
                get_latest_hourly_price_history_timestamp
            from src.infrastructure.database.repositories.market_watermarks import \
                get_latest_price_history_date

            tickers = self.tickers
            if tickers is None:
                tickers = get_default_universe(max_symbols=self.universe_limit)
            hourly_tickers = self.hourly_tickers or tickers
            expected_date = expected_latest_market_data_date()
            result = MarketDataStatusResult(
                engine=self.engine,
                latest_daily=get_latest_price_history_date(self.engine),
                latest_hourly=get_latest_hourly_price_history_timestamp(self.engine),
                expected_date=expected_date,
                daily_is_stale=local_mirror_is_stale(
                    self.engine, expected_date, tickers=tickers
                ),
                hourly_is_stale=local_mirror_hourly_is_stale(
                    self.engine, expected_date, tickers=hourly_tickers
                ),
            )
        except Exception as exc:
            result = MarketDataStatusResult(engine=self.engine, error=str(exc))
        self.completed.emit(result)


@dataclass(frozen=True)
class DatabaseRecoveryOutcome:
    engine: object
    success: bool
    error: str = ""


class DatabaseRecoveryWorker(QThread):
    """Verify PC MySQL connectivity without waiting for the local backup."""

    recovered = pyqtSignal(object, int)

    def __init__(
        self,
        generation: int,
        pc_engine=None,
    ) -> None:
        super().__init__()
        self.generation = int(generation)
        self.pc_engine = pc_engine

    def run(self) -> None:
        from sqlalchemy import text

        from src.infrastructure.database.engine import init_mysql_engine

        engine = self.pc_engine
        try:
            if engine is None:
                engine = init_mysql_engine(
                    log_unavailable=False,
                    ensure_schema=False,
                )
            if engine is None:
                outcome = DatabaseRecoveryOutcome(
                    None, False, error="PC MySQL is no longer reachable."
                )
            elif self.isInterruptionRequested():
                outcome = DatabaseRecoveryOutcome(
                    engine, False, error="Database connection check was interrupted."
                )
            else:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                outcome = DatabaseRecoveryOutcome(engine, True)
        except Exception as exc:
            logger.exception("Runtime MySQL recovery failed unexpectedly")
            outcome = DatabaseRecoveryOutcome(engine, False, error=str(exc))
        self.recovered.emit(outcome, self.generation)


class LocalMirrorSyncWorker(QThread):
    """Best-effort, silent PC -> laptop local-mirror top-up in the background."""

    completed = pyqtSignal(dict, str, bool, int)
    progress = pyqtSignal(str, int, int, int)

    def __init__(
        self,
        pc_engine,
        local_engine,
        hourly_symbols: Optional[List[str]] = None,
        *,
        generation: int = 0,
    ) -> None:
        super().__init__()
        self.pc_engine = pc_engine
        self.local_engine = local_engine
        self.hourly_symbols = (
            None if hourly_symbols is None else list(hourly_symbols)
        )
        self.generation = int(generation)

    def run(self) -> None:
        from src.infrastructure.database.mirror_copy import \
            sync_local_mirror_from_pc_checkpointed
        from src.infrastructure.database.mirror_engine import \
            init_local_mirror_engine

        try:
            if self.local_engine is None:
                self.local_engine = init_local_mirror_engine()
            if self.local_engine is None:
                raise RuntimeError("The local data mirror is unavailable.")
            written = sync_local_mirror_from_pc_checkpointed(
                self.pc_engine,
                self.local_engine,
                hourly_symbols=self.hourly_symbols,
                progress_callback=lambda phase, current, total: self.progress.emit(
                    phase,
                    current,
                    total,
                    self.generation,
                ),
                cancellation_callback=self.isInterruptionRequested,
            )
            self.completed.emit(written, "", False, self.generation)
        except Exception as exc:
            self.completed.emit({}, str(exc), False, self.generation)


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
        # Automatic cross-machine handoff: claims via the fenced
        # claim_main_device_if_stale primitive (re-verifying the expected
        # owner + heartbeat staleness atomically) instead of the plain
        # manual-activation path. See should_auto_claim_main /
        # auto_claim_main_device_if_stale in src/services/app_state.py.
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
            result.live_trading_enabled = control_result.control.enabled
            result.live_trading_revision = control_result.control.revision
        else:
            result.live_trading_error = (
                control_result.error
                or "Could not read shared live-trading control."
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
            from src.services.runtime_device_state_repository import (
                list_runtime_device_states,
            )
            from src.services.operator_commands import list_operator_commands

            result.runtime_devices = list_runtime_device_states(self.engine)
            result.operator_commands = list_operator_commands(
                self.engine, limit=10
            )
        except Exception:
            logger.debug(
                "Could not read runtime device/command status", exc_info=True
            )
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


def _control_runtime_identity_available(
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
    return -5.0 <= age_seconds <= 120.0


def _control_target_role_from_records(
    records, target_label: str
) -> Optional[LocalDeviceRole]:
    candidates = [
        record
        for record in records
        if _control_runtime_identity_available(record)
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

                target = _control_target_role_from_records(
                    list_runtime_device_states(self.engine),
                    self.target_label,
                )
                if target is None:
                    self.completed.emit(
                        ControlOwnerUpdate(
                            control=self.control,
                            success=False,
                            target_label=self.target_label,
                            error=(
                                f"No {self.target_label} runtime is registered in "
                                "shared coordination. Start or restart main.py on "
                                "that device."
                            ),
                        )
                    )
                    return
            if (
                target is not None
                and target.device_id == self.initiated_by.device_id
            ):
                # This worker is executing inside the selected local main.py
                # process, so refresh its proof immediately instead of making
                # a user action race the periodic heartbeat timer. Remote
                # targets must still publish their own independent heartbeat.
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


class MainWindow(
    SidebarMixin,
    HealthPanelMixin,
    DashboardMixin,
    MarketPulseMixin,
    ScannerMixin,
    WatchlistActionsMixin,
    PlanningSupportMixin,
    BuylistMixin,
    ChartCommandRoutingMixin,
    BuyboardMixin,
    ChartsControllerMixin,
    ChartsRenderMixin,
    QMainWindow,
):
    """Main dashboard window."""

    log_message_requested = pyqtSignal(str)
    worker_cleanup_requested = pyqtSignal(object)

    def __init__(self):
        """Initialize the main window."""
        super().__init__()
        # Pure-Python marker (never touches the C++ side) that __init__
        # actually ran -- tests widely use MainWindow.__new__(MainWindow)
        # to build a lightweight double whose Qt base was never
        # constructed; calling most QWidget methods on one of those is
        # undefined behavior in PyQt5, up to and including a
        # process-crashing access violation (not a catchable Python
        # exception) rather than a clean AttributeError/RuntimeError. Any
        # code that touches real Qt widget APIs on `self` outside of a
        # normal event-driven callback should check this first.
        self._qt_base_initialized = True
        # Widget construction must not initiate per-symbol network fallbacks.
        # This marker is cleared only after every synchronous startup refresh
        # has completed and the window is ready to enter the Qt event loop.
        self._window_initializing = True
        # ``append_log`` is also passed to the plain Python thread used for
        # background state saves.  Always cross back through a Qt signal before
        # touching the QTextEdit so that every widget mutation runs on the GUI
        # thread.
        self.log_message_requested.connect(self._append_log_on_ui_thread)
        self.worker_cleanup_requested.connect(self._on_tracked_worker_finished)
        self.setWindowTitle("Stock Dashboard")
        self._apply_global_stylesheet()
        self.setGeometry(100, 100, 1600, 900)

        self.universe_limit = None
        # The full universe can trigger cache/network work on a cold start.
        # ScannerWorker loads it off the GUI thread when it is first needed.
        self.universe_tickers: List[str] = []
        self.scanner = StockScanner()
        self.watchlist = self._load_watchlist()
        self.buylist_manager = self._load_buylist()
        self.trade_manager = self._load_trade_plans()
        self.order_ledger: List[BrokerOrder] = load_order_ledger()
        self.scanner_setups = self._load_scanner_setups()
        self.chart_drawings = self._load_chart_drawings()
        self.tab_options = self._load_tab_options()
        self.settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        orb_settings = configure_orb_settings(self.settings.get("orb_settings"))
        self.settings["orb_settings"] = orb_settings.to_dict()
        if "shortcuts" not in self.settings:
            self.settings["shortcuts"] = DEFAULT_SETTINGS["shortcuts"].copy()
        else:
            for k, v in DEFAULT_SETTINGS["shortcuts"].items():
                if k not in self.settings["shortcuts"]:
                    self.settings["shortcuts"][k] = v
        # MySQL is optional, and establishing a connection must never block the
        # desktop window from appearing.  A short-lived worker finishes setup
        # after the event loop begins.
        self.db_engine = None
        self.pc_db_engine = None
        self.coordination_db_engine = None
        self._pc_probe_engine = None
        self._local_mirror_engine = None
        self.db_engine_source = "none"
        self.db_enabled = False
        self.db_initializing = True
        self.database_init_worker = None
        self.coordination_database_init_worker = None
        self._coordination_runtime_heartbeat_worker = None
        self._last_coordination_runtime_heartbeat_attempt = None
        self.database_recovery_worker = None
        self._coordination_database_configured = coordination_database_configured()
        self._coordination_database_ready = False
        self._coordination_transition_generation = 0
        self._last_coordination_database_notice = ""
        self._pc_database_ready = False
        self._pc_database_coordination_ready = False
        self._last_pc_database_probe_ready = False
        self._main_availability_probe_complete = False
        self._main_availability_probe_database_ready = False
        # Execution state is deliberately independent of the optional
        # daily/hourly/intraday market-data databases.  A small local SQLite
        # store keeps Kanban cards, leases, commands, orders, and the trading
        # control available even when every historical database is offline.
        self.operational_db_engine = init_local_operational_engine()
        self._operational_database_ready = self.operational_db_engine is not None
        self._operational_state_generation = 0
        self._database_transition_generation = 0
        self._database_shutting_down = False
        self._database_reconciliation_in_progress = False
        self._last_database_reconciliation_notice = ""
        self._local_mirror_sync_worker = None
        self._local_mirror_sync_log_completion = True
        self._local_mirror_progress_phase = ""
        self._local_mirror_progress_samples = []
        self._last_pc_main_app_active = None
        self._last_inbound_coordination_event_id = ""
        self._last_outbound_coordination_event_id = ""
        self._remote_coordination_sync_pending = False
        self.state_sync_role = load_local_device_role()
        self.state_sync_worker = None
        self.live_trading_control_worker = None
        self._last_state_sync_notice = ""
        self._initial_state_sync_complete = False
        # Main-device lease fencing: the token this device believes it
        # currently holds (empty when pull-only), refreshed by
        # _on_state_sync_completed whenever a claim/reconcile confirms it,
        # and threaded into every live order submission via
        # _current_execution_lease_kwargs so ExecutionAuthority can re-verify
        # it at the actual broker boundary.
        self._current_lease_token = ""
        self._current_lease_epoch = 0
        self._last_successful_reconcile_at: Optional[dt.datetime] = None
        self._shared_live_trading_available = False
        self._shared_live_trading_revision = 0
        # Broker-boundary checks re-read canonical state. A database outage
        # therefore blocks ordinary commands immediately even if the toolbar
        # still displays the last confirmed state.
        trading_state.set_authoritative_provider(
            self._read_authoritative_live_trading_control
        )

        # Automatic cross-machine handoff (laptop <-> PC). Both env flags
        # default OFF -- only the unattended device (the PC, per the deployed
        # .env) should ever set AUTO_CLAIM_MAIN_ON_HANDOFF, and only after
        # EXPECTED_AUTO_CLAIM_HOSTNAME confirms this is that specific
        # machine, so a copied .env can't silently arm this elsewhere. The
        # laptop deliberately never auto-reclaims on startup. Execution
        # ownership can still be transferred explicitly with the owner controls.
        self._auto_claim_main_enabled = self._handoff_env_flag_true(
            "AUTO_CLAIM_MAIN_ON_HANDOFF"
        ) and self._expected_auto_claim_hostname_matches()
        self.handoff_reconciliation_worker = None
        self._handoff_generation = 0
        self._state_sync_auto_claim = False
        self._last_main_device_hostname = ""
        self._last_handoff_blocked_symbols: Tuple[str, ...] = ()
        self._handoff_reconciliation_required = False
        self._handoff_allow_auto_arm = False
        self.kis_account_snapshots: dict[tuple[str, str], dict] = {}
        # When each kis_account_snapshots entry was actually fetched from
        # KIS (review: "record_snapshot() assigns the current time unless a
        # timestamp is explicitly passed... it can potentially record stale
        # broker data as if it were freshly fetched" -- apply_cached_trade_account_size
        # recomputes display from this cache on many unrelated triggers, not
        # only on an actual new fetch, so it must not stamp buying_power_cache
        # with "now" itself). Set only at the two real fetch-completion sites
        # (_on_kis_snapshot_finished, _on_trade_account_snapshot_finished).
        self.kis_account_snapshot_fetched_at: dict[tuple[str, str], dt.datetime] = {}
        self._kis_api_last_success_at = ""
        self._kis_api_last_error = ""
        self.latest_intraday_prices: dict[str, float] = {}
        self.latest_intraday_sources: dict[tuple[str, str], str] = {}
        self.intraday_fetch_attempts: dict[str, dt.datetime] = {}
        self._cached_market_data_status = None
        self._historical_data_freshness = {
            MODE_1D: "checking",
            MODE_1H: "checking",
        }
        self._historical_data_expected_date = None
        self._historical_data_freshness_error = ""
        self.market_data_status_worker = None
        self.market_pulse_service = MarketPulseService()
        self.market_pulse_worker = None
        self.intraday_bulk_purpose = "buyboard_orb"
        self.scanner_results: List[dict] = []
        self.scanner_results_by_setup: dict[str, List[dict]] = {}
        self.scanner_funnel_counts_by_setup: dict[str, dict] = {}
        self.scanner_dataframe = pd.DataFrame()
        self.selected_scan_symbol: Optional[str] = None
        self.chart_view_windows: dict[str, dict] = {}
        # Deferred-refresh flags: chart edits (breakout price, drawings) used to
        # eagerly rebuild the watchlist table / dashboard summary / other chart
        # tabs even while those tabs weren't visible. These flags let that work
        # be skipped and picked up once the user actually switches to the tab
        # (see on_tab_changed / flush_stale_chart_views), instead of paying the
        # cost synchronously in the middle of an unrelated chart interaction.
        self._intraday_tab_chart_stale = False
        self._tradingview_tab_chart_stale = False
        self.running_scanner_setup_name: Optional[str] = None
        self.running_scanner_show_warnings = True
        self.scanner_worker = None
        self.kis_order_worker = None
        self._refresh_last_finished_at: Dict[str, Optional[str]] = {}
        self._refresh_last_log_count: Dict[str, int] = {}
        self._refresh_active_run_id: Dict[str, Optional[str]] = {}
        self._refresh_completion_display_tokens: Dict[str, tuple] = {}
        self._pending_local_mirror_hourly_refresh = False
        self._run_scanners_after_local_mirror_refresh = False
        self.kis_account_worker = None
        self.kis_startup_worker = None
        self.order_reconciliation_worker = None
        self._pending_reconciliation_groups: List[Tuple[str, str]] = []
        self._last_order_reconciliation_at = ""
        self._last_order_reconciliation_error = ""
        self._health_probe_worker = None
        self._repository_status_worker = None
        self._repository_status = None
        self.kis_retry_timer = None
        self.fx_rate_worker = None
        self._tracked_workers: dict[QThread, tuple[str, Optional[str]]] = {}
        self.usd_krw_rate_source = ""
        self.intraday_fetch_worker = None
        self.intraday_bulk_worker = None
        self._intraday_provider_warning_log_keys: set[str] = set()
        self.current_tradingview_symbol = ""
        self.tradingview_refresh_timestamps: dict[str, dt.datetime] = {}
        self.kis_daily_chart_unavailable_until: Optional[dt.datetime] = None
        self.kis_daily_chart_unavailable_key: str = ""
        self.kis_daily_chart_last_error: str = ""
        self.state_save_manager = get_state_save_manager()
        self.state_save_manager.metadata_file = (
            self._execution_state_metadata_path()
        )
        self.state_save_manager.set_engine(
            self._execution_state_engine(),
            device_id=self.state_sync_role.device_id,
            # A persisted local role is only a hint until this launch has
            # re-read the durable lease and reconciled KIS state.
            is_main_device=False,
        )
        self._init_controllers()

        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Create main layout
        layout = QVBoxLayout()

        # Create tab widget for different views
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Add tabs
        self._setup_tabs()
        self._build_stock_sidebar()
        self.tabs.currentChanged.connect(self.on_tab_changed)

        # Create bottom status widgets
        self._build_status_log(layout)
        self._apply_unresolved_order_startup_state()

        central_widget.setLayout(layout)

        # Create menu bar
        self._create_menu_bar()
        # Recover historical.py refresh state (e.g. main.py was restarted mid-refresh)
        # before the window is shown, then keep polling it live for the rest of the session.
        # Seed the "already seen" terminal-event marker from whatever's on disk so a
        # completed/error/terminated status from a *previous* session doesn't pop a
        # dialog every time the app opens -- only genuinely new events do that.
        for _mode in (MODE_1D, MODE_1H):
            reconcile_stale_status(_mode)
            _existing_status = read_status(_mode)
            self._refresh_last_finished_at[_mode] = _existing_status.get("finished_at")
        self._refresh_poll_timer = QTimer(self)
        self._refresh_poll_timer.setInterval(2000)
        self._refresh_poll_timer.timeout.connect(self._poll_refresh_status)
        self._refresh_poll_timer.start()
        self._poll_refresh_status()

        self.update_dashboard_summary()
        self.on_tab_changed()
        QTimer.singleShot(0, self._start_optional_database_initialization)
        QTimer.singleShot(250, self._start_state_sync)
        QTimer.singleShot(1500, self.preload_kis_accounts_on_startup)
        QTimer.singleShot(2500, lambda: self.refresh_usd_krw_rate(show_messages=False))
        QTimer.singleShot(4000, self.reconcile_open_orders)
        self._apply_shortcuts()
        self._window_initializing = False

    def _init_controllers(self) -> None:
        """Initialize non-rendering workflow controllers."""
        self.buylist_execution_controller = BuylistExecutionController(self)
        self.buylist_controller = BuylistController(self)
        self.scanner_controller = ScannerController(self)
        self.chart_data_controller = ChartDataController(self)
        self.account_controller = AccountController(self)

    def _execution_state_engine(self):
        """Return the one shared Kanban coordination store.

        A configured Internet coordination database is preferred. The PC
        database remains an optional historical-data source and the legacy
        coordination fallback when no dedicated store is configured.
        Watchlist/Buylist/Buy Today membership and main-device ownership
        must stay synced across the laptop and PC -- exactly one device may
        ever be Main, and that is only knowable through the store they both
        share (`state_sync.py`'s `__main_device__` row lives here). This is
        independent of historical daily/hourly/intraday market data, which
        is a separate optional cache -- a stale/unreachable *scanner* feed
        must never block execution, but losing the *shared coordination*
        channel must, or two disconnected devices can both believe they are
        Main at once. The local Kanban SQLite store is therefore recovery
        material only in a real application window. Loss of shared MySQL
        makes execution coordination fail closed.

        Lightweight unit-test windows created without a ``pc_db_engine``
        attribute retain the historical local-engine seam.
        """

        if (
            "pc_db_engine" not in self.__dict__
            or (
                not self.__dict__.get("_qt_base_initialized", False)
                and self.__dict__.get("pc_db_engine") is None
            )
        ):
            return self.__dict__.get("operational_db_engine")
        if self.__dict__.get("_coordination_database_configured", False):
            coordination_engine = self.__dict__.get("coordination_db_engine")
            if coordination_engine is not None and self.__dict__.get(
                "_coordination_database_ready", False
            ):
                return coordination_engine
            return None
        pc_engine = self.__dict__.get("pc_db_engine")
        if pc_engine is not None and self.__dict__.get(
            "_pc_database_ready", pc_engine is not None
        ):
            return pc_engine
        return None

    def _execution_state_ready(self) -> bool:
        engine = self._execution_state_engine()
        if engine is None:
            return False
        local_engine = self.__dict__.get("operational_db_engine")
        if engine is local_engine:
            return bool(self.__dict__.get("_operational_database_ready", True))
        if engine is self.__dict__.get("coordination_db_engine"):
            return bool(self.__dict__.get("_coordination_database_ready", False))
        return bool(
            self.__dict__.get("_pc_database_ready", engine is not None)
        )

    def _using_local_operational_authority(self) -> bool:
        engine = self._execution_state_engine()
        return bool(
            engine is not None
            and engine is self.__dict__.get("operational_db_engine")
        )

    def _execution_state_generation(self) -> int:
        """Return the generation for the current coordination-store route."""

        if self._using_local_operational_authority():
            key = "_operational_state_generation"
        elif self.__dict__.get("_coordination_database_configured", False):
            key = "_coordination_transition_generation"
        else:
            key = "_database_transition_generation"
        return int(self.__dict__.get(key, 0) or 0)

    def _execution_state_metadata_path(self):
        from src.services import app_state

        return (
            app_state.KANBAN_STATE_METADATA_FILE
            if self._using_local_operational_authority()
            else None
        )

    def _start_optional_database_initialization(self) -> None:
        """Begin optional database setup after widgets have been rendered."""
        if self.__dict__.get("_database_shutting_down", False):
            return
        self._start_coordination_database_initialization()
        if self.database_init_worker is not None and self.database_init_worker.isRunning():
            return
        worker = DatabaseInitWorker()
        self.database_init_worker = worker
        worker.initialized.connect(self._on_optional_database_initialized)
        self._track_worker("database_init_worker", worker)
        worker.start()

    def _start_coordination_database_initialization(self) -> None:
        if self.__dict__.get("_database_shutting_down", False):
            return
        if self.__dict__.get("_coordination_database_ready", False):
            return
        worker = self.__dict__.get("coordination_database_init_worker")
        if worker is not None:
            return
        worker = CoordinationDatabaseInitWorker()
        self.coordination_database_init_worker = worker
        worker.initialized.connect(self._on_coordination_database_initialized)
        self._track_worker("coordination_database_init_worker", worker)
        worker.start()

    def _on_coordination_database_initialized(self, engine, error: str = "") -> None:
        if self.__dict__.get("_database_shutting_down", False):
            if engine is not None:
                engine.dispose()
            return
        configured = bool(
            self.__dict__.get("_coordination_database_configured", False)
        )
        self.coordination_db_engine = engine
        self._coordination_database_ready = bool(configured and engine is not None)
        self._coordination_transition_generation = int(
            self.__dict__.get("_coordination_transition_generation", 0) or 0
        ) + 1
        if not configured:
            return
        if engine is None:
            self._pc_database_coordination_ready = False
            self._shared_live_trading_available = False
            self._initial_state_sync_complete = False
            self._bind_remote_state_engine(None, is_main_device=False)
            notice = error or "Shared coordination database is unavailable."
            if notice != self.__dict__.get(
                "_last_coordination_database_notice", ""
            ):
                self.append_log(notice)
            self._last_coordination_database_notice = notice
            return
        self._bind_remote_state_engine(engine, is_main_device=False)
        self._start_coordination_runtime_heartbeat(force=True)
        if self.__dict__.get("_last_coordination_database_notice"):
            self.append_log("Shared online coordination database recovered.")
        else:
            self.append_log(
                "Shared online coordination database connected. Execution ownership "
                "is independent of whether the PC historical database is online."
            )
        self.append_log(
            "TiDB RU profile active: "
            f"{execution_config.COORDINATION_RU_PROFILE} "
            "(local changes are dirty-event driven; "
            f"remote fallback {execution_config.COORDINATION_REMOTE_FALLBACK_SECONDS:g}s; "
            f"external pulse {execution_config.EXTERNAL_WATCHDOG_HEARTBEAT_SECONDS:g}s)."
        )
        from src.services.coordination_change_pulse import (
            read_inbound_change_pulse,
        )

        # Startup reconciliation below already absorbs anything that happened
        # before this process connected. Seed the file cursor so only later
        # listener notifications trigger another database pass.
        self._last_inbound_coordination_event_id = read_inbound_change_pulse()
        self._last_coordination_database_notice = ""
        self._start_state_sync()
        self._sync_buyboard_runtime_worker()

    def _start_coordination_runtime_heartbeat(self, *, force: bool = False) -> None:
        """Refresh the legacy process heartbeat when no runtime pulse exists."""

        if self.__dict__.get("_database_shutting_down", False):
            return
        if not self.__dict__.get("_coordination_database_ready", False):
            return
        engine = self.__dict__.get("coordination_db_engine")
        role = self.__dict__.get("state_sync_role")
        if engine is None or role is None:
            return
        runtime = self.__dict__.get("_buyboard_runtime_worker")
        if (
            runtime is not None
            and self._background_worker_running(runtime)
            and str(getattr(runtime, "_device_id", "") or "") == role.device_id
        ):
            # runtime_device_state is the canonical cross-device liveness row.
            # Keep app_runtime_status only as a compatibility fallback when
            # the guarded runtime is not running; do not pay for two TiDB
            # heartbeat UPDATEs from the same process.
            return
        worker = self.__dict__.get("_coordination_runtime_heartbeat_worker")
        if worker is not None:
            return
        now = time.monotonic()
        last_attempt = self.__dict__.get(
            "_last_coordination_runtime_heartbeat_attempt"
        )
        cadence = float(execution_config.COORDINATION_DEVICE_HEARTBEAT_SECONDS)
        if (
            not force
            and last_attempt is not None
            and now - float(last_attempt) < cadence
        ):
            return
        self._last_coordination_runtime_heartbeat_attempt = now
        worker = CoordinationRuntimeHeartbeatWorker(
            engine,
            hostname=role.hostname,
            parent=self,
        )
        self._coordination_runtime_heartbeat_worker = worker
        self._track_worker("_coordination_runtime_heartbeat_worker", worker)
        worker.start()

    def _on_optional_database_initialized(
        self, engine, source: str = "none", pc_engine=None, error: str = ""
    ) -> None:
        if self.__dict__.get("_database_shutting_down", False):
            disposed = set()
            for candidate in (engine, pc_engine):
                if candidate is None or id(candidate) in disposed:
                    continue
                disposed.add(id(candidate))
                try:
                    candidate.dispose()
                except Exception:
                    pass
            return
        self.db_engine = engine
        self.pc_db_engine = pc_engine
        self._pc_probe_engine = pc_engine
        self._local_mirror_engine = (
            engine
            if source == "local_mirror"
            else self.__dict__.get("_local_mirror_engine")
        )
        self.db_engine_source = source
        self.db_enabled = engine is not None
        self.db_initializing = False
        market_pulse_service = self.__dict__.get("market_pulse_service")
        if market_pulse_service is not None:
            market_pulse_service.set_engine(engine)
        self._pc_database_ready = bool(source == "pc" and pc_engine is not None)
        if (
            not self._using_local_operational_authority()
            and not self.__dict__.get("_coordination_database_configured", False)
        ):
            self._pc_database_coordination_ready = False
        self._last_pc_database_probe_ready = self._pc_database_ready
        self._main_availability_probe_complete = True
        self._main_availability_probe_database_ready = self._pc_database_ready
        self._bind_remote_state_engine(
            self._execution_state_engine(),
            is_main_device=bool(
                self.state_sync_role.is_main
                and self._using_local_operational_authority()
            ),
        )
        self._update_database_source_indicator()

        if source == "pc":
            self.append_log(
                "PC MySQL connection verified; using it immediately. "
                "The laptop safety backup will update in the background."
            )
            self._start_state_sync()
            self.run_all_scanners(show_warnings=False)
            self._start_background_local_mirror_sync(pc_engine)
        elif source == "local_mirror":
            self._handle_local_mirror_startup(engine)
        elif error:
            self.append_log(f"MySQL cache unavailable: {error}")
        else:
            self.append_log(
                "MySQL cache is unavailable; scanner and cached intraday features remain disabled."
            )
        self._start_market_data_status_refresh()
        self.update_dashboard_summary()
        # The startup resolver already performed the first PC database probe.
        # Start service polling only after that probe has completed so startup
        # never launches duplicate connection attempts in separate QThreads.
        self._poll_pc_status()

    def _start_market_data_status_refresh(self, *, force: bool = False) -> None:
        """Verify 1D/1H freshness on a worker, never on the Qt thread."""
        if not self.db_enabled or self.db_engine is None:
            self._cached_market_data_status = None
            self._historical_data_freshness = {
                MODE_1D: "unavailable",
                MODE_1H: "unavailable",
            }
            self._historical_data_expected_date = None
            self._historical_data_freshness_error = (
                "The market-data database is unavailable."
            )
            self._poll_refresh_status()
            return
        if not self.__dict__.get("_qt_base_initialized", False):
            # Lightweight unit-test doubles deliberately bypass QObject init.
            return
        worker = self.__dict__.get("market_data_status_worker")
        if (
            worker is not None
            and worker.isRunning()
            and getattr(worker, "engine", None) is self.db_engine
            and not force
        ):
            return
        self._cached_market_data_status = "Checking..."
        self._historical_data_freshness = {
            MODE_1D: "checking",
            MODE_1H: "checking",
        }
        self._historical_data_freshness_error = ""
        self._poll_refresh_status()
        worker = MarketDataStatusWorker(
            self.db_engine,
            tickers=list(self.__dict__.get("universe_tickers") or []) or None,
            hourly_tickers=(
                self._relevant_hourly_symbols()
                if self.__dict__.get("db_engine_source") == "local_mirror"
                else None
            ),
            universe_limit=self.__dict__.get("universe_limit"),
        )
        self.market_data_status_worker = worker
        worker.completed.connect(self._on_market_data_status_completed)
        self._track_worker("market_data_status_worker", worker)
        worker.start()

    @pyqtSlot(object)
    def _on_market_data_status_completed(self, result: MarketDataStatusResult) -> None:
        if (
            self.__dict__.get("_database_shutting_down", False)
            or result.engine is not self.__dict__.get("db_engine")
        ):
            return
        if result.error:
            self._cached_market_data_status = "Unavailable"
            self._historical_data_freshness = {
                MODE_1D: "unavailable",
                MODE_1H: "unavailable",
            }
            self._historical_data_expected_date = None
            self._historical_data_freshness_error = result.error
        elif result.latest_daily is None:
            self._cached_market_data_status = "No cached data"
        else:
            daily_status = self._format_market_data_status_from_date(
                result.latest_daily
            )
            if result.latest_hourly is None:
                self._cached_market_data_status = (
                    f"Daily {daily_status}; 1H no cached data"
                )
            else:
                hourly_text = pd.Timestamp(result.latest_hourly).strftime(
                    "%Y-%m-%d %H:%M"
                )
                self._cached_market_data_status = (
                    f"Daily {daily_status}; 1H latest {hourly_text} UTC"
                )
        if not result.error:
            def freshness_state(value: Optional[bool]) -> str:
                if value is True:
                    return "stale"
                if value is False:
                    return "fresh"
                return "unavailable"

            self._historical_data_expected_date = result.expected_date
            self._historical_data_freshness = {
                MODE_1D: freshness_state(result.daily_is_stale),
                MODE_1H: freshness_state(result.hourly_is_stale),
            }
            self._historical_data_freshness_error = ""
        self._poll_refresh_status()
        self.update_dashboard_summary()

    def _sync_active_pc_to_local_mirror(self) -> None:
        """Periodically preserve the active PC database for sudden failover."""
        if (
            self.__dict__.get("db_engine_source", "none") != "pc"
            or not self.__dict__.get("_pc_database_ready", False)
        ):
            return
        try:
            if any(is_refresh_running(mode)[0] for mode in (MODE_1D, MODE_1H)):
                return
        except Exception:
            # Exact content/causality fences in the worker remain authoritative
            # if the optional refresh-status file cannot be read.
            pass
        self._start_background_local_mirror_sync(
            self.__dict__.get("pc_db_engine"), log_completion=False
        )

    def _relevant_hourly_symbols(self) -> List[str]:
        """Return symbols whose full 1-hour history is useful on the laptop."""
        symbols = {REFERENCE_SYMBOL}

        def add_symbol(value) -> None:
            if isinstance(value, dict):
                value = value.get("symbol")
            else:
                value = getattr(value, "symbol", value)
            symbol = str(value or "").strip().upper()
            if symbol:
                symbols.add(symbol)

        watchlist = self.__dict__.get("watchlist")
        for item in getattr(watchlist, "items", []) or []:
            add_symbol(item)
        buylist = self.__dict__.get("buylist_manager")
        for item in getattr(buylist, "items", []) or []:
            add_symbol(item)
        for result in self.__dict__.get("scanner_results", []) or []:
            add_symbol(result)
        for results in (
            self.__dict__.get("scanner_results_by_setup", {}) or {}
        ).values():
            for result in results or []:
                add_symbol(result)

        return [REFERENCE_SYMBOL, *sorted(symbols - {REFERENCE_SYMBOL})]

    def _start_background_local_mirror_sync(
        self, pc_engine, *, log_completion: bool = True
    ) -> None:
        """Start a disposable PC-authoritative laptop backup off the UI thread."""
        from src.infrastructure.database.mirror_engine import \
            _local_mirror_enabled

        if self.__dict__.get("_database_shutting_down", False):
            return
        if not _local_mirror_enabled():
            return
        if (
            pc_engine is None
            or self.__dict__.get("_local_mirror_sync_worker") is not None
        ):
            return
        local_engine = self.__dict__.get("_local_mirror_engine")
        worker = LocalMirrorSyncWorker(
            pc_engine,
            local_engine,
            hourly_symbols=self._relevant_hourly_symbols(),
            generation=self._execution_state_generation(),
        )
        self._local_mirror_sync_worker = worker
        self._local_mirror_sync_log_completion = bool(log_completion)
        worker.progress.connect(self._on_local_mirror_sync_progress)
        worker.completed.connect(self._on_local_mirror_sync_completed)
        self._track_worker("_local_mirror_sync_worker", worker)
        self._local_mirror_progress_phase = "Preparing laptop safety backup"
        self._local_mirror_progress_samples = []
        progress_bar = self.__dict__.get("progress_bar")
        progress_label = self.__dict__.get("progress_label")
        if progress_bar is not None and progress_label is not None:
            progress_bar.setRange(0, 0)
            progress_label.setText(
                "Laptop backup — preparing the laptop safety copy..."
            )
            progress_label.setToolTip(
                "This copies PC market data to the laptop for offline use. "
                "The dashboard continues using the PC database while it runs."
            )
        if log_completion:
            self.append_log(
                "Laptop safety backup started in the background "
                "(PC data remains active)."
            )
        worker.start()

    @staticmethod
    def _friendly_local_mirror_phase(phase: str) -> str:
        table_labels = {
            "price_history": "daily prices",
            "hourly_price_history": "1-hour prices",
            "chart_indicators": "chart indicators",
            "chart_indicator_manifests": "chart status",
            "scanner_metrics": "scanner data",
            "scanner_metric_snapshots": "scanner status",
            "symbol_refresh_failures": "refresh status",
        }
        exact_labels = {
            "Preparing laptop safety backup": "Preparing the laptop safety copy",
            "Checking laptop backup checkpoint": "Checking whether the laptop copy is current",
            "Checking PC derived-data freshness": "Checking PC scanner data",
            "Starting record comparison": "Starting the record check",
            "Finalizing laptop safety backup": "Saving the laptop safety copy",
            "Laptop safety backup already up to date": "Laptop safety copy is already up to date",
            "Laptop safety backup complete": "Laptop safety copy complete",
        }
        if phase in exact_labels:
            return exact_labels[phase]
        phase_prefixes = {
            "Checking table layout": "Checking backup setup for {table}",
            "Counting backup records": "Counting {table}",
            "Checking PC changes": "Checking PC {table} for changes",
            "Checking laptop changes": "Checking laptop {table}",
            "Counting changed PC rows": "Counting changed PC {table}",
            "Copying changed PC data": "Copying changed {table} to laptop",
            "Verifying incremental backup": "Verifying changed {table}",
            "Finding changed partitions": "Locating changed {table}",
            "Reconciling changed partitions": "Repairing changed {table}",
            "Confirming PC checkpoint": "Final PC check for {table}",
            "Reading PC data": "Checking PC {table}",
            "Rechecking PC data": "Rechecking PC {table}",
            "Reading laptop backup": "Checking laptop {table}",
            "Updating laptop backup": "Copying changed {table} to laptop",
            "Verifying laptop backup": "Verifying laptop {table}",
            "Confirming PC unchanged": "Final PC check for {table}",
        }
        for prefix, template in phase_prefixes.items():
            marker = f"{prefix}: "
            if phase.startswith(marker):
                table_name = phase[len(marker):]
                return template.format(
                    table=table_labels.get(table_name, table_name.replace("_", " "))
                )
        return str(phase or "Working")

    @staticmethod
    def _format_local_mirror_eta(seconds: float) -> str:
        safe_seconds = max(0.0, float(seconds))
        if safe_seconds < 45:
            return "less than 1 min"
        if safe_seconds < 90:
            return "about 1 min"
        if safe_seconds < 3600:
            return f"about {math.ceil(safe_seconds / 60):d} min"
        hours = int(safe_seconds // 3600)
        minutes = int(math.ceil((safe_seconds % 3600) / 60))
        if minutes >= 60:
            hours += 1
            minutes = 0
        if minutes == 0:
            return f"about {hours:d} hr"
        return f"about {hours:d} hr {minutes:d} min"

    def _local_mirror_eta(self, current: int, total: int) -> str:
        now = time.monotonic()
        samples = list(self.__dict__.get("_local_mirror_progress_samples", []))
        if samples and (samples[-1][2] != total or current < samples[-1][1]):
            samples = []
        if not samples or current != samples[-1][1]:
            samples.append((now, current, total))
        cutoff = now - 120.0
        while len(samples) > 2 and samples[1][0] < cutoff:
            samples.pop(0)
        if len(samples) > 120:
            samples = samples[-120:]
        self._local_mirror_progress_samples = samples
        if current >= total:
            return "finishing"
        if len(samples) < 2:
            return "calculating"
        elapsed = samples[-1][0] - samples[0][0]
        completed = samples[-1][1] - samples[0][1]
        if elapsed < 2.0 or completed <= 0:
            return "calculating"
        rows_per_second = completed / elapsed
        remaining_seconds = (total - current) / rows_per_second
        return self._format_local_mirror_eta(remaining_seconds)

    def _on_local_mirror_sync_progress(
        self,
        phase: str,
        current: int,
        total: int,
        generation: int,
    ) -> None:
        if (
            self.__dict__.get("_database_shutting_down", False)
            or generation
            != self.__dict__.get("_database_transition_generation", 0)
        ):
            return
        friendly_phase = self._friendly_local_mirror_phase(str(phase or "Working"))
        self._local_mirror_progress_phase = friendly_phase
        progress_bar = self.__dict__.get("progress_bar")
        progress_label = self.__dict__.get("progress_label")
        if progress_bar is None or progress_label is None:
            return
        if int(total or 0) <= 0:
            progress_bar.setRange(0, 0)
            progress_label.setText(
                f"Laptop backup — {friendly_phase} | counting records..."
            )
            progress_label.setToolTip(
                "This is a PC-to-laptop safety copy for offline use. "
                "The dashboard is already using PC MySQL. "
                "An ETA appears as soon as the record count is known."
            )
            return
        safe_total = max(1, int(total))
        safe_current = max(0, min(int(current or 0), safe_total))
        percent = int((safe_current * 100) / safe_total)
        progress_bar.setRange(0, safe_total)
        progress_bar.setValue(safe_current)
        eta = self._local_mirror_eta(safe_current, safe_total)
        progress_label.setText(
            f"Laptop backup — {friendly_phase} | "
            f"{safe_current:,} / {safe_total:,} records ({percent}%) | ETA {eta}"
        )
        progress_label.setToolTip(
            "This checks and copies PC market data to the laptop for offline use. "
            "The count includes the comparison and verification passes. "
            "The dashboard is already using PC MySQL."
        )

    def _on_local_mirror_sync_completed(
        self,
        written: dict,
        error: str,
        needs_reconciliation: bool = False,
        generation: Optional[int] = None,
    ) -> None:
        worker = self.__dict__.get("_local_mirror_sync_worker")
        worker_local_engine = getattr(worker, "local_engine", None)
        if worker_local_engine is not None:
            self._local_mirror_engine = worker_local_engine
        if self.__dict__.get("_database_shutting_down", False):
            return
        if (
            generation is not None
            and generation
            != self.__dict__.get("_database_transition_generation", 0)
        ):
            return
        total = sum(written.values())
        progress_bar = self.__dict__.get("progress_bar")
        progress_label = self.__dict__.get("progress_label")
        if error:
            self._local_mirror_progress_phase = "incomplete"
            self._local_mirror_progress_samples = []
            if progress_bar is not None and progress_label is not None:
                progress_bar.setRange(0, 100)
                progress_bar.setValue(0)
                progress_label.setText("Laptop backup incomplete; will retry.")
                progress_label.setToolTip(str(error))
            self.append_log(
                f"Local data mirror sync incomplete ({total} row(s) written): {error}"
            )
            return
        self._local_mirror_progress_phase = "complete"
        self._local_mirror_progress_samples = []
        if progress_bar is not None and progress_label is not None:
            progress_bar.setRange(0, 100)
            progress_bar.setValue(100)
            if total:
                progress_label.setText(
                    f"Laptop backup complete ({total} row update(s) applied)."
                )
            else:
                progress_label.setText("Laptop backup already up to date.")
            progress_label.setToolTip(
                "PC to laptop safety backup completed successfully."
            )
            QTimer.singleShot(5000, self._clear_local_mirror_progress_if_finished)
        if total and self.__dict__.get("_local_mirror_sync_log_completion", True):
            self.append_log(f"Local data mirror updated ({total} row(s)) for offline fallback.")

    def _clear_local_mirror_progress_if_finished(self) -> None:
        if self.__dict__.get("_local_mirror_progress_phase") != "complete":
            return
        progress_label = self.__dict__.get("progress_label")
        progress_bar = self.__dict__.get("progress_bar")
        if (
            progress_label is None
            or progress_bar is None
            or not progress_label.text().startswith("Laptop backup complete")
        ):
            return
        self._local_mirror_progress_phase = ""
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        progress_label.setText("Ready.")
        progress_label.setToolTip("")

    def _handle_local_mirror_startup(self, engine) -> None:
        """PC unreachable; decide whether to use the local mirror silently or ask first."""
        try:
            self._handle_local_mirror_startup_inner(engine)
        except Exception:
            # This runs synchronously from a Qt slot invoked across a QThread
            # signal boundary -- an uncaught exception here can silently kill
            # the whole app (no dialog, no traceback) instead of just failing
            # this one startup step. Log it and keep the window usable.
            logger.exception("Local mirror startup handling failed")
            self.append_log(
                "Local data mirror startup check failed; continuing without it "
                "(see quant_app.log for details)."
            )

    def _handle_local_mirror_startup_inner(self, engine) -> None:
        expected_date = expected_latest_market_data_date()
        # Restrict staleness to the currently tracked universe. A symbol
        # dropped from the S&P 500/KIS list (delisted, ticker change, etc.)
        # stops being refreshed by historical.py and never accumulates
        # chronic-failure attempts either -- its old, permanently-lagging
        # price_history rows would otherwise flag the entire mirror stale
        # forever even when every actively tracked symbol is current.
        try:
            tickers = get_default_universe()
        except Exception:
            logger.exception("Could not load default universe for staleness check")
            tickers = None
        daily_is_stale = local_mirror_is_stale(
            engine, expected_date, tickers=tickers
        )
        hourly_tickers = MainWindow._relevant_hourly_symbols(self)
        hourly_is_stale = local_mirror_hourly_is_stale(
            engine, expected_date, tickers=hourly_tickers
        )
        if not daily_is_stale and not hourly_is_stale:
            self.append_log("PC unreachable; using local data mirror (up to date).")
            self.run_all_scanners(show_warnings=False)
            return

        self.append_log(
            f"PC unreachable and the local data mirror is stale "
            f"(market data expected through {expected_date})."
        )
        reply = QMessageBox.question(
            self,
            "Local data is out of date",
            "The PC is unreachable and this laptop's local data mirror is stale "
            f"(market data expected through {expected_date}).\n\n"
            "Fetch fresh data now directly from Yahoo Finance? This only updates "
            "this laptop's local copy. The routine 1-hour refresh downloads the "
            "rolling D-10 window and usually takes a few minutes.\n\n"
            "Choose No to keep working with the existing (slightly stale) local data.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.append_log("Fetching fresh data into the local mirror ...")
            self._run_scanners_after_local_mirror_refresh = True
            if daily_is_stale:
                # Keep the existing daily-then-hourly sequencing when daily
                # data needs work. The rolling D-10 hourly top-up is cheap and
                # guarantees the laptop refresh completes both histories.
                self._pending_local_mirror_hourly_refresh = True
                daily_running, _ = is_refresh_running(MODE_1D)
                if daily_running:
                    self.append_log(
                        "A 1D refresh is already running; the 1H refresh will start after it completes."
                    )
                elif not self.refresh_data_to_db():
                    self._pending_local_mirror_hourly_refresh = False
                    self._run_scanners_after_local_mirror_refresh = False
                    self.run_all_scanners(show_warnings=False)
            elif hourly_is_stale:
                hourly_running, _ = is_refresh_running(MODE_1H)
                if hourly_running:
                    self.append_log(
                        "A 1H local-mirror refresh is already running; waiting for it to complete."
                    )
                elif not self.refresh_hourly_data_to_db():
                    self._run_scanners_after_local_mirror_refresh = False
                    self.run_all_scanners(show_warnings=False)
        else:
            self.append_log(
                "Continuing with stale local data. Use 'Update 1D/1H Data' to refresh manually."
            )
            self.run_all_scanners(show_warnings=False)

    def _start_state_sync(
        self,
        *,
        activate: bool = False,
        auto_claim: bool = False,
        expected_owner_device_id: str = "",
        expected_standby_generation: int = 0,
        allow_local_bootstrap: bool = False,
    ) -> None:
        """Reconcile Kanban state without consulting historical-data storage."""
        if self.__dict__.get("_database_shutting_down", False):
            return
        execution_engine = self._execution_state_engine()
        if execution_engine is None or not self._execution_state_ready():
            return
        worker = self.state_sync_worker
        if worker is not None and worker.isRunning():
            if activate:
                QMessageBox.information(
                    self,
                    "State sync busy",
                    "Wait for the current state synchronization to finish, then try again.",
                )
            return
        from src.core.execution_config import is_buyboard_engine_enabled

        runtime_claim_required = bool(is_buyboard_engine_enabled())
        expected_standby_generation = int(expected_standby_generation or 0)
        if (
            runtime_claim_required
            and (activate or auto_claim)
            and expected_standby_generation <= 0
            and not (
                allow_local_bootstrap
                and self._using_local_operational_authority()
            )
        ):
            self.append_log(
                "Execution-owner claim deferred: this device has not published a "
                "fresh STANDBY_READY generation after final reconciliation."
            )
            return
        if activate or auto_claim:
            self._handoff_reconciliation_required = True
            self._handoff_allow_auto_arm = bool(auto_claim)
        worker = StateSyncWorker(
            execution_engine,
            self.state_sync_role,
            self._ensure_save_lock(),
            activate=activate or auto_claim,
            ownership_only_when_main=(
                not activate
                and not auto_claim
                and self.state_sync_role.is_main
                and self._initial_state_sync_complete
            ),
            generation=self._execution_state_generation(),
            auto_claim=auto_claim,
            expected_owner_device_id=expected_owner_device_id,
            expected_standby_generation=expected_standby_generation,
            require_runtime_ready_claim=(
                runtime_claim_required
                and not (
                    allow_local_bootstrap
                    and self._using_local_operational_authority()
                )
            ),
            metadata_path=self._execution_state_metadata_path(),
        )
        self.state_sync_worker = worker
        self._state_sync_action = "activate" if (activate or auto_claim) else "reconcile"
        self._state_sync_auto_claim = auto_claim
        worker.completed.connect(self._on_state_sync_completed)
        self._track_worker("state_sync_worker", worker)
        worker.start()

    def _sync_state_with_remote(self) -> None:
        """Compatibility wrapper for callers requesting an immediate sync."""
        self._start_state_sync()

    def _on_state_sync_completed(
        self, result: StateReconcileResult, generation: int
    ) -> None:
        if self.__dict__.get("_database_shutting_down", False):
            return
        if generation != self._execution_state_generation():
            return
        if not self._execution_state_ready():
            return
        execution_engine = self._execution_state_engine()
        live_trading_error = str(
            getattr(result, "live_trading_error", "") or ""
        )
        live_trading_enabled = getattr(result, "live_trading_enabled", None)
        if not live_trading_error and live_trading_enabled is not None:
            trading_state.set_trading_enabled(bool(live_trading_enabled))
            self._shared_live_trading_available = True
            self._shared_live_trading_revision = int(
                getattr(result, "live_trading_revision", 0) or 0
            )
            live_notice = ""
        else:
            self._shared_live_trading_available = False
            live_notice = live_trading_error or (
                "Shared live-trading control did not return a value."
            )
        if live_notice != self.__dict__.get("_last_live_trading_notice", ""):
            if live_notice:
                self.append_log(
                    "Live-trading control unavailable; ordinary broker "
                    f"mutations fail closed: {live_notice}"
                )
            elif self.__dict__.get("_last_live_trading_notice", ""):
                self.append_log(
                    "Shared live-trading control is reachable again."
                )
        self._last_live_trading_notice = live_notice
        if self.__dict__.get("trading_enabled_button") is not None:
            self._refresh_trading_enabled_widget()
        previous_main = bool(self.state_sync_role.is_main)
        if result.local_role is not None:
            self.state_sync_role = result.local_role
        else:
            self.state_sync_role = LocalDeviceRole(
                self.state_sync_role.device_id,
                self.state_sync_role.hostname,
                result.is_main_device,
            )
        if not result.errors:
            self._initial_state_sync_complete = True
            self._pc_database_coordination_ready = True
            self._last_successful_reconcile_at = dt.datetime.now(dt.timezone.utc)
            self._bind_remote_state_engine(
                execution_engine,
                is_main_device=result.is_main_device,
            )
        else:
            self._pc_database_coordination_ready = False
            self._bind_remote_state_engine(
                execution_engine,
                is_main_device=False,
            )
        self._current_lease_token = result.lease_token if result.is_main_device else ""
        self._current_lease_epoch = (
            int(getattr(result, "lease_epoch", 0) or 0)
            if result.is_main_device
            else 0
        )
        self._sync_buyboard_runtime_worker()
        if result.main_device_hostname:
            self._last_main_device_hostname = result.main_device_hostname
        self._refresh_control_ownership_status(result)

        action = getattr(self, "_state_sync_action", "reconcile")
        was_auto_claim = bool(self._state_sync_auto_claim)
        self._state_sync_auto_claim = False
        if action == "activate" and result.is_main_device:
            if was_auto_claim:
                self.append_log(
                    "Automatic handoff: claimed execution ownership "
                    f"({result.main_device_hostname or platform.node()}); "
                    "reconciling against the broker before resuming monitoring."
                )
            else:
                self.append_log(
                    "This device is now the Execution Owner; the other device is standby/read-only."
                )
        elif previous_main and not result.is_main_device:
            owner = result.main_device_hostname or "another device"
            self.append_log(
                f"Execution ownership moved to {owner}; this device is now standby/read-only."
            )

        updated_keys = set(result.updated_keys)
        if updated_keys:
            self.append_log(
                f"Pulled newer shared state: {', '.join(sorted(updated_keys))}."
            )
        if "watchlist" in updated_keys:
            self.watchlist = self._load_watchlist()
        if "buylist" in updated_keys:
            self.buylist_manager = self._load_buylist()
            self.populate_buylist_dashboard()
        if "trade_plans" in updated_keys:
            self.trade_manager = self._load_trade_plans()
        if "execution_queue" in updated_keys:
            # Lazily reloaded on next access (_ensure_execution_queue_manager
            # caches on self.execution_queue_manager) -- just drop the stale
            # cached instance so the freshly-pulled file wins.
            self.__dict__.pop("execution_queue_manager", None)
            if hasattr(self, "populate_buylist_dashboard"):
                self.populate_buylist_dashboard()
        if updated_keys.intersection({"watchlist", "buylist", "execution_queue"}):
            # Planning state is normalized into canonical trade-card rows by
            # an explicit projection refresh.  Do it on the actual sync event
            # instead of paying for a full board rebuild every few seconds.
            self.refresh_buyboard()
        if updated_keys.intersection({"watchlist", "buylist"}):
            # The tab is retired, but Watchlist/Buylist remain lightweight
            # sidebar sources and must immediately reflect pulled state.
            refresh_planning = getattr(
                self, "_update_watchlist_action_surfaces", None
            )
            if callable(refresh_planning):
                refresh_planning()
            else:
                refresh_sidebar = getattr(self, "refresh_sidebar_sources", None)
                if callable(refresh_sidebar):
                    refresh_sidebar()

        notices = []
        if result.conflict_keys:
            notices.append(
                "Sync conflict preserved local and remote copies for: "
                + ", ".join(sorted(result.conflict_keys))
            )
        if result.errors:
            notices.append("; ".join(result.errors))
        notice = " | ".join(notices)
        if notice and notice != self._last_state_sync_notice:
            self.append_log(notice)
        self._last_state_sync_notice = notice
        self.update_dashboard_summary()

        if action == "activate" and result.is_main_device and not result.errors:
            self._begin_post_claim_handoff(allow_auto_arm=was_auto_claim)
            return
        if action == "activate" and not result.is_main_device:
            self._handoff_reconciliation_required = False
            self._handoff_allow_auto_arm = False
        elif (
            action != "activate"
            and result.is_main_device
            and not result.errors
            and self.__dict__.get("_handoff_reconciliation_required", False)
        ):
            self._begin_post_claim_handoff(
                allow_auto_arm=self.__dict__.get("_handoff_allow_auto_arm", False)
            )
            return

        # Automatic handoff detection: only ever runs on a device explicitly
        # configured for it (PC's .env only, hostname-guarded), only on a
        # plain reconcile tick (never re-entering while an activation is
        # already in flight), and never while this device is already main.
        if (
            action != "activate"
            and self._auto_claim_main_enabled
            and not self._using_local_operational_authority()
            and not result.is_main_device
            and not (self.state_sync_worker and self.state_sync_worker.isRunning())
        ):
            should_claim, expected_owner_device_id, reason = should_auto_claim_main(
                execution_engine,
                self.state_sync_role,
                other_hostname=result.main_device_hostname,
            )
            if should_claim:
                generation = self._runtime_standby_generation_for_claim()
                runtime_claim_required = execution_config.is_buyboard_engine_enabled()
                if not runtime_claim_required or generation > 0:
                    self.append_log(
                        f"Automatic handoff: claiming Execution Owner ({reason})"
                        + (
                            f" from STANDBY_READY generation {generation}."
                            if runtime_claim_required
                            else "."
                        )
                    )
                    claim_kwargs = dict(
                        auto_claim=True,
                        expected_owner_device_id=expected_owner_device_id,
                    )
                    if runtime_claim_required:
                        claim_kwargs["expected_standby_generation"] = generation
                    self._start_state_sync(**claim_kwargs)

    def _runtime_standby_generation_for_claim(self) -> int:
        """Return this device's fresh durable lease-handoff generation, or zero."""

        from src.core.execution_config import is_buyboard_engine_enabled
        from src.core.runtime_readiness import RuntimeDeviceState

        if not is_buyboard_engine_enabled():
            return 0
        worker = self.__dict__.get("_buyboard_runtime_worker")
        role = self.__dict__.get("state_sync_role")
        engine = self._execution_state_engine()
        if worker is None or role is None or engine is None:
            return 0
        try:
            readiness = worker.engine_readiness(include_device_state=False)
            handoff_ready = getattr(worker, "lease_handoff_ready", None)
            if (
                not worker.isRunning()
                or not bool(getattr(worker, "_standby_only", False))
                or getattr(worker, "device_state", None)
                != RuntimeDeviceState.STANDBY_READY
                or not callable(handoff_ready)
                or not handoff_ready(readiness)
            ):
                return 0
            from src.services.runtime_device_state_repository import (
                get_runtime_device_state,
            )

            record = get_runtime_device_state(engine, device_id=role.device_id)
            if record is None or record.state != RuntimeDeviceState.STANDBY_READY:
                return 0
            age = (dt.datetime.now(dt.timezone.utc) - record.updated_at).total_seconds()
            if age < 0.0 or age > 60.0:
                return 0
            return int(record.readiness_generation or 0)
        except Exception:
            logger.exception("Could not verify this device's STANDBY_READY generation")
            return 0

    @staticmethod
    def _background_worker_running(worker) -> bool:
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    def _foreground_progress_running(self) -> bool:
        """Keep user-started work from being overwritten by readiness status."""

        if self.__dict__.get("_refresh_completion_display_tokens"):
            return True

        worker_names = (
            "_local_mirror_sync_worker",
            "scanner_worker",
            "intraday_fetch_worker",
            "intraday_bulk_worker",
        )
        return any(
            self._background_worker_running(self.__dict__.get(name))
            for name in worker_names
        )

    def _current_buyboard_readiness_display(
        self,
    ) -> Optional[BuyboardReadinessDisplay]:
        if not execution_config.is_buyboard_engine_enabled():
            return None

        worker = self.__dict__.get("_buyboard_runtime_worker")
        runtime_active = bool(
            worker is not None
            and self._background_worker_running(worker)
            and getattr(worker, "device_state", RuntimeDeviceState.STARTING)
            == RuntimeDeviceState.ACTIVE
        )
        # ACTIVE is a latched Buy Board state. A separate legacy Buylist
        # handoff/reconciliation may continue in the background, but it does
        # not revoke Buy Board activation and must not replace the stable ready
        # projection with an indeterminate activation message.
        if not runtime_active:
            state_sync_worker = self.__dict__.get("state_sync_worker")
            if (
                self.__dict__.get("_state_sync_action") == "activate"
                and self._background_worker_running(state_sync_worker)
            ):
                return BuyboardReadinessDisplay(
                    8,
                    8,
                    "Buy Board activation — transferring the execution lease (ETA unavailable)",
                    "The lease transaction is in progress and remains revision fenced.",
                    indeterminate=True,
                )

            handoff_worker = self.__dict__.get("handoff_reconciliation_worker")
            if self._background_worker_running(handoff_worker):
                return BuyboardReadinessDisplay(
                    8,
                    8,
                    "Buy Board activation — final broker reconciliation (ETA unavailable)",
                    "KIS account and order truth are being refreshed before execution can become ACTIVE.",
                    indeterminate=True,
                )

        if worker is None or not self._background_worker_running(worker):
            if not self._execution_state_ready():
                kis_ready = bool(
                    self.__dict__.get("kis_account_snapshots", {})
                    and self.__dict__.get("kis_account_snapshot_fetched_at", {})
                )
                return BuyboardReadinessDisplay(
                    0,
                    7,
                    (
                        "Buy Board recovery — KIS holdings/prices monitored; "
                        "execution disabled (Kanban operational store unavailable)"
                        if kis_ready
                        else "Buy Board recovery — connecting to KIS; execution "
                        "disabled (Kanban operational store unavailable)"
                    ),
                    "Historical data is not an execution requirement. The local "
                    "Kanban operational file itself could not be opened, so cards, "
                    "commands, orders, and the device lease cannot be persisted. "
                    "App execution is locked: open Recovery Procedure on the Buy "
                    "Board. Never duplicate an order between this app and KIS.",
                )
            return BuyboardReadinessDisplay(
                0,
                8,
                "Buy Board startup — runtime worker unavailable",
                "The local Kanban store is available; the execution worker is still starting. Historical-data availability is not a gate.",
                indeterminate=True,
            )
        try:
            readiness = worker.engine_readiness(include_device_state=False)
            display_readiness = getattr(
                worker, "readiness_for_operator_display", None
            )
            if callable(display_readiness):
                readiness = display_readiness(readiness)
            market_open = is_regular_session_open()
            until_open = (
                None
                if market_open
                else seconds_until_nyse_regular_session_open()
            )
            return _buyboard_readiness_display(
                readiness,
                device_state=getattr(
                    worker, "device_state", RuntimeDeviceState.STARTING
                ),
                reconciliation_accounts=(
                    worker.reconciliation_accounts_for_operator_display()
                    if callable(
                        getattr(
                            worker,
                            "reconciliation_accounts_for_operator_display",
                            None,
                        )
                    )
                    else tuple(
                        sorted(
                            str(item)
                            for item in (
                                getattr(
                                    worker,
                                    "reconciliation_accounts_in_progress",
                                    set(),
                                )
                                or set()
                            )
                        )
                    )
                ),
                regular_session_open=market_open,
                seconds_until_open=until_open,
                auto_claim_enabled=bool(
                    self.__dict__.get("_auto_claim_main_enabled", False)
                ),
                is_main_device=bool(
                    getattr(
                        self.__dict__.get("state_sync_role"),
                        "is_main",
                        False,
                    )
                ),
            )
        except Exception:
            logger.exception("Could not project Buy Board readiness progress")
            return BuyboardReadinessDisplay(
                0,
                8,
                "Buy Board readiness — status unavailable",
                "The readiness explanation could not be calculated; execution remains fail closed.",
            )

    def _update_buyboard_readiness_progress(self) -> None:
        progress_bar = self.__dict__.get("progress_bar")
        progress_label = self.__dict__.get("progress_label")
        if progress_bar is None or progress_label is None:
            return
        if self._foreground_progress_running():
            return
        display = self._current_buyboard_readiness_display()
        if display is None:
            if self.__dict__.pop("_buyboard_readiness_progress_active", False):
                progress_bar.setRange(0, 100)
                progress_bar.setValue(0)
                progress_label.setText("Ready.")
                progress_label.setToolTip("")
            return
        # Keep the shared bar on its normal 0-100 scale. Scanner/refresh
        # workers assume that scale when they take foreground ownership, and
        # a Qt busy range (0, 0) would otherwise make their later percentages
        # invisible. Duration-unknown phases are identified explicitly in the
        # label as "ETA unavailable" while the bar still shows how many of the
        # overall readiness gates have passed.
        progress_bar.setRange(0, 100)
        progress_bar.setValue(
            int((display.completed * 100) / max(1, display.total))
        )
        progress_label.setText(display.label)
        progress_label.setToolTip(display.tooltip)
        self._buyboard_readiness_progress_active = True

    # --- Automatic cross-machine handoff: post-claim reconciliation --------
    # The safety-critical sequence that must run before a newly-main device
    # may resume monitoring/live order submission. Manual activation uses the
    # same fence, but is never granted automatic kill-switch arming.

    def _begin_post_claim_handoff(self, *, allow_auto_arm: bool = False) -> None:
        """Lock pending flags, then reconcile against the broker off-thread."""
        existing_worker = self.__dict__.get("handoff_reconciliation_worker")
        if existing_worker is not None and existing_worker.isRunning():
            return
        self._handoff_generation += 1
        generation = self._handoff_generation
        self._handoff_reconciliation_required = True
        self._handoff_allow_auto_arm = bool(allow_auto_arm)

        # Main-device transfer never changes the deployment-wide kill switch.
        # Lease, readiness, and handoff reconciliation remain independent
        # all-orders fences until this device is actually safe to execute.
        if self.__dict__.get("_buylist_prod_monitor_active", False):
            self._toggle_buylist_monitor("PROD")

        reset_items = reset_runtime_only_order_flags(self.buylist_manager)
        if reset_items:
            symbols = ", ".join(sorted({item.symbol for item in reset_items}))
            self.append_log(
                f"Automatic handoff: locked {len(reset_items)} in-flight PROD "
                f"item(s) pending broker reconciliation ({symbols})."
            )
            self._save_buylist_state()
            self.populate_buylist_dashboard()

        worker = HandoffReconciliationWorker(
            self.buylist_manager, environment="PROD"
        )
        self.handoff_reconciliation_worker = worker
        worker.finished_reconciliation.connect(
            lambda outcome, gen=generation: self._on_post_claim_reconciliation_finished(
                outcome, gen
            )
        )
        worker.error_occurred.connect(self._on_post_claim_reconciliation_error)
        self._track_worker("handoff_reconciliation_worker", worker)
        worker.start()

    def _on_post_claim_reconciliation_finished(self, outcome, generation: int) -> None:
        if generation != self._handoff_generation:
            # Superseded by a newer handoff attempt.
            return
        if not self.state_sync_role.is_main:
            # Lost the lease while reconciliation was running -- an old
            # retry/reconciliation pass must never resume monitoring or arm
            # trading after that.
            return
        persisted, persistence_error = self._persist_post_claim_reconciliation_state()
        self.populate_buylist_dashboard()
        if not persisted:
            self._handoff_reconciliation_required = True
            blocked = tuple(outcome.blocked_symbols or outcome.reconciled_symbols)
            self._last_handoff_blocked_symbols = blocked or ("STATE_SYNC",)
            self.append_log(
                "Automatic handoff BLOCKED: reconciled state was not durably "
                f"published ({persistence_error}). Retrying in 30s."
            )
            if self.state_sync_role.is_main:
                QTimer.singleShot(
                    30_000, self._retry_post_claim_handoff_if_still_main
                )
            return
        self._last_handoff_blocked_symbols = tuple(outcome.blocked_symbols)
        if outcome.ok:
            self._handoff_reconciliation_required = False
            self.append_log(
                f"Automatic handoff: broker reconciliation clean for "
                f"{len(outcome.reconciled_symbols)} symbol(s)."
            )
            self.append_log(
                "Execution-owner transfer left the shared live-trading control "
                f"{'ON' if trading_state.is_trading_enabled() else 'OFF'}."
            )
            self._sync_buyboard_runtime_worker()
            self.append_log(
                "Automatic handoff complete: Buy Board execution ownership restored"
                f"{', live trading armed' if trading_state.is_trading_enabled() else ' (live trading NOT armed)'}."
            )
        else:
            blocked = ", ".join(outcome.blocked_symbols) or "unknown"
            self.append_log(
                f"Automatic handoff BLOCKED pending broker reconciliation for: "
                f"{blocked}. Retrying in 30s."
            )
            if outcome.errors:
                self.append_log(f"Reconciliation error(s): {'; '.join(outcome.errors)}")
            if not self.state_sync_role.is_main:
                # Lost the lease while we were blocked -- stop retrying.
                return
            QTimer.singleShot(30_000, self._retry_post_claim_handoff_if_still_main)

    def _persist_post_claim_reconciliation_state(self) -> tuple[bool, str]:
        """Durably save and strictly publish broker-corrected handoff state."""
        save_result = self._save_state_now(timeout=5.0, supersede_pending=True)
        if not save_result.success:
            return False, save_result.error or "local state save failed"
        execution_queue_payload = load_json(EXECUTION_QUEUE_FILE, {})
        try:
            published = publish_handoff_snapshot(
                self._execution_state_engine(),
                self.state_sync_role,
                self.buylist_manager.to_dict(),
                execution_queue_payload,
                metadata_path=self._execution_state_metadata_path(),
            )
        except Exception as exc:
            return False, str(exc)
        if not published:
            return False, "Kanban operational store did not confirm the handoff state"
        return True, ""

    def _retry_post_claim_handoff_if_still_main(self) -> None:
        if not self.state_sync_role.is_main:
            return
        if self.__dict__.get("_database_shutting_down", False):
            return
        self._begin_post_claim_handoff(
            allow_auto_arm=self.__dict__.get("_handoff_allow_auto_arm", False)
        )

    def _on_post_claim_reconciliation_error(self, message: str) -> None:
        self.append_log(f"Automatic handoff: reconciliation worker failed: {message}")

    def _auto_arm_trading_kill_switch(self) -> None:
        """Compatibility hook: handoff no longer owns the shared kill switch."""

        if self.__dict__.get("trading_enabled_button") is not None:
            self._refresh_trading_enabled_widget()
        self.append_log(
            "Live trading is controlled by the durable Kanban switch; "
            "Execution-owner handoff did not change it."
        )

    # Bounded age past which a cached "I am main" belief is no longer trusted
    # for order submission. Defense in depth against network partition: a
    # device that loses its connection to its operational store while still believing it's
    # main can never learn it's been demoted (its own reconciles just start
    # failing), so this makes it stop trading on its own before it could
    # even hear about an ownership change -- independent of, and in addition
    # to, ExecutionAuthority's live re-check at the actual broker boundary.
    _RECONCILE_FRESHNESS_MAX_AGE_SECONDS = 90

    @staticmethod
    def _handoff_env_flag_true(key: str) -> bool:
        value = get_env_value(key)
        if value is None:
            return False
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _expected_auto_claim_hostname_matches() -> bool:
        """Cheap insurance against a copy-pasted .env arming this elsewhere.

        No ``EXPECTED_AUTO_CLAIM_HOSTNAME`` configured means "not set up for
        auto-claim on this machine" -- fails closed, not open.
        """
        expected = get_env_value("EXPECTED_AUTO_CLAIM_HOSTNAME")
        if not expected or not expected.strip():
            return False
        return expected.strip().lower() == platform.node().strip().lower()

    def _current_execution_lease_kwargs(self) -> Dict[str, Any]:
        """Build the ExecutionAuthority/lease kwargs for a live order submission.

        Returns an all-None dict (no fencing requested) for lightweight
        test/dummy windows or whenever this device isn't main -- consistent
        with _state_sync_allows_order_submission already blocking non-main
        submission earlier in the same call chain. When main, threads the
        current lease token so ExecutionAuthority can re-verify it stayed
        current all the way to the actual broker boundary.
        """
        role = self.__dict__.get("state_sync_role")
        lease_token = self.__dict__.get("_current_lease_token", "")
        if role is None or not role.is_main or not lease_token:
            return {
                "execution_authority": None,
                "execution_lease": None,
                "lease_engine": None,
            }
        return {
            "execution_authority": ExecutionAuthority(),
            "execution_lease": LeaseHandle(
                device_id=role.device_id,
                lease_token=lease_token,
                lease_epoch=int(self.__dict__.get("_current_lease_epoch", 0) or 0),
            ),
            "lease_engine": self._execution_state_engine(),
        }

    # Maximum age of BuyboardRuntimeWorker.last_cycle_started_at (the
    # *attempt*-start signal, not completion) before the new engine is no
    # longer trusted to be actively iterating at all. Review finding P0:
    # a completion-only heartbeat flips "unhealthy" the moment one cycle's
    # sequential KIS calls (account snapshot, quote polling, order/position
    # reconciliation) run long, even though the worker is demonstrably
    # still working -- deliberately generous (an account refresh cycle can
    # legitimately involve several KIS calls with retry/backoff) so a single
    # slow cycle never triggers legacy fail-open, while still catching a
    # genuinely stalled/crashed loop within roughly a minute.
    _BUYBOARD_ENGINE_CYCLE_STALL_MAX_AGE_SECONDS = 45.0
    # Fallback threshold against last_heartbeat_at (cycle *completion*) --
    # only consulted when last_cycle_started_at itself is unavailable
    # (e.g. an older/minimal worker double), so this class still fails
    # closed rather than trusting an untracked worker indefinitely.
    _BUYBOARD_ENGINE_HEARTBEAT_MAX_AGE_SECONDS = 15.0

    def _ensure_critical_tray_icon(self) -> Optional[QSystemTrayIcon]:
        """Lazily creates and shows a system tray icon the first time a
        CRITICAL trading alert needs to reach the user outside the app's
        own log pane (review: "a card warning is insufficient when the
        user is asleep" / "the app is minimized"). Created on first use,
        not at startup, so an installation that never enables the Buy
        Board engine never gets a tray icon it has no use for. Reuses the
        app's own window icon if one is ever set; otherwise falls back to
        a standard critical-icon glyph so this needs zero new icon assets.
        """
        if not self.__dict__.get("_qt_base_initialized", False):
            # A lightweight test/dummy window whose Qt base never ran --
            # see the comment on _qt_base_initialized in __init__. Touching
            # QSystemTrayIcon/windowIcon()/style() here is not just
            # unsupported, it has been observed to crash the process
            # outright (access violation), which a try/except cannot catch.
            return None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = self.__dict__.get("_critical_tray_icon")
        if tray is not None:
            return tray
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_MessageBoxCritical)
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("quant_app -- Buy Board critical alerts")
        tray.show()
        self._critical_tray_icon = tray
        return tray

    def _show_critical_notification(self, title: str, message: str) -> None:
        """Routes a CRITICAL trading event to both the in-app log (the
        prior, sole behavior) and a native OS notification, so it reaches
        the user with the app minimized or unfocused too (review: "that
        does not protect unattended trading when the user is asleep / the
        app is minimized"). Never lets a notification-plumbing failure
        interrupt trading logic -- logging still happens even if the tray
        icon can't be created (headless environment, no tray support, ...).
        """
        self.append_log(message)
        try:
            tray = self._ensure_critical_tray_icon()
            if tray is not None:
                tray.showMessage(title, message, QSystemTrayIcon.Critical, 15000)
        except Exception:
            logger.exception("Failed to show critical tray notification")

    def _buyboard_engine_healthy(
        self,
        account_no: Optional[str] = None,
        *,
        action: str = "",
        symbol: str = "",
    ) -> bool:
        """Review finding: "legacy execution suppression depends only on
        the feature flag" -- it did not verify the new engine was actually
        running, holding its lease, and producing recent heartbeats, so a
        silently stopped/failed worker with the flag still on left *no*
        automatic engine protecting positions at all (both suppressed).

        Requires everything the review's own ``ExecutionOwnerState`` sketch
        does: the worker exists, is running, has completed startup
        reconciliation, and is demonstrably still iterating -- true broker
        lease currency is already covered by
        :meth:`_current_execution_lease_kwargs`/``ExecutionAuthority``,
        which the worker itself stops on
        (:meth:`~src.ui.buyboard.runtime_worker.BuyboardRuntimeWorker._lease_still_current`);
        a lease loss shows up here indirectly once its loop actually exits.

        ``account_no`` (review finding P0: "partial account failure still
        creates cross-account dual execution" -- one account's startup
        reconciliation failure previously made this method report globally
        unhealthy, which fails legacy protective exits *open* for every
        account, including ones the Buy Board engine had already fully
        confirmed and was actively managing) scopes the
        startup-reconciliation check to that specific account only: a
        different account's outstanding failure no longer suppresses a
        healthy account's own Buy Board ownership. Worker-level liveness
        (running, heartbeat/cycle iteration) still applies globally -- a
        genuinely stopped/crashed worker is unhealthy for every account
        regardless. Omit ``account_no`` for a worker-wide check.
        """
        worker = self.__dict__.get("_buyboard_runtime_worker")
        if worker is None:
            return False
        try:
            if not worker.isRunning():
                return False
        except RuntimeError:
            # The underlying Qt C++ object was already deleted.
            return False
        readiness_check = getattr(worker, "engine_readiness", None)
        if callable(readiness_check):
            readiness = readiness_check(
                account_no,
                action=action,
                symbol=symbol,
            )
            if not readiness.healthy:
                return False
        if not getattr(worker, "startup_reconciliation_ran", False):
            return False
        errors = getattr(worker, "startup_reconciliation_errors", None) or {}
        if account_no is not None:
            if action:
                action_ready = getattr(worker, "account_action_ready", None)
                if not callable(action_ready) or not action_ready(
                    account_no, symbol, action
                ):
                    return False
            else:
                # Review finding P0: "unknown accounts can be incorrectly
                # considered healthy" -- checking only "not in
                # startup_reconciliation_errors" treated an account that
                # was never processed as cleanly reconciled.
                reconciled = getattr(worker, "startup_reconciled_accounts", None) or set()
                if account_no not in reconciled:
                    return False
                if account_no in errors:
                    return False
        elif errors:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        cycle_started = getattr(worker, "last_cycle_started_at", None)
        if cycle_started is not None:
            started_age = (now - cycle_started).total_seconds()
            if started_age <= self._BUYBOARD_ENGINE_CYCLE_STALL_MAX_AGE_SECONDS:
                # Actively iterating -- even if the in-flight cycle itself
                # is slow, this is "busy," not "dead."
                return True
        last_heartbeat = getattr(worker, "last_heartbeat_at", None)
        if last_heartbeat is None:
            return False
        age_seconds = (now - last_heartbeat).total_seconds()
        return age_seconds <= self._BUYBOARD_ENGINE_HEARTBEAT_MAX_AGE_SECONDS

    def _sync_buyboard_runtime_worker(self) -> None:
        """Starts/stops :class:`~src.ui.buyboard.runtime_worker.BuyboardRuntimeWorker`
        to track main-device status and
        :func:`src.core.execution_config.is_buyboard_engine_enabled`
        (code review finding P0-1: nothing previously constructed or
        started this engine even when the flag was on). Safe to call
        repeatedly/idempotently -- called after every state-sync
        reconciliation pass and on tab setup, which is exactly when both
        of those inputs can change.

        Deliberately defensive: this entire branch is inert while the flag
        is off (the default), and any unexpected failure here must never
        prevent the rest of the application (least of all the legacy Buy
        Dashboard) from working, so every lookup is best-effort and the
        whole thing is wrapped in a broad try/except.
        """
        try:
            from src.core.execution_config import is_buyboard_engine_enabled

            worker = self.__dict__.get("_buyboard_runtime_worker")
            role = self.__dict__.get("state_sync_role")
            should_run = (
                is_buyboard_engine_enabled()
                and role is not None
                and self._execution_state_ready()
            )
            if not should_run:
                if worker is not None and worker.isRunning():
                    worker.request_stop()
                    worker.requestInterruption()
                return
            standby_only = not (
                role.is_main and bool(self.__dict__.get("_current_lease_token"))
            )
            if worker is not None and worker.isRunning():
                if bool(getattr(worker, "_standby_only", False)) == standby_only:
                    return  # already running in the correct role
                # Role changed: retain the same market-data service so an
                # unacknowledged stop breach survives recomposition.
                self._capture_buyboard_market_data_for_restart(worker)
                self._buyboard_runtime_restart_requested = True
                worker.request_stop()
                worker.requestInterruption()
                return

            from src.ui.buyboard.runtime_worker import BuyboardRuntimeWorker
            from src.services.external_alerting import build_external_alerting_service

            lease_kwargs = self._current_execution_lease_kwargs()
            if role.is_main and lease_kwargs.get("execution_authority") is None:
                return  # not actually main by the time we got here -- do not start

            queue_manager = self.__dict__.get("execution_queue_manager")
            execution_engine = self._execution_state_engine()
            external_alerting = build_external_alerting_service(
                execution_engine, device_id=role.device_id
            )

            # Review finding P0-1: this must be the real, per-account,
            # staleness-aware KIS buying power the legacy dashboard's own
            # KisAccountWorker already fetches (recorded into the cache by
            # DashboardMixin.apply_cached_trade_account_size every time that
            # worker completes) -- never a manual/hardcoded account-size
            # figure, and never the same number for two different accounts.
            from src.services import buying_power_cache

            buying_power_provider = buying_power_cache.make_buying_power_provider()
            account_equity_provider = buying_power_cache.make_account_equity_provider()

            new_worker = BuyboardRuntimeWorker(
                db_engine=execution_engine,
                environment="PROD",
                account_no="",  # unscoped -- processes every PROD account's cards
                buying_power_provider=buying_power_provider,
                account_equity_provider=account_equity_provider,
                execution_queue_item_lookup=(
                    (lambda symbol, env: queue_manager.get_item(symbol, env))
                    if queue_manager is not None
                    else None
                ),
                strategy_instance_id=execution_config.KANBAN_STRATEGY_INSTANCE_ID,
                market_data=self.__dict__.get("_buyboard_market_data_handoff"),
                standby_only=standby_only,
                device_id=role.device_id,
                hostname=role.hostname,
                external_alerting=external_alerting,
                **lease_kwargs,
            )
            new_worker.board_changed.connect(self.refresh_buyboard)
            new_worker.error_occurred.connect(self.append_log)
            new_worker.alert.connect(
                lambda message: self._show_critical_notification("Buy Board Alert", message)
            )
            self._track_worker("_buyboard_runtime_worker", new_worker)
            self._buyboard_runtime_worker = new_worker
            new_worker.start()
            self.__dict__.pop("_buyboard_market_data_handoff", None)
            self._buyboard_runtime_restart_requested = False
            self.append_log(
                "Buy Board runtime started in "
                f"{'standby/read-only' if standby_only else 'execution-owner activation'} mode."
            )
        except Exception:
            logger.exception("Failed to sync the Buy Board runtime worker")

    def _capture_buyboard_market_data_for_restart(self, worker) -> None:
        runtime = getattr(worker, "runtime", None)
        market_data = getattr(runtime, "market_data", None)
        if market_data is not None:
            self._buyboard_market_data_handoff = market_data

    def _state_sync_allows_order_submission(self) -> bool:
        """Allow broker submissions only from the recently-confirmed Execution Owner."""
        role = self.__dict__.get("state_sync_role")
        if role is None:
            # Lightweight test/dummy windows do not initialize sync state.
            return True
        if self.__dict__.get("_handoff_reconciliation_required", False):
            self.append_log(
                "KIS order submission blocked: execution-owner handoff broker "
                "reconciliation has not completed cleanly."
            )
            return False
        manager = self._state_save_manager()
        allowed = bool(
            role.is_main and getattr(manager, "_is_main_device", role.is_main)
        )
        coordination_stale = False
        if allowed:
            last_reconcile = self.__dict__.get("_last_successful_reconcile_at")
            if last_reconcile is None:
                allowed = False
            else:
                age_seconds = (
                    dt.datetime.now(dt.timezone.utc) - last_reconcile
                ).total_seconds()
                if age_seconds > self._RECONCILE_FRESHNESS_MAX_AGE_SECONDS:
                    allowed = False
                    coordination_stale = True
                    self.append_log(
                        "KIS order submission blocked: last successful state "
                        f"sync was {age_seconds:.0f}s ago (stale beyond "
                        f"{self._RECONCILE_FRESHNESS_MAX_AGE_SECONDS}s) -- this "
                        "device may have lost its connection and been "
                        "superseded elsewhere."
                    )
        if allowed:
            return True
        if coordination_stale:
            self.append_log(
                "CRITICAL: Kanban lease coordination is stale. All KIS "
                "orders, including protective exits, remain blocked to prevent "
                "split-brain execution. Manage the position directly in KIS if needed."
            )
            QMessageBox.warning(
                self,
                "Trading safety lock",
                "Kanban lease coordination is stale. All KIS submissions, "
                "including protective exits, are blocked to prevent two devices "
                "from trading at once.\n\nRestore the Kanban operational store or manage any "
                "live position directly in KIS.",
            )
            return False
        self.append_log(
            "KIS order submission blocked because this device is pull-only."
        )
        QMessageBox.warning(
            self,
            "Standby device",
            "Only the current Execution Owner may submit KIS orders. Select "
            "this machine under Execution Owner after it reports Executor Ready.",
        )
        return False

    def _apply_unresolved_order_startup_state(self) -> None:
        """Reflect durable unresolved broker orders in the UI after startup."""
        open_orders = [
            order
            for order in find_open_orders(self.order_ledger)
            if order.environment == "PROD"
        ]
        if not open_orders:
            return

        self.append_log(
            f"Loaded {len(open_orders)} unresolved broker order(s) from order ledger. "
            "Duplicate execution is blocked until reconciliation."
        )

        changed = False
        for order in open_orders:
            try:
                item = self.buylist_manager.get(order.symbol, order.environment)
            except TypeError:
                item = self.buylist_manager.get(order.symbol)
            if item is None:
                continue

            if order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE:
                new_status = "UNKNOWN_SUBMISSION_STATE"
            elif order.side == OrderSide.BUY:
                # Keep a holdings-confirmed position visible even when its
                # durable broker ledger entry has not reached a terminal state.
                # The ledger still blocks duplicate order submission.
                new_status = (
                    "BOUGHT"
                    if str(getattr(item, "monitoring_status", "") or "").upper()
                    == "BOUGHT"
                    and int(getattr(item, "shares_held", 0) or 0) > 0
                    else "BUY_SUBMITTED"
                )
            elif order.intent in {
                OrderIntent.PARTIAL_EXIT,
                OrderIntent.PARTIAL_TAKE_PROFIT,
            }:
                new_status = "PARTIAL_EXIT_SUBMITTED"
            else:
                new_status = "SELL_SUBMITTED"

            if getattr(item, "monitoring_status", "") != new_status:
                item.monitoring_status = new_status
                changed = True
            kis_order_id = order.broker_order_id or order.client_order_id
            if kis_order_id and getattr(item, "kis_order_id", "") != kis_order_id:
                item.kis_order_id = kis_order_id
                changed = True

        if changed:
            self._save_buylist_state()
            self.populate_buylist_dashboard()

    def _apply_global_stylesheet(self) -> None:
        """Apply a modern, premium TradingView-style global stylesheet."""
        global_css = """
        QMainWindow {
            background-color: #f8f9fa;
        }

        /* Group Box */
        QGroupBox {
            border: 1px solid #e0e3eb;
            border-radius: 6px;
            margin-top: 18px;
            padding-top: 12px;
            font-weight: bold;
            font-size: 14px;
            color: #131722;
            background-color: #ffffff;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 10px;
            padding: 0 5px;
            color: #131722;
        }

        /* Tabs */
        QTabWidget::pane {
            border: 1px solid #e0e3eb;
            background-color: #ffffff;
            border-radius: 6px;
        }
        QTabBar::tab {
            background: #f1f3f6;
            border: 1px solid #d1d4dc;
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            min-width: 100px;
            padding: 8px 16px;
            font-weight: 500;
            color: #5d606b;
            font-size: 14px;
        }
        QTabBar::tab:selected {
            background: #ffffff;
            border-color: #e0e3eb;
            color: #131722;
            border-bottom: 2px solid #2962ff;
            font-weight: bold;
        }
        QTabBar::tab:hover:!selected {
            background: #eef1f6;
            color: #131722;
        }

        /* Input Controls */
        QLineEdit, QTextEdit, QTextBrowser, QSpinBox, QComboBox, QDoubleSpinBox {
            border: 1px solid #d1d4dc;
            border-radius: 6px;
            padding: 6px 10px;
            font-size: 14px;
            color: #131722;
            background-color: #ffffff;
        }
        QLineEdit:focus, QTextEdit:focus, QTextBrowser:focus, QSpinBox:focus, QComboBox:focus, QDoubleSpinBox:focus {
            border: 1px solid #2962ff;
        }

        /* ComboBox Arrow */
        QComboBox {
            padding-right: 24px;
        }
        QComboBox::drop-down {
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 20px;
            border-left-width: 1px;
            border-left-color: #d1d4dc;
            border-left-style: solid;
            border-top-right-radius: 6px;
            border-bottom-right-radius: 6px;
        }
        QComboBox::down-arrow {
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid #5d606b;
            width: 0;
            height: 0;
        }

        /* Tables, Lists, Trees */
        QTableWidget, QTreeWidget, QListWidget {
            border: 1px solid #e0e3eb;
            border-radius: 6px;
            background-color: #ffffff;
            gridline-color: #f0f3f6;
            font-size: 14px;
            color: #131722;
            selection-background-color: #e2e4ea;
            selection-color: #131722;
        }
        QTableWidget::item, QTreeWidget::item, QListWidget::item {
            padding: 6px;
        }
        QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {
            background-color: #e2e4ea;
            color: #131722;
            font-weight: bold;
        }
        QHeaderView::section {
            background-color: #f8f9fa;
            color: #131722;
            font-weight: bold;
            font-size: 14px;
            padding: 8px;
            border: none;
            border-bottom: 2px solid #e0e3eb;
        }

        /* Scrollbars */
        QScrollBar:vertical {
            border: none;
            background: #f1f3f6;
            width: 10px;
            margin: 0px;
        }
        QScrollBar::handle:vertical {
            background: #d1d4dc;
            min-height: 20px;
            border-radius: 5px;
        }
        QScrollBar::handle:vertical:hover {
            background: #787b86;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            border: none;
            background: none;
            height: 0px;
        }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: none;
        }
        QScrollBar:horizontal {
            border: none;
            background: #f1f3f6;
            height: 10px;
            margin: 0px;
        }
        QScrollBar::handle:horizontal {
            background: #d1d4dc;
            min-width: 20px;
            border-radius: 5px;
        }
        QScrollBar::handle:horizontal:hover {
            background: #787b86;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            border: none;
            background: none;
            width: 0px;
        }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
            background: none;
        }

        /* DockWidget styling */
        QDockWidget {
            border: 1px solid #e0e3eb;
            titlebar-close-icon: none;
            titlebar-normal-icon: none;
        }
        QDockWidget::title {
            text-align: center;
            background-color: #f1f3f6;
            padding: 6px;
            font-weight: bold;
            color: #131722;
            border-bottom: 1px solid #e0e3eb;
        }

        /* Progress Bar */
        QProgressBar {
            border: 1px solid #d1d4dc;
            border-radius: 4px;
            background-color: #ffffff;
            text-align: center;
        }
        QProgressBar::chunk {
            background-color: #009688;
            border-radius: 3px;
        }

        /* Labels styling */
        QLabel {
            font-size: 14px;
            color: #131722;
        }

        /* Global buttons styling: defaults to light-gray style (less colors) */
        QPushButton {
            background-color: #f0f3f6;
            color: #131722;
            font-weight: bold;
            border-radius: 6px;
            padding: 8px 16px;
            font-size: 14px;
            border: 1px solid #d1d4dc;
        }
        QPushButton:hover {
            background-color: #e0e3eb;
        }
        QPushButton:pressed {
            background-color: #d1d4dc;
        }
        QPushButton:disabled {
            background-color: #f8f9fa;
            color: #b2b5be;
            border-color: #e0e3eb;
        }

        /* Blue Button Accents */
        QPushButton#addRuleButton,
        QPushButton#savePlanButton,
        QPushButton#addManualButton,
        QPushButton#saveSettingsButton,
        QPushButton#selectFilterButton {
            background-color: #2962ff;
            color: #ffffff;
            border: none;
        }
        QPushButton#addRuleButton:hover,
        QPushButton#savePlanButton:hover,
        QPushButton#addManualButton:hover,
        QPushButton#saveSettingsButton:hover,
        QPushButton#selectFilterButton:hover {
            background-color: #1a56db;
        }
        QPushButton#addRuleButton:pressed,
        QPushButton#savePlanButton:pressed,
        QPushButton#addManualButton:pressed,
        QPushButton#saveSettingsButton:pressed,
        QPushButton#selectFilterButton:pressed {
            background-color: #123e9c;
        }

        /* Green Button Accents */
        QPushButton#scanButton,
        QPushButton#refreshDbButton,
        QPushButton#refreshHourlyButton,
        QPushButton#refreshIntradayButton,
        QPushButton#runScannerButton {
            background-color: #009688;
            color: #ffffff;
            border: none;
        }
        QPushButton#scanButton:hover,
        QPushButton#refreshDbButton:hover,
        QPushButton#refreshHourlyButton:hover,
        QPushButton#refreshIntradayButton:hover,
        QPushButton#runScannerButton:hover {
            background-color: #00796b;
        }
        QPushButton#scanButton:pressed,
        QPushButton#refreshDbButton:pressed,
        QPushButton#refreshHourlyButton:pressed,
        QPushButton#refreshIntradayButton:pressed,
        QPushButton#runScannerButton:pressed {
            background-color: #004d40;
        }
        """
        self.setStyleSheet(global_css)

    def _load_watchlist(self) -> Watchlist:
        """Load persisted watchlist state."""
        return load_watchlist_state()

    def _load_buylist(self) -> BuylistManager:
        """Load persisted buylist state."""
        return load_buylist_state()

    def _load_trade_plans(self) -> TradePlanManager:
        """Load persisted trade plan state."""
        return load_trade_plans_state()

    def _ensure_save_lock(self) -> threading.Lock:
        if "_save_lock" not in self.__dict__:
            self._save_lock = threading.Lock()
        return self._save_lock

    def _state_save_manager(self):
        manager = self.__dict__.get("state_save_manager")
        if manager is None:
            manager = get_state_save_manager()
            self.state_save_manager = manager
        return manager

    def _bind_remote_state_engine(
        self,
        engine,
        *,
        is_main_device: Optional[bool] = None,
    ) -> None:
        """Attach or detach remote state persistence as one routing change."""
        role = self.state_sync_role
        if is_main_device is None:
            is_main_device = bool(role.is_main)
        manager = self._state_save_manager()
        # Keep base revisions isolated from the optional shared/history
        # database. Reusing its metadata would make a new local store look as
        # if its missing rows had been deleted remotely.
        manager.metadata_file = self._execution_state_metadata_path()
        manager.set_engine(
            engine,
            device_id=role.device_id,
            is_main_device=is_main_device,
        )

    def _state_save_payload(
        self,
    ) -> tuple[
        Dict[str, Any],
        Dict[str, Any],
        Dict[str, Any],
        Any,
        Dict[str, Any],
        Dict[str, Any],
    ]:
        values = self.__dict__
        watchlist = values.get("watchlist")
        buylist_manager = values.get("buylist_manager")
        trade_manager = values.get("trade_manager")
        scanner_setups = values.get("scanner_setups", [])
        chart_drawings = values.get("chart_drawings", {})
        tab_options = values.get("tab_options", {})

        watchlist_dict = (
            watchlist.to_dict()
            if watchlist is not None
            else {"name": "Default", "items": []}
        )
        buylist_dict = (
            buylist_manager.to_dict() if buylist_manager is not None else {"items": []}
        )
        trade_manager_dict = (
            trade_manager.to_dict() if trade_manager is not None else {"plans": []}
        )
        scanner_setups_copy = (
            list(scanner_setups) if isinstance(scanner_setups, list) else scanner_setups
        )
        chart_drawings_copy = (
            dict(chart_drawings) if isinstance(chart_drawings, dict) else chart_drawings
        )
        tab_options_copy = (
            dict(tab_options) if isinstance(tab_options, dict) else tab_options
        )
        return (
            watchlist_dict,
            buylist_dict,
            trade_manager_dict,
            scanner_setups_copy,
            chart_drawings_copy,
            tab_options_copy,
        )

    def _save_state(self) -> None:
        """Persist user-managed state."""
        payload = self._state_save_payload()

        save_app_state(
            *payload,
            save_lock=self._ensure_save_lock(),
            append_log=getattr(self, "append_log", None),
        )

    def _save_state_now(
        self,
        *,
        timeout: float | None = None,
        supersede_pending: bool = False,
    ) -> SaveResult:
        """Synchronously persist user-managed state."""
        return self._state_save_manager().save_now(
            *self._state_save_payload(),
            save_lock=self._ensure_save_lock(),
            append_log=getattr(self, "append_log", None),
            lock_timeout=timeout,
            supersede_pending=supersede_pending,
        )

    def _load_chart_drawings(self) -> dict:
        return load_chart_drawings_state()

    @staticmethod
    def _normalize_tab_options(data: dict) -> dict:
        raw_options = data.get("tabs", data) if isinstance(data, dict) else {}
        options = dict(DEFAULT_TAB_OPTIONS)
        if isinstance(raw_options, dict):
            for key in DEFAULT_TAB_OPTIONS:
                if key in raw_options:
                    options[key] = bool(raw_options[key])
        return options

    def _load_tab_options(self) -> dict:
        return self._normalize_tab_options(load_tab_options_state(DEFAULT_TAB_OPTIONS))

    @staticmethod
    def _normalize_scanner_setups(data: dict) -> dict:
        """Normalize scanner setup data loaded from disk."""
        raw_setups = data.get("setups", data)
        if not isinstance(raw_setups, dict):
            raw_setups = {}

        setups = {}
        for name, values in raw_setups.items():
            if not isinstance(values, dict):
                continue
            try:
                setup_name = str(name).strip()
                if not setup_name:
                    continue
                setup_data = {
                    "min_volume": float(values.get("min_volume", 40000.0)),
                    "min_dollar_volume": float(
                        values.get("min_dollar_volume", 35000.0)
                    ),
                    "min_adr": float(values.get("min_adr", 2.4)),
                    "min_growth_rank": float(values.get("min_growth_rank", 97.04)),
                    "min_trend_intensity": float(
                        values.get("min_trend_intensity", 90.0)
                    ),
                }

                if "rules" in values and isinstance(values["rules"], list):
                    normalized_rules = []
                    for r in values["rules"]:
                        if isinstance(r, dict) and "attribute" in r:
                            normalized_rules.append(
                                {
                                    "attribute": str(r.get("attribute")),
                                    "operator": str(r.get("operator", ">=")),
                                    "threshold": r.get("threshold", ""),
                                }
                            )
                    setup_data["rules"] = normalized_rules
                else:
                    setup_data["rules"] = [
                        {
                            "attribute": "volume",
                            "operator": ">=",
                            "threshold": setup_data["min_volume"],
                        },
                        {
                            "attribute": "dollar_volume",
                            "operator": ">=",
                            "threshold": setup_data["min_dollar_volume"],
                        },
                        {
                            "attribute": "adr_20",
                            "operator": ">=",
                            "threshold": setup_data["min_adr"],
                        },
                        {
                            "attribute": "growth_rank_1m",
                            "operator": ">=",
                            "threshold": setup_data["min_growth_rank"],
                        },
                        {
                            "attribute": "trend_intensity",
                            "operator": ">=",
                            "threshold": setup_data["min_trend_intensity"],
                        },
                    ]

                setups[setup_name] = setup_data
            except (TypeError, ValueError):
                continue

        if not setups:
            setups = {
                name: values.copy() for name, values in DEFAULT_SCANNER_SETUPS.items()
            }
        return setups

    def _load_scanner_setups(self) -> dict:
        """Load persisted scanner setups."""
        return self._normalize_scanner_setups(
            load_scanner_setups_state(DEFAULT_SCANNER_SETUPS)
        )

    def _flush_state_saves_for_shutdown(self, timeout: float = 5.0) -> SaveResult:
        manager = self._state_save_manager()
        deadline = time.monotonic() + timeout
        pending_timeout = min(3.0, timeout)
        pending_finished = manager.wait_for_pending_saves(timeout=pending_timeout)
        if not pending_finished:
            self.append_log(
                "Timed out waiting for pending local state save before shutdown."
            )

        remaining = max(0.0, deadline - time.monotonic())
        return self._save_state_now(timeout=remaining, supersede_pending=True)

    @staticmethod
    def _stop_workers_for_shutdown(
        running_workers: List[QThread], timeout_ms: int = WORKER_SHUTDOWN_TIMEOUT_MS
    ) -> bool:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        for worker in running_workers:
            request_stop = getattr(worker, "request_stop", None)
            if callable(request_stop):
                request_stop()
            worker.requestInterruption()
        for worker in running_workers:
            worker.quit()
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if not worker.wait(remaining_ms):
                return False
        return True

    def _shutdown_wait_message(
        self, unfinished_workers: List[QThread]
    ) -> Tuple[str, str]:
        """Describe the task that prevented a safe close."""
        mirror_worker = self.__dict__.get("_local_mirror_sync_worker")
        if mirror_worker in unfinished_workers:
            phase = self.__dict__.get(
                "_local_mirror_progress_phase",
                "closing its database transaction",
            )
            return (
                "Laptop backup finishing",
                "The PC-to-laptop safety backup is still stopping safely.\n\n"
                f"Current phase: {phase}\n\n"
                "The dashboard is not downloading market data for trading. "
                "Wait for the Laptop backup progress line to finish, then close again.",
            )
        task_names = ", ".join(
            type(worker).__name__ for worker in unfinished_workers
        ) or "unknown task"
        return (
            "Background task running",
            "A background task is still stopping safely.\n\n"
            f"Running: {task_names}\n\n"
            "Wait for it to finish before closing.",
        )

    def closeEvent(self, event) -> None:
        if not self._authorize_execution_shutdown():
            event.ignore()
            return
        self._database_shutting_down = True
        self._database_transition_generation = (
            self.__dict__.get("_database_transition_generation", 0) + 1
        )
        timers = [
            self.__dict__.get("state_sync_timer"),
            self.__dict__.get("sleep_readiness_timer"),
            self.__dict__.get("pc_status_timer"),
            self.__dict__.get("local_mirror_sync_timer"),
            self.__dict__.get("market_status_timer"),
            self.__dict__.get("buyboard_readiness_progress_timer"),
            self.__dict__.get("_buyboard_projection_timer"),
            self.__dict__.get("_buyboard_live_metric_timer"),
            self.__dict__.get("_buyboard_orb_data_timer"),
            self.__dict__.get("_buyboard_broker_truth_timer"),
            self.__dict__.get("_refresh_poll_timer"),
        ]
        timer_states = []
        for timer in timers:
            if timer is None:
                continue
            try:
                was_active = bool(timer.isActive())
            except Exception:
                was_active = True
            timer_states.append((timer, was_active))
            timer.stop()
        runtime_worker = self.__dict__.get("_buyboard_runtime_worker")
        if runtime_worker is not None:
            self._capture_buyboard_market_data_for_restart(runtime_worker)
        candidate_workers = [
            self.__dict__.get("database_init_worker"),
            self.__dict__.get("coordination_database_init_worker"),
            self.__dict__.get("database_recovery_worker"),
            self.__dict__.get("_local_mirror_sync_worker"),
            self.__dict__.get("state_sync_worker"),
            self.__dict__.get("_pc_status_worker"),
            self.__dict__.get("scanner_worker"),
            self.__dict__.get("kis_order_worker"),
            self.__dict__.get("intraday_fetch_worker"),
            self.__dict__.get("intraday_bulk_worker"),
            self.__dict__.get("kis_account_worker"),
            self.__dict__.get("kis_startup_worker"),
            self.__dict__.get("order_reconciliation_worker"),
            self.__dict__.get("fx_rate_worker"),
            self.__dict__.get("broker_order_query_worker"),
            self.__dict__.get("broker_order_cancel_worker"),
            self.__dict__.get("handoff_reconciliation_worker"),
            self.__dict__.get("_buyboard_runtime_worker"),
            *self.__dict__.get("_buylist_order_workers", []),
            *self.__dict__.get("_buylist_aux_workers", []),
            *list(self.__dict__.get("_tracked_workers", {})),
        ]
        seen_workers: set[int] = set()
        running_workers = []
        for worker in candidate_workers:
            if worker is None or id(worker) in seen_workers:
                continue
            seen_workers.add(id(worker))
            if worker.isRunning():
                running_workers.append(worker)
        if not self._stop_workers_for_shutdown(
            running_workers, timeout_ms=WORKER_SHUTDOWN_TIMEOUT_MS
        ):
            self._database_shutting_down = False
            for timer, was_active in timer_states:
                if was_active:
                    timer.start()
            unfinished_workers = [
                worker for worker in running_workers if worker.isRunning()
            ]
            title, message = self._shutdown_wait_message(unfinished_workers)
            QMessageBox.warning(
                self,
                title,
                message,
            )
            event.ignore()
            return
        # Strict shutdown ordering for a clean cross-machine handoff:
        # finish the final local save (already attempts a best-effort remote
        # push) -> strictly re-verify that push actually landed -> demote +
        # release the main-device lease -> mark the runtime heartbeat
        # stopped. Getting this order wrong is exactly how a released
        # laptop could re-claim its own just-vacated row on its very next
        # reconcile (see _release_main_device_ownership_for_shutdown).
        save_result = self._flush_state_saves_for_shutdown(timeout=5.0)
        if not save_result.success:
            message = save_result.error or "Unknown local state save error."
            self.append_log(f"Final local state save failed during shutdown: {message}")
            QMessageBox.warning(
                self,
                "Local Save Warning",
                f"Final local state save failed:\n\n{message}",
            )

        released = self._release_main_device_ownership_for_shutdown(
            final_save_succeeded=save_result.success
        )
        if not released and not self._authorize_emergency_close_after_release_failure():
            self._restore_protection_after_aborted_shutdown(timer_states)
            QMessageBox.critical(
                self,
                "Shutdown aborted",
                "Execution ownership could not be released safely. The dashboard "
                "has stayed open and restarted its protection runtime. Resolve the "
                "database/handoff failure before closing again.",
            )
            event.ignore()
            return

        safe_mark_runtime_process_stopped(
            self._execution_state_engine()
            if self._execution_state_ready()
            else None
        )
        super().closeEvent(event)

    def _release_main_device_ownership_for_shutdown(
        self, *, final_save_succeeded: bool = True
    ) -> bool:
        """Strict handoff publish + release, called once during closeEvent.

        Extracted as its own method (rather than inlined in the already-long
        closeEvent) so it's independently testable. A no-op for a pull-only
        device or when the Kanban operational store isn't ready -- most shutdowns
        never touch the network at all.
        """
        if not self.state_sync_role.is_main:
            return True
        if not self._execution_state_ready():
            return False
        execution_engine = self._execution_state_engine()
        decision = self._execution_shutdown_lease_decision(
            explicit_unprotected_acceptance=bool(
                self.__dict__.get("_shutdown_unprotected_accepted", False)
            )
        )
        if not decision.allowed:
            self.append_log(f"Execution ownership retained: {decision.reason}")
            return False
        runtime_worker = self.__dict__.get("_buyboard_runtime_worker")
        if (
            runtime_worker is not None
            and not bool(getattr(runtime_worker, "shutdown_prepared", False))
        ):
            self.append_log(
                "Execution ownership retained: Buy Board journal/final "
                "reconciliation/WebSocket shutdown sequence did not complete."
            )
            return False
        if not final_save_succeeded:
            self.append_log(
                "Execution ownership retained: final local state save failed; "
                "the next device must use stale-heartbeat takeover."
            )
            return False

        execution_queue_payload = load_json(EXECUTION_QUEUE_FILE, {})
        try:
            published = publish_handoff_snapshot(
                execution_engine,
                self.state_sync_role,
                self.buylist_manager.to_dict(),
                execution_queue_payload,
                metadata_path=self._execution_state_metadata_path(),
            )
        except Exception as exc:
            published = False
            self.append_log(f"Handoff publication failed: {exc}")
        if not published:
            self.append_log(
                "Execution ownership retained because the final handoff "
                "snapshot was not confirmed durably; the next device must "
                "use stale-heartbeat takeover."
            )
            return False

        owning_role = self.state_sync_role
        released, resulting_role, release_error = release_main_device_and_demote(
            execution_engine,
            owning_role,
            expected_lease_token=str(
                self.__dict__.get("_current_lease_token", "") or ""
            ),
            expected_lease_epoch=int(
                self.__dict__.get("_current_lease_epoch", 0) or 0
            ),
            disable_remote_writer=lambda: self._bind_remote_state_engine(
                execution_engine, is_main_device=False
            ),
        )
        if released:
            self.state_sync_role = resulting_role
            self._current_lease_token = ""
            self._current_lease_epoch = 0
        else:
            # The remote row is still owned by this device. Do not let a
            # failed local demotion make the live process believe otherwise.
            self.state_sync_role = owning_role
            try:
                self._bind_remote_state_engine(
                    execution_engine, is_main_device=True
                )
            except Exception:
                logger.exception("Could not restore remote writer after release failure")
            if release_error:
                self.append_log(f"Execution-owner release failed: {release_error}")
        return released

    def _authorize_emergency_close_after_release_failure(self) -> bool:
        """Require a second, explicit supervised override after release fails."""

        if self.__dict__.get("_auto_claim_main_enabled", False):
            self.append_log(
                "CRITICAL: unattended shutdown aborted because final lease release failed."
            )
            return False
        answer = QMessageBox.critical(
            self,
            "Execution-owner release failed",
            "The application could not complete its final state publication, "
            "runtime shutdown, or execution lease release. Closing now may "
            "leave positions unprotected and requires stale-heartbeat takeover.\n\n"
            "Exit anyway under supervised emergency acceptance?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        accepted = answer == QMessageBox.Yes
        if accepted:
            self.append_log(
                "CRITICAL: user explicitly accepted emergency shutdown after "
                "execution lease release failed."
            )
        return accepted

    def _restore_protection_after_aborted_shutdown(self, timer_states) -> None:
        """Re-open timers and the execution runtime after a refused close."""

        self._database_shutting_down = False
        self._database_transition_generation = (
            self.__dict__.get("_database_transition_generation", 0) + 1
        )
        engine = self._execution_state_engine()
        role = self.__dict__.get("state_sync_role")
        if engine is not None and role is not None:
            try:
                result = reconcile_state_with_remote(
                    engine,
                    role,
                    save_lock=self._ensure_save_lock(),
                    ownership_only_when_main=True,
                    allow_unprepared_claim=not execution_config.is_buyboard_engine_enabled(),
                    metadata_path=self._execution_state_metadata_path(),
                )
                if result.local_role is not None:
                    self.state_sync_role = result.local_role
                self._current_lease_token = (
                    result.lease_token if result.is_main_device else ""
                )
                self._current_lease_epoch = (
                    int(result.lease_epoch or 0) if result.is_main_device else 0
                )
                self._bind_remote_state_engine(
                    engine, is_main_device=result.is_main_device
                )
            except Exception:
                logger.exception("Could not restore ownership after aborted shutdown")
        for timer, was_active in timer_states:
            if was_active:
                timer.start()
        worker = self.__dict__.get("_buyboard_runtime_worker")
        if worker is not None:
            try:
                if not worker.isRunning():
                    self._buyboard_runtime_worker = None
            except RuntimeError:
                self._buyboard_runtime_worker = None
        self._buyboard_runtime_restart_requested = False
        self._sync_buyboard_runtime_worker()

    def _execution_shutdown_exposure(self):
        """Read durable cards/orders and name everything still exposed."""

        from src.core.execution_order_record import TERMINAL_EXECUTION_ORDER_STATUSES
        from src.core.runtime_readiness import ShutdownExposure
        from src.core.trade_card_state import BoardStatus
        from src.services.execution_order_repository import (
            list_execution_orders,
        )
        from src.services.trade_card_repository import list_trade_cards

        engine = self._execution_state_engine()
        if engine is None or not self._execution_state_ready():
            return ShutdownExposure(
                inspection_confirmed=False,
                inspection_error="Kanban operational store unavailable",
            )
        try:
            cards = list_trade_cards(engine, environment="PROD")
            open_statuses = {
                BoardStatus.OPEN_POSITION,
                BoardStatus.PARTIAL_SELL,
                BoardStatus.SELL_ALL,
            }
            positions = tuple(
                f"{card.account_no}/{card.symbol}"
                for card in cards
                if card.board_status in open_statuses
                or int(card.broker_quantity or 0) > 0
                or int(card.orderable_quantity or 0) > 0
            )
            working = [
                f"{order.account_no}/{order.symbol} order"
                for order in list_execution_orders(engine, environment="PROD")
                if order.status not in TERMINAL_EXECUTION_ORDER_STATUSES
            ]
            # The legacy ledger also contains old SIM/test orders.  They are
            # intentionally retained for audit/history, but can never be live
            # production exposure and must not block a PROD lease release.
            for order in find_open_orders(
                self.__dict__.get("order_ledger", []) or [],
                environment="PROD",
            ):
                working.append(
                    f"{getattr(order, 'account_no', '')}/{order.symbol} legacy order"
                )
            return ShutdownExposure(
                open_positions=tuple(positions),
                working_orders=tuple(working),
            )
        except Exception as exc:
            # Unknown exposure is never interpreted as flat.
            logger.exception("Could not inspect execution exposure for shutdown")
            from src.core.runtime_readiness import ShutdownExposure

            return ShutdownExposure(
                inspection_confirmed=False,
                inspection_error=f"inspection failed: {exc}",
            )

    def _execution_shutdown_lease_decision(
        self, *, explicit_unprotected_acceptance: bool = False
    ):
        from src.core.runtime_readiness import decide_shutdown_lease_release
        from src.services.runtime_device_state_repository import (
            confirm_standby_handoff,
            find_confirmed_standby_successor,
            find_standby_successor,
        )

        exposure = self._execution_shutdown_exposure()
        successor = None
        engine = self._execution_state_engine()
        role = self.__dict__.get("state_sync_role")
        outgoing_lease_epoch = int(
            self.__dict__.get("_current_lease_epoch", 0) or 0
        )
        if engine is not None and role is not None and outgoing_lease_epoch > 0:
            try:
                candidate = find_standby_successor(
                    engine, excluding_device_id=role.device_id
                )
                if candidate is not None and not candidate.handoff_confirmed:
                    # This write is the outgoing owner's side of the
                    # handshake. The lease is released only after reading
                    # the confirmed row back below.
                    confirm_standby_handoff(
                        engine,
                        device_id=candidate.device_id,
                        readiness_generation=candidate.readiness_generation,
                        outgoing_lease_epoch=outgoing_lease_epoch,
                    )
                successor = find_confirmed_standby_successor(
                    engine,
                    excluding_device_id=role.device_id,
                    expected_outgoing_lease_epoch=outgoing_lease_epoch,
                )
            except Exception:
                logger.exception("Could not verify a STANDBY_READY successor")
        return decide_shutdown_lease_release(
            exposure,
            successor_standby_ready=successor is not None,
            handoff_confirmed=bool(successor and successor.handoff_confirmed),
            unattended=bool(self.__dict__.get("_auto_claim_main_enabled", False)),
            explicit_unprotected_acceptance=explicit_unprotected_acceptance,
        )

    def _authorize_execution_shutdown(self) -> bool:
        """Apply E4 before timers/workers are disturbed."""

        role = self.__dict__.get("state_sync_role")
        if role is None or not role.is_main:
            return True
        decision = self._execution_shutdown_lease_decision()
        if decision.allowed:
            return True
        if self.__dict__.get("_auto_claim_main_enabled", False):
            self.append_log(f"CRITICAL: {decision.reason}")
            QMessageBox.critical(self, "Unattended shutdown refused", decision.reason)
            return False
        exposure = self._execution_shutdown_exposure()
        names = "\n".join(f"- {item}" for item in exposure.labels)
        answer = QMessageBox.critical(
            self,
            "Unprotected positions",
            "No confirmed STANDBY_READY successor exists. Closing will leave "
            "these positions/orders without application protection:\n\n"
            f"{names}\n\nAccept this unprotected supervised shutdown?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        accepted = answer == QMessageBox.Yes
        self._shutdown_unprotected_accepted = accepted
        if accepted:
            self.append_log(
                "CRITICAL: user explicitly accepted supervised shutdown with "
                f"unprotected exposure: {', '.join(exposure.labels)}"
            )
        return accepted

    def _track_worker(
        self,
        attribute_name: str,
        worker: QThread,
        *,
        collection_name: Optional[str] = None,
    ) -> None:
        """Keep a QThread alive and request cleanup without using sender()."""
        if not isinstance(worker, QThread):
            # Lightweight test doubles do not have Qt ownership or affinity.
            worker.finished.connect(
                lambda current=worker: self._clear_worker_reference(
                    attribute_name, current
                )
            )
            return

        if worker.parent() is None:
            worker.setParent(self)
        tracked = getattr(self, "_tracked_workers", None)
        if tracked is None:
            tracked = {}
            self._tracked_workers = tracked
        if worker in tracked:
            return
        tracked[worker] = (attribute_name, collection_name)
        # A receiver-less lambda may run on the worker thread.  It only emits
        # a Qt signal; all object/reference mutation happens in the slot on the
        # MainWindow thread.  Passing the worker explicitly also avoids the
        # fragile QObject.sender() wrapper path seen in the native crash.
        worker.finished.connect(
            lambda current=worker: self.worker_cleanup_requested.emit(current)
        )

    @pyqtSlot(object)
    def _on_tracked_worker_finished(self, worker: QThread) -> None:
        tracked = getattr(self, "_tracked_workers", {})
        details = tracked.pop(worker, None)
        if details is None:
            return
        attribute_name, collection_name = details
        if collection_name:
            collection = getattr(self, collection_name, [])
            if worker in collection:
                collection.remove(worker)
        self._clear_worker_reference(attribute_name, worker)
        if (
            attribute_name == "_buyboard_runtime_worker"
            and self.__dict__.get("_buyboard_runtime_restart_requested", False)
            and not self.__dict__.get("_database_shutting_down", False)
        ):
            QTimer.singleShot(0, self._sync_buyboard_runtime_worker)

    def _clear_worker_reference(self, attribute_name: str, worker: QThread) -> None:
        cleared = getattr(self, attribute_name, None) is worker
        if cleared:
            setattr(self, attribute_name, None)
        try:
            worker.deleteLater()
        except (AttributeError, RuntimeError):
            pass

    def _setup_tabs(self):
        """Set up the tab views."""
        self.dashboard_widget = QWidget()
        self._add_configured_tab("dashboard", self.dashboard_widget, "Dashboard")
        self._build_dashboard_tab()

        self.scanner_widget = QWidget()
        self._add_configured_tab("scanner", self.scanner_widget, "Scanner")
        self._build_scanner_tab()

        self.market_pulse_widget = QWidget()
        self._add_configured_tab(
            "market_pulse", self.market_pulse_widget, "Market Pulse"
        )
        self._build_market_pulse_tab()

        # The Buy Board is the sole operator-facing execution surface. The
        # persisted buylist/execution-queue models remain compatibility inputs
        # for ORB calculation and state migration, but no legacy dashboard is
        # constructed or exposed.
        self.buyboard_widget = QWidget()
        self._add_configured_tab("buyboard", self.buyboard_widget, "Buy Board")
        self._build_buyboard_tab()
        # P0-1: attempt to start the engine worker now too -- covers the
        # case where this device is already main and the flag is already
        # on at startup, rather than waiting for the next state-sync pass.
        # A no-op whenever BUYBOARD_ENGINE_ENABLED is unset (the default).
        self._sync_buyboard_runtime_worker()

        self.tradingview_widget = QWidget()
        self._add_configured_tab(
            "tradingview", self.tradingview_widget, "TradingView Chart"
        )
        self._build_tradingview_tab()
        self._install_tradingview_watchlist_controls()

        self.health_widget = QWidget()
        self._add_configured_tab("health", self.health_widget, "Health")
        self._build_health_tab()

        self.intraday_charts_widget = QWidget()
        # A single empty, hidden combo preserves the one unguarded completion
        # callback in the shared chart controller. No retired chart view,
        # controls, web engine, shortcuts, or symbol population are created.
        self.intraday_symbol_combo = QComboBox(self.intraday_charts_widget)
        self.intraday_symbol_combo.setVisible(False)

        # Account selectors are built on Dashboard; populate the shared trade
        # account alias once after all tabs finish constructing.
        self.populate_trade_account_combo()

    def _add_configured_tab(self, key: str, widget: QWidget, label: str) -> None:
        if self.tab_options.get(key, True):
            self.tabs.addTab(widget, label)

    def _create_menu_bar(self):
        """Create the application menu bar."""
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        settings_action = file_menu.addAction("Settings")
        settings_action.triggered.connect(self.show_settings_dialog)
        save_action = file_menu.addAction("Save Local Data")
        save_action.triggered.connect(self.save_local_data)
        restore_action = file_menu.addAction("Restore from Cloud Backup...")
        restore_action.triggered.connect(self.show_restore_backup_dialog)
        file_menu.addSeparator()
        backup_env_action = file_menu.addAction("Backup .env to Cloud (Encrypted)...")
        backup_env_action.triggered.connect(self.show_backup_env_dialog)
        restore_env_action = file_menu.addAction("Restore .env from Cloud (Encrypted)...")
        restore_env_action.triggered.connect(self.show_restore_env_dialog)
        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        tools_menu = menubar.addMenu("Tools")
        self.chart_settings_action = tools_menu.addAction("Chart Settings")
        self.chart_settings_action.triggered.connect(
            self.show_chart_settings_dialog
        )
        self.orb_settings_action = tools_menu.addAction("ORB Settings")
        self.orb_settings_action.triggered.connect(self.show_orb_settings_dialog)
        tools_menu.addSeparator()
        refresh_db_action = tools_menu.addAction("Update 1D Data")
        refresh_db_action.triggered.connect(self.refresh_data_to_db)
        scan_action = tools_menu.addAction("Run All Scanners")
        scan_action.triggered.connect(self.run_all_scanners)

        help_menu = menubar.addMenu("Help")
        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

        # Combined container for the PC remote-control status + US market
        # status, both shown in the top-right corner (setCornerWidget only
        # accepts one widget per corner, so both sections live inside a
        # shared outer container).
        self.corner_status_widget = QWidget()
        outer_corner_layout = QHBoxLayout()
        outer_corner_layout.setContentsMargins(0, 0, 10, 0)
        outer_corner_layout.setSpacing(16)

        # -- Live-trading kill switch --
        # Placed first (leftmost / most prominent). Starts DISABLED every
        # launch by design (src/services/trading_state.py has no persistence);
        # enabling requires an explicit click-through confirmation, disabling
        # is instant. See PROJECT_ARCHITECTURE.md Production Safety Notes.
        self.trading_enabled_button = QPushButton()
        self.trading_enabled_button.setCheckable(True)
        self.trading_enabled_button.clicked.connect(self._on_trading_enabled_toggled)
        outer_corner_layout.addWidget(self.trading_enabled_button)
        self._refresh_trading_enabled_widget()

        # -- Active market-data database --
        # This is deliberately separate from the PC health indicator: the PC
        # can be offline while the dashboard continues from its local mirror.
        self.database_source_status_widget = QWidget()
        database_source_layout = QHBoxLayout()
        database_source_layout.setContentsMargins(0, 0, 0, 0)
        database_source_layout.setSpacing(6)

        self.database_source_dot = QLabel()
        self.database_source_dot.setFixedSize(10, 10)
        self.database_source_label = QLabel("DB: Checking...")
        database_source_layout.addWidget(self.database_source_dot)
        database_source_layout.addWidget(self.database_source_label)
        self.database_source_status_widget.setLayout(database_source_layout)
        self._update_database_source_indicator()
        outer_corner_layout.addWidget(self.database_source_status_widget)

        # -- Shared-data PC and service status --
        self.pc_status_widget = QWidget()
        pc_status_layout = QHBoxLayout()
        pc_status_layout.setContentsMargins(0, 0, 0, 0)
        pc_status_layout.setSpacing(6)

        self.pc_status_dot = QLabel()
        self.pc_status_dot.setFixedSize(10, 10)
        self.pc_status_dot.setStyleSheet(
            "border-radius: 5px; background-color: #787b86;"
        )

        self.pc_status_label = QLabel("PC: Checking...")
        self.pc_status_label.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #131722;"
        )
        self.pc_services_label = QLabel(
            "PC DB: Checking | Listener: Checking | main.py: Checking"
        )
        self.pc_services_label.setStyleSheet("font-size: 10px; color: #555555;")

        pc_status_text_layout = QVBoxLayout()
        pc_status_text_layout.setContentsMargins(0, 0, 0, 0)
        pc_status_text_layout.setSpacing(0)
        pc_status_text_layout.addWidget(self.pc_status_label)
        pc_status_text_layout.addWidget(self.pc_services_label)
        pc_status_layout.addWidget(self.pc_status_dot)
        pc_status_layout.addLayout(pc_status_text_layout)
        self.pc_status_widget.setLayout(pc_status_layout)

        outer_corner_layout.addWidget(self.pc_status_widget)

        # -- US market status (existing) --
        self.market_status_widget = QWidget()
        corner_layout = QHBoxLayout()
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(6)

        # Indicator circle (colored dot)
        self.market_status_dot = QLabel()
        self.market_status_dot.setFixedSize(10, 10)
        self.market_status_dot.setStyleSheet(
            "border-radius: 5px; background-color: #f23645;"
        )  # Default red

        # Text label
        self.market_status_label = QLabel("US Market: Calculating...")
        self.market_status_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #131722;"
        )

        corner_layout.addWidget(self.market_status_dot)
        corner_layout.addWidget(self.market_status_label)
        self.market_status_widget.setLayout(corner_layout)

        outer_corner_layout.addWidget(self.market_status_widget)
        self.corner_status_widget.setLayout(outer_corner_layout)

        menubar.setCornerWidget(self.corner_status_widget, Qt.TopRightCorner)

        # Poll DB, remote-control listener, and main.py independently. Network
        # and database I/O stay on a worker so the UI thread never blocks.
        self._pc_status_worker: Optional[PcRemoteStatusWorker] = None
        self.pc_status_timer = QTimer(self)
        self.pc_status_timer.setInterval(5000)
        self.pc_status_timer.timeout.connect(self._poll_pc_status)
        self.pc_status_timer.start()
        QTimer.singleShot(0, self._poll_pc_status)

        # Keep the offline copy close behind PC MySQL even when the PC's
        # scheduled refresh finishes after this dashboard first reconnects.
        self.local_mirror_sync_timer = QTimer(self)
        self.local_mirror_sync_timer.setInterval(LOCAL_MIRROR_SYNC_INTERVAL_MS)
        self.local_mirror_sync_timer.timeout.connect(
            self._sync_active_pc_to_local_mirror
        )
        self.local_mirror_sync_timer.timeout.connect(
            self._start_market_data_status_refresh
        )
        self.local_mirror_sync_timer.start()

        # Set up a 1-second timer to update the countdown
        self.market_status_timer = QTimer(self)
        self.market_status_timer.setInterval(1000)
        self.market_status_timer.timeout.connect(self.update_market_countdown_status)
        self.market_status_timer.start()
        self.update_market_countdown_status()

    def _read_authoritative_live_trading_control(self) -> bool:
        """Read the durable Kanban switch for a broker-boundary decision."""

        engine = self._execution_state_engine()
        if engine is None or not self._execution_state_ready():
            raise RuntimeError("Kanban operational store is unavailable")
        result = get_live_trading_control(engine)
        if not result.success or result.control is None:
            raise RuntimeError(
                result.error or "Kanban live-trading control is unavailable"
            )
        return bool(result.control.enabled)

    def _refresh_trading_enabled_widget(self) -> None:
        """Project the durable Kanban kill switch into the toolbar."""
        button = getattr(self, "trading_enabled_button", None)
        if button is None:
            return
        locked = trading_state.is_trading_locked_disabled()
        enabled = trading_state.is_trading_enabled()
        control_configured = bool(
            "operational_db_engine" in self.__dict__
            or "pc_db_engine" in self.__dict__
        )
        shared_available = bool(
            self.__dict__.get("_shared_live_trading_available", False)
        )
        update_worker = self.__dict__.get("live_trading_control_worker")
        update_running = self._background_worker_running(update_worker)
        button.blockSignals(True)
        try:
            button.setChecked(enabled)
            if update_running:
                button.setText("LIVE TRADING ● UPDATING KANBAN CONTROL...")
                button.setToolTip(
                    "The Kanban operational control update is being committed."
                )
                button.setEnabled(False)
                button.setStyleSheet(
                    "QPushButton { background-color: #6b4f00; color: white; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
            elif locked:
                button.setText("LIVE TRADING ● LOCKED OFF")
                button.setToolTip(
                    "TRADING_ENABLED is blank, false, or invalid in "
                    ".env/environment; this machine is administratively locked."
                )
                button.setEnabled(False)
                button.setStyleSheet(
                    "QPushButton { background-color: #363a45; color: #787b86; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
            elif control_configured and not shared_available:
                button.setText(
                    "LIVE TRADING ● ON (EMERGENCY ONLY)"
                    if enabled
                    else "LIVE TRADING ● CONTROL OFFLINE"
                )
                button.setToolTip(
                    "The Kanban operational control cannot be read. Ordinary "
                    "orders fail closed. A last-confirmed ON state is usable "
                    "only by the bounded emergency protective path."
                )
                button.setEnabled(False)
                button.setStyleSheet(
                    "QPushButton { background-color: #6b4f00; color: white; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
            elif enabled:
                button.setText("LIVE TRADING ● ON (KANBAN)")
                button.setToolTip(
                    "The durable broker gate is ON in this Kanban operational "
                    "store. Only its current Execution Owner can execute. Click to "
                    "turn it OFF."
                )
                button.setEnabled(True)
                button.setStyleSheet(
                    "QPushButton { background-color: #f23645; color: white; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
            else:
                button.setText("LIVE TRADING ● DISABLED (KANBAN)")
                button.setToolTip(
                    "Broker submission is blocked. Click to turn it ON in the "
                    "current Kanban operational store (confirmation required)."
                )
                button.setEnabled(True)
                button.setStyleSheet(
                    "QPushButton { background-color: #363a45; color: #d1d4dc; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
        finally:
            button.blockSignals(False)

    def _on_trading_enabled_toggled(self, checked: bool) -> None:
        """Set the broker gate in the current Kanban operational store."""
        if checked:
            reply = QMessageBox.question(
                self,
                "Enable Live Trading",
                "This turns guarded KIS order submission ON for this Kanban "
                "operational store. Only its current Execution Owner can execute. "
                "Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._refresh_trading_enabled_widget()
                return
        # Lightweight widget tests have no database. Real windows always use
        # the durable row and never silently fall back to process-local state.
        if (
            "operational_db_engine" not in self.__dict__
            and "pc_db_engine" not in self.__dict__
        ):
            trading_state.set_trading_enabled(bool(checked))
            self._refresh_trading_enabled_widget()
            return

        active_worker = self.__dict__.get("live_trading_control_worker")
        if active_worker is not None and active_worker.isRunning():
            self._refresh_trading_enabled_widget()
            return
        worker = LiveTradingControlWorker(
            self._execution_state_engine(),
            self.state_sync_role,
            bool(checked),
        )
        self.live_trading_control_worker = worker
        worker.completed.connect(self._on_live_trading_control_updated)
        self._track_worker("live_trading_control_worker", worker)
        button = self.trading_enabled_button
        button.setEnabled(False)
        button.setText("LIVE TRADING ● UPDATING KANBAN CONTROL...")
        worker.start()

    def _on_live_trading_control_updated(self, result) -> None:
        """Apply a confirmed shared-control write to this process projection."""

        if not result.success or result.control is None:
            self._shared_live_trading_available = False
            self._refresh_trading_enabled_widget()
            QMessageBox.warning(
                self,
                "Live Trading Control Unavailable",
                "The Kanban switch was not changed. Ordinary broker mutations "
                "remain fail-closed.\n\n"
                + (result.error or "Kanban operational store unavailable."),
            )
            return

        trading_state.set_trading_enabled(result.control.enabled)
        self._shared_live_trading_available = bool(
            self._execution_state_engine() is not None
            and self._execution_state_ready()
        )
        self._shared_live_trading_revision = result.control.revision

        self._refresh_trading_enabled_widget()
        effective = trading_state.is_trading_enabled()
        self.append_log(
            f"Live trading {'ENABLED' if effective else 'DISABLED'} "
            f"in the Kanban store by {self.state_sync_role.hostname} "
            f"(revision {result.control.revision})."
        )

    def _build_status_log(self, parent_layout: QVBoxLayout) -> None:
        """Build the shared dashboard log and progress widgets."""
        status_widget = QWidget()
        status_widget.setMaximumHeight(225)
        status_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        status_layout = QVBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(4)

        progress_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(16)
        self.progress_label = QLabel("Ready.")
        self.progress_label.setMaximumHeight(22)
        progress_layout.addWidget(self.progress_bar, 2)
        progress_layout.addWidget(self.progress_label, 3)

        owner_layout = QHBoxLayout()
        owner_layout.setSpacing(4)
        owner_layout.addWidget(QLabel("Execution Owner:"))
        self.execution_owner_pc_button = QPushButton("PC")
        self.execution_owner_laptop_button = QPushButton("Laptop")
        self.execution_owner_pc_button.clicked.connect(
            lambda: self._on_control_owner_clicked("execution", "PC")
        )
        self.execution_owner_laptop_button.clicked.connect(
            lambda: self._on_control_owner_clicked("execution", "Laptop")
        )
        owner_layout.addWidget(self.execution_owner_pc_button)
        owner_layout.addWidget(self.execution_owner_laptop_button)
        owner_layout.addSpacing(12)
        owner_layout.addWidget(QLabel("Operator Control:"))
        self.operator_control_pc_button = QPushButton("PC")
        self.operator_control_laptop_button = QPushButton("Laptop")
        self.operator_control_locked_button = QPushButton("Locked")
        self.operator_control_pc_button.clicked.connect(
            lambda: self._on_control_owner_clicked("operator", "PC")
        )
        self.operator_control_laptop_button.clicked.connect(
            lambda: self._on_control_owner_clicked("operator", "Laptop")
        )
        self.operator_control_locked_button.clicked.connect(
            lambda: self._on_control_owner_clicked("operator", "Locked")
        )
        owner_layout.addWidget(self.operator_control_pc_button)
        owner_layout.addWidget(self.operator_control_laptop_button)
        owner_layout.addWidget(self.operator_control_locked_button)
        owner_layout.addSpacing(8)
        self.publish_trading_plan_button = QPushButton("Publish Today's Plan")
        self.publish_trading_plan_button.clicked.connect(
            self._on_publish_trading_plan_clicked
        )
        owner_layout.addWidget(self.publish_trading_plan_button)
        self.control_ownership_status = QLabel("Control owners: verifying shared state...")
        self.control_ownership_status.setWordWrap(True)
        self.control_ownership_status.setStyleSheet(
            "font-size: 11px; color: #555; padding-left: 8px;"
        )
        owner_layout.addWidget(self.control_ownership_status, 1)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet(
            "background-color: black; color: white; font-family: Consolas, monospace; font-size: 11px;"
        )
        self.log_output.setMinimumHeight(70)
        self.log_output.setMaximumHeight(80)
        self.log_output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        status_layout.addLayout(progress_layout)
        status_layout.addLayout(owner_layout)
        status_layout.addWidget(self.log_output)
        status_widget.setLayout(status_layout)
        parent_layout.addWidget(status_widget)
        # Pull-only devices stay current while both applications remain open,
        # and former main devices promptly notice an ownership transfer.
        self.state_sync_timer = QTimer(self)
        # Shared planning/control display refresh. Actual operator commands,
        # broker-boundary authority checks, and handoff button actions use
        # their own immediate paths; unchanged JSON revisions need one cloud
        # check only after a dirty token, plus the configured recovery fallback.
        self.state_sync_timer.setInterval(
            int(execution_config.COORDINATION_STATE_SYNC_SECONDS * 1000)
        )
        self.state_sync_timer.timeout.connect(self._start_state_sync)
        self.state_sync_timer.start()

        # This is the hot pulse requested by the operator. It reads only
        # process memory and two tiny local files; it never polls TiDB.
        self.coordination_change_timer = QTimer(self)
        self.coordination_change_timer.setInterval(1_000)
        self.coordination_change_timer.timeout.connect(
            self._process_internal_coordination_pulse
        )
        self.coordination_change_timer.start()

        # Cross-process signal for the PC's guarded-sleep automation (see
        # src/services/sleep_readiness.py and scripts/Invoke-GuardedSleep.ps1)
        # -- PowerShell/Task Scheduler cannot inspect this running Qt process
        # directly, so a small JSON snapshot is written periodically instead.
        self.sleep_readiness_timer = QTimer(self)
        self.sleep_readiness_timer.setInterval(30_000)
        self.sleep_readiness_timer.timeout.connect(self._write_sleep_readiness_snapshot)
        self.sleep_readiness_timer.start()

        # Read-only projection of the authoritative runtime readiness gates.
        # This shares the existing progress bar, but yields to user-started
        # scanner/refresh/mirror work so those task-specific percentages and
        # ETAs remain visible while they run.
        self.buyboard_readiness_progress_timer = QTimer(self)
        self.buyboard_readiness_progress_timer.setInterval(1_000)
        self.buyboard_readiness_progress_timer.timeout.connect(
            self._update_buyboard_readiness_progress
        )
        self.buyboard_readiness_progress_timer.start()
        self._update_buyboard_readiness_progress()

    @staticmethod
    def _control_device_kind(hostname: str, details=None) -> str:
        return runtime_device_kind(hostname, details)

    @staticmethod
    def _control_runtime_identity_available(record) -> bool:
        return _control_runtime_identity_available(record)

    def _control_identity_kind(
        self, *, device_id: str = "", hostname: str = ""
    ) -> str:
        records = list(self.__dict__.get("_runtime_device_records", ()) or ())
        exact = [
            record
            for record in records
            if device_id and str(record.device_id) == str(device_id)
        ]
        candidates = exact or [
            record
            for record in records
            if hostname
            and str(record.hostname).strip().lower()
            == str(hostname).strip().lower()
        ]
        if candidates:
            record = max(candidates, key=lambda item: item.updated_at)
            return self._control_device_kind(record.hostname, record.details)
        role = self.__dict__.get("state_sync_role")
        if role is not None and (
            (device_id and str(role.device_id) == str(device_id))
            or (
                hostname
                and str(role.hostname).strip().lower()
                == str(hostname).strip().lower()
            )
        ):
            return detect_local_device_kind(role.hostname)
        return self._control_device_kind(hostname)

    def _control_target_role(self, target_label: str) -> Optional[LocalDeviceRole]:
        if target_label == "Locked":
            return None
        records = self.__dict__.get("_runtime_device_records", ()) or ()
        target = _control_target_role_from_records(records, target_label)
        if target is not None:
            return target
        # A device-kind guess from this process's hardware is not a published
        # target identity. In particular, battery-less CI/VM hosts classify a
        # DESKTOP-named laptop role as PC. Ownership changes require a fresh
        # runtime row with an explicit device_kind and eligible state; without
        # that row the button must report the target unavailable.
        return None

    def _set_control_owner_buttons_enabled(self, enabled: bool) -> None:
        for name in (
            "execution_owner_pc_button",
            "execution_owner_laptop_button",
            "operator_control_pc_button",
            "operator_control_laptop_button",
            "operator_control_locked_button",
        ):
            button = self.__dict__.get(name)
            if button is not None:
                button.setEnabled(bool(enabled))

    def _on_control_owner_clicked(self, control: str, target_label: str) -> None:
        if not self._execution_state_ready():
            QMessageBox.warning(
                self,
                "Shared control unavailable",
                "The shared coordination database is unavailable. Ownership "
                "cannot be changed, and execution remains fail-closed. If TiDB "
                "is configured, verify its SQL endpoint, TLS, and Internet connection.",
            )
            return
        target = self._control_target_role(target_label)
        worker = self.__dict__.get("control_owner_worker")
        if worker is not None and worker.isRunning():
            return
        worker = ControlOwnerWorker(
            self._execution_state_engine(),
            self.state_sync_role,
            control=control,
            target=target,
            target_label=target_label,
        )
        self.control_owner_worker = worker
        worker.completed.connect(self._on_control_owner_updated)
        self._track_worker("control_owner_worker", worker)
        self._set_control_owner_buttons_enabled(False)
        worker.start()

    def _on_control_owner_updated(self, update: ControlOwnerUpdate) -> None:
        self._set_control_owner_buttons_enabled(True)
        if not update.success:
            QMessageBox.warning(
                self,
                "Owner switch blocked",
                update.error or "The shared owner did not change.",
            )
            self.append_log(update.error or "Control-owner switch was blocked.")
            return
        label = "Execution Owner" if update.control == "execution" else "Operator Control"
        self.append_log(f"{label} changed to {update.target_label}.")
        self._start_state_sync()

    def _on_publish_trading_plan_clicked(self) -> None:
        if is_regular_session_open():
            QMessageBox.information(
                self,
                "Market-open planning lock",
                "Market is open. Full plan publish is disabled. Use Live "
                "Intervention commands instead.",
            )
            return
        if not self._execution_state_ready():
            QMessageBox.warning(
                self,
                "Publish unavailable",
                "The shared coordination database is unavailable.",
            )
            return
        worker = self.__dict__.get("plan_publish_worker")
        if worker is not None and worker.isRunning():
            return
        queue_manager = self.__dict__.get("execution_queue_manager")
        if queue_manager is not None:
            self._save_execution_queue_state()
        saved = self._save_state_now(timeout=5.0, supersede_pending=True)
        if not saved.success:
            QMessageBox.warning(
                self,
                "Publish paused",
                saved.error or "The local planning files could not be saved.",
            )
            return
        worker = PlanPublishWorker(
            self._execution_state_engine(),
            self.state_sync_role,
            self._state_save_payload(),
            load_json(EXECUTION_QUEUE_FILE, {}),
            metadata_path=self._execution_state_metadata_path(),
            market_is_open=False,
        )
        self.plan_publish_worker = worker
        worker.completed.connect(self._on_trading_plan_published)
        self._track_worker("plan_publish_worker", worker)
        self.publish_trading_plan_button.setEnabled(False)
        self.publish_trading_plan_button.setText("Publishing...")
        worker.start()

    def _on_trading_plan_published(self, result) -> None:
        self.publish_trading_plan_button.setEnabled(True)
        self.publish_trading_plan_button.setText("Publish Today's Plan")
        if not result.success:
            QMessageBox.warning(
                self,
                "Plan publish blocked",
                result.error or "The shared planning snapshot did not verify.",
            )
            self.append_log(result.error or "Plan publish failed verification.")
            return
        revisions = result.revisions
        summary = (
            f"planning snapshot r{revisions.get('buylist', 0)}/"
            f"{revisions.get('trade_plans', 0)}, "
            f"execution queue r{revisions.get('execution_queue', 0)}, "
            f"compatibility state r{revisions.get('watchlist', 0)}"
        )
        if result.execution_owner_heartbeat_fresh:
            owner = result.execution_owner_hostname or "Execution Owner"
            message = f"{owner} has latest plan ({summary})."
            self.append_log(message)
            QMessageBox.information(self, "Plan published", message)
        else:
            owner = result.execution_owner_hostname or "Execution Owner"
            message = (
                f"The plan is verified in shared MySQL ({summary}), but {owner}'s "
                "main.py heartbeat is not fresh. Confirm the executor before market open."
            )
            self.append_log(message)
            QMessageBox.warning(self, "Plan published; executor not verified", message)
        self._start_state_sync()

    def _has_cached_local_operator_control(self) -> bool:
        """Use only the last background-verified control row for plan edits."""

        control = self.__dict__.get("_cached_operator_control")
        role = self.__dict__.get("state_sync_role")
        return bool(
            control is not None
            and role is not None
            and not bool(getattr(control, "locked", True))
            and str(getattr(control, "device_id", "") or "").strip()
            == str(getattr(role, "device_id", "") or "").strip()
            and str(getattr(role, "device_id", "") or "").strip()
        )

    def _refresh_control_ownership_status(self, result: StateReconcileResult) -> None:
        # ORB planning actions are synchronous UI gestures.  They must never
        # issue a blocking ownership query on the UI thread, so retain the
        # latest result from the background state-sync worker and fail closed
        # whenever that result was unavailable.
        self._cached_operator_control = (
            result.operator_control
            if not str(getattr(result, "operator_control_error", "") or "").strip()
            else None
        )
        self._cached_operator_control_verified_at = result.last_verified_at
        label = self.__dict__.get("control_ownership_status")
        if label is None:
            return
        self._runtime_device_records = tuple(result.runtime_devices or ())
        execution_host = result.main_device_hostname or "Unassigned"
        execution_label = (
            self._control_identity_kind(hostname=execution_host)
            if execution_host != "Unassigned"
            else execution_host
        )
        operator = result.operator_control
        if operator is None or bool(getattr(operator, "locked", True)):
            operator_label = "Locked"
        else:
            operator_label = self._control_identity_kind(
                device_id=operator.device_id,
                hostname=operator.hostname,
            )
        readiness = {"PC": "No", "Laptop": "No"}
        readiness_reason = {}
        newest_by_kind = {}
        for record in self._runtime_device_records:
            kind = self._control_device_kind(record.hostname, record.details)
            current = newest_by_kind.get(kind)
            if current is None or record.updated_at > current.updated_at:
                newest_by_kind[kind] = record
        for kind, record in newest_by_kind.items():
            ready = bool(record.details.get("executor_ready", False))
            readiness[kind] = "Yes" if ready else "No"
            if not ready:
                readiness_reason[kind] = str(
                    record.details.get("executor_not_ready_reason") or record.state.value
                )
        revisions = dict(result.state_revisions or {})
        verified = result.last_verified_at
        if isinstance(verified, dt.datetime):
            verified_text = verified.astimezone(KST_ZONE).strftime("%H:%M:%S KST")
        else:
            verified_text = "-"
        live_text = _live_execution_status_text(result.live_trading_enabled)
        commands = list(result.operator_commands or ())
        if commands:
            newest = commands[0]
            command_text = f"{newest.command_type.value}:{newest.status.value}"
        else:
            command_text = "None"
        text_value = (
            f"Current Execution Owner: {execution_label} | "
            f"Current Operator Control: {operator_label} | "
            f"PC Executor Ready: {readiness['PC']} | "
            f"Laptop Executor Ready: {readiness['Laptop']} | "
            f"Live Trading: {live_text} | "
            f"Plan/Queue Revisions: {revisions.get('buylist', 0)}/"
            f"{revisions.get('execution_queue', 0)} | Verified: {verified_text}"
            f" | Last Command: {command_text}"
        )
        label.setText(text_value)
        reason_text = "\n".join(
            f"{kind}: {reason}" for kind, reason in sorted(readiness_reason.items())
        )
        command_history = "\n".join(
            f"{item.created_at.isoformat()} {item.command_type.value} "
            f"{item.symbol} {item.status.value}"
            for item in commands
        )
        tooltip_sections = [text_value, reason_text, command_history]
        label.setToolTip("\n".join(item for item in tooltip_sections if item))

        active_style = "background-color: #137333; color: white; font-weight: bold;"
        inactive_style = ""
        self.execution_owner_pc_button.setStyleSheet(
            active_style if execution_label == "PC" else inactive_style
        )
        self.execution_owner_laptop_button.setStyleSheet(
            active_style if execution_label == "Laptop" else inactive_style
        )
        self.operator_control_pc_button.setStyleSheet(
            active_style if operator_label == "PC" else inactive_style
        )
        self.operator_control_laptop_button.setStyleSheet(
            active_style if operator_label == "Laptop" else inactive_style
        )
        self.operator_control_locked_button.setStyleSheet(
            active_style if operator_label == "Locked" else inactive_style
        )

    def _write_sleep_readiness_snapshot(self) -> None:
        try:
            write_sleep_readiness_snapshot(self)
        except Exception:
            logger.debug("Sleep-readiness snapshot write failed", exc_info=True)

    def append_log(self, message: str) -> None:
        """Request an in-app log update from any thread."""
        text = str(message)
        # A shared legacy intraday worker still carries old internal naming.
        # Keep those implementation labels out of the operator-facing log now
        # that the former dedicated Watchlist tab has been removed.
        text = text.replace(
            "Intraday watchlist refresh requires",
            "Intraday refresh requires",
        ).replace("watchlist symbols", "planning symbols")
        self.log_message_requested.emit(text)

    @pyqtSlot(str)
    def _append_log_on_ui_thread(self, message: str) -> None:
        """Append a log line on the window's Qt thread."""
        if not hasattr(self, "log_output"):
            return
        timestamp = pd.Timestamp.now().strftime("%H:%M:%S")
        self.log_output.append(f"[{timestamp}] {message}")
        self.log_output.verticalScrollBar().setValue(
            self.log_output.verticalScrollBar().maximum()
        )

    def _log_intraday_provider_warning(self, symbol: str, warning: str) -> None:
        warning_text = str(warning or "").strip()
        if not warning_text:
            return
        if warning_text == "KIS intraday disabled/unconfigured.":
            emitted_keys = getattr(self, "_intraday_provider_warning_log_keys", set())
            if warning_text in emitted_keys:
                return
            emitted_keys.add(warning_text)
            self._intraday_provider_warning_log_keys = emitted_keys
            self.append_log(
                "Intraday provider notice: KIS intraday disabled/unconfigured; using yfinance fallback."
            )
            return
        self.append_log(f"Intraday provider warning for {symbol}: {warning_text}")

    def update_progress(self, percent: int, current: int, total: int, eta: str) -> None:
        self.progress_bar.setValue(percent)
        self.progress_label.setText(
            f"Fetching {current}/{total} ({percent}%) - ETA {eta}"
        )

    def show_ready(self) -> None:
        self.progress_bar.setValue(0)
        self.progress_label.setText("Ready.")

    def show_refresh_error(self, message: str) -> None:
        self.append_log(f"Error: {message}")
        self.progress_label.setText("Refresh failed.")

    def show_refresh_complete(self, updated_count: int) -> None:
        self.append_log(f"Refresh complete: {updated_count} symbols updated.")
        self.progress_label.setText("Refresh complete.")

    @staticmethod
    def _nyse_holidays(year: int) -> set:
        """Return the set of NYSE observed holiday dates for the given year."""

        def nearest_weekday(d: dt.date) -> dt.date:
            if d.weekday() == 5:  # Saturday → Friday
                return d - dt.timedelta(days=1)
            if d.weekday() == 6:  # Sunday → Monday
                return d + dt.timedelta(days=1)
            return d

        def easter(y: int) -> dt.date:
            # Anonymous Gregorian algorithm
            a = y % 19
            b, c = divmod(y, 100)
            d, e = divmod(b, 4)
            f = (b + 8) // 25
            g = (b - f + 1) // 3
            h = (19 * a + b - d - g + 15) % 30
            i, k = divmod(c, 4)
            l = (32 + 2 * e + 2 * i - h - k) % 7
            m = (a + 11 * h + 22 * l) // 451
            month, day = divmod(114 + h + l - 7 * m, 31)
            return dt.date(y, month, day + 1)

        def nth_weekday(y: int, month: int, weekday: int, n: int) -> dt.date:
            first = dt.date(y, month, 1)
            delta = (weekday - first.weekday()) % 7
            return first + dt.timedelta(days=delta + 7 * (n - 1))

        def last_weekday(y: int, month: int, weekday: int) -> dt.date:
            last = dt.date(y, month + 1, 1) - dt.timedelta(days=1)
            delta = (last.weekday() - weekday) % 7
            return last - dt.timedelta(days=delta)

        holidays = {
            nearest_weekday(dt.date(year, 1, 1)),  # New Year's Day
            nth_weekday(year, 1, 0, 3),  # MLK Day (3rd Monday Jan)
            nth_weekday(year, 2, 0, 3),  # Presidents' Day (3rd Monday Feb)
            easter(year) - dt.timedelta(days=2),  # Good Friday
            last_weekday(year, 5, 0),  # Memorial Day (last Monday May)
            nearest_weekday(dt.date(year, 6, 19)),  # Juneteenth
            nearest_weekday(dt.date(year, 7, 4)),  # Independence Day
            nth_weekday(year, 9, 0, 1),  # Labor Day (1st Monday Sep)
            nth_weekday(year, 11, 3, 4),  # Thanksgiving (4th Thursday Nov)
            nearest_weekday(dt.date(year, 12, 25)),  # Christmas
        }
        # New Year's Day observed in the following year when Jan 1 is Saturday
        if (
            dt.date(year, 12, 31).weekday() == 6
        ):  # Dec 31 is Sunday → Jan 1 next year is Monday
            holidays.add(dt.date(year, 12, 31))
        return holidays

    def update_market_countdown_status(self) -> None:
        """Update the market status countdown label (US Market hours)."""
        if not hasattr(self, "market_status_label"):
            return

        now_ny = dt.datetime.now(US_MARKET_ZONE)
        weekday = now_ny.weekday()
        today = now_ny.date()

        is_holiday = today in self._nyse_holidays(today.year)
        market_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        is_open = (
            weekday < 5 and not is_holiday and market_open <= now_ny < market_close
        )
        was_open = getattr(self, "_market_was_open", None)
        if (
            was_open
            and not is_open
            and hasattr(self, "_deactivate_pre_entry_orb_monitoring")
        ):
            self._deactivate_pre_entry_orb_monitoring()
        self._market_was_open = is_open

        dot = getattr(self, "market_status_dot", None)

        def _set_dot_open():
            if dot:
                dot.setStyleSheet("border-radius: 5px; background-color: #26a69a;")

        def _set_dot_closed():
            if dot:
                dot.setStyleSheet("border-radius: 5px; background-color: #f23645;")

        if weekday >= 5:
            _set_dot_closed()
            self.market_status_label.setText("<b>Market Status:</b> Closed (Weekend)")
            return

        if is_holiday:
            _set_dot_closed()
            self.market_status_label.setText("<b>Market Status:</b> Closed (Holiday)")
            return

        if now_ny < market_open:
            _set_dot_closed()
            diff = market_open - now_ny
            seconds = int(diff.total_seconds())
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.market_status_label.setText(
                f"<b>Market Status:</b> Closed (Opens in {hours:02d}:{minutes:02d}:{seconds:02d})"
            )
        elif now_ny < market_close:
            _set_dot_open()
            diff = market_close - now_ny
            seconds = int(diff.total_seconds())
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.market_status_label.setText(
                f"<b>Market Status:</b> <font color='#009688'><b>OPEN</b></font> (Closes in {hours:02d}:{minutes:02d}:{seconds:02d})"
            )
        else:
            _set_dot_closed()
            if weekday == 4:
                self.market_status_label.setText(
                    "<b>Market Status:</b> Closed (Weekend)"
                )
            else:
                self.market_status_label.setText(
                    "<b>Market Status:</b> Closed (After Hours)"
                )

    def _update_database_source_indicator(self) -> None:
        """Show which database currently serves dashboard market-data reads."""
        dot = self.__dict__.get("database_source_dot")
        label = self.__dict__.get("database_source_label")
        if dot is None or label is None:
            return

        source = self.__dict__.get("db_engine_source", "none")
        enabled = bool(
            self.__dict__.get("db_enabled", False)
            and self.__dict__.get("db_engine") is not None
        )
        if self.__dict__.get("db_initializing", False):
            text_value = "DB: Checking..."
            dot_color = "#787b86"
            text_color = "#555555"
            tooltip = "Checking which market-data database is available."
        elif enabled and source == "pc":
            text_value = "DB: PC"
            dot_color = "#26a69a"
            text_color = "#137333"
            tooltip = "Market-data reads are using the PC MySQL database."
        elif enabled and source == "local_mirror":
            reconciling = bool(
                self.__dict__.get("_database_reconciliation_in_progress", False)
            )
            text_value = "DB: Local (Syncing...)" if reconciling else "DB: Local"
            dot_color = "#ffb300"
            text_color = "#9a6700"
            online_coordination = self._execution_state_ready()
            tooltip = (
                "Market-data reads remain on the laptop's local SQLite mirror "
                "while PC/local market data is reconciled. Shared online "
                "coordination remains available for ownership and execution."
                if online_coordination
                else (
                    "Market-data reads remain on the laptop's local SQLite mirror "
                    "while PC/local market data is reconciled. The shared "
                    "coordination database is offline, so new entries and operator "
                    "commands remain closed."
                )
                if reconciling
                else (
                    "Market-data reads are using the laptop's local SQLite "
                    "mirror. Shared online coordination remains available for "
                    "ownership and execution."
                    if online_coordination
                    else (
                        "Market-data reads are using the laptop's local SQLite "
                        "mirror. The shared coordination database is offline: new "
                        "entries and operator commands are closed; an already-active "
                        "runtime has only bounded emergency position protection."
                    )
                )
            )
        else:
            text_value = "DB: Offline"
            dot_color = "#f23645"
            text_color = "#b42318"
            tooltip = "No usable market-data database is currently connected."

        dot.setStyleSheet(
            f"border-radius: 5px; background-color: {dot_color};"
        )
        label.setText(text_value)
        label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {text_color};"
        )
        dot.setToolTip(tooltip)
        label.setToolTip(tooltip)

    def _switch_to_runtime_local_mirror(self) -> None:
        """Route market-data reads locally after the connected PC disappears."""
        if self.__dict__.get("_database_shutting_down", False):
            return
        was_pc_active = bool(
            self.__dict__.get("_pc_database_ready", False)
            or self.__dict__.get("db_engine_source", "none") == "pc"
        )
        if not was_pc_active:
            return

        current_pc_engine = self.__dict__.get("pc_db_engine")
        if self.__dict__.get("_pc_probe_engine") is None:
            self._pc_probe_engine = current_pc_engine
        self._pc_database_ready = False
        self._database_reconciliation_in_progress = False
        self.pc_db_engine = None
        self.db_engine = None
        self.db_engine_source = "none"
        self.db_enabled = False
        self._update_database_source_indicator()
        if self.__dict__.get("trading_enabled_button") is not None:
            self._refresh_trading_enabled_widget()
        self._database_transition_generation = (
            self.__dict__.get("_database_transition_generation", 0) + 1
        )
        execution_engine = self._execution_state_engine()
        if execution_engine is not None:
            try:
                self._bind_remote_state_engine(
                    execution_engine,
                    is_main_device=bool(
                        self.state_sync_role.is_main
                        and self._using_local_operational_authority()
                    ),
                )
            except Exception:
                logger.exception("Could not retain shared Kanban persistence")
        else:
            self._pc_database_coordination_ready = False
            self._shared_live_trading_available = False
            self._initial_state_sync_complete = False
            try:
                self._bind_remote_state_engine(None, is_main_device=False)
            except Exception:
                logger.exception(
                    "Could not detach remote state persistence during failover"
                )

        local_engine = self.__dict__.get("_local_mirror_engine")
        if local_engine is None:
            from src.infrastructure.database.mirror_engine import (
                _local_mirror_enabled, init_local_mirror_engine)

            if _local_mirror_enabled():
                local_engine = init_local_mirror_engine()
            self._local_mirror_engine = local_engine

        if local_engine is not None:
            self.db_engine = local_engine
            self.db_engine_source = "local_mirror"
            self.db_enabled = True
            if self._execution_state_ready():
                self.append_log(
                    "PC historical database went offline; switched to the local "
                    "data mirror. Shared coordination and execution remain online."
                )
            else:
                self.append_log(
                    "PC database went offline; switched automatically to the local data mirror. "
                    "Shared coordination and new entries are closed until it recovers."
                )
        else:
            self.db_engine = None
            self.db_engine_source = "none"
            self.db_enabled = False
            self.append_log(
                "PC database went offline and no local data mirror is available; "
                "cached market-data features are temporarily disabled."
            )

        self._update_database_source_indicator()
        self._cached_market_data_status = None
        self._start_market_data_status_refresh(force=True)
        self.update_dashboard_summary()

    def _activate_recovered_pc_database(self, engine) -> None:
        """Route to a reachable PC immediately; refresh the backup afterward."""
        if engine is None or self.__dict__.get("_database_shutting_down", False):
            return
        if (
            self.__dict__.get("_pc_database_ready", False)
            and self.__dict__.get("pc_db_engine") is engine
            and self.__dict__.get("db_engine_source", "none") == "pc"
        ):
            return

        previous_source = self.__dict__.get("db_engine_source", "none")
        self._database_transition_generation = (
            self.__dict__.get("_database_transition_generation", 0) + 1
        )
        self._database_reconciliation_in_progress = False
        self._last_database_reconciliation_notice = ""
        self._pc_probe_engine = engine
        self.pc_db_engine = engine
        self.db_engine = engine
        self.db_engine_source = "pc"
        self.db_enabled = True
        self._pc_database_ready = True
        if (
            not self._using_local_operational_authority()
            and not self.__dict__.get("_coordination_database_configured", False)
        ):
            self._pc_database_coordination_ready = False
        self._last_pc_database_probe_ready = True
        if (
            not self._using_local_operational_authority()
            and not self.__dict__.get("_coordination_database_configured", False)
        ):
            self._initial_state_sync_complete = False
        self._cached_market_data_status = None
        self._update_database_source_indicator()
        try:
            # Historical database recovery must never replace or demote the
            # dedicated Kanban operational store.
            execution_engine = self._execution_state_engine()
            self._bind_remote_state_engine(
                execution_engine,
                is_main_device=bool(
                    self.state_sync_role.is_main
                    and self._using_local_operational_authority()
                ),
            )
        except Exception:
            logger.exception("Could not attach recovered remote state persistence")

        if previous_source != "pc":
            self.append_log(
                "PC database is back online; switched automatically from the local mirror."
            )
        try:
            self.update_dashboard_summary()
            self._start_market_data_status_refresh(force=True)
            if not self._using_local_operational_authority():
                self._start_state_sync()
            self._start_background_local_mirror_sync(engine)
        except Exception:
            # This method runs from Qt signal handlers.  A secondary UI or
            # worker-start failure must not unwind through Qt and close the app.
            logger.exception("PC database recovery follow-up failed")

    def _start_database_recovery(self, pc_engine=None) -> None:
        """Verify a newly reachable PC without inspecting the local backup."""
        if self.__dict__.get("_database_shutting_down", False):
            return
        if self.__dict__.get("db_engine_source", "none") == "pc":
            return
        worker = self.__dict__.get("database_recovery_worker")
        if worker is not None:
            return
        generation = self.__dict__.get("_database_transition_generation", 0)
        if pc_engine is not None:
            self._pc_probe_engine = pc_engine
        worker = DatabaseRecoveryWorker(
            generation,
            pc_engine=pc_engine,
        )
        self.database_recovery_worker = worker
        worker.recovered.connect(self._on_database_recovery_finished)
        self._track_worker("database_recovery_worker", worker)
        worker.start()

    def _on_database_recovery_finished(self, outcome, generation: int) -> None:
        if not isinstance(outcome, DatabaseRecoveryOutcome):
            outcome = DatabaseRecoveryOutcome(outcome, outcome is not None)
        engine = outcome.engine
        self._database_reconciliation_in_progress = False
        self._update_database_source_indicator()
        current_generation = self.__dict__.get(
            "_database_transition_generation", 0
        )
        if (
            self.__dict__.get("_database_shutting_down", False)
            or generation != current_generation
            or not self.__dict__.get("_last_pc_database_probe_ready", False)
        ):
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass
            return
        if not outcome.success or engine is None:
            if engine is not None:
                self._pc_probe_engine = engine
            detail = outcome.error or "PC MySQL connection check failed."
            notice = f"failed:{detail}"
            if self.__dict__.get("_last_database_reconciliation_notice") != notice:
                self._last_database_reconciliation_notice = notice
                self.append_log(
                    "PC database connection is unavailable; continuing with "
                    f"the local database and retrying automatically. {detail}"
                )
            return
        try:
            self._activate_recovered_pc_database(engine)
        except Exception:
            logger.exception("Could not activate recovered PC database engine")
            self._pc_probe_engine = engine
            self.append_log(
                "PC database activation failed; continuing with the local "
                "database and retrying automatically."
            )
            if self.__dict__.get("pc_db_engine") is not engine:
                try:
                    engine.dispose()
                except Exception:
                    pass

    def _configure_coordination_fallback_timers(self, pulse_supported: bool) -> None:
        """Back off TiDB display polls while Tailscale change pulses are live."""

        state_timer = self.__dict__.get("state_sync_timer")
        board_timer = self.__dict__.get("_buyboard_projection_timer")
        state_seconds = (
            execution_config.COORDINATION_REMOTE_FALLBACK_SECONDS
            if pulse_supported
            else execution_config.COORDINATION_STATE_SYNC_SECONDS
        )
        board_seconds = (
            execution_config.COORDINATION_REMOTE_FALLBACK_SECONDS
            if pulse_supported
            else execution_config.COORDINATION_BOARD_PROJECTION_SECONDS
        )
        if state_timer is not None:
            state_timer.setInterval(int(state_seconds * 1000))
        if board_timer is not None:
            board_timer.setInterval(int(board_seconds * 1000))

    def _on_remote_coordination_change(
        self, changed_tables: tuple[str, ...] = ()
    ) -> None:
        """Perform one canonical read pass for a newly observed remote token."""

        if self.__dict__.get("_database_shutting_down", False):
            return
        tables = {str(table or "").lower() for table in changed_tables if table}
        broad_fallback = not tables
        if broad_fallback or tables & {
            "app_state_sync",
            "runtime_device_state",
            "operator_commands",
        }:
            self._remote_coordination_sync_pending = True
            self._drain_remote_coordination_sync()
        if broad_fallback or tables & {
            "trade_cards",
            "execution_orders",
            "execution_ownership",
            "discovered_external_orders",
        }:
            refresh = getattr(self, "refresh_buyboard", None)
            if callable(refresh):
                refresh(revision_only=True)

    def _drain_remote_coordination_sync(self) -> None:
        if (
            self.__dict__.get("_database_shutting_down", False)
            or not self.__dict__.get("_remote_coordination_sync_pending", False)
        ):
            return
        worker = self.__dict__.get("state_sync_worker")
        if worker is not None:
            try:
                if worker.isRunning():
                    QTimer.singleShot(250, self._drain_remote_coordination_sync)
                    return
            except RuntimeError:
                pass
        self._remote_coordination_sync_pending = False
        self._start_state_sync()

    def _process_internal_coordination_pulse(self) -> None:
        """Route local dirty generations without issuing a database query."""

        if (
            self.__dict__.get("_database_shutting_down", False)
            or not self.__dict__.get("_coordination_database_ready", False)
        ):
            return
        engine = self.__dict__.get("coordination_db_engine")
        role = self.__dict__.get("state_sync_role")
        if engine is None or role is None:
            return
        from src.services.coordination_change_pulse import (
            acknowledge_local_change_event,
            mark_remote_coordination_change,
            publish_outbound_change_pulse,
            read_inbound_change_event,
            stage_local_coordination_change,
        )

        kind = detect_local_device_kind(getattr(role, "hostname", platform.node()))
        change = stage_local_coordination_change(
            engine, device_id=role.device_id
        )
        if kind == "PC":
            if change.event_id and publish_outbound_change_pulse(
                change.event_id, tables=change.tables
            ):
                acknowledge_local_change_event(engine, change.event_id)
            inbound_event = read_inbound_change_event()
            inbound_event_id = inbound_event.event_id
            if (
                inbound_event_id
                and inbound_event_id
                != self.__dict__.get("_last_inbound_coordination_event_id", "")
            ):
                self._last_inbound_coordination_event_id = inbound_event_id
                if mark_remote_coordination_change(
                    engine,
                    inbound_event_id,
                    tables=inbound_event.tables,
                ):
                    self._on_remote_coordination_change(inbound_event.tables)
            return

        # Laptop delivery is piggy-backed on the existing asynchronous PC
        # status worker, so the one-second Qt pulse never performs socket I/O.
        if change.event_id and self.__dict__.get("_pc_status_worker") is None:
            self._poll_pc_status()

    def _poll_pc_status(self) -> None:
        """Kick off a background check of the always-on PC's status.

        Keep the completed worker referenced until Qt delivers ``finished``.
        Replacing a QThread merely because ``isRunning()`` became false can
        destroy its wrapper while queued signals are still being dispatched.
        """
        if self.__dict__.get("_database_shutting_down", False):
            return
        if self.__dict__.get("_coordination_database_configured", False):
            self._start_coordination_database_initialization()
        self._start_coordination_runtime_heartbeat()
        if self.__dict__.get("database_recovery_worker") is not None:
            return
        if self._pc_status_worker is not None:
            return
        probe_engine = self.__dict__.get("_pc_probe_engine")
        if probe_engine is None:
            probe_engine = self.pc_db_engine
        pending_event_id = ""
        pending_event_tables: tuple[str, ...] = ()
        role = self.__dict__.get("state_sync_role")
        execution_engine = (
            self._execution_state_engine()
            if self.__dict__.get("_coordination_database_configured", False)
            else None
        )
        if (
            role is not None
            and execution_engine is not None
            and detect_local_device_kind(
                getattr(role, "hostname", platform.node())
            )
            == "Laptop"
        ):
            from src.services.coordination_change_pulse import (
                stage_local_coordination_change,
            )

            pending_change = stage_local_coordination_change(
                execution_engine, device_id=role.device_id
            )
            pending_event_id = pending_change.event_id
            pending_event_tables = pending_change.tables
        worker = PcRemoteStatusWorker(
            probe_engine,
            coordination_notification_event_id=pending_event_id,
            coordination_notification_tables=pending_event_tables,
            parent=self,
        )
        self._pc_status_worker = worker
        worker.finished_status.connect(self._on_pc_status_result)
        self._track_worker("_pc_status_worker", worker)
        worker.start()

    def _on_pc_status_worker_finished(self, worker) -> None:
        """Release a status worker only after Qt has finished its thread."""
        self._clear_worker_reference("_pc_status_worker", worker)

    def _on_pc_status_result(self, status) -> None:
        from src.services.pc_remote_control import PcStatus

        if self.__dict__.get("_database_shutting_down", False):
            return
        dot = getattr(self, "pc_status_dot", None)
        label = getattr(self, "pc_status_label", None)
        services_label = getattr(self, "pc_services_label", None)
        button = getattr(self, "pc_status_button", None)
        if dot is None or label is None or services_label is None:
            return

        listener_on = status.listener_status == PcStatus.ON
        pulse_supported = bool(
            listener_on
            and getattr(status, "coordination_change_pulse_supported", False)
        )
        execution_engine = (
            self._execution_state_engine()
            if self.__dict__.get("_coordination_database_configured", False)
            else None
        )
        from src.services.coordination_change_pulse import (
            acknowledge_local_change_event,
            mark_remote_coordination_change,
            set_change_notifications_available,
        )

        set_change_notifications_available(execution_engine, pulse_supported)
        self._configure_coordination_fallback_timers(pulse_supported)
        delivered_event_id = str(
            getattr(status, "coordination_notification_event_id", "") or ""
        )
        if bool(
            getattr(status, "coordination_notification_delivered", False)
        ) and delivered_event_id:
            acknowledge_local_change_event(execution_engine, delivered_event_id)
        role = self.__dict__.get("state_sync_role")
        remote_event_id = str(
            getattr(status, "coordination_change_event_id", "") or ""
        )
        remote_event_tables = tuple(
            getattr(status, "coordination_change_tables", ()) or ()
        )
        if (
            role is not None
            and detect_local_device_kind(
                getattr(role, "hostname", platform.node())
            )
            == "Laptop"
            and execution_engine is not None
            and remote_event_id
            and remote_event_id
            != self.__dict__.get("_last_outbound_coordination_event_id", "")
        ):
            self._last_outbound_coordination_event_id = remote_event_id
            if mark_remote_coordination_change(
                execution_engine,
                remote_event_id,
                tables=remote_event_tables,
            ):
                self._on_remote_coordination_change(remote_event_tables)
        db_ready = bool(status.database_ready)
        self._main_availability_probe_complete = True
        self._main_availability_probe_database_ready = db_ready
        previous_main_app_active = self.__dict__.get(
            "_last_pc_main_app_active"
        )
        self._last_pc_database_probe_ready = db_ready
        if "db_engine_source" in self.__dict__:
            try:
                if db_ready:
                    probe_engine = self.__dict__.get("_pc_probe_engine")
                    if probe_engine is None:
                        probe_engine = self.__dict__.get("pc_db_engine")
                    if probe_engine is not None:
                        self._activate_recovered_pc_database(probe_engine)
                    elif not self.__dict__.get("_pc_database_ready", False):
                        self._start_database_recovery()
                else:
                    self._switch_to_runtime_local_mirror()
            except Exception:
                logger.exception("Runtime database transition failed")
                try:
                    self.append_log(
                        "Automatic database transition encountered an error; "
                        "the dashboard will keep retrying in the background."
                    )
                except Exception:
                    pass
        self._pc_is_on = db_ready or listener_on
        self._pc_remote_control_available = listener_on

        if db_ready:
            dot.setStyleSheet("border-radius: 5px; background-color: #26a69a;")  # green
            label.setText("PC: On")
        elif listener_on:
            dot.setStyleSheet("border-radius: 5px; background-color: #ffb300;")  # yellow
            label.setText("PC: On (DB unavailable)")
        else:
            dot.setStyleSheet("border-radius: 5px; background-color: #f23645;")  # red
            label.setText("PC: Unreachable")

        listener_text = {
            PcStatus.ON: "On",
            PcStatus.OFF: "Off",
            PcStatus.UNKNOWN: "Unknown",
        }.get(status.listener_status, "Unknown")
        if status.main_app_active is True:
            main_app_text = "On"
        elif status.main_app_active is False:
            main_app_text = "Off"
        else:
            main_app_text = "Unknown"
        services_text = (
            f"PC DB: {'On' if db_ready else 'Off'} | "
            f"Listener: {listener_text} | main.py: {main_app_text}"
        )
        services_label.setText(services_text)
        tooltip = (
            "The PC is considered online when either MySQL or the remote-control "
            "listener responds. main.py is reported from its database heartbeat.\n"
            + services_text
        )
        label.setToolTip(tooltip)
        services_label.setToolTip(tooltip)
        self._update_database_source_indicator()
        status_timer = self.__dict__.get("pc_status_timer")
        if status_timer is not None:
            # Reconnect quickly during an outage, then back off once healthy.
            status_timer.setInterval(15_000 if db_ready else 5_000)
        self._last_pc_main_app_active = status.main_app_active
        if (
            status.main_app_active is True
            and previous_main_app_active is not True
            and self.__dict__.get("db_engine_source", "none") == "pc"
        ):
            # The PC morning routine launches main.py only after its scheduled
            # refresh.  This edge is therefore a useful immediate mirror
            # top-up in addition to the periodic safety timer.
            self._sync_active_pc_to_local_mirror()

        if button is not None:
            if listener_on:
                button.setEnabled(True)
                button.setText("Turn Off")
                button.setToolTip("Ask the PC's remote-control listener to shut down safely.")
            elif self._pc_is_on:
                button.setEnabled(False)
                button.setText("Remote Control Offline")
                button.setToolTip(
                    "The PC/database is online, but the remote-control listener is not available."
                )
            else:
                button.setEnabled(True)
                button.setText("Wake PC")
                button.setToolTip("Open the router page used to wake the PC.")

    def _on_pc_status_button_clicked(self) -> None:
        if getattr(self, "_pc_remote_control_available", False):
            self._confirm_and_send_pc_shutdown()
        elif getattr(self, "_pc_is_on", False):
            QMessageBox.information(
                self,
                "Remote control unavailable",
                "The PC and database are online, but the remote-control listener is not running.",
            )
        else:
            self._open_pc_wake_page()

    def _open_pc_wake_page(self) -> None:
        """Open the router's admin login in a browser for the user to log
        in and trigger Wake On LAN by hand -- deliberately not automated,
        so no router credentials are ever stored in this app and nothing
        here depends on scripting an undocumented, ISP-specific web form."""
        url = os.getenv("PC_WAKE_URL", "").strip()
        if not url:
            QMessageBox.warning(
                self, "Not configured",
                "PC_WAKE_URL is not set in .env (e.g. http://your-router-ddns-host:PORT/).",
            )
            return
        webbrowser.open(url)

    def _confirm_and_send_pc_shutdown(self) -> None:
        from src.core.execution_config import is_buyboard_engine_enabled
        from src.services import trading_state

        try:
            live_session = bool(is_regular_session_open())
        except Exception:
            # An unknown calendar state must not make a destructive power
            # action look safer than it is while execution is armed.
            live_session = True
        if (
            live_session
            and is_buyboard_engine_enabled()
            and trading_state.is_trading_enabled()
            and self._execution_state_engine() is self.__dict__.get("pc_db_engine")
        ):
            QMessageBox.warning(
                self,
                "PC shutdown blocked during live trading",
                "This PC hosts the one canonical trading database. Turning it "
                "off now would stop new entries and operator commands, and an "
                "already-active executor would retain only short, bounded "
                "emergency protection.\n\nDisarm live trading or wait until the "
                "regular session closes before shutting down the PC.",
            )
            return
        reply = QMessageBox.question(
            self,
            "Turn off PC",
            "Send a shutdown signal to the always-on PC?\n\n"
            "It will wait for any in-progress data refresh to finish before "
            "actually powering off.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        from src.services.pc_remote_control import send_shutdown_signal

        result = send_shutdown_signal()
        if result.accepted:
            QMessageBox.information(self, "Shutdown requested", result.message)
        else:
            QMessageBox.warning(self, "Shutdown request failed", result.message)

    def show_settings_placeholder(self) -> None:
        QMessageBox.information(self, "Settings", "Settings are not implemented yet.")

    def show_settings_dialog(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.settings = dialog.settings
            self._apply_shortcuts()
            self.append_log("Settings updated and shortcuts applied.")

    def show_orb_settings_dialog(self) -> None:
        """Edit, persist, and apply ORB scoring and validity settings."""
        dialog = OrbSettingsDialog(self.settings.get("orb_settings"), self)
        if dialog.exec_() != QDialog.Accepted:
            return

        orb_settings = dialog.orb_settings()
        updated_settings = dict(self.settings)
        updated_settings["orb_settings"] = orb_settings.to_dict()
        try:
            save_json(SETTINGS_FILE, updated_settings)
        except Exception as exc:
            logger.exception("Failed to save ORB settings")
            QMessageBox.warning(
                self,
                "ORB Settings",
                f"The ORB settings could not be saved:\n{exc}",
            )
            return

        self.settings = updated_settings
        configure_orb_settings(orb_settings)
        self.append_log("ORB scoring ideals and validity bounds updated.")

        refresh_queue = getattr(self, "refresh_execution_queue", None)
        if callable(refresh_queue):
            try:
                refresh_queue("PROD", show_log=False)
            except Exception:
                logger.exception(
                    "ORB settings were saved, but the execution queue refresh failed"
                )
                self.append_log(
                    "ORB settings saved; queued plans will update on their next refresh."
                )
        refresh_board = getattr(self, "refresh_buyboard", None)
        if callable(refresh_board):
            refresh_board()

    def _apply_shortcuts(self) -> None:
        """Apply configured keyboard shortcuts from settings."""
        shortcuts = self.settings.get("shortcuts", {})

        def parse_key(key_str: str):
            if key_str == "Up":
                return QKeySequence(Qt.Key_Up)
            if key_str == "Down":
                return QKeySequence(Qt.Key_Down)
            if key_str == "Left":
                return QKeySequence(Qt.Key_Left)
            if key_str == "Right":
                return QKeySequence(Qt.Key_Right)
            return QKeySequence(key_str)

        # 1. Intraday charts shortcuts
        if hasattr(self, "intraday_up_shortcut"):
            self.intraday_up_shortcut.setKey(
                parse_key(shortcuts.get("prev_symbol", "Up"))
            )
        if hasattr(self, "intraday_down_shortcut"):
            self.intraday_down_shortcut.setKey(
                parse_key(shortcuts.get("next_symbol", "Down"))
            )
        if hasattr(self, "intraday_target_shortcut"):
            self.intraday_target_shortcut.setKey(
                parse_key(shortcuts.get("set_target", "T"))
            )
        if hasattr(self, "intraday_draw_shortcut"):
            self.intraday_draw_shortcut.setKey(
                parse_key(shortcuts.get("draw_line", "D"))
            )
        if hasattr(self, "intraday_erase_shortcut"):
            self.intraday_erase_shortcut.setKey(
                parse_key(shortcuts.get("erase_drawing", "E"))
            )
        if hasattr(self, "intraday_full_view_shortcut"):
            self.intraday_full_view_shortcut.setKey(
                parse_key(shortcuts.get("full_view", "F"))
            )

        # 2. TradingView widget shortcuts
        if hasattr(self, "tradingview_draw_shortcut"):
            self.tradingview_draw_shortcut.setKey(
                parse_key(shortcuts.get("draw_line", "D"))
            )
        if hasattr(self, "tradingview_target_shortcut"):
            self.tradingview_target_shortcut.setKey(
                parse_key(shortcuts.get("set_target", "T"))
            )
        if hasattr(self, "tradingview_up_shortcut"):
            self.tradingview_up_shortcut.setKey(
                parse_key(shortcuts.get("prev_symbol", "Up"))
            )
        if hasattr(self, "tradingview_down_shortcut"):
            self.tradingview_down_shortcut.setKey(
                parse_key(shortcuts.get("next_symbol", "Down"))
            )
        if hasattr(self, "tradingview_left_shortcut"):
            self.tradingview_left_shortcut.setKey(
                parse_key(shortcuts.get("pan_left", "Left"))
            )
        if hasattr(self, "tradingview_right_shortcut"):
            self.tradingview_right_shortcut.setKey(
                parse_key(shortcuts.get("pan_right", "Right"))
            )
        if hasattr(self, "tradingview_full_view_shortcut"):
            self.tradingview_full_view_shortcut.setKey(
                parse_key(shortcuts.get("full_view", "F"))
            )
        # 3. Update Button Labels
        t_key = shortcuts.get("set_target", "T")
        d_key = shortcuts.get("draw_line", "D")
        e_key = shortcuts.get("erase_drawing", "E")
        f_key = shortcuts.get("full_view", "F")

        if hasattr(self, "intraday_set_target_button"):
            self.intraday_set_target_button.setText(f"Set Breakout Price ({t_key})")
        if hasattr(self, "intraday_draw_line_button"):
            self.intraday_draw_line_button.setText(f"Draw Line ({d_key})")
        if hasattr(self, "intraday_erase_line_button"):
            self.intraday_erase_line_button.setText(f"Erase Drawing ({e_key})")
        if hasattr(self, "intraday_full_view_button"):
            self.intraday_full_view_button.setText(f"Full View ({f_key})")
        if hasattr(
            self, "intraday_queue_btn"
        ) and self.intraday_queue_btn.text().startswith("Queue"):
            self.intraday_queue_btn.setText("Queue for Buy (Q)")
        if hasattr(self, "intraday_activate_btn"):
            cur = self.intraday_activate_btn.text()
            self.intraday_activate_btn.setText(
                "Deactivate (A)" if cur.startswith("Deactivate") else "Activate (A)"
            )

        if hasattr(self, "tradingview_set_target_button"):
            self.tradingview_set_target_button.setText(f"Set Breakout Price ({t_key})")
        if hasattr(self, "tradingview_line_tool_button"):
            self.tradingview_line_tool_button.setText(f"Line Tool ({d_key})")
        if hasattr(self, "tradingview_full_view_button"):
            self.tradingview_full_view_button.setText(f"Full View ({f_key})")
        if hasattr(
            self, "tradingview_queue_btn"
        ) and self.tradingview_queue_btn.text().startswith("Queue"):
            self.tradingview_queue_btn.setText("Queue for Buy (Q)")
        if hasattr(self, "tradingview_activate_btn"):
            cur = self.tradingview_activate_btn.text()
            self.tradingview_activate_btn.setText(
                "Deactivate (A)" if cur.startswith("Deactivate") else "Activate (A)"
            )

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "About",
            "Stock Dashboard\n\nA PyQt5 trading dashboard with scanner, Buy Board, chart review, and guarded execution.",
        )

    def save_local_data(self) -> None:
        """Persist local planning and chart state on demand."""
        self._save_state()
        self.append_log("Saved local planning, chart, and scanner state.")
        QMessageBox.information(
            self,
            "Saved",
            "Local planning, chart, and scanner state has been saved.",
        )

    def show_restore_backup_dialog(self) -> None:
        """Stage a cloud snapshot, shut down safely, apply it, and restart."""
        dialog = RestoreBackupDialog(self)
        if dialog.exec_() != QDialog.Accepted or dialog.selected_snapshot is None:
            return

        # A non-daemon save thread that outlives closeEvent could otherwise
        # finish after the restore and put stale state back on disk.
        if not self._state_save_manager().wait_for_pending_saves(timeout=5.0):
            QMessageBox.warning(
                self,
                "Restore Paused",
                "A local state save is still running. Wait a moment and try again; "
                "no files were changed.",
            )
            return

        # Copy and validate the selected snapshot before closing. This also
        # freezes "current" so the final shutdown save cannot change the
        # restore source underneath us.
        with tempfile.TemporaryDirectory(prefix="quant_app_cloud_restore_") as staging:
            staging_dir = Path(staging)
            staged = restore_state_files(
                dialog.backup_root,
                staging_dir,
                snapshot=dialog.selected_snapshot,
                preserve_existing=False,
            )
            if not staged.success:
                QMessageBox.warning(
                    self,
                    "Restore Failed",
                    staged.error or "The selected backup could not be staged.",
                )
                return
            if not staged.restored:
                QMessageBox.information(
                    self,
                    "Nothing to Restore",
                    "That snapshot contains no recognized state files.",
                )
                return

            # close() synchronously stops workers and performs the normal
            # final local save. Apply only after it succeeds, otherwise a
            # stale in-memory shutdown save could overwrite restored data.
            if not self.close():
                QMessageBox.warning(
                    self,
                    "Restore Paused",
                    "The app could not close cleanly, so no local files were changed.",
                )
                return

            result = restore_state_directory(staging_dir, DATA_DIR)
            if not result.success:
                QMessageBox.warning(
                    None,
                    "Restore Failed",
                    (result.error or "The staged backup could not be applied.")
                    + "\n\nThe app is closed; restart it manually after checking data/.",
                )
                return

            self._restart_application()

    def show_backup_env_dialog(self) -> None:
        """File > Backup .env to Cloud (Encrypted) -- manual, passphrase-gated."""
        dialog = BackupEnvDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.append_log("Encrypted .env backup written to cloud backup destination.")

    def show_restore_env_dialog(self) -> None:
        """File > Restore .env from Cloud (Encrypted) -- manual, passphrase-gated."""
        dialog = RestoreEnvDialog(self)
        if dialog.exec_() != QDialog.Accepted or not dialog.restored:
            return

        self.append_log("Restored .env from encrypted cloud backup.")
        message = ".env restored from the encrypted cloud backup."
        if dialog.preserved_original:
            message += f"\n\nYour previous local .env was preserved at:\n{dialog.preserved_original}"
        message += "\n\nThe app needs to restart to load the restored credentials. Restart now?"
        reply = QMessageBox.question(
            self,
            "Restore Complete",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._restart_application()

    def _restart_application(self) -> bool:
        """Close cleanly, relaunch main.py, and report whether it started."""
        if self.isVisible() and not self.close():
            return False
        main_py = ROOT_DIR / "main.py"
        try:
            subprocess.Popen([sys.executable, str(main_py)], cwd=str(ROOT_DIR))
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Restart Failed",
                f"Could not relaunch automatically: {exc}\nPlease restart the app manually.",
            )
            return False
        app = QApplication.instance()
        if app is not None:
            app.quit()
        return True

    def _parse_float(self, value: QLineEdit, default: float) -> float:
        try:
            text = value.text().strip().replace("%", "")
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(number) or number < 0:
            return default
        return number

    def _parse_int(self, value: QLineEdit, default: int) -> int:
        try:
            return int(value.text())
        except ValueError:
            return default

    def _set_html_or_text(self, widget, html_content: str, text_content: str) -> None:
        """Set chart content on either QWebEngineView or QTextEdit fallback."""
        if QWebEngineView is not None and isinstance(widget, QWebEngineView):
            widget.setHtml(html_content)
        else:
            widget.setPlainText(text_content)
