"""Main application window for the stock dashboard."""
import datetime as dt
import threading
import time
from typing import Any, Dict, Optional, List, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QLabel,
    QLineEdit,
    QTextEdit,
    QProgressBar,
    QMessageBox,
    QSizePolicy,
    QDialog,
)
from PyQt5.QtCore import Qt, QThread, QTimer
from PyQt5.QtGui import QKeySequence
try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.core.scanner import StockScanner
from src.core.watchlist import Watchlist, TradePlanManager, BuylistManager
from src.core.trade_reviewer import TradeReviewer
from src.utils.data_loader import get_default_universe
from src.utils.db_loader import init_mysql_engine
from src.utils.storage import load_json
from src.services.app_state import (
    SETTINGS_FILE,
    SaveResult,
    get_state_save_manager,
    load_buylist_state,
    load_chart_drawings_state,
    load_scanner_setups_state,
    load_tab_options_state,
    load_trade_plans_state,
    load_watchlist_state,
    save_app_state,
)
from src.ui.controllers import (
    AccountController,
    BuylistExecutionController,
    ChartDataController,
    ScannerController,
    WatchlistController,
)
from src.ui.dialogs import SettingsDialog
from src.ui.mixins.sidebar_mixin import SidebarMixin
from src.ui.mixins.dashboard_mixin import DashboardMixin
from src.ui.mixins.scanner_mixin import ScannerMixin
from src.ui.mixins.watchlist_mixin import WatchlistMixin
from src.ui.mixins.buylist_mixin import BuylistMixin
from src.ui.mixins.charts_controller_mixin import ChartsControllerMixin
from src.ui.mixins.charts_render_mixin import ChartsRenderMixin
from src.ui.filter_catalog import (
    DEFAULT_SCANNER_SETUPS,
    DEFAULT_SETTINGS,
    DEFAULT_TAB_OPTIONS,
)
from src.ui.workers import WatchlistAiWorker
from src.services.order_ledger import (
    append_order,
    find_open_orders,
    has_open_order,
    load_order_ledger,
    save_order_ledger,
    update_order,
)
from src.utils.intraday_helpers import (
    extract_latest_opening_bar as _extract_latest_opening_bar,
)


__all__ = [
    "MainWindow",
    "QTimer",
    "WatchlistAiWorker",
    "_extract_latest_opening_bar",
    "append_order",
    "find_open_orders",
    "has_open_order",
    "load_order_ledger",
    "save_order_ledger",
    "update_order",
]







REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
WORKER_SHUTDOWN_TIMEOUT_MS = 30_000
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)




























