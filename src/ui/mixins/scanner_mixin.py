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



class ScannerMixin:
    def _build_scanner_tab(self) -> None:
        """Build content for the scanner tab."""
        layout = QHBoxLayout()

        form_group = QGroupBox("Scanner Filters")
        form_layout = QFormLayout()
        self.scanner_setup_combo = QComboBox()
        self.scanner_setup_combo.currentTextChanged.connect(self.apply_selected_scanner_setup)
        self.scanner_setup_name_input = QLineEdit()
        self.save_scanner_setup_button = QPushButton("Save / Update Setup")
        self.save_scanner_setup_button.clicked.connect(self.save_current_scanner_setup)
        self.delete_scanner_setup_button = QPushButton("Delete Setup")
        self.delete_scanner_setup_button.setObjectName("deleteSetupButton")
        self.delete_scanner_setup_button.clicked.connect(self.delete_current_scanner_setup)

        setup_button_layout = QHBoxLayout()
        setup_button_layout.addWidget(self.save_scanner_setup_button)
        setup_button_layout.addWidget(self.delete_scanner_setup_button)

        form_layout.addRow("Setup", self.scanner_setup_combo)
        form_layout.addRow("Setup Name", self.scanner_setup_name_input)
        form_layout.addRow(setup_button_layout)

        # Rules Panel (Non-scrollable, fits naturally)
        self.active_rule_widgets = []
        
        from PyQt5.QtWidgets import QFrame
        self.rules_container = QFrame()
        self.rules_container.setFrameShape(QFrame.StyledPanel)
        self.rules_container.setStyleSheet("""
            QFrame {
                border: 1px solid #e0e3eb;
                border-radius: 6px;
                background-color: #ffffff;
            }
        """)
        
        self.rules_scroll_layout = QVBoxLayout()
        self.rules_scroll_layout.setContentsMargins(6, 6, 6, 6)
        self.rules_scroll_layout.setSpacing(6)
        
        # Add a stretch at the bottom to push items up
        self.rules_scroll_layout.addStretch()
        self.rules_container.setLayout(self.rules_scroll_layout)
        
        # Header label on top
        active_rules_label = QLabel("Active Filter Rules")
        active_rules_label.setStyleSheet("font-weight: bold; color: #131722; font-size: 14px; margin-top: 8px; margin-bottom: 4px;")
        form_layout.addRow(active_rules_label)
        form_layout.addRow(self.rules_container)
        
        self.add_rule_button = QPushButton("＋ Add Filter Rule")
        self.add_rule_button.setObjectName("addRuleButton")
        self.add_rule_button.clicked.connect(self.show_add_rule_menu)
        form_layout.addRow(self.add_rule_button)
        
        self.populate_scanner_setup_combo()

        self.scanner_orb_score_checkbox = QCheckBox("Score by ORB recommendation")
        self.scanner_orb_score_checkbox.setChecked(True)
        form_layout.addRow(self.scanner_orb_score_checkbox)
 
        run_button = QPushButton("Run Scanner")
        run_button.setObjectName("runScannerButton")
        run_button.clicked.connect(self.run_scanner)
        form_layout.addRow(run_button)
 
        self.scanner_selection_label = QLabel("Selected symbol: None")
        form_layout.addRow(self.scanner_selection_label)
 
        add_watchlist_button = QPushButton("Add selected to Watchlist")
        add_watchlist_button.setObjectName("addWatchlistButton")
        add_watchlist_button.clicked.connect(self.add_selected_scanner_to_watchlist)
        form_layout.addRow(add_watchlist_button)
 
        self.scanner_metrics_details = QTextBrowser()
        self.scanner_metrics_details.setMinimumHeight(200)
        self.scanner_metrics_details.setMaximumHeight(280)
        self.scanner_metrics_details.setReadOnly(True)
        self.scanner_metrics_details.setHtml("<i>Select a symbol to view detailed computed metrics.</i>")
        form_layout.addRow("Metrics Details", self.scanner_metrics_details)

 
        form_group.setLayout(form_layout)
        layout.addWidget(form_group, 1)

        table_layout = QVBoxLayout()
        self.scanner_table = QTableWidget(0, 9)
        self.scanner_table.setHorizontalHeaderLabels([
            "Symbol",
            "Name",
            "Price",
            "Volume",
            "Dollar Vol",
            "ADR",
            "Growth Rank",
            "Trend Intensity",
            "ORB Score",
        ])
        self.scanner_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.scanner_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scanner_table.cellClicked.connect(self.on_scanner_row_selected)
        self.scanner_table.cellDoubleClicked.connect(self.load_scanner_item_to_trade_plan)
        self.scanner_table.itemSelectionChanged.connect(self.on_scanner_selection_changed)
        table_layout.addWidget(self.scanner_table)
        layout.addLayout(table_layout, 3)

        self.scanner_widget.setLayout(layout)
    def populate_scanner_setup_combo(self, selected_name: Optional[str] = None) -> None:
        """Refresh scanner setup selector."""
        if not hasattr(self, "scanner_setup_combo"):
            return

        if selected_name is None:
            selected_name = self.scanner_setup_combo.currentText() or next(iter(self.scanner_setups), "Setup 1")

        self.scanner_setup_combo.blockSignals(True)
        self.scanner_setup_combo.clear()
        self.scanner_setup_combo.addItems(sorted(self.scanner_setups.keys()))
        index = self.scanner_setup_combo.findText(selected_name)
        self.scanner_setup_combo.setCurrentIndex(index if index >= 0 else 0)
        self.scanner_setup_combo.blockSignals(False)
        self.apply_selected_scanner_setup(self.scanner_setup_combo.currentText())
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(selected_source={"type": "scan", "setup": self.scanner_setup_combo.currentText()})
    def show_add_rule_menu(self) -> None:
        """Show dialog table of available filter metrics to select and add (TradingView style)."""
        existing_attrs = {entry[0] for entry in self.active_rule_widgets}
        
        dialog = AddFilterDialog(self, disabled_attributes=existing_attrs)
        if dialog.exec_() == QDialog.Accepted:
            attr_key = dialog.selected_attribute
            if attr_key:
                self.add_scanner_rule_row(attribute=attr_key)
    def add_scanner_rule_row(self, attribute: str = "volume", operator: str = ">=", threshold: str = "") -> None:
        """Add a new rule row in the rules panel with a fixed attribute label."""
        row_widget = QWidget()
        row_widget.setObjectName("ruleRow")
        
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(10, 2, 10, 2)
        row_layout.setSpacing(6)
        
        # Fix attribute to a static label
        label_text = SCANNER_METRICS_LABELS.get(attribute, attribute)
        attr_label = QLabel(label_text)
        attr_label.setObjectName("attrLabel")
        
        op_combo = QComboBox()
        op_combo.setObjectName("opCombo")
        op_combo.addItems([">=", "<=", ">", "<", "==", "!="])
        op_combo.setCurrentText(operator)
        
        val_input = QLineEdit(str(threshold))
        val_input.setObjectName("valInput")
        val_input.setPlaceholderText("Value")
        
        del_btn = QPushButton("✕")
        del_btn.setToolTip("Delete this rule")
        del_btn.setObjectName("delBtn")
        
        row_layout.addWidget(attr_label, 3)
        row_layout.addWidget(op_combo, 1)
        row_layout.addWidget(val_input, 2)
        row_layout.addWidget(del_btn, 0)
        
        row_widget.setLayout(row_layout)
        
        # Style row_widget as a TradingView-style pill
        row_widget.setStyleSheet("""
            QWidget#ruleRow {
                background-color: #f1f3f6;
                border: 1px solid #d1d4dc;
                border-radius: 12px;
            }
            QLabel#attrLabel {
                border: none;
                background-color: transparent;
                color: #131722;
                font-size: 12px;
                font-weight: bold;
                padding: 1px 4px;
            }
            QComboBox {
                border: none;
                background-color: transparent;
                padding: 1px 4px;
                color: #131722;
                font-size: 12px;
                font-weight: bold;
            }
            QComboBox::drop-down {
                border: none;
                width: 12px;
            }
            QLineEdit {
                border: none;
                background-color: transparent;
                padding: 1px 4px;
                color: #2962ff;
                font-size: 12px;
                font-weight: bold;
                border-bottom: 1px dashed #787b86;
            }
            QLineEdit:focus {
                border-bottom: 1px solid #2962ff;
            }
            QPushButton#delBtn {
                border: none;
                background-color: transparent;
                color: #787b86;
                font-weight: bold;
                font-size: 12px;
                padding: 2px;
            }
            QPushButton#delBtn:hover {
                color: #f23645;
                background-color: #e0e3eb;
                border-radius: 8px;
            }
        """)
        
        self.rules_scroll_layout.insertWidget(self.rules_scroll_layout.count() - 1, row_widget)
        
        entry = (attribute, op_combo, val_input, del_btn, row_widget)
        self.active_rule_widgets.append(entry)
        
        del_btn.clicked.connect(lambda: self.remove_scanner_rule_row(entry))
    def remove_scanner_rule_row(self, entry: tuple) -> None:
        """Remove a rule row from the rules layout."""
        attr_key, op_combo, val_input, del_btn, row_widget = entry
        row_widget.deleteLater()
        if entry in self.active_rule_widgets:
            self.active_rule_widgets.remove(entry)
    def clear_scanner_rules(self) -> None:
        """Clear all scanner rules from the UI."""
        for entry in list(self.active_rule_widgets):
            self.remove_scanner_rule_row(entry)
    def load_scanner_rules(self, rules: list) -> None:
        """Load list of rules into the UI scroll area."""
        self.clear_scanner_rules()
        for rule in rules:
            self.add_scanner_rule_row(
                attribute=rule.get("attribute", "volume"),
                operator=rule.get("operator", ">="),
                threshold=rule.get("threshold", ""),
            )
    def get_current_scanner_rules_from_ui(self) -> list:
        """Extract rule dicts from active UI widgets."""
        rules = []
        for attr_key, op_combo, val_input, _, _ in self.active_rule_widgets:
            op = op_combo.currentText()
            val = val_input.text().strip()
            if attr_key:
                # If value is empty and it's a boolean field, default to True
                if not val and attr_key in ("above_sma_20", "above_ema_50", "ma_alignment", "breakout_20d", "breakout_50d", "parabolic_flag", "rs_above_sma_50"):
                    val = "True"
                rules.append({
                    "attribute": attr_key,
                    "operator": op,
                    "threshold": val
                })
        return rules
    def update_scanner_metrics_details(self, symbol: str) -> None:
        """Populate the metrics details browser with formatted values for a symbol."""
        stock = self._get_scanner_stock(symbol)
        if not stock:
            self.scanner_metrics_details.setText("No details available.")
            return
            
        lines = []
        lines.append(f"<b>--- {stock['symbol']} Metrics Summary ---</b><br>")
        
        lines.append("<b>Basic:</b>")
        lines.append(f"  Price: ${stock.get('price', 0.0):.2f} | Volume: {stock.get('volume', 0.0):,.0f} | 20d Avg Vol: {stock.get('avg_volume_20d', 0.0):,.0f}")
        lines.append(f"  Dollar Vol: ${stock.get('dollar_volume', 0.0):,.0f} | 20d Avg Dollar Vol: ${stock.get('avg_dollar_volume_20d', 0.0):,.0f}")
        
        lines.append("<br><b>Returns / Growth:</b>")
        lines.append(f"  1W: {stock.get('return_1w', 0.0):+.2f}% | 1M: {stock.get('return_1m', 0.0):+.2f}% (Rank: {stock.get('growth_rank_1m', 0.0):.1f})")
        lines.append(f"  3M: {stock.get('return_3m', 0.0):+.2f}% (Rank: {stock.get('growth_rank_3m', 0.0):.1f}) | 6M: {stock.get('return_6m', 0.0):+.2f}%")
        
        lines.append("<br><b>Trend / Moving Averages:</b>")
        lines.append(f"  SMA 20: ${stock.get('sma_20', 0.0):.2f} | EMA 50: ${stock.get('ema_50', 0.0):.2f} | SMA 200: ${stock.get('sma_200', 0.0):.2f}")
        alignment = "Bullish Alignment (20 > 50 > 200)" if stock.get("ma_alignment") else "No Alignment"
        lines.append(f"  MA Alignment: {alignment}")
        lines.append(f"  Dist from SMA20: {stock.get('distance_from_20ma_pct', 0.0):+.2f}% | Dist from EMA50: {stock.get('distance_from_50ema_pct', 0.0):+.2f}%")
        lines.append(f"  Trend Intensity: {stock.get('trend_intensity', 0.0):.1f} | Trend Score: {stock.get('trend_score', 0.0):.1f}")
        
        lines.append("<br><b>Breakout / Consolidation:</b>")
        lines.append(f"  Consolidation Range (10d): {stock.get('consolidation_range_10d_pct', 0.0):.2f}% | Tightness: {stock.get('consolidation_tightness', 0.0):.1f}")
        lines.append(f"  Pullback from 50d High: {stock.get('pullback_depth_pct', 0.0):.2f}% | Dist from 52w High: {stock.get('close_to_52w_high_pct', 0.0):.2f}%")
        bo20 = "YES" if stock.get("breakout_20d") else "NO"
        bo50 = "YES" if stock.get("breakout_50d") else "NO"
        lines.append(f"  Breakout 20d: {bo20} | Breakout 50d: {bo50}")
        
        lines.append("<br><b>Relative Strength:</b>")
        lines.append(f"  RS Score: {stock.get('rs_score_252', 0.0):.1f} | RS Above SMA50: {'YES' if stock.get('rs_above_sma_50') else 'NO'} | RS Slope (20d): {stock.get('rs_slope_20d', 0.0):+.2f}%")
        
        self.scanner_metrics_details.setHtml("<br>".join(lines))
    def apply_selected_scanner_setup(self, setup_name: str) -> None:
        """Apply selected scanner setup values to filter inputs."""
        setup = self.scanner_setups.get(setup_name)
        if not setup:
            return

        self.scanner_setup_name_input.setText(setup_name)
        
        # Load rules
        rules = setup.get("rules")
        if not rules:
            # Generate from basic fields for backward compatibility
            rules = [
                {"attribute": "volume", "operator": ">=", "threshold": setup.get("min_volume", 40000.0)},
                {"attribute": "dollar_volume", "operator": ">=", "threshold": setup.get("min_dollar_volume", 35000.0)},
                {"attribute": "adr_20", "operator": ">=", "threshold": setup.get("min_adr", 2.4)},
                {"attribute": "growth_rank_1m", "operator": ">=", "threshold": setup.get("min_growth_rank", 97.04)},
                {"attribute": "trend_intensity", "operator": ">=", "threshold": setup.get("min_trend_intensity", 90.0)},
            ]
        self.load_scanner_rules(rules)
        
        if hasattr(self, "scanner_table"):
            self.scanner_results = list(self.scanner_results_by_setup.get(setup_name, []))
            self.scanner_dataframe = pd.DataFrame(self.scanner_results)
            self.populate_scanner_table()
            
        if hasattr(self, "scanner_metrics_details"):
            self.scanner_metrics_details.setHtml("<i>Select a symbol to view detailed computed metrics.</i>")
    def get_current_scanner_setup_values(self) -> dict:
        """Read scanner setup values from filter inputs."""
        rules = self.get_current_scanner_rules_from_ui()
        min_volume = 40000.0
        min_dollar_volume = 35000.0
        min_adr = 2.4
        min_growth_rank = 97.04
        min_trend_intensity = 90.0
        
        for r in rules:
            try:
                if r["attribute"] == "volume":
                    min_volume = float(r["threshold"]) if r["threshold"] else min_volume
                elif r["attribute"] == "dollar_volume":
                    min_dollar_volume = float(r["threshold"]) if r["threshold"] else min_dollar_volume
                elif r["attribute"] in ("adr", "adr_20"):
                    min_adr = float(r["threshold"]) if r["threshold"] else min_adr
                elif r["attribute"] in ("growth_rank", "growth_rank_1m"):
                    min_growth_rank = float(r["threshold"]) if r["threshold"] else min_growth_rank
                elif r["attribute"] == "trend_intensity":
                    min_trend_intensity = float(r["threshold"]) if r["threshold"] else min_trend_intensity
            except (ValueError, TypeError):
                pass
                
        return {
            "min_volume": min_volume,
            "min_dollar_volume": min_dollar_volume,
            "min_adr": min_adr,
            "min_growth_rank": min_growth_rank,
            "min_trend_intensity": min_trend_intensity,
            "rules": rules,
        }
    def save_current_scanner_setup(self) -> None:
        """Save or update the scanner setup from current filter values."""
        setup_name = self.scanner_setup_name_input.text().strip()
        if not setup_name:
            QMessageBox.warning(self, "Invalid setup", "Enter a setup name before saving.")
            return

        self.scanner_setups[setup_name] = self.get_current_scanner_setup_values()
        save_json(SCANNER_SETUPS_FILE, {"setups": self.scanner_setups})
        self.populate_scanner_setup_combo(selected_name=setup_name)
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(selected_source={"type": "scan", "setup": setup_name})
        self.append_log(f"Saved scanner setup: {setup_name}.")
    def delete_current_scanner_setup(self) -> None:
        """Delete the selected scanner setup."""
        setup_name = self.scanner_setup_combo.currentText()
        if not setup_name:
            return
        if len(self.scanner_setups) <= 1:
            QMessageBox.warning(self, "Cannot delete", "At least one scanner setup must remain.")
            return

        del self.scanner_setups[setup_name]
        self.scanner_results_by_setup.pop(setup_name, None)
        save_json(SCANNER_SETUPS_FILE, {"setups": self.scanner_setups})
        self.populate_scanner_setup_combo()
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources()
        self.append_log(f"Deleted scanner setup: {setup_name}.")
    def _scanner_is_running(self) -> bool:
        return hasattr(self, "scanner_worker") and self.scanner_worker is not None and self.scanner_worker.isRunning()
    def _scanner_orb_scoring_enabled(self) -> bool:
        return bool(hasattr(self, "scanner_orb_score_checkbox") and self.scanner_orb_score_checkbox.isChecked())
    def _prepare_scanner_run(self, show_warnings: bool = True) -> bool:
        """Validate that a database scanner run can start."""
        if self._scanner_is_running():
            if show_warnings:
                QMessageBox.information(
                    self,
                    "Scanner Running",
                    "A scanner run is already in progress. Please wait for it to complete."
                )
            return False

        if not self.db_enabled or self.db_engine is None:
            message = "MySQL cache is not configured or cannot be reached. Use Update 1D Data after configuring the database."
            self.append_log(f"Scanner blocked: {message}")
            if show_warnings:
                QMessageBox.warning(self, "Database unavailable", message)
            return False

        return True
    def _start_scanner_worker(self) -> None:
        """Start the worker that loads scanner metrics from MySQL."""
        self.universe_tickers = get_default_universe(max_symbols=self.universe_limit)
        self.progress_label.setText("Scanning MySQL cache...")
        self.progress_bar.setValue(0)

        self.scanner_worker = ScannerWorker(
            tickers=self.universe_tickers,
            engine=self.db_engine,
            min_volume=0,
            min_dollar_volume=0,
            min_adr=0,
            min_growth_rank=0,
            min_trend_intensity=0,
        )
        self.scanner_worker.log_message.connect(self.append_log)
        self.scanner_worker.finished_scan.connect(self._on_scanner_finished)
        self.scanner_worker.error_occurred.connect(self._on_scanner_error)
        self.scanner_worker.finished.connect(
            lambda worker=self.scanner_worker: self._clear_worker_reference("scanner_worker", worker)
        )
        self.scanner_worker.start()
    def run_all_scanners(self, checked: bool = False, show_warnings: bool = True) -> None:
        """Run all configured scanner setups against the MySQL cache."""
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.scanner_controller import ScannerController

        controller = get_controller(self, "scanner_controller", ScannerController)
        controller.run_all_scanners(checked=checked, show_warnings=show_warnings)

    def run_scanner(self, checked: bool = False, show_warnings: bool = True) -> None:
        """Start the selected database-backed scanner asynchronously."""
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.scanner_controller import ScannerController

        controller = get_controller(self, "scanner_controller", ScannerController)
        controller.run_scanner(checked=checked, show_warnings=show_warnings)
    def _scan_metrics_for_setup(self, setup_name: str, stock_metrics: list) -> List[dict]:
        """Apply a named scanner setup to raw stock metrics."""
        setup = self.scanner_setups.get(setup_name, self.get_current_scanner_setup_values())
        scanner = StockScanner()

        # Load rules from setup
        rules = setup.get("rules")
        if not rules:
            # Generate from basic fields for backward compatibility
            rules = [
                {"attribute": "volume", "operator": ">=", "threshold": setup.get("min_volume", 40000.0)},
                {"attribute": "dollar_volume", "operator": ">=", "threshold": setup.get("min_dollar_volume", 35000.0)},
                {"attribute": "adr_20", "operator": ">=", "threshold": setup.get("min_adr", 2.4)},
                {"attribute": "growth_rank_1m", "operator": ">=", "threshold": setup.get("min_growth_rank", 97.04)},
                {"attribute": "trend_intensity", "operator": ">=", "threshold": setup.get("min_trend_intensity", 90.0)},
            ]

        op_map = {
            ">": ComparisonOperator.GREATER_THAN,
            "<": ComparisonOperator.LESS_THAN,
            "==": ComparisonOperator.EQUAL,
            ">=": ComparisonOperator.GREATER_EQUAL,
            "<=": ComparisonOperator.LESS_EQUAL,
            "!=": ComparisonOperator.NOT_EQUAL,
        }

        # Always add a rule to require at least 1 day of price history
        scanner.add_rule(ScanRule(
            name="price_history_days",
            attribute="price_history_days",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=1.0
        ))

        for r in rules:
            attr = r.get("attribute")
            if not attr:
                continue
            op_str = r.get("operator", ">=")
            op = op_map.get(op_str, ComparisonOperator.GREATER_EQUAL)
            val_str = str(r.get("threshold", ""))

            # Parse threshold value to appropriate type
            if val_str.lower() in ("true", "yes"):
                threshold = True
            elif val_str.lower() in ("false", "no"):
                threshold = False
            else:
                try:
                    threshold = float(val_str)
                except ValueError:
                    threshold = val_str

            scanner.add_rule(ScanRule(
                name=attr,
                attribute=attr,
                operator=op,
                threshold=threshold
            ))

        return scanner.scan_with_scoring(
            stock_metrics,
            scorers=[
                self._score_growth_rank,
                self._score_trend_intensity,
                self._score_adr,
            ],
        )
    def _start_scanner_orb_score_phase(self, selected_source: dict) -> None:
        symbols = sorted({
            stock.get("symbol", "").strip().upper()
            for results in self.scanner_results_by_setup.values()
            for stock in results
            if stock.get("symbol")
        })
        if not symbols:
            self._finish_scanner_after_orb_phase(selected_source)
            return
        if self.intraday_bulk_worker is not None and self.intraday_bulk_worker.isRunning():
            self.append_log("Scanner ORB phase skipped fetch because intraday refresh is already running; scoring cached data.")
            self._score_scanner_results_by_orb()
            self._finish_scanner_after_orb_phase(selected_source)
            return

        self.pending_scanner_orb_source = selected_source
        self.intraday_bulk_purpose = "scanner_orb"
        engine = self.db_engine if self.db_enabled else None
        self.progress_label.setText(f"Scanner ORB phase: fetching {len(symbols)} candidate symbols...")
        self.append_log(f"Scanner ORB phase: fetching provider intraday data for {len(symbols)} candidates.")
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
    def _finish_scanner_after_orb_phase(self, selected_source: Optional[dict]) -> None:
        active_setup = self.scanner_setup_combo.currentText()
        self.scanner_results = list(self.scanner_results_by_setup.get(active_setup, self.scanner_results))
        self.scanner_dataframe = pd.DataFrame(self.scanner_results)
        self.populate_scanner_table()
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(selected_source=selected_source or {"type": "scan", "setup": active_setup})
        self.update_dashboard_summary()
        self.progress_label.setText("Scanner complete.")
        self.progress_bar.setValue(100)
        self.running_scanner_setup_name = None
        self.running_scanner_show_warnings = True
    def _score_scanner_results_by_orb(self) -> None:
        for setup_name, results in list(self.scanner_results_by_setup.items()):
            enriched = []
            for stock in results:
                stock_copy = dict(stock)
                stock_copy.update(self._calculate_best_orb_scan_score(stock_copy))
                enriched.append(stock_copy)
            enriched.sort(key=lambda item: item.get("orb_score", -1.0), reverse=True)
            self.scanner_results_by_setup[setup_name] = enriched
    def _calculate_best_orb_scan_score(self, stock: dict) -> dict:
        symbol = str(stock.get("symbol", "")).strip().upper()
        if not symbol:
            return {"orb_score": 0.0, "orb_plan": ""}
        account_size = self._parse_float(self.account_size_input, 0.0) if hasattr(self, "account_size_input") else 0.0
        selected_risk_percent = self._parse_float(self.risk_percent_input, 0.0) / 100.0 if hasattr(self, "risk_percent_input") else 0.0
        risk_cases = self._orb_risk_cases(selected_risk_percent)
        adr_percent = float(stock.get("adr") or 0.0)
        five_minute = self._latest_intraday_session(self._load_cached_intraday_interval(symbol, "5m", window_days=7))
        one_minute = self._latest_intraday_session(self._load_cached_intraday_interval(symbol, "1m", window_days=7))
        best: Optional[dict] = None
        for risk_percent in risk_cases:
            for window, history in [("1m", one_minute), ("5m", five_minute), ("30m", five_minute)]:
                orb_range = calculate_orb_range(symbol, history, window)
                if orb_range is None:
                    continue
                sizing = self._calculate_orb_position_values(
                    account_size=account_size,
                    risk_percent=risk_percent,
                    entry_price=float(orb_range.high),
                    stop_price=float(orb_range.low),
                    adr_percent=adr_percent,
                )
                if not self._orb_position_plan_is_valid(sizing, adr_percent):
                    continue
                recommendation_score = self._score_orb_position_recommendation(sizing, risk_percent)
                candidate = {
                    "orb_score": recommendation_score,
                    "orb_plan": f"{risk_percent * 100:.2f}% {window}",
                    "orb_entry": float(orb_range.high),
                    "orb_stop": float(orb_range.low),
                    "orb_shares": sizing["shares"],
                    "risk_percent": risk_percent,
                }
                if best is None or (candidate["orb_score"], -risk_percent) > (best["orb_score"], -best["risk_percent"]):
                    best = candidate
        if best is None:
            return {"orb_score": 0.0, "orb_plan": ""}
        best.pop("risk_percent", None)
        return best
    def _show_scanner_results_for_setup(self, setup_name: str) -> None:
        """Display cached results for a scanner setup in the Scanner tab table."""
        self.scanner_results = list(self.scanner_results_by_setup.get(setup_name, []))
        self.scanner_dataframe = pd.DataFrame(self.scanner_results)
        self.populate_scanner_table()
    def _on_scanner_finished(self, stock_metrics: list, _: object) -> None:
        setup_name = self.running_scanner_setup_name or self.scanner_setup_combo.currentText()
        if not stock_metrics:
            self.scanner_results = []
            if setup_name == "__ALL__":
                for name in self.scanner_setups:
                    self.scanner_results_by_setup[name] = []
            else:
                self.scanner_results_by_setup[setup_name] = []
            self.scanner_dataframe = pd.DataFrame()
            self.populate_scanner_table()
            self.update_dashboard_summary()
            self.append_log("Scanner completed: no cached database rows found.")
            if self.running_scanner_show_warnings:
                QMessageBox.warning(
                    self,
                    "Scanner Empty",
                    "No cached database data was found for the universe. Run Update 1D Data first, then scan again."
            )
            self.progress_label.setText("Scanner complete.")
            self.running_scanner_setup_name = None
            return

        if setup_name == "__ALL__":
            for name in self.scanner_setups:
                results = self._scan_metrics_for_setup(name, stock_metrics)
                self.scanner_results_by_setup[name] = list(results)
                self.append_log(f"Scanner completed for {name}: {len(results)} symbols found.")
            active_setup_name = self.scanner_setup_combo.currentText()
            self.scanner_results = list(self.scanner_results_by_setup.get(active_setup_name, []))
            selected_source = {"type": "scan", "setup": active_setup_name}
        else:
            self.scanner_results = self._scan_metrics_for_setup(setup_name, stock_metrics)
            self.scanner_results_by_setup[setup_name] = list(self.scanner_results)
            if self.scanner_results:
                self.append_log(f"Scanner completed for {setup_name}: {len(self.scanner_results)} symbols found.")
            else:
                self.append_log(f"Scanner completed for {setup_name}: no symbols passed filters.")
            selected_source = {"type": "scan", "setup": setup_name}

        self.scanner_dataframe = pd.DataFrame(self.scanner_results)

        self.populate_scanner_table()
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(selected_source=selected_source)
        self.update_dashboard_summary()
        if self._scanner_orb_scoring_enabled() and self.running_scanner_show_warnings and self.scanner_results:
            self._start_scanner_orb_score_phase(selected_source=selected_source)
            return
        self.progress_label.setText("Scanner complete.")
        self.progress_bar.setValue(100)
        self.running_scanner_setup_name = None
        self.running_scanner_show_warnings = True
    def _on_scanner_error(self, error_message: str) -> None:
        self.append_log(f"Scanner error: {error_message}")
        QMessageBox.warning(self, "Scanner failed", error_message)
        self.progress_label.setText("Scanner failed.")
        self.running_scanner_setup_name = None
        self.running_scanner_show_warnings = True
    def _is_refresh_worker_running(self) -> bool:
        return self.refresh_worker is not None and self.refresh_worker.isRunning()
    def _is_hourly_refresh_worker_running(self) -> bool:
        return getattr(self, "hourly_refresh_worker", None) is not None and self.hourly_refresh_worker.isRunning()
    def _is_daily_update_complete(self) -> bool:
        return bool(getattr(self.refresh_worker, "daily_update_complete", False))
    def _sync_refresh_button_states(self) -> None:
        daily_running = self._is_refresh_worker_running()
        hourly_running = self._is_hourly_refresh_worker_running()
        if hasattr(self, "refresh_db_button"):
            self.refresh_db_button.setEnabled(not daily_running and not hourly_running)
        if hasattr(self, "refresh_hourly_button"):
            self.refresh_hourly_button.setEnabled(not hourly_running and (not daily_running or self._is_daily_update_complete()))
    def _on_refresh_worker_finished(self, worker) -> None:
        self._clear_worker_reference("refresh_worker", worker)
        self._sync_refresh_button_states()
    def _on_hourly_refresh_worker_finished(self, worker) -> None:
        self._clear_worker_reference("hourly_refresh_worker", worker)
        self._sync_refresh_button_states()
    def refresh_data_to_db(self) -> None:
        """Refresh KIS-registered US universe history from yfinance into MySQL cache."""
        if not self.db_enabled:
            QMessageBox.warning(
                self,
                "Database unavailable",
                "MySQL cache is not configured or cannot be reached."
            )
            return

        if self.refresh_worker is not None and self.refresh_worker.isRunning():
            QMessageBox.information(
                self,
                "Refresh in progress",
                "A MySQL refresh is already running. Please wait for it to finish."
            )
            return

        self.universe_tickers = get_default_universe(max_symbols=self.universe_limit, refresh=True)
        self.append_log(
            f"Loaded {len(self.universe_tickers)} KIS-registered US symbols. "
            "Starting 1D data update for MySQL cache, indicators, and scanner metrics..."
        )
        self.progress_bar.setValue(0)
        self.progress_label.setText("Starting refresh...")
        self._sync_refresh_button_states()

        refresh_tickers = list(dict.fromkeys([REFERENCE_SYMBOL, *self.universe_tickers]))
        self.refresh_worker = RefreshWorker(
            refresh_tickers,
            engine=self.db_engine,
            period="1y",
            interval="1d",
        )
        self.refresh_worker.log_message.connect(self.append_log)
        self.refresh_worker.progress_changed.connect(self.update_progress)
        self.refresh_worker.daily_data_finished.connect(self._on_daily_data_update_finished)
        self.refresh_worker.finished_refresh.connect(self._on_refresh_finished)
        self.refresh_worker.error_occurred.connect(self._on_refresh_error)
        self.refresh_worker.finished.connect(
            lambda worker=self.refresh_worker: self._on_refresh_worker_finished(worker)
        )
        self.refresh_worker.start()
        self._sync_refresh_button_states()
    def _on_daily_data_update_finished(self, updated) -> None:
        if hasattr(self, "refresh_hourly_button"):
            self.refresh_hourly_button.setEnabled(not self._is_hourly_refresh_worker_running())
        self.progress_label.setText("1D update complete. Calculating indicators and scanner metrics...")
        self.append_log(f"1D data update complete. Updated {len(updated)} symbols.")
    def _on_refresh_finished(self, updated):
        self.show_refresh_complete(len(updated))
        self.update_dashboard_summary(force=True)
        self._sync_refresh_button_states()
    def _on_refresh_error(self, error_message: str) -> None:
        self.show_refresh_error(error_message)
        QMessageBox.warning(self, "Refresh failed", error_message)
        self._sync_refresh_button_states()
    def refresh_hourly_data_to_db(self) -> None:
        """Refresh only the historical 1-hour chart cache for KIS-registered US symbols."""
        if not self.db_enabled:
            QMessageBox.warning(
                self,
                "Database unavailable",
                "MySQL cache is not configured or cannot be reached."
            )
            return

        if self.refresh_worker is not None and self.refresh_worker.isRunning() and not self._is_daily_update_complete():
            QMessageBox.information(
                self,
                "Refresh in progress",
                "A 1D data update is still running. Please wait for the 1D download phase to finish."
            )
            return
        if getattr(self, "hourly_refresh_worker", None) is not None and self.hourly_refresh_worker.isRunning():
            QMessageBox.information(
                self,
                "1H refresh in progress",
                "A 1-hour data refresh is already running. Please wait for it to finish."
            )
            return

        self.universe_tickers = get_default_universe(max_symbols=self.universe_limit, refresh=True)
        refresh_tickers = list(dict.fromkeys([REFERENCE_SYMBOL, *self.universe_tickers]))
        latest_hourly = get_latest_hourly_price_history_timestamp(self.db_engine)
        latest_text = pd.Timestamp(latest_hourly).strftime("%Y-%m-%d %H:%M UTC") if latest_hourly else "none"
        self.append_log(
            f"Loaded {len(self.universe_tickers)} KIS-registered US symbols. "
            f"Starting 1H data refresh. Latest cached 1H timestamp: {latest_text}."
        )
        self.progress_bar.setValue(0)
        self.progress_label.setText("Starting 1H refresh...")

        self.hourly_refresh_worker = HourlyRefreshWorker(
            refresh_tickers,
            engine=self.db_engine,
            full_period="730d",
        )
        self.hourly_refresh_worker.log_message.connect(self.append_log)
        self.hourly_refresh_worker.progress_changed.connect(self.update_progress)
        self.hourly_refresh_worker.finished_refresh.connect(self._on_hourly_refresh_finished)
        self.hourly_refresh_worker.error_occurred.connect(self._on_hourly_refresh_error)
        self.hourly_refresh_worker.finished.connect(
            lambda worker=self.hourly_refresh_worker: self._on_hourly_refresh_worker_finished(worker)
        )
        self.hourly_refresh_worker.start()
        self._sync_refresh_button_states()
    def _on_hourly_refresh_finished(self, updated) -> None:
        latest_hourly = get_latest_hourly_price_history_timestamp(self.db_engine)
        latest_text = pd.Timestamp(latest_hourly).strftime("%Y-%m-%d %H:%M UTC") if latest_hourly else "none"
        self.append_log(f"1H data refresh complete. Updated {len(updated)} symbols. Latest 1H timestamp: {latest_text}.")
        self.progress_label.setText("1H refresh complete.")
        self.progress_bar.setValue(100)
        self.update_dashboard_summary(force=True)
        self._sync_refresh_button_states()
    def _on_hourly_refresh_error(self, error_message: str) -> None:
        self.progress_label.setText("1H refresh failed.")
        self.append_log(f"1H data refresh failed: {error_message}")
        QMessageBox.warning(self, "1H refresh failed", error_message)
        self._sync_refresh_button_states()
    def populate_scanner_table(self) -> None:
        """Populate the scanner table with the latest scan results."""
        self.scanner_table.setRowCount(0)
        for stock in self.scanner_results:
            row = self.scanner_table.rowCount()
            self.scanner_table.insertRow(row)
            self.scanner_table.setItem(row, 0, QTableWidgetItem(stock["symbol"]))
            self.scanner_table.setItem(row, 1, QTableWidgetItem(stock.get("name", stock["symbol"])))
            self.scanner_table.setItem(row, 2, QTableWidgetItem(f"{stock['price']:.2f}"))
            self.scanner_table.setItem(row, 3, QTableWidgetItem(str(stock["volume"])))
            self.scanner_table.setItem(row, 4, QTableWidgetItem(f"{stock['dollar_volume']:.0f}"))
            self.scanner_table.setItem(row, 5, QTableWidgetItem(f"{stock['adr']:.2f}%"))
            self.scanner_table.setItem(row, 6, QTableWidgetItem(f"{stock['growth_rank']:.2f}"))
            self.scanner_table.setItem(row, 7, QTableWidgetItem(f"{stock['trend_intensity']:.1f}"))
            orb_score = stock.get("orb_score")
            orb_plan = stock.get("orb_plan", "")
            orb_text = "" if orb_score is None else f"{float(orb_score):.1f} {orb_plan}".strip()
            self.scanner_table.setItem(row, 8, QTableWidgetItem(orb_text))
        if hasattr(self, "sidebar_source_combo"):
            source = self.sidebar_source_combo.currentData() or {}
            if source.get("type") == "scan" and source.get("setup") == self.scanner_setup_combo.currentText():
                self.refresh_stock_sidebar()
        self.populate_chart_symbol_combo()
    def on_scanner_row_selected(self, row: int, column: int) -> None:
        """Handle scanner row selection."""
        self._select_scanner_row(row)
    def on_scanner_selection_changed(self) -> None:
        """Handle keyboard or mouse scanner row selection changes."""
        selected_items = self.scanner_table.selectedItems()
        if not selected_items:
            return
        self._select_scanner_row(selected_items[0].row())
    def _select_scanner_row(self, row: int) -> None:
        """Update app state from the selected scanner row."""
        symbol_item = self.scanner_table.item(row, 0)
        if not symbol_item:
            return
        self.selected_scan_symbol = symbol_item.text()
        self.scanner_selection_label.setText(f"Selected symbol: {self.selected_scan_symbol}")
        self._set_chart_symbol(self.selected_scan_symbol)
        self.update_scanner_metrics_details(self.selected_scan_symbol)
    def update_scanner_preview_chart(self, symbol: str) -> None:
        pass
    def _get_scanner_stock(self, symbol: str) -> Optional[dict]:
        return next((item for item in self.scanner_results if item["symbol"] == symbol), None)
    def load_scanner_item_to_trade_plan(self, row: int, column: int) -> None:
        """Scanner double-click: select symbol on chart and refresh ORB panel."""
        symbol_item = self.scanner_table.item(row, 0)
        if symbol_item is None:
            return

        stock = self._get_scanner_stock(symbol_item.text())
        if stock is None:
            return

        symbol = stock["symbol"]
        self._set_chart_symbol(symbol)
        self.refresh_watchlist_orb_panel(symbol)
    def add_selected_scanner_to_watchlist(self) -> None:
        """Add the selected scanner entry to the watchlist."""
        if not self.selected_scan_symbol:
            QMessageBox.warning(self, "No selection", "Please select a stock from the scanner results first.")
            return

        stock = self._get_scanner_stock(self.selected_scan_symbol)
        if stock is None:
            QMessageBox.warning(self, "Not found", "Selected stock is no longer available in scanner results.")
            return

        self.watchlist.add(symbol=stock["symbol"], name=stock["name"], entry_price=stock["price"])
        self.populate_watchlist_table()
        self.update_dashboard_summary()
        self._save_state()
        self.prefetch_intraday_cache_for_symbol(stock["symbol"])
        self.append_log(f"Added {stock['symbol']} to watchlist.")
