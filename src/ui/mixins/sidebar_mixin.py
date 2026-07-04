from __future__ import annotations

import datetime as dt
import html
import json
import math
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QDockWidget, QLabel,
    QPushButton, QLineEdit, QFormLayout, QTableWidget, QTableWidgetItem,
    QListWidget, QListWidgetItem, QComboBox, QCheckBox, QSpinBox, QTextEdit,
    QProgressBar, QMessageBox, QGroupBox, QHeaderView, QAbstractItemView,
    QSizePolicy, QShortcut, QDialog, QKeySequenceEdit, QScrollArea,
    QTextBrowser, QSplitter, QSlider, QDialogButtonBox, QMenu
)
from PyQt5.QtCore import Qt, QThread, QTimer, QUrl
from PyQt5.QtGui import QColor, QKeySequence
try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.core.position_sizer import PositionSizer
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus, OPEN_ORDER_STATUSES
from src.core.orb import calculate_orb_range, evaluate_orb_entry_signal, resample_intraday_bars
from src.core.scanner import StockScanner, ComparisonOperator, ScanRule
from src.core.watchlist import Watchlist, TradePlanManager, TradePlan, BuylistManager, BuylistItem
from src.core.trade_reviewer import TradeReviewer, TradeSetup
from src.utils.data_loader import download_price_history, get_default_universe, _extract_symbol_history
from src.utils.db_loader import (
    init_mysql_engine, load_symbol_history_from_db, load_hourly_history_from_db,
    get_latest_price_history_date, get_latest_hourly_price_history_timestamp,
    load_chart_indicators_from_db, calculate_chart_indicators,
    refresh_chart_indicators_for_symbol, save_symbol_history_to_db,
    delete_intraday_history_for_symbol,
)
from src.utils.storage import load_json, save_json
from src.api.kis_account_snapshot_dual import KisEnvironment, discover_account_profiles, load_config
from src.services.app_state import (
    SCANNER_SETUPS_FILE, SETTINGS_FILE, load_buylist_state, load_chart_drawings_state,
    load_scanner_setups_state, load_tab_options_state, load_trade_plans_state,
    load_watchlist_state, save_app_state,
)
from src.services.intraday_data_service import format_intraday_source_label, load_best_intraday_history
from src.ui.chart_bridge import ChartBridge
from src.ui.dialogs import SettingsDialog, AddFilterDialog
from src.ui.filter_catalog import (
    DEFAULT_SCANNER_SETUPS, DEFAULT_SETTINGS, DEFAULT_TAB_OPTIONS,
    FILTER_CATALOG, SCANNER_METRICS_LABELS,
)
from src.ui.workers import (
    FxRateWorker, IntradayBulkFetchWorker, IntradayFetchWorker,
    KisAccountWorker, KisOrderWorker, KisStartupAccountsWorker, OrderReconciliationWorker,
    ScannerWorker, SingleStockAiWorker, WatchlistAiWorker,
)
from src.services.order_ledger import (
    append_order, find_open_orders, has_open_order, load_order_ledger,
    save_order_ledger, update_order,
)
from src.utils.intraday_helpers import (
    extract_latest_opening_bar as _extract_latest_opening_bar,
    intraday_cache_needs_backfill,
    utcnow_naive as _utcnow_naive,
)

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)