class MainWindow(
    SidebarMixin,
    DashboardMixin,
    ScannerMixin,
    WatchlistMixin,
    BuylistMixin,
    ChartsControllerMixin,
    ChartsRenderMixin,
    QMainWindow,
):
    """Main dashboard window."""

    def __init__(self):
        """Initialize the main window."""
        super().__init__()
        self.setWindowTitle("Stock Dashboard")
        self._apply_global_stylesheet()
        self.setGeometry(100, 100, 1600, 900)

        self.universe_limit = None
        self.universe_tickers = get_default_universe(max_symbols=self.universe_limit)
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
        self.reviewer = TradeReviewer(rulebook_dir="rulebooks")
        self.db_engine = init_mysql_engine()
        self.db_enabled = self.db_engine is not None
        self.kis_account_snapshots: dict[tuple[str, str], dict] = {}
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
        self.running_scanner_setup_name: Optional[str] = None
        self.running_scanner_show_warnings = True
        self.scanner_worker = None
        self.refresh_worker = None
        self.hourly_refresh_worker = None
        self.kis_account_worker = None
        self.kis_startup_worker = None
        self.order_reconciliation_worker = None
        self._pending_reconciliation_groups: List[Tuple[str, str]] = []
        self.kis_retry_timer = None
        self.fx_rate_worker = None
        self.usd_krw_rate_source = ""
        self.intraday_fetch_worker = None
        self.intraday_bulk_worker = None
        self.live_data_timer = None
        self.current_tradingview_symbol = ""
        self.tradingview_refresh_timestamps: dict[str, dt.datetime] = {}
        self.kis_daily_chart_unavailable_until: Optional[dt.datetime] = None
        self.kis_daily_chart_unavailable_key: str = ""
        self.kis_daily_chart_last_error: str = ""
        self.state_save_manager = get_state_save_manager()
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

        # Run initial scans
        self.run_all_scanners(show_warnings=False)
        self.update_dashboard_summary()
        self.on_tab_changed()
        QTimer.singleShot(1500, self.preload_kis_accounts_on_startup)
        QTimer.singleShot(2500, lambda: self.refresh_usd_krw_rate(show_messages=False))
        QTimer.singleShot(4000, self.reconcile_open_orders)
        self._apply_shortcuts()

    def _init_controllers(self) -> None:
        """Initialize non-rendering workflow controllers."""
        self.watchlist_controller = WatchlistController(self)
        self.buylist_execution_controller = BuylistExecutionController(self)
        self.scanner_controller = ScannerController(self)
        self.chart_data_controller = ChartDataController(self)
        self.account_controller = AccountController(self)

    def _apply_unresolved_order_startup_state(self) -> None:
        """Reflect durable unresolved broker orders in the UI after startup."""
        open_orders = find_open_orders(self.order_ledger)
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
                new_status = "BUY_SUBMITTED"
            elif order.intent in {OrderIntent.PARTIAL_EXIT, OrderIntent.PARTIAL_TAKE_PROFIT}:
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

    def _state_save_payload(self) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Any, Dict[str, Any], Dict[str, Any]]:
        values = self.__dict__
        watchlist = values.get("watchlist")
        buylist_manager = values.get("buylist_manager")
        trade_manager = values.get("trade_manager")
        scanner_setups = values.get("scanner_setups", [])
        chart_drawings = values.get("chart_drawings", {})
        tab_options = values.get("tab_options", {})

        watchlist_dict = watchlist.to_dict() if watchlist is not None else {"name": "Default", "items": []}
        buylist_dict = buylist_manager.to_dict() if buylist_manager is not None else {"items": []}
        trade_manager_dict = trade_manager.to_dict() if trade_manager is not None else {"plans": []}
        scanner_setups_copy = list(scanner_setups) if isinstance(scanner_setups, list) else scanner_setups
        chart_drawings_copy = dict(chart_drawings) if isinstance(chart_drawings, dict) else chart_drawings
        tab_options_copy = dict(tab_options) if isinstance(tab_options, dict) else tab_options
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
                    "min_dollar_volume": float(values.get("min_dollar_volume", 35000.0)),
                    "min_adr": float(values.get("min_adr", 2.4)),
                    "min_growth_rank": float(values.get("min_growth_rank", 97.04)),
                    "min_trend_intensity": float(values.get("min_trend_intensity", 90.0)),
                }

                if "rules" in values and isinstance(values["rules"], list):
                    normalized_rules = []
                    for r in values["rules"]:
                        if isinstance(r, dict) and "attribute" in r:
                            normalized_rules.append({
                                "attribute": str(r.get("attribute")),
                                "operator": str(r.get("operator", ">=")),
                                "threshold": r.get("threshold", "")
                            })
                    setup_data["rules"] = normalized_rules
                else:
                    setup_data["rules"] = [
                        {"attribute": "volume", "operator": ">=", "threshold": setup_data["min_volume"]},
                        {"attribute": "dollar_volume", "operator": ">=", "threshold": setup_data["min_dollar_volume"]},
                        {"attribute": "adr_20", "operator": ">=", "threshold": setup_data["min_adr"]},
                        {"attribute": "growth_rank_1m", "operator": ">=", "threshold": setup_data["min_growth_rank"]},
                        {"attribute": "trend_intensity", "operator": ">=", "threshold": setup_data["min_trend_intensity"]},
                    ]

                setups[setup_name] = setup_data
            except (TypeError, ValueError):
                continue

        if not setups:
            setups = {name: values.copy() for name, values in DEFAULT_SCANNER_SETUPS.items()}
        return setups

    def _load_scanner_setups(self) -> dict:
        """Load persisted scanner setups."""
        return self._normalize_scanner_setups(load_scanner_setups_state(DEFAULT_SCANNER_SETUPS))

    def _flush_state_saves_for_shutdown(self, timeout: float = 5.0) -> SaveResult:
        manager = self._state_save_manager()
        deadline = time.monotonic() + timeout
        pending_timeout = min(3.0, timeout)
        pending_finished = manager.wait_for_pending_saves(timeout=pending_timeout)
        if not pending_finished:
            self.append_log("Timed out waiting for pending local state save before shutdown.")

        remaining = max(0.0, deadline - time.monotonic())
        return self._save_state_now(timeout=remaining, supersede_pending=True)

    @staticmethod
    def _stop_workers_for_shutdown(running_workers: List[QThread], timeout_ms: int = WORKER_SHUTDOWN_TIMEOUT_MS) -> bool:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        for worker in running_workers:
            worker.requestInterruption()
        for worker in running_workers:
            worker.quit()
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if not worker.wait(remaining_ms):
                return False
        return True

    def closeEvent(self, event) -> None:
        if self.live_data_timer is not None:
            self.live_data_timer.stop()
        if hasattr(self, "market_status_timer") and self.market_status_timer is not None:
            self.market_status_timer.stop()
        running_workers = [
            worker for worker in [
                self.scanner_worker,
                self.intraday_fetch_worker,
                self.intraday_bulk_worker,
                self.refresh_worker,
                self.hourly_refresh_worker,
                self.kis_account_worker,
                self.kis_startup_worker,
                self.order_reconciliation_worker,
                self.fx_rate_worker,
            ]
            if worker is not None and worker.isRunning()
        ]
        if not self._stop_workers_for_shutdown(running_workers, timeout_ms=WORKER_SHUTDOWN_TIMEOUT_MS):
            QMessageBox.warning(
                self,
                "Background task running",
                "A background data fetch is still running. Wait for it to finish before closing.",
            )
            event.ignore()
            return
        save_result = self._flush_state_saves_for_shutdown(timeout=5.0)
        if not save_result.success:
            message = save_result.error or "Unknown local state save error."
            self.append_log(f"Final local state save failed during shutdown: {message}")
            QMessageBox.warning(
                self,
                "Local Save Warning",
                f"Final local state save failed:\n\n{message}",
            )
        super().closeEvent(event)

    def _clear_worker_reference(self, attribute_name: str, worker: QThread) -> None:
        if getattr(self, attribute_name, None) is worker:
            setattr(self, attribute_name, None)

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
        self._add_configured_tab("tradingview", self.tradingview_widget, "TradingView Chart")
        self._build_tradingview_tab()

        self.intraday_charts_widget = QWidget()
        self._add_configured_tab("intraday_charts", self.intraday_charts_widget, "Intraday Charts")
        self._build_intraday_charts_tab()

        # Wire env combo â†’ watchlist refresh (Trade Plan tab removed)
        self.watchlist_env_combo.currentIndexChanged.connect(self.on_watchlist_env_changed)
        self.watchlist_env_combo.currentIndexChanged.connect(self.populate_watchlist_table)
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
        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        tools_menu = menubar.addMenu("Tools")
        refresh_action = tools_menu.addAction("Refresh Dashboard")
        refresh_action.triggered.connect(self.update_dashboard_summary)
        refresh_db_action = tools_menu.addAction("Update 1D Data")
        refresh_db_action.triggered.connect(self.refresh_data_to_db)
        scan_action = tools_menu.addAction("Run All Scanners")
        scan_action.triggered.connect(self.run_all_scanners)

        help_menu = menubar.addMenu("Help")
        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

        # Create a container widget for the US market status in the top right corner
        self.market_status_widget = QWidget()
        corner_layout = QHBoxLayout()
        corner_layout.setContentsMargins(0, 0, 10, 0)
        corner_layout.setSpacing(6)
        
        # Indicator circle (colored dot)
        self.market_status_dot = QLabel()
        self.market_status_dot.setFixedSize(10, 10)
        self.market_status_dot.setStyleSheet("border-radius: 5px; background-color: #f23645;")  # Default red
        
        # Text label
        self.market_status_label = QLabel("US Market: Calculating...")
        self.market_status_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #131722;")
        
        corner_layout.addWidget(self.market_status_dot)
        corner_layout.addWidget(self.market_status_label)
        self.market_status_widget.setLayout(corner_layout)
        
        menubar.setCornerWidget(self.market_status_widget, Qt.TopRightCorner)

        # Set up a 1-second timer to update the countdown
        self.market_status_timer = QTimer(self)
        self.market_status_timer.setInterval(1000)
        self.market_status_timer.timeout.connect(self.update_market_countdown_status)
        self.market_status_timer.start()
        self.update_market_countdown_status()

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
        progress_layout.addWidget(self.progress_bar, 3)
        progress_layout.addWidget(self.progress_label, 1)

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

    def append_log(self, message: str) -> None:
        if not hasattr(self, "log_output"):
            return
        timestamp = pd.Timestamp.now().strftime("%H:%M:%S")
        self.log_output.append(f"[{timestamp}] {message}")
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def update_progress(self, percent: int, current: int, total: int, eta: str) -> None:
        self.progress_bar.setValue(percent)
        self.progress_label.setText(f"Fetching {current}/{total} ({percent}%) - ETA {eta}")

    def show_ready(self) -> None:
        self.progress_bar.setValue(0)
        self.progress_label.setText("Ready.")

    def show_refresh_error(self, message: str) -> None:
        self.append_log(f"Error: {message}")
        self.progress_label.setText("Refresh failed.")

    def show_refresh_complete(self, updated_count: int) -> None:
        self.append_log(f"Refresh complete: {updated_count} symbols updated.")
        self.progress_label.setText("Refresh complete.")

    def update_market_countdown_status(self) -> None:
        """Update the market status countdown label (US Market hours)."""
        if not hasattr(self, "market_status_label"):
            return
        
        now_ny = dt.datetime.now(US_MARKET_ZONE)
        weekday = now_ny.weekday()
        
        dot = getattr(self, "market_status_dot", None)

        def _set_dot_open():
            if dot:
                dot.setStyleSheet("border-radius: 5px; background-color: #26a69a;")

        def _set_dot_closed():
            if dot:
                dot.setStyleSheet("border-radius: 5px; background-color: #f23645;")

        if weekday >= 5:
            # Weekend
            _set_dot_closed()
            self.market_status_label.setText("<b>Market Status:</b> Closed (Weekend Off Day)")
            return

        market_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)

        if now_ny < market_open:
            # Market day, before open
            _set_dot_closed()
            diff = market_open - now_ny
            seconds = int(diff.total_seconds())
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.market_status_label.setText(
                f"<b>Market Status:</b> Closed (Opens in {hours:02d}:{minutes:02d}:{seconds:02d})"
            )
        elif now_ny < market_close:
            # Market is open
            _set_dot_open()
            diff = market_close - now_ny
            seconds = int(diff.total_seconds())
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.market_status_label.setText(
                f"<b>Market Status:</b> <font color='#009688'><b>OPEN</b></font> (Closes in {hours:02d}:{minutes:02d}:{seconds:02d})"
            )
        else:
            # Market day, after close
            _set_dot_closed()
            if weekday == 4:
                # Friday after close
                self.market_status_label.setText("<b>Market Status:</b> Closed (Weekend Off Day)")
            else:
                self.market_status_label.setText("<b>Market Status:</b> Closed (After Hours)")

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
        if hasattr(self, 'intraday_up_shortcut'):
            self.intraday_up_shortcut.setKey(parse_key(shortcuts.get("prev_symbol", "Up")))
        if hasattr(self, 'intraday_down_shortcut'):
            self.intraday_down_shortcut.setKey(parse_key(shortcuts.get("next_symbol", "Down")))
        if hasattr(self, 'intraday_target_shortcut'):
            self.intraday_target_shortcut.setKey(parse_key(shortcuts.get("set_target", "T")))
        if hasattr(self, 'intraday_draw_shortcut'):
            self.intraday_draw_shortcut.setKey(parse_key(shortcuts.get("draw_line", "D")))
        if hasattr(self, 'intraday_erase_shortcut'):
            self.intraday_erase_shortcut.setKey(parse_key(shortcuts.get("erase_drawing", "E")))
        if hasattr(self, 'intraday_full_view_shortcut'):
            self.intraday_full_view_shortcut.setKey(parse_key(shortcuts.get("full_view", "A")))

        # 2. Charts tab shortcuts
        if hasattr(self, 'chart_target_shortcut'):
            self.chart_target_shortcut.setKey(parse_key(shortcuts.get("set_target", "T")))
        if hasattr(self, 'chart_draw_shortcut'):
            self.chart_draw_shortcut.setKey(parse_key(shortcuts.get("draw_line", "D")))
        if hasattr(self, 'chart_erase_shortcut'):
            self.chart_erase_shortcut.setKey(parse_key(shortcuts.get("erase_drawing", "E")))
        if hasattr(self, 'chart_left_shortcut'):
            self.chart_left_shortcut.setKey(parse_key(shortcuts.get("pan_left", "Left")))
        if hasattr(self, 'chart_right_shortcut'):
            self.chart_right_shortcut.setKey(parse_key(shortcuts.get("pan_right", "Right")))
        if hasattr(self, 'chart_up_shortcut'):
            self.chart_up_shortcut.setKey(parse_key(shortcuts.get("prev_symbol", "Up")))
        if hasattr(self, 'chart_down_shortcut'):
            self.chart_down_shortcut.setKey(parse_key(shortcuts.get("next_symbol", "Down")))
        if hasattr(self, 'chart_full_view_shortcut'):
            self.chart_full_view_shortcut.setKey(parse_key(shortcuts.get("full_view", "A")))

        # 3. TradingView widget shortcuts
        if hasattr(self, 'tradingview_draw_shortcut'):
            self.tradingview_draw_shortcut.setKey(parse_key(shortcuts.get("draw_line", "D")))
        if hasattr(self, 'tradingview_target_shortcut'):
            self.tradingview_target_shortcut.setKey(parse_key(shortcuts.get("set_target", "T")))
        if hasattr(self, 'tradingview_up_shortcut'):
            self.tradingview_up_shortcut.setKey(parse_key(shortcuts.get("prev_symbol", "Up")))
        if hasattr(self, 'tradingview_down_shortcut'):
            self.tradingview_down_shortcut.setKey(parse_key(shortcuts.get("next_symbol", "Down")))
        if hasattr(self, 'tradingview_full_view_shortcut'):
            self.tradingview_full_view_shortcut.setKey(parse_key(shortcuts.get("full_view", "A")))
        if hasattr(self, 'tradingview_watchlist_shortcut'):
            self.tradingview_watchlist_shortcut.setKey(parse_key(shortcuts.get("add_watchlist", "W")))

        # 4. Update Button Labels
        t_key = shortcuts.get("set_target", "T")
        d_key = shortcuts.get("draw_line", "D")
        e_key = shortcuts.get("erase_drawing", "E")
        a_key = shortcuts.get("full_view", "A")
        w_key = shortcuts.get("add_watchlist", "W")

        if hasattr(self, 'intraday_set_target_button'):
            self.intraday_set_target_button.setText(f"Set Breakout Price ({t_key})")
        if hasattr(self, 'intraday_draw_line_button'):
            self.intraday_draw_line_button.setText(f"Draw Line ({d_key})")
        if hasattr(self, 'intraday_erase_line_button'):
            self.intraday_erase_line_button.setText(f"Erase Drawing ({e_key})")
        if hasattr(self, 'intraday_full_view_button'):
            self.intraday_full_view_button.setText(f"Full View ({a_key})")

        if hasattr(self, 'chart_set_target_button'):
            self.chart_set_target_button.setText(f"Set Breakout Price ({t_key})")
        if hasattr(self, 'chart_draw_line_button'):
            self.chart_draw_line_button.setText(f"Draw Line ({d_key})")
        if hasattr(self, 'chart_erase_line_button'):
            self.chart_erase_line_button.setText(f"Erase Drawing ({e_key})")
        if hasattr(self, 'chart_full_view_button'):
            self.chart_full_view_button.setText(f"Full View ({a_key})")

        if hasattr(self, 'tradingview_set_target_button'):
            self.tradingview_set_target_button.setText(f"Set Breakout Price ({t_key})")
        if hasattr(self, 'tradingview_line_tool_button'):
            self.tradingview_line_tool_button.setText(f"Line Tool ({d_key})")
        if hasattr(self, 'tradingview_full_view_button'):
            self.tradingview_full_view_button.setText(f"Full View ({a_key})")
        if hasattr(self, 'tradingview_add_watchlist_button'):
            self.tradingview_add_watchlist_button.setText(f"Add Watchlist ({w_key})")

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
        QMessageBox.information(self, "Saved", "Local watchlist, trade plans, and scanner setups have been saved.")

    def _parse_float(self, value: QLineEdit, default: float) -> float:
        try:
            text = value.text().strip().replace("%", "")
            return float(text)
        except ValueError:
            return default

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

