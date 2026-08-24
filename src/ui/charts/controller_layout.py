"""Chart tab and control construction."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QDoubleValidator, QIntValidator, QKeySequence
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                             QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QPushButton, QShortcut, QSlider,
                             QTableWidget, QTextEdit, QVBoxLayout)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.api.kis_account_snapshot_dual import KisEnvironment
from src.ui.chart_bridge import ChartBridge
from src.ui.chart_settings_dialog import (
    CHART_SETTING_GROUPS,
    TRADINGVIEW_CHART_SETTINGS,
    TRADINGVIEW_STOCK_PROFILE_OPACITY_DEFAULT,
    TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME,
    ChartSettingsDialog,
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


class ChartsLayoutMixin:
    def _initialize_chart_setting_checkboxes(self, definitions, parent) -> None:
        """Create hidden state holders for settings shown in the modal dialog."""
        for name, label, default in definitions:
            if hasattr(self, name):
                continue
            checkbox = QCheckBox(label, parent)
            checkbox.setObjectName(name)
            checkbox.setChecked(default)
            checkbox.hide()
            setattr(self, name, checkbox)

    def show_chart_settings_dialog(self) -> None:
        """Open chart settings and refresh only the affected visible chart."""
        current_values = {
            name: getattr(self, name).isChecked()
            for _title, definitions in CHART_SETTING_GROUPS
            for name, _label, _default in definitions
            if hasattr(self, name)
        }
        opacity_slider = self.__dict__.get(
            TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME
        )
        if opacity_slider is not None:
            current_values[TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME] = (
                opacity_slider.value()
            )
        dialog = ChartSettingsDialog(current_values, self)
        if dialog.exec_() != dialog.Accepted:
            return

        changed_names = set()
        dialog_values = dialog.values()
        for name, checked in dialog_values.items():
            if name == TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME:
                continue
            checkbox = getattr(self, name, None)
            if checkbox is None or checkbox.isChecked() == checked:
                continue
            signals_were_blocked = checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(signals_were_blocked)
            changed_names.add(name)

        requested_opacity = int(
            dialog_values.get(
                TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME,
                TRADINGVIEW_STOCK_PROFILE_OPACITY_DEFAULT,
            )
        )
        if (
            opacity_slider is not None
            and opacity_slider.value() != requested_opacity
        ):
            signals_were_blocked = opacity_slider.blockSignals(True)
            opacity_slider.setValue(requested_opacity)
            opacity_slider.blockSignals(signals_were_blocked)
            changed_names.add(TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME)

        tradingview_names = {
            name for name, _label, _default in TRADINGVIEW_CHART_SETTINGS
        }
        tradingview_names.add(TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME)
        active_widget = (
            self.tabs.currentWidget() if hasattr(self, "tabs") else None
        )
        if changed_names & tradingview_names:
            if active_widget is getattr(self, "tradingview_widget", None):
                self.load_tradingview_chart(force=True)
            else:
                self._tradingview_tab_chart_stale = True

    def _build_combined_drawings(self, symbol: str, timeframe: str) -> list:
        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            return []
        drawings = self.chart_drawings.get(symbol_key, [])
        requested_timeframe = self._normalize_drawing_timeframe(timeframe)
        if not requested_timeframe:
            return list(drawings)
        return [
            drawing
            for drawing in drawings
            if isinstance(drawing, dict)
            and self._drawing_timeframes_match(
                requested_timeframe,
                self._resolve_drawing_timeframe(
                    drawing, default=requested_timeframe
                ),
            )
        ]

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
        self.usd_krw_rate_input = QLineEdit()
        self.usd_krw_rate_input.setPlaceholderText("Fetching...")
        self.usd_krw_rate_input.setReadOnly(True)
        self.risk_percent_input = QLineEdit("1")
        for input_widget in (
            self.entry_price_input,
            self.stop_loss_input,
            self.take_profit_input,
        ):
            price_validator = QDoubleValidator(
                0.0, 1_000_000_000.0, 6, input_widget
            )
            price_validator.setNotation(QDoubleValidator.StandardNotation)
            input_widget.setValidator(price_validator)
        position_size_validator = QIntValidator(0, 2_000_000_000, form_group)
        self.position_size_input.setValidator(position_size_validator)
        account_size_validator = QDoubleValidator(
            0.0, 1_000_000_000_000.0, 2, self.account_size_input
        )
        account_size_validator.setNotation(QDoubleValidator.StandardNotation)
        self.account_size_input.setValidator(account_size_validator)
        risk_validator = QDoubleValidator(0.0, 100.0, 4, self.risk_percent_input)
        risk_validator.setNotation(QDoubleValidator.StandardNotation)
        self.risk_percent_input.setValidator(risk_validator)
        self.reason_input = QTextEdit()
        self.trade_kis_environment_combo = QComboBox()
        self.trade_kis_environment_combo.addItem(KisEnvironment.PROD.value)
        self.trade_kis_environment_combo.currentTextChanged.connect(
            self.populate_trade_account_combo
        )
        self.trade_kis_environment_combo.setVisible(False)
        self.trade_kis_account_combo = QComboBox()
        self.trade_kis_account_combo.currentIndexChanged.connect(
            self.apply_cached_trade_account_size
        )
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
        form_layout.addRow("KIS Profile", QLabel("PROD — Live Trading"))
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
        self.account_size_input.textChanged.connect(
            self.recalculate_watchlist_scoreboard_sizes
        )
        self.risk_percent_input.textChanged.connect(
            self.recalculate_watchlist_scoreboard_sizes
        )
        self.reason_input.textChanged.connect(self.update_trade_plan_feedback)

        form_group.setLayout(form_layout)
        layout.addWidget(form_group, 1)

        right_layout = QVBoxLayout()
        self.orb_trade_plan_table = QTableWidget(0, 10)
        self.orb_trade_plan_table.setHorizontalHeaderLabels([""] * 10)
        self.orb_trade_plan_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.orb_trade_plan_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        right_layout.addWidget(QLabel("ORB Position Plan"))
        self.orb_valid_only_checkbox = QCheckBox("Show valid plans only")
        self.orb_valid_only_checkbox.setChecked(True)
        self.orb_valid_only_checkbox.stateChanged.connect(
            self.refresh_orb_trade_plan_table
        )
        right_layout.addWidget(self.orb_valid_only_checkbox)
        right_layout.addWidget(self.orb_trade_plan_table, 2)

        self.trade_plan_table = QTableWidget(0, 5)
        self.trade_plan_table.setHorizontalHeaderLabels(
            [
                "Symbol",
                "Entry",
                "Stop",
                "Exit Model",
                "Status",
            ]
        )
        self.trade_plan_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
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
            checkbox.stateChanged.connect(
                lambda _state: self.plot_intraday_watchlist_symbol()
            )
            settings_layout.addWidget(checkbox)
        settings_layout.addStretch(1)
        layout.addLayout(settings_layout)

        self.intraday_status_label = QLabel(
            "Select a watchlist symbol to load intraday data."
        )
        self.intraday_status_label.setWordWrap(True)
        layout.addWidget(self.intraday_status_label)

        if QWebEngineView is not None:
            self.intraday_chart_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.intraday_chart_channel = QWebChannel()
                self.intraday_chart_channel.registerObject(
                    "chartBridge", self.chart_bridge
                )
                self.intraday_chart_view.page().setWebChannel(
                    self.intraday_chart_channel
                )
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
        self.intraday_erase_all_button.clicked.connect(
            self.clear_current_chart_drawings
        )
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
        self.intraday_symbol_combo.currentTextChanged.connect(
            self._update_intraday_queue_btn
        )
        self.intraday_symbol_combo.currentTextChanged.connect(
            self._update_intraday_activate_btn
        )

        self.intraday_up_shortcut = QShortcut(
            QKeySequence(Qt.Key_Up), self.intraday_charts_widget
        )
        self.intraday_up_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_up_shortcut.activated.connect(
            lambda: self.step_intraday_watchlist_symbol(-1)
        )
        self.intraday_down_shortcut = QShortcut(
            QKeySequence(Qt.Key_Down), self.intraday_charts_widget
        )
        self.intraday_down_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_down_shortcut.activated.connect(
            lambda: self.step_intraday_watchlist_symbol(1)
        )
        self.intraday_target_shortcut = QShortcut(
            QKeySequence("T"), self.intraday_charts_widget
        )
        self.intraday_target_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_target_shortcut.activated.connect(self.enable_chart_target_mode)
        self.intraday_draw_shortcut = QShortcut(
            QKeySequence("D"), self.intraday_charts_widget
        )
        self.intraday_draw_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_draw_shortcut.activated.connect(self.enable_chart_drawing_mode)
        self.intraday_erase_shortcut = QShortcut(
            QKeySequence("E"), self.intraday_charts_widget
        )
        self.intraday_erase_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_erase_shortcut.activated.connect(self.enable_chart_erase_mode)
        self.intraday_full_view_shortcut = QShortcut(
            QKeySequence("F"), self.intraday_charts_widget
        )
        self.intraday_full_view_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_full_view_shortcut.activated.connect(self.reset_chart_full_view)
        self.intraday_queue_shortcut = QShortcut(
            QKeySequence("Q"), self.intraday_charts_widget
        )
        self.intraday_queue_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_queue_shortcut.activated.connect(self._intraday_queue_toggle)
        self.intraday_activate_shortcut = QShortcut(
            QKeySequence("A"), self.intraday_charts_widget
        )
        self.intraday_activate_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_activate_shortcut.activated.connect(
            self._intraday_activate_toggle
        )
        self.intraday_refresh_shortcut = QShortcut(
            QKeySequence("R"), self.intraday_charts_widget
        )
        self.intraday_refresh_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.intraday_refresh_shortcut.activated.connect(self._intraday_force_refresh)
        self._update_intraday_queue_btn()
        self._update_intraday_activate_btn()

    def _intraday_force_refresh(self) -> None:
        symbol = (
            self.intraday_symbol_combo.currentText().strip().upper()
            if hasattr(self, "intraday_symbol_combo")
            else ""
        )
        if symbol:
            window_days = self._get_intraday_window_days()
            self.start_intraday_fetch(symbol, window_days=window_days)
        self.plot_intraday_watchlist_symbol(allow_fetch=False)

    def _intraday_queue_toggle(self) -> None:
        symbol = (
            self.intraday_symbol_combo.currentText().strip().upper()
            if hasattr(self, "intraday_symbol_combo")
            else ""
        )
        if not symbol:
            return
        self._chart_queue_toggle(symbol)
        self._update_intraday_queue_btn()

    def _update_intraday_queue_btn(self, _text: str = "") -> None:
        btn = getattr(self, "intraday_queue_btn", None)
        if btn is None:
            return
        symbol = (
            self.intraday_symbol_combo.currentText().strip().upper()
            if hasattr(self, "intraday_symbol_combo")
            else ""
        )
        self._apply_chart_queue_btn_state(symbol, btn)

    def _build_tradingview_tab(self) -> None:
        """Build a TradingView widget tab for watchlist symbols."""
        layout = QVBoxLayout()
        self._initialize_chart_setting_checkboxes(
            TRADINGVIEW_CHART_SETTINGS, self.tradingview_widget
        )
        if not hasattr(self, TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME):
            opacity_slider = QSlider(Qt.Horizontal, self.tradingview_widget)
            opacity_slider.setObjectName(TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME)
            opacity_slider.setRange(20, 100)
            opacity_slider.setValue(TRADINGVIEW_STOCK_PROFILE_OPACITY_DEFAULT)
            opacity_slider.hide()
            setattr(
                self,
                TRADINGVIEW_STOCK_PROFILE_OPACITY_NAME,
                opacity_slider,
            )

        controls_layout = QHBoxLayout()
        self.tradingview_symbol_combo = QComboBox()
        self.tradingview_symbol_combo.setMinimumWidth(180)
        self.tradingview_symbol_combo.setEditable(True)
        self.tradingview_symbol_combo.lineEdit().textEdited.connect(
            self.filter_tradingview_symbol_combo
        )
        self.populate_tradingview_watchlist_symbols()
        self.tradingview_symbol_combo.activated.connect(
            lambda _index: self._schedule_tradingview_navigation_load()
        )

        previous_button = QPushButton("Previous")
        previous_button.clicked.connect(
            lambda: self.step_tradingview_watchlist_symbol(-1)
        )
        next_button = QPushButton("Next")
        next_button.clicked.connect(lambda: self.step_tradingview_watchlist_symbol(1))
        refresh_button = QPushButton("Load Chart (R)")
        refresh_button.clicked.connect(
            lambda: self.load_tradingview_chart(force=True, fetch_live=True)
        )

        controls_layout.addWidget(QLabel("Symbol:"))
        controls_layout.addWidget(self.tradingview_symbol_combo)
        self.tradingview_timeframe_combo = QComboBox()
        self.tradingview_timeframe_combo.addItems(["1D", "1H", "5M"])
        self.tradingview_timeframe_combo.currentTextChanged.connect(
            lambda _text: self.load_tradingview_chart(force=True)
        )
        controls_layout.addWidget(QLabel("Timeframe:"))
        controls_layout.addWidget(self.tradingview_timeframe_combo)
        self.tradingview_window_combo = QComboBox()
        self.tradingview_window_combo.addItems(["1D", "3D", "5D", "7D"])
        self.tradingview_window_combo.setCurrentText("7D")
        self.tradingview_window_combo.currentTextChanged.connect(
            lambda _text: self.load_tradingview_chart(force=True)
        )
        controls_layout.addWidget(QLabel("5M window:"))
        controls_layout.addWidget(self.tradingview_window_combo)
        controls_layout.addWidget(previous_button)
        controls_layout.addWidget(next_button)
        controls_layout.addWidget(refresh_button)
        controls_layout.addStretch(1)
        layout.addLayout(controls_layout)

        self.tradingview_status_label = QLabel(
            "TradingView widget uses public market symbols and requires internet access."
        )
        self.tradingview_status_label.setWordWrap(True)
        layout.addWidget(self.tradingview_status_label)

        if QWebEngineView is not None:
            self.tradingview_chart_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.tradingview_chart_channel = QWebChannel()
                self.tradingview_chart_channel.registerObject(
                    "chartBridge", self.chart_bridge
                )
                self.tradingview_chart_view.page().setWebChannel(
                    self.tradingview_chart_channel
                )
            self.tradingview_split_chart_view = QWebEngineView()
            if QWebChannel is not None:
                if not hasattr(self, "chart_bridge"):
                    self.chart_bridge = ChartBridge(self)
                self.tradingview_split_chart_channel = QWebChannel()
                self.tradingview_split_chart_channel.registerObject(
                    "chartBridge", self.chart_bridge
                )
                self.tradingview_split_chart_view.page().setWebChannel(
                    self.tradingview_split_chart_channel
                )
            for chart_view in (
                self.tradingview_chart_view,
                self.tradingview_split_chart_view,
            ):
                chart_view.loadFinished.connect(
                    lambda loaded, view=chart_view: (
                        self._resync_tradingview_drawings_in_view(view)
                        if loaded
                        else None
                    )
                )
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
        self.tradingview_line_tool_button.clicked.connect(
            self.toggle_tradingview_line_tool_mode
        )
        self.tradingview_erase_all_button = QPushButton("Erase All")
        self.tradingview_erase_all_button.setObjectName("eraseAllButton")
        self.tradingview_erase_all_button.clicked.connect(
            self.clear_current_chart_drawings
        )
        self.tradingview_set_target_button = QPushButton("Set Breakout Price (T)")
        self.tradingview_set_target_button.clicked.connect(
            self.enable_chart_target_mode
        )
        self.tradingview_clear_target_button = QPushButton("Clear Breakout")
        self.tradingview_clear_target_button.setObjectName("clearTargetButton")
        self.tradingview_clear_target_button.clicked.connect(
            self.clear_current_chart_target
        )
        self.tradingview_full_view_button = QPushButton("Full View (F)")
        self.tradingview_full_view_button.clicked.connect(self.reset_chart_full_view)
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
        tools_layout.addWidget(self.tradingview_queue_btn)
        tools_layout.addWidget(self.tradingview_activate_btn)
        tools_layout.addWidget(self.tradingview_full_view_button)
        tools_layout.addStretch(1)
        layout.addLayout(tools_layout)

        # Update canonical queue/activate controls whenever the symbol changes.
        self.tradingview_symbol_combo.currentTextChanged.connect(
            self._update_tradingview_queue_btn
        )
        self.tradingview_symbol_combo.currentTextChanged.connect(
            self._update_tradingview_activate_btn
        )

        self.tradingview_widget.setLayout(layout)
        self.tradingview_draw_shortcut = QShortcut(
            QKeySequence("D"), self.tradingview_widget
        )
        self.tradingview_draw_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_draw_shortcut.activated.connect(
            self.toggle_tradingview_line_tool_mode
        )
        self.tradingview_target_shortcut = QShortcut(
            QKeySequence("T"), self.tradingview_widget
        )
        self.tradingview_target_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_target_shortcut.activated.connect(
            self.enable_chart_target_mode
        )
        self.tradingview_queue_shortcut = QShortcut(
            QKeySequence("Q"), self.tradingview_widget
        )
        self.tradingview_queue_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_queue_shortcut.activated.connect(
            self._tradingview_queue_toggle
        )
        self.tradingview_activate_shortcut = QShortcut(
            QKeySequence("A"), self.tradingview_widget
        )
        self.tradingview_activate_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_activate_shortcut.activated.connect(
            self._tradingview_activate_toggle
        )
        self.tradingview_up_shortcut = QShortcut(
            QKeySequence(Qt.Key_Up), self.tradingview_widget
        )
        self.tradingview_up_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_up_shortcut.activated.connect(
            lambda: self.step_tradingview_watchlist_symbol(-1)
        )
        self.tradingview_down_shortcut = QShortcut(
            QKeySequence(Qt.Key_Down), self.tradingview_widget
        )
        self.tradingview_down_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_down_shortcut.activated.connect(
            lambda: self.step_tradingview_watchlist_symbol(1)
        )
        self.tradingview_left_shortcut = QShortcut(
            QKeySequence(Qt.Key_Left), self.tradingview_widget
        )
        self.tradingview_left_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_left_shortcut.activated.connect(
            lambda: self.pan_tradingview_chart_view(-self._chart_pan_step_bars())
        )
        self.tradingview_right_shortcut = QShortcut(
            QKeySequence(Qt.Key_Right), self.tradingview_widget
        )
        self.tradingview_right_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_right_shortcut.activated.connect(
            lambda: self.pan_tradingview_chart_view(self._chart_pan_step_bars())
        )
        self.tradingview_full_view_shortcut = QShortcut(
            QKeySequence("F"), self.tradingview_widget
        )
        self.tradingview_full_view_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_full_view_shortcut.activated.connect(
            self.reset_chart_full_view
        )
        self.tradingview_load_shortcut = QShortcut(
            QKeySequence(Qt.Key_F4), self.tradingview_widget
        )
        self.tradingview_load_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_load_shortcut.activated.connect(
            lambda: self.load_tradingview_chart(force=True, fetch_live=True)
        )
        self.tradingview_refresh_shortcut = QShortcut(
            QKeySequence("R"), self.tradingview_widget
        )
        self.tradingview_refresh_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.tradingview_refresh_shortcut.activated.connect(
            lambda: self.load_tradingview_chart(force=True, fetch_live=True)
        )
        self._update_tradingview_queue_btn()
        self._update_tradingview_activate_btn()
        self.load_tradingview_chart(show_empty_message=False)