class SidebarMixin:
    def _build_stock_sidebar(self) -> None:
        """Build a left stock sidebar for non-scanner workflows."""
        self.stock_sidebar = QDockWidget("Stocks", self)
        self.stock_sidebar.setAllowedAreas(Qt.LeftDockWidgetArea)
        self.stock_sidebar.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.stock_sidebar.setMinimumWidth(150)
        self.stock_sidebar.setMaximumWidth(190)

        sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout()
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(6)

        self.sidebar_source_combo = QComboBox()
        self.sidebar_source_combo.setMinimumWidth(145)
        self.sidebar_source_combo.currentIndexChanged.connect(self.refresh_stock_sidebar)
        sidebar_layout.addWidget(self.sidebar_source_combo)

        self.sidebar_stock_list = QListWidget()
        self.sidebar_stock_list.setMinimumWidth(145)
        self.sidebar_stock_list.itemSelectionChanged.connect(self.on_sidebar_selection_changed)
        self.sidebar_stock_list.itemDoubleClicked.connect(self.sidebar_show_chart)
        sidebar_layout.addWidget(self.sidebar_stock_list, 1)

        self.sidebar_selected_label = QLabel("Selected: None")
        self.sidebar_selected_label.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_selected_label)

        add_button = QPushButton("Add to Watchlist")
        add_button.clicked.connect(self.sidebar_add_selected_to_watchlist)
        sidebar_layout.addWidget(add_button)

        trade_button = QPushButton("Use in Trade Plan")
        trade_button.clicked.connect(self.sidebar_load_trade_plan)
        sidebar_layout.addWidget(trade_button)

        chart_button = QPushButton("Show Chart")
        chart_button.clicked.connect(self.sidebar_show_chart)
        sidebar_layout.addWidget(chart_button)

        sidebar_widget.setLayout(sidebar_layout)
        self.stock_sidebar.setWidget(sidebar_widget)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.stock_sidebar)
        self.refresh_sidebar_sources()
    def refresh_sidebar_sources(self, selected_source: Optional[dict] = None) -> None:
        """Refresh sidebar source options for each scanner setup plus watchlist."""
        if not hasattr(self, "sidebar_source_combo"):
            return

        current_data = selected_source or self.sidebar_source_combo.currentData()
        self.sidebar_source_combo.blockSignals(True)
        self.sidebar_source_combo.clear()
        for setup_name in sorted(self.scanner_setups.keys()):
            self.sidebar_source_combo.addItem(f"Scan: {setup_name}", {"type": "scan", "setup": setup_name})
        self.sidebar_source_combo.addItem("Watchlist", {"type": "watchlist"})
        self.sidebar_source_combo.addItem("Buylist", {"type": "buylist"})

        selected_index = 0
        if isinstance(current_data, dict):
            for index in range(self.sidebar_source_combo.count()):
                if self.sidebar_source_combo.itemData(index) == current_data:
                    selected_index = index
                    break
        self.sidebar_source_combo.setCurrentIndex(selected_index)
        self.sidebar_source_combo.blockSignals(False)
        self.refresh_stock_sidebar()
    def on_tab_changed(self, *args) -> None:
        """Apply sidebar selection to the newly active tab."""
        if not hasattr(self, "stock_sidebar"):
            return
        self.stock_sidebar.setVisible(True)
        if self.tabs.currentWidget() is self.intraday_charts_widget:
            self._set_sidebar_source_to_watchlist()
        self.apply_sidebar_selection_to_current_tab()
    def _set_sidebar_source_to_watchlist(self) -> None:
        if not hasattr(self, "sidebar_source_combo"):
            return
        for index in range(self.sidebar_source_combo.count()):
            data = self.sidebar_source_combo.itemData(index) or {}
            if data.get("type") == "watchlist":
                if self.sidebar_source_combo.currentIndex() != index:
                    self.sidebar_source_combo.setCurrentIndex(index)
                return
    def refresh_stock_sidebar(self, *args) -> None:
        """Refresh sidebar stock list from scanner results or watchlist."""
        if not hasattr(self, "sidebar_stock_list"):
            return

        current_symbol = self._get_sidebar_selected_symbol()
        current_row = self.sidebar_stock_list.currentRow()
        self.sidebar_stock_list.clear()
        source = self.sidebar_source_combo.currentData() or {}

        if source.get("type") == "scan":
            setup_name = source.get("setup", "")
            for stock in self.scanner_results_by_setup.get(setup_name, []):
                symbol = stock.get("symbol", "")
                price = stock.get("price")
                label = f"{symbol}"
                if price is not None:
                    label += f"  {float(price):.2f}"
                if stock.get("orb_score"):
                    label += f"  ORB {float(stock.get('orb_score', 0.0)):.0f}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "symbol": symbol,
                    "name": stock.get("name", symbol),
                    "price": price,
                    "orb_score": stock.get("orb_score"),
                    "orb_plan": stock.get("orb_plan"),
                    "source": "scanner",
                    "setup": setup_name,
                })
                self.sidebar_stock_list.addItem(item)
        elif source.get("type") == "buylist":
            for buy_item in self.buylist_manager.items:
                label = f"{buy_item.symbol}  {buy_item.entry_price:.2f}  Score {buy_item.total_score:.0f}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "symbol": buy_item.symbol,
                    "name": buy_item.name,
                    "price": buy_item.entry_price,
                    "source": "buylist",
                    "breakout_price": buy_item.breakout_price,
                    "stop_loss": buy_item.stop_loss,
                    "notes": buy_item.notes,
                    "ai_summary": buy_item.ai_summary,
                })
                self.sidebar_stock_list.addItem(item)
        else:
            for watch_item in self.watchlist.items:
                label = watch_item.symbol
                if watch_item.entry_price is not None:
                    label += f"  {watch_item.entry_price:.2f}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "symbol": watch_item.symbol,
                    "name": watch_item.name,
                    "price": watch_item.entry_price,
                    "source": "watchlist",
                })
                self.sidebar_stock_list.addItem(item)

        if current_symbol:
            for row in range(self.sidebar_stock_list.count()):
                item = self.sidebar_stock_list.item(row)
                data = item.data(Qt.UserRole) or {}
                if data.get("symbol") == current_symbol:
                    self.sidebar_stock_list.setCurrentRow(row)
                    break
        if self.sidebar_stock_list.currentRow() < 0 and self.sidebar_stock_list.count() > 0:
            # Symbol no longer in list (e.g. removed) — stay at same position rather than jumping to top
            restore = min(current_row, self.sidebar_stock_list.count() - 1) if current_row >= 0 else 0
            self.sidebar_stock_list.setCurrentRow(restore)
        self.on_sidebar_selection_changed()
    def _get_sidebar_selected_data(self) -> Optional[dict]:
        if not hasattr(self, "sidebar_stock_list"):
            return None
        item = self.sidebar_stock_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return data if isinstance(data, dict) else None
    def _get_sidebar_selected_symbol(self) -> Optional[str]:
        data = self._get_sidebar_selected_data()
        return data.get("symbol") if data else None
    def _sidebar_symbols(self) -> List[str]:
        if not hasattr(self, "sidebar_stock_list"):
            return []
        symbols = []
        for row in range(self.sidebar_stock_list.count()):
            item = self.sidebar_stock_list.item(row)
            data = item.data(Qt.UserRole) or {}
            symbol = str(data.get("symbol", "")).strip().upper()
            if symbol:
                symbols.append(symbol)
        return symbols
    def on_sidebar_selection_changed(self) -> None:
        """Update shared selection state from sidebar."""
        data = self._get_sidebar_selected_data()
        if not data:
            self.sidebar_selected_label.setText("Selected: None")
            return

        symbol = data.get("symbol", "")
        self.selected_scan_symbol = symbol
        self._set_chart_symbol(symbol)
        self.sidebar_selected_label.setText(f"Selected: {symbol}")
        self.apply_sidebar_selection_to_current_tab()
    def apply_sidebar_selection_to_current_tab(self) -> None:
        """Apply selected sidebar stock to the active workflow tab."""
        if not hasattr(self, "tabs"):
            return
        data = self._get_sidebar_selected_data()
        if not data:
            return

        symbol = data.get("symbol", "")
        name = data.get("name", symbol)
        price = data.get("price")
        current_widget = self.tabs.currentWidget()

        if current_widget is self.watchlist_widget:
            self.watchlist_symbol_input.setText(symbol)
            self.watchlist_name_input.setText(name)
        elif hasattr(self, "trade_plan_widget") and current_widget is self.trade_plan_widget:
            self._seed_trade_plan_fields(symbol=symbol, price=price, name=name, overwrite=True)
        elif current_widget is self.charts_widget:
            self._set_chart_symbol(symbol)
            self.plot_selected_symbol(show_warnings=False, use_live_fallback=False)
        elif current_widget is self.intraday_charts_widget:
            self._set_intraday_symbol(symbol)
            self.plot_intraday_watchlist_symbol()
        elif current_widget is self.tradingview_widget:
            self._set_tradingview_symbol(symbol)
            if hasattr(self, "tradingview_symbol_combo"):
                self.load_tradingview_chart(force=True)
        elif current_widget is self.scanner_widget:
            self.scanner_selection_label.setText(f"Selected symbol: {symbol}")
            self.update_scanner_preview_chart(symbol)
    def sidebar_add_selected_to_watchlist(self) -> None:
        """Add selected sidebar stock to the watchlist."""
        data = self._get_sidebar_selected_data()
        if not data:
            QMessageBox.warning(self, "No selection", "Select a stock from the sidebar first.")
            return

        symbol = data.get("symbol", "")
        self.watchlist.add(
            symbol=symbol,
            name=data.get("name", symbol),
            entry_price=data.get("price"),
        )
        self.populate_watchlist_table()
        self.update_dashboard_summary()
        self._save_state()
        self.sidebar_source_combo.setCurrentText("Watchlist")
        self.refresh_stock_sidebar()
        self.prefetch_intraday_cache_for_symbol(symbol)
        self.append_log(f"Added/updated {symbol} in watchlist from sidebar.")
    def sidebar_load_trade_plan(self) -> None:
        """Load selected sidebar stock — Trade Plan tab removed, redirect to Watchlist + chart."""
        data = self._get_sidebar_selected_data()
        if not data:
            QMessageBox.warning(self, "No selection", "Select a stock from the sidebar first.")
            return

        symbol = data.get("symbol", "")
        price = data.get("price")
        name = data.get("name", "")

        self._set_chart_symbol(symbol)
        self.refresh_watchlist_orb_panel(symbol)
        if hasattr(self, "watchlist_widget"):
            self.tabs.setCurrentWidget(self.watchlist_widget)
    def sidebar_show_chart(self, *args) -> None:
        """Show selected sidebar stock on the TradingView chart tab or Charts tab (if Buylist)."""
        data = self._get_sidebar_selected_data()
        if not data:
            QMessageBox.warning(self, "No selection", "Select a stock from the sidebar first.")
            return

        symbol = data.get("symbol", "")
        self._set_chart_symbol(symbol)

        if data.get("source") == "buylist":
            self.tabs.setCurrentWidget(self.charts_widget)
            self.plot_selected_symbol(show_warnings=False, use_live_fallback=False)
        else:
            if hasattr(self, "tradingview_symbol_combo"):
                self._set_tradingview_symbol(symbol)
            self.tabs.setCurrentWidget(self.tradingview_widget)
            self.load_tradingview_chart(force=True)
