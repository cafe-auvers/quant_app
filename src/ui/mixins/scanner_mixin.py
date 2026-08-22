from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtCore import Qt, QThread, QTimer, QUrl
from PyQt5.QtGui import QColor, QKeySequence
from PyQt5.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDockWidget,
                             QFormLayout, QGroupBox, QHBoxLayout,
                             QKeySequenceEdit, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMenu, QMessageBox, QProgressBar,
                             QPushButton, QScrollArea, QShortcut, QSizePolicy,
                             QSlider, QSpinBox, QSplitter, QTabWidget,
                             QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.api.kis_account_snapshot_dual import (KisEnvironment,
                                               discover_account_profiles,
                                               load_config)
from src.core.order_state import (OPEN_ORDER_STATUSES, BrokerOrder,
                                  OrderIntent, OrderSide, OrderStatus)
from src.core.scanner import ComparisonOperator, ScanRule, StockScanner
from src.core.watchlist import (BuylistItem, BuylistManager, TradePlan,
                                TradePlanManager, Watchlist)
from src.services.app_state import (SCANNER_SETUPS_FILE, SETTINGS_FILE,
                                    load_buylist_state,
                                    load_chart_drawings_state,
                                    load_scanner_setups_state,
                                    load_tab_options_state,
                                    load_trade_plans_state,
                                    load_watchlist_state, save_app_state)
from src.services.historical_refresh_control import (MODE_1D, MODE_1H,
                                                     is_refresh_running,
                                                     launch_refresh,
                                                     read_status,
                                                     reconcile_stale_status,
                                                     terminate_refresh)
from src.services.intraday_data_service import (format_intraday_source_label,
                                                load_best_intraday_history)
from src.services.order_ledger import (append_order, find_open_orders,
                                       has_open_order, load_order_ledger,
                                       save_order_ledger, update_order)
from src.ui.chart_bridge import ChartBridge
from src.ui.dialogs import AddFilterDialog, SettingsDialog
from src.ui.filter_catalog import (DEFAULT_SCANNER_SETUPS, DEFAULT_SETTINGS,
                                   DEFAULT_TAB_OPTIONS, FILTER_CATALOG,
                                   SCANNER_METRICS_LABELS)
from src.ui.workers import (FxRateWorker, IntradayFetchWorker, KisAccountWorker,
                            KisOrderWorker, KisStartupAccountsWorker,
                            OrderReconciliationWorker, ScannerWorker)
from src.utils.data_loader import (_extract_symbol_history,
                                   download_price_history)
from src.utils.intraday_helpers import \
    extract_latest_opening_bar as _extract_latest_opening_bar
