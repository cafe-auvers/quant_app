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



class DashboardMixin:
    def _build_dashboard_tab(self) -> None:
        """Build content for the dashboard tab."""
        layout = QVBoxLayout()
        summary_group = QGroupBox("Dashboard Summary")
        summary_layout = QVBoxLayout()
        self.dashboard_summary_label = QLabel()
        self.dashboard_summary_label.setWordWrap(True)
        summary_layout.addWidget(self.dashboard_summary_label)
        summary_group.setLayout(summary_layout)
        layout.addWidget(summary_group)

        kis_group = QGroupBox("KIS Account Snapshot")
        kis_layout = QVBoxLayout()

        kis_form = QFormLayout()
        self.kis_environment_combo = QComboBox()
        self.kis_environment_combo.addItems([KisEnvironment.SIM.value, KisEnvironment.PROD.value])
        self.kis_environment_combo.currentTextChanged.connect(self.populate_kis_account_combo)
        kis_form.addRow("Profile", self.kis_environment_combo)

        self.kis_account_combo = QComboBox()
        self.kis_account_combo.currentIndexChanged.connect(self.update_kis_account_status)
        kis_form.addRow("Account", self.kis_account_combo)

        kis_options_layout = QHBoxLayout()
        self.kis_domestic_checkbox = QCheckBox("Domestic")
        self.kis_domestic_checkbox.setChecked(True)
        self.kis_overseas_checkbox = QCheckBox("Overseas")
        self.kis_overseas_checkbox.setChecked(True)
        kis_options_layout.addWidget(self.kis_domestic_checkbox)
        kis_options_layout.addWidget(self.kis_overseas_checkbox)
        kis_options_layout.addStretch()
        kis_form.addRow("Sections", kis_options_layout)
        kis_layout.addLayout(kis_form)

        self.kis_account_status_label = QLabel()
        self.kis_account_status_label.setWordWrap(True)
        kis_layout.addWidget(self.kis_account_status_label)

        self.kis_account_summary_label = QLabel("No account snapshot loaded.")
        self.kis_account_summary_label.setWordWrap(True)
        kis_layout.addWidget(self.kis_account_summary_label)

        self.kis_holdings_table = QTableWidget(0, 7)
        self.kis_holdings_table.setHorizontalHeaderLabels([
            "Symbol",
            "Name",
            "Qty",
            "Avg",
            "Price",
            "Eval",
            "P/L %",
        ])
        self.kis_holdings_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.kis_holdings_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.kis_holdings_table.setMinimumHeight(180)
        kis_layout.addWidget(self.kis_holdings_table)

        kis_button_layout = QHBoxLayout()
        self.kis_refresh_button = QPushButton("Refresh KIS Snapshot")
        self.kis_refresh_button.setObjectName("kisRefreshButton")
        self.kis_refresh_button.clicked.connect(self.refresh_kis_account_snapshot)
        kis_button_layout.addWidget(self.kis_refresh_button)
        kis_button_layout.addStretch()
        kis_layout.addLayout(kis_button_layout)

        kis_group.setLayout(kis_layout)
        layout.addWidget(kis_group)

        button_layout = QHBoxLayout()
        scan_button = QPushButton("Run All Scanners")
        scan_button.setObjectName("scanButton")
        scan_button.clicked.connect(self.run_all_scanners)
        button_layout.addWidget(scan_button)

        refresh_button = QPushButton("Refresh Summary")
        refresh_button.setObjectName("refreshSummaryButton")
        refresh_button.clicked.connect(self.update_dashboard_summary)
        button_layout.addWidget(refresh_button)

        self.refresh_db_button = QPushButton("Update 1D Data")
        self.refresh_db_button.setObjectName("refreshDbButton")
        self.refresh_db_button.clicked.connect(self.refresh_data_to_db)
        button_layout.addWidget(self.refresh_db_button)
        self.refresh_hourly_button = QPushButton("Update 1H Data")
        self.refresh_hourly_button.setObjectName("refreshHourlyButton")
        self.refresh_hourly_button.clicked.connect(self.refresh_hourly_data_to_db)
        button_layout.addWidget(self.refresh_hourly_button)
        self.refresh_intraday_button = QPushButton("Update Watchlist Intraday")
        self.refresh_intraday_button.setObjectName("refreshIntradayButton")
        self.refresh_intraday_button.clicked.connect(self.refresh_watchlist_intraday_cache)
        button_layout.addWidget(self.refresh_intraday_button)
        layout.addLayout(button_layout)

        live_group = QGroupBox("Live Intraday Updates")
        live_layout = QHBoxLayout()
        self.live_data_checkbox = QCheckBox("Live Data Auto Refresh")
        self.live_data_checkbox.toggled.connect(self._on_live_data_toggled)
        live_layout.addWidget(self.live_data_checkbox)

        self.live_refresh_minutes_spin = QSpinBox()
        self.live_refresh_minutes_spin.setRange(1, 60)
        self.live_refresh_minutes_spin.setValue(5)
        self.live_refresh_minutes_spin.setSuffix(" min")
        self.live_refresh_minutes_spin.valueChanged.connect(self._on_live_refresh_interval_changed)
        live_layout.addWidget(QLabel("Every"))
        live_layout.addWidget(self.live_refresh_minutes_spin)

        self.live_data_source_label = QLabel(format_intraday_source_label("yfinance"))
        self.live_data_source_label.setWordWrap(True)
        live_layout.addWidget(self.live_data_source_label, stretch=1)

        self.live_data_status_label = QLabel("Live data: off")
        live_layout.addWidget(self.live_data_status_label)
        live_group.setLayout(live_layout)
        layout.addWidget(live_group)

        self.dashboard_widget.setLayout(layout)
        self.populate_kis_account_combo()
    def populate_kis_account_combo(self, *args) -> None:
        """Refresh selectable KIS accounts from local configuration."""
        if not hasattr(self, "kis_account_combo"):
            return

        environment = self.kis_environment_combo.currentText() if hasattr(self, "kis_environment_combo") else "PROD"
        current_account = self.kis_account_combo.currentData()
        self.kis_account_combo.blockSignals(True)
        self.kis_account_combo.clear()

        profiles = [
            profile for profile in discover_account_profiles()
            if profile.get("environment") == environment
        ]
        for profile in profiles:
            self.kis_account_combo.addItem(profile["label"], profile)

        if current_account:
            selected_index = -1
            for index in range(self.kis_account_combo.count()):
                profile = self.kis_account_combo.itemData(index) or {}
                if profile.get("account_no") == current_account.get("account_no"):
                    selected_index = index
                    break
            if selected_index >= 0:
                self.kis_account_combo.setCurrentIndex(selected_index)

        self.kis_account_combo.blockSignals(False)
        self.update_kis_account_status()
        if hasattr(self, "trade_kis_account_combo"):
            self.populate_trade_account_combo()
    def _setup_live_data_timer(self) -> None:
        self.live_data_timer = QTimer(self)
        self.live_data_timer.setInterval(LIVE_INTRADAY_REFRESH_INTERVAL_MS)
        self.live_data_timer.timeout.connect(self._run_live_intraday_refresh_tick)
    def _on_live_data_toggled(self, enabled: bool) -> None:
        if self.live_data_timer is None:
            return
        if enabled:
            self._on_live_refresh_interval_changed(self.live_refresh_minutes_spin.value())
            self.live_data_timer.start()
            self.live_data_status_label.setText("Live data: on")
            self.append_log("Live intraday auto refresh enabled.")
            self._run_live_intraday_refresh_tick()
            return

        self.live_data_timer.stop()
        self.live_data_status_label.setText("Live data: off")
        self.append_log("Live intraday auto refresh disabled.")
    def _on_live_refresh_interval_changed(self, minutes: int) -> None:
        interval_ms = max(1, int(minutes)) * 60 * 1000
        if self.live_data_timer is not None:
            self.live_data_timer.setInterval(interval_ms)
    def _run_live_intraday_refresh_tick(self) -> None:
        if not hasattr(self, "live_data_checkbox") or not self.live_data_checkbox.isChecked():
            return
        if not self._is_us_regular_market_open():
            self.live_data_status_label.setText("Live data: waiting for U.S. market hours")
            return
        if self.intraday_bulk_worker is not None and self.intraday_bulk_worker.isRunning():
            self.live_data_status_label.setText("Live data: refresh already running")
            return

        self.live_data_status_label.setText("Live data: refreshing watchlist")
        self.refresh_watchlist_intraday_cache(show_messages=False, triggered_by_live=True)
    @staticmethod
    def _is_us_regular_market_open(now: Optional[dt.datetime] = None) -> bool:
        if now is None:
            market_now = dt.datetime.now(US_MARKET_ZONE)
        elif now.tzinfo is None:
            market_now = now.replace(tzinfo=US_MARKET_ZONE)
        else:
            market_now = now.astimezone(US_MARKET_ZONE)

        if market_now.weekday() >= 5:
            return False
        current_time = market_now.time()
        return US_MARKET_OPEN_TIME <= current_time < US_MARKET_CLOSE_TIME
    def _selected_dashboard_kis_profile(self) -> Optional[dict]:
        if not hasattr(self, "kis_environment_combo") or not hasattr(self, "kis_account_combo"):
            return None
        profile = self.kis_account_combo.currentData()
        if not profile:
            return None
        return {
            "environment": self.kis_environment_combo.currentText(),
            "account_no": profile.get("account_no", ""),
            "label": profile.get("label", ""),
        }
    def update_kis_account_status(self) -> None:
        """Show whether the selected KIS profile is ready to fetch."""
        if not hasattr(self, "kis_account_status_label"):
            return

        environment = self.kis_environment_combo.currentText() if hasattr(self, "kis_environment_combo") else "SIM"
        profile = self.kis_account_combo.currentData() if hasattr(self, "kis_account_combo") else None
        if not profile:
            self.kis_account_status_label.setText(
                f"{environment} credentials can use configured API keys, but no account number is configured. "
                "KIS balance APIs require an account number; add KIS_PROD_ACCOUNT_NO or KIS_PROD_ACCOUNTS to .env."
            )
            return

        try:
            config = load_config(KisEnvironment(environment), account_no_override=profile.get("account_no"))
        except Exception as exc:
            self.kis_account_status_label.setText(
                f"{environment} profile not configured: {exc}. "
                "Add the KIS_* values to .env before refreshing."
            )
            return

        self.kis_account_status_label.setText(
            f"{environment} profile ready. Selected account {config.account_no_masked}, base URL {config.base_url}."
        )
    def preload_kis_accounts_on_startup(self) -> None:
        """Fetch all configured SIM/PROD account snapshots once at startup."""
        if self.kis_startup_worker is not None and self.kis_startup_worker.isRunning():
            return
        profiles = discover_account_profiles()
        if not profiles:
            self.append_log("Startup KIS preload skipped: no configured SIM/PROD accounts.")
            return
        if self.kis_account_worker is not None and self.kis_account_worker.isRunning():
            self.append_log("Startup KIS preload skipped: manual KIS refresh is already running.")
            return

        self.append_log(f"Starting startup KIS preload for {len(profiles)} configured account(s).")
        if hasattr(self, "kis_account_status_label"):
            self.kis_account_status_label.setText("Startup KIS preload running...")
        self.kis_startup_worker = KisStartupAccountsWorker(profiles)
        self.kis_startup_worker.log_message.connect(self.append_log)
        self.kis_startup_worker.finished_profiles.connect(self._on_startup_kis_accounts_finished)
        self.kis_startup_worker.finished.connect(
            lambda worker=self.kis_startup_worker: self._clear_worker_reference("kis_startup_worker", worker)
        )
        self.kis_startup_worker.start()
    def _on_startup_kis_accounts_finished(self, snapshots: dict, errors: list) -> None:
        self.kis_account_snapshots.update(snapshots)
        self.sync_buylist_positions_from_kis_snapshots(snapshots)
        selected_profile = self._selected_dashboard_kis_profile()
        if selected_profile:
            selected_snapshot = self.kis_account_snapshots.get(
                (selected_profile["environment"], selected_profile["account_no"])
            )
            if selected_snapshot:
                fx = self._parse_float(self.usd_krw_rate_input, 0.0) if hasattr(self, "usd_krw_rate_input") else 0.0
                self.kis_account_summary_label.setText(self._format_kis_snapshot_summary(selected_snapshot, fx_rate=fx))
                self.populate_kis_holdings_table(self._flatten_kis_holdings(selected_snapshot))
        if selected_profile:
            self.refresh_usd_krw_rate(show_messages=False)
        else:
            self.apply_cached_trade_account_size()
        loaded_count = len(snapshots)
        if errors:
            self.append_log(f"Startup KIS preload loaded {loaded_count} account(s), {len(errors)} failed.")
            for error in errors[:5]:
                self.append_log(f"Startup KIS preload failed: {self._format_kis_error_message(error)}")
            if hasattr(self, "kis_account_status_label"):
                self.kis_account_status_label.setText(
                    f"Startup KIS preload loaded {loaded_count} account(s), {len(errors)} failed."
                )
        else:
            self.append_log(f"Startup KIS preload loaded {loaded_count} account(s).")
            if hasattr(self, "kis_account_status_label"):
                self.kis_account_status_label.setText(f"Startup KIS preload loaded {loaded_count} account(s).")
    def refresh_kis_account_snapshot(self) -> None:
        """Fetch the selected read-only KIS account snapshot in the background."""
        if self.kis_startup_worker is not None and self.kis_startup_worker.isRunning():
            QMessageBox.information(self, "KIS preload running", "Startup KIS account preload is still running.")
            return
        if self.kis_account_worker is not None and self.kis_account_worker.isRunning():
            QMessageBox.information(self, "KIS refresh running", "A KIS account refresh is already running.")
            return

        include_domestic = self.kis_domestic_checkbox.isChecked()
        include_overseas = self.kis_overseas_checkbox.isChecked()
        if not include_domestic and not include_overseas:
            QMessageBox.warning(self, "No section selected", "Select Domestic, Overseas, or both.")
            return

        environment = self.kis_environment_combo.currentText()
        profile = self.kis_account_combo.currentData()
        if not profile:
            QMessageBox.warning(
                self,
                "No KIS account",
                "Add KIS_PROD_ACCOUNT_NO or KIS_PROD_ACCOUNTS to .env, then restart or refresh the Dashboard.",
            )
            return

        self.kis_refresh_button.setEnabled(False)
        self.kis_account_status_label.setText(f"Fetching {profile.get('label', environment)} account snapshot...")
        self.kis_account_worker = KisAccountWorker(
            environment=environment,
            include_domestic=include_domestic,
            include_overseas=include_overseas,
            account_no=profile.get("account_no"),
        )
        self.kis_account_worker.finished_snapshot.connect(self._on_kis_snapshot_finished)
        self.kis_account_worker.error_occurred.connect(self._on_kis_snapshot_error)
        self.kis_account_worker.finished.connect(
            lambda worker=self.kis_account_worker: self._clear_worker_reference("kis_account_worker", worker)
        )
        self.kis_account_worker.start()
    def _on_kis_snapshot_finished(self, snapshot: dict) -> None:
        self._schedule_kis_refresh_button_enable()
        profile = self._selected_dashboard_kis_profile()
        if profile:
            self.kis_account_snapshots[(profile["environment"], profile["account_no"])] = snapshot
            self.sync_buylist_positions_from_kis_snapshots({(profile["environment"], profile["account_no"]): snapshot})
        self.kis_account_status_label.setText("KIS account snapshot loaded.")
        fx = self._parse_float(self.usd_krw_rate_input, 0.0) if hasattr(self, "usd_krw_rate_input") else 0.0
        self.kis_account_summary_label.setText(self._format_kis_snapshot_summary(snapshot, fx_rate=fx))
        self.populate_kis_holdings_table(self._flatten_kis_holdings(snapshot))
        self.apply_cached_trade_account_size()
        self.append_log("Loaded KIS account snapshot.")
        self.reconcile_open_orders()
    def _on_kis_snapshot_error(self, error_message: str) -> None:
        self._schedule_kis_refresh_button_enable()
        friendly_message = self._format_kis_error_message(error_message)
        self.kis_account_status_label.setText(f"KIS account snapshot failed: {friendly_message}")
        self.append_log(f"KIS account snapshot failed: {friendly_message}")
    def _schedule_kis_refresh_button_enable(self) -> None:
        if not hasattr(self, "kis_refresh_button"):
            return
        self.kis_refresh_button.setEnabled(False)
        QTimer.singleShot(3000, lambda: self.kis_refresh_button.setEnabled(True))
    @staticmethod
    def _format_kis_error_message(error_message: str) -> str:
        if "rate limit" in error_message.lower() or "EGW00201" in error_message or "EGW00215" in error_message:
            return "KIS rate limit exceeded. Wait a few seconds before refreshing again."
        if "INVALID_CHECK_ACNO" in error_message or "account number/product code" in error_message:
            return (
                "KIS rejected the selected account number/product code. "
                "Check the SIM/PROD account number and product code in .env."
            )
        return error_message
    def populate_kis_holdings_table(self, holdings: List[Dict[str, Any]]) -> None:
        # Deduplicate by symbol: keep only the first occurrence per ticker
        # (KIS can return the same stock under multiple exchanges, e.g. DELL on NASD + NYSE)
        seen: set = set()
        deduped = []
        for h in holdings:
            sym = h.get("symbol", "").strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                deduped.append(h)
        self.kis_holdings_table.setRowCount(0)
        for holding in deduped:
            row = self.kis_holdings_table.rowCount()
            self.kis_holdings_table.insertRow(row)
            values = [
                holding.get("symbol", ""),
                holding.get("name", ""),
                self._format_number(holding.get("quantity"), decimals=4),
                self._format_number(holding.get("average_price"), decimals=4),
                self._format_number(holding.get("current_price"), decimals=4),
                self._format_number(holding.get("evaluation_amount"), decimals=2),
                self._format_number(holding.get("profit_loss_rate_pct"), decimals=2),
            ]
            for column, value in enumerate(values):
                self.kis_holdings_table.setItem(row, column, QTableWidgetItem(value))
    @staticmethod
    def _format_kis_snapshot_summary(snapshot: Dict[str, Any], fx_rate: float = 0.0) -> str:
        """Format a human-readable KIS account snapshot summary."""
        env = snapshot.get('environment', '')
        acct = snapshot.get('account', '')
        fetched = snapshot.get('fetched_at', '')
        parts = [f"Fetched: {fetched}  |  Profile: {env}  |  Account: {acct}"]

        # Domestic KRW cash and KR stocks
        cash_krw = 0.0
        kr_stock_krw = 0.0
        kr_pnl_krw = 0.0
        domestic = snapshot.get("domestic")
        if isinstance(domestic, dict):
            dom_summary = domestic.get("summary", {})
            def _f(key: str) -> float:
                try:
                    return float(dom_summary.get(key) or 0)
                except (TypeError, ValueError):
                    return 0.0
            cash_krw = _f("cash_total_krw") or _f("d2_deposit_krw")
            kr_stock_krw = _f("stock_evaluation_krw")
            kr_pnl_krw = _f("evaluation_profit_loss_krw")

        # Overseas USD stocks (deduped by symbol) and cash
        ovrs_stock_usd = 0.0
        ovrs_cash_usd = 0.0
        ovrs_pnl_usd = 0.0
        ovrs_count = 0
        overseas = snapshot.get("overseas")
        if isinstance(overseas, dict):
            seen_syms: set = set()
            for h in overseas.get("holdings", []):
                if not isinstance(h, dict):
                    continue
                sym = h.get("symbol", "").strip().upper()
                if sym and sym not in seen_syms:
                    seen_syms.add(sym)
                    ovrs_count += 1
                    try:
                        ovrs_stock_usd += float(h.get("evaluation_amount") or 0)
                        ovrs_pnl_usd += float(h.get("profit_loss") or 0)
                    except (TypeError, ValueError):
                        pass
            for exch_summary in overseas.get("summary_by_exchange", {}).values():
                if not isinstance(exch_summary, dict):
                    continue
                try:
                    v = float(exch_summary.get("cash_balance_usd") or 0)
                    if v > ovrs_cash_usd:
                        ovrs_cash_usd = v
                except (TypeError, ValueError):
                    pass

        # Breakdown line — mirrors the log message from apply_cached_trade_account_size
        pnl_sign = '+' if ovrs_pnl_usd >= 0 else ''
        parts.append(
            f"KRW cash: {cash_krw:,.0f}  |  KR stocks: {kr_stock_krw:,.0f}  |  "
            f"US stocks: ${ovrs_stock_usd:,.2f} ({ovrs_count} holding(s), P/L {pnl_sign}${ovrs_pnl_usd:,.2f})  |  "
            f"USD cash: ${ovrs_cash_usd:,.2f}"
        )
        parts.append(f"Domestic: cash {cash_krw:,.0f} KRW")
        parts.append(f"Overseas: {ovrs_count} holdings loaded.")
        if kr_stock_krw > 0 and kr_pnl_krw != 0:
            kr_pnl_sign = '+' if kr_pnl_krw >= 0 else ''
            parts.append(f"KR stock P/L: {kr_pnl_sign}{kr_pnl_krw:,.0f} KRW")

        # Total in KRW and USD if FX rate is known
        if fx_rate > 0:
            total_krw = cash_krw + kr_stock_krw + (ovrs_stock_usd + ovrs_cash_usd) * fx_rate
            total_usd = total_krw / fx_rate
            parts.append(
                f"Total (est.): {total_krw:,.0f} KRW = ${total_usd:,.2f} USD  @ {fx_rate:.2f} KRW/USD"
            )

        return "\n".join(parts)
    @staticmethod
    def _flatten_kis_holdings(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        holdings: List[Dict[str, Any]] = []
        domestic = snapshot.get("domestic")
        if isinstance(domestic, dict):
            holdings.extend(item for item in domestic.get("holdings", []) if isinstance(item, dict))

        overseas = snapshot.get("overseas")
        if isinstance(overseas, dict):
            holdings.extend(item for item in overseas.get("holdings", []) if isinstance(item, dict))
        return holdings
    @staticmethod
    def _format_number(value: Any, decimals: int = 2) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        return f"{number:,.{decimals}f}"
    def populate_trade_account_combo(self, *args) -> bool:
        if not hasattr(self, "trade_kis_account_combo"):
            return False

        environment = self.trade_kis_environment_combo.currentText() if hasattr(self, "trade_kis_environment_combo") else "SIM"
        current_account = self.trade_kis_account_combo.currentData()
        self.trade_kis_account_combo.blockSignals(True)
        self.trade_kis_account_combo.clear()
        for profile in discover_account_profiles():
            if profile.get("environment") == environment:
                self.trade_kis_account_combo.addItem(profile["label"], profile)

        if current_account:
            for index in range(self.trade_kis_account_combo.count()):
                profile = self.trade_kis_account_combo.itemData(index) or {}
                if profile.get("account_no") == current_account.get("account_no"):
                    self.trade_kis_account_combo.setCurrentIndex(index)
                    break
        self.trade_kis_account_combo.blockSignals(False)
        self.apply_cached_trade_account_size()
        return True
    def refresh_trade_account_size(self) -> None:
        from src.ui.controllers.account_controller import AccountController
        from src.ui.controllers.base import get_controller

        controller = get_controller(self, "account_controller", AccountController)
        controller.refresh_trade_account_size()
    def _on_trade_account_snapshot_finished(self, snapshot: dict) -> None:
        profile = self.trade_kis_account_combo.currentData() if hasattr(self, "trade_kis_account_combo") else None
        environment = self.trade_kis_environment_combo.currentText() if hasattr(self, "trade_kis_environment_combo") else ""
        if profile:
            self.kis_account_snapshots[(environment, profile.get("account_no", ""))] = snapshot
            self.sync_buylist_positions_from_kis_snapshots({(environment, profile.get("account_no", "")): snapshot})
        self.refresh_usd_krw_rate(show_messages=False)
        self.append_log("Loaded KIS account value for trade sizing.")
        self.reconcile_open_orders()
    def _on_trade_account_snapshot_error(self, error_message: str) -> None:
        friendly_message = self._format_kis_error_message(error_message)
        self.append_log(f"KIS account value failed: {friendly_message}")
    def refresh_usd_krw_rate(self, show_messages: bool = True) -> None:
        if self.fx_rate_worker is not None and self.fx_rate_worker.isRunning():
            if show_messages:
                self.append_log("USD/KRW refresh is already running.")
            return
        snapshot = self._selected_trade_account_snapshot()
        if show_messages:
            self.append_log("Refreshing USD/KRW rate from KIS snapshot, yfinance fallback...")
        self._set_usd_krw_rate_status("USD/KRW: refreshing...")
        self.fx_rate_worker = FxRateWorker(snapshot=snapshot)
        self.fx_rate_worker.finished_rate.connect(self._on_usd_krw_rate_finished)
        self.fx_rate_worker.error_occurred.connect(self._on_usd_krw_rate_error)
        self.fx_rate_worker.finished.connect(
            lambda worker=self.fx_rate_worker: self._clear_worker_reference("fx_rate_worker", worker)
        )
        self.fx_rate_worker.start()
    def _selected_trade_account_snapshot(self) -> Optional[dict]:
        if not hasattr(self, "trade_kis_account_combo") or not hasattr(self, "trade_kis_environment_combo"):
            return None
        profile = self.trade_kis_account_combo.currentData()
        if not profile:
            return None
        environment = self.trade_kis_environment_combo.currentText()
        return self.kis_account_snapshots.get((environment, profile.get("account_no", "")))
    def _on_usd_krw_rate_finished(self, rate: float, source: str, timestamp: str) -> None:
        self.usd_krw_rate_source = source
        if hasattr(self, "usd_krw_rate_input"):
            old_block = self.usd_krw_rate_input.blockSignals(True)
            self.usd_krw_rate_input.setText(f"{rate:.2f}")
            self.usd_krw_rate_input.blockSignals(old_block)
        self._set_usd_krw_rate_status(f"USD/KRW {rate:.2f} from {source} ({timestamp})")
        self.append_log(f"USD/KRW updated: {rate:.2f} from {source}.")
        self.apply_cached_trade_account_size()
    def _on_usd_krw_rate_error(self, error_message: str) -> None:
        current_rate = self._parse_float(self.usd_krw_rate_input, 0.0) if hasattr(self, "usd_krw_rate_input") else 0.0
        if current_rate > 0:
            self._set_usd_krw_rate_status(f"USD/KRW refresh failed; keeping {current_rate:.2f}")
        else:
            self._set_usd_krw_rate_status("USD/KRW refresh failed")
        self.append_log(f"USD/KRW refresh failed: {error_message}")
        self.apply_cached_trade_account_size()
    def _set_usd_krw_rate_status(self, text: str) -> None:
        label = self.__dict__.get("usd_krw_rate_status_label")
        if label is not None:
            label.setText(text)
    def apply_cached_trade_account_size(self, *args) -> None:
        if not hasattr(self, "trade_kis_account_combo") or not hasattr(self, "account_size_input"):
            return
            
        environment = self.trade_kis_environment_combo.currentText()
        profile = self.trade_kis_account_combo.currentData()
        snapshot = None
        fallback_reason = "no KIS profile selected"

        if profile:
            account_no = profile.get("account_no", "")
            snapshot = self.kis_account_snapshots.get((environment, account_no))
            if snapshot is None:
                fallback_reason = f"snapshot not loaded for ({environment}, {account_no})"

        if snapshot:
            usd_krw_rate = self._parse_float(self.usd_krw_rate_input, 1388.89)
            if usd_krw_rate <= 0:
                usd_krw_rate = 1388.89
            breakdown = self._extract_kis_account_value_krw(
                snapshot,
                fx_rate=usd_krw_rate,
                return_breakdown=True,
            )
            if breakdown:
                account_value_krw = breakdown["total_krw"]
                account_value_usd = account_value_krw / usd_krw_rate
                old_block = self.account_size_input.blockSignals(True)
                self.account_size_input.setText(f"{account_value_usd:.2f}")
                self.account_size_input.blockSignals(old_block)
                ovrs_cash = breakdown["ovrs_cash_usd"]
                ovrs_stock = breakdown["ovrs_stock_usd"]
                self.append_log(
                    f"Using {environment} account value: {account_value_krw:,.0f} KRW "
                    f"= {account_value_usd:,.2f} USD "
                    f"[KRW cash: {breakdown['cash_krw']:,.0f} | "
                    f"KR stocks: {breakdown['kr_stock_krw']:,.0f} | "
                    f"US stocks: ${ovrs_stock:,.2f} | "
                    f"USD cash: ${ovrs_cash:,.2f}]"
                )
                self.update_trade_plan_feedback()
                self.recalculate_watchlist_scoreboard_sizes()  # also refreshes ORB panel
                if hasattr(self, "refresh_execution_queue"):
                    self.refresh_execution_queue(environment, show_log=False)
                return
            fallback_reason = "account value is zero in snapshot"

        # Fallback if no profile, no snapshot, or account value is invalid
        if not hasattr(self, "manual_account_sizes"):
            self.manual_account_sizes = {"SIM": 100000.0, "PROD": 10000.0}
        default_val = self.manual_account_sizes.get(environment, 10000.0 if environment == "PROD" else 100000.0)

        old_block = self.account_size_input.blockSignals(True)
        self.account_size_input.setText(f"{default_val:.2f}")
        self.account_size_input.blockSignals(old_block)
        self.append_log(
            f"No KIS snapshot ({fallback_reason}). Using default {environment} balance: ${default_val:,.2f}"
        )
        self.update_trade_plan_feedback()
        self.recalculate_watchlist_scoreboard_sizes()  # also refreshes ORB panel
        if hasattr(self, "refresh_execution_queue"):
            self.refresh_execution_queue(environment, show_log=False)
    def on_account_size_text_changed(self) -> None:
        """Cache the manually entered account size for the active environment."""
        if not hasattr(self, "trade_kis_environment_combo") or not hasattr(self, "account_size_input"):
            return
        env = self.trade_kis_environment_combo.currentText()
        val = self._parse_float(self.account_size_input, 0.0)
        if val > 0:
            if not hasattr(self, "manual_account_sizes"):
                self.manual_account_sizes = {"SIM": 100000.0, "PROD": 10000.0}
            self.manual_account_sizes[env] = val
    @staticmethod
    def _extract_kis_account_value_krw(
        snapshot: Dict[str, Any],
        fx_rate: float = 0.0,
        *,
        return_breakdown: bool = False,
    ) -> Optional[Any]:
        """Return the account total in KRW, or a component breakdown when requested."""
        domestic = snapshot.get("domestic")
        summary = domestic.get("summary", {}) if isinstance(domestic, dict) else {}
        if not isinstance(summary, dict):
            summary = {}

        def _f(key: str) -> float:
            try:
                return float(summary.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        # cash_total_krw (dnca_tot_amt) = total KRW deposit, not gross account value.
        # d2_deposit_krw (prvs_rcdl_excc_amt) is the *previous-day settlement amount*,
        # so use it only if the primary cash field is missing.
        cash_krw = _f("cash_total_krw") or _f("d2_deposit_krw")
        gross_domestic_krw = _f("total_evaluation_krw") or _f("tot_evlu_amt")

        # Domestic KR-listed stock evaluation is already KRW. Some snapshots carry
        # only gross total plus cash, so derive the stock leg when needed.
        kr_stock_krw = _f("stock_evaluation_krw")
        if kr_stock_krw <= 0 and gross_domestic_krw > cash_krw:
            kr_stock_krw = gross_domestic_krw - cash_krw

        # Overseas stock equity: sum per-holding evaluation_amount (from output1).
        # output1 is genuinely per-holding, so summing avoids the triple-counting
        # that occurs with output2 summary fields, which are global totals repeated
        # identically for each exchange query. NASD, NYSE, AMEX all return the same
        # ovrs_stck_evlu_tota / frcr_dncl_amt, so summing output2 inflates by 3x).
        #
        # Overseas cash: take MAX of cash_balance_usd across exchanges because it is
        # a single global deposit figure, not a per-exchange split.
        ovrs_stock_usd = 0.0
        ovrs_cash_usd = 0.0
        if fx_rate > 0:
            overseas = snapshot.get("overseas")
            if isinstance(overseas, dict):
                for holding in overseas.get("holdings", []):
                    if not isinstance(holding, dict):
                        continue
                    try:
                        ovrs_stock_usd += float(holding.get("evaluation_amount") or 0)
                    except (TypeError, ValueError):
                        pass

                for exch_summary in overseas.get("summary_by_exchange", {}).values():
                    if not isinstance(exch_summary, dict):
                        continue
                    try:
                        v = float(exch_summary.get("cash_balance_usd") or 0)
                        if v > ovrs_cash_usd:
                            ovrs_cash_usd = v
                    except (TypeError, ValueError):
                        pass

                if ovrs_cash_usd == 0.0:
                    # Log raw summary fields to identify which field carries USD cash
                    raw_summaries = {
                        exch: s.get("raw_summary", {})
                        for exch, s in overseas.get("summary_by_exchange", {}).items()
                        if isinstance(s, dict)
                    }
                    import logging
                    logging.getLogger(__name__).debug(
                        "USD cash=0; overseas output2 raw_summary: %s", raw_summaries
                    )

        domestic_total_krw = gross_domestic_krw if gross_domestic_krw > 0 else cash_krw + kr_stock_krw
        total_krw = domestic_total_krw + (ovrs_stock_usd + ovrs_cash_usd) * fx_rate
        if total_krw <= 0:
            return None

        if return_breakdown:
            return {
                "total_krw": total_krw,
                "cash_krw": cash_krw,
                "kr_stock_krw": kr_stock_krw,
                "ovrs_stock_usd": ovrs_stock_usd,
                "ovrs_cash_usd": ovrs_cash_usd,
            }
        return total_krw
    def update_dashboard_summary(self, *args, force: bool = False) -> None:
        """Update the dashboard summary section."""
        is_manual = force
        if hasattr(self, "sender") and self.sender() is not None:
            sender = self.sender()
            try:
                txt = getattr(sender, "text", lambda: "")().lower()
                if "refresh" in txt or "run" in txt:
                    is_manual = True
            except Exception:
                pass
        
        if is_manual:
            self._cached_market_data_status = None

        symbols = [stock["symbol"] for stock in self.scanner_results]
        db_status = "enabled" if self.db_enabled else "disabled"
        market_data_status = self._format_market_data_status()

        buylist_lines = []
        if hasattr(self, "buylist_manager"):
            for env in ("PROD", "SIM"):
                env_items = [it for it in self.buylist_manager.items if it.environment == env]
                bought = [it for it in env_items if it.monitoring_status == "BOUGHT"]
                active = [it for it in env_items if it.monitoring_status == "ACTIVE"]
                if env_items:
                    syms = ", ".join(it.symbol for it in bought) if bought else "none"
                    buylist_lines.append(
                        f"Buylist {env}: {len(bought)}/5 positions ({syms})"
                        + (f", {len(active)} watching" if active else "")
                    )

        text = (
            f"Scanner yielded {len(self.scanner_results)} candidates.\n"
            f"Watchlist contains {len(self.watchlist.items)} symbols.\n"
            + ("\n".join(buylist_lines) + "\n" if buylist_lines else "")
            + f"Active trade plans: {len(self.trade_manager.get_active_plans())}.\n"
            f"MySQL cache: {db_status}.\n"
            f"Market data status: {market_data_status}.\n"
            f"Top scanner candidates: {', '.join(symbols[:5]) or 'None'}."
        )
        self.dashboard_summary_label.setText(text)
    def _format_market_data_status(self) -> str:
        if not self.db_enabled or self.db_engine is None:
            return "Unavailable"

        if getattr(self, "_cached_market_data_status", None) is not None:
            return self._cached_market_data_status

        try:
            latest_date = get_latest_price_history_date(self.db_engine)
            if latest_date is None:
                self._cached_market_data_status = "No cached data"
                return self._cached_market_data_status

            daily_status = self._format_market_data_status_from_date(latest_date)
            latest_hourly = get_latest_hourly_price_history_timestamp(self.db_engine)
            if latest_hourly is None:
                self._cached_market_data_status = f"Daily {daily_status}; 1H no cached data"
                return self._cached_market_data_status

            hourly_text = pd.Timestamp(latest_hourly).strftime("%Y-%m-%d %H:%M")
            self._cached_market_data_status = f"Daily {daily_status}; 1H latest {hourly_text} UTC"
            return self._cached_market_data_status
        except Exception:
            return "Unavailable"
    @staticmethod
    def _format_market_data_status_from_date(latest_date, now: Optional[dt.datetime] = None) -> str:
        latest_timestamp = pd.Timestamp(latest_date)
        if latest_timestamp.tzinfo is not None:
            latest_timestamp = latest_timestamp.tz_convert("UTC")

        latest_market_date = latest_timestamp.date()
        expected_date = DashboardMixin._expected_latest_market_data_date(now)
        latest_text = latest_market_date.strftime("%Y-%m-%d")
        expected_text = expected_date.strftime("%Y-%m-%d")
        if latest_market_date >= expected_date:
            return f"Up to date ({latest_text})"

        return f"Needs refresh ({latest_text}; expected {expected_text} after 7:00 AM KST)"
    @staticmethod
    def _expected_latest_market_data_date(now: Optional[dt.datetime] = None) -> dt.date:
        if now is None:
            kst_now = dt.datetime.now(KST_ZONE)
        elif now.tzinfo is None:
            kst_now = now.replace(tzinfo=KST_ZONE)
        else:
            kst_now = now.astimezone(KST_ZONE)

        candidate = kst_now.date() - dt.timedelta(days=1)
        if kst_now.time() < MARKET_DATA_READY_TIME_KST:
            candidate -= dt.timedelta(days=1)

        return DashboardMixin._previous_weekday(candidate)
    @staticmethod
    def _previous_weekday(day: dt.date) -> dt.date:
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        return day
    def run_single_stock_ai_analysis(self) -> None:
        """Run the new detailed single stock AI quantitative analysis."""
        selected_rows = self.watchlist_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select a stock from the watchlist to analyze.")
            return

        row = selected_rows[0].row()
        symbol_item = self.watchlist_table.item(row, 0)
        if symbol_item is None:
            return

        symbol = symbol_item.text().strip().upper()

        # Show sidebar and set loading state
        self.ai_sidebar.setVisible(True)
        self.ai_report_view.setHtml(f"<h3>Analyzing {symbol}...</h3><p>Running detailed quantitative swing-trading assessment. Please wait...</p>")

        # Create and start the worker thread for single stock analysis
        self.single_ai_worker = SingleStockAiWorker(symbol, self.watchlist.get(symbol), self.db_engine, self)
        self.single_ai_worker.finished_analysis.connect(self.on_single_stock_ai_finished)
        self.single_ai_worker.start()
    def on_single_stock_ai_finished(self, ai_res: dict) -> None:
        """Called when single stock AI analysis worker thread finishes."""
        if "error" in ai_res:
            self.ai_report_view.setHtml(f"<h3>Analysis Failed</h3><p>{ai_res['error']}</p>")
            return

        full_json = ai_res.get("full_json")
        if not full_json:
            self.ai_report_view.setHtml("<h3>Analysis Error</h3><p>Could not retrieve report data.</p>")
            return

        # Update the local watchlist dictionary and scores mapping
        symbol = full_json.get("symbol", "").upper().strip()
        item = self.watchlist.get(symbol)
        if item:
            item.ai_analysis = ai_res
            
        # Update self.watchlist_scores so the table row matches
        if not hasattr(self, "watchlist_scores"):
            self.watchlist_scores = {}
            
        # Map back to scoreboard structure expected by table formatter
        self.watchlist_scores[symbol] = {
            "price": full_json.get("risk_assessment", {}).get("entry_price", 0.0),
            "total_score": full_json.get("total_score", 0),
            "status": full_json.get("decision", "WATCHING"),
            "rr": 0.0,
            "stop_adr": full_json.get("risk_assessment", {}).get("stop_distance_pct", 0.0),
            "risk_percent": full_json.get("risk_assessment", {}).get("account_risk_pct", 0.0),
            "position_percent": full_json.get("risk_assessment", {}).get("position_size_pct", 0.0),
            "env": self.watchlist_env_combo.currentText() if hasattr(self, "watchlist_env_combo") else "SIM"
        }

        # Format the html report
        from src.core.scoring import render_quant_analysis_html
        html = render_quant_analysis_html(full_json)
        self.ai_report_view.setHtml(html)
        
        # Redraw table and save state on main GUI thread safely
        self.populate_watchlist_table()
        self._save_state()
    def _score_growth_rank(self, stock: dict) -> float:
        return stock.get("growth_rank", 0.0) / 100.0
    def _score_trend_intensity(self, stock: dict) -> float:
        return stock.get("trend_intensity", 0.0) / 100.0
    def _score_adr(self, stock: dict) -> float:
        return stock.get("adr", 0.0) / 5.0
