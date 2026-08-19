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
from src.core.watchlist import (BuylistManager, TradePlan, TradePlanManager,
                                Watchlist)
from src.infrastructure.database.repositories.market_bars import \
    load_symbol_history_from_db
from src.risk.orb_position import (MAX_CAPITAL_PERCENT, MAX_STOP_ADR,
                                   MIN_CAPITAL_PERCENT, MIN_STOP_ADR,
                                   calculate_orb_position_values,
                                   is_orb_position_plan_valid,
                                   score_orb_position_recommendation)
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
from src.utils.config import DATA_DIR
from src.utils.data_loader import (_extract_symbol_history,
                                   download_price_history,
                                   get_default_universe)
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
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class WatchlistMixin:
    def _build_watchlist_tab(self) -> None:
        """Build content for the watchlist tab."""
        tab_layout = QVBoxLayout()
        tab_layout.setContentsMargins(0, 0, 0, 0)

        # Top-level splitter: Left side is controls and table; Right side is full-height AI analysis sidebar
        self.watchlist_splitter = QSplitter(Qt.Horizontal)

        # Left Panel (Controls + Table)
        left_panel = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(10, 10, 10, 10)

        # Environment combo kept for compatibility (buylist environment tagging
        # and other callers key off trade_kis_environment_combo). KIS account
        # selection, Account USD, and USD/KRW now live on the Dashboard tab
        # (see _build_dashboard_tab) — trade_kis_account_combo, account_size_input,
        # and usd_krw_rate_input are built there (Dashboard is built first in
        # _setup_tabs) and reused here under the same attribute names, so this
        # tab's sizing math is unchanged.
        self.watchlist_env_combo = QComboBox()
        self.watchlist_env_combo.addItem("PROD")
        self.watchlist_env_combo.setVisible(False)
        # trade_kis_environment_combo is the same widget — no separate combo needed
        self.trade_kis_environment_combo = self.watchlist_env_combo

        sizing_layout = QHBoxLayout()
        sizing_layout.addWidget(QLabel("Buffer %:"))
        self.watchlist_buffer_pct_input = QLineEdit("0.10")
        buffer_validator = QDoubleValidator(
            0.0, 100.0, 4, self.watchlist_buffer_pct_input
        )
        buffer_validator.setNotation(QDoubleValidator.StandardNotation)
        self.watchlist_buffer_pct_input.setValidator(buffer_validator)
        self.watchlist_buffer_pct_input.setMaximumWidth(50)
        self.watchlist_buffer_pct_input.setToolTip(
            "Small buffer above breakout_price to avoid false touches (default 0.10%). "
            "Global — applies the same to all stocks."
        )
        self.watchlist_buffer_pct_input.textChanged.connect(
            self._on_watchlist_orb_filter_changed
        )
        sizing_layout.addWidget(self.watchlist_buffer_pct_input)
        sizing_layout.addStretch()
        left_layout.addLayout(sizing_layout, 0)
        # risk_percent_input was removed entirely (2026-08-09): the ORB Position
        # Plan table already sweeps every risk case (0.25%-2%), and a selected
        # column's risk_percent is locked in and used for real order sizing
        # (see execution_queue.py: build_or_update_from_watchlist_item /
        # lock_risk_percent) — a single global override added no value. Every
        # read site (buylist_mixin.py, scanner_mixin.py, workers.py, and the
        # rest of this file) already guards with hasattr(self, "risk_percent_input")
        # and falls back to 1%, so no widget needs to exist.
        # account_size_input / usd_krw_rate_input / trade_kis_account_combo are
        # built on the Dashboard tab (_build_dashboard_tab) along with their
        # signal wiring — see the "KIS Account Snapshot" group there.
        # NOTE: initial population of trade_kis_account_combo happens in _setup_tabs via an
        # explicit populate_trade_account_combo() call after the signal wiring. Do NOT add a
        # duplicate currentTextChanged connection here.

        # Hidden inputs kept for compatibility with add_manual_watchlist_item callers
        self.watchlist_symbol_input = QLineEdit()
        self.watchlist_symbol_input.setVisible(False)
        self.watchlist_name_input = QLineEdit()
        self.watchlist_name_input.setVisible(False)
        self.watchlist_entry_input = QLineEdit()
        self.watchlist_entry_input.setVisible(False)

        # Watchlist Table
        self.watchlist_table = QTableWidget(0, 15)
        self.watchlist_table.setHorizontalHeaderLabels(
            [
                "Symbol",
                "Name",
                "Price",
                "Score",
                "Status",
                "Stop/ADR",
                "Risk %",
                "Capital %",
                "Trade Plan",
                "Env",
                "Entry Price",
                "Breakout Price",
                "Stop Loss",
                "Notes",
                "Added",
            ]
        )
        header = self.watchlist_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.watchlist_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.watchlist_table.cellDoubleClicked.connect(
            self.load_watchlist_item_to_trade_plan
        )
        self.watchlist_table.itemSelectionChanged.connect(
            self.on_watchlist_selection_changed
        )

        # ORB Position Plan panel — identical calculation to the Trade Plan tab
        orb_group = QGroupBox("ORB Position Plan")
        orb_group_layout = QVBoxLayout()
        orb_group_layout.setContentsMargins(5, 5, 5, 5)

        orb_header_layout = QHBoxLayout()
        self.watchlist_orb_symbol_label = QLabel(
            "Select a watchlist symbol to view its ORB plan"
        )
        self.watchlist_orb_symbol_label.setStyleSheet(
            "font-weight: bold; color: #aaaaaa;"
        )
        orb_header_layout.addWidget(self.watchlist_orb_symbol_label)
        orb_header_layout.addStretch()
        self.watchlist_orb_valid_only_checkbox = QCheckBox("Valid plans only")
        self.watchlist_orb_valid_only_checkbox.setChecked(True)
        self.watchlist_orb_valid_only_checkbox.stateChanged.connect(
            self._on_watchlist_orb_filter_changed
        )
        orb_header_layout.addWidget(self.watchlist_orb_valid_only_checkbox)
        orb_group_layout.addLayout(orb_header_layout)

        # Daily breakout price row — user enters the structural level from the daily chart
        orb_breakout_layout = QHBoxLayout()
        orb_breakout_layout.addWidget(QLabel("Daily Breakout $:"))
        self.watchlist_breakout_price_input = QLineEdit()
        breakout_validator = QDoubleValidator(
            0.0, 1_000_000_000.0, 6, self.watchlist_breakout_price_input
        )
        breakout_validator.setNotation(QDoubleValidator.StandardNotation)
        self.watchlist_breakout_price_input.setValidator(breakout_validator)
        self.watchlist_breakout_price_input.setPlaceholderText(
            "e.g. 123.45 — leave blank for ORB-only"
        )
        self.watchlist_breakout_price_input.setMaximumWidth(185)
        self.watchlist_breakout_price_input.textChanged.connect(
            self._on_watchlist_orb_filter_changed
        )
        orb_breakout_layout.addWidget(self.watchlist_breakout_price_input)
        orb_breakout_layout.addStretch()
        orb_group_layout.addLayout(orb_breakout_layout)

        self.watchlist_orb_table = QTableWidget(0, 10)
        self.watchlist_orb_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.watchlist_orb_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.watchlist_orb_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        orb_group_layout.addWidget(self.watchlist_orb_table)
        orb_group.setLayout(orb_group_layout)

        # Vertical splitter: watchlist table on top, ORB panel below
        watchlist_orb_splitter = QSplitter(Qt.Vertical)
        watchlist_orb_splitter.addWidget(self.watchlist_table)
        watchlist_orb_splitter.addWidget(orb_group)
        watchlist_orb_splitter.setSizes([350, 250])
        left_layout.addWidget(watchlist_orb_splitter, 1)

        # Bottom Buttons
        button_layout = QHBoxLayout()
        remove_button = QPushButton("Remove Selected")
        remove_button.setObjectName("removeSelectedButton")
        remove_button.clicked.connect(self.remove_selected_watchlist_item)
        button_layout.addWidget(remove_button)

        self.check_ai_button = QPushButton()  # Invisible dummy button for compatibility

        self.analyze_stock_ai_button = QPushButton("Analyze with AI")
        self.analyze_stock_ai_button.setObjectName("analyzeStockAiButton")
        self.analyze_stock_ai_button.clicked.connect(self.run_watchlist_ai_review)
        button_layout.addWidget(self.analyze_stock_ai_button)

        self.refresh_watchlist_orb_button = QPushButton("Refresh ORB Status")
        self.refresh_watchlist_orb_button.setObjectName("refreshWatchlistOrbButton")
        self.refresh_watchlist_orb_button.setToolTip(
            "Refresh intraday data and evaluate ORB entry status for every watchlist symbol"
        )
        self.refresh_watchlist_orb_button.clicked.connect(
            self.refresh_watchlist_orb_statuses_with_data
        )
        button_layout.addWidget(self.refresh_watchlist_orb_button)

        self.move_buylist_button = QPushButton("Move Selected to Queue")
        self.move_buylist_button.setObjectName("moveBuylistButton")
        self.move_buylist_button.clicked.connect(self.move_selected_to_buylist)
        self.move_buylist_button.setShortcut("B")
        self.move_buylist_button.setToolTip(
            "Move selected Watchlist symbol to the Buy Board Buylist column (shortcut: B)"
        )
        button_layout.addWidget(self.move_buylist_button)

        snapshot_button = QPushButton("Save Data Snapshot")
        snapshot_button.setObjectName("saveSnapshotButton")
        snapshot_button.setToolTip(
            "Save a JSON snapshot of the watchlist table and ORB plan for debugging"
        )
        snapshot_button.clicked.connect(self.save_watchlist_snapshot)
        button_layout.addWidget(snapshot_button)

        left_layout.addLayout(button_layout, 0)
        left_panel.setLayout(left_layout)

        # Add Left Panel to Splitter
        self.watchlist_splitter.addWidget(left_panel)

        # AI sidebar widget
        self.ai_sidebar = QWidget()
        self.ai_sidebar.setVisible(False)
        ai_sidebar_layout = QVBoxLayout()
        ai_sidebar_layout.setContentsMargins(10, 10, 10, 10)

        sidebar_header = QHBoxLayout()
        sidebar_title = QLabel("AI Quant Analysis")
        sidebar_title.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #ffffff;"
        )
        sidebar_header.addWidget(sidebar_title)
        sidebar_header.addStretch()

        close_sidebar_btn = QPushButton("X")
        close_sidebar_btn.setFixedSize(22, 22)
        close_sidebar_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #333333;
                color: #ffffff;
                border: none;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #555555;
            }
        """
        )
        close_sidebar_btn.clicked.connect(lambda: self.ai_sidebar.setVisible(False))
        sidebar_header.addWidget(close_sidebar_btn)
        ai_sidebar_layout.addLayout(sidebar_header)

        self.ai_report_view = QTextBrowser()
        self.ai_report_view.setOpenExternalLinks(True)
        self.ai_report_view.setStyleSheet(
            """
            QTextBrowser {
                background-color: #1e1e1e;
                color: #dcdcdc;
                border: 1px solid #333333;
                padding: 5px;
            }
        """
        )
        ai_sidebar_layout.addWidget(self.ai_report_view)
        self.ai_sidebar.setLayout(ai_sidebar_layout)

        self.watchlist_splitter.addWidget(self.ai_sidebar)
        self.watchlist_splitter.setSizes([850, 350])

        tab_layout.addWidget(self.watchlist_splitter)
        self.watchlist_widget.setLayout(tab_layout)
        # Keep window construction deterministic and offline-safe.  Live chart
        # fallback performs network I/O per symbol and must not block the Qt
        # event loop before the first window is shown.
        self.populate_watchlist_table(
            use_live_fallback=False, calculate_scores=False
        )

    def _get_account_balance_for_env(self, env: str) -> float:
        """Get the active account balance for the given environment.

        A configured KIS account is fail-closed: both its selected snapshot and
        a valid USD/KRW rate are required.  Manual/default values are available
        only when there is no configured account selection.
        """
        # Never let a stale widget value from another account outrank the
        # currently selected account's source data.
        if hasattr(self, "trade_kis_account_combo") and hasattr(
            self, "kis_account_snapshots"
        ):
            profile = self.trade_kis_account_combo.currentData()
            if profile:
                profile_env = str(profile.get("environment") or env).upper()
                if profile_env != str(env or "").upper():
                    return 0.0
                snapshot = self.kis_account_snapshots.get(
                    (env, profile.get("account_no", ""))
                )
                usd_krw_rate = (
                    self._parse_float(self.usd_krw_rate_input, 0.0)
                    if hasattr(self, "usd_krw_rate_input")
                    else 0.0
                )
                if snapshot is None or usd_krw_rate <= 0:
                    return 0.0
                account_value_krw = self._extract_kis_account_value_krw(
                    snapshot, fx_rate=usd_krw_rate
                )
                if account_value_krw and account_value_krw > 0:
                    return account_value_krw / usd_krw_rate
                return 0.0

        if hasattr(self, "account_size_input"):
            val = self._parse_float(self.account_size_input, 0.0)
            if val > 0:
                return val

        # Offline/manual planning fallback when no KIS account is selected.
        if hasattr(self, "manual_account_sizes"):
            val = self.manual_account_sizes.get(env, 0.0)
            if val > 0:
                return val

        return 10000.0 if env == "PROD" else 100000.0

    def _calculate_item_scores(self, item, *, use_live_fallback: bool = True) -> dict:
        """Calculate live trade plan and deterministic scores for a watchlist item.

        Sizing logic is identical to the Trade Plan ORB panel:
        - Saved plan  â†’ use plan's stored entry / stop / risk_percent directly.
        - Manual entry â†’ use item.entry_price / stop_loss, find best valid risk %.
        - ORB / Daily  â†’ derive entry & stop from ORB or ADR fallback, then find
                         best valid risk % using the same _orb_risk_cases iterator
                         and _orb_position_plan_is_valid / _score_orb_position_recommendation
                         that the Trade Plan tab uses.
        """
        import pandas as pd

        from src.core.orb import calculate_orb_range
        from src.core.scoring import calculate_deterministic_scores

        symbol = item.symbol.upper().strip()
        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        # Read account_size_input directly — same source as refresh_orb_trade_plan_table —
        # to guarantee the watchlist "Trade Plan" column uses the exact same account balance.
        account_size = (
            self._parse_float(self.account_size_input, 0.0)
            if hasattr(self, "account_size_input")
            else 0.0
        )
        if account_size <= 0:
            account_size = self._get_account_balance_for_env(env)

        # Load daily price history (local cache first, then live fallback)
        history = self._load_chart_history_for_timeframe(
            symbol, timeframe="1D", use_live_fallback=False
        )
        if history.empty and use_live_fallback:
            history = self._load_chart_history_for_timeframe(
                symbol, timeframe="1D", use_live_fallback=True
            )

        if history.empty:
            return {
                "symbol": symbol,
                "price": 0.0,
                "total_score": 0.0,
                "status": "ERROR",
                "stop_adr": None,
                "risk_percent": 0.01,
                "position_percent": 0.0,
                "trade_plan": "No history data",
                "env": env,
            }

        latest_bar = history.iloc[-1]
        price = float(latest_bar["Close"])

        # ADR — identical to _calculate_adr_percent_for_symbol
        prev_close = history["Close"].astype(float).shift(1)
        adr_raw = (
            (history["High"].astype(float) - history["Low"].astype(float)) / prev_close
        ).replace([float("inf"), float("-inf")], pd.NA)
        adr_value = adr_raw.rolling(20, min_periods=5).mean().iloc[-1]
        adr_percent: Optional[float] = (
            float(adr_value * 100.0) if not pd.isna(adr_value) else None
        )

        # â”€â”€ Determine entry / stop / target â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        # â”€â”€ Determine entry / stop / target â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

        entry_price: float = price
        stop_loss: float = price * (1.0 - (0.75 * (adr_percent or 2.5) / 100.0))
        breakout_price: float = self._positive_finite_number(
            getattr(item, "breakout_price", None), default=0.0
        )
        buffer_pct: float = 0.001
        case_type: str = "DAILY"

        if (
            item.entry_price
            and item.entry_price > 0
            and item.stop_loss
            and item.stop_loss > 0
        ):
            entry_price = item.entry_price
            stop_loss = item.stop_loss
            case_type = "MANUAL"
        else:
            # Try ALL ORB windows (1m, 5m, 30m) Ã— ALL risk cases and pick the globally
            # best valid plan — identical logic to refresh_watchlist_orb_panel so the
            # watchlist table always shows the same numbers as the ORB panel below it.
            import datetime as dt

            since_dt = _utcnow_naive() - dt.timedelta(days=7)
            five_minute = pd.DataFrame()
            one_minute = pd.DataFrame()
            if self.db_enabled and self.db_engine is not None:
                try:
                    five_minute, _five_source = load_best_intraday_history(
                        symbol, self.db_engine, interval="5m", since=since_dt
                    )
                except Exception:
                    pass
                try:
                    one_minute, _one_source = load_best_intraday_history(
                        symbol, self.db_engine, interval="1m", since=since_dt
                    )
                except Exception:
                    pass

            def _latest_sess(df: "pd.DataFrame") -> "pd.DataFrame":
                if df.empty:
                    return df
                sdf = df.sort_index()
                dates_arr = pd.to_datetime(sdf.index).date
                return sdf[dates_arr == dates_arr[-1]]

            five_min_sess = _latest_sess(five_minute)
            one_min_sess = _latest_sess(one_minute)

            # Pre-read risk % so the window search and the downstream loop share the same cases
            selected_risk = (
                self._parse_float(self.risk_percent_input, 1.0) / 100.0
                if hasattr(self, "risk_percent_input")
                else 0.01
            )
            risk_cases_orb = self._orb_risk_cases(selected_risk)
            buffer_pct = (
                self._watchlist_orb_buffer_pct()
                if hasattr(self, "watchlist_buffer_pct_input")
                else 0.001
            )
            breakout_trigger = (
                breakout_price * (1 + buffer_pct) if breakout_price > 0 else 0.0
            )

            _orb_best_entry: Optional[float] = None
            _orb_best_stop: Optional[float] = None
            _orb_best_risk: float = selected_risk
            _orb_best_sizing: Optional[dict] = None
            _orb_best_score: float = -2.0

            for w_name, w_df in [
                ("1m", one_min_sess),
                ("5m", five_min_sess),
                ("30m", five_min_sess),
            ]:
                if w_df.empty:
                    continue
                orb_range = calculate_orb_range(symbol, w_df, w_name)
                if not orb_range:
                    continue
                orb_high = float(orb_range.high)
                w_entry = (
                    max(orb_high, breakout_trigger)
                    if breakout_trigger > 0
                    else orb_high
                )
                w_stop = float(orb_range.low)
                for rc in risk_cases_orb:
                    s = self._calculate_orb_position_values(
                        account_size=account_size,
                        risk_percent=rc,
                        entry_price=w_entry,
                        stop_price=w_stop,
                        adr_percent=adr_percent,
                    )
                    if self._orb_position_plan_is_valid(s, adr_percent):
                        score = self._score_orb_position_recommendation(s, rc)
                        if score > _orb_best_score:
                            _orb_best_score = score
                            _orb_best_entry = w_entry
                            _orb_best_stop = w_stop
                            _orb_best_risk = rc
                            _orb_best_sizing = s

            if _orb_best_entry is None:
                # No valid plan across any window; fall back to 1m (narrowest stop) then 5m
                # so calculate_deterministic_scores receives the most conservative stop.
                for w_name, w_df in [("1m", one_min_sess), ("5m", five_min_sess)]:
                    if w_df.empty:
                        continue
                    orb_range = calculate_orb_range(symbol, w_df, w_name)
                    if orb_range:
                        orb_high = float(orb_range.high)
                        _orb_best_entry = (
                            max(orb_high, breakout_trigger)
                            if breakout_trigger > 0
                            else orb_high
                        )
                        _orb_best_stop = float(orb_range.low)
                        break

            if _orb_best_entry is not None:
                entry_price = _orb_best_entry
                stop_loss = _orb_best_stop
                case_type = "ORB"

        # â”€â”€ Select the best valid risk % â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # For ORB cases, selected_risk / _orb_best_risk / _orb_best_sizing are already
        # computed above.  For MANUAL and DAILY cases we run the risk loop from scratch.
        if "selected_risk" not in dir():
            selected_risk = (
                self._parse_float(self.risk_percent_input, 1.0) / 100.0
                if hasattr(self, "risk_percent_input")
                else 0.01
            )

        # Seed best_sizing from the cross-window search when available
        best_sizing: Optional[dict] = locals().get("_orb_best_sizing")
        best_risk_pct: float = locals().get("_orb_best_risk", selected_risk)
        best_score: float = locals().get("_orb_best_score", -2.0)

        if best_sizing is None:
            # MANUAL or DAILY case (or ORB fallback with no valid plan): iterate risk cases
            for rc in self._orb_risk_cases(selected_risk):
                s = self._calculate_orb_position_values(
                    account_size=account_size,
                    risk_percent=rc,
                    entry_price=entry_price,
                    stop_price=stop_loss,
                    adr_percent=adr_percent,
                )
                if self._orb_position_plan_is_valid(s, adr_percent):
                    score = self._score_orb_position_recommendation(s, rc)
                    if score > best_score:
                        best_score = score
                        best_sizing = s
                        best_risk_pct = rc

        # If still no valid plan, compute sizing at user-selected risk (display-only)
        if best_sizing is None:
            best_risk_pct = selected_risk
            best_sizing = self._calculate_orb_position_values(
                account_size=account_size,
                risk_percent=best_risk_pct,
                entry_price=entry_price,
                stop_price=stop_loss,
                adr_percent=adr_percent,
            )

        risk_pct = best_risk_pct
        sizing = best_sizing

        # â”€â”€ Deterministic scores â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        scores = calculate_deterministic_scores(
            symbol=symbol,
            history=history,
            entry_price=entry_price,
            breakout_price=breakout_price if breakout_price > 0 else None,
            stop_loss=stop_loss,
            account_size=account_size,
            risk_percent=risk_pct,
        )

        shares_val = int(sizing["shares"])
        cap_pct_val = sizing["capital_percent"]
        desc = f"{case_type}: Buy {shares_val} shares @ ${entry_price:.2f} (Cap: {cap_pct_val:.1f}%, Risk: {risk_pct * 100:.2f}%)"

        scores["price"] = price
        scores["rr"] = scores.get("rr", 0.0)
        scores["stop_adr"] = sizing["sl_adr"]
        scores["risk_percent"] = risk_pct
        scores["position_percent"] = cap_pct_val
        scores["trade_plan"] = desc
        scores["env"] = env

        # â”€â”€ Safety net: enforce ORB position validity against our own sizing â”€â”€â”€â”€â”€â”€
        # calculate_deterministic_scores uses PositionSizer internally which can
        # diverge from _calculate_orb_position_values in edge cases (tiny account,
        # very high-priced stocks, floating-point rounding).  Ensure warnings are
        # always consistent with the values actually displayed in the table.
        plan_warnings = scores.setdefault("warnings", [])
        if cap_pct_val >= MAX_CAPITAL_PERCENT and not any(
            "Capital allocation" in w for w in plan_warnings
        ):
            plan_warnings.append(
                f"Capital allocation ({cap_pct_val:.2f}%) exceeds hard limit of 30%"
            )
        if shares_val < 1 and not any("0 shares" in w for w in plan_warnings):
            plan_warnings.append("Position size calculation resulted in 0 shares")

        # â”€â”€ Stale AI-cache invalidation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # If the cached AI analysis was built with a materially different stop loss
        # (e.g. DAILY fallback vs current ORB stop), clear it so the AI sidebar
        # doesn't show a misleading rejection reason.
        cached_ai = getattr(item, "ai_analysis", None)
        if cached_ai and isinstance(cached_ai, dict):
            cached_stop = cached_ai.get("stop_loss", 0.0) or 0.0
            try:
                cached_stop = float(cached_stop)
            except (TypeError, ValueError):
                cached_stop = 0.0
            # Clear if the stop moved by more than 2% of entry (i.e. the plan changed materially)
            if (
                cached_stop > 0
                and abs(cached_stop - stop_loss) / max(entry_price, 0.01) > 0.02
            ):
                item.ai_analysis = None

        # Status
        has_hard_reject = len(scores.get("warnings", [])) > 0
        if has_hard_reject:
            scores["status"] = "REJECTED"
        else:
            scores["status"] = "BUY_READY"

        # Cache for loader (double-click to Trade Plan)
        if not hasattr(self, "watchlist_scores"):
            self.watchlist_scores = {}
        previous_scores = self.watchlist_scores.get(symbol, {})
        self.watchlist_scores[symbol] = self._merge_watchlist_score_cache(
            previous_scores,
            {
                "price": price,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": 0.0,
                "breakout_price": breakout_price if breakout_price > 0 else None,
                "buffer_pct": buffer_pct,
                "total_score": scores.get("total_score", 0.0),
                "status": scores.get("status", "WATCHING"),
                "rr": scores.get("rr", 0.0),
                "stop_adr": sizing["sl_adr"],
                "risk_percent": risk_pct,
                "position_percent": cap_pct_val,
                "trade_plan": desc,
                "env": env,
            },
        )

        return scores

    @staticmethod
    def _merge_watchlist_score_cache(
        previous_scores: dict, current_scores: dict
    ) -> dict:
        return {
            **previous_scores,
            **current_scores,
        }

    @staticmethod
    def _watchlist_display_status(status: str, orb_status: Optional[str]) -> str:
        """Return the status that should be shown in the watchlist table."""
        if orb_status in {
            "NO_INTRADAY",
            "NO_VALID_ORB",
            "BELOW_BREAKOUT",
            "WAITING_ENTRY",
            "NO_ENTRY",
        }:
            return orb_status
        if orb_status == "BUY_READY":
            return "BUY_READY"
        return status

    @staticmethod
    def _watchlist_status_row_color(
        status: str, orb_status: Optional[str]
    ) -> Optional[QColor]:
        """Return the row color for the effective watchlist status."""
        if orb_status in {"NO_INTRADAY", "NO_VALID_ORB"}:
            return QColor(108, 117, 125)
        if orb_status == "WAITING_ENTRY":
            return QColor(39, 174, 96)
        if status == "BUY_READY":
            return QColor(39, 174, 96)
        if status == "REJECTED":
            return QColor(192, 57, 43)
        return None

    def mark_watchlist_and_dashboard_dirty(self) -> None:
        """Defer populate_watchlist_table()/update_dashboard_summary() until
        the Watchlist/Dashboard tab is actually visible.

        populate_watchlist_table() recalculates ORB scores for every
        watchlist item, and update_dashboard_summary() does its own full
        pass -- both are expensive and pointless to run every time a chart
        interaction (breakout price, drawing) happens on an unrelated tab.
        If either tab IS currently active, refresh immediately as before so
        on-screen data never goes stale while being looked at.
        """
        active_widget = (
            self.__dict__.get("tabs").currentWidget()
            if self.__dict__.get("tabs") is not None
            else None
        )
        if active_widget is self.__dict__.get("watchlist_widget"):
            self.populate_watchlist_table()
        else:
            self._watchlist_table_dirty = True
        if active_widget is self.__dict__.get("dashboard_widget"):
            self.update_dashboard_summary()
        else:
            self._dashboard_summary_dirty = True

    def _flush_dirty_watchlist_and_dashboard(self) -> None:
        """Called from on_tab_changed: catch up any refresh that was
        deferred by mark_watchlist_and_dashboard_dirty()."""
        active_widget = (
            self.__dict__.get("tabs").currentWidget()
            if self.__dict__.get("tabs") is not None
            else None
        )
        if active_widget is None:
            return
        if (
            self.__dict__.get("_watchlist_table_dirty")
            and active_widget is self.__dict__.get("watchlist_widget")
        ):
            self._watchlist_table_dirty = False
            self.populate_watchlist_table()
        if (
            self.__dict__.get("_dashboard_summary_dirty")
            and active_widget is self.__dict__.get("dashboard_widget")
        ):
            self._dashboard_summary_dirty = False
            self.update_dashboard_summary()

    def populate_watchlist_table(
        self,
        *,
        use_live_fallback: bool = True,
        calculate_scores: bool = True,
    ) -> None:
        """Populate the watchlist scoreboard table."""
        calculate_scores = calculate_scores and not self.__dict__.get(
            "_window_initializing", False
        )
        use_live_fallback = use_live_fallback and not self.__dict__.get(
            "_window_initializing", False
        )
        self.watchlist_table.setRowCount(0)

        for item in self.watchlist.items:
            symbol = item.symbol.strip().upper()
            row = self.watchlist_table.rowCount()
            self.watchlist_table.insertRow(row)

            if calculate_scores:
                try:
                    scores = self._calculate_item_scores(
                        item, use_live_fallback=use_live_fallback
                    )
                except Exception as e:
                    scores = {
                        "symbol": symbol,
                        "price": 0.0,
                        "total_score": 0.0,
                        "status": "ERROR",
                        "stop_adr": 0.0,
                        "risk_percent": 0.01,
                        "position_percent": 0.0,
                        "trade_plan": f"Error: {str(e)}",
                        "env": "PROD",
                    }
            else:
                scores = {
                    "symbol": symbol,
                    "price": 0.0,
                    "total_score": 0.0,
                    "status": "WATCHING",
                    "stop_adr": None,
                    "risk_percent": 0.01,
                    "position_percent": 0.0,
                    "trade_plan": "Refresh required",
                    "env": "PROD",
                    **dict(getattr(self, "watchlist_scores", {}).get(symbol, {})),
                }

            # Extract scores from cached AI analysis (score_breakdown) if available
            ai_data = getattr(item, "ai_analysis", None)
            if ai_data and isinstance(ai_data, dict) and "full_json" in ai_data:
                total_score = ai_data["full_json"].get(
                    "total_score", scores.get("total_score", 0.0)
                )
                status = ai_data["full_json"].get(
                    "decision", scores.get("status", "WATCHING")
                )
            else:
                total_score = scores.get("total_score", 0.0)
                status = scores.get("status", "WATCHING")

            # Clean status display format mapping
            if status == "BUYLIST_READY":
                status = "BUY_READY"
            elif status == "WATCH_ONLY":
                status = "WATCHING"
            elif status == "REJECT":
                status = "REJECTED"

            def qitem(val):
                return QTableWidgetItem(str(val) if val is not None else "")

            self.watchlist_table.setItem(row, 0, qitem(item.symbol))
            self.watchlist_table.setItem(row, 1, qitem(item.name))

            price_val = scores.get("price")
            price_str = f"{price_val:.2f}" if price_val and price_val > 0 else ""
            self.watchlist_table.setItem(row, 2, qitem(price_str))

            self.watchlist_table.setItem(row, 3, qitem(total_score))
            self.watchlist_table.setItem(row, 4, qitem(status))

            self.watchlist_table.setItem(
                row,
                5,
                qitem(
                    f"{scores.get('stop_adr', 0.0):.1f}"
                    if isinstance(scores.get("stop_adr"), (int, float))
                    else ""
                ),
            )

            risk_pct_val = scores.get("risk_percent", "")
            risk_pct_str = (
                f"{risk_pct_val*100:.2f}%"
                if isinstance(risk_pct_val, (int, float)) and risk_pct_val < 1.0
                else (
                    f"{risk_pct_val:.2f}%"
                    if isinstance(risk_pct_val, (int, float))
                    else ""
                )
            )
            self.watchlist_table.setItem(row, 6, qitem(risk_pct_str))

            cap_pct_val = scores.get("position_percent", "")
            cap_pct_str = (
                f"{cap_pct_val:.2f}%" if isinstance(cap_pct_val, (int, float)) else ""
            )
            self.watchlist_table.setItem(row, 7, qitem(cap_pct_str))

            self.watchlist_table.setItem(row, 8, qitem(scores.get("trade_plan", "")))
            self.watchlist_table.setItem(row, 9, qitem(scores.get("env", "")))

            self.watchlist_table.setItem(
                row, 10, qitem(self._format_optional_price(item.entry_price))
            )
            self.watchlist_table.setItem(
                row, 11, qitem(self._format_optional_price(item.breakout_price))
            )
            self.watchlist_table.setItem(
                row, 12, qitem(self._format_optional_price(item.stop_loss))
            )
            self.watchlist_table.setItem(row, 13, qitem(item.notes))

            added_date = getattr(item, "added_date", None)
            added_str = (
                added_date.astimezone(KST_ZONE).strftime("%Y/%m/%d")
                if added_date
                else ""
            )
            self.watchlist_table.setItem(row, 14, qitem(added_str))

            # â”€â”€ ORB status takes precedence over scoring status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # NO_ENTRY  â†’ all valid ORB plans have no entry zone â†’ overrides BUY_READY
            # BUY_READY â†’ a confirmed_orb_breakout signal is present
            orb_status = (
                self.watchlist_scores.get(symbol, {}).get("orb_status")
                if hasattr(self, "watchlist_scores")
                else None
            )
            if orb_status is None and getattr(
                self, "_force_watchlist_orb_status_eval", False
            ):
                records = self._calculate_watchlist_orb_records_for_symbol(symbol)
                orb_status = self._derive_watchlist_orb_status(records)
                self.watchlist_scores.setdefault(symbol, {})["orb_status"] = orb_status

            # ORB status takes precedence after the symbol's ORB plan panel has
            # been evaluated. NO_ENTRY must override a BUY_READY scoring status.
            display_status = self._watchlist_display_status(status, orb_status)
            # Re-write column 4 with the possibly-overridden status
            self.watchlist_table.setItem(row, 4, qitem(display_status))

            # Row color — ORB status wins over scoring status when set
            row_color = self._watchlist_status_row_color(display_status, orb_status)
            if row_color:
                for col in range(self.watchlist_table.columnCount()):
                    cell = self.watchlist_table.item(row, col)
                    if cell:
                        cell.setBackground(row_color)
                        cell.setForeground(QColor(255, 255, 255))

        self.watchlist_table.resizeColumnsToContents()
        self.watchlist_table.setColumnWidth(8, 250)  # Trade Plan
        self.watchlist_table.setColumnWidth(9, 100)  # Env
        self.watchlist_table.setColumnWidth(13, 200)  # Notes

        if hasattr(self, "sidebar_source_combo"):
            source = self.sidebar_source_combo.currentData() or {}
            if source.get("type") in ("watchlist", "buylist"):
                self.refresh_stock_sidebar()
        self.populate_chart_symbol_combo()
        self.populate_intraday_watchlist_symbols()
        self.populate_tradingview_watchlist_symbols()
        if hasattr(self, "_update_tradingview_watchlist_btn"):
            self._update_tradingview_watchlist_btn()

    def remove_selected_watchlist_item(self) -> None:
        selected = self.watchlist_table.currentRow()
        if selected < 0:
            QMessageBox.warning(
                self, "No selection", "Please select a watchlist row to remove."
            )
            return

        symbol_item = self.watchlist_table.item(selected, 0)
        if symbol_item is None:
            return

        symbol = symbol_item.text()
        removed = self.watchlist.remove(symbol)
        if removed:
            if hasattr(self, "watchlist_scores") and symbol in self.watchlist_scores:
                del self.watchlist_scores[symbol]
            self.delete_intraday_cache_for_symbol(symbol)
            self.populate_watchlist_table()
            # Restore cursor to same position (or last row if removed the last item)
            new_count = self.watchlist_table.rowCount()
            if new_count > 0:
                self.watchlist_table.setCurrentCell(min(selected, new_count - 1), 0)
            self.update_dashboard_summary()
            self._save_state()
            self.append_log(f"Removed {symbol} from watchlist.")

    def load_watchlist_item_to_trade_plan(self, row: int, column: int) -> None:
        """Double-click handler: select symbol and refresh the ORB panel below.

        Also populates the Daily Breakout $ field from the watchlist item's
        breakout_price so the ORB plan reflects the user-entered structural breakout level.
        """
        symbol_item = self.watchlist_table.item(row, 0)
        if symbol_item is None:
            return
        symbol = symbol_item.text().strip().upper()
        self._set_chart_symbol(symbol)
        # Populate breakout price field from this symbol's breakout_price
        self._load_breakout_price_for_symbol(symbol)
        self.refresh_watchlist_orb_panel(symbol)

    def on_trade_kis_environment_changed(self, env: str) -> None:
        watchlist_combo = self.__dict__.get("watchlist_env_combo")
        trade_combo = self.__dict__.get("trade_kis_environment_combo")
        if (
            watchlist_combo is not None
            and watchlist_combo is not trade_combo
            and watchlist_combo.currentText() != env
        ):
            target_index = watchlist_combo.findText(env)
            if target_index >= 0:
                watchlist_combo.setCurrentIndex(target_index)
            return

        populated = self.populate_trade_account_combo()
        if not populated:
            self.apply_cached_trade_account_size()
        self.calculate_position_size(show_warnings=False)
        if hasattr(self, "run_watchlist_ai_review"):
            self.run_watchlist_ai_review()

    def on_watchlist_env_changed(self, index: int) -> None:
        """Repopulate account combo when the environment changes (balance reload is chained inside)."""
        watchlist_combo = self.__dict__.get("watchlist_env_combo")
        trade_combo = self.__dict__.get("trade_kis_environment_combo")
        env = watchlist_combo.currentText() if watchlist_combo is not None else ""
        if (
            trade_combo is not None
            and trade_combo is not watchlist_combo
            and trade_combo.currentText() != env
        ):
            old_block = trade_combo.blockSignals(True)
            target_index = trade_combo.findText(env)
            if target_index >= 0:
                trade_combo.setCurrentIndex(target_index)
            trade_combo.blockSignals(old_block)

        populated = self.populate_trade_account_combo()
        if not populated:
            self.apply_cached_trade_account_size()
        self.calculate_position_size(show_warnings=False)

    def recalculate_watchlist_scoreboard_sizes(self) -> None:
        """Recalculate all watchlist scoreboard data when account size or risk % changes."""
        if not hasattr(self, "watchlist_table"):
            return
        # Account snapshot callbacks run on the GUI thread. Re-render the
        # durable/cached projection immediately and leave database/network
        # score recalculation to the explicit refresh workflow.
        self.populate_watchlist_table(
            use_live_fallback=False, calculate_scores=False
        )
        # Also refresh the ORB panel for whichever symbol is currently selected
        selected = (
            self.watchlist_table.selectionModel().selectedRows()
            if self.watchlist_table.selectionModel()
            else []
        )
        if selected:
            sym_item = self.watchlist_table.item(selected[0].row(), 0)
            if sym_item:
                self.refresh_watchlist_orb_panel(sym_item.text().strip().upper())

    # ------------------------------------------------------------------
    # Data snapshot (debug tool)
    # ------------------------------------------------------------------
    def save_watchlist_snapshot(self) -> None:
        """Save a JSON snapshot of the watchlist table + ORB panel for debugging data gaps."""
        import datetime as dt

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = DATA_DIR / f"watchlist_snapshot_{timestamp}.json"

        # â”€â”€ 1. Environment / account inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "?"
        )
        trade_env = (
            self.trade_kis_environment_combo.currentText()
            if hasattr(self, "trade_kis_environment_combo")
            else "?"
        )
        account_raw = (
            self.account_size_input.text()
            if hasattr(self, "account_size_input")
            else ""
        )
        account_parsed = (
            self._parse_float(self.account_size_input, 0.0)
            if hasattr(self, "account_size_input")
            else 0.0
        )
        risk_raw = (
            self.risk_percent_input.text()
            if hasattr(self, "risk_percent_input")
            else ""
        )
        risk_parsed = (
            self._parse_float(self.risk_percent_input, 0.0) / 100.0
            if hasattr(self, "risk_percent_input")
            else 0.0
        )
        balance_from_env_fn = self._get_account_balance_for_env(env)

        # â”€â”€ 2. Selected symbol â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        selected_symbol = ""
        selected_rows = (
            self.watchlist_table.selectionModel().selectedRows()
            if self.watchlist_table.selectionModel()
            else []
        )
        if selected_rows:
            sym_item = self.watchlist_table.item(selected_rows[0].row(), 0)
            if sym_item:
                selected_symbol = sym_item.text().strip().upper()

        # â”€â”€ 3. Dump watchlist table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        wl_headers = [
            (
                self.watchlist_table.horizontalHeaderItem(c).text()
                if self.watchlist_table.horizontalHeaderItem(c)
                else f"col{c}"
            )
            for c in range(self.watchlist_table.columnCount())
        ]
        wl_rows = []
        for r in range(self.watchlist_table.rowCount()):
            row_data = {}
            for c, hdr in enumerate(wl_headers):
                cell = self.watchlist_table.item(r, c)
                row_data[hdr] = cell.text() if cell else ""
            wl_rows.append(row_data)

        # â”€â”€ 4. Dump ORB panel table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        orb_rows_dump: List[dict] = []
        if hasattr(self, "watchlist_orb_table"):
            t = self.watchlist_orb_table
            orb_col_count = t.columnCount()
            for r in range(t.rowCount()):
                row_key_item = t.item(r, 0)
                row_key = row_key_item.text() if row_key_item else f"row{r}"
                row_data: dict = {"metric": row_key}
                for c in range(1, orb_col_count):
                    col_label_items = []
                    for hr in range(2):  # first 2 rows are Risk% and Window
                        hi = t.item(hr, c)
                        col_label_items.append(hi.text() if hi else "")
                    col_key = (
                        f"{col_label_items[0]}_{col_label_items[1]}"
                        if any(col_label_items)
                        else f"col{c}"
                    )
                    cell = t.item(r, c)
                    row_data[col_key] = cell.text() if cell else ""
                orb_rows_dump.append(row_data)

        # â”€â”€ 5. Diagnostic: re-run _calculate_item_scores for selected sym â”€
        diagnostic: dict = {
            "selected_symbol": selected_symbol,
            "watchlist_env": env,
            "trade_plan_env": trade_env,
            "account_size_input_text": account_raw,
            "account_size_input_parsed_usd": round(account_parsed, 4),
            "risk_percent_input_text": risk_raw,
            "risk_percent_parsed": round(risk_parsed, 6),
            "_get_account_balance_for_env_result": round(balance_from_env_fn, 4),
            "envs_in_sync": env == trade_env,
        }
        if selected_symbol:
            try:
                item = self.watchlist.get(selected_symbol)
                if item:
                    scores = self._calculate_item_scores(item)
                    # _calculate_item_scores puts entry/stop/shares in watchlist_scores cache
                    wl_cache = getattr(self, "watchlist_scores", {}).get(
                        selected_symbol, {}
                    )
                    diagnostic["_calculate_item_scores"] = {
                        "account_size_used": round(account_parsed, 4),
                        "entry_price": round(wl_cache.get("entry_price", 0.0), 4),
                        "stop_loss": round(wl_cache.get("stop_loss", 0.0), 4),
                        "position_percent": round(
                            scores.get("position_percent", 0.0), 4
                        ),
                        "risk_percent_used": round(scores.get("risk_percent", 0.0), 6),
                        "stop_adr": (
                            round(scores.get("stop_adr", 0.0), 4)
                            if scores.get("stop_adr") is not None
                            else None
                        ),
                        "trade_plan_string": scores.get("trade_plan", ""),
                        "rr": round(scores.get("rr", 0.0), 4),
                        "total_score": round(scores.get("total_score", 0.0), 2),
                        "status": scores.get("status", ""),
                        "warnings": scores.get("warnings", []),
                    }
            except Exception as exc:
                diagnostic["_calculate_item_scores"] = {"error": str(exc)}

            # Direct ORB panel calculation for the same symbol
            try:
                adr_pct = self._calculate_adr_percent_for_symbol(selected_symbol)
                five_min = self._latest_intraday_session(
                    self._load_cached_intraday_interval(selected_symbol, "5m", 7)
                )
                one_min = self._latest_intraday_session(
                    self._load_cached_intraday_interval(selected_symbol, "1m", 7)
                )
                orb_diag: dict = {
                    "account_size_used": round(account_parsed, 4),
                    "adr_percent": round(adr_pct, 4) if adr_pct is not None else None,
                    "5m_session_bars": len(five_min),
                    "1m_session_bars": len(one_min),
                }
                for w_name, w_df in [("1m", one_min), ("5m", five_min)]:
                    if not w_df.empty:
                        orb_range = calculate_orb_range(selected_symbol, w_df, w_name)
                        if orb_range:
                            entry = float(orb_range.high)
                            stop = float(orb_range.low)
                            sizing = self._calculate_orb_position_values(
                                account_size=account_parsed,
                                risk_percent=risk_parsed,
                                entry_price=entry,
                                stop_price=stop,
                                adr_percent=adr_pct,
                            )
                            orb_diag[f"orb_{w_name}"] = {
                                "entry": round(entry, 4),
                                "stop": round(stop, 4),
                                "risk_per_share": round(sizing["risk_per_share"], 4),
                                "shares": int(sizing["shares"]),
                                "investment": round(sizing["investment"], 2),
                                "capital_percent": round(sizing["capital_percent"], 4),
                                "stop_loss_percent": round(
                                    sizing["stop_loss_percent"], 4
                                ),
                                "sl_adr": (
                                    round(sizing["sl_adr"], 4)
                                    if sizing["sl_adr"] is not None
                                    else None
                                ),
                                "valid": self._orb_position_plan_is_valid(
                                    sizing, adr_pct
                                ),
                            }
                        else:
                            orb_diag[f"orb_{w_name}"] = "no_orb_range"
                    else:
                        orb_diag[f"orb_{w_name}"] = "no_intraday_data"
                diagnostic["refresh_watchlist_orb_panel"] = orb_diag
            except Exception as exc:
                diagnostic["refresh_watchlist_orb_panel"] = {"error": str(exc)}

        snapshot = {
            "timestamp": timestamp,
            "diagnostic": diagnostic,
            "watchlist_table": {"headers": wl_headers, "rows": wl_rows},
            "orb_panel": {
                "symbol": selected_symbol,
                "rows": orb_rows_dump,
            },
        }

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False, default=str)
            self.append_log(f"Snapshot saved: {out_path}")
            QMessageBox.information(
                self,
                "Snapshot Saved",
                f"Snapshot written to:\n{out_path.resolve()}\n\n"
                f"Key values captured:\n"
                f"  account_size_input = {account_raw!r}\n"
                f"  parsed USD         = {account_parsed:,.2f}\n"
                f"  _get_account…env() = {balance_from_env_fn:,.2f}\n"
                f"  risk %             = {risk_raw!r}\n"
                f"  watchlist env      = {env}\n"
                f"  trade plan env     = {trade_env}\n"
                f"  selected symbol    = {selected_symbol or '(none)'}",
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "Snapshot Failed", f"Could not save snapshot:\n{exc}"
            )

    def run_watchlist_ai_review(self) -> None:
        """Start the background thread to analyze and score all watchlist symbols."""
        if not self.watchlist.items:
            QMessageBox.information(
                self,
                "Empty Watchlist",
                "Watchlist is empty. Add symbols to watch first.",
            )
            return

        existing_worker = self.__dict__.get("watchlist_worker")
        if existing_worker is not None and existing_worker.isRunning():
            QMessageBox.information(
                self,
                "Analysis running",
                "Watchlist AI analysis is already running. Wait for it to finish before starting another review.",
            )
            return

        self.analyze_stock_ai_button.setEnabled(False)
        self.analyze_stock_ai_button.setText("Analyzing...")

        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        account_size = self._get_account_balance_for_env(env)
        risk_percent = (
            self._parse_float(self.risk_percent_input, 1.0) / 100.0
            if hasattr(self, "risk_percent_input")
            else 0.01
        )

        active_plans = (
            {
                plan.symbol.upper(): plan
                for plan in self.trade_manager.get_active_plans()
            }
            if hasattr(self, "trade_manager")
            else {}
        )
        self.watchlist_worker = WatchlistAiWorker(
            watchlist_items=self.watchlist.items,
            db_engine=self.db_engine,
            account_size=account_size,
            risk_percent=risk_percent,
            active_plans=active_plans,
            env=env,
        )
        self.watchlist_worker.progress_update.connect(
            lambda msg: self.progress_label.setText(msg)
        )
        self.watchlist_worker.log_message.connect(self.append_log)
        self.watchlist_worker.finished_analysis.connect(
            self.on_watchlist_ai_review_finished
        )
        self.watchlist_worker.finished_analysis_df.connect(
            self.on_watchlist_df_finished
        )
        self._track_worker("watchlist_worker", self.watchlist_worker)
        self.watchlist_worker.start()

    def on_watchlist_df_finished(self, df: pd.DataFrame) -> None:
        """Called when watchlist worker thread finishes with DataFrame."""
        self.watchlist_df = df

    def on_watchlist_ai_review_finished(self, results: dict) -> None:
        """Called when watchlist worker thread finishes."""
        self.watchlist_scores = results
        self.populate_watchlist_table()
        self.analyze_stock_ai_button.setEnabled(True)
        self.analyze_stock_ai_button.setText("Analyze with AI")
        self.progress_label.setText("Watchlist AI analysis completed.")
        self.append_log("Watchlist scoreboard updated.")
        self._save_state()
        if hasattr(self, "refresh_execution_queue"):
            self.refresh_execution_queue(
                (
                    self.watchlist_env_combo.currentText()
                    if hasattr(self, "watchlist_env_combo")
                    else "PROD"
                ),
                show_log=False,
            )

    def move_selected_to_buylist(self) -> None:
        """Move only the selected watchlist symbol into the execution queue."""
        selected = self.watchlist_table.currentRow()
        if selected < 0:
            QMessageBox.warning(
                self, "No selection", "Please select a watchlist candidate row first."
            )
            return

        symbol_item = self.watchlist_table.item(selected, 0)
        if symbol_item is None:
            return

        symbol = symbol_item.text().strip().upper()
        item = self.watchlist.get(symbol)
        if item is None:
            return

        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        added = self.refresh_execution_queue(
            env,
            symbols=[symbol],
            create_missing=True,
        )
        self.populate_watchlist_table()
        self.populate_buylist_dashboard()
        self.update_dashboard_summary()
        self._save_state()

        if added:
            QMessageBox.information(
                self,
                "Added to Buy Board",
                f"'{symbol}' added to the Buy Board Buylist column.\n\n"
                "Move it to Buy Today when you want the Buy Board engine to monitor and execute its ORB plan.",
            )
        else:
            QMessageBox.warning(
                self,
                "Queue not updated",
                f"'{symbol}' could not be added to the {env} execution queue. Check the log for details.",
            )
        self.refresh_orb_trade_plan_table()

    def on_watchlist_selection_changed(self) -> None:
        """Called when the selected row in the watchlist changes (mouse click or keyboard arrows)."""
        selected_rows = self.watchlist_table.selectionModel().selectedRows()
        if not selected_rows:
            return

        row = selected_rows[0].row()
        symbol_item = self.watchlist_table.item(row, 0)
        if symbol_item is None:
            return

        symbol = symbol_item.text().strip().upper()
        item = self.watchlist.get(symbol)
        if item is None:
            return

        # Refresh the ORB panel below the watchlist table for the selected symbol
        self.refresh_watchlist_orb_panel(symbol)

        # Check if we have cached AI analysis for this item
        cached = getattr(item, "ai_analysis", None)
        if cached and isinstance(cached, dict) and cached.get("full_json"):
            from src.core.scoring import render_quant_analysis_html

            html = render_quant_analysis_html(cached["full_json"])
            self.ai_sidebar.setVisible(True)
            self.ai_report_view.setHtml(html)
        else:
            self.ai_sidebar.setVisible(True)
            self.ai_report_view.setHtml(
                f"<h3>{symbol}</h3>"
                f"<p>No AI analysis cached for today.</p>"
                f"<p>Click <b>Analyze with AI</b> to run the quantitative assessment for all watchlist symbols.</p>"
            )

    def _load_cached_intraday_interval(
        self, symbol: str, interval: str, window_days: int = 7
    ) -> pd.DataFrame:
        symbol = symbol.strip().upper()
        if not symbol or not self.db_enabled or self.db_engine is None:
            return pd.DataFrame()
        since = _utcnow_naive() - dt.timedelta(
            days=max(1, min(7, int(window_days or 7)))
        )
        try:
            bars, source = load_best_intraday_history(
                symbol, self.db_engine, interval=interval, since=since
            )
            self.latest_intraday_sources[(symbol, interval)] = source
            return bars
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _latest_intraday_session(intraday: pd.DataFrame) -> pd.DataFrame:
        if intraday.empty:
            return pd.DataFrame()
        bars = intraday.sort_index().copy()
        session_dates = pd.to_datetime(bars.index).date
        latest_date = session_dates[-1]
        return bars[session_dates == latest_date]

    def _calculate_adr_percent_for_symbol(self, symbol: str) -> Optional[float]:
        if not symbol or not self.db_enabled or self.db_engine is None:
            return None
        history = load_symbol_history_from_db(symbol, self.db_engine, interval="1d")
        if history.empty or len(history) < 2:
            return None
        prev_close = history["Close"].astype(float).shift(1)
        adr = (
            (history["High"].astype(float) - history["Low"].astype(float)) / prev_close
        ).replace(
            [float("inf"), float("-inf")],
            pd.NA,
        )
        value = adr.rolling(20, min_periods=5).mean().iloc[-1]
        if pd.isna(value):
            return None
        return float(value * 100.0)

    def _get_trade_plan_target_price(self, symbol: str) -> Optional[float]:
        item = self.watchlist.get(symbol)
        if item is None or item.breakout_price is None:
            return None
        price = self._positive_finite_number(item.breakout_price, default=0.0)
        return price or None

    def _format_optional_price(self, value: Optional[float]) -> str:
        return "" if value is None else f"{float(value):.2f}"

    @staticmethod
    def _orb_risk_cases(selected_risk_percent: float) -> List[float]:
        cases = [0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02]
        if selected_risk_percent > 0 and all(
            abs(selected_risk_percent - case) > 0.00001 for case in cases
        ):
            cases.append(selected_risk_percent)
        return sorted(cases)

    @staticmethod
    def _orb_position_plan_headers(risk_cases: List[float]) -> List[str]:
        headers = ["Metric"]
        for risk_percent in risk_cases:
            risk_label = f"{risk_percent * 100:.2f}%"
            headers.extend(
                [
                    f"{risk_label} 1m",
                    f"{risk_label} 5m",
                    f"{risk_label} 30m",
                ]
            )
        return headers

    @staticmethod
    def _orb_position_plan_is_valid(sizing: dict, adr_percent: Optional[float]) -> bool:
        return is_orb_position_plan_valid(sizing, adr_percent)

    @staticmethod
    def _score_orb_position_recommendation(sizing: dict, risk_percent: float) -> float:
        return score_orb_position_recommendation(sizing, risk_percent)

    @staticmethod
    def _format_orb_recommendation(score: float, valid: bool) -> str:
        if not valid:
            return "Invalid"
        if score >= 85:
            return f"Excellent {score:.0f}"
        if score >= 70:
            return f"Good {score:.0f}"
        return f"OK {score:.0f}"

    @staticmethod
    def _sort_orb_plan_records(records: List[dict]) -> List[dict]:
        return sorted(
            records,
            key=lambda record: (
                bool(record.get("valid")),
                float(record.get("recommendation_score", -1.0)),
                -float(record.get("risk_percent", 0.0)),
            ),
            reverse=True,
        )

    @staticmethod
    def _calculate_orb_position_values(
        account_size: float,
        risk_percent: float,
        entry_price: float,
        stop_price: float,
        adr_percent: Optional[float] = None,
    ) -> dict:
        return calculate_orb_position_values(
            account_size,
            risk_percent,
            entry_price,
            stop_price,
            adr_percent,
        )

    def _apply_orb_trade_plan_selection(self, column: int, checked: bool) -> None:
        pass

    def _apply_orb_trade_plan_column(
        self, column: int, update_checkbox_state: bool = False
    ) -> None:
        pass

    def _auto_select_best_orb_plan(self) -> None:
        pass

    def refresh_orb_trade_plan_table(self) -> None:
        """Redirect to the watchlist ORB panel (Trade Plan tab removed)."""
        if not hasattr(self, "watchlist_table"):
            return
        selected = (
            self.watchlist_table.selectionModel().selectedRows()
            if self.watchlist_table.selectionModel()
            else []
        )
        if selected:
            sym_item = self.watchlist_table.item(selected[0].row(), 0)
            if sym_item:
                self.refresh_watchlist_orb_panel(sym_item.text().strip().upper())

    def _load_breakout_price_for_symbol(self, symbol: str) -> None:
        """Populate the Daily Breakout $ field from watchlist.breakout_price for a symbol.

        Blocks the textChanged signal during the update so it does not trigger a
        save-back loop or a premature ORB panel refresh.
        """
        if not hasattr(self, "watchlist_breakout_price_input"):
            return
        item = self.watchlist.get(symbol) if hasattr(self, "watchlist") else None
        tp = item.breakout_price if item is not None else None
        old_block = self.watchlist_breakout_price_input.blockSignals(True)
        try:
            self.watchlist_breakout_price_input.setText(
                f"{tp:.2f}" if tp and tp > 0 else ""
            )
        finally:
            self.watchlist_breakout_price_input.blockSignals(old_block)

    def _on_watchlist_orb_filter_changed(self) -> None:
        """Re-render the ORB panel; save breakout price edits back to watchlist.breakout_price."""
        if not hasattr(self, "watchlist_orb_symbol_label"):
            return
        text = self.watchlist_orb_symbol_label.text().strip()
        if not text or text == "Select a watchlist symbol to view its ORB plan":
            return
        symbol = text.upper()
        # Persist only a positive, finite manual breakout level.  Invalid UI
        # text is treated as an unset level instead of leaking nan/inf into an
        # execution plan.
        if hasattr(self, "watchlist_breakout_price_input") and hasattr(
            self, "watchlist"
        ):
            bp_text = self.watchlist_breakout_price_input.text().strip()
            new_tp = (
                self._positive_finite_number(bp_text, default=0.0)
                if bp_text
                else None
            )
            item = self.watchlist.get(symbol)
            if item is not None and item.breakout_price != new_tp:
                item.breakout_price = new_tp
                self._save_state()
        self.refresh_watchlist_orb_panel(symbol)

    def _on_watchlist_orb_plan_selected(self, column: int, checked: bool) -> None:
        """Apply the chosen ORB plan column to the corresponding watchlist table row."""
        if getattr(self, "_updating_watchlist_orb_selection", False):
            return
        if not checked:
            return

        self._updating_watchlist_orb_selection = True
        try:
            table = self.watchlist_orb_table
            # Uncheck every other column's checkbox (radio-button behaviour)
            for col in range(1, table.columnCount()):
                if col == column:
                    continue
                wrapper = table.cellWidget(0, col)
                if wrapper:
                    cb = wrapper.findChild(QCheckBox)
                    if cb and cb.isChecked():
                        cb.setChecked(False)

            plan = getattr(self, "watchlist_orb_column_data", {}).get(column)
            if not plan or not plan.get("valid"):
                QMessageBox.warning(
                    self,
                    "Invalid ORB plan",
                    "Only a plan that passes the position and risk checks can be selected.",
                )
                return

            symbol = plan["symbol"]
            sizing = plan["sizing"]
            try:
                risk_pct = float(plan["risk_percent"])
                orb_high = float(
                    plan.get("orb_high") or plan.get("entry_price") or 0.0
                )
                entry_trigger = float(plan.get("entry_trigger") or orb_high)
                stop_price = float(plan.get("stop_price") or 0.0)
                raw_breakout = plan.get("breakout_price")
                bp = float(raw_breakout) if raw_breakout is not None else None
                buffer_pct = float(plan.get("buffer_pct", 0.001))
            except (TypeError, ValueError):
                QMessageBox.warning(
                    self,
                    "Invalid ORB plan",
                    "The selected plan contains an invalid price or risk value.",
                )
                return
            if (
                not all(
                    math.isfinite(value)
                    for value in (risk_pct, orb_high, entry_trigger, stop_price, buffer_pct)
                )
                or (bp is not None and not math.isfinite(bp))
                or risk_pct <= 0
                or entry_trigger <= 0
                or stop_price <= 0
                or stop_price >= entry_trigger
                or buffer_pct < 0
            ):
                QMessageBox.warning(
                    self,
                    "Invalid ORB plan",
                    "The selected plan must have finite positive prices and a stop below entry.",
                )
                return
            shares_val = int(sizing.get("shares", 0))
            cap_pct = sizing.get("capital_percent", 0.0)
            sl_adr = sizing.get("sl_adr")

            desc = (
                f"ORB: Buy {shares_val} shares @ ${entry_trigger:.2f}"
                f" (Cap: {cap_pct:.1f}%, Risk: {risk_pct * 100:.2f}%)"
            )

            # Locate the symbol's row in the watchlist table
            watchlist_row = -1
            for r in range(self.watchlist_table.rowCount()):
                sym_item = self.watchlist_table.item(r, 0)
                if sym_item and sym_item.text().strip().upper() == symbol:
                    watchlist_row = r
                    break

            if watchlist_row < 0:
                return

            def _set(col_idx: int, text: str) -> None:
                item = self.watchlist_table.item(watchlist_row, col_idx)
                if item is None:
                    item = QTableWidgetItem(text)
                    self.watchlist_table.setItem(watchlist_row, col_idx, item)
                else:
                    item.setText(text)

            _set(5, f"{sl_adr:.0f}" if sl_adr is not None else "")  # Stop/ADR
            _set(6, f"{risk_pct * 100:.2f}%")  # Risk %
            _set(7, f"{cap_pct:.2f}%")  # Capital %
            _set(8, desc)  # Trade Plan
            _set(
                10, f"{entry_trigger:.2f}" if entry_trigger else ""
            )  # Entry Price (= trigger)
            _set(11, f"{bp:.2f}" if bp else "")  # Breakout Price
            _set(12, f"{stop_price:.2f}" if stop_price else "")  # Stop Loss

            watch_item = self.watchlist.get(symbol)
            if watch_item is None:
                return
            watch_item.entry_price = entry_trigger
            watch_item.stop_loss = stop_price
            if bp is not None and bp > 0:
                watch_item.breakout_price = bp
            watch_item.selected_orb_plan = {
                "window": str(plan.get("window", "")),
                "risk_percent": risk_pct,
                "entry_trigger": entry_trigger,
                "stop_price": stop_price,
                "breakout_price": bp,
                "buffer_pct": buffer_pct,
                "shares": shares_val,
                "capital_percent": float(cap_pct or 0.0),
                "stop_adr": float(sl_adr) if sl_adr is not None else None,
                "selected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

            # Keep watchlist_scores cache consistent
            if hasattr(self, "watchlist_scores") and symbol in self.watchlist_scores:
                self.watchlist_scores[symbol].update(
                    {
                        "entry_price": entry_trigger,
                        "orb_high": orb_high,
                        "breakout_price": bp,
                        "target_price": 0.0,
                        "buffer_pct": buffer_pct,
                        "stop_loss": stop_price,
                        "risk_percent": risk_pct,
                        "position_percent": cap_pct,
                        "stop_adr": sl_adr,
                        "trade_plan": desc,
                    }
                )
            self._save_state()

            # If the symbol is already queued, immediately recompute its
            # candidate with this saved window/risk/buffer.  Unqueued symbols
            # remain unqueued until the user explicitly moves them to the queue.
            manager = self.__dict__.get("execution_queue_manager")
            env = (
                self.watchlist_env_combo.currentText()
                if hasattr(self, "watchlist_env_combo")
                else "PROD"
            )
            if manager is not None and manager.get_item(symbol, env) is not None:
                self.refresh_execution_queue(
                    env,
                    show_log=False,
                    symbols=[symbol],
                    create_missing=False,
                )
        finally:
            self._updating_watchlist_orb_selection = False

    def refresh_watchlist_orb_statuses_with_data(self) -> None:
        """Refresh intraday data, then evaluate ORB entry status for all watchlist rows."""
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.watchlist_controller import WatchlistController

        controller = get_controller(self, "watchlist_controller", WatchlistController)
        controller.refresh_orb_statuses_with_data()

    def refresh_all_watchlist_orb_statuses(self) -> None:
        """Evaluate aggregate ORB status for every watchlist symbol without changing selection."""
        from src.ui.controllers.base import get_controller
        from src.ui.controllers.watchlist_controller import WatchlistController

        controller = get_controller(self, "watchlist_controller", WatchlistController)
        controller.refresh_all_orb_statuses()

    def _apply_cached_orb_statuses_to_watchlist_table(self) -> None:
        if not hasattr(self, "watchlist_table") or not hasattr(
            self, "watchlist_scores"
        ):
            return

        for row in range(self.watchlist_table.rowCount()):
            symbol_item = self.watchlist_table.item(row, 0)
            status_item = self.watchlist_table.item(row, 4)
            if symbol_item is None or status_item is None:
                continue

            symbol = symbol_item.text().strip().upper()
            orb_status = self.watchlist_scores.get(symbol, {}).get("orb_status")
            display_status = self._watchlist_display_status(
                status_item.text().strip(), orb_status
            )
            status_item.setText(display_status)

            row_color = self._watchlist_status_row_color(display_status, orb_status)
            if row_color is None:
                continue

            for col in range(self.watchlist_table.columnCount()):
                cell = self.watchlist_table.item(row, col)
                if cell:
                    cell.setBackground(row_color)
                    cell.setForeground(QColor(255, 255, 255))

    def _refresh_selected_watchlist_orb_panel(self) -> None:
        if not hasattr(self, "watchlist_table"):
            return
        selected = (
            self.watchlist_table.selectionModel().selectedRows()
            if self.watchlist_table.selectionModel()
            else []
        )
        if not selected:
            return
        sym_item = self.watchlist_table.item(selected[0].row(), 0)
        if sym_item:
            self.refresh_watchlist_orb_panel(sym_item.text().strip().upper())

    def _calculate_watchlist_orb_records_for_symbol(self, symbol: str) -> list:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return []

        account_size = (
            self._parse_float(self.account_size_input, 0.0)
            if hasattr(self, "account_size_input")
            else 0.0
        )
        selected_risk_percent = (
            self._parse_float(self.risk_percent_input, 0.0) / 100.0
            if hasattr(self, "risk_percent_input")
            else 0.01
        )
        risk_cases = self._orb_risk_cases(selected_risk_percent)
        adr_percent = self._calculate_adr_percent_for_symbol(symbol)
        breakout_price = self._watchlist_breakout_price_for_symbol(symbol)
        buffer_pct = self._watchlist_orb_buffer_pct()
        breakout_trigger = (
            breakout_price * (1 + buffer_pct) if breakout_price > 0 else 0.0
        )
        current_live_price = self._watchlist_orb_signal_price(symbol)

        five_minute = self._latest_intraday_session(
            self._load_cached_intraday_interval(symbol, "5m", window_days=7)
        )
        one_minute = self._latest_intraday_session(
            self._load_cached_intraday_interval(symbol, "1m", window_days=7)
        )
        orb_windows = [
            ("1m", one_minute),
            ("5m", five_minute),
            ("30m", five_minute),
        ]

        records = []
        for risk_percent in risk_cases:
            for window, history in orb_windows:
                if history.empty:
                    records.append(
                        {
                            "risk_percent": risk_percent,
                            "window": window,
                            "valid": False,
                            "sizing": {},
                            "status_reason": "no_intraday",
                        }
                    )
                    continue

                orb_range = calculate_orb_range(symbol, history, window)
                if orb_range is None:
                    records.append(
                        {
                            "risk_percent": risk_percent,
                            "window": window,
                            "valid": False,
                            "sizing": {},
                            "status_reason": "no_orb",
                        }
                    )
                    continue

                orb_high = float(orb_range.high)
                stop_price = float(orb_range.low)
                entry_trigger = orb_high
                signal_price = current_live_price if current_live_price > 0 else 0.0
                entry_signal = evaluate_orb_entry_signal(
                    orb_high=orb_high,
                    orb_low=stop_price,
                    breakout_price=breakout_price if breakout_price > 0 else None,
                    current_price=signal_price,
                    buffer_pct=buffer_pct,
                )

                sizing = self._calculate_orb_position_values(
                    account_size=account_size,
                    risk_percent=risk_percent,
                    entry_price=entry_trigger,
                    stop_price=stop_price,
                    adr_percent=adr_percent,
                )
                breakout_plan_valid = breakout_price > 0 and orb_high > breakout_trigger
                plan_valid = breakout_plan_valid and self._orb_position_plan_is_valid(
                    sizing, adr_percent
                )
                if breakout_price <= 0:
                    status_reason = "no_breakout"
                elif orb_high <= breakout_trigger:
                    status_reason = "below_breakout"
                elif not self._orb_position_plan_is_valid(sizing, adr_percent):
                    status_reason = "invalid_sizing"
                elif entry_signal.signal == "confirmed_orb_breakout":
                    status_reason = "confirmed"
                else:
                    status_reason = "price_not_ready"

                records.append(
                    {
                        "risk_percent": risk_percent,
                        "window": window,
                        "valid": plan_valid,
                        "sizing": sizing,
                        "entry_signal_key": entry_signal.signal,
                        "status_reason": status_reason,
                    }
                )
        return records

    def _watchlist_breakout_price_for_symbol(self, symbol: str) -> float:
        item = self.watchlist.get(symbol) if hasattr(self, "watchlist") else None
        return self._positive_finite_number(
            getattr(item, "breakout_price", None), default=0.0
        )

    @staticmethod
    def _positive_finite_number(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return number if math.isfinite(number) and number > 0 else default

    def _watchlist_orb_buffer_pct(self) -> float:
        if not hasattr(self, "watchlist_buffer_pct_input"):
            return 0.001
        try:
            text = self.watchlist_buffer_pct_input.text().strip()
            raw_percent = float(text) if text else 0.10
        except (TypeError, ValueError, OverflowError):
            return 0.001
        if not math.isfinite(raw_percent) or raw_percent < 0 or raw_percent > 100:
            return 0.001
        return raw_percent / 100.0

    def _watchlist_orb_signal_price(self, symbol: str) -> float:
        current_live_price = self._positive_finite_number(
            getattr(self, "latest_intraday_prices", {}).get(symbol, 0.0), default=0.0
        )
        if current_live_price > 0:
            return current_live_price
        try:
            daily_history = self._load_chart_history_for_timeframe(
                symbol, "1D", use_live_fallback=False, window_days=10
            )
            if (
                daily_history is not None
                and not daily_history.empty
                and "Close" in daily_history.columns
            ):
                return self._positive_finite_number(
                    daily_history["Close"].iloc[-1], default=0.0
                )
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _derive_watchlist_orb_status(records: list) -> str:
        if not records:
            return "NO_INTRADAY"

        reasons = [r.get("status_reason") for r in records]
        if reasons and all(reason == "no_intraday" for reason in reasons):
            return "NO_INTRADAY"

        valid_records = [r for r in records if r.get("valid") and r.get("sizing")]
        if not valid_records:
            if any(reason == "below_breakout" for reason in reasons):
                return "BELOW_BREAKOUT"
            return "NO_VALID_ORB"

        signals = [r.get("entry_signal_key", "no_entry") for r in valid_records]
        if any(s == "confirmed_orb_breakout" for s in signals):
            return "BUY_READY"
        if any(s == "orb_only_inside_base" for s in signals):
            return "BELOW_BREAKOUT"
        if all(s == "no_entry" for s in signals):
            return "WAITING_ENTRY"
        return "WATCHING"

    def refresh_watchlist_orb_panel(self, symbol: str = "") -> None:
        """Populate the ORB position plan below the watchlist table for the given symbol.

        Uses account_size_input directly — identical to refresh_orb_trade_plan_table —
        so the numbers always match the Trade Plan tab.
        """
        if not hasattr(self, "watchlist_orb_table"):
            return

        symbol = (symbol or "").strip().upper()
        if hasattr(self, "watchlist_orb_symbol_label"):
            self.watchlist_orb_symbol_label.setText(
                symbol if symbol else "Select a watchlist symbol to view its ORB plan"
            )
            self.watchlist_orb_symbol_label.setStyleSheet(
                "font-weight: bold; color: #ffffff;"
                if symbol
                else "font-weight: bold; color: #aaaaaa;"
            )

        # Always reload the Daily Breakout $ field from this symbol's watchlist breakout_price.
        # Signals are blocked inside _load_breakout_price_for_symbol so no save-back loop fires.
        # We track the last-displayed symbol so we don't stomp on a user edit mid-typing for
        # the SAME symbol, but we always update when the symbol actually changes.
        if symbol and hasattr(self, "watchlist_breakout_price_input"):
            last_orb_symbol = getattr(self, "_last_orb_panel_symbol", None)
            if last_orb_symbol != symbol:
                self._load_breakout_price_for_symbol(symbol)
                self._last_orb_panel_symbol = symbol

        self.watchlist_orb_column_data = {}
        table = self.watchlist_orb_table
        table.setRowCount(0)
        table.clearSpans()

        metric_labels = [
            "Recommendation",
            "Entry Signal",
            "ORB High",
            "Breakout Price",
            "Entry Trigger",
            "Stop Price",
            "Risk / Share",
            "Shares",
            "Investment",
            "Capital %",
            "ADR %",
            "Stop Loss %",
            "SL / ADR",
        ]
        header_rows = 3  # row 0: Select, row 1: Risk %, row 2: Window
        table.setRowCount(len(metric_labels) + header_rows)
        table.setItem(0, 0, QTableWidgetItem("Select"))
        table.setItem(1, 0, QTableWidgetItem("Risk %"))
        table.setItem(2, 0, QTableWidgetItem("Window"))
        for row, label in enumerate(metric_labels):
            table.setItem(row + header_rows, 0, QTableWidgetItem(label))

        if not symbol:
            return

        # Read manually entered daily breakout price and buffer from UI inputs
        breakout_price = 0.0
        if hasattr(self, "watchlist_breakout_price_input"):
            breakout_price = self._positive_finite_number(
                self.watchlist_breakout_price_input.text().strip(), default=0.0
            )
        buffer_pct = self._watchlist_orb_buffer_pct()
        breakout_trigger = (
            breakout_price * (1 + buffer_pct) if breakout_price > 0 else 0.0
        )

        # Use account_size_input directly — same as refresh_orb_trade_plan_table
        account_size = (
            self._parse_float(self.account_size_input, 0.0)
            if hasattr(self, "account_size_input")
            else 0.0
        )
        selected_risk_percent = (
            self._parse_float(self.risk_percent_input, 0.0) / 100.0
            if hasattr(self, "risk_percent_input")
            else 0.01
        )
        risk_cases = self._orb_risk_cases(selected_risk_percent)
        adr_percent = self._calculate_adr_percent_for_symbol(symbol)

        # Resolve the actual current price for entry signal evaluation.
        # Priority: live intraday price â†’ last close from daily history.
        current_live_price = getattr(self, "latest_intraday_prices", {}).get(
            symbol, 0.0
        )
        if current_live_price <= 0:
            try:
                _daily_hist = self._load_chart_history_for_timeframe(
                    symbol, "1D", use_live_fallback=False, window_days=10
                )
                if (
                    _daily_hist is not None
                    and not _daily_hist.empty
                    and "Close" in _daily_hist.columns
                ):
                    current_live_price = float(_daily_hist["Close"].iloc[-1])
            except Exception:
                pass

        five_minute = self._latest_intraday_session(
            self._load_cached_intraday_interval(symbol, "5m", window_days=7)
        )
        one_minute = self._latest_intraday_session(
            self._load_cached_intraday_interval(symbol, "1m", window_days=7)
        )
        if five_minute.empty and self._can_start_intraday_fetch(symbol, 7):
            self.start_intraday_fetch(symbol, window_days=7)

        orb_windows = [
            ("1m", one_minute),
            ("5m", five_minute),
            ("30m", five_minute),
        ]

        records = []
        for risk_percent in risk_cases:
            for window, history in orb_windows:
                if history.empty:
                    records.append(
                        {
                            "risk_percent": risk_percent,
                            "window": window,
                            "valid": False,
                            "recommendation_score": -1.0,
                            "values": ["No cache"] + [""] * (len(metric_labels) - 1),
                            "sizing": {},
                            "status_reason": "no_intraday",
                        }
                    )
                    continue
                orb_range = calculate_orb_range(symbol, history, window)
                if orb_range is None:
                    records.append(
                        {
                            "risk_percent": risk_percent,
                            "window": window,
                            "valid": False,
                            "recommendation_score": -1.0,
                            "values": ["No ORB"] + [""] * (len(metric_labels) - 1),
                            "sizing": {},
                            "status_reason": "no_orb",
                        }
                    )
                    continue
                orb_high = float(orb_range.high)
                stop_price = float(orb_range.low)
                entry_trigger = orb_high

                # Evaluate the combined entry signal using the actual current live price.
                # This shows the REAL zone the stock is in right now, not a hypothetical.
                from src.core.orb import evaluate_orb_entry_signal

                signal_price = current_live_price if current_live_price > 0 else 0.0
                entry_signal = evaluate_orb_entry_signal(
                    orb_high=orb_high,
                    orb_low=stop_price,
                    breakout_price=breakout_price if breakout_price > 0 else None,
                    current_price=signal_price,
                    buffer_pct=buffer_pct,
                )

                sizing = self._calculate_orb_position_values(
                    account_size=account_size,
                    risk_percent=risk_percent,
                    entry_price=entry_trigger,
                    stop_price=stop_price,
                    adr_percent=adr_percent,
                )
                breakout_plan_valid = breakout_price > 0 and orb_high > breakout_trigger
                column_valid = breakout_plan_valid and self._orb_position_plan_is_valid(
                    sizing, adr_percent
                )
                if breakout_price <= 0:
                    status_reason = "no_breakout"
                elif orb_high <= breakout_trigger:
                    status_reason = "below_breakout"
                elif not self._orb_position_plan_is_valid(sizing, adr_percent):
                    status_reason = "invalid_sizing"
                elif entry_signal.signal == "confirmed_orb_breakout":
                    status_reason = "confirmed"
                else:
                    status_reason = "price_not_ready"
                recommendation_score = self._score_orb_position_recommendation(
                    sizing, risk_percent
                )

                # Human-readable signal label + machine key for aggregate status
                signal_key = entry_signal.signal
                signal_display = {
                    "missing_breakout_price": "Missing breakout",
                    "orb_high_below_breakout_trigger": "ORB below breakout",
                    "confirmed_orb_breakout": "✓ Confirmed",
                    "orb_only_inside_base": "⚠ ORB only / below BKT",
                    "structural_breakout_not_fully_confirmed": "◑ Partial (probe)",
                    "no_entry": "✗ No entry",
                }.get(entry_signal.signal, entry_signal.signal)
                if False:
                    signal_display = "✗ ORB < Breakout (invalid)"

                records.append(
                    {
                        "window": window,
                        "risk_percent": risk_percent,
                        "orb_high": orb_high,
                        "entry_price": orb_high,
                        "entry_trigger": entry_trigger,
                        "stop_price": stop_price,
                        "valid": column_valid,
                        "entry_signal_key": signal_key,
                        "status_reason": status_reason,
                        "recommendation_score": recommendation_score,
                        "sizing": sizing,
                        "values": [
                            self._format_orb_recommendation(
                                recommendation_score, column_valid
                            ),
                            signal_display,
                            f"{orb_high:.2f}",
                            f"{breakout_price:.2f}" if breakout_price > 0 else "—",
                            f"{entry_trigger:.2f}",
                            f"{stop_price:.2f}",
                            f"{sizing['risk_per_share']:.2f}",
                            f"{sizing['shares']:.0f}",
                            f"${sizing['investment']:.2f}",
                            f"{sizing['capital_percent']:.1f}%",
                            "" if adr_percent is None else f"{adr_percent:.2f}%",
                            f"{sizing['stop_loss_percent']:.2f}%",
                            (
                                ""
                                if sizing["sl_adr"] is None
                                else f"{sizing['sl_adr']:.0f}%"
                            ),
                        ],
                    }
                )

        records = self._sort_orb_plan_records(records)
        table.setColumnCount(1 + len(records))
        table.setHorizontalHeaderLabels([""] * table.columnCount())
        for col in range(table.columnCount()):
            table.setColumnHidden(col, False)

        valid_count = 0
        show_valid_only = (
            hasattr(self, "watchlist_orb_valid_only_checkbox")
            and self.watchlist_orb_valid_only_checkbox.isChecked()
        )
        saved_plan = getattr(self.watchlist.get(symbol), "selected_orb_plan", None)
        saved_window = (
            str(saved_plan.get("window", "") or "")
            if isinstance(saved_plan, dict)
            else ""
        )
        try:
            saved_risk_percent = (
                float(saved_plan.get("risk_percent"))
                if isinstance(saved_plan, dict)
                and saved_plan.get("risk_percent") is not None
                else None
            )
        except (TypeError, ValueError):
            saved_risk_percent = None
        for col, record in enumerate(records, start=1):
            is_valid = record.get("valid", False)
            if is_valid:
                valid_count += 1

            should_hide = show_valid_only and (not is_valid or valid_count > 5)

            # Row 0: centred checkbox
            cb = QCheckBox()
            if not is_valid:
                cb.setEnabled(False)
                cb.setToolTip("This plan does not meet the position and risk checks")
            else:
                cb.setToolTip("Apply and save this plan for the execution queue")
            if (
                is_valid
                and record.get("window") == saved_window
                and saved_risk_percent is not None
                and abs(float(record.get("risk_percent", 0.0)) - saved_risk_percent)
                < 0.000001
            ):
                # Set before wiring the signal so rebuilding the panel does not
                # overwrite the saved choice or trigger a redundant refresh.
                cb.setChecked(True)
            cb.toggled.connect(
                lambda checked, c=col: self._on_watchlist_orb_plan_selected(c, checked)
            )
            cb_wrapper = QWidget()
            cb_layout = QHBoxLayout(cb_wrapper)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb_layout.setAlignment(Qt.AlignCenter)
            cb_layout.addWidget(cb)
            table.setCellWidget(0, col, cb_wrapper)

            table.setItem(
                1, col, QTableWidgetItem(f"{record['risk_percent'] * 100:.2f}%")
            )
            table.setItem(2, col, QTableWidgetItem(record["window"]))

            sizing = record.get("sizing", {})
            for row, value in enumerate(record["values"]):
                cell = QTableWidgetItem(value)
                metric_name = metric_labels[row]
                if (
                    (
                        metric_name == "Capital %"
                        and sizing
                        and (
                            sizing.get("capital_percent", 0) < MIN_CAPITAL_PERCENT
                            or sizing.get("capital_percent", 0) >= MAX_CAPITAL_PERCENT
                        )
                    )
                    or (
                        metric_name == "Stop Loss %"
                        and sizing
                        and adr_percent is not None
                        and adr_percent > 0
                        and sizing.get("stop_loss_percent", 0) >= adr_percent
                    )
                    or (
                        metric_name == "SL / ADR"
                        and sizing
                        and sizing.get("sl_adr") is not None
                        and (
                            sizing["sl_adr"] < MIN_STOP_ADR
                            or sizing["sl_adr"] > MAX_STOP_ADR
                        )
                    )
                ):
                    cell.setBackground(QColor(210, 70, 60))  # coral red — readable
                    cell.setForeground(QColor(255, 255, 255))
                elif is_valid:
                    cell.setBackground(QColor(39, 174, 96))  # emerald green — readable
                    cell.setForeground(QColor(255, 255, 255))
                table.setItem(row + header_rows, col, cell)

            # Store plan data for all columns (not just valid) so the checkbox handler can read it
            self.watchlist_orb_column_data[col] = {
                "symbol": symbol,
                "window": record["window"],
                "risk_percent": record["risk_percent"],
                "orb_high": record.get("orb_high", record.get("entry_price", 0.0)),
                "entry_price": record.get("orb_high", record.get("entry_price", 0.0)),
                "entry_trigger": record.get(
                    "entry_trigger", record.get("entry_price", 0.0)
                ),
                "breakout_price": breakout_price if breakout_price > 0 else None,
                "buffer_pct": buffer_pct,
                "stop_price": record.get("stop_price", 0.0),
                "sizing": record.get("sizing", {}),
                "valid": is_valid,
            }

            table.setColumnHidden(col, should_hide)

        # â”€â”€ Derive an aggregate ORB entry status for this symbol â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Base the status on VALID records only (invalid columns can't be entered).
        # If there are no valid plans at all, that itself means NO_ENTRY.
        valid_records = [
            r
            for r in records
            if r.get("valid")
            and r.get("sizing")  # must be valid AND have real sizing data
        ]
        if valid_records:
            signals = [r.get("entry_signal_key", "no_entry") for r in valid_records]
            if any(s == "confirmed_orb_breakout" for s in signals):
                orb_status = "BUY_READY"
            elif all(s == "no_entry" for s in signals):
                orb_status = "NO_ENTRY"
            else:
                orb_status = "WATCHING"
        else:
            # No valid ORB plans at all â†’ nothing to enter on
            orb_status = "NO_ENTRY"

        orb_status = self._derive_watchlist_orb_status(records)

        if not hasattr(self, "watchlist_scores"):
            self.watchlist_scores = {}
        if symbol not in self.watchlist_scores:
            self.watchlist_scores[symbol] = {}
        prev_status = self.watchlist_scores[symbol].get("orb_status")
        self.watchlist_scores[symbol]["orb_status"] = orb_status
        # Only repopulate the table when the status actually changes to avoid flicker
        if prev_status != orb_status:
            self.populate_watchlist_table()

    def add_manual_watchlist_item(self) -> None:
        """Add or update a watchlist item from manual inputs."""
        symbol = self.watchlist_symbol_input.text().strip().upper()
        name = self.watchlist_name_input.text().strip() or symbol

        if not symbol:
            QMessageBox.warning(
                self, "Invalid input", "Enter a symbol before adding to the watchlist."
            )
            return

        self.watchlist.add(symbol=symbol, name=name)
        self.populate_watchlist_table()
        self.update_dashboard_summary()
        self._save_state()
        self.prefetch_intraday_cache_for_symbol(symbol)
        self.watchlist_symbol_input.clear()
        self.watchlist_name_input.clear()
        self.append_log(f"Added/updated {symbol} in watchlist.")

    def _seed_trade_plan_fields(
        self,
        symbol: str,
        price: Optional[float] = None,
        name: str = "",
        overwrite: bool = False,
    ) -> None:
        """Populate chart fields from a selected symbol (Trade Plan tab removed)."""
        symbol = symbol.strip().upper()
        if not symbol:
            return
        if not hasattr(self, "symbol_input"):
            self._set_chart_symbol(symbol)
            self.refresh_watchlist_orb_panel(symbol)
            return

        self.symbol_input.setText(symbol)
        self._set_chart_symbol(symbol)
        if price and price > 0:
            if overwrite or not self.entry_price_input.text().strip():
                self.entry_price_input.setText(f"{price:.2f}")
            if overwrite or not self.stop_loss_input.text().strip():
                self.stop_loss_input.setText(f"{price * 0.92:.2f}")
        if name and (overwrite or not self.reason_input.toPlainText().strip()):
            self.reason_input.setPlainText(
                f"Watching {symbol} ({name}) from scanner/watchlist."
            )
        self.update_trade_plan_feedback()
        self.refresh_orb_trade_plan_table()

    def update_trade_prices_from_latest(self, symbol: str, latest_price: float) -> None:
        """Update active trade-plan prices from a refreshed market price."""
        symbol = symbol.strip().upper()
        if not symbol or latest_price <= 0:
            return

        self.latest_intraday_prices[symbol] = float(latest_price)
        if not hasattr(self, "symbol_input"):
            return
        active_symbol = self.symbol_input.text().strip().upper()
        if active_symbol != symbol:
            return

        old_entry_block = self.entry_price_input.blockSignals(True)
        old_stop_block = self.stop_loss_input.blockSignals(True)
        self.entry_price_input.setText(f"{latest_price:.2f}")
        self.stop_loss_input.setText(f"{latest_price * 0.92:.2f}")
        self.entry_price_input.blockSignals(old_entry_block)
        self.stop_loss_input.blockSignals(old_stop_block)
        self.update_trade_plan_feedback()

    def calculate_position_size(
        self, show_warnings: bool = True, update_output: bool = True
    ) -> bool:
        """Calculate shares from account risk, entry, and stop."""
        inputs = self._trade_plan_inputs()
        account_size = self._trade_plan_number(inputs["account_size"])
        entry_price = self._trade_plan_number(inputs["entry_price"])
        stop_loss = self._trade_plan_number(inputs["stop_loss"])
        risk_percent = self._trade_plan_number(inputs["risk_percent"])

        problem = ""
        if account_size is None or account_size <= 0:
            problem = "Enter a finite account size greater than zero."
        elif entry_price is None or entry_price <= 0:
            problem = "Enter a finite entry price greater than zero."
        elif stop_loss is None or stop_loss <= 0:
            problem = "Enter a finite stop loss greater than zero."
        elif stop_loss >= entry_price:
            problem = "For a long trade, the stop loss must be below the entry price."
        elif risk_percent is None or not 0 < risk_percent <= 100:
            problem = "Risk must be greater than 0% and no more than 100%."

        if problem:
            self._set_trade_plan_position_size(0)
            if update_output:
                self._set_trade_plan_feedback_text(problem)
            if show_warnings:
                QMessageBox.warning(self, "Invalid trade plan", problem)
            return False

        risk_fraction = risk_percent / 100.0
        sizing = PositionSizer(
            account_size=account_size,
            max_risk_per_trade=risk_fraction,
        ).size_risk_based(entry_price, stop_loss, risk_fraction)
        if sizing.shares <= 0:
            problem = "The position size could not be calculated safely."
            self._set_trade_plan_position_size(0)
            if update_output:
                self._set_trade_plan_feedback_text(problem)
            if show_warnings:
                QMessageBox.warning(self, "Invalid trade plan", problem)
            return False

        self._set_trade_plan_position_size(sizing.shares)
        if update_output:
            self._set_trade_plan_feedback_text(
                "Position size: "
                f"{sizing.shares:,} shares | "
                f"${sizing.dollar_amount:,.2f} position | "
                f"${sizing.risk_amount:,.2f} risk"
            )
        return True

    def review_trade(self, show_warnings: bool = True) -> bool:
        """Review a planned trade using basic rule validation."""
        if not self.calculate_position_size(
            show_warnings=show_warnings, update_output=False
        ):
            return False

        inputs = self._trade_plan_inputs()
        account_size = self._trade_plan_number(inputs["account_size"])
        entry_price = self._trade_plan_number(inputs["entry_price"])
        stop_loss = self._trade_plan_number(inputs["stop_loss"])
        risk_percent = self._trade_plan_number(inputs["risk_percent"])
        position_size = self._trade_plan_integer(inputs["position_size"])
        # The fixed take-profit field is optional because the strategy uses a
        # rule-based EMA exit.  Retain a supplied guide price for persistence.
        take_profit = self._trade_plan_number(inputs["take_profit"], blank=0.0)
        symbol = self._trade_plan_text(inputs["symbol"]).upper()
        reason_widget = inputs["reason"]
        reason = (
            reason_widget.toPlainText().strip()
            if reason_widget is not None and hasattr(reason_widget, "toPlainText")
            else ""
        )

        if (
            not symbol
            or account_size is None
            or entry_price is None
            or entry_price <= 0
            or stop_loss is None
            or stop_loss <= 0
            or stop_loss >= entry_price
            or risk_percent is None
            or not 0 < risk_percent <= 100
            or position_size <= 0
            or take_profit is None
            or take_profit < 0
        ):
            # This should only be reachable when a field changed while sizing
            # was being recalculated; keep the form fail-closed in that case.
            problem = "Complete the trade-plan fields with finite values before review."
            self._set_trade_plan_feedback_text(problem)
            if show_warnings:
                QMessageBox.warning(self, "Invalid trade plan", problem)
            return False

        risk_fraction = risk_percent / 100.0
        sizing = PositionSizer(
            account_size=account_size,
            max_risk_per_trade=risk_fraction,
        ).size_risk_based(entry_price, stop_loss, risk_fraction)
        reviewer = self.__dict__.get("reviewer") or TradeReviewer()
        review = reviewer.review_trade(
            TradeSetup(
                symbol=symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                size_shares=position_size,
                risk_amount=sizing.risk_amount,
                reasoning=reason,
            )
        )

        feedback = f"{'Approved' if review.approved else 'Not approved'} ({review.confidence:.0%}): {review.summary}"
        if review.violations:
            feedback += "\nViolations: " + "; ".join(review.violations)
        if review.recommendations:
            feedback += "\nRecommendations: " + "; ".join(review.recommendations)
        self._set_trade_plan_feedback_text(feedback)

        if show_warnings and not review.approved:
            QMessageBox.warning(self, "Trade plan needs changes", feedback)
        return review.approved

    def update_trade_plan_feedback(self) -> None:
        """Automatically update position size and trade review as fields change."""
        if self.__dict__.get("_updating_trade_plan_feedback"):
            return
        self.__dict__["_updating_trade_plan_feedback"] = True
        try:
            self._show_trade_plan_actions()
            if self.calculate_position_size(show_warnings=False, update_output=False):
                self.review_trade(show_warnings=False)
            else:
                self._set_trade_plan_feedback_text(
                    "Enter a valid account size, entry, stop, and risk to review the plan."
                )
        finally:
            self.__dict__["_updating_trade_plan_feedback"] = False

    def save_trade_plan(self) -> None:
        """Save the current trade plan."""
        if not self.calculate_position_size(show_warnings=True, update_output=False):
            return
        if not self.review_trade(show_warnings=True):
            return

        inputs = self._trade_plan_inputs()
        symbol = self._trade_plan_text(inputs["symbol"]).upper()
        account_size = self._trade_plan_number(inputs["account_size"])
        entry_price = self._trade_plan_number(inputs["entry_price"])
        stop_loss = self._trade_plan_number(inputs["stop_loss"])
        take_profit = self._trade_plan_number(inputs["take_profit"], blank=0.0)
        risk_percent = self._trade_plan_number(inputs["risk_percent"])
        position_size = self._trade_plan_integer(inputs["position_size"])
        reason_widget = inputs["reason"]
        reason = (
            reason_widget.toPlainText().strip()
            if reason_widget is not None and hasattr(reason_widget, "toPlainText")
            else ""
        )
        if (
            not symbol
            or account_size is None
            or account_size <= 0
            or entry_price is None
            or entry_price <= 0
            or stop_loss is None
            or stop_loss <= 0
            or stop_loss >= entry_price
            or take_profit is None
            or take_profit < 0
            or risk_percent is None
            or not 0 < risk_percent <= 100
            or position_size <= 0
        ):
            # Keep this guard even though the preceding review normally catches
            # invalid values, so persistence cannot be reached with a partial form.
            QMessageBox.warning(
                self,
                "Invalid trade plan",
                "Enter a symbol and complete all required finite trade-plan values.",
            )
            return

        symbol_widget = inputs["symbol"]
        if symbol_widget is not None and hasattr(symbol_widget, "setText"):
            symbol_widget.setText(symbol)

        manager = self.__dict__.get("trade_manager")
        if manager is None:
            manager = TradePlanManager()
            self.trade_manager = manager

        active_plan = next(
            (
                plan
                for plan in manager.get_active_plans()
                if plan.symbol.strip().upper() == symbol
            ),
            None,
        )
        if active_plan is None:
            manager.add_plan(
                TradePlan(
                    symbol=symbol,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    position_size=position_size,
                    reason=reason,
                    risk_percent=risk_percent / 100.0,
                )
            )
        else:
            active_plan.entry_price = entry_price
            active_plan.stop_loss = stop_loss
            active_plan.take_profit = take_profit
            active_plan.position_size = position_size
            active_plan.reason = reason
            active_plan.risk_percent = risk_percent / 100.0

        self._save_state()
        self.populate_trade_plan_table()
        self._set_trade_plan_feedback_text(f"Saved active trade plan for {symbol}.")

    def populate_trade_plan_table(self) -> None:
        """Populate the trade plan table with active plans."""
        table = self.__dict__.get("trade_plan_table")
        if table is None:
            return

        manager = self.__dict__.get("trade_manager")
        plans = manager.get_active_plans() if manager is not None else []
        table.setRowCount(len(plans))
        for row, plan in enumerate(plans):
            symbol_item = QTableWidgetItem(plan.symbol)
            symbol_item.setData(Qt.UserRole, plan.symbol)
            table.setItem(row, 0, symbol_item)
            table.setItem(row, 1, QTableWidgetItem(f"{plan.entry_price:.2f}"))
            table.setItem(row, 2, QTableWidgetItem(f"{plan.stop_loss:.2f}"))
            exit_model = "Rule-based EMA exit"
            if plan.take_profit > 0:
                exit_model += f" (guide ${plan.take_profit:.2f})"
            table.setItem(row, 3, QTableWidgetItem(exit_model))
            table.setItem(row, 4, QTableWidgetItem(plan.status.title()))

        self._attach_trade_plan_table_if_needed(table)
        table.setVisible(True)
        self._show_trade_plan_actions()

    def load_saved_trade_plan(self, row: int, column: int) -> None:
        """Load a saved trade plan back into the form."""
        table = self.__dict__.get("trade_plan_table")
        manager = self.__dict__.get("trade_manager")
        if table is None or manager is None or row < 0 or row >= table.rowCount():
            return

        symbol_item = table.item(row, 0)
        if symbol_item is None:
            return
        symbol = str(symbol_item.data(Qt.UserRole) or symbol_item.text()).strip().upper()
        plan = next(
            (
                candidate
                for candidate in manager.get_active_plans()
                if candidate.symbol.strip().upper() == symbol
            ),
            None,
        )
        if plan is None:
            return

        inputs = self._trade_plan_inputs()
        values = {
            "symbol": plan.symbol,
            "entry_price": f"{plan.entry_price:.2f}",
            "stop_loss": f"{plan.stop_loss:.2f}",
            "take_profit": f"{plan.take_profit:.2f}" if plan.take_profit > 0 else "",
            "position_size": str(plan.position_size),
            "risk_percent": f"{getattr(plan, 'risk_percent', 0.01) * 100:g}",
        }
        previous_signal_states = []
        try:
            for name, value in values.items():
                widget = inputs[name]
                if widget is None or not hasattr(widget, "setText"):
                    continue
                previous_signal_states.append((widget, widget.blockSignals(True)))
                widget.setText(value)
            reason_widget = inputs["reason"]
            if reason_widget is not None and hasattr(reason_widget, "setPlainText"):
                previous_signal_states.append(
                    (reason_widget, reason_widget.blockSignals(True))
                )
                reason_widget.setPlainText(plan.reason)
        finally:
            for widget, previous_state in previous_signal_states:
                widget.blockSignals(previous_state)

        if self.__dict__.get("chart_symbol_input") is not None:
            self._set_chart_symbol(plan.symbol)
        self.update_trade_plan_feedback()

    def _trade_plan_inputs(self) -> Dict[str, Any]:
        """Return form widgets without assuming the mixin owns the window."""
        values = self.__dict__
        return {
            "symbol": values.get("symbol_input"),
            "entry_price": values.get("entry_price_input"),
            "stop_loss": values.get("stop_loss_input"),
            "take_profit": values.get("take_profit_input"),
            "position_size": values.get("position_size_input"),
            "account_size": values.get("account_size_input"),
            "risk_percent": values.get("risk_percent_input"),
            "reason": values.get("reason_input"),
        }

    @staticmethod
    def _trade_plan_text(widget: Any) -> str:
        if widget is None or not hasattr(widget, "text"):
            return ""
        return str(widget.text()).strip()

    @classmethod
    def _trade_plan_number(
        cls, widget: Any, blank: Optional[float] = None
    ) -> Optional[float]:
        text = cls._trade_plan_text(widget).replace(",", "").replace("%", "")
        if not text:
            return blank
        try:
            value = float(text)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    @classmethod
    def _trade_plan_integer(cls, widget: Any) -> int:
        value = cls._trade_plan_number(widget)
        if value is None or value <= 0 or not value.is_integer():
            return 0
        return int(value)

    def _set_trade_plan_position_size(self, shares: int) -> None:
        widget = self._trade_plan_inputs()["position_size"]
        if widget is None or not hasattr(widget, "setText"):
            return
        previous_state = widget.blockSignals(True)
        try:
            widget.setText(str(max(0, int(shares))))
        finally:
            widget.blockSignals(previous_state)

    def _set_trade_plan_feedback_text(self, text: str) -> None:
        output = self.__dict__.get("trade_review_output")
        if output is not None and hasattr(output, "setText"):
            output.setText(text)

    def _show_trade_plan_actions(self) -> None:
        widget = self.__dict__.get("trade_plan_widget")
        if widget is None or not hasattr(widget, "findChild"):
            return
        save_button = widget.findChild(QPushButton, "savePlanButton")
        if save_button is not None:
            save_button.setVisible(True)

    def _attach_trade_plan_table_if_needed(self, table: QTableWidget) -> None:
        """Attach the saved-plan table that older layouts created but omitted."""
        if table.parent() is not None:
            return
        trade_plan_widget = self.__dict__.get("trade_plan_widget")
        if trade_plan_widget is None or not hasattr(trade_plan_widget, "layout"):
            return
        outer_layout = trade_plan_widget.layout()
        orb_table = self.__dict__.get("orb_trade_plan_table")
        if outer_layout is None or orb_table is None:
            return

        for index in range(outer_layout.count()):
            right_layout = outer_layout.itemAt(index).layout()
            if right_layout is None or right_layout.indexOf(orb_table) < 0:
                continue
            label = self.__dict__.get("trade_plan_table_label")
            if label is None:
                label = QLabel("Saved Trade Plans")
                self.trade_plan_table_label = label
                right_layout.addWidget(label)
            right_layout.addWidget(table, 1)
            return