from src.utils.intraday_helpers import intraday_cache_needs_backfill
from src.utils.intraday_helpers import utcnow_naive as _utcnow_naive
from src.utils.storage import load_json, save_json

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
REFRESH_SUCCESS_DISPLAY_MS = 5_000
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class ScannerMixin:
    def _build_scanner_tab(self) -> None:
        """Build content for the scanner tab."""
        layout = QHBoxLayout()

        form_group = QGroupBox("Scanner Filters")
        form_layout = QFormLayout()
        self.scanner_setup_combo = QComboBox()
        self.scanner_setup_combo.currentTextChanged.connect(
            self.apply_selected_scanner_setup
        )
        self.scanner_setup_name_input = QLineEdit()
        self.save_scanner_setup_button = QPushButton("Save / Update Setup")
        self.save_scanner_setup_button.clicked.connect(self.save_current_scanner_setup)
        self.delete_scanner_setup_button = QPushButton("Delete Setup")
        self.delete_scanner_setup_button.setObjectName("deleteSetupButton")
        self.delete_scanner_setup_button.clicked.connect(
            self.delete_current_scanner_setup
        )

        setup_button_layout = QHBoxLayout()
        setup_button_layout.addWidget(self.save_scanner_setup_button)
        setup_button_layout.addWidget(self.delete_scanner_setup_button)

        form_layout.addRow("Setup", self.scanner_setup_combo)
        form_layout.addRow("Setup Name", self.scanner_setup_name_input)
        form_layout.addRow(setup_button_layout)

        # Rules Panel (Non-scrollable, fits naturally)
        self.active_rule_widgets = []
        self.active_rule_count_labels = []
        self._loading_scanner_rules = False

        from PyQt5.QtWidgets import QFrame

        self.rules_container = QFrame()
        self.rules_container.setFrameShape(QFrame.StyledPanel)
        self.rules_container.setStyleSheet(
            """
            QFrame {
                border: 1px solid #e0e3eb;
                border-radius: 6px;
                background-color: #ffffff;
            }
        """
        )

        self.rules_scroll_layout = QVBoxLayout()
        self.rules_scroll_layout.setContentsMargins(6, 6, 6, 6)
        self.rules_scroll_layout.setSpacing(6)

        # Add a stretch at the bottom to push items up
        self.rules_scroll_layout.addStretch()
        self.rules_container.setLayout(self.rules_scroll_layout)

        # Header row: universe count followed by the active filter title.
        rules_header_layout = QHBoxLayout()
        self.scanner_universe_count_label = QLabel("Universe: —")
        self.scanner_universe_count_label.setObjectName("scannerUniverseCount")
        self.scanner_universe_count_label.setMinimumWidth(110)
        self.scanner_universe_count_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.scanner_universe_count_label.setStyleSheet(
            "font-weight: bold; color: #2962ff; font-size: 13px; "
            "margin-top: 8px; margin-bottom: 4px;"
        )
        active_rules_label = QLabel("Active Filter Rules")
        active_rules_label.setStyleSheet(
            "font-weight: bold; color: #131722; font-size: 14px; margin-top: 8px; margin-bottom: 4px;"
        )
        rules_header_layout.addWidget(self.scanner_universe_count_label)
        rules_header_layout.addWidget(active_rules_label)
        rules_header_layout.addStretch()
        form_layout.addRow(rules_header_layout)
        form_layout.addRow(self.rules_container)

        self.add_rule_button = QPushButton("＋ Add Filter Rule")
        self.add_rule_button.setObjectName("addRuleButton")
        self.add_rule_button.clicked.connect(self.show_add_rule_menu)
        form_layout.addRow(self.add_rule_button)

        self._scanner_live_refresh_timer = QTimer(self.scanner_widget)
        self._scanner_live_refresh_timer.setSingleShot(True)
        self._scanner_live_refresh_timer.setInterval(300)
        self._scanner_live_refresh_timer.timeout.connect(
            self._run_live_scanner_refresh
        )
        self._scanner_live_refresh_pending = False
        self._scanner_run_is_live = False

        self._scanner_live_refresh_enabled = False
        self.populate_scanner_setup_combo()
        self._scanner_live_refresh_enabled = True

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
        self.scanner_metrics_details.setHtml(
            "<i>Select a symbol to view detailed computed metrics.</i>"
        )
        form_layout.addRow("Metrics Details", self.scanner_metrics_details)

        form_group.setLayout(form_layout)
        layout.addWidget(form_group)

        self.scanner_widget.setLayout(layout)

    def populate_scanner_setup_combo(self, selected_name: Optional[str] = None) -> None:
        """Refresh scanner setup selector."""
        if not hasattr(self, "scanner_setup_combo"):
            return

        if selected_name is None:
            selected_name = self.scanner_setup_combo.currentText() or next(
                iter(self.scanner_setups), "Setup 1"
            )

        self.scanner_setup_combo.blockSignals(True)
        self.scanner_setup_combo.clear()
        self.scanner_setup_combo.addItems(sorted(self.scanner_setups.keys()))
        index = self.scanner_setup_combo.findText(selected_name)
        self.scanner_setup_combo.setCurrentIndex(index if index >= 0 else 0)
        self.scanner_setup_combo.blockSignals(False)
        self.apply_selected_scanner_setup(self.scanner_setup_combo.currentText())
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(
                selected_source={
                    "type": "scan",
                    "setup": self.scanner_setup_combo.currentText(),
                }
            )

    def show_add_rule_menu(self) -> None:
        """Show dialog table of available filter metrics to select and add (TradingView style)."""
        existing_attrs = {entry[0] for entry in self.active_rule_widgets}

        dialog = AddFilterDialog(self, disabled_attributes=existing_attrs)
        if dialog.exec_() == QDialog.Accepted:
            attr_key = dialog.selected_attribute
            if attr_key:
                self.add_scanner_rule_row(attribute=attr_key)

    def add_scanner_rule_row(
        self, attribute: str = "volume", operator: str = ">=", threshold: str = ""
    ) -> None:
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

        count_label = QLabel("—")
        count_label.setObjectName("funnelCountLabel")
        count_label.setMinimumWidth(70)
        count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        count_label.setToolTip("Symbols remaining after this filter")

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

        row_layout.addWidget(count_label, 0)
        row_layout.addWidget(attr_label, 3)
        row_layout.addWidget(op_combo, 1)
        row_layout.addWidget(val_input, 2)
        row_layout.addWidget(del_btn, 0)

        row_widget.setLayout(row_layout)

        # Style row_widget as a TradingView-style pill
        row_widget.setStyleSheet(
            """
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
            QLabel#funnelCountLabel {
                border: none;
                background-color: transparent;
                color: #2962ff;
                font-size: 12px;
                font-weight: bold;
                padding: 1px 6px 1px 0;
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
        """
        )

        self.rules_scroll_layout.insertWidget(
            self.rules_scroll_layout.count() - 1, row_widget
        )

        entry = (attribute, op_combo, val_input, del_btn, row_widget)
        self.active_rule_widgets.append(entry)
        self.active_rule_count_labels.append(count_label)

        del_btn.clicked.connect(lambda: self.remove_scanner_rule_row(entry))
        op_combo.currentTextChanged.connect(self._invalidate_current_scanner_funnel)
        val_input.textChanged.connect(self._invalidate_current_scanner_funnel)
        self._invalidate_current_scanner_funnel()

    def remove_scanner_rule_row(self, entry: tuple) -> None:
        """Remove a rule row from the rules layout."""
        attr_key, op_combo, val_input, del_btn, row_widget = entry
        index = self.active_rule_widgets.index(entry) if entry in self.active_rule_widgets else -1
        row_widget.deleteLater()
        if entry in self.active_rule_widgets:
            self.active_rule_widgets.remove(entry)
        if index >= 0 and index < len(self.active_rule_count_labels):
            self.active_rule_count_labels.pop(index)
        self._invalidate_current_scanner_funnel()

    def clear_scanner_rules(self) -> None:
        """Clear all scanner rules from the UI."""
        for entry in list(self.active_rule_widgets):
            self.remove_scanner_rule_row(entry)

    def load_scanner_rules(self, rules: list) -> None:
        """Load list of rules into the UI scroll area."""
        self._loading_scanner_rules = True
        try:
            self.clear_scanner_rules()
            for rule in rules:
                self.add_scanner_rule_row(
                    attribute=rule.get("attribute", "volume"),
                    operator=rule.get("operator", ">="),
                    threshold=rule.get("threshold", ""),
                )
        finally:
            self._loading_scanner_rules = False

    def get_current_scanner_rules_from_ui(self) -> list:
        """Extract rule dicts from active UI widgets."""
        rules = []
        for attr_key, op_combo, val_input, _, _ in self.active_rule_widgets:
            op = op_combo.currentText()
            val = val_input.text().strip()
            if attr_key:
                # If value is empty and it's a boolean field, default to True
                if not val and attr_key in (
                    "above_sma_20",
                    "above_ema_50",
                    "ma_alignment",
                    "breakout_20d",
                    "breakout_50d",
                    "parabolic_flag",
                    "rs_above_sma_50",
                ):
                    val = "True"
                rules.append({"attribute": attr_key, "operator": op, "threshold": val})
        return rules

    def _invalidate_current_scanner_funnel(self, *_args) -> None:
        """Clear stale funnel values when the visible rule set changes."""
        if getattr(self, "_loading_scanner_rules", False):
            return
        setup_name = (
            self.scanner_setup_combo.currentText()
            if hasattr(self, "scanner_setup_combo")
            else ""
        )
        if setup_name:
            self.__dict__.setdefault("scanner_funnel_counts_by_setup", {}).pop(
                setup_name, None
            )
        self._display_scanner_funnel()
        self._schedule_live_scanner_refresh()

    def _schedule_live_scanner_refresh(self) -> None:
        """Debounce live database filtering while the user edits a rule."""
        timer = self.__dict__.get("_scanner_live_refresh_timer")
        if (
            timer is not None
            and getattr(self, "_scanner_live_refresh_enabled", False)
            and not getattr(self, "_loading_scanner_rules", False)
        ):
            timer.start()

    def _run_live_scanner_refresh(self) -> None:
        """Run the visible rules against the cached snapshot without dialogs."""
        if self._scanner_is_running():
            self._scanner_live_refresh_pending = True
            return
        if (
            getattr(self, "db_initializing", False)
            or not getattr(self, "db_enabled", False)
            or getattr(self, "db_engine", None) is None
        ):
            return

        self._scanner_live_refresh_pending = False
        self._scanner_run_is_live = True
        self.running_scanner_setup_name = self.scanner_setup_combo.currentText()
        self.running_scanner_show_warnings = False
        self._start_scanner_worker()

    def _start_pending_live_scanner_refresh(self) -> None:
        """Run the latest edit after an older scanner query leaves the worker."""
        if not getattr(self, "_scanner_live_refresh_pending", False):
            return
        self._scanner_live_refresh_pending = False
        timer = self.__dict__.get("_scanner_live_refresh_timer")
        if timer is not None:
            timer.start(150)

    def _display_scanner_funnel(self, setup_name: Optional[str] = None) -> None:
        """Show the universe and sequential remaining counts beside the rules."""
        if not hasattr(self, "scanner_universe_count_label"):
            return
        if setup_name is None and hasattr(self, "scanner_setup_combo"):
            setup_name = self.scanner_setup_combo.currentText()

        funnel = self.__dict__.get("scanner_funnel_counts_by_setup", {}).get(
            setup_name or "", {}
        )
        setup_combo = getattr(self, "scanner_setup_combo", None)
        if (
            funnel.get("rules") is not None
            and setup_combo is not None
            and setup_name == setup_combo.currentText()
            and self._scanner_rule_signature(funnel.get("rules") or [])
            != self._scanner_rule_signature(self.get_current_scanner_rules_from_ui())
        ):
            funnel = {}
        universe_count = funnel.get("universe_count")
        rule_counts = list(funnel.get("rule_counts") or [])
        if universe_count is None:
            self.scanner_universe_count_label.setText("Universe: —")
        else:
            self.scanner_universe_count_label.setText(
                f"Universe: {int(universe_count):,}"
            )

        previous_count = universe_count
        for index, label in enumerate(self.active_rule_count_labels):
            if index >= len(rule_counts):
                label.setText("—")
                label.setToolTip("Symbols remaining after this filter")
                continue
            count = int(rule_counts[index])
            label.setText(f"{count:,}")
            if previous_count is None:
                label.setToolTip(f"{count:,} symbols remain")
            else:
                removed = max(0, int(previous_count) - count)
                label.setToolTip(
                    f"{count:,} remain; {removed:,} filtered out by this rule"
                )
            previous_count = count

    def update_scanner_metrics_details(self, symbol: str) -> None:
        """Populate the metrics details browser with formatted values for a symbol."""
        stock = self._get_scanner_stock(symbol)
        if not stock:
            self.scanner_metrics_details.setText("No details available.")
            return

        lines = []
        lines.append(f"<b>--- {stock['symbol']} Metrics Summary ---</b><br>")

        lines.append("<b>Basic:</b>")
        lines.append(
            f"  Price: ${stock.get('price', 0.0):.2f} | Volume: {stock.get('volume', 0.0):,.0f} | 20d Avg Vol: {stock.get('avg_volume_20d', 0.0):,.0f}"
        )
        lines.append(
            f"  Dollar Vol: ${stock.get('dollar_volume', 0.0):,.0f} | 20d Avg Dollar Vol: ${stock.get('avg_dollar_volume_20d', 0.0):,.0f}"
        )

        lines.append("<br><b>Returns / Growth:</b>")
        lines.append(
            f"  1W: {stock.get('return_1w', 0.0):+.2f}% | 1M: {stock.get('return_1m', 0.0):+.2f}% (Rank: {stock.get('growth_rank_1m', 0.0):.1f})"
        )
        lines.append(
            f"  3M: {stock.get('return_3m', 0.0):+.2f}% (Rank: {stock.get('growth_rank_3m', 0.0):.1f}) | 6M: {stock.get('return_6m', 0.0):+.2f}%"
        )

        lines.append("<br><b>Trend / Moving Averages:</b>")
        lines.append(
            f"  SMA 20: ${stock.get('sma_20', 0.0):.2f} | EMA 50: ${stock.get('ema_50', 0.0):.2f} | SMA 200: ${stock.get('sma_200', 0.0):.2f}"
        )
        alignment = (
            "Bullish Alignment (20 > 50 > 200)"
            if stock.get("ma_alignment")
            else "No Alignment"
        )
        lines.append(f"  MA Alignment: {alignment}")
        lines.append(
            f"  Dist from SMA20: {stock.get('distance_from_20ma_pct', 0.0):+.2f}% | Dist from EMA50: {stock.get('distance_from_50ema_pct', 0.0):+.2f}%"
        )
        lines.append(
            f"  Trend Intensity: {stock.get('trend_intensity', 0.0):.1f} | Trend Score: {stock.get('trend_score', 0.0):.1f}"
        )

        lines.append("<br><b>Breakout / Consolidation:</b>")
        lines.append(
            f"  Consolidation Range (10d): {stock.get('consolidation_range_10d_pct', 0.0):.2f}% | Tightness: {stock.get('consolidation_tightness', 0.0):.1f}"
        )
        lines.append(
            f"  Pullback from 50d High: {stock.get('pullback_depth_pct', 0.0):.2f}% | Dist from 52w High: {stock.get('close_to_52w_high_pct', 0.0):.2f}%"
        )
        bo20 = "YES" if stock.get("breakout_20d") else "NO"
        bo50 = "YES" if stock.get("breakout_50d") else "NO"
        lines.append(f"  Breakout 20d: {bo20} | Breakout 50d: {bo50}")

        lines.append("<br><b>Relative Strength:</b>")
        lines.append(
            f"  RS Score: {stock.get('rs_score_252', 0.0):.1f} | RS Above SMA50: {'YES' if stock.get('rs_above_sma_50') else 'NO'} | RS Slope (20d): {stock.get('rs_slope_20d', 0.0):+.2f}%"
        )

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
                {
                    "attribute": "volume",
                    "operator": ">=",
                    "threshold": setup.get("min_volume", 40000.0),
                },
                {
                    "attribute": "dollar_volume",
                    "operator": ">=",
                    "threshold": setup.get("min_dollar_volume", 35000.0),
                },
                {
                    "attribute": "adr_20",
                    "operator": ">=",
                    "threshold": setup.get("min_adr", 2.4),
                },
                {
                    "attribute": "growth_rank_1m",
                    "operator": ">=",
                    "threshold": setup.get("min_growth_rank", 97.04),
                },
                {
                    "attribute": "trend_intensity",
                    "operator": ">=",
                    "threshold": setup.get("min_trend_intensity", 90.0),
                },
            ]
        self.load_scanner_rules(rules)

        self.scanner_results = list(
            self.scanner_results_by_setup.get(setup_name, [])
        )
        self.scanner_dataframe = pd.DataFrame(self.scanner_results)
        self.populate_scanner_table()
        self._display_scanner_funnel(setup_name)

        if hasattr(self, "scanner_metrics_details"):
            self.scanner_metrics_details.setHtml(
                "<i>Select a symbol to view detailed computed metrics.</i>"
            )
        self._schedule_live_scanner_refresh()

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
                    min_dollar_volume = (
                        float(r["threshold"]) if r["threshold"] else min_dollar_volume
                    )
                elif r["attribute"] in ("adr", "adr_20"):
                    min_adr = float(r["threshold"]) if r["threshold"] else min_adr
                elif r["attribute"] in ("growth_rank", "growth_rank_1m"):
                    min_growth_rank = (
                        float(r["threshold"]) if r["threshold"] else min_growth_rank
                    )
                elif r["attribute"] == "trend_intensity":
                    min_trend_intensity = (
                        float(r["threshold"]) if r["threshold"] else min_trend_intensity
                    )
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

    @staticmethod
    def _rules_for_scanner_setup(setup: dict) -> list:
        """Return explicit rules, including legacy threshold-only setups."""
        rules = list(setup.get("rules") or [])
        if rules:
            return rules
        return [
            {
                "attribute": "volume",
                "operator": ">=",
                "threshold": setup.get("min_volume", 40000.0),
            },
            {
                "attribute": "dollar_volume",
                "operator": ">=",
                "threshold": setup.get("min_dollar_volume", 35000.0),
            },
            {
                "attribute": "adr_20",
                "operator": ">=",
                "threshold": setup.get("min_adr", 2.4),
            },
            {
                "attribute": "growth_rank_1m",
                "operator": ">=",
                "threshold": setup.get("min_growth_rank", 97.04),
            },
            {
                "attribute": "trend_intensity",
                "operator": ">=",
                "threshold": setup.get("min_trend_intensity", 90.0),
            },
        ]

    @staticmethod
    def _scanner_rule_signature(rules: list) -> tuple:
        """Normalize rule values so UI strings and persisted numbers compare equally."""
        signature = []
        for rule in rules or []:
            threshold = rule.get("threshold", "")
            if isinstance(threshold, bool):
                threshold_text = "true" if threshold else "false"
            else:
                raw_text = str(threshold).strip()
                if raw_text.lower() in ("true", "yes"):
                    threshold_text = "true"
                elif raw_text.lower() in ("false", "no"):
                    threshold_text = "false"
                else:
                    try:
                        threshold_text = f"{float(raw_text):.15g}"
                    except (TypeError, ValueError):
                        threshold_text = raw_text
            signature.append(
                (
                    str(rule.get("attribute") or ""),
                    str(rule.get("operator") or ">="),
                    threshold_text,
                )
            )
        return tuple(signature)

    def save_current_scanner_setup(self) -> None:
        """Save or update the scanner setup from current filter values."""
        setup_name = self.scanner_setup_name_input.text().strip()
        if not setup_name:
            QMessageBox.warning(
                self, "Invalid setup", "Enter a setup name before saving."
            )
            return

        self.scanner_setups[setup_name] = self.get_current_scanner_setup_values()
        save_json(SCANNER_SETUPS_FILE, {"setups": self.scanner_setups})
        self.populate_scanner_setup_combo(selected_name=setup_name)
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(
                selected_source={"type": "scan", "setup": setup_name}
            )
        self.append_log(f"Saved scanner setup: {setup_name}.")

    def delete_current_scanner_setup(self) -> None:
        """Delete the selected scanner setup."""
        setup_name = self.scanner_setup_combo.currentText()
        if not setup_name:
            return
        if len(self.scanner_setups) <= 1:
            QMessageBox.warning(
                self, "Cannot delete", "At least one scanner setup must remain."
            )
            return

        del self.scanner_setups[setup_name]
        self.scanner_results_by_setup.pop(setup_name, None)
        save_json(SCANNER_SETUPS_FILE, {"setups": self.scanner_setups})
        self.populate_scanner_setup_combo()
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources()
        self.append_log(f"Deleted scanner setup: {setup_name}.")

    def _scanner_is_running(self) -> bool:
        return (
            hasattr(self, "scanner_worker")
            and self.scanner_worker is not None
            and self.scanner_worker.isRunning()
        )

    def _prepare_scanner_run(self, show_warnings: bool = True) -> bool:
        """Validate that a database scanner run can start."""
        if self._scanner_is_running():
            if show_warnings:
                QMessageBox.information(
                    self,
                    "Scanner Running",
                    "A scanner run is already in progress. Please wait for it to complete.",
                )
            return False

        if getattr(self, "db_initializing", False):
            message = "Market-data cache connection is still initializing. Please try again in a moment."
            self.append_log(f"Scanner blocked: {message}")
            if show_warnings:
                QMessageBox.information(self, "Database initializing", message)
            return False

        if not self.db_enabled or self.db_engine is None:
            message = "No market-data cache is available. Check the PC connection or local mirror."
            self.append_log(f"Scanner blocked: {message}")
            if show_warnings:
                QMessageBox.warning(self, "Database unavailable", message)
            return False

        return True

    def _start_scanner_worker(self) -> None:
        """Start the worker that loads cached scanner metrics."""
        self.progress_label.setText("Scanning market-data cache...")
        self.progress_bar.setValue(0)

        setup_name = self.running_scanner_setup_name
        if setup_name == "__ALL__":
            scanner_rules_by_setup = {
                name: self._rules_for_scanner_setup(setup)
                for name, setup in self.scanner_setups.items()
            }
        else:
            active_name = setup_name or self.scanner_setup_combo.currentText()
            if active_name == self.scanner_setup_combo.currentText():
                active_setup = self.get_current_scanner_setup_values()
            else:
                active_setup = self.scanner_setups.get(active_name, {})
            scanner_rules_by_setup = {
                active_name: self._rules_for_scanner_setup(active_setup)
            }

        self.scanner_worker = ScannerWorker(
            tickers=self.universe_tickers or None,
            engine=self.db_engine,
            min_volume=0,
            min_dollar_volume=0,
            min_adr=0,
            min_growth_rank=0,
            min_trend_intensity=0,
            universe_limit=self.universe_limit,
            scanner_rules_by_setup=scanner_rules_by_setup,
        )
        self.scanner_worker.universe_loaded.connect(self._on_scanner_universe_loaded)
        self.scanner_worker.log_message.connect(self.append_log)
        self.scanner_worker.finished_scan.connect(self._on_scanner_finished)
        self.scanner_worker.error_occurred.connect(self._on_scanner_error)
        self._track_worker("scanner_worker", self.scanner_worker)
        self.scanner_worker.start()

    def _on_scanner_universe_loaded(self, tickers: List[str]) -> None:
        """Retain the asynchronously loaded universe for future scans/searches."""
        self.universe_tickers = list(tickers or [])
        self.append_log(f"Loaded {len(self.universe_tickers):,} scanner universe symbols.")

    def run_all_scanners(
        self, checked: bool = False, show_warnings: bool = True
    ) -> None:
        """Run all configured scanner setups against the market-data cache."""
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

    def _scan_metrics_for_setup(
        self, setup_name: str, stock_metrics: list
    ) -> List[dict]:
        """Apply a named scanner setup to raw stock metrics."""
        setup = self.scanner_setups.get(
            setup_name, self.get_current_scanner_setup_values()
        )
        scanner = StockScanner()

        rules = ScannerMixin._rules_for_scanner_setup(setup)

        op_map = {
            ">": ComparisonOperator.GREATER_THAN,
            "<": ComparisonOperator.LESS_THAN,
            "==": ComparisonOperator.EQUAL,
            ">=": ComparisonOperator.GREATER_EQUAL,
            "<=": ComparisonOperator.LESS_EQUAL,
            "!=": ComparisonOperator.NOT_EQUAL,
        }

        # Scanner metric rows without usable price history are not part of the
        # scannable universe. Visible funnel stages correspond only to the
        # user's active rules.
        history_rule = ScanRule(
            name="price_history_days",
            attribute="price_history_days",
            operator=ComparisonOperator.GREATER_EQUAL,
            threshold=1.0,
        )
        scannable_metrics = [
            stock for stock in stock_metrics if history_rule.evaluate(stock)
        ]

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

            scanner.add_rule(
                ScanRule(name=attr, attribute=attr, operator=op, threshold=threshold)
            )

        filtered, rule_counts = scanner.scan_with_funnel(scannable_metrics)
        self.__dict__.setdefault("scanner_funnel_counts_by_setup", {})[
            setup_name
        ] = {
            "universe_count": len(scannable_metrics),
            "rule_counts": rule_counts,
        }
        return scanner.score_results(
            filtered,
            scorers=[
                self._score_growth_rank,
                self._score_trend_intensity,
                self._score_adr,
            ],
        )

    def _show_scanner_results_for_setup(self, setup_name: str) -> None:
        """Display cached results and funnel values for a scanner setup."""
        self.scanner_results = list(self.scanner_results_by_setup.get(setup_name, []))
        self.scanner_dataframe = pd.DataFrame(self.scanner_results)
        self.populate_scanner_table()
        self._display_scanner_funnel(setup_name)

    def _on_database_scanner_finished(self, payload: dict) -> None:
        """Apply rows and funnel aggregates already filtered by the database."""
        setup_name = (
            self.running_scanner_setup_name or self.scanner_setup_combo.currentText()
        )
        results_by_setup = payload.get("results_by_setup") or {}
        funnels_by_setup = payload.get("funnels_by_setup") or {}
        rules_by_setup = payload.get("rules_by_setup") or {}
        is_live = bool(getattr(self, "_scanner_run_is_live", False))

        for name, rows in results_by_setup.items():
            requested_rules = list(rules_by_setup.get(name) or [])
            if name == self.scanner_setup_combo.currentText():
                visible_rules = self.get_current_scanner_rules_from_ui()
                if self._scanner_rule_signature(
                    requested_rules
                ) != self._scanner_rule_signature(visible_rules):
                    self._scanner_live_refresh_pending = True
                    continue
            scored_rows = StockScanner.score_results(
                list(rows or []),
                scorers=[
                    self._score_growth_rank,
                    self._score_trend_intensity,
                    self._score_adr,
                ],
            )
            self.scanner_results_by_setup[name] = scored_rows
            funnel = dict(funnels_by_setup.get(name) or {})
            funnel["rules"] = requested_rules
            self.scanner_funnel_counts_by_setup[name] = funnel
            if not is_live:
                self.append_log(
                    f"Scanner completed for {name}: {len(scored_rows)} symbols found."
                )

        active_setup = self.scanner_setup_combo.currentText()
        if setup_name != "__ALL__" and setup_name in results_by_setup:
            active_setup = setup_name
        self.scanner_results = list(
            self.scanner_results_by_setup.get(active_setup, [])
        )
        self.scanner_dataframe = pd.DataFrame(self.scanner_results)
        self.populate_scanner_table()
        self._display_scanner_funnel(active_setup)
        selected_source = {"type": "scan", "setup": active_setup}
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(selected_source=selected_source)
        self.update_dashboard_summary()

        queried_universe = sum(
            int((funnels_by_setup.get(name) or {}).get("universe_count") or 0)
            for name in results_by_setup
        )
        if (
            queried_universe == 0
            and self.running_scanner_show_warnings
            and not is_live
        ):
            QMessageBox.warning(
                self,
                "Scanner Empty",
                "No cached scanner snapshot was found for the universe. "
                "Run Update 1D Data first, then scan again.",
            )
        self.progress_label.setText("Scanner complete.")
        self.progress_bar.setValue(100)
        self.running_scanner_setup_name = None
        self.running_scanner_show_warnings = True
        self._scanner_run_is_live = False
        self._start_pending_live_scanner_refresh()

    def _on_scanner_finished(self, stock_metrics: list, payload: object) -> None:
        if isinstance(payload, dict) and payload.get("database_filtered"):
            self._on_database_scanner_finished(payload)
            return
        setup_name = (
            self.running_scanner_setup_name or self.scanner_setup_combo.currentText()
        )
        if not stock_metrics:
            self.scanner_results = []
            if setup_name == "__ALL__":
                for name in self.scanner_setups:
                    self.scanner_results_by_setup[name] = []
                    self._scan_metrics_for_setup(name, [])
            else:
                self.scanner_results_by_setup[setup_name] = []
                self._scan_metrics_for_setup(setup_name, [])
            self.scanner_dataframe = pd.DataFrame()
            self.populate_scanner_table()
            self._display_scanner_funnel(
                self.scanner_setup_combo.currentText()
                if setup_name == "__ALL__"
                else setup_name
            )
            self.update_dashboard_summary()
            self.append_log("Scanner completed: no cached database rows found.")
            if self.running_scanner_show_warnings:
                QMessageBox.warning(
                    self,
                    "Scanner Empty",
                    "No cached database data was found for the universe. Run Update 1D Data first, then scan again.",
                )
            self.progress_label.setText("Scanner complete.")
            self.running_scanner_setup_name = None
            self.running_scanner_show_warnings = True
            self._scanner_run_is_live = False
            self._start_pending_live_scanner_refresh()
            return

        if setup_name == "__ALL__":
            for name in self.scanner_setups:
                results = self._scan_metrics_for_setup(name, stock_metrics)
                self.scanner_results_by_setup[name] = list(results)
                self.append_log(
                    f"Scanner completed for {name}: {len(results)} symbols found."
                )
            active_setup_name = self.scanner_setup_combo.currentText()
            self.scanner_results = list(
                self.scanner_results_by_setup.get(active_setup_name, [])
            )
            selected_source = {"type": "scan", "setup": active_setup_name}
        else:
            self.scanner_results = self._scan_metrics_for_setup(
                setup_name, stock_metrics
            )
            self.scanner_results_by_setup[setup_name] = list(self.scanner_results)
            if self.scanner_results:
                self.append_log(
                    f"Scanner completed for {setup_name}: {len(self.scanner_results)} symbols found."
                )
            else:
                self.append_log(
                    f"Scanner completed for {setup_name}: no symbols passed filters."
                )
            selected_source = {"type": "scan", "setup": setup_name}

        self.scanner_dataframe = pd.DataFrame(self.scanner_results)

        self.populate_scanner_table()
        self._display_scanner_funnel(
            self.scanner_setup_combo.currentText()
            if setup_name == "__ALL__"
            else setup_name
        )
        if hasattr(self, "sidebar_source_combo"):
            self.refresh_sidebar_sources(selected_source=selected_source)
        self.update_dashboard_summary()
        self.progress_label.setText("Scanner complete.")
        self.progress_bar.setValue(100)
        self.running_scanner_setup_name = None
        self.running_scanner_show_warnings = True
        self._scanner_run_is_live = False
        self._start_pending_live_scanner_refresh()

    def _on_scanner_error(self, error_message: str) -> None:
        self.append_log(f"Scanner error: {error_message}")
        if self.running_scanner_show_warnings and not getattr(
            self, "_scanner_run_is_live", False
        ):
            QMessageBox.warning(self, "Scanner failed", error_message)
        self.progress_label.setText("Scanner failed.")
        self.running_scanner_setup_name = None
        self.running_scanner_show_warnings = True
        self._scanner_run_is_live = False
        self._start_pending_live_scanner_refresh()

    def refresh_data_to_db(self) -> bool:
        """Launch (or terminate) the standalone 1D historical.py refresh process."""
        if getattr(self, "_database_reconciliation_in_progress", False):
            QMessageBox.information(
                self,
                "Database synchronization in progress",
                "PC/local data is being synchronized. The 1D refresh can start "
                "after the database switch finishes or retries.",
            )
            return False
        if not self.db_enabled:
            QMessageBox.warning(
                self,
                "Database unavailable",
                "No market-data cache is available.",
            )
            return False

        running, _ = is_refresh_running(MODE_1D)
        if running:
            self._confirm_and_terminate_refresh(MODE_1D, "1D Data")
            return False

        self.append_log(
            "Launching 1D data refresh as a background process (historical.py)..."
        )
        self.progress_bar.setValue(0)
        self.progress_label.setText("Starting 1D refresh...")
        try:
            result = launch_refresh(MODE_1D, universe_limit=self.universe_limit)
        except Exception as exc:
            QMessageBox.warning(
                self, "Launch failed", f"Could not start historical.py: {exc}"
            )
            return False
        self._refresh_active_run_id[MODE_1D] = result.run_id
        self._poll_refresh_status()
        return True

    def refresh_hourly_data_to_db(self) -> bool:
        """Launch (or terminate) the standalone 1H historical.py refresh process."""
        if getattr(self, "_database_reconciliation_in_progress", False):
            QMessageBox.information(
                self,
                "Database synchronization in progress",
                "PC/local data is being synchronized. The 1H refresh can start "
                "after the database switch finishes or retries.",
            )
            return False
        if not self.db_enabled:
            QMessageBox.warning(
                self,
                "Database unavailable",
                "No market-data cache is available.",
            )
            return False

        running, _ = is_refresh_running(MODE_1H)
        if running:
            self._confirm_and_terminate_refresh(MODE_1H, "1H Data")
            return False

        self.append_log(
            "Launching 1H data refresh as a background process (historical.py)..."
        )
        self.progress_bar.setValue(0)
        self.progress_label.setText("Starting 1H refresh...")
        try:
            result = launch_refresh(MODE_1H, universe_limit=self.universe_limit)
        except Exception as exc:
            QMessageBox.warning(
                self, "Launch failed", f"Could not start historical.py: {exc}"
            )
            return False
        self._refresh_active_run_id[MODE_1H] = result.run_id
        self._poll_refresh_status()
        return True

    def _confirm_and_terminate_refresh(self, mode: str, label: str) -> None:
        reply = QMessageBox.question(
            self,
            f"Terminate {label} refresh?",
            f"A {label} refresh is currently running. Are you sure you want to terminate it?\n\n"
            "Data already saved to the active market-data cache will NOT be lost. "
            "The next refresh will resume from where this one left off.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        terminated = terminate_refresh(mode)
        if terminated:
            self.append_log(
                f"Termination requested for {label} refresh. Process confirmed stopped."
            )
        else:
            status = read_status(mode)
            if status.get("status") == "running":
                self.append_log(
                    f"Termination requested for {label} refresh, but the process is still running. "
                    "Try again if needed."
                )
            else:
                self.append_log(f"{label} refresh was not running (already finished).")
        self._poll_refresh_status()

    def _poll_refresh_status(self) -> None:
        """Polled by main_window's refresh-status QTimer to reflect historical.py progress."""
        for mode, button, label_prefix in (
            (MODE_1D, getattr(self, "refresh_db_button", None), "Update 1D Data"),
            (MODE_1H, getattr(self, "refresh_hourly_button", None), "Update 1H Data"),
        ):
            if button is None:
                continue
            running, status = is_refresh_running(mode)
            if not running and status.get("status") in ("running", "starting"):
                reconcile_stale_status(mode)
                status = read_status(mode)
            if status.get("status") in ("starting", "running"):
                # Adopt whatever run is actually active as "the" run we're tracking,
                # regardless of whether this session's button click started it (e.g.
                # after reopening main.py mid-refresh) — see _is_new_terminal_refresh_event.
                self._refresh_active_run_id[mode] = status.get("run_id")
            self._apply_refresh_status_to_ui(
                mode, button, label_prefix, running, status
            )

    def _apply_refresh_status_to_ui(
        self, mode: str, button, label_prefix: str, running: bool, status: dict
    ) -> None:
        self._append_new_status_log_lines(mode, status.get("recent_log") or [])
        if running:
            progress = status.get("progress") or {}
            percent = progress.get("percent", 0)
            is_starting = status.get("status") == "starting"
            eta = progress.get("eta_text") or ("starting..." if is_starting else "?")
            phase = status.get("phase") or ("starting" if is_starting else "")
            action_label = (
                label_prefix.split(" ", 1)[1] if " " in label_prefix else label_prefix
            )
            button.setText(f"Terminate {action_label} ({percent}%, ETA {eta})")
            button.setEnabled(True)
            self.progress_bar.setValue(percent)
            self.progress_label.setText(
                f"{label_prefix}: {phase} - {percent}% (ETA {eta})"
            )
            return

        button.setText(label_prefix)
        button.setEnabled(True)
        outcome = status.get("status")
        if outcome not in ("completed", "error", "terminated"):
            return

        is_new_terminal_event = self._is_new_terminal_refresh_event(mode, status)
        if outcome == "completed":
            # A successful historical refresh is an event, not durable system
            # state.  Show it once for a newly finished run, then release the
            # shared progress area back to Buy Board readiness.  In particular,
            # do not repaint a previous session's completed status every poll.
            if not is_new_terminal_event:
                return
            self._handle_refresh_terminal_status(mode, status)
            if any(is_refresh_running(candidate)[0] for candidate in (MODE_1D, MODE_1H)):
                return
            summary = self._refresh_terminal_summary_text(mode, status)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.progress_label.setText(summary)
            token = (status.get("run_id"), status.get("finished_at"))
            visible_tokens = self.__dict__.setdefault(
                "_refresh_completion_display_tokens", {}
            )
            visible_tokens[mode] = token
            QTimer.singleShot(
                REFRESH_SUCCESS_DISPLAY_MS,
                lambda m=mode, t=token, text=summary: self._clear_refresh_success_if_current(
                    m, t, text
                ),
            )
            return

        # Errors and user termination remain visible until another foreground
        # task updates the status area; these outcomes may require attention.
        self.progress_label.setText(self._refresh_terminal_summary_text(mode, status))
        if is_new_terminal_event:
            self._handle_refresh_terminal_status(mode, status)

    def _clear_refresh_success_if_current(
        self,
        mode: str,
        token: tuple,
        expected_text: str,
    ) -> None:
        visible_tokens = self.__dict__.get(
            "_refresh_completion_display_tokens", {}
        )
        if visible_tokens.get(mode) != token:
            return
        visible_tokens.pop(mode, None)
        if visible_tokens:
            return
        progress_label = self.__dict__.get("progress_label")
        progress_bar = self.__dict__.get("progress_bar")
        if (
            progress_label is None
            or progress_bar is None
            or progress_label.text() != expected_text
        ):
            return
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        progress_label.setText("Ready.")
        progress_label.setToolTip("")
        readiness_update = getattr(self, "_update_buyboard_readiness_progress", None)
        if callable(readiness_update):
            readiness_update()

    def _is_new_terminal_refresh_event(self, mode: str, status: dict) -> bool:
        finished_at = status.get("finished_at")
        if not finished_at:
            return False
        active_run_id = self._refresh_active_run_id.get(mode)
        if active_run_id is not None and status.get("run_id") != active_run_id:
            # A different (older or unrelated) run's terminal event -- the run this
            # session is actually tracking hasn't reached a terminal state yet.
            return False
        if self._refresh_last_finished_at.get(mode) == finished_at:
            return False
        self._refresh_last_finished_at[mode] = finished_at
        return True

    def _append_new_status_log_lines(self, mode: str, recent_log: list) -> None:
        seen = self._refresh_last_log_count.get(mode, 0)
        if seen > len(recent_log):
            seen = 0  # a new run started and the recent_log buffer reset
        for line in recent_log[seen:]:
            self.append_log(line)
        self._refresh_last_log_count[mode] = len(recent_log)

    def _handle_refresh_terminal_status(self, mode: str, status: dict) -> None:
        result = status.get("result") or {}
        outcome = status.get("status")
        derived_note = self._refresh_derived_data_note(mode, status)
        if outcome == "completed":
            self.show_refresh_complete(result.get("updated_count", 0))
            self.update_dashboard_summary(force=True)
            if getattr(self, "db_engine_source", "none") == "pc":
                mirror_sync = getattr(
                    self, "_sync_active_pc_to_local_mirror", None
                )
                if callable(mirror_sync):
                    mirror_sync()
        elif outcome == "error":
            message = result.get("error_message") or "Unknown error"
            self.show_refresh_error(message)
            if derived_note:
                self.append_log(derived_note)
            QMessageBox.warning(self, "Refresh failed", message)
            self.update_dashboard_summary(force=True)
        elif outcome == "terminated":
            self.append_log(f"{mode.upper()} refresh was terminated by user request.")
            if derived_note:
                self.append_log(derived_note)
            self.progress_label.setText(f"{mode.upper()} refresh terminated.")
            self.update_dashboard_summary(force=True)

        if mode == MODE_1D and getattr(
            self, "_pending_local_mirror_hourly_refresh", False
        ):
            self._pending_local_mirror_hourly_refresh = False
            if outcome == "completed":
                hourly_running, _ = is_refresh_running(MODE_1H)
                if hourly_running:
                    self.append_log(
                        "The 1H refresh is already running; no duplicate was launched."
                    )
                else:
                    self.append_log("Starting the queued 1H local-mirror refresh ...")
                    if not self.refresh_hourly_data_to_db():
                        self._run_scanners_after_local_mirror_refresh = False
                        self.run_all_scanners(show_warnings=False)
            else:
                self.append_log(
                    "The queued 1H refresh was not started because the 1D refresh did not complete."
                )

        should_resume_scanners = getattr(
            self, "_run_scanners_after_local_mirror_refresh", False
        ) and (
            mode == MODE_1H or (mode == MODE_1D and outcome != "completed")
        )
        if should_resume_scanners:
            self._run_scanners_after_local_mirror_refresh = False
            self.run_all_scanners(show_warnings=False)

    def _refresh_derived_data_note(self, mode: str, status: dict) -> str:
        """Explain when 1D price data was saved but indicators/scanner metrics didn't finish."""
        if mode != MODE_1D:
            return ""
        completed_phases = status.get("completed_phases") or []
        if (
            status.get("derived_data_complete")
            or "daily_history" not in completed_phases
        ):
            return ""
        return (
            "Price data was saved, but chart indicators/scanner metrics did not finish. "
            "Run the 1D refresh again to bring derived data back in sync."
        )

    def _refresh_terminal_summary_text(self, mode: str, status: dict) -> str:
        """Short, always-safe-to-show summary of the last known outcome (no dialogs/side effects)."""
        outcome = status.get("status")
        result = status.get("result") or {}
        label = mode.upper()
        note = (
            " Derived data may be stale."
            if self._refresh_derived_data_note(mode, status)
            else ""
        )
        if outcome == "completed":
            return f"{label} refresh: completed ({result.get('updated_count', 0)} updated)."
        if outcome == "error":
            return f"{label} refresh: error - {result.get('error_message') or 'unknown error'}.{note}"
        if outcome == "terminated":
            return f"{label} refresh: terminated.{note}"
        return f"{label} refresh: {outcome}."

    def populate_scanner_table(self) -> None:
        """Refresh shared symbol views after scanner results change."""
        if hasattr(self, "sidebar_source_combo"):
            source = self.sidebar_source_combo.currentData() or {}
            if (
                source.get("type") == "scan"
                and source.get("setup") == self.scanner_setup_combo.currentText()
            ):
                self.refresh_stock_sidebar()
        self.populate_tradingview_watchlist_symbols()

    def update_scanner_preview_chart(self, symbol: str) -> None:
        """Update Scanner-tab details from the shared sidebar selection."""
        self.update_scanner_metrics_details(symbol)

    def _get_scanner_stock(self, symbol: str) -> Optional[dict]:
        return next(
            (item for item in self.scanner_results if item["symbol"] == symbol), None
        )

    def add_selected_scanner_to_watchlist(self) -> None:
        """Persist the selected scan result in the passive Watchlist stage."""

        if not self.selected_scan_symbol:
            QMessageBox.warning(
                self,
                "No selection",
                "Please select a stock from the scanner results first.",
            )
            return
        stock = self._get_scanner_stock(self.selected_scan_symbol)
        if stock is None:
            QMessageBox.warning(
                self,
                "Not found",
                "Selected stock is no longer available in scanner results.",
            )
            return
        add_candidate = getattr(self, "_add_watchlist_candidate", None)
        if callable(add_candidate):
            add_candidate(
                stock["symbol"],
                name=stock.get("name") or stock["symbol"],
                entry_price=stock.get("price"),
                source="Scanner",
            )
