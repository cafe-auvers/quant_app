from __future__ import annotations

import datetime as dt
import html
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtCore import Qt, QThread, QTimer, QUrl
from PyQt5.QtGui import QColor, QDoubleValidator, QKeySequence
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QDockWidget, QFormLayout,
                             QGroupBox, QHBoxLayout, QHeaderView,
                             QKeySequenceEdit, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMenu, QMessageBox, QProgressBar,
                             QPushButton, QScrollArea, QShortcut, QSizePolicy,
                             QSlider, QSpinBox, QSplitter, QTableWidget,
                             QTableWidgetItem, QTabWidget, QTextBrowser,
                             QTextEdit, QVBoxLayout, QWidget)

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
from src.core.orb import (calculate_orb_range, evaluate_orb_entry_signal,
                          resample_intraday_bars)
from src.core.order_state import (OPEN_ORDER_STATUSES, BrokerOrder,
                                  OrderIntent, OrderSide, OrderStatus)
from src.core.scanner import ComparisonOperator, ScanRule, StockScanner
from src.core.trade_reviewer import TradeReviewer, TradeSetup
from src.core.watchlist import (BuylistItem, BuylistManager, TradePlan,
                                TradePlanManager, Watchlist)
from src.infrastructure.database.repositories.market_bars import \
    get_latest_hourly_price_history_timestamp
from src.infrastructure.database.repositories.market_watermarks import \
    get_latest_price_history_date
from src.risk.position_sizer import PositionSizer
from src.services.app_state import (SCANNER_SETUPS_FILE, SETTINGS_FILE,
                                    load_buylist_state,
                                    load_chart_drawings_state,
                                    load_scanner_setups_state,
                                    load_tab_options_state,
                                    load_trade_plans_state,
                                    load_watchlist_state, save_app_state)
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
from src.ui.workers import (FxRateWorker, IntradayBulkFetchWorker,
                            IntradayFetchWorker, KisAccountWorker,
                            KisOrderWorker, KisStartupAccountsWorker,
                            OrderReconciliationWorker, ScannerWorker,
                            SingleStockAiWorker, WatchlistAiWorker)
from src.utils.data_loader import (_extract_symbol_history,
                                   download_price_history,
                                   get_default_universe)
from src.utils.intraday_helpers import \
    extract_latest_opening_bar as _extract_latest_opening_bar
from src.utils.intraday_helpers import intraday_cache_needs_backfill
from src.utils.intraday_helpers import utcnow_naive as _utcnow_naive
from src.utils.market_calendar import (expected_latest_market_data_date,
                                       previous_nyse_trading_day)
