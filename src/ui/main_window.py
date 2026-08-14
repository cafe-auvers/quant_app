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
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QProgressBar,
                             QPushButton, QSizePolicy, QTabWidget, QTextEdit,
                             QVBoxLayout, QWidget)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None

from src.core.order_state import (BrokerOrder, OrderIntent, OrderSide,
                                  OrderStatus)
from src.core.scanner import StockScanner
from src.core.trade_reviewer import TradeReviewer
from src.core.watchlist import BuylistManager, TradePlanManager, Watchlist
from src.infrastructure.database.mirror_engine import resolve_data_engine
from src.infrastructure.database.mirror_freshness import (
    local_mirror_hourly_is_stale, local_mirror_is_stale)
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
from src.services.order_ledger import (append_order, find_open_orders,
                                       has_open_order, load_order_ledger,
                                       merge_orders, save_order_ledger,
                                       update_order)
from src.services.runtime_status import safe_mark_runtime_process_stopped
from src.services.sleep_readiness import write_sleep_readiness_snapshot
from src.services.state_sync import LocalDeviceRole, load_local_device_role
from src.ui.buylist import BuylistMixin
from src.ui.charts.controller import ChartsControllerMixin
from src.ui.charts.renderer import ChartsRenderMixin
from src.ui.controllers import (AccountController, BuylistController,
                                BuylistExecutionController,
                                ChartDataController, ScannerController,
                                WatchlistController)
from src.ui.dialogs import (BackupEnvDialog, RestoreBackupDialog,
                            RestoreEnvDialog, SettingsDialog)
from src.ui.filter_catalog import (DEFAULT_SCANNER_SETUPS, DEFAULT_SETTINGS,
                                   DEFAULT_TAB_OPTIONS)
from src.ui.health import HealthPanelMixin
from src.ui.mixins.dashboard_mixin import DashboardMixin
from src.ui.mixins.scanner_mixin import ScannerMixin
from src.ui.mixins.sidebar_mixin import SidebarMixin
from src.ui.mixins.watchlist_mixin import WatchlistMixin
from src.ui.order_workers import HandoffReconciliationWorker
from src.ui.workers import PcRemoteStatusWorker, WatchlistAiWorker
from src.utils.config import DATA_DIR, ROOT_DIR, RULEBOOK_DIR, get_env_value
from src.utils.data_loader import get_default_universe
from src.utils.intraday_helpers import \
    extract_latest_opening_bar as _extract_latest_opening_bar
from src.utils.market_calendar import expected_latest_market_data_date
from src.utils.storage import load_json

