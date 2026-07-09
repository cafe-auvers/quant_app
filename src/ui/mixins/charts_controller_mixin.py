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



class ChartsControllerMixin:
    def _build_combined_drawings(self, symbol: str, timeframe: str) -> list:
        return list(self.chart_drawings.get(symbol, []))
    def _build_trade_plan_tab(self) -> None:
        """Build content for the trade plan tab."""
        layout = QHBoxLayout()

        form_group = QGroupBox("Trade Plan")
        form_layout = QFormLayout()
        self.symbol_input = QLineEdit()
        self.entry_price_input = QLineEdit()
        self.stop_loss_input = QLineEdit()
        self.take_profit_input = QLineEdit()
        self.position_size_input = QLineEdit()
        self.account_size_input = QLineEdit("100000")
        self.usd_krw_rate_input = QLineEdit("1388.89")
        self.usd_krw_rate_input.setReadOnly(True)
        self.risk_percent_input = QLineEdit("1")
        self.reason_input = QTextEdit()
        self.trade_kis_environment_combo = QComboBox()
        self.trade_kis_environment_combo.addItems([KisEnvironment.SIM.value, KisEnvironment.PROD.value])
        self.trade_kis_environment_combo.currentTextChanged.connect(self.populate_trade_account_combo)
        self.trade_kis_account_combo = QComboBox()
        self.trade_kis_account_combo.currentIndexChanged.connect(self.apply_cached_trade_account_size)
        account_button = QPushButton("Use KIS Account Value")
        account_button.clicked.connect(self.refresh_trade_account_size)
        fx_button = QPushButton("Refresh USD/KRW")
        fx_button.clicked.connect(lambda: self.refresh_usd_krw_rate(show_messages=True))
        refresh_orb_button = QPushButton("Refresh ORB Plan")
        refresh_orb_button.clicked.connect(self.refresh_orb_trade_plan_table)

        form_layout.addRow("Symbol", self.symbol_input)
        form_layout.addRow("Entry Price", self.entry_price_input)
        form_layout.addRow("Stop Loss", self.stop_loss_input)
        form_layout.addRow("Position Size", self.position_size_input)
        form_layout.addRow("KIS Profile", self.trade_kis_environment_combo)
        form_layout.addRow("KIS Account", self.trade_kis_account_combo)
        form_layout.addRow("Account Size USD", self.account_size_input)
        form_layout.addRow("USD to KRW", self.usd_krw_rate_input)
        self.usd_krw_rate_status_label = QLabel("USD/KRW not refreshed")
        form_layout.addRow("FX Source", self.usd_krw_rate_status_label)
        form_layout.addRow(fx_button)
        form_layout.addRow(account_button)
        form_layout.addRow("Risk %", self.risk_percent_input)
        form_layout.addRow(refresh_orb_button)
        form_layout.addRow("Reason", self.reason_input)

        save_button = QPushButton("Save Plan")
        save_button.setObjectName("savePlanButton")
        save_button.clicked.connect(self.save_trade_plan)
        save_button.setVisible(False)
        form_layout.addRow(save_button)

        self.trade_review_output = QLabel("Review result will appear here.")
        self.trade_review_output.setWordWrap(True)
        form_layout.addRow(self.trade_review_output)

        for input_widget in [
            self.symbol_input,
            self.entry_price_input,
            self.stop_loss_input,
            self.take_profit_input,
            self.account_size_input,
            self.usd_krw_rate_input,
            self.risk_percent_input,
        ]:
            input_widget.textChanged.connect(self.update_trade_plan_feedback)
            input_widget.textChanged.connect(self.refresh_orb_trade_plan_table)
        self.account_size_input.textChanged.connect(self.on_account_size_text_changed)
        self.account_size_input.textChanged.connect(self.recalculate_watchlist_scoreboard_sizes)
        self.risk_percent_input.textChanged.connect(self.recalculate_watchlist_scoreboard_sizes)
        self.reason_input.textChanged.connect(self.update_trade_plan_feedback)

        form_group.setLayout(form_layout)
        layout.addWidget(form_group, 1)

        right_layout = QVBoxLayout()
        self.orb_trade_plan_table = QTableWidget(0, 10)
        self.orb_trade_plan_table.setHorizontalHeaderLabels([""] * 10)
        self.orb_trade_plan_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.orb_trade_plan_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        right_layout.addWidget(QLabel("ORB Position Plan"))
        self.orb_valid_only_checkbox = QCheckBox("Show valid plans only")
        self.orb_valid_only_checkbox.setChecked(True)
        self.orb_valid_only_checkbox.stateChanged.connect(self.refresh_orb_trade_plan_table)
        right_layout.addWidget(self.orb_valid_only_checkbox)
        right_layout.addWidget(self.orb_trade_plan_table, 2)

        self.trade_plan_table = QTableWidget(0, 5)
        self.trade_plan_table.setHorizontalHeaderLabels([
            "Symbol",
            "Entry",
            "Stop",
            "Exit Model",
            "Status",
        ])
        self.trade_plan_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.trade_plan_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.trade_plan_table.cellDoubleClicked.connect(self.load_saved_trade_plan)
        self.trade_plan_table.setVisible(False)
        layout.addLayout(right_layout, 2)

        self.trade_plan_widget.setLayout(layout)
        self.populate_trade_account_combo()
        self.populate_trade_plan_table()
        self.refresh_orb_trade_plan_table()
    def _build_intraday_charts_tab(self) -> None:
        """Build a watchlist-only intraday chart tab."""
        layout = QVBoxLayout()
        controls_layout = QHBoxLayout()

        self.intraday_symbol_combo = QComboBox()
        self.populate_intraday_watchlist_symbols()
        self.intraday_interval_combo = QComboBox()
        self.intraday_interval_combo.addItems(["5m", "30m", "1h"])
        self.intraday_interval_combo.setCurrentText("1h")
        self.intraday_window_combo = QComboBox()
        self.intraday_window_combo.addItems(["1D", "3D", "5D", "7D"])
        self.intraday_window_combo.setCurrentText("7D")
        refresh_button = QPushButton("Refresh Intraday Chart (R)")
        refresh_button.clicked.connect(self.plot_intraday_watchlist_symbol)

        controls_layout.addWidget(QLabel("Watchlist symbol:"))
        controls_layout.addWidget(self.intraday_symbol_combo)
        controls_layout.addWidget(QLabel("Interval:"))
        controls_layout.addWidget(self.intraday_interval_combo)
        controls_layout.addWidget(QLabel("Window:"))
        controls_layout.addWidget(self.intraday_window_combo)
        controls_layout.addWidget(refresh_button)
        controls_layout.addStretch(1)
        layout.addLayout(controls_layout)

        settings_layout = QHBoxLayout()
        settings_layout.addWidget(QLabel("Chart settings:"))
        self.intraday_show_volume_checkbox = QCheckBox("Volume")
        self.intraday_show_volume_checkbox.setChecked(True)
        self.intraday_show_ema_checkbox = QCheckBox("EMA lines")
        self.intraday_show_ema_checkbox.setChecked(False)
        self.intraday_show_rs_checkbox = QCheckBox("RS vs SPY")
        self.intraday_show_rs_checkbox.setChecked(False)
        self.intraday_show_rs_checkbox.setEnabled(False)
        for checkbox in [
            self.intraday_show_volume_checkbox,
            self.intraday_show_ema_checkbox,
            self.intraday_show_rs_checkbox,
        ]:
            checkbox.stateChanged.connect(lambda _state: self.plot_intraday_watchlist_symbol())
            settings_layout.addWidget(checkbox)
        settings_layout.addStretch(1)
        layout.addLayout(settings_layout)

        self.intraday_status_label = QLabel("Select a watchlist symbol to load intraday data.")
        self.intraday_status_label.setWordWrap(True)
        layout.addWidget(self.intraday_status_label)

        if QWebEngineView is not None:
            self.intraday_chart_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.intraday_chart_channel = QWebChannel()
                self.intraday_chart_channel.registerObject("chartBridge", self.chart_bridge)
                self.intraday_chart_view.page().setWebChannel(self.intraday_chart_channel)
        else:
            self.intraday_chart_view = QTextEdit()
            self.intraday_chart_view.setReadOnly(True)

        chart_area_layout = QVBoxLayout()
        self.intraday_set_target_button = QPushButton("Set Breakout Price (T)")
        self.intraday_set_target_button.clicked.connect(self.enable_chart_target_mode)
        self.intraday_draw_line_button = QPushButton("Draw Line (D)")
        self.intraday_draw_line_button.clicked.connect(self.enable_chart_drawing_mode)
        self.intraday_erase_line_button = QPushButton("Erase Drawing (E)")
        self.intraday_erase_line_button.setObjectName("eraseLineButton")
        self.intraday_erase_line_button.clicked.connect(self.enable_chart_erase_mode)
        self.intraday_erase_all_button = QPushButton("Erase All")
        self.intraday_erase_all_button.setObjectName("eraseAllButton")
        self.intraday_erase_all_button.clicked.connect(self.clear_current_chart_drawings)
        self.intraday_full_view_button = QPushButton("Full View (F)")
        self.intraday_full_view_button.clicked.connect(self.reset_chart_full_view)
        self.intraday_queue_btn = QPushButton("Queue for Buy (Q)")
        self.intraday_queue_btn.setMinimumWidth(150)
        self.intraday_queue_btn.clicked.connect(self._intraday_queue_toggle)
        self.intraday_activate_btn = QPushButton("Activate (A)")
        self.intraday_activate_btn.setMinimumWidth(110)
        self.intraday_activate_btn.clicked.connect(self._intraday_activate_toggle)

        chart_area_layout.addWidget(self.intraday_chart_view, 1)
        intraday_tools_layout = QHBoxLayout()
        intraday_tools_layout.addWidget(self.intraday_set_target_button)
        intraday_tools_layout.addWidget(self.intraday_draw_line_button)
        intraday_tools_layout.addWidget(self.intraday_erase_line_button)
        intraday_tools_layout.addWidget(self.intraday_erase_all_button)
        intraday_tools_layout.addWidget(self.intraday_queue_btn)
        intraday_tools_layout.addWidget(self.intraday_activate_btn)
        intraday_tools_layout.addWidget(self.intraday_full_view_button)
        intraday_tools_layout.addStretch(1)
        chart_area_layout.addLayout(intraday_tools_layout)
        layout.addLayout(chart_area_layout, 1)
        self.intraday_charts_widget.setLayout(layout)

        # Update queue/activate buttons whenever symbol changes
        self.intraday_symbol_combo.currentTextChanged.connect(self._update_intraday_queue_btn)
        self.intraday_symbol_combo.currentTextChanged.connect(self._update_intraday_activate_btn)

        self.intraday_up_shortcut = QShortcut(QKeySequence(Qt.Key_Up), self.intraday_charts_widget)
        self.intraday_up_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_up_shortcut.activated.connect(lambda: self.step_intraday_watchlist_symbol(-1))
        self.intraday_down_shortcut = QShortcut(QKeySequence(Qt.Key_Down), self.intraday_charts_widget)
        self.intraday_down_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_down_shortcut.activated.connect(lambda: self.step_intraday_watchlist_symbol(1))
        self.intraday_target_shortcut = QShortcut(QKeySequence("T"), self.intraday_charts_widget)
        self.intraday_target_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_target_shortcut.activated.connect(self.enable_chart_target_mode)
        self.intraday_draw_shortcut = QShortcut(QKeySequence("D"), self.intraday_charts_widget)
        self.intraday_draw_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_draw_shortcut.activated.connect(self.enable_chart_drawing_mode)
        self.intraday_erase_shortcut = QShortcut(QKeySequence("E"), self.intraday_charts_widget)
        self.intraday_erase_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_erase_shortcut.activated.connect(self.enable_chart_erase_mode)
        self.intraday_full_view_shortcut = QShortcut(QKeySequence("F"), self.intraday_charts_widget)
        self.intraday_full_view_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_full_view_shortcut.activated.connect(self.reset_chart_full_view)
        self.intraday_queue_shortcut = QShortcut(QKeySequence("Q"), self.intraday_charts_widget)
        self.intraday_queue_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_queue_shortcut.activated.connect(self._intraday_queue_toggle)
        self.intraday_activate_shortcut = QShortcut(QKeySequence("A"), self.intraday_charts_widget)
        self.intraday_activate_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_activate_shortcut.activated.connect(self._intraday_activate_toggle)
        self.intraday_refresh_shortcut = QShortcut(QKeySequence("R"), self.intraday_charts_widget)
        self.intraday_refresh_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_refresh_shortcut.activated.connect(self._intraday_force_refresh)
        self._update_intraday_queue_btn()
        self._update_intraday_activate_btn()
    def _intraday_force_refresh(self) -> None:
        symbol = self.intraday_symbol_combo.currentText().strip().upper() if hasattr(self, "intraday_symbol_combo") else ""
        if symbol:
            window_days = self._get_intraday_window_days()
            self.start_intraday_fetch(symbol, window_days=window_days)
        self.plot_intraday_watchlist_symbol(allow_fetch=False)
    def _intraday_queue_toggle(self) -> None:
        symbol = self.intraday_symbol_combo.currentText().strip().upper() if hasattr(self, "intraday_symbol_combo") else ""
        if not symbol:
            return
        self._chart_queue_toggle(symbol)
        self._update_intraday_queue_btn()
    def _update_intraday_queue_btn(self, _text: str = "") -> None:
        btn = getattr(self, "intraday_queue_btn", None)
        if btn is None:
            return
        symbol = self.intraday_symbol_combo.currentText().strip().upper() if hasattr(self, "intraday_symbol_combo") else ""
        self._apply_chart_queue_btn_state(symbol, btn)
    def _build_charts_tab(self) -> None:
        """Build content for the charts tab."""
        layout = QVBoxLayout()
        button_layout = QHBoxLayout()
        self.chart_symbol_input = QComboBox()
        self.chart_symbol_input.setEditable(True)
        self.chart_symbol_input.setInsertPolicy(QComboBox.NoInsert)
        self.chart_symbol_input.lineEdit().setPlaceholderText("Symbol (or select scanner row)")
        self.populate_chart_symbol_combo()
        self.chart_symbol_input.lineEdit().textEdited.connect(self.filter_chart_symbol_combo)
        self.chart_symbol_input.activated.connect(lambda _index: self.plot_selected_symbol(show_warnings=False))
        plot_button = QPushButton("Plot Selected Symbol")
        plot_button.clicked.connect(lambda: self.plot_selected_symbol(use_live_fallback=True))

        button_layout.addWidget(QLabel("Symbol:"))
        button_layout.addWidget(self.chart_symbol_input)
        self.chart_timeframe_combo = QComboBox()
        self.chart_timeframe_combo.addItems(["1D", "1H"])
        self.chart_timeframe_combo.currentTextChanged.connect(lambda _text: self.plot_selected_symbol(show_warnings=False))
        button_layout.addWidget(QLabel("Timeframe:"))
        button_layout.addWidget(self.chart_timeframe_combo)
        self.chart_split_screen_checkbox = QCheckBox("Split 1D / 1H")
        self.chart_split_screen_checkbox.stateChanged.connect(lambda _state: self.plot_selected_symbol(show_warnings=False))
        button_layout.addWidget(self.chart_split_screen_checkbox)
        button_layout.addWidget(plot_button)

        layout.addLayout(button_layout)

        settings_layout = QHBoxLayout()
        settings_layout.addWidget(QLabel("Chart settings:"))
        self.chart_show_volume_checkbox = QCheckBox("Volume")
        self.chart_show_volume_checkbox.setChecked(True)
        self.chart_show_rs_checkbox = QCheckBox("RS vs SPY")
        self.chart_show_rs_checkbox.setChecked(True)
        self.chart_show_ema_checkbox = QCheckBox("EMA lines")
        self.chart_show_ema_checkbox.setChecked(True)
        self.chart_show_adr_checkbox = QCheckBox("ADR")
        self.chart_show_adr_checkbox.setChecked(True)
        self.chart_show_growth_1m_checkbox = QCheckBox("1M growth")
        self.chart_show_growth_1m_checkbox.setChecked(True)
        self.chart_show_growth_3m_checkbox = QCheckBox("3M growth")
        self.chart_show_growth_3m_checkbox.setChecked(True)
        self.chart_show_growth_6m_checkbox = QCheckBox("6M growth")
        self.chart_show_growth_6m_checkbox.setChecked(False)

        for checkbox in [
            self.chart_show_volume_checkbox,
            self.chart_show_rs_checkbox,
            self.chart_show_ema_checkbox,
            self.chart_show_adr_checkbox,
            self.chart_show_growth_1m_checkbox,
            self.chart_show_growth_3m_checkbox,
            self.chart_show_growth_6m_checkbox,
        ]:
            checkbox.stateChanged.connect(lambda _state: self.plot_selected_symbol(show_warnings=False))
            settings_layout.addWidget(checkbox)
        settings_layout.addStretch(1)
        layout.addLayout(settings_layout)

        if QWebEngineView is not None:
            self.chart_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.chart_channel = QWebChannel()
                self.chart_channel.registerObject("chartBridge", self.chart_bridge)
                self.chart_view.page().setWebChannel(self.chart_channel)
            self.chart_split_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.chart_split_channel = QWebChannel()
                self.chart_split_channel.registerObject("chartBridge", self.chart_bridge)
                self.chart_split_view.page().setWebChannel(self.chart_split_channel)
        else:
            self.chart_view = QTextEdit()
            self.chart_view.setReadOnly(True)
            self.chart_split_view = QTextEdit()
            self.chart_split_view.setReadOnly(True)

        chart_area_layout = QVBoxLayout()
        self.chart_set_target_button = QPushButton("Set Breakout Price (T)")
        self.chart_set_target_button.clicked.connect(self.enable_chart_target_mode)
        self.chart_draw_line_button = QPushButton("Draw Line (D)")
        self.chart_draw_line_button.clicked.connect(self.enable_chart_drawing_mode)
        self.chart_erase_line_button = QPushButton("Erase Drawing (E)")
        self.chart_erase_line_button.setObjectName("eraseLineButton")
        self.chart_erase_line_button.clicked.connect(self.enable_chart_erase_mode)
        self.chart_erase_all_button = QPushButton("Erase All")
        self.chart_erase_all_button.setObjectName("eraseAllButton")
        self.chart_erase_all_button.clicked.connect(self.clear_current_chart_drawings)
        self.chart_full_view_button = QPushButton("Full View (F)")
        self.chart_full_view_button.clicked.connect(self.reset_chart_full_view)

        chart_views_layout = QHBoxLayout()
        chart_views_layout.addWidget(self.chart_view, 1)
        chart_views_layout.addWidget(self.chart_split_view, 1)
        self.chart_split_view.setVisible(False)
        chart_area_layout.addLayout(chart_views_layout, 1)
        chart_tools_layout = QHBoxLayout()
        chart_tools_layout.addWidget(self.chart_set_target_button)
        chart_tools_layout.addWidget(self.chart_draw_line_button)
        chart_tools_layout.addWidget(self.chart_erase_line_button)
        chart_tools_layout.addWidget(self.chart_erase_all_button)
        chart_tools_layout.addWidget(self.chart_full_view_button)
        chart_tools_layout.addStretch(1)
        chart_area_layout.addLayout(chart_tools_layout)

        layout.addLayout(chart_area_layout, 1)
        self.charts_widget.setLayout(layout)
        self.chart_target_shortcut = QShortcut(QKeySequence("T"), self.charts_widget)
        self.chart_target_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_target_shortcut.activated.connect(self.enable_chart_target_mode)
        self.chart_draw_shortcut = QShortcut(QKeySequence("D"), self.charts_widget)
        self.chart_draw_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_draw_shortcut.activated.connect(self.enable_chart_drawing_mode)
        self.chart_erase_shortcut = QShortcut(QKeySequence("E"), self.charts_widget)
        self.chart_erase_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_erase_shortcut.activated.connect(self.enable_chart_erase_mode)
        self.chart_left_shortcut = QShortcut(QKeySequence(Qt.Key_Left), self.charts_widget)
        self.chart_left_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_left_shortcut.activated.connect(lambda: self.pan_chart_window(-self._chart_pan_step_bars()))
        self.chart_right_shortcut = QShortcut(QKeySequence(Qt.Key_Right), self.charts_widget)
        self.chart_right_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_right_shortcut.activated.connect(lambda: self.pan_chart_window(self._chart_pan_step_bars()))
        self.chart_up_shortcut = QShortcut(QKeySequence(Qt.Key_Up), self.charts_widget)
        self.chart_up_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_up_shortcut.activated.connect(lambda: self.step_chart_symbol(-1))
        self.chart_down_shortcut = QShortcut(QKeySequence(Qt.Key_Down), self.charts_widget)
        self.chart_down_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_down_shortcut.activated.connect(lambda: self.step_chart_symbol(1))
        self.chart_full_view_shortcut = QShortcut(QKeySequence("F"), self.charts_widget)
        self.chart_full_view_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_full_view_shortcut.activated.connect(self.reset_chart_full_view)
        self.chart_load_shortcut = QShortcut(QKeySequence(Qt.Key_F4), self.charts_widget)
        self.chart_load_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.chart_load_shortcut.activated.connect(lambda: self.plot_selected_symbol(use_live_fallback=True))
        self._draw_placeholder_chart()
    def _build_tradingview_tab(self) -> None:
        """Build a TradingView widget tab for watchlist symbols."""
        layout = QVBoxLayout()

        controls_layout = QHBoxLayout()
        self.tradingview_symbol_combo = QComboBox()
        self.tradingview_symbol_combo.setMinimumWidth(180)
        self.tradingview_symbol_combo.setEditable(True)
        self.tradingview_symbol_combo.lineEdit().textEdited.connect(self.filter_tradingview_symbol_combo)
        self.populate_tradingview_watchlist_symbols()
        self.tradingview_symbol_combo.activated.connect(lambda _index: self.load_tradingview_chart(force=True))

        previous_button = QPushButton("Previous")
        previous_button.clicked.connect(lambda: self.step_tradingview_watchlist_symbol(-1))
        next_button = QPushButton("Next")
        next_button.clicked.connect(lambda: self.step_tradingview_watchlist_symbol(1))
        refresh_button = QPushButton("Load Chart (R)")
        refresh_button.clicked.connect(lambda: self.load_tradingview_chart(force=True, fetch_live=True))

        controls_layout.addWidget(QLabel("Symbol:"))
        controls_layout.addWidget(self.tradingview_symbol_combo)
        self.tradingview_timeframe_combo = QComboBox()
        self.tradingview_timeframe_combo.addItems(["1D", "1H", "5M"])
        self.tradingview_timeframe_combo.currentTextChanged.connect(lambda _text: self.load_tradingview_chart(force=True))
        controls_layout.addWidget(QLabel("Timeframe:"))
        controls_layout.addWidget(self.tradingview_timeframe_combo)
        self.tradingview_window_combo = QComboBox()
        self.tradingview_window_combo.addItems(["1D", "3D", "5D", "7D"])
        self.tradingview_window_combo.setCurrentText("7D")
        self.tradingview_window_combo.currentTextChanged.connect(lambda _text: self.load_tradingview_chart(force=True))
        controls_layout.addWidget(QLabel("5M window:"))
        controls_layout.addWidget(self.tradingview_window_combo)
        self.tradingview_split_screen_checkbox = QCheckBox("Split 1D / 1H")
        self.tradingview_split_screen_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        self.tradingview_show_volume_checkbox = QCheckBox("Volume")
        self.tradingview_show_volume_checkbox.setChecked(True)
        self.tradingview_show_volume_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        self.tradingview_show_ema_checkbox = QCheckBox("EMA 10/20/50")
        self.tradingview_show_ema_checkbox.setChecked(True)
        self.tradingview_show_ema_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        self.tradingview_show_rs_checkbox = QCheckBox("RS/TI65")
        self.tradingview_show_rs_checkbox.setChecked(True)
        self.tradingview_show_rs_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        self.tradingview_show_adr_checkbox = QCheckBox("ADR")
        self.tradingview_show_adr_checkbox.setChecked(True)
        self.tradingview_show_adr_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        self.tradingview_show_growth_1m_checkbox = QCheckBox("1M growth")
        self.tradingview_show_growth_1m_checkbox.setChecked(True)
        self.tradingview_show_growth_1m_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        self.tradingview_show_growth_3m_checkbox = QCheckBox("3M growth")
        self.tradingview_show_growth_3m_checkbox.setChecked(True)
        self.tradingview_show_growth_3m_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        self.tradingview_show_growth_6m_checkbox = QCheckBox("6M growth")
        self.tradingview_show_growth_6m_checkbox.setChecked(False)
        self.tradingview_show_growth_6m_checkbox.stateChanged.connect(lambda _state: self.load_tradingview_chart(force=True))
        controls_layout.addWidget(self.tradingview_split_screen_checkbox)
        controls_layout.addWidget(self.tradingview_show_volume_checkbox)
        controls_layout.addWidget(self.tradingview_show_ema_checkbox)
        controls_layout.addWidget(self.tradingview_show_rs_checkbox)
        controls_layout.addWidget(self.tradingview_show_adr_checkbox)
        controls_layout.addWidget(self.tradingview_show_growth_1m_checkbox)
        controls_layout.addWidget(self.tradingview_show_growth_3m_checkbox)
        controls_layout.addWidget(self.tradingview_show_growth_6m_checkbox)
        controls_layout.addWidget(previous_button)
        controls_layout.addWidget(next_button)
        controls_layout.addWidget(refresh_button)
        controls_layout.addStretch(1)
        layout.addLayout(controls_layout)

        self.tradingview_status_label = QLabel("TradingView widget uses public market symbols and requires internet access.")
        self.tradingview_status_label.setWordWrap(True)
        layout.addWidget(self.tradingview_status_label)

        if QWebEngineView is not None:
            self.tradingview_chart_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.tradingview_chart_channel = QWebChannel()
                self.tradingview_chart_channel.registerObject("chartBridge", self.chart_bridge)
                self.tradingview_chart_view.page().setWebChannel(self.tradingview_chart_channel)
            self.tradingview_split_chart_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.tradingview_split_chart_channel = QWebChannel()
                self.tradingview_split_chart_channel.registerObject("chartBridge", self.chart_bridge)
                self.tradingview_split_chart_view.page().setWebChannel(self.tradingview_split_chart_channel)
        else:
            self.tradingview_chart_view = QTextEdit()
            self.tradingview_chart_view.setReadOnly(True)
            self.tradingview_split_chart_view = QTextEdit()
            self.tradingview_split_chart_view.setReadOnly(True)

        tradingview_views_layout = QHBoxLayout()
        tradingview_views_layout.addWidget(self.tradingview_chart_view, 1)
        tradingview_views_layout.addWidget(self.tradingview_split_chart_view, 1)
        self.tradingview_split_chart_view.setVisible(False)
        layout.addLayout(tradingview_views_layout, 1)
        tools_layout = QHBoxLayout()
        self.tradingview_line_tool_button = QPushButton("Line Tool (D)")
        self.tradingview_line_tool_active = False
        self.tradingview_line_tool_button.clicked.connect(self.toggle_tradingview_line_tool_mode)
        self.tradingview_erase_all_button = QPushButton("Erase All")
        self.tradingview_erase_all_button.setObjectName("eraseAllButton")
        self.tradingview_erase_all_button.clicked.connect(self.clear_current_chart_drawings)
        self.tradingview_set_target_button = QPushButton("Set Breakout Price (T)")
        self.tradingview_set_target_button.clicked.connect(self.enable_chart_target_mode)
        self.tradingview_clear_target_button = QPushButton("Clear Breakout")
        self.tradingview_clear_target_button.setObjectName("clearTargetButton")
        self.tradingview_clear_target_button.clicked.connect(self.clear_current_chart_target)
        self.tradingview_full_view_button = QPushButton("Full View (F)")
        self.tradingview_full_view_button.clicked.connect(self.reset_chart_full_view)
        self.tradingview_add_watchlist_button = QPushButton("Add to Watchlist (W)")
        self.tradingview_add_watchlist_button.clicked.connect(self.add_current_tradingview_symbol_to_watchlist)
        self.tradingview_queue_btn = QPushButton("Queue for Buy (Q)")
        self.tradingview_queue_btn.setMinimumWidth(150)
        self.tradingview_queue_btn.clicked.connect(self._tradingview_queue_toggle)
        self.tradingview_activate_btn = QPushButton("Activate (A)")
        self.tradingview_activate_btn.setMinimumWidth(110)
        self.tradingview_activate_btn.clicked.connect(self._tradingview_activate_toggle)
        tools_layout.addWidget(self.tradingview_set_target_button)
        tools_layout.addWidget(self.tradingview_line_tool_button)
        tools_layout.addWidget(self.tradingview_clear_target_button)
        tools_layout.addWidget(self.tradingview_erase_all_button)
        tools_layout.addWidget(self.tradingview_add_watchlist_button)
        tools_layout.addWidget(self.tradingview_queue_btn)
        tools_layout.addWidget(self.tradingview_activate_btn)
        tools_layout.addWidget(self.tradingview_full_view_button)
        tools_layout.addStretch(1)
        layout.addLayout(tools_layout)

        # Update queue/watchlist/activate buttons whenever symbol changes
        self.tradingview_symbol_combo.currentTextChanged.connect(self._update_tradingview_queue_btn)
        self.tradingview_symbol_combo.currentTextChanged.connect(self._update_tradingview_watchlist_btn)
        self.tradingview_symbol_combo.currentTextChanged.connect(self._update_tradingview_activate_btn)

        self.tradingview_widget.setLayout(layout)
        self.tradingview_draw_shortcut = QShortcut(QKeySequence("D"), self.tradingview_widget)
        self.tradingview_draw_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_draw_shortcut.activated.connect(self.toggle_tradingview_line_tool_mode)
        self.tradingview_target_shortcut = QShortcut(QKeySequence("T"), self.tradingview_widget)
        self.tradingview_target_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_target_shortcut.activated.connect(self.enable_chart_target_mode)
        self.tradingview_queue_shortcut = QShortcut(QKeySequence("Q"), self.tradingview_widget)
        self.tradingview_queue_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_queue_shortcut.activated.connect(self._tradingview_queue_toggle)
        self.tradingview_activate_shortcut = QShortcut(QKeySequence("A"), self.tradingview_widget)
        self.tradingview_activate_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_activate_shortcut.activated.connect(self._tradingview_activate_toggle)
        self.tradingview_up_shortcut = QShortcut(QKeySequence(Qt.Key_Up), self.tradingview_widget)
        self.tradingview_up_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_up_shortcut.activated.connect(lambda: self.step_tradingview_watchlist_symbol(-1))
        self.tradingview_down_shortcut = QShortcut(QKeySequence(Qt.Key_Down), self.tradingview_widget)
        self.tradingview_down_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_down_shortcut.activated.connect(lambda: self.step_tradingview_watchlist_symbol(1))
        self.tradingview_left_shortcut = QShortcut(QKeySequence(Qt.Key_Left), self.tradingview_widget)
        self.tradingview_left_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_left_shortcut.activated.connect(lambda: self.pan_tradingview_chart_view(-self._chart_pan_step_bars()))
        self.tradingview_right_shortcut = QShortcut(QKeySequence(Qt.Key_Right), self.tradingview_widget)
        self.tradingview_right_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_right_shortcut.activated.connect(lambda: self.pan_tradingview_chart_view(self._chart_pan_step_bars()))
        self.tradingview_full_view_shortcut = QShortcut(QKeySequence("F"), self.tradingview_widget)
        self.tradingview_full_view_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_full_view_shortcut.activated.connect(self.reset_chart_full_view)
        self.tradingview_watchlist_shortcut = QShortcut(QKeySequence("W"), self.tradingview_widget)
        self.tradingview_watchlist_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_watchlist_shortcut.activated.connect(self.add_current_tradingview_symbol_to_watchlist)
        self.tradingview_load_shortcut = QShortcut(QKeySequence(Qt.Key_F4), self.tradingview_widget)
        self.tradingview_load_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_load_shortcut.activated.connect(lambda: self.load_tradingview_chart(force=True, fetch_live=True))
        self.tradingview_refresh_shortcut = QShortcut(QKeySequence("R"), self.tradingview_widget)
        self.tradingview_refresh_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_refresh_shortcut.activated.connect(lambda: self.load_tradingview_chart(force=True, fetch_live=True))
        self._update_tradingview_queue_btn()
        self._update_tradingview_watchlist_btn()
        self._update_tradingview_activate_btn()
        self.load_tradingview_chart(show_empty_message=False)
    def _refresh_active_chart_for_symbol(self, symbol: str) -> None:
        """Force-refresh the current chart view if it matches symbol."""
        symbol = symbol.strip().upper()
        if hasattr(self, "tabs") and hasattr(self, "tradingview_widget") and self.tabs.currentWidget() is self.tradingview_widget:
            active = self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
            if active == symbol:
                QTimer.singleShot(50, lambda: self.load_tradingview_chart(force=True))
        else:
            chart_sym = self._get_chart_symbol() if hasattr(self, "chart_symbol_input") else ""
            if chart_sym and chart_sym.strip().upper() == symbol:
                QTimer.singleShot(50, lambda: self.plot_selected_symbol(show_warnings=False))
    def _active_chart_timeframe(self) -> str:
        """Return the timeframe currently selected on the active chart tab."""
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.__dict__.get("tradingview_widget"):
            return self.tradingview_timeframe_combo.currentText().strip().upper() if hasattr(self, "tradingview_timeframe_combo") else "1D"
        return self.chart_timeframe_combo.currentText().strip().upper() if hasattr(self, "chart_timeframe_combo") else "1D"
    def _set_chart_symbol(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if isinstance(self.chart_symbol_input, QComboBox):
            self.chart_symbol_input.setEditText(symbol)
        else:
            self.chart_symbol_input.setText(symbol)
    def _get_chart_symbol(self) -> str:
        if isinstance(self.chart_symbol_input, QComboBox):
            return self.chart_symbol_input.currentText().strip().upper()
        return self.chart_symbol_input.text().strip().upper()
    def populate_chart_symbol_combo(self) -> None:
        if not hasattr(self, "chart_symbol_input") or not isinstance(self.chart_symbol_input, QComboBox):
            return

        current_text = self.chart_symbol_input.currentText().strip().upper()
        symbols = self._get_chart_symbol_universe()

        self.chart_symbol_input.blockSignals(True)
        self.chart_symbol_input.clear()
        self.chart_symbol_input.addItems(sorted(symbols))
        self.chart_symbol_input.setEditText(current_text)
        self.chart_symbol_input.blockSignals(False)
    def filter_chart_symbol_combo(self, text: str) -> None:
        if not isinstance(self.chart_symbol_input, QComboBox):
            return

        prefix = text.strip().upper()
        filtered = self._filter_symbols_by_prefix(self._get_chart_symbol_universe(), prefix)

        self.chart_symbol_input.blockSignals(True)
        self.chart_symbol_input.clear()
        self.chart_symbol_input.addItems(filtered)
        self.chart_symbol_input.setEditText(prefix)
        self.chart_symbol_input.blockSignals(False)
        self.chart_symbol_input.showPopup()
    def _get_chart_symbol_universe(self) -> set:
        symbols = set(self.universe_tickers)
        symbols.update(item.symbol for item in self.watchlist.items)
        symbols.update(stock["symbol"] for stock in self.scanner_results if stock.get("symbol"))
        symbols.update(plan.symbol for plan in self.trade_manager.get_active_plans())
        return symbols
    def populate_intraday_watchlist_symbols(self) -> None:
        if not hasattr(self, "intraday_symbol_combo"):
            return
        current_text = self.intraday_symbol_combo.currentText().strip().upper()
        self.intraday_symbol_combo.blockSignals(True)
        self.intraday_symbol_combo.clear()
        self.intraday_symbol_combo.addItems([item.symbol for item in self.watchlist.items])
        if current_text:
            index = self.intraday_symbol_combo.findText(current_text)
            if index >= 0:
                self.intraday_symbol_combo.setCurrentIndex(index)
        self.intraday_symbol_combo.blockSignals(False)
    def populate_tradingview_watchlist_symbols(self) -> None:
        if not hasattr(self, "tradingview_symbol_combo"):
            return
        current_text = self.tradingview_symbol_combo.currentText().strip().upper()
        symbols = sorted(self._get_chart_symbol_universe())

        self.tradingview_symbol_combo.blockSignals(True)
        self.tradingview_symbol_combo.clear()
        self.tradingview_symbol_combo.addItems(symbols)
        if current_text:
            index = self.tradingview_symbol_combo.findText(current_text)
            if index >= 0:
                self.tradingview_symbol_combo.setCurrentIndex(index)
            elif self.tradingview_symbol_combo.isEditable():
                self.tradingview_symbol_combo.setEditText(current_text)
        self.tradingview_symbol_combo.blockSignals(False)
    def filter_tradingview_symbol_combo(self, text: str) -> None:
        if not hasattr(self, "tradingview_symbol_combo"):
            return
        prefix = text.strip().upper()
        filtered = self._filter_symbols_by_prefix(self._get_chart_symbol_universe(), prefix)

        self.tradingview_symbol_combo.blockSignals(True)
        self.tradingview_symbol_combo.clear()
        self.tradingview_symbol_combo.addItems(filtered)
        self.tradingview_symbol_combo.setEditText(prefix)
        self.tradingview_symbol_combo.blockSignals(False)
        self.tradingview_symbol_combo.showPopup()
    def _set_intraday_symbol(self, symbol: str) -> None:
        if not hasattr(self, "intraday_symbol_combo"):
            return
        symbol = symbol.strip().upper()
        index = self.intraday_symbol_combo.findText(symbol)
        if index >= 0:
            self.intraday_symbol_combo.setCurrentIndex(index)
    def refresh_intraday_chart_if_symbol(self, symbol: str, allow_fetch: bool = False) -> None:
        intraday_symbol_combo = self.__dict__.get("intraday_symbol_combo")
        if intraday_symbol_combo is None:
            return
        if intraday_symbol_combo.currentText().strip().upper() == symbol.strip().upper():
            self.plot_intraday_watchlist_symbol(allow_fetch=allow_fetch)
    def refresh_chart_views_for_symbol(self, symbol: str, allow_fetch: bool = False) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        chart_symbol = self._get_chart_symbol() if self.__dict__.get("chart_symbol_input") is not None else ""
        if chart_symbol and chart_symbol.strip().upper() == symbol:
            self.plot_selected_symbol(show_warnings=False)
        self.refresh_intraday_chart_if_symbol(symbol, allow_fetch=allow_fetch)
    def refresh_other_chart_views_for_symbol(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        active_widget = self.__dict__.get("tabs").currentWidget() if self.__dict__.get("tabs") is not None else None
        chart_symbol = self._get_chart_symbol() if self.__dict__.get("chart_symbol_input") is not None else ""
        if active_widget is not self.__dict__.get("charts_widget") and chart_symbol and chart_symbol.strip().upper() == symbol:
            self.plot_selected_symbol(show_warnings=False)
        intraday_symbol_combo = self.__dict__.get("intraday_symbol_combo")
        if (
            active_widget is not self.__dict__.get("intraday_charts_widget")
            and intraday_symbol_combo is not None
            and intraday_symbol_combo.currentText().strip().upper() == symbol
        ):
            self.plot_intraday_watchlist_symbol(allow_fetch=False)
        tradingview_symbol_combo = self.__dict__.get("tradingview_symbol_combo")
        if (
            active_widget is not self.__dict__.get("tradingview_widget")
            and tradingview_symbol_combo is not None
            and tradingview_symbol_combo.currentText().strip().upper() == symbol
        ):
            self.load_tradingview_chart(force=True)
    def step_tradingview_watchlist_symbol(self, direction: int) -> None:
        if not hasattr(self, "tradingview_symbol_combo"):
            return
        symbols = self._sidebar_symbols()
        if not symbols:
            symbols = [
                self.tradingview_symbol_combo.itemText(index).strip().upper()
                for index in range(self.tradingview_symbol_combo.count())
                if self.tradingview_symbol_combo.itemText(index).strip()
            ]
        if not symbols:
            self.tradingview_status_label.setText("No symbols available.")
            return

        current_symbol = self.tradingview_symbol_combo.currentText().strip().upper()
        try:
            current_index = symbols.index(current_symbol)
        except ValueError:
            current_index = 0 if int(direction) > 0 else len(symbols) - 1
        next_index = (current_index + int(direction)) % len(symbols)
        next_symbol = symbols[next_index]
        self._set_tradingview_symbol(next_symbol)
        if hasattr(self, "sidebar_stock_list"):
            for row in range(self.sidebar_stock_list.count()):
                item = self.sidebar_stock_list.item(row)
                data = item.data(Qt.UserRole) or {}
                if str(data.get("symbol", "")).strip().upper() == next_symbol:
                    self.sidebar_stock_list.setCurrentRow(row)
                    break
        self.load_tradingview_chart(force=True)
    def _set_tradingview_symbol(self, symbol: str) -> None:
        if not hasattr(self, "tradingview_symbol_combo"):
            return
        symbol = symbol.strip().upper()
        index = self.tradingview_symbol_combo.findText(symbol)
        if index >= 0:
            self.tradingview_symbol_combo.setCurrentIndex(index)
        elif self.tradingview_symbol_combo.isEditable():
            self.tradingview_symbol_combo.setEditText(symbol)
    def add_current_tradingview_symbol_to_watchlist(self) -> None:
        symbol = self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
        if not symbol:
            QMessageBox.information(self, "No symbol", "Load a symbol before adding it to the watchlist.")
            return
        existing = self.watchlist.get(symbol)
        source_combo = self.__dict__.get("sidebar_source_combo")
        source = source_combo.currentData() if source_combo is not None else {"type": "watchlist"}
        source_type = source.get("type") if isinstance(source, dict) else ""
        if existing is not None and source_type == "watchlist":
            self.watchlist.remove(symbol)
            self.populate_watchlist_table()
            self.update_dashboard_summary()
            self._save_state()
            self.append_log(f"Removed {symbol} from watchlist from TradingView.")
            self._update_tradingview_watchlist_btn()
            return
        if existing is not None:
            self._update_tradingview_watchlist_btn()
            return
        name = symbol
        selected = self._get_sidebar_selected_data()
        if selected and str(selected.get("symbol", "")).strip().upper() == symbol:
            name = selected.get("name", symbol) or symbol
        self.watchlist.add(symbol=symbol, name=name)
        self.populate_watchlist_table()
        self.update_dashboard_summary()
        self._save_state()
        self.prefetch_intraday_cache_for_symbol(symbol)
        self.append_log(f"Added/updated {symbol} in watchlist from TradingView.")
        self._update_tradingview_watchlist_btn()
    def _update_tradingview_watchlist_btn(self, _text: str = "") -> None:
        btn = self.__dict__.get("tradingview_add_watchlist_button")
        if btn is None:
            return
        combo = self.__dict__.get("tradingview_symbol_combo")
        symbol = combo.currentText().strip().upper() if combo is not None else ""
        watchlist = self.__dict__.get("watchlist")
        in_watchlist = symbol and watchlist is not None and watchlist.get(symbol) is not None
        if in_watchlist:
            btn.setText("Remove from Watchlist (W)")
            btn.setStyleSheet("background-color: #c0392b; color: white; font-weight: 600;")
        else:
            btn.setText("Add to Watchlist (W)")
            btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: 600;")

    def _tradingview_queue_toggle(self) -> None:
        symbol = self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
        if not symbol:
            return
        self._chart_queue_toggle(symbol)
        self._update_tradingview_queue_btn()

    def _update_tradingview_queue_btn(self, _text: str = "") -> None:
        btn = getattr(self, "tradingview_queue_btn", None)
        if btn is None:
            return
        symbol = self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
        self._apply_chart_queue_btn_state(symbol, btn)

    def _chart_queue_toggle(self, symbol: str) -> None:
        if not symbol:
            return
        env = self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
        buylist_manager = getattr(self, "buylist_manager", None)
        item = buylist_manager.get(symbol, env) if buylist_manager is not None else None
        in_queue = item is not None and self._is_execution_queue_buylist_item(item)
        if in_queue:
            if item.monitoring_status in ("BOUGHT", "BUY_SUBMITTED", "BUY_PARTIAL"):
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.warning(self, "Active position", f"{symbol} has an active position and cannot be removed here.")
                return
            buylist_manager.remove(symbol, env)
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(f"[Chart] {symbol} removed from execution queue.")
        else:
            watch_item = self.watchlist.get(symbol) if hasattr(self, "watchlist") else None
            if watch_item is None or not watch_item.breakout_price:
                QMessageBox.information(
                    self,
                    "Breakout price required",
                    f"Set a breakout price for {symbol} before queuing it for buy.",
                )
                return
            self.refresh_execution_queue(env, symbols=[symbol], create_missing=True)
            self.populate_buylist_dashboard()
            self.append_log(f"[Chart] {symbol} queued for buy.")

    def _apply_chart_queue_btn_state(self, symbol: str, btn) -> None:
        env = self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
        buylist_manager = getattr(self, "buylist_manager", None)
        item = buylist_manager.get(symbol, env) if buylist_manager is not None else None
        in_queue = item is not None and self._is_execution_queue_buylist_item(item)
        if in_queue:
            btn.setText("Remove from Queue")
            btn.setStyleSheet("background-color: #c0392b; color: white; font-weight: 600;")
        else:
            btn.setText("Queue for Buy (Q)")
            btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: 600;")

    def _is_symbol_monitor_active(self, symbol: str, env: str) -> bool:
        buylist_manager = getattr(self, "buylist_manager", None)
        if buylist_manager is None or not symbol:
            return False
        item = buylist_manager.get(symbol, env)
        if item is None:
            return False
        if self._is_execution_queue_buylist_item(item):
            return bool(getattr(item, "orb_monitor_enabled", False))
        return str(getattr(item, "monitoring_status", "")).upper() in ("ACTIVE", "BOUGHT")

    def _chart_activate_toggle(self, symbol: str) -> None:
        if not symbol:
            return
        env = self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
        buylist_manager = getattr(self, "buylist_manager", None)
        if buylist_manager is None:
            return
        item = buylist_manager.get(symbol, env)
        if item is None:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.information(self, "Not in queue", f"{symbol} is not queued. Use 'Queue for Buy' first.")
            return

        is_active = self._is_symbol_monitor_active(symbol, env)
        if is_active:
            if self._is_execution_queue_buylist_item(item):
                item.orb_monitor_enabled = False
            elif str(getattr(item, "monitoring_status", "")).upper() not in ("BOUGHT",):
                item.monitoring_status = "WATCHING"
                self._clear_buylist_auto_order_block(item)
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(f"[Chart] {symbol} monitoring deactivated.")
        else:
            if str(getattr(item, "monitoring_status", "")).upper() == "BOUGHT":
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.information(self, "Already bought", f"{symbol} is already in a BOUGHT position.")
                return
            if self._is_execution_queue_buylist_item(item):
                item.orb_monitor_enabled = True
                active_attr = f"_buylist_{env.lower()}_monitor_active"
                if not getattr(self, active_attr, False):
                    self._toggle_buylist_monitor(env)
            else:
                bought_count = sum(
                    1 for it in buylist_manager.items
                    if str(getattr(it, "monitoring_status", "")).upper() == "BOUGHT" and it.environment == env
                )
                if bought_count >= 30:
                    from PyQt5.QtWidgets import QMessageBox
                    QMessageBox.warning(self, "Max positions", "Already holding 30 positions.")
                    return
                item.monitoring_status = "ACTIVE"
                self._clear_buylist_auto_order_block(item)
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(f"[Chart] {symbol} monitoring activated.")
            # Pre-load intraday data so the Intraday tab is ready
            self._set_intraday_symbol(symbol)
            self.prefetch_intraday_cache_for_symbol(symbol)

    def _apply_chart_activate_btn_state(self, symbol: str, btn) -> None:
        env = self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
        buylist_manager = getattr(self, "buylist_manager", None)
        item = buylist_manager.get(symbol, env) if buylist_manager and symbol else None
        in_queue = item is not None
        is_active = self._is_symbol_monitor_active(symbol, env)
        btn.setEnabled(in_queue)
        if is_active:
            btn.setText("Deactivate (A)")
            btn.setStyleSheet("background-color: #c0392b; color: white; font-weight: 600;")
        elif in_queue:
            btn.setText("Activate (A)")
            btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: 600;")
        else:
            btn.setText("Activate (A)")
            btn.setStyleSheet("")

    def _update_tradingview_activate_btn(self, _text: str = "") -> None:
        btn = getattr(self, "tradingview_activate_btn", None)
        if btn is None:
            return
        symbol = self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
        self._apply_chart_activate_btn_state(symbol, btn)

    def _tradingview_activate_toggle(self) -> None:
        symbol = self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
        if not symbol:
            return
        self._chart_activate_toggle(symbol)
        self._update_tradingview_activate_btn()

    def _update_intraday_activate_btn(self, _text: str = "") -> None:
        btn = getattr(self, "intraday_activate_btn", None)
        if btn is None:
            return
        symbol = self.intraday_symbol_combo.currentText().strip().upper() if hasattr(self, "intraday_symbol_combo") else ""
        self._apply_chart_activate_btn_state(symbol, btn)

    def _intraday_activate_toggle(self) -> None:
        symbol = self.intraday_symbol_combo.currentText().strip().upper() if hasattr(self, "intraday_symbol_combo") else ""
        if not symbol:
            return
        self._chart_activate_toggle(symbol)
        self._update_intraday_activate_btn()

    def load_tradingview_chart(self, show_empty_message: bool = True, force: bool = False, fetch_live: bool = False, skip_split_view: bool = False) -> None:
        if not hasattr(self, "tradingview_symbol_combo"):
            return
        symbol = self.tradingview_symbol_combo.currentText().strip().upper()
        if not symbol:
            message = "Enter or select a symbol first."
            self.current_tradingview_symbol = ""
            if hasattr(self, "tradingview_status_label"):
                self.tradingview_status_label.setText(message)
            if show_empty_message:
                self._set_html_or_text(
                    self.tradingview_chart_view,
                    self._generate_message_html("No watchlist symbols", message),
                    message,
                )
                if hasattr(self, "tradingview_split_chart_view"):
                    self._set_html_or_text(
                        self.tradingview_split_chart_view,
                        self._generate_message_html("No watchlist symbols", message),
                        message,
                    )
            return

        tradingview_symbol = self._to_tradingview_symbol(symbol)
        base_options = {
            "show_volume": self.tradingview_show_volume_checkbox.isChecked()
            if hasattr(self, "tradingview_show_volume_checkbox")
            else True,
            "show_ema": self.tradingview_show_ema_checkbox.isChecked()
            if hasattr(self, "tradingview_show_ema_checkbox")
            else True,
            "show_rs": self.tradingview_show_rs_checkbox.isChecked()
            if hasattr(self, "tradingview_show_rs_checkbox")
            else True,
            "show_adr": self.tradingview_show_adr_checkbox.isChecked()
            if hasattr(self, "tradingview_show_adr_checkbox")
            else True,
            "show_growth_1m": self.tradingview_show_growth_1m_checkbox.isChecked()
            if hasattr(self, "tradingview_show_growth_1m_checkbox")
            else True,
            "show_growth_3m": self.tradingview_show_growth_3m_checkbox.isChecked()
            if hasattr(self, "tradingview_show_growth_3m_checkbox")
            else True,
            "show_growth_6m": self.tradingview_show_growth_6m_checkbox.isChecked()
            if hasattr(self, "tradingview_show_growth_6m_checkbox")
            else False,
            "window_days": self._get_tradingview_window_days(),
        }
        now = dt.datetime.now(dt.timezone.utc)

        split_enabled = (
            hasattr(self, "tradingview_split_screen_checkbox")
            and self.tradingview_split_screen_checkbox.isChecked()
        )
        if split_enabled:
            self.tradingview_split_chart_view.setVisible(True)
            primary_status = self._render_tradingview_chart_view(
                self.tradingview_chart_view,
                symbol=symbol,
                tradingview_symbol=tradingview_symbol,
                timeframe="1D",
                base_options=base_options,
                now=now,
                force=force,
                fetch_live=fetch_live,
                view_key="left",
            )
            if not skip_split_view:
                split_status = self._render_tradingview_chart_view(
                    self.tradingview_split_chart_view,
                    symbol=symbol,
                    tradingview_symbol=tradingview_symbol,
                    timeframe="1H",
                    base_options=base_options,
                    now=now,
                    force=force,
                    fetch_live=fetch_live,
                    view_key="right",
                )
            else:
                split_status = "1H skipped (drawing sync)"
            self.current_tradingview_symbol = (
                f"{tradingview_symbol}|split|volume={int(base_options['show_volume'])}|"
                f"ema={int(base_options['show_ema'])}|rs={int(base_options.get('show_rs', True))}"
            )
            self.tradingview_status_label.setText(f"{primary_status} | {split_status}")
            return

        if hasattr(self, "tradingview_split_chart_view"):
            self.tradingview_split_chart_view.setVisible(False)
        timeframe = (
            self.tradingview_timeframe_combo.currentText().strip().upper()
            if hasattr(self, "tradingview_timeframe_combo")
            else "1D"
        )
        status = self._render_tradingview_chart_view(
            self.tradingview_chart_view,
            symbol=symbol,
            tradingview_symbol=tradingview_symbol,
            timeframe=timeframe,
            base_options=base_options,
            now=now,
            force=force,
            fetch_live=fetch_live,
            view_key="single",
        )
        self.current_tradingview_symbol = (
            f"{tradingview_symbol}|{timeframe}|volume={int(base_options['show_volume'])}|"
            f"ema={int(base_options['show_ema'])}|rs={int(base_options.get('show_rs', True))}"
        )
        self.tradingview_status_label.setText(status)
    def _render_tradingview_chart_view(
        self,
        target_view,
        symbol: str,
        tradingview_symbol: str,
        timeframe: str,
        base_options: dict,
        now: dt.datetime,
        force: bool,
        view_key: str,
        fetch_live: bool = False,
    ) -> str:
        options = {
            "show_volume": bool(base_options.get("show_volume", True)),
            "show_ema": bool(base_options.get("show_ema", True)),
            "show_rs": bool(base_options.get("show_rs", True)),
            "show_adr": bool(base_options.get("show_adr", True)),
            "show_growth_1m": bool(base_options.get("show_growth_1m", True)),
            "show_growth_3m": bool(base_options.get("show_growth_3m", True)),
            "show_growth_6m": bool(base_options.get("show_growth_6m", False)),
            "window_days": int(base_options.get("window_days", 7) or 7),
            "timeframe": timeframe,
        }
        if timeframe.strip().upper() != "1D":
            options.update({
                "show_adr": False,
                "show_growth_1m": False,
                "show_growth_3m": False,
                "show_growth_6m": False,
            })
        refresh_key = (
            f"{view_key}|{tradingview_symbol}|{timeframe}|"
            f"volume={int(options['show_volume'])}|ema={int(options['show_ema'])}|"
            f"rs={int(options.get('show_rs', True))}|adr={int(options.get('show_adr', False))}|"
            f"g1={int(options.get('show_growth_1m', False))}|g3={int(options.get('show_growth_3m', False))}|"
            f"g6={int(options.get('show_growth_6m', False))}|window={options.get('window_days', 7)}"
        )
        last_refresh = self.tradingview_refresh_timestamps.get(refresh_key)
        if not force and not self._tradingview_refresh_due(last_refresh, now=now):
            next_refresh = last_refresh + dt.timedelta(seconds=TRADINGVIEW_REFRESH_INTERVAL_SECONDS)
            seconds_left = max(1, int((next_refresh - now).total_seconds()))
            return f"{timeframe} skipped; next auto refresh in {seconds_left // 60}m {seconds_left % 60}s"

        try:
            history = self._load_chart_history_for_timeframe(
                symbol,
                timeframe,
                use_live_fallback=fetch_live,
                window_days=int(options.get("window_days", 7) or 7),
                force_refresh=fetch_live,
            )
        except TypeError:
            history = self._load_chart_history_for_timeframe(
                symbol,
                timeframe,
                use_live_fallback=fetch_live,
                window_days=int(options.get("window_days", 7) or 7),
            )
        chart_history = self._normalize_chart_history(
            history,
            symbol,
            max_rows=self._tradingview_max_history_bars(timeframe, int(options.get("window_days", 7) or 7)),
        )
        if chart_history.empty:
            message = f"No {timeframe} chart data found for {symbol}."
            self._set_html_or_text(
                target_view,
                self._generate_message_html(symbol, message),
                message,
            )
            self.tradingview_refresh_timestamps[refresh_key] = now
            return message

        latest_text = self._format_chart_latest_text(chart_history, timeframe)
        options["data_latest_text"] = latest_text

        drawings = self._build_combined_drawings(symbol, timeframe)
        watchlist = self.__dict__.get("watchlist")
        watchlist_item = watchlist.get(symbol) if watchlist is not None else None
        target_price = watchlist_item.breakout_price if watchlist_item is not None else None
        buylist_manager = self.__dict__.get("buylist_manager")
        buylist_item = buylist_manager.get(symbol) if buylist_manager is not None else None
        buy_price: Optional[float] = None
        buy_stop_loss: Optional[float] = None
        if buylist_item is not None:
            raw_buy = (
                buylist_item.avg_cost
                if buylist_item.monitoring_status == "BOUGHT" and buylist_item.avg_cost > 0
                else buylist_item.entry_price
            )
            buy_price = float(raw_buy) if raw_buy and float(raw_buy) > 0 else None
            raw_stop = buylist_item.stop_loss
            buy_stop_loss = float(raw_stop) if raw_stop and float(raw_stop) > 0 else None
        indicators = self._load_tradingview_indicator_history(symbol, timeframe, chart_history) if options.get("show_rs", True) else pd.DataFrame()
        html_content = self._generate_tradingview_lightweight_chart_html(
            tradingview_symbol,
            chart_history,
            options=options,
            drawings=drawings,
            storage_symbol=symbol,
            indicators=indicators,
            target_price=target_price,
            buy_price=buy_price,
            stop_loss=buy_stop_loss,
        )
        if QWebEngineView is not None and isinstance(target_view, QWebEngineView):
            target_view.setHtml(html_content, QUrl("https://www.tradingview.com/"))
        else:
            target_view.setPlainText(
                f"TradingView Lightweight Chart for {tradingview_symbol} requires PyQtWebEngine."
            )
        self.tradingview_refresh_timestamps[refresh_key] = now
        return f"Loaded {timeframe} chart for {tradingview_symbol}"
    @staticmethod
    def _format_chart_latest_text(history: pd.DataFrame, timeframe: str) -> str:
        if history.empty:
            return "latest: unavailable"
        latest = pd.Timestamp(history.index[-1])
        timeframe = timeframe.strip().upper()
        if timeframe in {"1H", "5M"}:
            kst = (latest.tz_localize("UTC") if latest.tzinfo is None else latest).tz_convert(KST_ZONE)
            return f"latest: {kst.strftime('%Y-%m-%d %H:%M')} KST"
        return f"latest: {latest.strftime('%Y-%m-%d')}"
    def _load_tradingview_indicator_history(self, symbol: str, timeframe: str, chart_history: pd.DataFrame) -> pd.DataFrame:
        if chart_history.empty:
            return pd.DataFrame()
        symbol = symbol.strip().upper()
        timeframe = timeframe.strip().upper()
        if timeframe == "1D" and self.db_enabled and self.db_engine is not None:
            indicators = load_chart_indicators_from_db(symbol, self.db_engine)
            if indicators.empty and refresh_chart_indicators_for_symbol(symbol, self.db_engine, reference_symbol=REFERENCE_SYMBOL):
                indicators = load_chart_indicators_from_db(symbol, self.db_engine)
            if not indicators.empty:
                return self._align_chart_indicators(chart_history, indicators)

        reference_history = self._load_chart_history_for_timeframe(REFERENCE_SYMBOL, timeframe, use_live_fallback=False)
        reference_history = self._normalize_chart_history(reference_history, REFERENCE_SYMBOL, max_rows=260)
        if reference_history.empty or "Close" not in reference_history.columns:
            return pd.DataFrame()
        indicators = calculate_chart_indicators(symbol, chart_history, reference_history)
        return self._align_chart_indicators(chart_history, indicators)
    def step_intraday_watchlist_symbol(self, direction: int) -> None:
        if not hasattr(self, "intraday_symbol_combo"):
            return
        count = self.intraday_symbol_combo.count()
        if count <= 0:
            self.intraday_status_label.setText("Add symbols to the watchlist first.")
            return

        current_index = self.intraday_symbol_combo.currentIndex()
        if current_index < 0:
            current_index = 0
        next_index = (current_index + direction) % count
        self.intraday_symbol_combo.setCurrentIndex(next_index)
        symbol = self.intraday_symbol_combo.currentText().strip().upper()

        sidebar_stock_list = self.__dict__.get("sidebar_stock_list")
        if sidebar_stock_list is not None:
            for row in range(sidebar_stock_list.count()):
                item = sidebar_stock_list.item(row)
                data = item.data(Qt.UserRole) or {}
                if data.get("symbol") == symbol:
                    sidebar_stock_list.setCurrentRow(row)
                    break

        self.plot_intraday_watchlist_symbol()
    def plot_intraday_watchlist_symbol(self, allow_fetch: bool = True) -> None:
        if not hasattr(self, "intraday_symbol_combo"):
            return
        symbol = self.intraday_symbol_combo.currentText().strip().upper()
        if not symbol:
            self.intraday_status_label.setText("Add symbols to the watchlist first.")
            return

        interval = self.intraday_interval_combo.currentText()
        window_days = self._get_intraday_window_days()
        symbol_history, cache_source = self._load_cached_intraday_5m_with_source(symbol, window_days=window_days)
        needs_backfill = self._intraday_cache_needs_backfill(
            symbol_history if symbol_history is not None else pd.DataFrame(),
            _utcnow_naive() - dt.timedelta(days=window_days),
        )
        if allow_fetch and needs_backfill and self._can_start_intraday_fetch(symbol, window_days):
            self.start_intraday_fetch(symbol, window_days=window_days)
        if symbol_history is None or symbol_history.empty:
            self._set_html_or_text(
                self.intraday_chart_view,
                self._generate_message_html("Loading intraday data", f"Fetching {window_days} days of 5-minute data for {symbol} in the background."),
                f"Fetching {window_days} days of 5-minute data for {symbol} in the background.",
            )
            self.intraday_status_label.setText(f"Fetching {symbol} intraday data in the background...")
            return

        chart_history = resample_intraday_bars(symbol_history, interval)
        if chart_history.empty:
            self.intraday_status_label.setText(f"No {interval} intraday bars available for {symbol}.")
            return

        latest_price = float(chart_history["Close"].iloc[-1])
        self.update_trade_prices_from_latest(symbol, latest_price)
        watchlist_item = self.watchlist.get(symbol)
        target_price = watchlist_item.breakout_price if watchlist_item is not None else None
        drawings = self.chart_drawings.get(symbol, [])
        self._set_html_or_text(
            self.intraday_chart_view,
            self._generate_local_chart_html(
                symbol,
                chart_history,
                compact=False,
                options=self._get_intraday_chart_options(),
                target_price=target_price,
                drawings=drawings,
            ),
            f"{symbol} intraday {interval} chart loaded. Latest price: {latest_price:.2f}",
        )
        latest_time = pd.Timestamp(chart_history.index[-1]).strftime("%Y-%m-%d %H:%M")
        source_note = f"{cache_source or 'legacy'} cache"
        if needs_backfill:
            source_note += "; background refresh running"
        if hasattr(self, "live_data_source_label") and cache_source:
            self.live_data_source_label.setText(format_intraday_source_label(cache_source))
        self.intraday_status_label.setText(
            f"{symbol} {interval} {window_days}D intraday chart loaded from {source_note}. "
            f"Latest {latest_price:.2f} at {latest_time}."
        )
    def _get_intraday_window_days(self) -> int:
        if not hasattr(self, "intraday_window_combo"):
            return 7
        text = self.intraday_window_combo.currentText().strip().upper().replace("D", "")
        try:
            return max(1, min(7, int(text)))
        except ValueError:
            return 7
    def _get_tradingview_window_days(self) -> int:
        if not hasattr(self, "tradingview_window_combo"):
            return 7
        text = self.tradingview_window_combo.currentText().strip().upper().replace("D", "")
        try:
            return max(1, min(7, int(text)))
        except ValueError:
            return 7
    @staticmethod
    def _tradingview_max_history_bars(timeframe: str, window_days: int = 7) -> Optional[int]:
        timeframe = timeframe.strip().upper()
        if timeframe == "5M":
            return max(100, min(2000, max(1, int(window_days or 7)) * 120))
        if timeframe == "1H":
            return 1000
        return 260
    def _get_intraday_chart_options(self) -> dict:
        return {
            "show_volume": self.intraday_show_volume_checkbox.isChecked(),
            "show_rs": False,
            "show_ema": self.intraday_show_ema_checkbox.isChecked(),
            "show_adr": False,
            "show_growth_1m": False,
            "show_growth_3m": False,
            "show_growth_6m": False,
            "max_history_bars": 2000,
            "visible_bars": 2000,
            "intraday_chart": True,
        }
    def _intraday_fetch_key(self, symbol: str, window_days: int) -> str:
        return f"{symbol.strip().upper()}:{max(1, min(7, int(window_days or 7)))}"
    def _can_start_intraday_fetch(self, symbol: str, window_days: int, cooldown_seconds: int = 300) -> bool:
        key = self._intraday_fetch_key(symbol, window_days)
        last_attempt = self.intraday_fetch_attempts.get(key)
        if last_attempt is None:
            return True
        return (_utcnow_naive() - last_attempt).total_seconds() >= cooldown_seconds
    def _load_cached_intraday_5m(self, symbol: str, window_days: int = 7) -> Optional[pd.DataFrame]:
        bars, _source = self._load_cached_intraday_5m_with_source(symbol, window_days=window_days)
        return bars
    def _load_cached_intraday_5m_with_source(self, symbol: str, window_days: int = 7) -> tuple[pd.DataFrame, str]:
        window_days = max(1, min(7, int(window_days or 7)))
        since = _utcnow_naive() - dt.timedelta(days=window_days)
        if self.db_enabled and self.db_engine is not None:
            try:
                bars, source = load_best_intraday_history(symbol, self.db_engine, interval="5m", since=since)
                self.latest_intraday_sources[(symbol.strip().upper(), "5m")] = source
                return bars, source
            except Exception:
                return pd.DataFrame(), "none"
        return pd.DataFrame(), "none"
    def start_intraday_fetch(self, symbol: str, window_days: int = 7) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        if self.intraday_fetch_worker is not None and self.intraday_fetch_worker.isRunning():
            self.append_log(f"Intraday fetch for {symbol} already running, skipping.")
            return
        engine = self.db_engine if self.db_enabled else None
        profile = self._selected_dashboard_kis_profile() or {}
        self.intraday_fetch_attempts[self._intraday_fetch_key(symbol, window_days)] = _utcnow_naive()
        self.append_log(f"Starting intraday fetch for {symbol} ({window_days}d window)...")
        self.intraday_fetch_worker = IntradayFetchWorker(
            symbol,
            engine,
            window_days=window_days,
            fetch_days=None,
            environment=profile.get("environment", "SIM"),
            account_no=profile.get("account_no", ""),
            exchange="NASD",
            allow_fallback=True,
        )
        self.intraday_fetch_worker.finished_fetch.connect(self._on_intraday_fetch_finished)
        self.intraday_fetch_worker.provider_warning.connect(
            lambda symbol, warning: self.append_log(f"Intraday provider warning for {symbol}: {warning}")
        )
        self.intraday_fetch_worker.error_occurred.connect(self._on_intraday_fetch_error)
        self.intraday_fetch_worker.finished.connect(
            lambda worker=self.intraday_fetch_worker: self._clear_worker_reference("intraday_fetch_worker", worker)
        )
        self.intraday_fetch_worker.start()
    def _on_intraday_fetch_finished(self, symbol: str, fetched, window_days: int, source: str) -> None:
        source_text = "yfinance fallback" if source == "yfinance" else source
        self.latest_intraday_sources[(symbol.strip().upper(), "5m")] = source
        latest_ts = ""
        try:
            if hasattr(fetched, "index") and not fetched.empty:
                latest_ts = f" | latest bar: {pd.Timestamp(fetched.index.max())}"
        except Exception:
            pass
        self.append_log(f"Updated intraday cache for {symbol} from {source_text}.{latest_ts}")
        if hasattr(self, "live_data_source_label"):
            self.live_data_source_label.setText(format_intraday_source_label(source))
        if self.intraday_symbol_combo.currentText().strip().upper() == symbol:
            self.plot_intraday_watchlist_symbol(allow_fetch=False)
        if hasattr(self, "symbol_input") and self.symbol_input.text().strip().upper() == symbol:
            self.refresh_orb_trade_plan_table()
        if hasattr(self, "refresh_execution_queue"):
            env = self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
            self.refresh_execution_queue(env, show_log=False)
        if (
            hasattr(self, "tradingview_timeframe_combo")
            and hasattr(self, "tradingview_widget")
            and hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            timeframe = self.tradingview_timeframe_combo.currentText().strip().upper()
            if timeframe in ("5M", "1H"):
                active = self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
                if active == symbol.strip().upper():
                    self.load_tradingview_chart(force=True)
    def _on_intraday_fetch_error(self, symbol: str, message: str) -> None:
        self.append_log(f"Intraday fetch failed for {symbol}: {message}")
        if hasattr(self, "intraday_status_label"):
            self.intraday_status_label.setText(f"Intraday fetch failed for {symbol}: {message}")
    def refresh_watchlist_intraday_cache(
        self,
        checked: bool = False,
        show_messages: bool = True,
        triggered_by_live: bool = False,
        source: str = "",
    ) -> None:
        symbols = [item.symbol for item in self.watchlist.items]
        if not symbols:
            if show_messages:
                QMessageBox.information(self, "No watchlist", "Add symbols to the watchlist first.")
            if triggered_by_live and hasattr(self, "live_data_status_label"):
                self.live_data_status_label.setText("Live data: no watchlist symbols")
            return
        if self.intraday_bulk_worker is not None and self.intraday_bulk_worker.isRunning():
            if show_messages:
                QMessageBox.information(self, "Intraday refresh running", "Watchlist intraday refresh is already running.")
            if triggered_by_live and hasattr(self, "live_data_status_label"):
                self.live_data_status_label.setText("Live data: refresh already running")
            return

        engine = self.db_engine if self.db_enabled else None
        self.intraday_bulk_purpose = "watchlist"
        if hasattr(self, "refresh_watchlist_orb_button"):
            self.refresh_watchlist_orb_button.setEnabled(False)
        self.refresh_intraday_button.setEnabled(False)
        log_source = source or ("live auto refresh" if triggered_by_live else "manual refresh")
        self.append_log(f"Starting 5-minute intraday {log_source} for {len(symbols)} watchlist symbols.")
        profile = self._selected_dashboard_kis_profile() or {}
        self.intraday_bulk_worker = IntradayBulkFetchWorker(
            symbols,
            engine,
            window_days=7,
            environment=profile.get("environment", "SIM"),
            account_no=profile.get("account_no", ""),
            exchange="NASD",
            allow_fallback=True,
        )
        self.intraday_bulk_worker.progress.connect(self._on_intraday_bulk_progress)
        self.intraday_bulk_worker.provider_warning.connect(
            lambda symbol, warning: self.append_log(f"Intraday provider warning for {symbol}: {warning}")
        )
        self.intraday_bulk_worker.finished_bulk.connect(self._on_intraday_bulk_finished)
        self.intraday_bulk_worker.finished.connect(
            lambda worker=self.intraday_bulk_worker: self._clear_worker_reference("intraday_bulk_worker", worker)
        )
        self.intraday_bulk_worker.start()
    def _on_intraday_bulk_progress(self, symbol: str, index: int, total: int) -> None:
        self.progress_label.setText(f"Intraday {index}/{total}: {symbol}")
    def _on_intraday_bulk_finished(self, updated: list, failed: list) -> None:
        if self.intraday_bulk_purpose == "scanner_orb":
            self.intraday_bulk_purpose = "watchlist"
            self.append_log(f"Scanner ORB phase intraday fetch complete: {len(updated)} updated, {len(failed)} failed.")
            if failed:
                self.append_log("Scanner ORB fetch failures: " + "; ".join(failed[:5]))
            self._score_scanner_results_by_orb()
            selected_source = self.pending_scanner_orb_source
            self.pending_scanner_orb_source = None
            self._finish_scanner_after_orb_phase(selected_source)
            return

        self.refresh_intraday_button.setEnabled(True)
        if hasattr(self, "refresh_watchlist_orb_button"):
            self.refresh_watchlist_orb_button.setEnabled(True)
        self.progress_label.setText("Intraday refresh complete.")
        self.append_log(f"Intraday refresh complete: {len(updated)} updated, {len(failed)} failed.")
        if failed:
            self.append_log("Intraday failures: " + "; ".join(failed[:5]))
        if getattr(self, "_refresh_orb_after_intraday_bulk", False):
            self._refresh_orb_after_intraday_bulk = False
            self.refresh_all_watchlist_orb_statuses()
        if hasattr(self, "refresh_execution_queue"):
            env = self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
            self.refresh_execution_queue(env, show_log=False)
            if hasattr(self, "_auto_replace_working_entry_queue_items"):
                self._auto_replace_working_entry_queue_items(env)
            if hasattr(self, "_auto_submit_execute_ready_queue_items"):
                self._auto_submit_execute_ready_queue_items(env)
        if hasattr(self, "live_data_checkbox") and self.live_data_checkbox.isChecked():
            status = f"Live data: updated {len(updated)}, failed {len(failed)}"
            if not self._is_us_regular_market_open():
                status += "; waiting for U.S. market hours"
            self.live_data_status_label.setText(status)
        if hasattr(self, "intraday_symbol_combo") and self.tabs.currentWidget() is self.intraday_charts_widget:
            self.plot_intraday_watchlist_symbol()
        if (
            hasattr(self, "tradingview_timeframe_combo")
            and hasattr(self, "tradingview_widget")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            timeframe = self.tradingview_timeframe_combo.currentText().strip().upper()
            if timeframe in ("5M", "1H"):
                self.load_tradingview_chart(force=True)
    @staticmethod
    def _intraday_cache_needs_backfill(cached: pd.DataFrame, since: dt.datetime) -> bool:
        return intraday_cache_needs_backfill(cached, since)
    def prefetch_intraday_cache_for_symbol(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        try:
            self.start_intraday_fetch(symbol, window_days=7)
            self.append_log(f"Queued 7-day intraday cache refresh for {symbol}.")
        except Exception as exc:
            self.append_log(f"Intraday prefetch failed for {symbol}: {exc}")
    def delete_intraday_cache_for_symbol(self, symbol: str) -> None:
        if not self.db_enabled or self.db_engine is None:
            return
        try:
            deleted = delete_intraday_history_for_symbol(self.db_engine, symbol)
            if deleted:
                self.append_log(f"Removed {deleted} intraday cache rows for {symbol}.")
        except Exception as exc:
            self.append_log(f"Intraday cache delete failed for {symbol}: {exc}")
    @staticmethod
    def _filter_symbols_by_prefix(symbols, prefix: str) -> List[str]:
        prefix = prefix.strip().upper()
        return [symbol for symbol in sorted({str(item).strip().upper() for item in symbols if str(item).strip()}) if symbol.startswith(prefix)]
    def _active_chart_view(self):
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.tradingview_widget:
            return getattr(self, "tradingview_chart_view", None)
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.intraday_charts_widget:
            return getattr(self, "intraday_chart_view", None)
        return getattr(self, "chart_view", None)
    def _active_chart_command_views(self) -> List[Any]:
        active_view = self._active_chart_view()
        if not (hasattr(self, "tabs") and self.tabs.currentWidget() is self.tradingview_widget):
            return [active_view] if active_view is not None else []
        views = [getattr(self, "tradingview_chart_view", None)]
        split_view = getattr(self, "tradingview_split_chart_view", None)
        if split_view is not None and split_view.isVisible():
            views.append(split_view)
        return [view for view in views if view is not None]
    def _active_chart_symbol(self) -> str:
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.tradingview_widget:
            return self.tradingview_symbol_combo.currentText().strip().upper() if hasattr(self, "tradingview_symbol_combo") else ""
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.intraday_charts_widget:
            return self.intraday_symbol_combo.currentText().strip().upper() if hasattr(self, "intraday_symbol_combo") else ""
        return self._get_chart_symbol() or (self.selected_scan_symbol or "")
    def _active_chart_buttons(self) -> dict:
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.tradingview_widget:
            return {
                "target": getattr(self, "tradingview_set_target_button", None),
                "draw": getattr(self, "tradingview_line_tool_button", None),
                "erase": getattr(self, "tradingview_erase_line_button", None),
            }
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.intraday_charts_widget:
            return {
                "target": getattr(self, "intraday_set_target_button", None),
                "draw": getattr(self, "intraday_draw_line_button", None),
                "erase": getattr(self, "intraday_erase_line_button", None),
            }
        return {
            "target": getattr(self, "chart_set_target_button", None),
            "draw": getattr(self, "chart_draw_line_button", None),
            "erase": getattr(self, "chart_erase_line_button", None),
        }
    @staticmethod
    def _set_button_state(button, text: str, active: bool = False) -> None:
        if button is None:
            return
        button.setText(text)
        button.setStyleSheet("font-weight: 600;" if active else "")
    def _reset_chart_mode_buttons(self) -> None:
        settings = self.__dict__.get("settings") or {}
        shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
        t_key = shortcuts.get("set_target", "T")
        d_key = shortcuts.get("draw_line", "D")
        e_key = shortcuts.get("erase_drawing", "E")
        for prefix in ["chart", "intraday"]:
            self._set_button_state(self.__dict__.get(f"{prefix}_set_target_button"), f"Set Breakout Price ({t_key})")
            self._set_button_state(self.__dict__.get(f"{prefix}_draw_line_button"), f"Draw Line ({d_key})")
            self._set_button_state(self.__dict__.get(f"{prefix}_erase_line_button"), f"Erase Drawing ({e_key})")
        self._set_button_state(self.__dict__.get("tradingview_set_target_button"), f"Set Breakout Price ({t_key})")
        self._set_button_state(self.__dict__.get("tradingview_line_tool_button"), f"Line Tool ({d_key})")
        self.tradingview_line_tool_active = False
    def enable_chart_target_mode(self) -> None:
        if not self._active_chart_symbol():
            QMessageBox.information(self, "No chart symbol", "Plot a symbol before setting a breakout price.")
            return
        active_views = self._active_chart_command_views()
        web_views = [view for view in active_views if QWebEngineView is not None and isinstance(view, QWebEngineView)]
        if web_views:
            web_views[0].setFocus()
            buttons = self._active_chart_buttons()
            settings = self.__dict__.get("settings") or {}
            shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            d_key = shortcuts.get("draw_line", "D")
            e_key = shortcuts.get("erase_drawing", "E")
            draw_label = f"Line Tool ({d_key})" if hasattr(self, "tabs") and self.tabs.currentWidget() is self.__dict__.get("tradingview_widget") else f"Draw Line ({d_key})"
            self._set_button_state(buttons["target"], "Click chart to set breakout", active=True)
            self._set_button_state(buttons["draw"], draw_label)
            self._set_button_state(buttons["erase"], f"Erase Drawing ({e_key})")
            self.tradingview_line_tool_active = False
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableTargetMode && window.enableTargetMode();",
                    lambda result: None,
                )
            self.append_log("Breakout price mode enabled. Click a price level on the chart.")
        else:
            self.append_log("Breakout price mode requires PyQtWebEngine chart view.")
    def enable_chart_drawing_mode(self) -> None:
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.__dict__.get("tradingview_widget"):
            self.enable_tradingview_line_tool_mode()
            return
        if not self._active_chart_symbol():
            QMessageBox.information(self, "No chart symbol", "Plot a symbol before drawing on the chart.")
            return
        active_views = self._active_chart_command_views()
        web_views = [view for view in active_views if QWebEngineView is not None and isinstance(view, QWebEngineView)]
        if web_views:
            web_views[0].setFocus()
            buttons = self._active_chart_buttons()
            settings = self.__dict__.get("settings") or {}
            shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            t_key = shortcuts.get("set_target", "T")
            e_key = shortcuts.get("erase_drawing", "E")
            self._set_button_state(buttons["draw"], "Click start point", active=True)
            self._set_button_state(buttons["target"], f"Set Breakout Price ({t_key})")
            self._set_button_state(buttons["erase"], f"Erase Drawing ({e_key})")
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableDrawingMode && window.enableDrawingMode();",
                    lambda result: None,
                )
            self.append_log("Drawing mode enabled. Click start and end points on the chart.")
        else:
            self.append_log("Drawing mode requires PyQtWebEngine chart view.")
    def toggle_tradingview_line_tool_mode(self) -> None:
        if getattr(self, "tradingview_line_tool_active", False):
            self.disable_tradingview_line_tool_mode()
        else:
            self.enable_tradingview_line_tool_mode()
    def disable_tradingview_line_tool_mode(self) -> None:
        if not hasattr(self, "tabs") or self.tabs.currentWidget() is not self.tradingview_widget:
            return
        symbol = self._active_chart_symbol()
        active_views = self._active_chart_command_views()
        web_views = [view for view in active_views if QWebEngineView is not None and isinstance(view, QWebEngineView)]
        for view in web_views:
            view.page().runJavaScript(
                "window.disableLineToolMode && window.disableLineToolMode();",
                lambda result: None,
            )
        self.tradingview_line_tool_active = False
        settings = self.__dict__.get("settings") or {}
        shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
        d_key = shortcuts.get("draw_line", "D")
        self._set_button_state(getattr(self, "tradingview_line_tool_button", None), f"Line Tool ({d_key})")
        if symbol:
            QTimer.singleShot(150, lambda symbol=symbol: self._sync_tradingview_drawings_after_tool_close(symbol))
        self.append_log("TradingView line tool disabled.")
    def _sync_tradingview_drawings_after_tool_close(self, symbol: str) -> None:
        if not hasattr(self, "tabs") or self.tabs.currentWidget() is not self.tradingview_widget:
            return
        active_symbol = self._active_chart_symbol()
        if active_symbol and active_symbol == symbol.strip().upper():
            self.load_tradingview_chart(force=True, skip_split_view=True)
    def enable_tradingview_line_tool_mode(self) -> None:
        if not hasattr(self, "tabs") or self.tabs.currentWidget() is not self.tradingview_widget:
            return
        if not self._active_chart_symbol():
            QMessageBox.information(self, "No chart symbol", "Load a symbol before drawing on the chart.")
            return
        active_views = self._active_chart_command_views()
        web_views = [view for view in active_views if QWebEngineView is not None and isinstance(view, QWebEngineView)]
        if web_views:
            web_views[0].setFocus()
            self.tradingview_line_tool_active = True
            settings = self.__dict__.get("settings") or {}
            shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            t_key = shortcuts.get("set_target", "T")
            self._set_button_state(getattr(self, "tradingview_line_tool_button", None), "Line Tool Active", active=True)
            self._set_button_state(getattr(self, "tradingview_set_target_button", None), f"Set Breakout Price ({t_key})")
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableLineToolMode && window.enableLineToolMode();",
                    lambda result: None,
                )
            self.append_log("TradingView line tool enabled. Click a line to edit it, or click empty space to draw.")
        else:
            self.append_log("Line tool requires PyQtWebEngine chart view.")
    def enable_tradingview_edit_mode(self) -> None:
        if not hasattr(self, "tabs") or self.tabs.currentWidget() is not self.tradingview_widget:
            return
        if not self._active_chart_symbol():
            QMessageBox.information(self, "No chart symbol", "Load a symbol before editing drawings.")
            return
        active_views = self._active_chart_command_views()
        web_views = [view for view in active_views if QWebEngineView is not None and isinstance(view, QWebEngineView)]
        if web_views:
            web_views[0].setFocus()
            self.tradingview_line_tool_active = True
            settings = self.__dict__.get("settings") or {}
            shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            t_key = shortcuts.get("set_target", "T")
            self._set_button_state(getattr(self, "tradingview_line_tool_button", None), "Line Tool Active", active=True)
            self._set_button_state(getattr(self, "tradingview_set_target_button", None), f"Set Breakout Price ({t_key})")
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableLineToolMode && window.enableLineToolMode();",
                    lambda result: None,
                )
            self.append_log("TradingView line tool enabled. Click a line to edit it, or click empty space to draw.")
        else:
            self.append_log("Edit mode requires PyQtWebEngine chart view.")
    def enable_chart_erase_mode(self) -> None:
        if not self._active_chart_symbol():
            QMessageBox.information(self, "No chart symbol", "Plot a symbol before erasing drawings.")
            return
        active_views = self._active_chart_command_views()
        web_views = [view for view in active_views if QWebEngineView is not None and isinstance(view, QWebEngineView)]
        if web_views:
            web_views[0].setFocus()
            buttons = self._active_chart_buttons()
            settings = self.__dict__.get("settings") or {}
            shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            t_key = shortcuts.get("set_target", "T")
            d_key = shortcuts.get("draw_line", "D")
            draw_label = f"Line Tool ({d_key})" if hasattr(self, "tabs") and self.tabs.currentWidget() is self.__dict__.get("tradingview_widget") else f"Draw Line ({d_key})"
            self._set_button_state(buttons["erase"], "Click drawing to erase", active=True)
            self._set_button_state(buttons["target"], f"Set Breakout Price ({t_key})")
            self._set_button_state(buttons["draw"], draw_label)
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableEraseMode && window.enableEraseMode();",
                    lambda result: None,
                )
            self.append_log("Erase mode enabled. Click a drawing line to remove it.")
        else:
            self.append_log("Erase mode requires PyQtWebEngine chart view.")
    def _chart_pan_step_bars(self) -> int:
        settings = self.__dict__.get("settings") or {}
        try:
            step = int(settings.get("chart_pan_step_bars", 1)) if isinstance(settings, dict) else 1
        except (TypeError, ValueError):
            step = 1
        return max(1, step)
    def pan_tradingview_chart_view(self, delta_bars: int) -> None:
        for view in self._active_chart_command_views():
            if QWebEngineView is not None and isinstance(view, QWebEngineView):
                view.page().runJavaScript(
                    f"window.panView && window.panView({int(delta_bars)});",
                    lambda result: None,
                )
    def set_chart_target_price(self, symbol: str, breakout_price: float) -> None:
        symbol = symbol.strip().upper()
        if not symbol or breakout_price <= 0:
            return

        item = self.watchlist.get(symbol)
        if item is None:
            item = self.watchlist.add(symbol=symbol, name=symbol)
        item.breakout_price = round(float(breakout_price), 2)
        self.populate_watchlist_table()
        self.update_dashboard_summary()
        self._save_state()
        self._reset_chart_mode_buttons()
        self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Saved breakout price for {symbol}: {item.breakout_price:.2f}")
    def clear_chart_target_price(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return

        item = self.watchlist.get(symbol)
        if item is None or item.breakout_price is None:
            return

        env = self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
        buylist_manager = getattr(self, "buylist_manager", None)
        buylist_item = buylist_manager.get(symbol, env) if buylist_manager is not None else None
        if buylist_item is not None and self._is_execution_queue_buylist_item(buylist_item):
            if buylist_item.monitoring_status in ("BOUGHT", "BUY_SUBMITTED", "BUY_PARTIAL"):
                QMessageBox.warning(
                    self,
                    "Active position",
                    f"{symbol} has an active position and cannot be dequeued here. Breakout price was not cleared.",
                )
                return
            buylist_manager.remove(symbol, env)
            self.populate_buylist_dashboard()
            self.append_log(f"[Chart] {symbol} removed from execution queue (breakout price cleared).")

        item.breakout_price = None
        self.populate_watchlist_table()
        self.update_dashboard_summary()
        self._save_state()
        self._reset_chart_mode_buttons()
        self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Removed breakout price for {symbol}.")
    def save_chart_drawing(self, symbol: str, drawing_json: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        try:
            drawing = json.loads(drawing_json)
            clean_drawing = {
                "id": str(drawing.get("id") or f"{symbol}-{dt.datetime.now().timestamp()}"),
                "type": "line",
                "start_date": str(drawing["start_date"]),
                "start_price": round(float(drawing["start_price"]), 2),
                "end_date": str(drawing["end_date"]),
                "end_price": round(float(drawing["end_price"]), 2),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return

        self.chart_drawings.setdefault(symbol, []).append(clean_drawing)
        self._save_state()
        if not self._is_active_tradingview_line_tool_symbol(symbol):
            self._reset_chart_mode_buttons()
            self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Saved chart line for {symbol}.")
    def update_chart_drawing(self, symbol: str, drawing_json: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        try:
            drawing = json.loads(drawing_json)
            drawing_id = str(drawing["id"])
            clean_drawing = {
                "id": drawing_id,
                "type": "line",
                "start_date": str(drawing["start_date"]),
                "start_price": round(float(drawing["start_price"]), 2),
                "end_date": str(drawing["end_date"]),
                "end_price": round(float(drawing["end_price"]), 2),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return

        drawings = self.chart_drawings.get(symbol, [])
        for index, existing in enumerate(drawings):
            if str(existing.get("id")) == drawing_id:
                drawings[index] = clean_drawing
                self.chart_drawings[symbol] = drawings
                self._save_state()
                if not self._is_active_tradingview_line_tool_symbol(symbol):
                    self.refresh_other_chart_views_for_symbol(symbol)
                self.append_log(f"Updated chart line for {symbol}.")
                return
    def delete_chart_drawing(self, symbol: str, drawing_id: str) -> None:
        symbol = symbol.strip().upper()
        drawing_id = str(drawing_id)
        drawings = self.chart_drawings.get(symbol, [])
        remaining = [drawing for drawing in drawings if str(drawing.get("id")) != drawing_id]
        if len(remaining) == len(drawings):
            return
        if remaining:
            self.chart_drawings[symbol] = remaining
        else:
            self.chart_drawings.pop(symbol, None)
        self._save_state()
        if not self._is_active_tradingview_line_tool_symbol(symbol):
            self._reset_chart_mode_buttons()
            self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Removed chart drawing for {symbol}.")
    def _is_active_tradingview_line_tool_symbol(self, symbol: str) -> bool:
        if not self.__dict__.get("tradingview_line_tool_active", False):
            return False
        if not hasattr(self, "tabs") or self.tabs.currentWidget() is not self.__dict__.get("tradingview_widget"):
            return False
        return self._active_chart_symbol() == symbol.strip().upper()
    def clear_chart_drawings(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol or symbol not in self.chart_drawings:
            return
        self.chart_drawings.pop(symbol, None)
        self._save_state()
        self._reset_chart_mode_buttons()
        self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Removed all chart drawings for {symbol}.")
    def clear_current_chart_drawings(self) -> None:
        symbol = self._active_chart_symbol()
        if not symbol:
            QMessageBox.information(self, "No chart symbol", "Plot a symbol before erasing drawings.")
            return
        for active_view in self._active_chart_command_views():
            if QWebEngineView is not None and isinstance(active_view, QWebEngineView):
                active_view.page().runJavaScript("window.clearAllDrawings && window.clearAllDrawings();")
        self.clear_chart_drawings(symbol)
    def clear_current_chart_target(self) -> None:
        symbol = self._active_chart_symbol()
        if not symbol:
            QMessageBox.information(self, "No chart symbol", "Plot a symbol before clearing the breakout price.")
            return
        for active_view in self._active_chart_command_views():
            if QWebEngineView is not None and isinstance(active_view, QWebEngineView):
                active_view.page().runJavaScript("window.clearTargetPrice && window.clearTargetPrice();")
        self.clear_chart_target_price(symbol)
    def _get_chart_options(self) -> dict:
        return {
            "show_volume": self.chart_show_volume_checkbox.isChecked(),
            "show_rs": self.chart_show_rs_checkbox.isChecked(),
            "show_ema": self.chart_show_ema_checkbox.isChecked(),
            "show_adr": self.chart_show_adr_checkbox.isChecked(),
            "show_growth_1m": self.chart_show_growth_1m_checkbox.isChecked(),
            "show_growth_3m": self.chart_show_growth_3m_checkbox.isChecked(),
            "show_growth_6m": self.chart_show_growth_6m_checkbox.isChecked(),
        }
    def _get_chart_navigation_state(self) -> dict:
        symbol = self._get_chart_symbol() or (self.selected_scan_symbol or "")
        return self.chart_view_windows.get(symbol.strip().upper(), {}).copy()
    def _get_chart_render_options(self) -> dict:
        options = self._get_chart_options()
        navigation = self._get_chart_navigation_state()
        if "bars" in navigation:
            options["visible_bars"] = navigation["bars"]
        if "end" in navigation:
            options["visible_end"] = navigation["end"]
        return options
    def _get_chart_render_options_for_timeframe(self, timeframe: str) -> dict:
        options = self._get_chart_render_options()
        timeframe = timeframe.strip().upper()
        if timeframe == "1H":
            options.update({
                "show_rs": False,
                "show_adr": False,
                "show_growth_1m": False,
                "show_growth_3m": False,
                "show_growth_6m": False,
                "intraday_chart": True,
                "max_history_bars": 2000,
            })
        return options
    def _load_chart_history_for_timeframe(
        self,
        symbol: str,
        timeframe: str,
        use_live_fallback: bool = True,
        window_days: int = 7,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.chart_data_controller import ChartDataController

        controller = get_controller(self, "chart_data_controller", ChartDataController)
        return controller.load_history_for_timeframe(
            symbol,
            timeframe,
            use_live_fallback=use_live_fallback,
            window_days=window_days,
            force_refresh=force_refresh,
        )
    def _fetch_latest_daily_bar_for_chart(self, symbol: str) -> pd.DataFrame:
        """Fetch the latest available daily OHLCV bar from KIS for chart refresh."""
        symbol = symbol.strip().upper()
        if not symbol:
            return pd.DataFrame()
        now = dt.datetime.now(dt.timezone.utc)
        config, profile_key = self._chart_kis_daily_config()
        unavailable_until = self.__dict__.get("kis_daily_chart_unavailable_until")
        unavailable_key = self.__dict__.get("kis_daily_chart_unavailable_key", "")
        if unavailable_until is not None and profile_key == unavailable_key and now < unavailable_until:
            return pd.DataFrame()
        try:
            from src.api.kis_fetch_all_daily import KISClient, fetch_watchlist_overseas_daily_bars

            if config is None and profile_key != "legacy":
                raise RuntimeError(f"Selected KIS chart profile is not configured: {profile_key}")
            target_yyyymmdd = dt.datetime.now(KST_ZONE).strftime("%Y%m%d")
            client = None
            exchanges = ("NAS", "NYS", "AMS")
            if config is not None:
                client = KISClient(
                    app_key=config.app_key,
                    app_secret=config.app_secret,
                    base_url=config.base_url,
                )
                exchanges = self._chart_kis_daily_exchanges(config.overseas_exchanges)
            records = fetch_watchlist_overseas_daily_bars(
                [symbol],
                target_yyyymmdd=target_yyyymmdd,
                client=client,
                exchanges=exchanges,
            )
            if not records:
                return pd.DataFrame()
            row = records[0]
            date_value = pd.to_datetime(str(row.get("date", "")), format="%Y%m%d", errors="coerce")
            if pd.isna(date_value):
                return pd.DataFrame()
            frame = pd.DataFrame(
                {
                    "Open": [float(row.get("open", 0) or 0)],
                    "High": [float(row.get("high", 0) or 0)],
                    "Low": [float(row.get("low", 0) or 0)],
                    "Close": [float(row.get("close", 0) or 0)],
                    "Volume": [float(row.get("volume", 0) or 0)],
                },
                index=[pd.Timestamp(date_value)],
            )
            if self.db_enabled and self.db_engine is not None:
                save_symbol_history_to_db(symbol, frame, self.db_engine, interval="1d")
            return frame
        except Exception as exc:
            self.kis_daily_chart_unavailable_until = now + dt.timedelta(seconds=KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS)
            self.kis_daily_chart_unavailable_key = profile_key
            error_text = str(exc)
            last_error = self.__dict__.get("kis_daily_chart_last_error", "")
            if error_text != last_error:
                self.kis_daily_chart_last_error = error_text
                self.append_log(
                    f"KIS daily chart refresh unavailable ({error_text}). "
                    "Using yfinance fallback for chart loads."
                )
            return pd.DataFrame()
    def _chart_kis_daily_config(self):
        profile = None
        kis_account_combo = self.__dict__.get("kis_account_combo")
        kis_environment_combo = self.__dict__.get("kis_environment_combo")
        if kis_account_combo is not None and kis_environment_combo is not None:
            selected_profile = kis_account_combo.currentData()
            if selected_profile:
                profile = {
                    "environment": kis_environment_combo.currentText(),
                    "account_no": selected_profile.get("account_no", ""),
                    "label": selected_profile.get("label", ""),
                }

        trade_account_combo = self.__dict__.get("trade_kis_account_combo")
        trade_environment_combo = self.__dict__.get("trade_kis_environment_combo")
        if profile is None and trade_account_combo is not None and trade_environment_combo is not None:
            trade_profile = trade_account_combo.currentData()
            if trade_profile:
                profile = {
                    "environment": trade_environment_combo.currentText(),
                    "account_no": trade_profile.get("account_no", ""),
                    "label": trade_profile.get("label", ""),
                }
        if profile is None:
            return None, "legacy"

        environment = str(profile.get("environment") or "PROD").strip().upper()
        account_no = str(profile.get("account_no") or "").strip()
        profile_key = f"{environment}:{account_no or 'default'}"
        try:
            return load_config(KisEnvironment(environment), account_no_override=account_no or None), profile_key
        except Exception:
            return None, profile_key
    @staticmethod
    def _chart_kis_daily_exchanges(exchanges) -> tuple[str, ...]:
        aliases = {
            "NASD": "NAS",
            "NASDAQ": "NAS",
            "NYSE": "NYS",
            "AMEX": "AMS",
        }
        normalized = []
        for exchange in exchanges or ():
            code = aliases.get(str(exchange).strip().upper(), str(exchange).strip().upper())
            if code and code not in normalized:
                normalized.append(code)
        return tuple(normalized) or ("NAS", "NYS", "AMS")
    def update_chart_window(self, symbol: str, visible_bars: int, visible_end: int) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        self.chart_view_windows[symbol] = {
            "bars": max(20, int(visible_bars)),
            "end": max(1, int(visible_end)),
        }
        self._set_chart_symbol(symbol)
        self.plot_selected_symbol(show_warnings=False)
    def pan_chart_window(self, delta_bars: int) -> None:
        symbol = self._get_chart_symbol() or (self.selected_scan_symbol or "")
        symbol = symbol.strip().upper()
        if not symbol:
            return

        state = self.chart_view_windows.get(symbol, {"bars": 90})
        visible_bars = max(20, int(state.get("bars", 90)))
        visible_end = int(state.get("end", 0))
        max_end = visible_end
        timeframe = (
            self.chart_timeframe_combo.currentText().strip().upper()
            if hasattr(self, "chart_timeframe_combo") and not (
                hasattr(self, "chart_split_screen_checkbox") and self.chart_split_screen_checkbox.isChecked()
            )
            else "1D"
        )
        if self.db_enabled and self.db_engine is not None:
            history = self._load_chart_history_for_timeframe(symbol, timeframe, use_live_fallback=False)
            chart_history = self._normalize_chart_history(history, symbol)
            if not chart_history.empty:
                max_end = len(chart_history) + min(30, max(0, visible_bars - 5))
                if visible_end <= 0:
                    visible_end = len(chart_history)
        if max_end <= 0:
            return

        next_end = max(1, min(max_end, visible_end + int(delta_bars)))
        self.update_chart_window(symbol, visible_bars, next_end)
    def step_chart_symbol(self, direction: int) -> None:
        if not isinstance(self.chart_symbol_input, QComboBox):
            return
        if self.chart_symbol_input.count() == 0:
            self.populate_chart_symbol_combo()
        count = self.chart_symbol_input.count()
        if count == 0:
            return

        current_symbol = self._get_chart_symbol()
        symbols = [self.chart_symbol_input.itemText(index).strip().upper() for index in range(count)]
        try:
            current_index = symbols.index(current_symbol)
        except ValueError:
            current_index = 0 if direction > 0 else count - 1

        next_index = max(0, min(count - 1, current_index + int(direction)))
        self.chart_symbol_input.setCurrentIndex(next_index)
        self._set_chart_symbol(self.chart_symbol_input.itemText(next_index))
        self.plot_selected_symbol(show_warnings=False)
    def reset_chart_full_view(self, symbol: Optional[str] = None) -> None:
        tabs = self.__dict__.get("tabs")
        is_intraday = tabs is not None and tabs.currentWidget() is self.__dict__.get("intraday_charts_widget")
        is_tradingview = tabs is not None and tabs.currentWidget() is self.__dict__.get("tradingview_widget")
        symbol = (symbol or self._active_chart_symbol() or "").strip().upper()
        if not symbol:
            return
        if is_tradingview:
            for active_view in self._active_chart_command_views():
                if QWebEngineView is not None and isinstance(active_view, QWebEngineView):
                    active_view.page().runJavaScript("window.resetFullView && window.resetFullView();")
            return
        self.chart_view_windows.pop(symbol, None)
        if is_intraday:
            self._set_intraday_symbol(symbol)
            self.plot_intraday_watchlist_symbol()
            return
        self._set_chart_symbol(symbol)
        self.plot_selected_symbol(show_warnings=False)
    def plot_selected_symbol(
        self,
        checked: bool = False,
        show_warnings: bool = True,
        use_live_fallback: bool = False,
    ) -> None:
        """Plot a symbol's price history using a local in-app chart."""
        symbol = self._get_chart_symbol()
        if not symbol and self.selected_scan_symbol:
            symbol = self.selected_scan_symbol

        if not symbol:
            if show_warnings:
                QMessageBox.warning(self, "No symbol", "Enter or select a symbol to plot.")
            return

        split_enabled = (
            hasattr(self, "chart_split_screen_checkbox")
            and self.chart_split_screen_checkbox.isChecked()
        )
        timeframes = ["1D", "1H"] if split_enabled else [
            self.chart_timeframe_combo.currentText().strip().upper()
            if hasattr(self, "chart_timeframe_combo")
            else "1D"
        ]

        histories = {
            timeframe: self._load_chart_history_for_timeframe(symbol, timeframe, use_live_fallback=use_live_fallback)
            for timeframe in timeframes
        }
        if all(history.empty for history in histories.values()):
            if show_warnings:
                QMessageBox.warning(self, "No data", f"Unable to validate {symbol}. Symbol may not exist.")
            else:
                self._set_html_or_text(
                    self.chart_view,
                    self._generate_message_html(symbol, "No chart data found."),
                    f"{symbol}: no chart data found.",
                )
            return

        chart_histories = {
            timeframe: self._normalize_chart_history(
                history,
                symbol,
                max_rows=self._get_chart_render_options_for_timeframe(timeframe).get("max_history_bars", 180),
            )
            for timeframe, history in histories.items()
        }
        if all(history.empty for history in chart_histories.values()):
            if show_warnings:
                QMessageBox.warning(self, "No data", f"Unable to build a chart for {symbol}.")
            else:
                self._set_html_or_text(
                    self.chart_view,
                    self._generate_message_html(symbol, "Unable to build chart from available data."),
                    f"{symbol}: unable to build chart from available data.",
                )
            return

        indicators = pd.DataFrame()
        if "1D" in timeframes and self.db_enabled and self.db_engine is not None:
            indicators = load_chart_indicators_from_db(symbol, self.db_engine)
            if indicators.empty and refresh_chart_indicators_for_symbol(symbol, self.db_engine, reference_symbol=REFERENCE_SYMBOL):
                indicators = load_chart_indicators_from_db(symbol, self.db_engine)

        watchlist_item = self.watchlist.get(symbol)
        target_price = watchlist_item.breakout_price if watchlist_item is not None else None
        primary_timeframe = timeframes[0]
        drawings = self._build_combined_drawings(symbol, primary_timeframe)
        primary_history = chart_histories.get(primary_timeframe, pd.DataFrame())
        primary_options = self._get_chart_render_options_for_timeframe(primary_timeframe)
        primary_window_start, primary_window_end = self._get_visible_time_window(primary_history, primary_options)
        if primary_history.empty:
            primary_html = self._generate_message_html(symbol, f"No {primary_timeframe} chart data available.")
        else:
            primary_html = self._generate_local_chart_html(
                symbol,
                primary_history,
                indicators=indicators if primary_timeframe == "1D" else pd.DataFrame(),
                options=primary_options,
                target_price=target_price,
                drawings=drawings,
            )
        self._set_html_or_text(
            self.chart_view,
            primary_html,
            f"{symbol} chart data loaded.\n\n"
            f"Latest close: {float(primary_history['Close'].iloc[-1]):.2f}" if not primary_history.empty else f"{symbol}: no {primary_timeframe} chart data.",
        )

        if split_enabled:
            self.chart_split_view.setVisible(True)
            split_history = chart_histories.get("1H", pd.DataFrame())
            split_options = self._get_chart_render_options_for_timeframe("1H")
            if primary_window_start is not None and primary_window_end is not None:
                split_options["visible_start_time"] = primary_window_start
                split_options["visible_end_time"] = primary_window_end
                split_options["visible_end_time_is_date"] = primary_timeframe == "1D"
            split_drawings = self._build_combined_drawings(symbol, "1H")
            split_html = (
                self._generate_message_html(symbol, "No 1H chart data available. Update Watchlist Intraday or wait for background fetch.")
                if split_history.empty
                else self._generate_local_chart_html(
                    symbol,
                    split_history,
                    indicators=pd.DataFrame(),
                    options=split_options,
                    target_price=target_price,
                    drawings=split_drawings,
                )
            )
            self._set_html_or_text(
                self.chart_split_view,
                split_html,
                f"{symbol} 1H chart loaded." if not split_history.empty else f"{symbol}: no 1H chart data.",
            )
        else:
            self.chart_split_view.setVisible(False)
        if not primary_history.empty:
            self.statusBar().showMessage(
                f"{symbol} {primary_timeframe} chart loaded. "
                f"Indicator cache: {'loaded' if not indicators.empty else 'not available'}."
            )
    def _draw_placeholder_chart(self) -> None:
        """Display placeholder chart."""
        placeholder_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Chart Placeholder</title>
            <style>
                body {
                    margin: 0;
                    padding: 0;
                    background-color: #1e1e1e;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    height: 100vh;
                    color: white;
                    font-family: Arial, sans-serif;
                }
                .placeholder {
                    text-align: center;
                    font-size: 18px;
                    color: #888;
                }
            </style>
        </head>
        <body>
            <div class="placeholder">
                <p>Select a symbol and click "Plot Selected Symbol" to view the local chart</p>
            </div>
        </body>
        </html>
        """
        if QWebEngineView is not None:
            self.chart_view.setHtml(placeholder_html)
        else:
            self.chart_view.setPlainText("Select a symbol and click Plot Selected Symbol.")