from src.utils.storage import load_json, save_json

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
        self.kis_environment_combo.addItem(KisEnvironment.PROD.value)
        self.kis_environment_combo.currentTextChanged.connect(
            self.populate_kis_account_combo
        )
        self.kis_environment_combo.setVisible(False)
        kis_form.addRow("Profile", QLabel("PROD — Live Trading"))

        self.kis_account_combo = QComboBox()
        self.kis_account_combo.currentIndexChanged.connect(
            self.update_kis_account_status
        )
        kis_form.addRow("Account", self.kis_account_combo)
        # This one account selector also drives trade sizing and order routing
        # (Watchlist ORB panel, Buy Dashboard, real KIS order submission) — it
        # used to be a second, independently-selectable combo on the Watchlist
        # tab (trade_kis_account_combo). Aliasing removes the risk of viewing
        # one account here while orders silently route to a different one.
        self.trade_kis_account_combo = self.kis_account_combo
        self.kis_account_combo.currentIndexChanged.connect(
            self.apply_cached_trade_account_size
        )

        kis_options_layout = QHBoxLayout()
        self.kis_domestic_checkbox = QCheckBox("Domestic")
        self.kis_domestic_checkbox.setChecked(True)
        self.kis_overseas_checkbox = QCheckBox("Overseas")
        self.kis_overseas_checkbox.setChecked(True)
        kis_options_layout.addWidget(self.kis_domestic_checkbox)
        kis_options_layout.addWidget(self.kis_overseas_checkbox)
        kis_options_layout.addStretch()
        kis_form.addRow("Sections", kis_options_layout)

        # Trade sizing inputs — used by the Watchlist ORB Position Plan panel
        # and Buy Dashboard for share/capital-% math. Not shown in the UI:
        # the "Total (est.)" line in kis_account_summary_label already
        # displays this same USD figure and the USD/KRW rate together, and
        # both are auto-populated (account combo change / "Use KIS Balance" /
        # startup preload) — there's nothing to manually type here. The
        # widgets still have to exist though: ~10 call sites across
        # watchlist/buylist/scanner read account_size_input directly for
        # real position sizing and order routing.
        self.account_size_input = QLineEdit("100000")
        account_size_validator = QDoubleValidator(
            0.0, 1_000_000_000_000.0, 2, self.account_size_input
        )
        account_size_validator.setNotation(QDoubleValidator.StandardNotation)
        self.account_size_input.setValidator(account_size_validator)
        self.account_size_input.setVisible(False)

        self.usd_krw_rate_input = QLineEdit()
        self.usd_krw_rate_input.setReadOnly(True)
        self.usd_krw_rate_input.setVisible(False)
        # "Use KIS Balance" (below) already triggers a fresh USD/KRW fetch
        # after loading the snapshot, so a standalone Refresh FX trigger
        # isn't needed as a separate visible control.
        self.usd_krw_rate_refresh_button = QPushButton("Refresh FX")
        self.usd_krw_rate_refresh_button.clicked.connect(
            lambda: self.refresh_usd_krw_rate(show_messages=True)
        )
        self.usd_krw_rate_refresh_button.setVisible(False)
        self.usd_krw_rate_status_label = QLabel("USD/KRW not refreshed")
        self.usd_krw_rate_status_label.setVisible(False)

        kis_layout.addLayout(kis_form)

        self.account_size_input.textChanged.connect(self.on_account_size_text_changed)
        self.account_size_input.textChanged.connect(
            self.recalculate_watchlist_scoreboard_sizes
        )
        self.usd_krw_rate_input.textChanged.connect(
            self.apply_cached_trade_account_size
        )

        self.kis_account_status_label = QLabel()
        self.kis_account_status_label.setWordWrap(True)
        kis_layout.addWidget(self.kis_account_status_label)

        self.kis_account_summary_label = QLabel("No account snapshot loaded.")
        self.kis_account_summary_label.setWordWrap(True)
        kis_layout.addWidget(self.kis_account_summary_label)

        self.kis_holdings_table = QTableWidget(0, 7)
        self.kis_holdings_table.setHorizontalHeaderLabels(
            [
                "Symbol",
                "Name",
                "Qty",
                "Avg",
                "Price",
                "Eval",
                "P/L %",
            ]
        )
        self.kis_holdings_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.kis_holdings_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.kis_holdings_table.setMinimumHeight(180)
        kis_layout.addWidget(self.kis_holdings_table)

        kis_button_layout = QHBoxLayout()
        self.kis_refresh_button = QPushButton("Refresh KIS Snapshot")
        self.kis_refresh_button.setObjectName("kisRefreshButton")
        self.kis_refresh_button.setToolTip(
            "Fetches the account snapshot and refreshes USD/KRW, so position "
            "sizing (Account USD) is ready off this same click — no separate step."
        )
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

        self.pc_status_button = QPushButton("Wake PC")
        self.pc_status_button.setObjectName("pcStatusButton")
        self.pc_status_button.clicked.connect(self._on_pc_status_button_clicked)
        button_layout.addWidget(self.pc_status_button)

        refresh_button = QPushButton("Refresh Summary")
        refresh_button.setObjectName("refreshSummaryButton")
        refresh_button.clicked.connect(self._refresh_dashboard_summary_manually)
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
        self.refresh_intraday_button.clicked.connect(
            self.refresh_watchlist_intraday_cache
        )
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
        self.live_refresh_minutes_spin.valueChanged.connect(
            self._on_live_refresh_interval_changed
        )
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

        environment = (
            self.kis_environment_combo.currentText()
            if hasattr(self, "kis_environment_combo")
            else "PROD"
        )
        current_account = self.kis_account_combo.currentData()
        self.kis_account_combo.blockSignals(True)
        self.kis_account_combo.clear()

        profiles = [
            profile
            for profile in discover_account_profiles()
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
            self._on_live_refresh_interval_changed(
                self.live_refresh_minutes_spin.value()
            )
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
        if (
            not hasattr(self, "live_data_checkbox")
            or not self.live_data_checkbox.isChecked()
        ):
            return
        if not self._is_us_regular_market_open():
            self.live_data_status_label.setText(
                "Live data: waiting for U.S. market hours"
            )
            return
        if (
            self.intraday_bulk_worker is not None
            and self.intraday_bulk_worker.isRunning()
        ):
            self.live_data_status_label.setText("Live data: refresh already running")
            return

        self.live_data_status_label.setText("Live data: refreshing watchlist")
        self.refresh_watchlist_intraday_cache(
            show_messages=False, triggered_by_live=True
        )

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
        if not hasattr(self, "kis_environment_combo") or not hasattr(
            self, "kis_account_combo"
        ):
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

        environment = (
            self.kis_environment_combo.currentText()
            if hasattr(self, "kis_environment_combo")
            else "PROD"
        )
        profile = (
            self.kis_account_combo.currentData()
            if hasattr(self, "kis_account_combo")
            else None
        )
        if not profile:
            self.kis_account_status_label.setText(
                f"{environment} credentials can use configured API keys, but no account number is configured. "
                "KIS balance APIs require an account number; add KIS_PROD_ACCOUNT_NO or KIS_PROD_ACCOUNTS to .env."
            )
            return

        try:
            config = load_config(
                KisEnvironment(environment),
                account_no_override=profile.get("account_no"),
            )
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
        """Fetch all configured production account snapshots once at startup."""
        if self.kis_startup_worker is not None and self.kis_startup_worker.isRunning():
            return
        profiles = discover_account_profiles()
        if not profiles:
            self.append_log("Startup KIS preload skipped: no configured PROD accounts.")
            return
        if self.kis_account_worker is not None and self.kis_account_worker.isRunning():
            self.append_log(
                "Startup KIS preload skipped: manual KIS refresh is already running."
            )
            return

        self.append_log(
            f"Starting startup KIS preload for {len(profiles)} configured account(s)."
        )
        if hasattr(self, "kis_account_status_label"):
            self.kis_account_status_label.setText("Startup KIS preload running...")
        self.kis_startup_worker = KisStartupAccountsWorker(profiles)
        self.kis_startup_worker.log_message.connect(self.append_log)
        self.kis_startup_worker.finished_profiles.connect(
            self._on_startup_kis_accounts_finished
        )
        self._track_worker("kis_startup_worker", self.kis_startup_worker)
        self.kis_startup_worker.start()

    def _on_startup_kis_accounts_finished(self, snapshots: dict, errors: list) -> None:
        if snapshots:
            self._kis_api_last_success_at = dt.datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
        self._kis_api_last_error = (
            self._format_kis_error_message(str(errors[0])) if errors else ""
        )
        self.kis_account_snapshots.update(snapshots)
        self.sync_buylist_positions_from_kis_snapshots(snapshots)
        selected_profile = self._selected_dashboard_kis_profile()
        if selected_profile:
            selected_snapshot = self.kis_account_snapshots.get(
                (selected_profile["environment"], selected_profile["account_no"])
            )
            if selected_snapshot:
                fx = (
                    self._parse_float(self.usd_krw_rate_input, 0.0)
                    if hasattr(self, "usd_krw_rate_input")
                    else 0.0
                )
                self.kis_account_summary_label.setText(
                    self._format_kis_snapshot_summary(selected_snapshot, fx_rate=fx)
                )
                self.populate_kis_holdings_table(
                    self._flatten_kis_holdings(selected_snapshot)
                )
        if selected_profile:
            # Re-evaluate sizing immediately.  This deliberately clears any
            # stale/default USD value while FX is still unavailable; the FX
            # callback will populate the verified value once it succeeds.
            self.apply_cached_trade_account_size()
            self.refresh_usd_krw_rate(show_messages=False)
        else:
            self.apply_cached_trade_account_size()
        loaded_count = len(snapshots)
        if errors:
            self.append_log(
                f"Startup KIS preload loaded {loaded_count} account(s), {len(errors)} failed."
            )
            for error in errors[:5]:
                self.append_log(
                    f"Startup KIS preload failed: {self._format_kis_error_message(error)}"
                )
            if hasattr(self, "kis_account_status_label"):
                self.kis_account_status_label.setText(
                    f"Startup KIS preload loaded {loaded_count} account(s), {len(errors)} failed."
                )
        else:
            self.append_log(f"Startup KIS preload loaded {loaded_count} account(s).")
            if hasattr(self, "kis_account_status_label"):
                self.kis_account_status_label.setText(
                    f"Startup KIS preload loaded {loaded_count} account(s)."
                )

    def refresh_kis_account_snapshot(self) -> None:
        """Fetch the selected read-only KIS account snapshot in the background."""
        if self.kis_startup_worker is not None and self.kis_startup_worker.isRunning():
            QMessageBox.information(
                self,
                "KIS preload running",
                "Startup KIS account preload is still running.",
            )
            return
        if self.kis_account_worker is not None and self.kis_account_worker.isRunning():
            QMessageBox.information(
                self, "KIS refresh running", "A KIS account refresh is already running."
            )
            return

        include_domestic = self.kis_domestic_checkbox.isChecked()
        include_overseas = self.kis_overseas_checkbox.isChecked()
        if not include_domestic and not include_overseas:
            QMessageBox.warning(
                self, "No section selected", "Select Domestic, Overseas, or both."
            )
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
        self.kis_account_status_label.setText(
            f"Fetching {profile.get('label', environment)} account snapshot..."
        )
        requested_profile = dict(profile)
        self.kis_account_worker = KisAccountWorker(
            environment=environment,
            include_domestic=include_domestic,
            include_overseas=include_overseas,
            account_no=requested_profile.get("account_no"),
        )
        self.kis_account_worker.finished_snapshot.connect(
            lambda snapshot, requested=requested_profile: self._on_kis_snapshot_finished(
                snapshot, requested
            )
        )
        self.kis_account_worker.error_occurred.connect(
            lambda error, requested=requested_profile: self._on_kis_snapshot_error(
                error, requested
            )
        )
        self._track_worker("kis_account_worker", self.kis_account_worker)
        self.kis_account_worker.start()

    def _on_kis_snapshot_finished(
        self, snapshot: dict, requested_profile: Optional[dict] = None
    ) -> None:
        self._kis_api_last_success_at = dt.datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        self._kis_api_last_error = ""
        self._schedule_kis_refresh_button_enable()
        # The combo can change while the worker is in flight.  Store and sync
        # under the profile that made the request, not the current selection.
        profile = requested_profile or self._selected_dashboard_kis_profile()
        if profile:
            environment = str(profile.get("environment") or "").upper()
            account_no = str(profile.get("account_no") or "")
            self.kis_account_snapshots[
                (environment, account_no)
            ] = snapshot
            self.sync_buylist_positions_from_kis_snapshots(
                {(environment, account_no): snapshot}
            )
        current_profile_fn = getattr(self, "_selected_dashboard_kis_profile", None)
        current_profile = current_profile_fn() if callable(current_profile_fn) else None
        current_key = (
            (
                str(current_profile.get("environment") or "").upper(),
                str(current_profile.get("account_no") or ""),
            )
            if isinstance(current_profile, dict)
            else None
        )
        requested_key = (
            (environment, account_no) if profile else None
        )
        if requested_key is not None and current_key not in {None, requested_key}:
            label = str(profile.get("label") or account_no)
            self.kis_account_status_label.setText(
                f"KIS snapshot loaded for {label}; current selection was not changed."
            )
            self.append_log(f"Loaded KIS account snapshot for {label}.")
        else:
            self.kis_account_status_label.setText("KIS account snapshot loaded.")
            fx = (
                self._parse_float(self.usd_krw_rate_input, 0.0)
                if hasattr(self, "usd_krw_rate_input")
                else 0.0
            )
            self.kis_account_summary_label.setText(
                self._format_kis_snapshot_summary(snapshot, fx_rate=fx)
            )
            self.populate_kis_holdings_table(self._flatten_kis_holdings(snapshot))
            # Invalidate/recalculate sizing before the asynchronous FX refresh
            # so an old account value cannot remain actionable while it runs.
            self.apply_cached_trade_account_size()
            # Refresh USD/KRW too, so position sizing (account_size_input) is
            # ready off this same snapshot without a separate step.  The FX
            # completion callback recalculates with the verified rate.
            self.refresh_usd_krw_rate(show_messages=False)
            self.append_log("Loaded KIS account snapshot.")
        self.reconcile_open_orders()

    def _on_kis_snapshot_error(
        self, error_message: str, requested_profile: Optional[dict] = None
    ) -> None:
        self._schedule_kis_refresh_button_enable()
        friendly_message = self._format_kis_error_message(error_message)
        self._kis_api_last_error = friendly_message
        profile_label = (
            str(requested_profile.get("label") or "")
            if isinstance(requested_profile, dict)
            else ""
        )
        context = f" for {profile_label}" if profile_label else ""
        self.kis_account_status_label.setText(
            f"KIS account snapshot failed{context}: {friendly_message}"
        )
        self.append_log(f"KIS account snapshot failed{context}: {friendly_message}")

    def _schedule_kis_refresh_button_enable(self) -> None:
        if not hasattr(self, "kis_refresh_button"):
            return
        self.kis_refresh_button.setEnabled(False)
        QTimer.singleShot(3000, lambda: self.kis_refresh_button.setEnabled(True))

    @staticmethod
    def _format_kis_error_message(error_message: str) -> str:
        if (
            "rate limit" in error_message.lower()
            or "EGW00201" in error_message
            or "EGW00215" in error_message
        ):
            return (
                "KIS rate limit exceeded. Wait a few seconds before refreshing again."
            )
        if (
            "INVALID_CHECK_ACNO" in error_message
            or "account number/product code" in error_message
        ):
            return (
                "KIS rejected the selected account number/product code. "
                "Check the PROD account number and product code in .env."
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
    def _format_kis_snapshot_summary(
        snapshot: Dict[str, Any], fx_rate: float = 0.0
    ) -> str:
        """Format a human-readable KIS account snapshot summary.

        Cash/stock/total figures come from _extract_kis_account_value_krw —
        the same function apply_cached_trade_account_size() uses to set
        Account USD — so "Total (est.)" below is guaranteed to be the exact
        number driving position sizing, not a second, independently
        maintained calculation that could quietly drift from it. Only P/L
        and holding-count stats (irrelevant to sizing) are computed locally.
        Stays a staticmethod (calls the other by class name) so it remains
        callable without a MainWindow instance, e.g. in tests.
        """
        try:
            fx_rate = float(fx_rate)
        except (TypeError, ValueError, OverflowError):
            fx_rate = 0.0
        if not math.isfinite(fx_rate) or fx_rate <= 0:
            fx_rate = 0.0

        env = snapshot.get("environment", "")
        acct = snapshot.get("account", "")
        fetched = snapshot.get("fetched_at", "")
        parts = [f"Fetched: {fetched}  |  Profile: {env}  |  Account: {acct}"]

        breakdown = (
            DashboardMixin._extract_kis_account_value_krw(
                snapshot, fx_rate=fx_rate, return_breakdown=True
            )
            or {}
        )
        cash_krw = breakdown.get("cash_krw", 0.0)
        kr_stock_krw = breakdown.get("kr_stock_krw", 0.0)
        ovrs_stock_usd = breakdown.get("ovrs_stock_usd", 0.0)
        ovrs_cash_usd = breakdown.get("ovrs_cash_usd", 0.0)  # frcr fallback already folded in
        frcr_evlu_tota_krw = breakdown.get("frcr_evlu_tota_krw", 0.0)
        total_krw = breakdown.get("total_krw", 0.0)

        # P/L and holding-count stats — display-only, not part of sizing math
        kr_pnl_krw = 0.0
        domestic = snapshot.get("domestic")
        if isinstance(domestic, dict):
            try:
                kr_pnl_krw = float(
                    domestic.get("summary", {}).get("evaluation_profit_loss_krw") or 0
                )
            except (TypeError, ValueError):
                kr_pnl_krw = 0.0

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
                        ovrs_pnl_usd += float(h.get("profit_loss") or 0)
                    except (TypeError, ValueError):
                        pass

        # Breakdown line
        pnl_sign = "+" if ovrs_pnl_usd >= 0 else ""
        parts.append(
            f"KRW cash: {cash_krw:,.0f}  |  KR stocks: {kr_stock_krw:,.0f}  |  "
            f"US stocks: ${ovrs_stock_usd:,.2f} ({ovrs_count} holding(s), P/L {pnl_sign}${ovrs_pnl_usd:,.2f})  |  "
            f"USD cash: ${ovrs_cash_usd:,.2f}"
        )
        # Cash summary — KRW deposit + USD assets
        cash_line = f"Cash: {cash_krw:,.0f} KRW"
        if ovrs_cash_usd > 0:
            note = (
                " (frcr, pre-settlement)"
                if frcr_evlu_tota_krw > 0 and ovrs_stock_usd == 0
                else ""
            )
            cash_line += f"  +  ${ovrs_cash_usd:,.2f} USD{note}"
        parts.append(cash_line)
        parts.append(f"Overseas: {ovrs_count} holdings loaded.")
        if kr_stock_krw > 0 and kr_pnl_krw != 0:
            kr_pnl_sign = "+" if kr_pnl_krw >= 0 else ""
            parts.append(f"KR stock P/L: {kr_pnl_sign}{kr_pnl_krw:,.0f} KRW")

        # Total in KRW and USD if FX rate is known — identical figure to
        # what Account USD (position sizing) uses.
        if fx_rate > 0 and total_krw > 0:
            total_usd = total_krw / fx_rate
            parts.append(
                f"Total (est.): {total_krw:,.0f} KRW = ${total_usd:,.2f} USD  @ {fx_rate:.2f} KRW/USD"
            )
        elif fx_rate <= 0:
            parts.append("Position sizing: unavailable until USD/KRW is refreshed.")
        else:
            parts.append("Position sizing: unavailable because account value is invalid.")

        return "\n".join(parts)

    @staticmethod
    def _flatten_kis_holdings(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        holdings: List[Dict[str, Any]] = []
        domestic = snapshot.get("domestic")
        if isinstance(domestic, dict):
            holdings.extend(
                item for item in domestic.get("holdings", []) if isinstance(item, dict)
            )

        overseas = snapshot.get("overseas")
        if isinstance(overseas, dict):
            holdings.extend(
                item for item in overseas.get("holdings", []) if isinstance(item, dict)
            )
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

        environment = (
            self.trade_kis_environment_combo.currentText()
            if hasattr(self, "trade_kis_environment_combo")
            else "PROD"
        )
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

    def _on_trade_account_snapshot_finished(
        self, snapshot: dict, requested_profile: Optional[dict] = None
    ) -> None:
        self._kis_api_last_success_at = dt.datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        self._kis_api_last_error = ""
        # Use the request's profile.  A user can change the sizing account
        # before the background request returns.
        profile = requested_profile or (
            self.trade_kis_account_combo.currentData()
            if hasattr(self, "trade_kis_account_combo")
            else None
        )
        environment = str(
            (profile or {}).get("environment")
            or (
                self.trade_kis_environment_combo.currentText()
                if hasattr(self, "trade_kis_environment_combo")
                else ""
            )
        ).upper()
        if profile:
            self.kis_account_snapshots[(environment, profile.get("account_no", ""))] = (
                snapshot
            )
            self.sync_buylist_positions_from_kis_snapshots(
                {(environment, profile.get("account_no", "")): snapshot}
            )
        # Clear/recalculate against the newly stored snapshot before starting
        # the asynchronous FX refresh.  This prevents a previous account's
        # value from remaining active if that refresh fails.
        self.apply_cached_trade_account_size()
        self.refresh_usd_krw_rate(show_messages=False)
        label = str(profile.get("label") or environment) if profile else environment
        self.append_log(f"Loaded KIS account value for {label} trade sizing.")
        self.reconcile_open_orders()

    def _on_trade_account_snapshot_error(
        self, error_message: str, requested_profile: Optional[dict] = None
    ) -> None:
        friendly_message = self._format_kis_error_message(error_message)
        self._kis_api_last_error = friendly_message
        profile_label = (
            str(requested_profile.get("label") or "")
            if isinstance(requested_profile, dict)
            else ""
        )
        context = f" for {profile_label}" if profile_label else ""
        self.append_log(f"KIS account value failed{context}: {friendly_message}")

    def refresh_usd_krw_rate(self, show_messages: bool = True) -> None:
        if self.fx_rate_worker is not None and self.fx_rate_worker.isRunning():
            if show_messages:
                self.append_log("USD/KRW refresh is already running.")
            return
        snapshot = self._selected_trade_account_snapshot()
        if show_messages:
            self.append_log(
                "Refreshing USD/KRW rate from KIS snapshot, yfinance fallback..."
            )
        self._set_usd_krw_rate_status("USD/KRW: refreshing...")
        self.fx_rate_worker = FxRateWorker(snapshot=snapshot)
        self.fx_rate_worker.finished_rate.connect(self._on_usd_krw_rate_finished)
        self.fx_rate_worker.error_occurred.connect(self._on_usd_krw_rate_error)
        self._track_worker("fx_rate_worker", self.fx_rate_worker)
        self.fx_rate_worker.start()

    def _selected_trade_account_snapshot(self) -> Optional[dict]:
        if not hasattr(self, "trade_kis_account_combo") or not hasattr(
            self, "trade_kis_environment_combo"
        ):
            return None
        profile = self.trade_kis_account_combo.currentData()
        if not profile:
            return None
        environment = self.trade_kis_environment_combo.currentText()
        return self.kis_account_snapshots.get(
            (environment, profile.get("account_no", ""))
        )

    def _on_usd_krw_rate_finished(
        self, rate: float, source: str, timestamp: str
    ) -> None:
        self.usd_krw_rate_source = source
        if hasattr(self, "usd_krw_rate_input"):
            old_block = self.usd_krw_rate_input.blockSignals(True)
            self.usd_krw_rate_input.setText(f"{rate:.2f}")
            self.usd_krw_rate_input.blockSignals(old_block)
        self._set_usd_krw_rate_status(f"USD/KRW {rate:.2f} from {source} ({timestamp})")
        self.append_log(f"USD/KRW updated: {rate:.2f} from {source}.")
        self._refresh_dashboard_snapshot_fx(rate)
        self.apply_cached_trade_account_size()

    def _refresh_dashboard_snapshot_fx(self, rate: float) -> None:
        """Re-render the selected Dashboard snapshot with the latest live FX rate."""
        selected_profile = self._selected_dashboard_kis_profile()
        summary_label = self.__dict__.get("kis_account_summary_label")
        if not selected_profile or summary_label is None:
            return
        key = (
            str(selected_profile.get("environment") or "").upper(),
            str(selected_profile.get("account_no") or ""),
        )
        snapshot = self.kis_account_snapshots.get(key)
        if snapshot:
            summary_label.setText(
                self._format_kis_snapshot_summary(snapshot, fx_rate=rate)
            )

    def _on_usd_krw_rate_error(self, error_message: str) -> None:
        current_rate = (
            self._parse_float(self.usd_krw_rate_input, 0.0)
            if hasattr(self, "usd_krw_rate_input")
            else 0.0
        )
        if current_rate > 0:
            self._set_usd_krw_rate_status(
                f"USD/KRW refresh failed; keeping {current_rate:.2f}"
            )
        else:
            self._set_usd_krw_rate_status("USD/KRW refresh failed")
        self.append_log(f"USD/KRW refresh failed: {error_message}")
        self.apply_cached_trade_account_size()

    def _set_usd_krw_rate_status(self, text: str) -> None:
        label = self.__dict__.get("usd_krw_rate_status_label")
        if label is not None:
            label.setText(text)

    def apply_cached_trade_account_size(self, *args) -> None:
        if not hasattr(self, "trade_kis_account_combo") or not hasattr(
            self, "account_size_input"
        ):
            return

        # trade_kis_environment_combo is built later, in _build_watchlist_tab
        # (Dashboard is built first). This can fire during Dashboard's own
        # startup population, before that widget exists yet.
        environment = (
            self.trade_kis_environment_combo.currentText()
            if hasattr(self, "trade_kis_environment_combo")
            else "PROD"
        )
        profile = self.trade_kis_account_combo.currentData()
        snapshot = None
        fallback_reason = "no KIS profile selected"

        if profile:
            account_no = profile.get("account_no", "")
            snapshot = self.kis_account_snapshots.get((environment, account_no))
            if snapshot is None:
                fallback_reason = (
                    f"snapshot not loaded for ({environment}, {account_no})"
                )

        if snapshot is not None:
            usd_krw_rate = self._parse_float(self.usd_krw_rate_input, 0.0)
            if usd_krw_rate <= 0:
                fallback_reason = "live USD/KRW rate is unavailable"
            else:
                breakdown = self._extract_kis_account_value_krw(
                    snapshot,
                    fx_rate=usd_krw_rate,
                    return_breakdown=True,
                )
                if breakdown:
                    account_value_krw = breakdown["total_krw"]
                    account_value_usd = account_value_krw / usd_krw_rate
                    if profile:
                        # buydashboard_to_kanban.md P0-1: the Kanban entry
                        # engine's buying_power_provider must reflect real,
                        # recently-refreshed KIS state, never a manual
                        # figure -- record it here, the same place/moment
                        # the legacy dashboard's own account-size field is
                        # refreshed, so the new engine and the legacy
                        # dashboard are never sized off different data.
                        # ovrs_cash_usd is *usable cash* (excludes existing
                        # stock holdings' value); account_value_usd is the
                        # full-equity risk-sizing base.
                        from src.services import buying_power_cache

                        buying_power_cache.record_snapshot(
                            environment=environment,
                            account_no=account_no,
                            usable_buying_power_usd=breakdown["ovrs_cash_usd"],
                            total_equity_usd=account_value_usd,
                            source="kis_account_snapshot",
                        )
                    old_block = self.account_size_input.blockSignals(True)
                    self.account_size_input.setText(f"{account_value_usd:.2f}")
                    self.account_size_input.blockSignals(old_block)
                    ovrs_cash = breakdown["ovrs_cash_usd"]
                    ovrs_stock = breakdown["ovrs_stock_usd"]
                    frcr_krw = breakdown.get("frcr_evlu_tota_krw", 0.0)
                    frcr_note = (
                        f" [via frcr_evlu_tota {frcr_krw:,.0f} KRW — pre-settlement]"
                        if frcr_krw > 0 and ovrs_stock == 0
                        else ""
                    )
                    self.append_log(
                        f"Using {environment} account value: {account_value_krw:,.0f} KRW "
                        f"= {account_value_usd:,.2f} USD "
                        f"[KRW cash: {breakdown['cash_krw']:,.0f} | "
                        f"KR stocks: {breakdown['kr_stock_krw']:,.0f} | "
                        f"US stocks: ${ovrs_stock:,.2f} | "
                        f"USD cash: ${ovrs_cash:,.2f}]{frcr_note}"
                    )
                    self.update_trade_plan_feedback()
                    self.recalculate_watchlist_scoreboard_sizes()  # also refreshes ORB panel
                    if hasattr(self, "refresh_execution_queue"):
                        self.refresh_execution_queue(environment, show_log=False)
                    return
                fallback_reason = "account value is zero or invalid in snapshot"

        if profile:
            # A configured live account must never fall back to a hidden manual
            # or hard-coded balance.  Empty text parses as zero throughout the
            # sizing pipeline and makes execution-queue candidates unavailable.
            old_block = self.account_size_input.blockSignals(True)
            self.account_size_input.setText("")
            self.account_size_input.blockSignals(old_block)
            self.append_log(
                f"KIS position sizing unavailable for {environment}: {fallback_reason}."
            )
            self.update_trade_plan_feedback()
            self.recalculate_watchlist_scoreboard_sizes()
            if hasattr(self, "refresh_execution_queue"):
                self.refresh_execution_queue(environment, show_log=False)
            return

        # Fallback if no profile, no snapshot, or account value is invalid
        if not hasattr(self, "manual_account_sizes"):
            self.manual_account_sizes = {"PROD": 10000.0}
        default_val = self.manual_account_sizes.get(environment, 10000.0)

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
        if not hasattr(self, "trade_kis_environment_combo") or not hasattr(
            self, "account_size_input"
        ):
            return
        env = self.trade_kis_environment_combo.currentText()
        val = self._parse_float(self.account_size_input, 0.0)
        if val > 0:
            if not hasattr(self, "manual_account_sizes"):
                self.manual_account_sizes = {"PROD": 10000.0}
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

        def _number(value: Any) -> float:
            try:
                number = float(value or 0)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return number if math.isfinite(number) else 0.0

        def _f(key: str) -> float:
            return _number(summary.get(key))

        try:
            fx_rate = float(fx_rate)
        except (TypeError, ValueError, OverflowError):
            fx_rate = 0.0
        if not math.isfinite(fx_rate) or fx_rate <= 0:
            fx_rate = 0.0

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
        summary_stock_usd = 0.0
        # CTRP6548R reports the broker's KRW-valued foreign-assets subtotal
        # and whole-account total.  Unlike TTTS3012R output2, these include
        # both overseas holdings and foreign-currency cash.
        frcr_evlu_tota_krw = 0.0
        broker_total_assets_krw = 0.0
        if fx_rate > 0:
            overseas = snapshot.get("overseas")
            if isinstance(overseas, dict):
                frcr_evlu_tota_krw = _number(overseas.get("frcr_evlu_tota_krw"))
                broker_total_assets_krw = _number(overseas.get("tot_asst_krw"))

                for holding in overseas.get("holdings", []):
                    if not isinstance(holding, dict):
                        continue
                    holding_value = _number(holding.get("evaluation_amount"))
                    if holding_value <= 0:
                        quantity = _number(holding.get("quantity"))
                        current_price = _number(holding.get("current_price"))
                        holding_value = quantity * current_price
                    if holding_value > 0:
                        ovrs_stock_usd += holding_value

                for exch_summary in overseas.get("summary_by_exchange", {}).values():
                    if not isinstance(exch_summary, dict):
                        continue
                    value = _number(exch_summary.get("cash_balance_usd"))
                    if value > ovrs_cash_usd:
                        ovrs_cash_usd = value
                    # Summary totals may be repeated by several exchange
                    # queries, so MAX is safe while SUM can triple-count.
                    stock_value = _number(
                        exch_summary.get("foreign_stock_evaluation")
                    )
                    if stock_value > summary_stock_usd:
                        summary_stock_usd = stock_value

                if ovrs_stock_usd <= 0:
                    ovrs_stock_usd = summary_stock_usd

                if (
                    ovrs_cash_usd == 0.0
                    and ovrs_stock_usd == 0.0
                    and frcr_evlu_tota_krw == 0.0
                    and broker_total_assets_krw == 0.0
                ):
                    # Log raw summary fields to identify which field carries USD cash
                    raw_summaries = {
                        exch: s.get("raw_summary", {})
                        for exch, s in overseas.get("summary_by_exchange", {}).items()
                        if isinstance(s, dict)
                    }
                    import logging

                    logging.getLogger(__name__).warning(
                        "USD cash resolved to 0; overseas output2 raw_summary: %s",
                        raw_summaries,
                    )

        domestic_total_krw = (
            gross_domestic_krw if gross_domestic_krw > 0 else cash_krw + kr_stock_krw
        )

        component_overseas_krw = (ovrs_stock_usd + ovrs_cash_usd) * fx_rate
        # Prefer the broker's foreign-assets subtotal.  It captures US stocks
        # plus USD cash even though TTTS3012R commonly omits a cash field.  A
        # whole-account total is the final authority when present; otherwise
        # retain the component calculation for older/cached snapshots.
        overseas_total_krw = (
            frcr_evlu_tota_krw
            if frcr_evlu_tota_krw > 0
            else component_overseas_krw
        )
        if broker_total_assets_krw >= domestic_total_krw and broker_total_assets_krw > 0:
            total_krw = broker_total_assets_krw
            overseas_total_krw = max(0.0, total_krw - domestic_total_krw)
        else:
            total_krw = domestic_total_krw + overseas_total_krw

        # When TTTS3012R has no explicit cash field, derive the foreign cash
        # residual from CTRP6548R so the displayed breakdown reconciles to the
        # same entire-capital figure used by position sizing.
        if overseas_total_krw > 0:
            overseas_total_usd = overseas_total_krw / fx_rate
            derived_cash_usd = max(0.0, overseas_total_usd - ovrs_stock_usd)
            if ovrs_cash_usd <= 0 and derived_cash_usd > 0:
                ovrs_cash_usd = derived_cash_usd

        if not math.isfinite(total_krw) or total_krw <= 0:
            return None

        if return_breakdown:
            # Express frcr_evlu_tota_krw as USD for the log/display breakdown
            frcr_usd = (frcr_evlu_tota_krw / fx_rate) if fx_rate > 0 else 0.0
            return {
                "total_krw": total_krw,
                "cash_krw": cash_krw,
                "kr_stock_krw": kr_stock_krw,
                "ovrs_stock_usd": ovrs_stock_usd,
                # When no holding rows are available, the foreign subtotal is
                # necessarily unresolved between stock and cash.  Preserve the
                # legacy cash-like display rather than dropping it entirely.
                "ovrs_cash_usd": ovrs_cash_usd if ovrs_cash_usd > 0 else frcr_usd,
                "frcr_evlu_tota_krw": frcr_evlu_tota_krw,
                "broker_total_assets_krw": broker_total_assets_krw,
                "overseas_total_krw": overseas_total_krw,
            }
        return total_krw

    def _refresh_dashboard_summary_manually(self, *_signal_args) -> None:
        """Refresh user-requested data without consulting QObject.sender()."""
        self.update_dashboard_summary(force=True)

    def update_dashboard_summary(self, *_signal_args, force: bool = False) -> None:
        """Update the dashboard summary section."""
        is_manual = force

        if is_manual:
            self._cached_market_data_status = None

        symbols = [stock["symbol"] for stock in self.scanner_results]
        _db_source_labels = {"pc": "PC", "local_mirror": "local mirror"}
        db_status = (
            f"enabled ({_db_source_labels.get(getattr(self, 'db_engine_source', ''), 'unknown')})"
            if self.db_enabled
            else "disabled"
        )
        market_data_status = self._format_market_data_status()

        buylist_lines = []
        if hasattr(self, "buylist_manager"):
            env_items = [
                it for it in self.buylist_manager.items if it.environment == "PROD"
            ]
            bought = [it for it in env_items if it.monitoring_status == "BOUGHT"]
            active = [it for it in env_items if it.monitoring_status == "ACTIVE"]
            if env_items:
                syms = ", ".join(it.symbol for it in bought) if bought else "none"
                buylist_lines.append(
                    f"Buylist PROD: {len(bought)}/5 positions ({syms})"
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
                self._cached_market_data_status = (
                    f"Daily {daily_status}; 1H no cached data"
                )
                return self._cached_market_data_status

            hourly_text = pd.Timestamp(latest_hourly).strftime("%Y-%m-%d %H:%M")
            self._cached_market_data_status = (
                f"Daily {daily_status}; 1H latest {hourly_text} UTC"
            )
            return self._cached_market_data_status
        except Exception:
            return "Unavailable"

    @staticmethod
    def _format_market_data_status_from_date(
        latest_date, now: Optional[dt.datetime] = None
    ) -> str:
        latest_timestamp = pd.Timestamp(latest_date)
        if latest_timestamp.tzinfo is not None:
            latest_timestamp = latest_timestamp.tz_convert("UTC")

        latest_market_date = latest_timestamp.date()
        expected_date = DashboardMixin._expected_latest_market_data_date(now)
        latest_text = latest_market_date.strftime("%Y-%m-%d")
        expected_text = expected_date.strftime("%Y-%m-%d")
        if latest_market_date >= expected_date:
            return f"Up to date ({latest_text})"

        return (
            f"Needs refresh ({latest_text}; expected {expected_text} after 7:00 AM KST)"
        )

    @staticmethod
    def _expected_latest_market_data_date(now: Optional[dt.datetime] = None) -> dt.date:
        return expected_latest_market_data_date(now)

    @staticmethod
    def _previous_weekday(day: dt.date) -> dt.date:
        """Backward-compatible name for callers of the old dashboard helper."""
        return previous_nyse_trading_day(day)

    def run_single_stock_ai_analysis(self) -> None:
        """Run the new detailed single stock AI quantitative analysis."""
        existing_worker = self.__dict__.get("single_ai_worker")
        if existing_worker is not None and existing_worker.isRunning():
            QMessageBox.information(
                self,
                "Analysis running",
                "A single-stock AI analysis is already running. Wait for it to finish before starting another one.",
            )
            return
        selected_rows = self.watchlist_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(
                self,
                "No Selection",
                "Please select a stock from the watchlist to analyze.",
            )
            return

        row = selected_rows[0].row()
        symbol_item = self.watchlist_table.item(row, 0)
        if symbol_item is None:
            return

        symbol = symbol_item.text().strip().upper()

        # Show sidebar and set loading state
        self.ai_sidebar.setVisible(True)
        self.ai_report_view.setHtml(
            f"<h3>Analyzing {symbol}...</h3><p>Running detailed quantitative swing-trading assessment. Please wait...</p>"
        )

        # Create and start the worker thread for single stock analysis
        self.single_ai_worker = SingleStockAiWorker(
            symbol, self.watchlist.get(symbol), self.db_engine, self
        )
        self.single_ai_worker.finished_analysis.connect(
            self.on_single_stock_ai_finished
        )
        self._track_worker("single_ai_worker", self.single_ai_worker)
        self.single_ai_worker.start()

    def on_single_stock_ai_finished(self, ai_res: dict) -> None:
        """Called when single stock AI analysis worker thread finishes."""
        if "error" in ai_res:
            self.ai_report_view.setHtml(
                f"<h3>Analysis Failed</h3><p>{ai_res['error']}</p>"
            )
            return

        full_json = ai_res.get("full_json")
        if not full_json:
            self.ai_report_view.setHtml(
                "<h3>Analysis Error</h3><p>Could not retrieve report data.</p>"
            )
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
            "stop_adr": full_json.get("risk_assessment", {}).get(
                "stop_distance_pct", 0.0
            ),
            "risk_percent": full_json.get("risk_assessment", {}).get(
                "account_risk_pct", 0.0
            ),
            "position_percent": full_json.get("risk_assessment", {}).get(
                "position_size_pct", 0.0
            ),
            "env": (
                self.watchlist_env_combo.currentText()
                if hasattr(self, "watchlist_env_combo")
                else "PROD"
            ),
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
