from __future__ import annotations

import datetime as dt
import html
import json
import math
import sys
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
    FxRateWorker, HourlyRefreshWorker, IntradayBulkFetchWorker, IntradayFetchWorker,
    KisAccountWorker, KisOrderWorker, KisStartupAccountsWorker, OrderReconciliationWorker,
    RefreshWorker, ScannerWorker, SingleStockAiWorker, WatchlistAiWorker,
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
EXECUTION_QUEUE_FILE = Path("data/execution_queue.json")


def _main_window_global(name: str, fallback):
    module = sys.modules.get("src.ui.main_window")
    return getattr(module, name, fallback) if module is not None else fallback



class BuylistMixin:
    def _build_buylist_env_panel(self, env: str) -> QWidget:
        """Build one environment panel (PROD or SIM) for the Buy Dashboard."""
        is_prod = env == "PROD"
        accent = "#b71c1c" if is_prod else "#0d47a1"
        label_text = "PROD  —  Live Trading" if is_prod else "SIM  —  Paper Trading"

        panel = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        # â”€â”€ Header + summary bar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        header_layout = QHBoxLayout()

        env_label = QLabel(f"  {label_text}  ")
        env_label.setWordWrap(False)
        env_label.setStyleSheet(
            f"background-color: {accent}; color: white; font-weight: bold; "
            f"border-radius: 4px; padding: 2px 8px; white-space: nowrap;"
        )
        header_layout.addWidget(env_label)
        header_layout.addSpacing(12)

        positions_lbl = QLabel("Positions: 0 / 5")
        positions_lbl.setStyleSheet("font-weight: bold; color: #4CAF50;")
        capital_lbl   = QLabel("Capital: 0.0%")
        pnl_lbl       = QLabel("P&L: —")
        monitor_lbl   = QLabel("Monitor: OFF")
        monitor_lbl.setStyleSheet("color: #888;")

        header_layout.addWidget(positions_lbl)
        header_layout.addSpacing(14)
        header_layout.addWidget(capital_lbl)
        header_layout.addSpacing(14)
        header_layout.addWidget(pnl_lbl)
        header_layout.addStretch()
        header_layout.addWidget(monitor_lbl)

        monitor_btn = QPushButton("Start Monitor")
        monitor_btn.setObjectName(f"buylistMonitorToggle_{env}")
        monitor_btn.setFixedWidth(120)
        monitor_btn.clicked.connect(lambda _=False, e=env: self._toggle_buylist_monitor(e))
        header_layout.addWidget(monitor_btn)
        layout.addLayout(header_layout)

        # Store per-env summary label references
        if is_prod:
            self.buylist_prod_positions_label = positions_lbl
            self.buylist_prod_capital_label   = capital_lbl
            self.buylist_prod_pnl_label        = pnl_lbl
            self.buylist_prod_monitor_status_label = monitor_lbl
        else:
            self.buylist_sim_positions_label = positions_lbl
            self.buylist_sim_capital_label   = capital_lbl
            self.buylist_sim_pnl_label        = pnl_lbl
            self.buylist_sim_monitor_status_label = monitor_lbl

        # â”€â”€ Table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Columns: Symbol | Name | Status | Monitor | Entry(ORB) | Breakout | Stop |
        #          Current | P&L% | Shares | Capital% | Days | Alerts
        table = QTableWidget(0, 13)
        table.setHorizontalHeaderLabels([
            "Symbol", "Name", "Status", "Monitor", "Entry", "Breakout", "Stop",
            "Current", "P&L%", "Shares", "Capital%",
            "Days", "Alerts",
        ])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        for col, width in enumerate([65, 120, 80, 62, 70, 72, 70, 70, 60, 55, 65, 48, 170]):
            table.setColumnWidth(col, width)
        layout.addWidget(table, 1)

        if is_prod:
            self.buylist_prod_table = table
        else:
            self.buylist_sim_table = table

        # ——— Action buttons ——————————————————————————————————————
        btn_layout = QHBoxLayout()
        # min_width keeps multi-word labels from breaking across lines
        btns = [
            ("Activate",      80,  None,
             lambda _=False, e=env: self._buylist_activate_selected(e)),
            ("Refresh Queue", 110, None,
             lambda _=False, e=env: self.refresh_execution_queue(e)),
            ("Review Order", 105, None,
             lambda _=False, e=env: self._buylist_review_selected_queue_order(e)),
            (f"Submit {env}", 105, "background-color: #4CAF50; color: white;",
             lambda _=False, e=env: self._buylist_submit_selected_queue_order(e)),
            ("Deactivate",    90,  None,
             lambda _=False, e=env: self._buylist_deactivate_selected(e)),
            ("Breakeven",    100, "background-color: #2196F3; color: white;",
             lambda _=False, e=env: self._buylist_move_to_breakeven_selected(e)),
            ("Sell 1/3–1/2", 110, "background-color: #FF9800; color: white;",
             lambda _=False, e=env: self._buylist_sell_half_selected(e)),
            ("Sell All",      80,  "background-color: #f44336; color: white;",
             lambda _=False, e=env: self._buylist_sell_all_selected(e)),
            ("Remove",        75,  None,
             lambda _=False, e=env: self._buylist_remove_selected(e)),
            ("Refresh",       75,  None,
             lambda _=False, e=env: self.populate_buylist_dashboard()),
        ]
        for label, min_w, style, slot in btns:
            btn = QPushButton(label)
            btn.setMinimumWidth(min_w)
            if style:
                btn.setStyleSheet(style)
            btn.clicked.connect(slot)
            btn_layout.addWidget(btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        panel.setLayout(layout)
        return panel
    def _build_buylist_tab(self) -> None:
        """Build the Buylist Dashboard tab — PROD and SIM panels each with their own monitor."""
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_buylist_env_panel("PROD"))
        splitter.addWidget(self._build_buylist_env_panel("SIM"))
        splitter.setSizes([1, 1])
        layout.addWidget(splitter, 1)

        self.buylist_widget.setLayout(layout)

        # Two independent monitor timers — neither auto-starts
        self.buylist_prod_monitor_timer = QTimer()
        self.buylist_prod_monitor_timer.timeout.connect(lambda: self._run_buylist_monitor_cycle("PROD"))
        self._buylist_prod_monitor_active = False

        self.buylist_sim_monitor_timer = QTimer()
        self.buylist_sim_monitor_timer.timeout.connect(lambda: self._run_buylist_monitor_cycle("SIM"))
        self._buylist_sim_monitor_active = False

        self._buylist_order_workers: List[QThread] = []

        self.populate_buylist_dashboard()

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — populate & refresh
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def populate_buylist_dashboard(self) -> None:
        """Refresh both PROD and SIM buylist tables."""
        for env in ("PROD", "SIM"):
            self._populate_buylist_env_table(env)
    def _populate_buylist_env_table(self, env: str) -> None:
        """Populate the table for one environment and update its summary bar."""
        table_attr = f"buylist_{env.lower()}_table"
        if not hasattr(self, table_attr):
            return
        table: QTableWidget = getattr(self, table_attr)

        items = [it for it in self.buylist_manager.items if it.environment == env]
        table.setRowCount(0)

        bought_count  = sum(1 for it in items if it.monitoring_status == "BOUGHT")
        total_capital = 0.0
        total_pnl_usd = 0.0

        for item in items:
            row = table.rowCount()
            table.insertRow(row)
            queue_display = self._queue_display_state_for_buylist_item(item)
            display_status = queue_display.display_status if queue_display else self._buylist_dashboard_status(item)
            is_queue_item = self._is_execution_queue_buylist_item(item)

            current_price = (
                queue_display.current_price
                if queue_display and queue_display.current_price > 0
                else self.latest_intraday_prices.get(item.symbol, 0.0)
            )
            pnl_pct = pnl_usd = 0.0
            if item.monitoring_status == "BOUGHT" and item.avg_cost > 0 and current_price > 0:
                pnl_pct = (current_price - item.avg_cost) / item.avg_cost * 100.0
                pnl_usd = (current_price - item.avg_cost) * item.shares_held

            days_held = 0
            if item.buy_date:
                from datetime import datetime as _dt
                days_held = (_dt.now() - item.buy_date).days

            # For BOUGHT positions use the frozen position_percent snapshotted at fill time —
            # account_size_input can change (e.g. KIS balance load) and would give nonsense %.
            if item.monitoring_status == "BOUGHT" and item.shares_held > 0 and item.avg_cost > 0:
                capital_pct = item.position_percent
            elif queue_display:
                capital_pct = queue_display.capital_percent
            else:
                account_size = self._parse_float(self.account_size_input, 100000.0) if hasattr(self, "account_size_input") else 100000.0
                capital_pct = (
                    item.shares_held * item.avg_cost / account_size * 100.0
                    if account_size > 0 and item.avg_cost > 0
                    else item.position_percent
                )
            alerts = self._buylist_compute_alerts(item, current_price, days_held, queue_display)

            def _cell(text: str) -> QTableWidgetItem:
                c = QTableWidgetItem(str(text))
                c.setTextAlignment(Qt.AlignCenter)
                return c

            entry_price = queue_display.entry_price if queue_display else item.entry_price
            stop_loss = queue_display.stop_loss if queue_display else item.stop_loss
            bp_val = (
                queue_display.breakout_price
                if queue_display and queue_display.breakout_price is not None
                else getattr(item, "breakout_price", None)
            ) or 0.0
            bp_display = f"{bp_val:.2f}" if bp_val > 0 else "—"

            table.setItem(row, 0,  _cell(queue_display.symbol if queue_display else item.symbol))
            display_name = queue_display.name if queue_display else item.name
            table.setItem(row, 1,  _cell(display_name[:16] if display_name else ""))
            table.setItem(row, 2,  _cell(display_status))
            monitor_on = item.monitoring_status in ("ACTIVE", "BOUGHT") and not (
                is_queue_item and item.monitoring_status == "ACTIVE"
            )
            table.setItem(row, 3,  _cell("ON" if monitor_on else "OFF"))
            table.setItem(row, 4,  _cell(f"{entry_price:.2f}"))
            table.setItem(row, 5,  _cell(bp_display))                  # daily breakout level
            table.setItem(row, 6,  _cell(f"{stop_loss:.2f}"))
            table.setItem(row, 7,  _cell(f"{current_price:.2f}" if current_price > 0 else "-"))
            table.setItem(row, 8,  _cell(f"{pnl_pct:+.1f}%" if item.monitoring_status == "BOUGHT" else "-"))
            planned_shares = queue_display.planned_shares if queue_display else int(getattr(item, "_planned_shares", 0) or 0)
            display_shares = item.shares_held if item.monitoring_status == "BOUGHT" else planned_shares
            table.setItem(row, 9,  _cell(str(display_shares) if display_shares > 0 else "-"))
            table.setItem(row, 10, _cell(f"{capital_pct:.1f}%"))
            table.setItem(row, 11, _cell(str(days_held) if item.monitoring_status == "BOUGHT" else "-"))

            alert_cell = _cell(alerts if alerts else "OK")
            if "STOP" in alerts:
                alert_cell.setBackground(QColor("#e53935"))
                alert_cell.setForeground(QColor("white"))
            elif alerts and alerts != "OK":
                alert_cell.setBackground(QColor("#fb8c00"))
                alert_cell.setForeground(QColor("white"))
            table.setItem(row, 12, alert_cell)

            # Row color by status
            row_color = None
            if item.monitoring_status == "BOUGHT":
                row_color = QColor("#2e7d32") if pnl_pct >= 0 else QColor("#c62828")  # medium green / red
            elif item.monitoring_status == "ACTIVE" and not is_queue_item:
                row_color = QColor("#1565c0")    # medium blue
            elif item.monitoring_status == "SOLD":
                row_color = QColor("#546e7a")    # blue-grey
            if row_color:
                for col in range(table.columnCount()):
                    cell = table.item(row, col)
                    if cell:
                        cell.setBackground(row_color)

            if item.monitoring_status == "BOUGHT":
                total_capital += capital_pct
                total_pnl_usd += pnl_usd

        # Update summary labels
        pos_lbl = getattr(self, f"buylist_{env.lower()}_positions_label", None)
        cap_lbl = getattr(self, f"buylist_{env.lower()}_capital_label", None)
        pnl_lbl = getattr(self, f"buylist_{env.lower()}_pnl_label", None)
        if pos_lbl:
            pos_lbl.setText(f"Positions: {bought_count} / 5")
            pos_lbl.setStyleSheet(f"font-weight: bold; color: {'#f44336' if bought_count >= 5 else '#4CAF50'};")
        if cap_lbl:
            cap_lbl.setText(f"Capital: {total_capital:.1f}%")
        if pnl_lbl:
            sign = "+" if total_pnl_usd >= 0 else ""
            pnl_lbl.setText(f"P&L: {sign}${total_pnl_usd:,.0f}")
            pnl_lbl.setStyleSheet(f"color: {'#4CAF50' if total_pnl_usd >= 0 else '#f44336'}; font-weight: bold;")
    def _buylist_compute_alerts(self, item, current_price: float, days_held: int, queue_display=None) -> str:
        """Return a pipe-separated alert string for a buylist item."""
        alerts = []
        if queue_display is not None:
            if queue_display.display_status:
                alerts.append(queue_display.display_status)
            if queue_display.selected_window:
                alerts.append(f"ORB {queue_display.selected_window}")
            if queue_display.planned_shares > 0:
                alerts.append(f"Qty {queue_display.planned_shares}")
            return " | ".join(dict.fromkeys(alerts))

        queue_status = self._execution_queue_status_for_buylist_item(item)
        if queue_status:
            alerts.append(queue_status)

        if item.monitoring_status == "BOUGHT":
            if current_price > 0 and item.stop_loss > 0 and current_price <= item.stop_loss:
                alerts.append("STOP HIT")
            if getattr(item, "auto_order_block_reason", ""):
                alerts.append("KIS ORDER BLOCKED")
            if 3 <= days_held <= 5 and not item.sell_half_done:
                alerts.append("SELL 1/3—1/2 (day rule)")
            ema10 = getattr(item, "_ema10", 0.0)
            ema20 = getattr(item, "_ema20", 0.0)
            if ema10 > 0 and current_price < ema10:
                alerts.append("< 10 EMA")
            if ema20 > 0 and current_price < ema20:
                alerts.append("< 20 EMA")
        elif item.monitoring_status == "ACTIVE":
            if self._is_orb_buylist_item(item):
                alerts.append("QUEUE REQUIRED")
                return " | ".join(dict.fromkeys(alerts))
            bought_count = sum(1 for it in self.buylist_manager.items
                               if it.monitoring_status == "BOUGHT" and it.environment == item.environment)
            if bought_count >= 5:
                alerts.append("MAX POSITIONS")
            # Show where price stands relative to ORB high and daily breakout trigger
            bp = getattr(item, "breakout_price", None) or 0.0
            if bp > 0 and current_price > 0:
                buf = getattr(item, "buffer_pct", 0.001)
                breakout_trigger = bp * (1 + buf)
                entry_trigger = max(item.entry_price, breakout_trigger)
                if current_price >= entry_trigger:
                    alerts.append(f"TRIGGER MET ${entry_trigger:.2f}")
                elif current_price > item.entry_price:
                    alerts.append(f"ORB OK / below BKT ${breakout_trigger:.2f}")
                else:
                    alerts.append(f"below ORB ${item.entry_price:.2f}")
        else:
            queue_statuses = self._execution_queue_status_values()
            status_text = str(getattr(item, "monitoring_status", "") or "").upper()
            if status_text in queue_statuses:
                alerts.append(status_text)
            selected_window = getattr(item, "_selected_orb_window", "")
            if selected_window:
                alerts.append(f"ORB {selected_window}")
            planned_shares = int(getattr(item, "_planned_shares", 0) or 0)
            if planned_shares > 0:
                alerts.append(f"Qty {planned_shares}")
        return " | ".join(dict.fromkeys(alerts))

    @staticmethod
    def _execution_queue_value(value) -> str:
        return str(getattr(value, "value", value) or "")

    @staticmethod
    def _execution_queue_status_values() -> set:
        from src.core.execution_queue import ExecutionQueueStatus

        return {status.value for status in ExecutionQueueStatus}

    def _execution_queue_status_for_buylist_item(self, item) -> Optional[str]:
        if item is None or not self._is_pre_entry_execution_queue_buylist_item(item):
            return None
        queue_item = self._execution_queue_item_for_buylist_item(item)
        if queue_item is None:
            return None
        return self._execution_queue_value(queue_item.status)

    def _queue_display_state_for_buylist_item(self, item):
        if item is None or not self._is_pre_entry_execution_queue_buylist_item(item):
            return None
        queue_item = self._execution_queue_item_for_buylist_item(item)
        if queue_item is None:
            return None
        from src.core.execution_queue import build_queue_display_state

        return build_queue_display_state(queue_item, item)

    def _execution_queue_item_for_buylist_item(self, item):
        if item is None:
            return None
        symbol = str(getattr(item, "symbol", "") or "").upper()
        if not symbol:
            return None
        environment = str(getattr(item, "environment", "") or "SIM").upper()
        manager = self.__dict__.get("execution_queue_manager")
        if manager is None:
            manager = self._ensure_execution_queue_manager()
        get_item = getattr(manager, "get_item", None)
        if callable(get_item):
            return get_item(symbol, environment)
        from src.core.execution_queue import queue_key

        queue_item = manager.items.get(queue_key(symbol, environment))
        if queue_item is None and environment == "SIM":
            queue_item = manager.items.get(symbol)
        return queue_item

    def _buylist_dashboard_status(self, item) -> str:
        queue_status = self._execution_queue_status_for_buylist_item(item)
        if queue_status:
            return queue_status
        return str(getattr(item, "monitoring_status", "") or "")

    def _is_orb_buylist_item(self, item) -> bool:
        if self._is_execution_queue_buylist_item(item):
            return True
        method = str(getattr(item, "breakout_method", "") or "").lower()
        if "orb" in method:
            return True
        if str(getattr(item, "_selected_orb_window", "") or ""):
            return True
        try:
            breakout_price = float(getattr(item, "breakout_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            breakout_price = 0.0
        return breakout_price > 0

    def _ensure_execution_queue_manager(self):
        from src.core.execution_queue import ExecutionQueueManager

        manager = self.__dict__.get("execution_queue_manager")
        if manager is not None:
            return manager

        data = load_json(EXECUTION_QUEUE_FILE, {})
        try:
            manager = ExecutionQueueManager.from_dict(data) if data else ExecutionQueueManager()
        except Exception as exc:
            self.append_log(f"Execution queue state could not be loaded; starting fresh: {exc}")
            manager = ExecutionQueueManager()
        self.execution_queue_manager = manager
        return manager

    def _save_execution_queue_state(self) -> None:
        manager = self.__dict__.get("execution_queue_manager")
        if manager is None:
            return
        try:
            save_json(EXECUTION_QUEUE_FILE, manager.to_dict())
        except Exception as exc:
            self.append_log(f"Execution queue state save failed: {exc}")

    @staticmethod
    def _format_queue_price(value) -> str:
        try:
            price = float(value or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        return f"${price:.2f}" if price > 0 else "-"

    @staticmethod
    def _format_queue_percent(value) -> str:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            return "-"
        return f"{pct:.1f}%"

    @staticmethod
    def _is_execution_queue_buylist_item(item) -> bool:
        from src.core.execution_queue import ExecutionQueueStatus

        queue_statuses = {status.value for status in ExecutionQueueStatus}
        method = str(getattr(item, "breakout_method", "") or "")
        status = str(getattr(item, "monitoring_status", "") or "").upper()
        return method.startswith("execution_queue") or status in queue_statuses

    @staticmethod
    def _is_pre_entry_execution_queue_buylist_item(item) -> bool:
        from src.core.execution_queue import is_pre_entry_execution_queue_item

        return is_pre_entry_execution_queue_item(item)

    def _execution_queue_target_items(
        self,
        env: str,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ) -> Tuple[List[Any], List[str]]:
        watch_items = list(getattr(getattr(self, "watchlist", None), "items", []) or [])
        watch_by_symbol = {
            str(getattr(item, "symbol", "") or "").strip().upper(): item
            for item in watch_items
            if str(getattr(item, "symbol", "") or "").strip()
        }
        queued_symbols = [
            str(getattr(item, "symbol", "") or "").strip().upper()
            for item in list(getattr(getattr(self, "buylist_manager", None), "items", []) or [])
            if str(getattr(item, "environment", "") or "").upper() == env
            and self._is_execution_queue_buylist_item(item)
        ]

        if symbols is None:
            target_symbols = queued_symbols
        else:
            requested = []
            for raw_symbol in symbols:
                symbol = str(raw_symbol or "").strip().upper()
                if symbol and symbol not in requested:
                    requested.append(symbol)
            target_symbols = requested if create_missing else [symbol for symbol in requested if symbol in queued_symbols]

        targets: List[Any] = []
        missing: List[str] = []
        for symbol in target_symbols:
            item = watch_by_symbol.get(symbol)
            if item is None:
                existing = self.buylist_manager.get(symbol, env) if hasattr(self, "buylist_manager") else None
                if existing is not None and self._is_execution_queue_buylist_item(existing):
                    item = existing
            if item is None:
                missing.append(symbol)
                continue
            targets.append(item)
        return targets, missing

    def _build_execution_queue_refresh_request(
        self,
        env: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ):
        from src.ui.controllers.buylist_execution_controller import ExecutionQueueRefreshRequest

        env = (env or (self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM")).upper()
        requested_symbols = None
        if symbols is not None:
            requested_symbols = []
            for raw_symbol in symbols:
                symbol = str(raw_symbol or "").strip().upper()
                if symbol and symbol not in requested_symbols:
                    requested_symbols.append(symbol)

        target_items, missing_symbols = self._execution_queue_target_items(
            env,
            requested_symbols,
            create_missing=create_missing,
        )
        manager = None
        account_size = 100000.0
        risk_percent = 0.01
        buffer_pct = 0.001
        account_no = ""
        if target_items:
            manager = self._ensure_execution_queue_manager()
            account_size = self._get_account_balance_for_env(env) if hasattr(self, "_get_account_balance_for_env") else 100000.0
            risk_percent = (
                self._parse_float(self.risk_percent_input, 1.0) / 100.0
                if hasattr(self, "risk_percent_input") else 0.01
            )
            if risk_percent <= 0:
                risk_percent = 0.01
            buffer_pct = self._watchlist_orb_buffer_pct() if hasattr(self, "_watchlist_orb_buffer_pct") else 0.001
            account_no = self._first_account_no_for_environment(env) or ""

        return ExecutionQueueRefreshRequest(
            env=env,
            manager=manager,
            buylist_manager=self.buylist_manager,
            target_items=target_items,
            missing_symbols=missing_symbols,
            requested_symbols=requested_symbols,
            account_size=account_size,
            risk_percent=risk_percent,
            buffer_pct=buffer_pct,
            account_no=account_no,
            latest_intraday_session=self._latest_intraday_session,
            load_intraday_interval=lambda symbol, interval, window_days: self._load_cached_intraday_interval(
                symbol,
                interval,
                window_days=window_days,
            ),
            signal_price_for_symbol=(
                self._watchlist_orb_signal_price if hasattr(self, "_watchlist_orb_signal_price") else lambda _symbol: 0.0
            ),
            set_latest_intraday_price=lambda symbol, price: self.latest_intraday_prices.__setitem__(symbol, price),
            has_duplicate_open_order=self._has_duplicate_open_order,
            adr_percent_for_symbol=self._calculate_adr_percent_for_symbol,
        )

    def _apply_execution_queue_refresh_result(self, result, show_log: bool = True) -> None:
        if result.target_count > 0:
            self.populate_buylist_dashboard()
            if hasattr(self, "update_dashboard_summary"):
                self.update_dashboard_summary()
            self._save_buylist_state()
            self._save_execution_queue_state()

        if not show_log:
            return

        if result.target_count == 0:
            if result.requested_symbols is None:
                self.append_log(f"[Execution Queue/{result.env}] No queued buylist symbols to refresh.")
            else:
                self.append_log(f"[Execution Queue/{result.env}] No selected watchlist symbols could be queued.")
            if result.missing_symbols:
                self.append_log(f"[Execution Queue/{result.env}] Missing symbols: " + ", ".join(result.missing_symbols[:10]))
            return

        counts_text = ", ".join(f"{key}={value}" for key, value in sorted(result.status_counts.items())) or "none"
        self.append_log(
            f"[Execution Queue/{result.env}] Refreshed {result.refreshed} {result.scope} symbol(s): {counts_text}."
        )
        if result.missing_symbols:
            self.append_log(f"[Execution Queue/{result.env}] Missing symbols: " + ", ".join(result.missing_symbols[:10]))
        if result.failures:
            self.append_log(f"[Execution Queue/{result.env}] Refresh failures: " + "; ".join(result.failures[:10]))

    def refresh_execution_queue(
        self,
        env: Optional[str] = None,
        show_log: bool = True,
        symbols: Optional[List[str]] = None,
        *,
        create_missing: bool = False,
    ) -> int:
        """Refresh existing queue rows, or intentionally queue selected symbols."""
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.buylist_execution_controller import BuylistExecutionController

        controller = get_controller(self, "buylist_execution_controller", BuylistExecutionController)
        request = self._build_execution_queue_refresh_request(
            env,
            symbols=symbols,
            create_missing=create_missing,
        )
        result = controller.refresh_execution_queue(request)
        self._last_execution_queue_refresh_result = result
        self._apply_execution_queue_refresh_result(result, show_log=show_log)
        return result.refreshed

    def _apply_execution_queue_item_to_buylist(self, queue_item, watch_item, env: str, buffer_pct: float) -> None:
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.buylist_execution_controller import BuylistExecutionController

        controller = get_controller(self, "buylist_execution_controller", BuylistExecutionController)
        controller.apply_execution_queue_item_to_buylist(queue_item, watch_item, env, buffer_pct)

    def _queue_item_for_buylist_item(self, item):
        if item is None:
            return None
        return self._execution_queue_item_for_buylist_item(item)

    def _format_execution_queue_order_review(self, env: str, item, queue_item) -> str:
        candidate = getattr(queue_item, "selected_candidate", None)
        if candidate is None:
            return f"{item.symbol} has no selected ORB candidate."

        account_no = self._first_account_no_for_environment(env) or "<not selected>"
        entry_trigger = float(candidate.entry_trigger or 0.0)
        shares = int(candidate.shares or 0)
        stop_loss = float(candidate.stop_loss or 0.0)
        estimated_amount = entry_trigger * shares
        risk_amount = max(0.0, entry_trigger - stop_loss) * shares
        warnings = list(getattr(candidate, "warnings", []) or []) + list(getattr(queue_item, "warnings", []) or [])
        warning_text = "; ".join(dict.fromkeys(warnings)) if warnings else "None"
        return "\n".join([
            f"Environment: {env}",
            f"Account: {account_no}",
            f"Symbol: {item.symbol}",
            f"Selected ORB: {candidate.window}",
            "Side: BUY",
            f"Limit price: {self._format_queue_price(entry_trigger)}",
            f"Quantity: {shares}",
            f"Estimated amount: {self._format_queue_price(estimated_amount)}",
            f"Breakout price: {self._format_queue_price(candidate.breakout_price)}",
            f"Breakout trigger: {self._format_queue_price(candidate.breakout_trigger)}",
            f"ORB high: {self._format_queue_price(candidate.orb_high)}",
            f"ORB low: {self._format_queue_price(candidate.orb_low)}",
            f"Entry trigger: {self._format_queue_price(candidate.entry_trigger)}",
            f"Stop loss: {self._format_queue_price(candidate.stop_loss)}",
            f"Risk amount: {self._format_queue_price(risk_amount)}",
            f"Capital allocation: {self._format_queue_percent(candidate.capital_percent)}",
            f"Stop/ADR: {self._format_queue_percent(candidate.stop_adr)}",
            f"Score: {float(candidate.score or 0.0):.1f}",
            f"Warnings: {warning_text}",
        ])

    def _buylist_review_selected_queue_order(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select an execution queue row first.")
            return
        queue_item = self._queue_item_for_buylist_item(item)
        if queue_item is None:
            QMessageBox.warning(self, "No queue item", f"{item.symbol} is not in the execution queue. Click Refresh Queue first.")
            return
        review = self._format_execution_queue_order_review(env, item, queue_item)
        QMessageBox.information(self, f"Review BUY Order - {item.symbol}", review)

    def _buylist_submit_selected_queue_order(self, env: str) -> None:
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.buylist_execution_controller import BuylistExecutionController

        controller = get_controller(self, "buylist_execution_controller", BuylistExecutionController)
        controller.submit_selected_queue_order(env)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — action button handlers
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _buylist_selected_item(self, env: str):
        """Return the BuylistItem for the selected row in the given env table, or None."""
        table_attr = f"buylist_{env.lower()}_table"
        if not hasattr(self, table_attr):
            return None
        table: QTableWidget = getattr(self, table_attr)
        if not table.selectedItems():
            return None
        row = table.currentRow()
        sym_cell = table.item(row, 0)
        if not sym_cell:
            return None
        return self.buylist_manager.get(sym_cell.text().strip().upper(), env)
    def _buylist_activate_selected(self, env: str) -> None:
        """Activate the selected buylist item for entry monitoring."""
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to activate.")
            return
        if item.monitoring_status == "BOUGHT":
            QMessageBox.information(self, "Already bought", f"{item.symbol} is already in a BOUGHT position.")
            return
        if self._is_orb_buylist_item(item):
            QMessageBox.information(
                self,
                "Execution Queue",
                f"{item.symbol} is an ORB entry. Queue it from the Watchlist and use Review Order and Submit Buy.",
            )
            return
        bought_count = sum(1 for it in self.buylist_manager.items if it.monitoring_status == "BOUGHT" and it.environment == env)
        if bought_count >= 5:
            QMessageBox.warning(self, "Max positions", "Already holding 5 positions. Sell one before activating another.")
            return
        item.monitoring_status = "ACTIVE"
        self._clear_buylist_auto_order_block(item)
        self._save_state()
        self.populate_buylist_dashboard()
        self.append_log(f"[Buylist/{env}] {item.symbol} set to ACTIVE — monitoring for entry at ${item.entry_price:.2f}.")
    def _buylist_deactivate_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to deactivate.")
            return
        if item.monitoring_status != "BOUGHT" and self._is_execution_queue_buylist_item(item):
            QMessageBox.information(
                self,
                "Execution Queue",
                f"{item.symbol} is managed by the execution queue. Remove the row if it is no longer needed.",
            )
            return
        if item.monitoring_status == "BOUGHT":
            reply = QMessageBox.question(
                self,
                "Reset position?",
                f"{item.symbol} is BOUGHT ({item.shares_held} shares @ ${item.avg_cost:.2f}).\n\n"
                f"Reset to WATCHING — no sell order is placed.\n"
                f"Only use this to discard incorrect SIM entries.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            item.shares_held = 0
            item.avg_cost = 0.0
            item.buy_date = None
            item.sell_half_done = False
            item.kis_order_id = ""
            item.position_percent = 0.0
            item.monitoring_status = "WATCHING"
            self._clear_buylist_auto_order_block(item)
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(f"[Buylist/{env}] {item.symbol} position reset to WATCHING (no KIS order placed).")
            return
        item.monitoring_status = "WATCHING"
        self._clear_buylist_auto_order_block(item)
        self._save_state()
        self.populate_buylist_dashboard()
        self.append_log(f"[Buylist/{env}] {item.symbol} deactivated.")
    def _buylist_sell_half_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to sell.")
            return
        if item.monitoring_status != "BOUGHT" or item.shares_held <= 0:
            QMessageBox.warning(self, "No position", f"{item.symbol} has no open position.")
            return
        qty_third = max(1, item.shares_held // 3)
        qty_half = max(1, item.shares_held // 2)

        dialog = QDialog(self)
        dialog.setWindowTitle("Partial Sell — Day Rule")
        layout = QVBoxLayout()

        info_label = QLabel(
            f"Sell partial position in {item.symbol}  ({item.shares_held} shares held).\n"
            f"Choose any amount between 1/3 ({qty_third} shares) and 1/2 ({qty_half} shares):"
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(qty_third)
        slider.setMaximum(qty_half)
        slider.setValue(qty_third)
        slider.setEnabled(qty_half > qty_third)
        layout.addWidget(slider)

        spin = QSpinBox()
        spin.setMinimum(qty_third)
        spin.setMaximum(qty_half)
        spin.setValue(qty_third)
        layout.addWidget(spin)

        pct_label = QLabel(f"{qty_third / item.shares_held:.1%} of position")
        layout.addWidget(pct_label)

        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)

        def _on_value_changed(value: int) -> None:
            pct_label.setText(f"{value / item.shares_held:.1%} of position")

        spin.valueChanged.connect(_on_value_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        dialog.setLayout(layout)
        if dialog.exec_() == QDialog.Accepted:
            self._submit_kis_sell_order(item, spin.value(), reason="partial sell")
    def _buylist_sell_all_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to sell.")
            return
        if item.monitoring_status != "BOUGHT" or item.shares_held <= 0:
            QMessageBox.warning(self, "No position", f"{item.symbol} has no open position.")
            return
        reply = QMessageBox.question(
            self, "Confirm Sell All",
            f"Sell all {item.shares_held} shares of {item.symbol}?\nThis will submit a market order.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._submit_kis_sell_order(item, item.shares_held, reason="manual sell all")
    def _buylist_remove_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row to remove.")
            return
        if item.monitoring_status in ("ACTIVE", "BOUGHT"):
            reply = QMessageBox.question(
                self, "Confirm Remove",
                f"{item.symbol} is currently {item.monitoring_status}. Remove anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self.buylist_manager.remove(item.symbol, env)
        self._save_state()
        self.populate_buylist_dashboard()
        self.append_log(f"[Buylist/{env}] {item.symbol} removed from buylist.")
    def _buylist_move_to_breakeven_selected(self, env: str) -> None:
        item = self._buylist_selected_item(env)
        if not item:
            QMessageBox.warning(self, "No selection", "Select a buylist row first.")
            return
        breakeven = item.avg_cost if item.avg_cost > 0 else item.entry_price
        if breakeven <= 0:
            QMessageBox.warning(self, "No price", f"No avg cost or entry price set for {item.symbol}.")
            return
        if item.stop_loss >= breakeven:
            QMessageBox.information(
                self, "Already at Breakeven",
                f"{item.symbol} stop (${item.stop_loss:.2f}) is already at or above breakeven (${breakeven:.2f})."
            )
            return
        reply = QMessageBox.question(
            self, "Move Stop to Breakeven",
            f"Move {item.symbol} stop loss from ${item.stop_loss:.2f} to breakeven ${breakeven:.2f}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        old_stop = item.stop_loss
        item.stop_loss = breakeven
        self._save_state()
        self.populate_buylist_dashboard()
        self.append_log(
            f"[Buylist/{env}] {item.symbol} stop manually moved to breakeven "
            f"${breakeven:.2f} (was ${old_stop:.2f})."
        )


    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — monitor timer
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _toggle_buylist_monitor(self, env: str) -> None:
        """Toggle the monitor timer for one environment (PROD or SIM)."""
        active_attr = f"_buylist_{env.lower()}_monitor_active"
        timer_attr  = f"buylist_{env.lower()}_monitor_timer"
        lbl_attr    = f"buylist_{env.lower()}_monitor_status_label"
        btn_name    = f"buylistMonitorToggle_{env}"

        if not hasattr(self, timer_attr):
            return

        timer: QTimer = getattr(self, timer_attr)
        is_active: bool = getattr(self, active_attr, False)
        lbl: QLabel = getattr(self, lbl_attr, None)
        btn = self.findChild(QPushButton, btn_name)

        if is_active:
            timer.stop()
            setattr(self, active_attr, False)
            if lbl:
                lbl.setText("Monitor: OFF")
                lbl.setStyleSheet("color: #888;")
            if btn:
                btn.setText("Start Monitor")
            self.append_log(f"[Buylist/{env}] Monitor stopped.")
        else:
            timer.start(60000)
            setattr(self, active_attr, True)
            if lbl:
                lbl.setText("Monitor: ON (60s)")
                lbl.setStyleSheet("color: #4CAF50; font-weight: bold;")
            if btn:
                btn.setText("Stop Monitor")
            self.append_log(f"[Buylist/{env}] Monitor started — checking every 60 seconds.")
            self._run_buylist_monitor_cycle(env)  # run immediately
    def _run_buylist_monitor_cycle(self, env: str) -> None:
        """Check ACTIVE/BOUGHT items for one environment and fire orders as needed."""
        if not hasattr(self, "buylist_manager"):
            return

        items = [it for it in self.buylist_manager.items if it.environment == env]
        self._restore_monitorable_buylist_error_positions(items, env)
        active_items = [it for it in items if it.monitoring_status in ("ACTIVE", "BOUGHT")]
        if not active_items:
            return

        bought_count = sum(1 for it in items if it.monitoring_status == "BOUGHT")

        for item in active_items:
            self._buylist_refresh_item_data(item)
            current_price = self.latest_intraday_prices.get(item.symbol, 0.0)
            if current_price <= 0:
                continue

            if item.monitoring_status == "ACTIVE":
                if self._is_orb_buylist_item(item):
                    if not getattr(item, "_orb_queue_required_notice_logged", False):
                        item._orb_queue_required_notice_logged = True
                        self.append_log(
                            f"[Buylist/{env}] {item.symbol} is an ORB entry; skipping legacy ACTIVE auto-buy. "
                            "Use the execution queue Review Order and Submit Buy flow."
                        )
                    continue

                # Compute entry_trigger: max(ORB high, breakout_price * (1+buffer))
                bp = getattr(item, "breakout_price", None) or 0.0
                buf = getattr(item, "buffer_pct", 0.001)
                breakout_trigger = bp * (1 + buf) if bp > 0 else 0.0
                entry_trigger = max(item.entry_price, breakout_trigger) if breakout_trigger > 0 else item.entry_price
                auto_order_blocked = self._buylist_auto_order_blocked(item)

                # Chase guard: if price has already run â‰¥2% above the trigger the setup is
                # stale (entered from a previous session or the breakout already happened).
                # Do not auto-buy; log once per cycle.
                MAX_CHASE_PCT = 0.02
                if current_price > entry_trigger * (1 + MAX_CHASE_PCT):
                    overshoot_pct = (current_price / entry_trigger - 1) * 100
                    self.append_log(
                        f"[Buylist/{env}] {item.symbol} price ${current_price:.2f} is "
                        f"+{overshoot_pct:.1f}% above trigger ${entry_trigger:.2f} — "
                        f"setup stale, skipping auto-buy."
                    )
                elif (
                    bought_count < 5
                    and current_price >= entry_trigger
                    and not auto_order_blocked
                    and not getattr(item, "_buy_order_pending", False)
                ):
                    item._buy_order_pending = True
                    if bp > 0:
                        self.append_log(
                            f"[Buylist/{env}] {item.symbol} confirmed breakout — "
                            f"trigger ${entry_trigger:.2f} (ORB ${item.entry_price:.2f}, "
                            f"daily breakout ${bp:.2f}, current ${current_price:.2f}) — submitting BUY."
                        )
                    else:
                        self.append_log(
                            f"[Buylist/{env}] {item.symbol} hit ORB entry ${entry_trigger:.2f} "
                            f"(current ${current_price:.2f}) — submitting BUY order."
                        )
                    # Order at current_price so avg_cost reflects what we'd actually pay;
                    # entry_trigger is the condition, not necessarily the fill price.
                    self._submit_kis_buy_order(item, order_price=current_price)
                    bought_count += 1
                elif (
                    current_price >= entry_trigger
                    and auto_order_blocked
                    and not getattr(item, "_auto_order_block_notice_logged", False)
                ):
                    item._auto_order_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] Trigger met for {item.symbol}, but auto KIS order is blocked: "
                        f"{getattr(item, 'auto_order_block_reason', '')}"
                    )
                elif bp > 0 and current_price > item.entry_price and current_price < breakout_trigger:
                    self.append_log(
                        f"[Buylist/{env}] {item.symbol} above ORB ${item.entry_price:.2f} "
                        f"but below breakout trigger ${breakout_trigger:.2f} (${current_price:.2f}) — waiting."
                    )

            elif item.monitoring_status == "BOUGHT":
                auto_order_blocked = self._buylist_auto_order_blocked(item)
                if (
                    item.stop_loss > 0
                    and current_price <= item.stop_loss
                    and not auto_order_blocked
                    and not getattr(item, "_stop_order_pending", False)
                ):
                    item._stop_order_pending = True
                    self.append_log(
                        f"[Buylist/{env}] STOP HIT — {item.symbol} ${current_price:.2f} "
                        f"<= stop ${item.stop_loss:.2f}. Submitting SELL ALL."
                    )
                    self._submit_kis_sell_order(item, item.shares_held, reason="stop-loss")
                elif (
                    item.stop_loss > 0
                    and current_price <= item.stop_loss
                    and auto_order_blocked
                    and not getattr(item, "_auto_order_block_notice_logged", False)
                ):
                    item._auto_order_block_notice_logged = True
                    self.append_log(
                        f"[Buylist/{env}] STOP still hit for {item.symbol}, but auto KIS order is blocked: "
                        f"{getattr(item, 'auto_order_block_reason', '')}"
                    )

        self._populate_buylist_env_table(env)

    def _restore_monitorable_buylist_error_positions(self, items, env: str) -> None:
        """Keep held positions monitorable after a rejected exit order."""
        changed = False
        for item in items:
            if getattr(item, "monitoring_status", "") != "ERROR":
                continue
            try:
                shares_held = int(getattr(item, "shares_held", 0) or 0)
            except (TypeError, ValueError):
                shares_held = 0
            if shares_held <= 0:
                continue

            item.monitoring_status = "BOUGHT"
            item._stop_order_pending = False
            changed = True
            self.append_log(
                f"[Buylist/{env}] {item.symbol} restored from ERROR to BOUGHT "
                f"because {shares_held} shares are still marked held."
            )
        if changed:
            self._save_buylist_state()

    @staticmethod
    def _is_kis_sim_unsupported_order_error(error_message: str) -> bool:
        return "90000000" in str(error_message or "")

    @staticmethod
    def _buylist_auto_order_blocked(item) -> bool:
        return bool(str(getattr(item, "auto_order_block_reason", "") or "").strip())

    def _set_buylist_auto_order_block(self, item, reason: str) -> None:
        item.auto_order_block_reason = str(reason or "").strip()
        item._buy_order_pending = False
        item._stop_order_pending = False
        item._auto_order_block_notice_logged = False

    def _clear_buylist_auto_order_block(self, item) -> None:
        if hasattr(item, "auto_order_block_reason"):
            item.auto_order_block_reason = ""
        item._auto_order_block_notice_logged = False
    def _buylist_refresh_item_data(self, item) -> None:
        """Fetch latest 30d daily closes and compute 10/20 EMA for a buylist item.

        Calls Yahoo Finance v8 chart API directly with a browser User-Agent — yfinance's
        internal requests get blocked by Yahoo when the default bot UA is used.
        Falls back to yfinance.Ticker.history() if the direct call fails.
        """
        import requests as _req

        symbol = item.symbol
        closes = None

        # Primary: direct Yahoo Finance v8 chart API (browser UA bypasses Yahoo's bot block)
        try:
            session = getattr(self, "_yf_session", None)
            if session is None:
                session = _req.Session()
                session.headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
                self._yf_session = session

            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            r = session.get(url, params={"interval": "1d", "range": "30d", "events": "div,splits"}, timeout=15)
            r.raise_for_status()
            payload = r.json()
            raw_closes = payload["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [float(c) for c in raw_closes if c is not None]
        except Exception as exc:
            self.append_log(f"[Buylist] Direct fetch failed for {symbol}: {exc} — trying yfinance fallback.")

        # Fallback: yfinance Ticker.history() with stderr suppressed
        if not closes:
            import io, sys, yfinance as yf
            _stderr = sys.stderr
            try:
                sys.stderr = io.StringIO()
                hist = yf.Ticker(symbol).history(period="30d", interval="1d")
                if not hist.empty:
                    closes = hist["Close"].dropna().tolist()
            except Exception:
                pass
            finally:
                sys.stderr = _stderr

        if not closes:
            self.append_log(f"[Buylist] No price data for {symbol} — skipping this cycle.")
            return

        self.latest_intraday_prices[symbol] = closes[-1]
        item._ema10 = self._compute_ema(closes, 10)
        item._ema20 = self._compute_ema(closes, 20)
    @staticmethod
    def _compute_ema(prices: list, period: int) -> float:
        """Compute exponential moving average."""
        if len(prices) < period:
            return 0.0
        k = 2.0 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = price * k + ema * (1.0 - k)
        return float(ema)

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Buylist Dashboard — KIS order submission
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _first_account_no_for_environment(self, environment: str) -> Optional[str]:
        try:
            from src.api.kis_account_snapshot_dual import discover_account_profiles

            profiles = [p for p in discover_account_profiles() if p.get("environment") == environment]
            if not profiles:
                return None
            return profiles[0].get("account_no") or None
        except Exception as exc:
            self.append_log(f"KIS account discovery failed for {environment}: {exc}")
            return None
    @staticmethod
    def _sell_intent_for_reason(reason: str) -> OrderIntent:
        reason_text = (reason or "").lower()
        if "stop" in reason_text:
            return OrderIntent.STOP_LOSS
        if "partial" in reason_text or "half" in reason_text:
            return OrderIntent.PARTIAL_EXIT
        if "manual" in reason_text or "all" in reason_text or "exit" in reason_text:
            return OrderIntent.MANUAL_EXIT
        return OrderIntent.UNKNOWN
    def _has_duplicate_open_order(
        self,
        environment: str,
        account_no: str,
        symbol: str,
        side: OrderSide,
        intent: OrderIntent,
    ) -> bool:
        load_fn = _main_window_global("load_order_ledger", load_order_ledger)
        has_open_fn = _main_window_global("has_open_order", has_open_order)
        self.order_ledger = load_fn()
        return has_open_fn(
            environment=environment,
            account_no=account_no or "",
            symbol=symbol,
            side=side,
            intent=intent,
        )
    def _save_buylist_state(self) -> None:
        save_state = getattr(self, "_save_state", None)
        if callable(save_state):
            save_state()
            self._save_execution_queue_state()
            return

        manager_save = getattr(getattr(self, "buylist_manager", None), "save", None)
        if callable(manager_save):
            manager_save()
        self._save_execution_queue_state()
    def _buylist_order_price(self, item, *fallbacks) -> float:
        live_price = getattr(item, "current_price", None)
        if not live_price:
            live_price = getattr(self, "latest_intraday_prices", {}).get(getattr(item, "symbol", ""), 0.0)
        for value in (live_price, *fallbacks, getattr(item, "stop_loss", 0.0), getattr(item, "avg_cost", 0.0), getattr(item, "entry_price", 0.0)):
            try:
                price = float(value or 0.0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return max(0.01, price)
        return 0.01
    @staticmethod
    def _buylist_order_environment(item) -> str:
        return str(getattr(item, "environment", None) or getattr(item, "market", None) or "REAL").upper()
    def _buylist_order_quantity(self, item, order_price: float, quantity: Optional[int] = None) -> int:
        try:
            qty = int(quantity or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            return qty

        account_size = self._get_account_balance_for_env(self._buylist_order_environment(item)) if hasattr(self, "_get_account_balance_for_env") else 0.0
        position_percent = float(getattr(item, "position_percent", 0.0) or 0.0)
        if account_size > 0 and position_percent > 0 and order_price > 0:
            return max(1, int((account_size * position_percent / 100.0) // order_price))
        return max(1, int(getattr(item, "shares_held", 0) or 1))
    def _submit_kis_buy_order(self, item, quantity: Optional[int] = None, limit_price: Optional[float] = None, order_price: Optional[float] = None) -> None:
        """Submit a KIS buy order without treating broker acceptance as a fill."""
        env = self._buylist_order_environment(item)
        account_no = self._first_account_no_for_environment(env) or ""
        intent = OrderIntent.ENTRY
        if self._is_pre_entry_execution_queue_buylist_item(item):
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None:
                manager = self._ensure_execution_queue_manager()
            queue_item = self._execution_queue_item_for_buylist_item(item) if manager is not None else None
            candidate = getattr(queue_item, "selected_candidate", None) if queue_item is not None else None
            queue_status = self._execution_queue_value(getattr(queue_item, "status", "")) if queue_item is not None else ""
            if candidate is not None and queue_status == "EXECUTE_READY":
                if quantity is None:
                    quantity = int(getattr(candidate, "shares", 0) or 0)
                if order_price is None and limit_price is None:
                    order_price = float(getattr(candidate, "entry_trigger", 0.0) or 0.0)
        if self._has_duplicate_open_order(env, account_no, item.symbol, OrderSide.BUY, intent):
            item._buy_order_pending = False
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None and self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()
            if manager is not None:
                manager.mark_order_failed(item.symbol, order_status="DUPLICATE", environment=env)
                queue_item = self._execution_queue_item_for_buylist_item(item)
                if queue_item is not None:
                    item.monitoring_status = self._execution_queue_value(queue_item.status)
                    item.status = item.monitoring_status
                self._save_execution_queue_state()
            self.append_log(
                f"Open BUY ENTRY order already exists for {item.symbol} in {env} account {account_no}. "
                "Reconcile or cancel it before submitting another order."
            )
            return

        explicit_price = None
        for value in (order_price, limit_price):
            try:
                price = float(value or 0.0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                explicit_price = price
                break
        order_price = max(0.01, explicit_price) if explicit_price is not None else self._buylist_order_price(item)
        quantity = self._buylist_order_quantity(item, order_price, quantity)
        try:
            self.kis_order_worker = KisOrderWorker(
                env,
                item.symbol,
                quantity,
                order_price,
                "buy",
                account_no=account_no,
                intent=intent,
                buylist_symbol_key=f"{env}:{item.symbol}",
            )
            self.kis_order_worker.finished_order.connect(
                lambda order, it=item: self._on_buy_order_accepted(it, order)
            )
            self.kis_order_worker.error_occurred.connect(
                lambda error, it=item: self._on_order_error(it.symbol, "buy", error, it)
            )
            self.kis_order_worker.start()
            self.append_log(
                f"BUY submitted for {item.symbol}: {quantity} shares @ limit ${order_price:.2f}"
            )
        except Exception as exc:
            item._buy_order_pending = False
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None and self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()
            if manager is not None:
                manager.mark_order_failed(item.symbol, order_status="ERROR", environment=env)
                queue_item = self._execution_queue_item_for_buylist_item(item)
                if queue_item is not None:
                    item.monitoring_status = self._execution_queue_value(queue_item.status)
                    item.status = item.monitoring_status
                self._save_execution_queue_state()
            if manager is None:
                item.monitoring_status = "ERROR"
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            QMessageBox.warning(self, "KIS order failed", str(exc))
    def _submit_kis_sell_order(self, item, quantity: int, reason: str) -> None:
        """Submit a KIS sell order without reducing local position until fill confirmation."""
        env = self._buylist_order_environment(item)
        account_no = self._first_account_no_for_environment(env) or ""
        intent = self._sell_intent_for_reason(reason)
        if self._has_duplicate_open_order(env, account_no, item.symbol, OrderSide.SELL, intent):
            item._stop_order_pending = False
            self.append_log(
                f"Open SELL {intent.value} order already exists for {item.symbol} in {env} account {account_no}. "
                "Reconcile or cancel it before submitting another order."
            )
            return

        order_price = self._buylist_order_price(item)
        try:
            self.kis_order_worker = KisOrderWorker(
                env,
                item.symbol,
                quantity,
                order_price,
                "sell",
                account_no=account_no,
                intent=intent,
                buylist_symbol_key=f"{env}:{item.symbol}",
            )
            self.kis_order_worker.finished_order.connect(
                lambda order, it=item, rsn=reason: self._on_sell_order_accepted(it, quantity, rsn, order)
            )
            self.kis_order_worker.error_occurred.connect(
                lambda error, it=item: self._on_order_error(it.symbol, "sell", error, it)
            )
            self.kis_order_worker.start()
            self.append_log(
                f"SELL submitted for {item.symbol}: {quantity} shares @ limit ${order_price:.2f} ({reason})"
            )
        except Exception as exc:
            item._stop_order_pending = False
            item.monitoring_status = "ERROR"
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            QMessageBox.warning(self, "KIS order failed", str(exc))
    def _record_broker_order(self, order: BrokerOrder) -> None:
        append_fn = _main_window_global("append_order", append_order)
        load_fn = _main_window_global("load_order_ledger", load_order_ledger)
        append_fn(order)
        self.order_ledger = load_fn()
    def _on_buy_order_accepted(self, item, order: BrokerOrder) -> None:
        item._buy_order_pending = False
        self._record_broker_order(order)
        manager = self.__dict__.get("execution_queue_manager")
        queue_item = self._execution_queue_item_for_buylist_item(item) if manager is not None else None
        env = self._buylist_order_environment(item)

        if order.status == OrderStatus.REJECTED:
            if manager is not None:
                manager.mark_order_failed(item.symbol, order_status="REJECTED", environment=env)
                queue_item = self._execution_queue_item_for_buylist_item(item)
            queue_status = self._execution_queue_status_for_buylist_item(item)
            block_reason = ""
            if self._is_kis_sim_unsupported_order_error(order.error_message):
                block_reason = "KIS SIM rejected overseas order routing for this account/API (90000000)."
                self._set_buylist_auto_order_block(item, block_reason)
                item.monitoring_status = queue_status or ("WATCHING" if self._is_orb_buylist_item(item) else "ACTIVE")
            else:
                item.monitoring_status = queue_status or "ERROR"
            item.status = item.monitoring_status
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"BUY rejected for {item.symbol}: {order.error_message or 'broker rejected order'} "
                f"(status restored to {item.monitoring_status})"
            )
            if block_reason:
                self.append_log(f"[Buylist/{item.environment}] Auto KIS order retries blocked for {item.symbol}: {block_reason}")
            QMessageBox.warning(
                self,
                "KIS order rejected",
                f"{item.symbol} buy order was rejected.\n\n{order.error_message or 'No broker error message provided.'}",
            )
            return

        if manager is not None and queue_item is not None:
            manager.mark_order_submitted(
                item.symbol,
                order_id=order.broker_order_id or order.client_order_id,
                order_status=self._execution_queue_value(order.status).upper() or "SUBMITTED",
                environment=env,
            )
            item.monitoring_status = self._execution_queue_status_for_buylist_item(item) or "ORDER_SUBMITTED"
        else:
            item.monitoring_status = "BUY_SUBMITTED"
        item.status = item.monitoring_status
        item.kis_order_id = order.broker_order_id or order.client_order_id
        self._clear_buylist_auto_order_block(item)
        self._save_buylist_state()
        self.populate_buylist_dashboard()
        self.append_log(
            f"BUY order accepted by broker for {item.symbol}: {order.quantity} shares; waiting for fill confirmation"
        )
        timer = _main_window_global("QTimer", QTimer)
        timer.singleShot(5000, self.reconcile_open_orders)
    def _on_sell_order_accepted(self, item, quantity: int, reason: str, order: BrokerOrder) -> None:
        item._stop_order_pending = False
        self._record_broker_order(order)

        if order.status == OrderStatus.REJECTED:
            try:
                shares_held = int(getattr(item, "shares_held", 0) or 0)
            except (TypeError, ValueError):
                shares_held = 0
            block_reason = ""
            if self._is_kis_sim_unsupported_order_error(order.error_message):
                block_reason = "KIS SIM rejected overseas order routing for this account/API (90000000)."
                self._set_buylist_auto_order_block(item, block_reason)
            item.monitoring_status = "BOUGHT" if shares_held > 0 else "WATCHING"
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            self.append_log(
                f"SELL rejected for {item.symbol}: {order.error_message or 'broker rejected order'} "
                f"(status restored to {item.monitoring_status})"
            )
            if block_reason:
                self.append_log(f"[Buylist/{item.environment}] Auto KIS order retries blocked for {item.symbol}: {block_reason}")
            QMessageBox.warning(
                self,
                "KIS order rejected",
                f"{item.symbol} sell order was rejected.\n\n{order.error_message or 'No broker error message provided.'}",
            )
            return

        if order.intent in {OrderIntent.PARTIAL_EXIT, OrderIntent.PARTIAL_TAKE_PROFIT}:
            item.monitoring_status = "PARTIAL_EXIT_SUBMITTED"
        else:
            item.monitoring_status = "SELL_SUBMITTED"
        item.kis_order_id = order.broker_order_id or order.client_order_id
        self._clear_buylist_auto_order_block(item)
        self._save_buylist_state()
        self.populate_buylist_dashboard()
        self.append_log(
            f"SELL order accepted by broker for {item.symbol}: {quantity} shares ({reason}); waiting for fill confirmation"
        )
        timer = _main_window_global("QTimer", QTimer)
        timer.singleShot(5000, self.reconcile_open_orders)
    def _on_buy_order_filled(self, item, quantity: int, order_price: float, result: dict) -> None:
        """Backward-compatible slot: broker acceptance is no longer treated as filled."""
        env = self._buylist_order_environment(item)
        account_no = self._first_account_no_for_environment(env) or ""
        order = BrokerOrder.create(
            environment=env,
            account_no=account_no,
            symbol=item.symbol,
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity_requested=quantity,
            limit_price=order_price,
            buylist_symbol_key=f"{env}:{item.symbol}",
        )
        order.status = OrderStatus.ACCEPTED
        order.raw_submit_response = result or {}
        output = (result or {}).get("output") if isinstance(result, dict) else None
        if isinstance(output, dict):
            order.broker_order_id = str(output.get("ODNO") or output.get("odno") or "")
        self._on_buy_order_accepted(item, order)
    def _on_sell_order_filled(self, item, quantity: int, reason: str, result: dict) -> None:
        """Backward-compatible slot: broker acceptance is no longer treated as filled."""
        env = self._buylist_order_environment(item)
        account_no = self._first_account_no_for_environment(env) or ""
        order_price = self._buylist_order_price(item)
        order = BrokerOrder.create(
            environment=env,
            account_no=account_no,
            symbol=item.symbol,
            side=OrderSide.SELL,
            intent=self._sell_intent_for_reason(reason),
            quantity_requested=quantity,
            limit_price=order_price,
            buylist_symbol_key=f"{env}:{item.symbol}",
        )
        order.status = OrderStatus.ACCEPTED
        order.raw_submit_response = result or {}
        output = (result or {}).get("output") if isinstance(result, dict) else None
        if isinstance(output, dict):
            order.broker_order_id = str(output.get("ODNO") or output.get("odno") or "")
        self._on_sell_order_accepted(item, quantity, reason, order)
    def reconcile_open_orders(self) -> None:
        if self.order_reconciliation_worker and self.order_reconciliation_worker.isRunning():
            return

        load_fn = _main_window_global("load_order_ledger", load_order_ledger)
        find_fn = _main_window_global("find_open_orders", find_open_orders)
        self.order_ledger = load_fn()
        open_orders = find_fn(self.order_ledger)
        if not open_orders:
            self._pending_reconciliation_groups = []
            return

        grouped: Dict[Tuple[str, str], List[BrokerOrder]] = {}
        for order in open_orders:
            grouped.setdefault((order.environment, order.account_no), []).append(order)

        if not self._pending_reconciliation_groups:
            self._pending_reconciliation_groups = sorted(grouped.keys())

        environment = ""
        account_no = ""
        while self._pending_reconciliation_groups:
            environment, account_no = self._pending_reconciliation_groups.pop(0)
            if (environment, account_no) in grouped:
                break
        else:
            return

        previous_snapshot = self.kis_account_snapshots.get((environment, account_no), {})
        self.order_reconciliation_worker = OrderReconciliationWorker(
            environment,
            account_no,
            grouped[(environment, account_no)],
            previous_snapshot=previous_snapshot,
        )
        self.order_reconciliation_worker.finished_reconciliation.connect(
            lambda orders, snapshot, env=environment, acct=account_no: self._on_order_reconciliation_finished(env, acct, orders, snapshot)
        )
        self.order_reconciliation_worker.error_occurred.connect(
            lambda message: self.append_log(f"Order reconciliation failed: {message}")
        )
        self.order_reconciliation_worker.finished.connect(
            lambda: setattr(self, "order_reconciliation_worker", None)
        )
        self.order_reconciliation_worker.start()
        self.append_log(
            f"Reconciling {len(grouped[(environment, account_no)])} open broker order(s) for {environment} {account_no or '<unknown account>'}"
        )
    def _on_order_reconciliation_finished(
        self,
        environment: str,
        account_no: str,
        updated_orders: List[BrokerOrder],
        snapshot: dict,
    ) -> None:
        self.kis_account_snapshots[(environment, account_no)] = snapshot or {}
        load_fn = _main_window_global("load_order_ledger", load_order_ledger)
        save_fn = _main_window_global("save_order_ledger", save_order_ledger)
        latest_orders = load_fn()
        by_id = {order.client_order_id: order for order in latest_orders}
        for order in updated_orders:
            by_id[order.client_order_id] = order
        save_fn(list(by_id.values()))
        self.order_ledger = load_fn()
        self.apply_confirmed_order_fills_to_buylist(updated_orders)
        self.sync_buylist_positions_from_kis_snapshots({(environment, account_no): snapshot})

        if self._pending_reconciliation_groups:
            timer = _main_window_global("QTimer", QTimer)
            timer.singleShot(1000, self.reconcile_open_orders)
    @staticmethod
    def _buylist_to_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
    @classmethod
    def _buylist_snapshot_holdings(cls, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        holdings: List[Dict[str, Any]] = []
        if not isinstance(snapshot, dict):
            return holdings
        for section_name in ("domestic", "overseas"):
            section = snapshot.get(section_name)
            if isinstance(section, dict):
                holdings.extend(item for item in section.get("holdings", []) if isinstance(item, dict))
        return holdings
    def sync_buylist_positions_from_kis_snapshots(self, snapshots: Optional[Dict[Any, dict]] = None) -> int:
        """Sync held buylist positions to real KIS account holdings when snapshots are available."""
        from src.ui.controllers.account_controller import AccountController
        from src.ui.controllers.base import get_controller

        controller = get_controller(self, "account_controller", AccountController)
        return controller.sync_positions_from_kis(snapshots)

    def sync_positions_from_kis(self, snapshots: Optional[Dict[Any, dict]] = None) -> int:
        from src.ui.controllers.account_controller import AccountController
        from src.ui.controllers.base import get_controller

        controller = get_controller(self, "account_controller", AccountController)
        return controller.sync_positions_from_kis(snapshots)
    def apply_confirmed_order_fills_to_buylist(self, updated_orders: List[BrokerOrder]) -> None:
        changed = False
        for order in updated_orders:
            if order.status not in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
                continue
            try:
                item = self.buylist_manager.get(order.symbol, order.environment)
            except TypeError:
                item = self.buylist_manager.get(order.symbol)
            if not item:
                continue

            filled_qty = max(0, int(order.filled_quantity or 0))
            applied_qty = max(0, int(getattr(order, "applied_filled_quantity", 0) or 0))
            newly_filled_qty = max(0, filled_qty - applied_qty)
            if filled_qty <= 0 or newly_filled_qty <= 0:
                continue

            if order.side == OrderSide.BUY:
                manager = self.__dict__.get("execution_queue_manager")
                if manager is None and self._is_execution_queue_buylist_item(item):
                    manager = self._ensure_execution_queue_manager()
                if manager is not None:
                    manager.mark_order_filled(
                        order.symbol,
                        order_id=order.broker_order_id or order.client_order_id,
                        order_status=self._execution_queue_value(order.status).upper(),
                        environment=str(getattr(item, "environment", "") or getattr(order, "environment", "") or "SIM").upper(),
                    )
                    queue_status = self._execution_queue_status_for_buylist_item(item)
                    if queue_status:
                        item.status = queue_status
                item.shares_held = filled_qty
                if order.avg_fill_price:
                    item.avg_cost = float(order.avg_fill_price)
                if not getattr(item, "buy_date", None):
                    item.buy_date = dt.datetime.now()
                item.kis_order_id = order.broker_order_id or order.client_order_id
                item.monitoring_status = "BOUGHT" if order.status == OrderStatus.FILLED else "BUY_PARTIAL"
                if item.avg_cost and item.shares_held:
                    item.position_percent = 100.0
                self.append_log(
                    f"BUY fill confirmed for {order.symbol}: {filled_qty}/{order.quantity} shares"
                )
                order.applied_filled_quantity = filled_qty
                update_fn = _main_window_global("update_order", update_order)
                update_fn(order)
                changed = True
                continue

            previous_shares = max(0, int(getattr(item, "shares_held", 0) or 0))
            remaining_shares = max(0, previous_shares - newly_filled_qty)
            item.shares_held = remaining_shares
            item.kis_order_id = order.broker_order_id or order.client_order_id
            if order.intent in {OrderIntent.PARTIAL_EXIT, OrderIntent.PARTIAL_TAKE_PROFIT}:
                item.sell_half_done = True
                if getattr(item, "avg_cost", 0):
                    item.stop_loss = max(float(item.stop_loss or 0), float(item.avg_cost))
            if remaining_shares <= 0 and order.status == OrderStatus.FILLED:
                item.monitoring_status = "SOLD"
            else:
                item.monitoring_status = "BOUGHT"
            self.append_log(
                f"SELL fill confirmed for {order.symbol}: {filled_qty}/{order.quantity} shares; {remaining_shares} remaining"
            )
            order.applied_filled_quantity = filled_qty
            update_fn = _main_window_global("update_order", update_order)
            update_fn(order)
            changed = True

        if changed:
            self._save_buylist_state()
            self.populate_buylist_dashboard()
            load_fn = _main_window_global("load_order_ledger", load_order_ledger)
            self.order_ledger = load_fn()
    def request_cancel_order(self, client_order_id: str) -> bool:
        load_fn = _main_window_global("load_order_ledger", load_order_ledger)
        update_fn = _main_window_global("update_order", update_order)
        self.order_ledger = load_fn()
        target = next((order for order in self.order_ledger if order.client_order_id == client_order_id), None)
        if target is None:
            self.append_log(f"Cancel request skipped: order {client_order_id} not found")
            return False
        if target.status not in OPEN_ORDER_STATUSES:
            self.append_log(f"Cancel request skipped: order {client_order_id} is already {target.status.value}")
            return False
        target.status = OrderStatus.CANCEL_REQUESTED
        target.touch()
        update_fn(target)
        self.order_ledger = load_fn()
        self.append_log(
            f"Cancel requested for {target.symbol} {target.side.value} order {client_order_id}; direct KIS cancel endpoint is not implemented yet"
        )
        return True
    def _on_order_error(self, symbol: str, side: str, error: str, item=None) -> None:
        # Clear pending flags so the next monitor cycle can retry
        if item is not None:
            item._buy_order_pending = False
            item._stop_order_pending = False
            manager = self.__dict__.get("execution_queue_manager")
            if manager is None and self._is_execution_queue_buylist_item(item):
                manager = self._ensure_execution_queue_manager()
            if manager is not None and str(side).lower() == "buy":
                manager.mark_order_failed(
                    symbol,
                    order_status="ERROR",
                    environment=str(getattr(item, "environment", "") or "SIM").upper(),
                )
                queue_item = self._execution_queue_item_for_buylist_item(item)
                if queue_item is not None:
                    item.monitoring_status = self._execution_queue_value(queue_item.status)
                    item.status = item.monitoring_status
                self._save_execution_queue_state()
            self._save_buylist_state()
            self.populate_buylist_dashboard()
        self.append_log(f"[Buylist] KIS {side.upper()} order FAILED for {symbol}: {error}")
        QMessageBox.warning(self, f"Order Failed — {symbol}", f"{side.upper()} order error:\n{error}")
    def _cleanup_order_worker(self, worker: QThread) -> None:
        if worker in self._buylist_order_workers:
            self._buylist_order_workers.remove(worker)