__all__ = [
    "MainWindow",
    "QTimer",
    "WatchlistAiWorker",
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
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
LOCAL_MIRROR_SYNC_INTERVAL_MS = 15 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
WORKER_SHUTDOWN_TIMEOUT_MS = 30_000
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


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

    def run(self) -> None:
        try:
            if self.auto_claim:
                result = auto_claim_main_device_if_stale(
                    self.engine,
                    self.role,
                    expected_owner_device_id=self.expected_owner_device_id,
                    save_lock=self.save_lock,
                )
            elif self.activate:
                result = activate_device_as_main(
                    self.engine,
                    self.role,
                    save_lock=self.save_lock,
                )
            else:
                result = reconcile_state_with_remote(
                    self.engine,
                    self.role,
                    save_lock=self.save_lock,
                    ownership_only_when_main=self.ownership_only_when_main,
                )
        except Exception as exc:
            logger.exception("State sync worker failed")
            result = StateReconcileResult(
                errors=[f"State sync failed: {exc}"],
                local_role=self.role,
            )
        self.completed.emit(result, self.generation)


class MainWindow(
    SidebarMixin,
    HealthPanelMixin,
    DashboardMixin,
    ScannerMixin,
    WatchlistMixin,
    BuylistMixin,
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
        self.watchlist_scores = {}
        self.trade_manager = self._load_trade_plans()
        self.order_ledger: List[BrokerOrder] = load_order_ledger()
        self.scanner_setups = self._load_scanner_setups()
        self.chart_drawings = self._load_chart_drawings()
        self.tab_options = self._load_tab_options()
        self.settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        if "shortcuts" not in self.settings:
            self.settings["shortcuts"] = DEFAULT_SETTINGS["shortcuts"].copy()
        else:
            for k, v in DEFAULT_SETTINGS["shortcuts"].items():
                if k not in self.settings["shortcuts"]:
                    self.settings["shortcuts"][k] = v
        self.reviewer = TradeReviewer(rulebook_dir=RULEBOOK_DIR)
        # MySQL is optional, and establishing a connection must never block the
        # desktop window from appearing.  A short-lived worker finishes setup
        # after the event loop begins.
        self.db_engine = None
        self.pc_db_engine = None
        self._pc_probe_engine = None
        self._local_mirror_engine = None
        self.db_engine_source = "none"
        self.db_enabled = False
        self.db_initializing = True
        self.database_init_worker = None
        self.database_recovery_worker = None
        self._pc_database_ready = False
        self._pc_database_coordination_ready = False
        self._last_pc_database_probe_ready = False
        self._database_transition_generation = 0
        self._database_shutting_down = False
        self._database_reconciliation_in_progress = False
        self._last_database_reconciliation_notice = ""
        self._local_mirror_sync_worker = None
        self._local_mirror_sync_log_completion = True
        self._local_mirror_progress_phase = ""
        self._local_mirror_progress_samples = []
        self._last_pc_main_app_active = None
        self.state_sync_role = load_local_device_role()
        self.state_sync_worker = None
        self._last_state_sync_notice = ""
        self._initial_state_sync_complete = False
        # Main-device lease fencing: the token this device believes it
        # currently holds (empty when pull-only), refreshed by
        # _on_state_sync_completed whenever a claim/reconcile confirms it,
        # and threaded into every live order submission via
        # _current_execution_lease_kwargs so ExecutionAuthority can re-verify
        # it at the actual broker boundary.
        self._current_lease_token = ""
        self._last_successful_reconcile_at: Optional[dt.datetime] = None

        # Automatic cross-machine handoff (laptop <-> PC). Both env flags
        # default OFF -- only the unattended device (the PC, per the deployed
        # .env) should ever set AUTO_CLAIM_MAIN_ON_HANDOFF, and only after
        # EXPECTED_AUTO_CLAIM_HOSTNAME confirms this is that specific
        # machine, so a copied .env can't silently arm this elsewhere. The
        # laptop deliberately never auto-reclaims on startup -- it stays
        # pull-only until "Use This Device as Main" is clicked manually.
        self._auto_claim_main_enabled = self._handoff_env_flag_true(
            "AUTO_CLAIM_MAIN_ON_HANDOFF"
        ) and self._expected_auto_claim_hostname_matches()
        self._auto_arm_trading_on_handoff = self._handoff_env_flag_true(
            "AUTO_ARM_TRADING_ON_HANDOFF"
        )
        self.handoff_reconciliation_worker = None
        self._handoff_generation = 0
        self._state_sync_auto_claim = False
        self._last_main_device_hostname = ""
        self._last_handoff_blocked_symbols: Tuple[str, ...] = ()
        self.kis_account_snapshots: dict[tuple[str, str], dict] = {}
        self._kis_api_last_success_at = ""
        self._kis_api_last_error = ""
        self.latest_intraday_prices: dict[str, float] = {}
        self.latest_intraday_sources: dict[tuple[str, str], str] = {}
        self.intraday_fetch_attempts: dict[str, dt.datetime] = {}
        self._cached_market_data_status = None
        self.orb_trade_plan_column_data: dict[int, dict] = {}
        self.updating_orb_selection = False
        self.intraday_bulk_purpose = "watchlist"
        self.pending_scanner_orb_source: Optional[dict] = None
        self.scanner_results: List[dict] = []
        self.scanner_results_by_setup: dict[str, List[dict]] = {}
        self.scanner_dataframe = pd.DataFrame()
        self.selected_scan_symbol: Optional[str] = None
        self.chart_view_windows: dict[str, dict] = {}
        # Deferred-refresh flags: chart edits (breakout price, drawings) used to
        # eagerly rebuild the watchlist table / dashboard summary / other chart
        # tabs even while those tabs weren't visible. These flags let that work
        # be skipped and picked up once the user actually switches to the tab
        # (see on_tab_changed / flush_stale_chart_views), instead of paying the
        # cost synchronously in the middle of an unrelated chart interaction.
        self._watchlist_table_dirty = False
        self._dashboard_summary_dirty = False
        self._charts_tab_chart_stale = False
        self._intraday_tab_chart_stale = False
        self._tradingview_tab_chart_stale = False
        self.running_scanner_setup_name: Optional[str] = None
        self.running_scanner_show_warnings = True
        self.scanner_worker = None
        self.watchlist_worker = None
        self.single_ai_worker = None
        self.kis_order_worker = None
        self._refresh_last_finished_at: Dict[str, Optional[str]] = {}
        self._refresh_last_log_count: Dict[str, int] = {}
        self._refresh_active_run_id: Dict[str, Optional[str]] = {}
        self._pending_local_mirror_hourly_refresh = False
        self._run_scanners_after_local_mirror_refresh = False
        self.kis_account_worker = None
        self.kis_startup_worker = None
        self.order_reconciliation_worker = None
        self._pending_reconciliation_groups: List[Tuple[str, str]] = []
        self._last_order_reconciliation_at = ""
        self._last_order_reconciliation_error = ""
        self._health_probe_worker = None
        self.kis_retry_timer = None
        self.fx_rate_worker = None
        self._tracked_workers: dict[QThread, tuple[str, Optional[str]]] = {}
        self.usd_krw_rate_source = ""
        self.intraday_fetch_worker = None
        self.intraday_bulk_worker = None
        self._intraday_provider_warning_log_keys: set[str] = set()
        self.live_data_timer = None
        self.current_tradingview_symbol = ""
        self.tradingview_refresh_timestamps: dict[str, dt.datetime] = {}
        self.kis_daily_chart_unavailable_until: Optional[dt.datetime] = None
        self.kis_daily_chart_unavailable_key: str = ""
        self.kis_daily_chart_last_error: str = ""
        self.state_save_manager = get_state_save_manager()
        self.state_save_manager.set_engine(
            None,
            device_id=self.state_sync_role.device_id,
            is_main_device=self.state_sync_role.is_main,
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
        self._setup_live_data_timer()

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
        QTimer.singleShot(1500, self.preload_kis_accounts_on_startup)
        QTimer.singleShot(2500, lambda: self.refresh_usd_krw_rate(show_messages=False))
        QTimer.singleShot(4000, self.reconcile_open_orders)
        self._apply_shortcuts()

    def _init_controllers(self) -> None:
        """Initialize non-rendering workflow controllers."""
        self.watchlist_controller = WatchlistController(self)
        self.buylist_execution_controller = BuylistExecutionController(self)
        self.buylist_controller = BuylistController(self)
        self.scanner_controller = ScannerController(self)
        self.chart_data_controller = ChartDataController(self)
        self.account_controller = AccountController(self)

    def _start_optional_database_initialization(self) -> None:
        """Begin optional database setup after widgets have been rendered."""
        if self.__dict__.get("_database_shutting_down", False):
            return
        if self.database_init_worker is not None and self.database_init_worker.isRunning():
            return
        worker = DatabaseInitWorker()
        self.database_init_worker = worker
        worker.initialized.connect(self._on_optional_database_initialized)
        self._track_worker("database_init_worker", worker)
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
        self._pc_database_ready = bool(source == "pc" and pc_engine is not None)
        self._pc_database_coordination_ready = False
        self._last_pc_database_probe_ready = self._pc_database_ready
        self._bind_remote_state_engine(pc_engine, is_main_device=False)
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
        self.update_dashboard_summary()
        # The startup resolver already performed the first PC database probe.
        # Start service polling only after that probe has completed so startup
        # never launches duplicate connection attempts in separate QThreads.
        self._poll_pc_status()

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
            generation=self.__dict__.get("_database_transition_generation", 0),
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
        hourly_is_stale = local_mirror_hourly_is_stale(
            engine, expected_date, tickers=tickers
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
    ) -> None:
        """Start one ownership/state reconciliation in a background worker."""
        if self.__dict__.get("_database_shutting_down", False):
            return
        pc_engine = self.__dict__.get("pc_db_engine")
        if pc_engine is None or not self.__dict__.get(
            "_pc_database_ready", pc_engine is not None
        ):
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
        worker = StateSyncWorker(
            self.pc_db_engine,
            self.state_sync_role,
            self._ensure_save_lock(),
            activate=activate or auto_claim,
            ownership_only_when_main=(
                not activate
                and not auto_claim
                and self.state_sync_role.is_main
                and self._initial_state_sync_complete
            ),
            generation=self.__dict__.get("_database_transition_generation", 0),
            auto_claim=auto_claim,
            expected_owner_device_id=expected_owner_device_id,
        )
        self.state_sync_worker = worker
        self._state_sync_action = "activate" if (activate or auto_claim) else "reconcile"
        self._state_sync_auto_claim = auto_claim
        worker.completed.connect(self._on_state_sync_completed)
        self._track_worker("state_sync_worker", worker)
        if activate and hasattr(self, "main_device_button"):
            self.main_device_button.setEnabled(False)
            self.main_device_button.setText("Activating Main Device...")
        worker.start()

    def _sync_state_with_remote(self) -> None:
        """Compatibility wrapper for callers requesting an immediate sync."""
        self._start_state_sync()

    def _on_state_sync_completed(
        self, result: StateReconcileResult, generation: int
    ) -> None:
        if self.__dict__.get("_database_shutting_down", False):
            return
        if generation != self.__dict__.get("_database_transition_generation", 0):
            return
        if not self.__dict__.get("_pc_database_ready", False):
            return
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
                self.pc_db_engine,
                is_main_device=result.is_main_device,
            )
        else:
            self._pc_database_coordination_ready = False
            self._bind_remote_state_engine(
                self.pc_db_engine,
                is_main_device=False,
            )
        self._current_lease_token = result.lease_token if result.is_main_device else ""
        if result.main_device_hostname:
            self._last_main_device_hostname = result.main_device_hostname
        self._update_main_device_button(
            main_hostname=result.main_device_hostname,
        )

        action = getattr(self, "_state_sync_action", "reconcile")
        was_auto_claim = bool(self._state_sync_auto_claim)
        self._state_sync_auto_claim = False
        if action == "activate" and result.is_main_device:
            if was_auto_claim:
                self.append_log(
                    "Automatic handoff: claimed main-device ownership "
                    f"({result.main_device_hostname or platform.node()}); "
                    "reconciling against the broker before resuming monitoring."
                )
            else:
                self.append_log(
                    "This device is now the exclusive main device; the other device is pull-only."
                )
        elif previous_main and not result.is_main_device:
            owner = result.main_device_hostname or "another device"
            self.append_log(
                f"Main-device ownership moved to {owner}; this device is now pull-only."
            )

        updated_keys = set(result.updated_keys)
        if updated_keys:
            self.append_log(
                f"Pulled newer shared state: {', '.join(sorted(updated_keys))}."
            )
        if "watchlist" in updated_keys:
            self.watchlist = self._load_watchlist()
            self.populate_watchlist_table()
        if "buylist" in updated_keys:
            self.buylist_manager = self._load_buylist()
            self.populate_buylist_dashboard()
        if "trade_plans" in updated_keys:
            self.trade_manager = self._load_trade_plans()
            self.populate_trade_plan_table()
        if "execution_queue" in updated_keys:
            # Lazily reloaded on next access (_ensure_execution_queue_manager
            # caches on self.execution_queue_manager) -- just drop the stale
            # cached instance so the freshly-pulled file wins.
            self.__dict__.pop("execution_queue_manager", None)
            if hasattr(self, "populate_buylist_dashboard"):
                self.populate_buylist_dashboard()

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
            if was_auto_claim:
                self._begin_post_claim_handoff()
            return

        # Automatic handoff detection: only ever runs on a device explicitly
        # configured for it (PC's .env only, hostname-guarded), only on a
        # plain reconcile tick (never re-entering while an activation is
        # already in flight), and never while this device is already main.
        if (
            action != "activate"
            and self._auto_claim_main_enabled
            and not result.is_main_device
            and not (self.state_sync_worker and self.state_sync_worker.isRunning())
        ):
            should_claim, expected_owner_device_id, reason = should_auto_claim_main(
                self.pc_db_engine,
                self.state_sync_role,
                other_hostname=result.main_device_hostname,
            )
            if should_claim:
                self.append_log(f"Automatic handoff: claiming main device ({reason}).")
                self._start_state_sync(
                    auto_claim=True,
                    expected_owner_device_id=expected_owner_device_id,
                )

    # --- Automatic cross-machine handoff: post-claim reconciliation --------
    # The safety-critical sequence that must run before a device that just
    # auto-claimed main-device status is allowed to resume monitoring/live
    # order submission. Never entered from the manual "Use This Device as
    # Main" button (see the was_auto_claim guard above) -- that flow keeps
    # its existing behavior unchanged.

    def _begin_post_claim_handoff(self) -> None:
        """Lock pending flags, then reconcile against the broker off-thread."""
        self._handoff_generation += 1
        generation = self._handoff_generation

        reset_items = reset_runtime_only_order_flags(self.buylist_manager)
        if reset_items:
            symbols = ", ".join(sorted({item.symbol for item in reset_items}))
            self.append_log(
                f"Automatic handoff: locked {len(reset_items)} in-flight PROD "
                f"item(s) pending broker reconciliation ({symbols})."
            )
            self._save_buylist_state()
            self.populate_buylist_dashboard()

        account_no = ""
        if hasattr(self, "_first_account_no_for_environment"):
            account_no = self._first_account_no_for_environment("PROD") or ""

        worker = HandoffReconciliationWorker(
            self.buylist_manager, environment="PROD", account_no=account_no
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
        self._save_buylist_state()
        self.populate_buylist_dashboard()
        self._last_handoff_blocked_symbols = tuple(outcome.blocked_symbols)
        if outcome.ok:
            self.append_log(
                f"Automatic handoff: broker reconciliation clean for "
                f"{len(outcome.reconciled_symbols)} symbol(s)."
            )
            self._auto_arm_trading_kill_switch()
            started = self._ensure_buylist_monitor_running("PROD")
            self.append_log(
                "Automatic handoff complete: monitor "
                f"{'started' if started else 'already running'}"
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

    def _retry_post_claim_handoff_if_still_main(self) -> None:
        if not self.state_sync_role.is_main:
            return
        if self.__dict__.get("_database_shutting_down", False):
            return
        self._begin_post_claim_handoff()

    def _on_post_claim_reconciliation_error(self, message: str) -> None:
        self.append_log(f"Automatic handoff: reconciliation worker failed: {message}")

    def _auto_arm_trading_kill_switch(self) -> None:
        """Arm live trading after a clean automatic handoff -- gated, not automatic-by-default.

        Deliberately a narrower, separately-configured policy from
        AUTO_CLAIM_MAIN_ON_HANDOFF: the kill switch otherwise starts
        disabled on every launch and is only armed by an explicit in-process
        UI click. Every condition below must hold, not just "we are main."
        """
        if not self._auto_arm_trading_on_handoff:
            self.append_log(
                "Live trading NOT auto-armed: AUTO_ARM_TRADING_ON_HANDOFF is not set."
            )
            return
        if trading_state.is_trading_locked_disabled():
            self.append_log(
                "Live trading remains locked off by TRADING_ENABLED; "
                "automatic handoff cannot arm it."
            )
            return
        if not self.state_sync_role.is_main or not self._current_lease_token:
            self.append_log("Live trading NOT auto-armed: lease is not currently held.")
            return
        if not self.__dict__.get("_pc_database_ready", False):
            self.append_log("Live trading NOT auto-armed: shared database is not reachable.")
            return
        trading_state.set_trading_enabled(True)
        button = getattr(self, "trading_enabled_button", None)
        if button is not None:
            button.blockSignals(True)
            try:
                button.setChecked(True)
            finally:
                button.blockSignals(False)
        if hasattr(self, "_refresh_trading_enabled_widget"):
            self._refresh_trading_enabled_widget()
        self.append_log(
            "Live trading auto-armed (automatic PC handoff, broker reconciliation clean)."
        )

    def _update_main_device_button(self, *, main_hostname: str = "") -> None:
        button = getattr(self, "main_device_button", None)
        if button is None:
            return
        is_main = bool(self.state_sync_role.is_main)
        button.setEnabled(
            bool(
                self.__dict__.get(
                    "_pc_database_ready",
                    self.__dict__.get("pc_db_engine") is not None,
                )
            )
            or bool(self.__dict__.get("db_initializing", False))
        )
        if is_main:
            button.setText("Main Device: ON")
            button.setStyleSheet(
                "background-color: #137333; color: white; font-weight: bold;"
            )
            button.setToolTip(
                "This device is the only device allowed to push shared app state."
            )
        else:
            button.setText("Use This Device as Main")
            button.setStyleSheet("")
            owner_text = main_hostname or "the other device"
            button.setToolTip(
                f"This device is pull-only. Click to transfer main ownership from {owner_text}."
            )

    def _on_main_device_button_clicked(self) -> None:
        if self.state_sync_role.is_main:
            QMessageBox.information(
                self,
                "Main device",
                "This device is already the exclusive main device.",
            )
            return
        if self.pc_db_engine is None:
            QMessageBox.warning(
                self,
                "State sync unavailable",
                "Connect to the shared MySQL database before transferring main-device ownership.",
            )
            return
        reply = QMessageBox.question(
            self,
            "Use This Device as Main",
            "Transfer main-device ownership to this device?\n\n"
            "The other device will become pull-only. Remote revisions are checked so "
            "stale state cannot overwrite newer data.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._start_state_sync(activate=True)

    # Bounded age past which a cached "I am main" belief is no longer trusted
    # for order submission. Defense in depth against network partition: a
    # device that loses its connection to MySQL while still believing it's
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
                device_id=role.device_id, lease_token=lease_token
            ),
            "lease_engine": self.__dict__.get("pc_db_engine"),
        }

    def _state_sync_allows_order_submission(self) -> bool:
        """Allow broker submissions only from the active, recently-confirmed main device."""
        role = self.__dict__.get("state_sync_role")
        if role is None:
            # Lightweight test/dummy windows do not initialize sync state.
            return True
        manager = self._state_save_manager()
        allowed = bool(
            role.is_main and getattr(manager, "_is_main_device", role.is_main)
        )
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
                    self.append_log(
                        "KIS order submission blocked: last successful state "
                        f"sync was {age_seconds:.0f}s ago (stale beyond "
                        f"{self._RECONCILE_FRESHNESS_MAX_AGE_SECONDS}s) -- this "
                        "device may have lost its connection and been "
                        "superseded elsewhere."
                    )
        if allowed:
            return True
        self.append_log(
            "KIS order submission blocked because this device is pull-only."
        )
        QMessageBox.warning(
            self,
            "Pull-only device",
            "Only the active main device may submit KIS orders. "
            "Click 'Use This Device as Main' before trading from this device.",
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
        self._state_save_manager().set_engine(
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
        self._database_shutting_down = True
        self._database_transition_generation = (
            self.__dict__.get("_database_transition_generation", 0) + 1
        )
        timers = [
            self.__dict__.get("live_data_timer"),
            self.__dict__.get("state_sync_timer"),
            self.__dict__.get("sleep_readiness_timer"),
            self.__dict__.get("pc_status_timer"),
            self.__dict__.get("local_mirror_sync_timer"),
            self.__dict__.get("market_status_timer"),
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
        candidate_workers = [
            getattr(self, "database_init_worker", None),
            getattr(self, "database_recovery_worker", None),
            getattr(self, "_local_mirror_sync_worker", None),
            getattr(self, "state_sync_worker", None),
            getattr(self, "_pc_status_worker", None),
            self.scanner_worker,
            self.watchlist_worker,
            self.single_ai_worker,
            self.kis_order_worker,
            self.intraday_fetch_worker,
            self.intraday_bulk_worker,
            self.kis_account_worker,
            self.kis_startup_worker,
            self.order_reconciliation_worker,
            self.fx_rate_worker,
            getattr(self, "broker_order_query_worker", None),
            getattr(self, "broker_order_cancel_worker", None),
            getattr(self, "handoff_reconciliation_worker", None),
            *getattr(self, "_buylist_order_workers", []),
            *getattr(self, "_buylist_aux_workers", []),
            *list(getattr(self, "_tracked_workers", {})),
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

        self._release_main_device_ownership_for_shutdown()

        safe_mark_runtime_process_stopped(
            self.pc_db_engine
            if getattr(self, "_pc_database_ready", False)
            else None
        )
        super().closeEvent(event)

    def _release_main_device_ownership_for_shutdown(self) -> None:
        """Strict handoff publish + release, called once during closeEvent.

        Extracted as its own method (rather than inlined in the already-long
        closeEvent) so it's independently testable. A no-op for a pull-only
        device or when the shared database isn't ready -- most shutdowns
        never touch the network at all.
        """
        if not getattr(self, "_pc_database_ready", False):
            return
        if not self.state_sync_role.is_main:
            return

        execution_queue_payload = load_json(EXECUTION_QUEUE_FILE, {})
        published = publish_handoff_snapshot(
            self.pc_db_engine,
            self.state_sync_role,
            self.buylist_manager.to_dict(),
            execution_queue_payload,
        )
        if not published:
            self.append_log(
                "Handoff publish did not confirm remotely; the next device "
                "will only pick this up via the stale-heartbeat fallback."
            )

        released, self.state_sync_role, release_error = release_main_device_and_demote(
            self.pc_db_engine, self.state_sync_role
        )
        self._current_lease_token = ""
        if not released and release_error:
            self.append_log(f"Main-device release failed: {release_error}")

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

        self.watchlist_widget = QWidget()
        self._add_configured_tab("watchlist", self.watchlist_widget, "Watchlist")
        self._build_watchlist_tab()

        self.buylist_widget = QWidget()
        self._add_configured_tab("buylist", self.buylist_widget, "Buy Dashboard")
        self._build_buylist_tab()

        self.charts_widget = QWidget()
        self._add_configured_tab("charts", self.charts_widget, "Charts")
        self._build_charts_tab()

        self.tradingview_widget = QWidget()
        self._add_configured_tab(
            "tradingview", self.tradingview_widget, "TradingView Chart"
        )
        self._build_tradingview_tab()

        self.health_widget = QWidget()
        self._add_configured_tab("health", self.health_widget, "Health")
        self._build_health_tab()

        self.intraday_charts_widget = QWidget()
        self._add_configured_tab(
            "intraday_charts", self.intraday_charts_widget, "Intraday Charts"
        )
        self._build_intraday_charts_tab()

        # Wire env combo â†’ watchlist refresh (Trade Plan tab removed)
        self.watchlist_env_combo.currentIndexChanged.connect(
            self.on_watchlist_env_changed
        )
        self.watchlist_env_combo.currentIndexChanged.connect(
            self.populate_watchlist_table
        )
        # currentIndexChanged was emitted during addItems before the signal was connected,
        # so populate_trade_account_combo was never called. Trigger it once explicitly now.
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
        refresh_action = tools_menu.addAction("Refresh Dashboard")
        refresh_action.triggered.connect(self._refresh_dashboard_summary_manually)
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
        self.pc_status_timer.setInterval(15000)
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
        self.local_mirror_sync_timer.start()

        # Set up a 1-second timer to update the countdown
        self.market_status_timer = QTimer(self)
        self.market_status_timer.setInterval(1000)
        self.market_status_timer.timeout.connect(self.update_market_countdown_status)
        self.market_status_timer.start()
        self.update_market_countdown_status()

    def _refresh_trading_enabled_widget(self) -> None:
        """Sync the toolbar kill-switch button to the real trading_state."""
        button = getattr(self, "trading_enabled_button", None)
        if button is None:
            return
        locked = trading_state.is_trading_locked_disabled()
        enabled = trading_state.is_trading_enabled()
        button.blockSignals(True)
        try:
            button.setChecked(enabled)
            if locked:
                button.setText("LIVE TRADING ● LOCKED OFF")
                button.setToolTip(
                    "TRADING_ENABLED is blank, false, or invalid in "
                    ".env/environment; this forces live trading off and cannot "
                    "be overridden from the UI."
                )
                button.setEnabled(False)
                button.setStyleSheet(
                    "QPushButton { background-color: #363a45; color: #787b86; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
            elif enabled:
                button.setText("LIVE TRADING ● ON")
                button.setToolTip("Guarded order submission is armed. Click to disable.")
                button.setEnabled(True)
                button.setStyleSheet(
                    "QPushButton { background-color: #f23645; color: white; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
            else:
                button.setText("LIVE TRADING ● DISABLED")
                button.setToolTip(
                    "Guarded order submission is blocked. Click to enable (confirmation required)."
                )
                button.setEnabled(True)
                button.setStyleSheet(
                    "QPushButton { background-color: #363a45; color: #d1d4dc; "
                    "font-weight: bold; padding: 4px 10px; border-radius: 4px; }"
                )
        finally:
            button.blockSignals(False)

    def _on_trading_enabled_toggled(self, checked: bool) -> None:
        """Handle a click on the toolbar live-trading kill switch.

        Disabling is always immediate. Enabling requires an explicit
        confirmation click-through so activating order submission is deliberate.
        """
        if checked:
            reply = QMessageBox.question(
                self,
                "Enable Live Trading",
                "This allows guarded KIS order submission (PROD and SIM) for the "
                "rest of this session. Enable live trading?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._refresh_trading_enabled_widget()
                return
            trading_state.set_trading_enabled(True)
        else:
            trading_state.set_trading_enabled(False)

        self._refresh_trading_enabled_widget()
        effective = trading_state.is_trading_enabled()
        self.append_log(
            f"Live trading {'ENABLED' if effective else 'DISABLED'} "
            f"(guarded KIS order submission gate)."
        )

    def _build_status_log(self, parent_layout: QVBoxLayout) -> None:
        """Build the shared dashboard log and progress widgets."""
        status_widget = QWidget()
        status_widget.setMaximumHeight(145)
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
        self.main_device_button = QPushButton()
        self.main_device_button.setObjectName("mainDeviceButton")
        self.main_device_button.setMaximumWidth(210)
        self.main_device_button.clicked.connect(
            self._on_main_device_button_clicked
        )
        progress_layout.addWidget(self.progress_bar, 2)
        progress_layout.addWidget(self.progress_label, 3)
        progress_layout.addWidget(self.main_device_button)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet(
            "background-color: black; color: white; font-family: Consolas, monospace; font-size: 11px;"
        )
        self.log_output.setMinimumHeight(70)
        self.log_output.setMaximumHeight(95)
        self.log_output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        status_layout.addLayout(progress_layout)
        status_layout.addWidget(self.log_output)
        status_widget.setLayout(status_layout)
        parent_layout.addWidget(status_widget)
        self._update_main_device_button()

        # Pull-only devices stay current while both applications remain open,
        # and former main devices promptly notice an ownership transfer.
        self.state_sync_timer = QTimer(self)
        self.state_sync_timer.setInterval(15_000)
        self.state_sync_timer.timeout.connect(self._start_state_sync)
        self.state_sync_timer.start()

        # Cross-process signal for the PC's guarded-sleep automation (see
        # src/services/sleep_readiness.py and scripts/Invoke-GuardedSleep.ps1)
        # -- PowerShell/Task Scheduler cannot inspect this running Qt process
        # directly, so a small JSON snapshot is written periodically instead.
        self.sleep_readiness_timer = QTimer(self)
        self.sleep_readiness_timer.setInterval(30_000)
        self.sleep_readiness_timer.timeout.connect(self._write_sleep_readiness_snapshot)
        self.sleep_readiness_timer.start()

    def _write_sleep_readiness_snapshot(self) -> None:
        try:
            write_sleep_readiness_snapshot(self)
        except Exception:
            logger.debug("Sleep-readiness snapshot write failed", exc_info=True)

    def append_log(self, message: str) -> None:
        """Request an in-app log update from any thread."""
        self.log_message_requested.emit(str(message))

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
            tooltip = (
                "Market-data reads remain on the laptop's local SQLite mirror "
                "while PC/local market data is reconciled."
                if reconciling
                else "Market-data reads are using the laptop's local SQLite mirror."
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
            or self.__dict__.get("pc_db_engine") is not None
            or self.__dict__.get("db_engine_source", "none") == "pc"
        )
        if not was_pc_active:
            return

        current_pc_engine = self.__dict__.get("pc_db_engine")
        if self.__dict__.get("_pc_probe_engine") is None:
            self._pc_probe_engine = current_pc_engine
        self._pc_database_ready = False
        self._pc_database_coordination_ready = False
        self._database_reconciliation_in_progress = False
        self.pc_db_engine = None
        self.db_engine = None
        self.db_engine_source = "none"
        self.db_enabled = False
        self._update_database_source_indicator()
        self._database_transition_generation = (
            self.__dict__.get("_database_transition_generation", 0) + 1
        )
        self._initial_state_sync_complete = False
        try:
            self._bind_remote_state_engine(None, is_main_device=False)
        except Exception:
            logger.exception("Could not detach remote state persistence during failover")

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
            self.append_log(
                "PC database went offline; switched automatically to the local data mirror."
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
        self._update_main_device_button()
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
        self._pc_database_coordination_ready = False
        self._last_pc_database_probe_ready = True
        self._initial_state_sync_complete = False
        self._cached_market_data_status = None
        self._update_database_source_indicator()
        try:
            # Remote writes and order submission remain disabled until the
            # current-generation reconciliation confirms device ownership.
            self._bind_remote_state_engine(engine, is_main_device=False)
        except Exception:
            logger.exception("Could not attach recovered remote state persistence")

        if previous_source != "pc":
            self.append_log(
                "PC database is back online; switched automatically from the local mirror."
            )
        try:
            self._update_main_device_button()
            self.update_dashboard_summary()
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

    def _poll_pc_status(self) -> None:
        """Kick off a background check of the always-on PC's status.

        Keep the completed worker referenced until Qt delivers ``finished``.
        Replacing a QThread merely because ``isRunning()`` became false can
        destroy its wrapper while queued signals are still being dispatched.
        """
        if self.__dict__.get("_database_shutting_down", False):
            return
        if getattr(self, "db_initializing", False):
            return
        if self.__dict__.get("database_recovery_worker") is not None:
            return
        if self._pc_status_worker is not None:
            return
        probe_engine = self.__dict__.get("_pc_probe_engine")
        if probe_engine is None:
            probe_engine = self.pc_db_engine
        worker = PcRemoteStatusWorker(probe_engine, parent=self)
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
        db_ready = bool(status.database_ready)
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

        # 2. Charts tab shortcuts
        if hasattr(self, "chart_target_shortcut"):
            self.chart_target_shortcut.setKey(
                parse_key(shortcuts.get("set_target", "T"))
            )
        if hasattr(self, "chart_draw_shortcut"):
            self.chart_draw_shortcut.setKey(parse_key(shortcuts.get("draw_line", "D")))
        if hasattr(self, "chart_erase_shortcut"):
            self.chart_erase_shortcut.setKey(
                parse_key(shortcuts.get("erase_drawing", "E"))
            )
        if hasattr(self, "chart_left_shortcut"):
            self.chart_left_shortcut.setKey(
                parse_key(shortcuts.get("pan_left", "Left"))
            )
        if hasattr(self, "chart_right_shortcut"):
            self.chart_right_shortcut.setKey(
                parse_key(shortcuts.get("pan_right", "Right"))
            )
        if hasattr(self, "chart_up_shortcut"):
            self.chart_up_shortcut.setKey(parse_key(shortcuts.get("prev_symbol", "Up")))
        if hasattr(self, "chart_down_shortcut"):
            self.chart_down_shortcut.setKey(
                parse_key(shortcuts.get("next_symbol", "Down"))
            )
        if hasattr(self, "chart_full_view_shortcut"):
            self.chart_full_view_shortcut.setKey(
                parse_key(shortcuts.get("full_view", "F"))
            )

        # 3. TradingView widget shortcuts
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
        if hasattr(self, "tradingview_watchlist_shortcut"):
            self.tradingview_watchlist_shortcut.setKey(
                parse_key(shortcuts.get("add_watchlist", "W"))
            )

        # 4. Update Button Labels
        t_key = shortcuts.get("set_target", "T")
        d_key = shortcuts.get("draw_line", "D")
        e_key = shortcuts.get("erase_drawing", "E")
        f_key = shortcuts.get("full_view", "F")
        w_key = shortcuts.get("add_watchlist", "W")

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

        if hasattr(self, "chart_set_target_button"):
            self.chart_set_target_button.setText(f"Set Breakout Price ({t_key})")
        if hasattr(self, "chart_draw_line_button"):
            self.chart_draw_line_button.setText(f"Draw Line ({d_key})")
        if hasattr(self, "chart_erase_line_button"):
            self.chart_erase_line_button.setText(f"Erase Drawing ({e_key})")
        if hasattr(self, "chart_full_view_button"):
            self.chart_full_view_button.setText(f"Full View ({f_key})")

        if hasattr(self, "tradingview_set_target_button"):
            self.tradingview_set_target_button.setText(f"Set Breakout Price ({t_key})")
        if hasattr(self, "tradingview_line_tool_button"):
            self.tradingview_line_tool_button.setText(f"Line Tool ({d_key})")
        if hasattr(self, "tradingview_full_view_button"):
            self.tradingview_full_view_button.setText(f"Full View ({f_key})")
        if hasattr(self, "tradingview_add_watchlist_button"):
            cur_wl = self.tradingview_add_watchlist_button.text()
            self.tradingview_add_watchlist_button.setText(
                f"Remove from Watchlist ({w_key})"
                if cur_wl.startswith("Remove")
                else f"Add to Watchlist ({w_key})"
            )
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
            "Stock Dashboard\n\nA PyQt5 trading dashboard prototype with scanner, watchlist, and trade planning.",
        )

    def save_local_data(self) -> None:
        """Persist watchlist and trade plans on demand."""
        self._save_state()
        self.append_log("Saved local watchlist, trade plans, and scanner setups.")
        QMessageBox.information(
            self,
            "Saved",
            "Local watchlist, trade plans, and scanner setups have been saved.",
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
