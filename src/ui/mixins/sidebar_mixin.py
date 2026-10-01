from __future__ import annotations

import datetime as dt
from typing import List, Optional

from PyQt5.QtCore import QDate, Qt
from PyQt5.QtGui import QColor, QTextCharFormat
from PyQt5.QtWidgets import (
    QCalendarWidget,
    QComboBox,
    QDateEdit,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from zoneinfo import ZoneInfo

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.core.trade_card_state import BoardStatus

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
        self.stock_sidebar.setMinimumWidth(190)
        self.stock_sidebar.setMaximumWidth(240)

        sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout()
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(6)

        self.sidebar_source_combo = QComboBox()
        self.sidebar_source_combo.setMinimumWidth(145)
        self.sidebar_source_combo.currentIndexChanged.connect(self.refresh_stock_sidebar)
        sidebar_layout.addWidget(self.sidebar_source_combo)

        self.sidebar_watchlist_date_controls = QWidget()
        date_controls_layout = QHBoxLayout()
        date_controls_layout.setContentsMargins(0, 0, 0, 0)
        date_controls_layout.setSpacing(5)

        self.sidebar_watchlist_date_edit = QDateEdit()
        self.sidebar_watchlist_date_edit.setObjectName(
            "sidebarWatchlistDateEdit"
        )
        self.sidebar_watchlist_date_edit.setCalendarPopup(True)
        self.sidebar_watchlist_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.sidebar_watchlist_date_edit.setToolTip(
            "Choose an NYSE session watchlist. Earlier sessions are fixed "
            "history."
        )
        self.sidebar_watchlist_date_edit.setStyleSheet(
            "QDateEdit { background: white; border: 1px solid #b8bec5; "
            "border-radius: 4px; padding: 4px 6px; }"
        )
        date_controls_layout.addWidget(self.sidebar_watchlist_date_edit, 1)

        self.sidebar_watchlist_current_button = QPushButton("Current")
        self.sidebar_watchlist_current_button.setObjectName(
            "sidebarWatchlistCurrentButton"
        )
        self.sidebar_watchlist_current_button.setMaximumWidth(62)
        self.sidebar_watchlist_current_button.setToolTip(
            "Return to the editable current-session watchlist."
        )
        self.sidebar_watchlist_current_button.clicked.connect(
            self._select_current_watchlist_session
        )
        date_controls_layout.addWidget(self.sidebar_watchlist_current_button)
        self.sidebar_watchlist_date_controls.setLayout(date_controls_layout)
        sidebar_layout.addWidget(self.sidebar_watchlist_date_controls)

        self.sidebar_watchlist_date_label = QLabel()
        self.sidebar_watchlist_date_label.setObjectName(
            "sidebarWatchlistDateLabel"
        )
        self.sidebar_watchlist_date_label.setStyleSheet(
            "color: #5f6872; font-size: 11px; padding: 0 2px 2px 2px;"
        )
        sidebar_layout.addWidget(self.sidebar_watchlist_date_label)

        self.sidebar_watchlist_calendar = (
            self.sidebar_watchlist_date_edit.calendarWidget()
        )
        self.sidebar_watchlist_calendar.setObjectName(
            "sidebarWatchlistCalendar"
        )
        self.sidebar_watchlist_calendar.setGridVisible(False)
        self.sidebar_watchlist_calendar.setVerticalHeaderFormat(
            QCalendarWidget.NoVerticalHeader
        )
        self.sidebar_watchlist_calendar.setHorizontalHeaderFormat(
            QCalendarWidget.ShortDayNames
        )
        self.sidebar_watchlist_calendar.setToolTip(
            "Each date is an NYSE session watchlist. The current session is "
            "editable; earlier sessions are fixed history."
        )
        self.sidebar_watchlist_calendar.setMinimumWidth(280)
        self.sidebar_watchlist_calendar.setStyleSheet(
            "QCalendarWidget QWidget#qt_calendar_navigationbar {"
            "background: #eceff1; border: none; }"
            "QCalendarWidget QToolButton { color: #37474f; "
            "background: transparent; border: none; font-weight: 600; "
            "padding: 5px; }"
            "QCalendarWidget QToolButton:hover { background: #dde2e5; "
            "border-radius: 3px; }"
            "QCalendarWidget QAbstractItemView { color: #263238; "
            "background: white; selection-background-color: #607d8b; "
            "selection-color: white; outline: none; }"
        )
        self._watchlist_calendar_follow_current = True
        self._watchlist_calendar_formatted_dates = set()
        self.sidebar_watchlist_date_edit.dateChanged.connect(
            self._on_sidebar_watchlist_date_changed
        )

        self.sidebar_stock_list = QListWidget()
        self.sidebar_stock_list.setMinimumWidth(145)
        self.sidebar_stock_list.setUniformItemSizes(True)
        self.sidebar_stock_list.itemSelectionChanged.connect(self.on_sidebar_selection_changed)
        self.sidebar_stock_list.itemDoubleClicked.connect(self.sidebar_show_chart)
        sidebar_layout.addWidget(self.sidebar_stock_list, 1)

        self.sidebar_selected_label = QLabel("Selected: None")
        self.sidebar_selected_label.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_selected_label)

        self.sidebar_add_watchlist_button = QPushButton("Add to Watchlist")
        self.sidebar_add_watchlist_button.setObjectName("sidebarAddWatchlistButton")
        self.sidebar_add_watchlist_button.clicked.connect(
            self.sidebar_add_selected_to_watchlist
        )
        sidebar_layout.addWidget(self.sidebar_add_watchlist_button)

        self.sidebar_move_buylist_button = QPushButton("Add to Buylist")
        self.sidebar_move_buylist_button.setObjectName("sidebarMoveBuylistButton")
        self.sidebar_move_buylist_button.clicked.connect(
            self.sidebar_move_selected_to_buylist
        )
        sidebar_layout.addWidget(self.sidebar_move_buylist_button)

        self.sidebar_remove_watchlist_button = QPushButton("Remove from Watchlist")
        self.sidebar_remove_watchlist_button.setObjectName(
            "sidebarRemoveWatchlistButton"
        )
        self.sidebar_remove_watchlist_button.clicked.connect(
            self.sidebar_remove_selected_from_watchlist
        )
        sidebar_layout.addWidget(self.sidebar_remove_watchlist_button)

        chart_button = QPushButton("Show Chart")
        chart_button.clicked.connect(self.sidebar_show_chart)
        sidebar_layout.addWidget(chart_button)

        sidebar_widget.setLayout(sidebar_layout)
        self.stock_sidebar.setWidget(sidebar_widget)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.stock_sidebar)
        self.refresh_sidebar_sources()

    @staticmethod
    def _qdate_from_date(value: dt.date) -> QDate:
        return QDate(value.year, value.month, value.day)

    @staticmethod
    def _date_from_qdate(value: QDate) -> dt.date:
        return dt.date(value.year(), value.month(), value.day())

    def _current_watchlist_session_date(self) -> dt.date:
        watchlist = self.__dict__.get("watchlist")
        resolved = getattr(watchlist, "active_session_date", None)
        if isinstance(resolved, dt.date):
            return resolved
        return dt.datetime.now(US_MARKET_ZONE).date()

    def _selected_watchlist_session_date(self) -> dt.date:
        date_edit = self.__dict__.get("sidebar_watchlist_date_edit")
        if date_edit is None:
            return self._current_watchlist_session_date()
        return self._date_from_qdate(date_edit.date())

    def _watchlist_view_is_current(self) -> bool:
        return (
            self._selected_watchlist_session_date()
            == self._current_watchlist_session_date()
        )

    def _watchlist_items_for_sidebar(self):
        watchlist = self.__dict__.get("watchlist")
        if watchlist is None:
            return ()
        session_date = self._selected_watchlist_session_date()
        loader = getattr(watchlist, "items_for_session", None)
        if callable(loader):
            return loader(session_date)
        return tuple(getattr(watchlist, "items", ()))

    def _refresh_watchlist_calendar(self) -> None:
        calendar = self.__dict__.get("sidebar_watchlist_calendar")
        date_edit = self.__dict__.get("sidebar_watchlist_date_edit")
        label = self.__dict__.get("sidebar_watchlist_date_label")
        if calendar is None or date_edit is None or label is None:
            return

        current_date = self._current_watchlist_session_date()
        current_qdate = self._qdate_from_date(current_date)
        previous_selected = date_edit.date()
        follow_current = bool(
            self.__dict__.get("_watchlist_calendar_follow_current", True)
        )
        date_edit.blockSignals(True)
        date_edit.setMaximumDate(current_qdate)
        if follow_current or previous_selected > current_qdate:
            date_edit.setDate(current_qdate)
        date_edit.blockSignals(False)

        empty_format = QTextCharFormat()
        for formatted_date in self.__dict__.get(
            "_watchlist_calendar_formatted_dates", set()
        ):
            calendar.setDateTextFormat(formatted_date, empty_format)

        saved_format = QTextCharFormat()
        saved_format.setFontWeight(600)
        saved_format.setForeground(QColor("#37474f"))
        formatted_dates = set()
        watchlist = self.__dict__.get("watchlist")
        for session_date in getattr(watchlist, "session_dates", ()):
            qdate = self._qdate_from_date(session_date)
            calendar.setDateTextFormat(qdate, saved_format)
            formatted_dates.add(qdate)
        self._watchlist_calendar_formatted_dates = formatted_dates

        selected_date = self._selected_watchlist_session_date()
        is_current = selected_date == current_date
        has_session = bool(
            watchlist is not None
            and getattr(watchlist, "has_session", lambda _date: False)(
                selected_date
            )
        )
        if is_current:
            status = "Current session  •  Editable"
        elif has_session:
            status = "Historical snapshot  •  Read-only"
        else:
            status = "No saved watchlist  •  Read-only"
        label.setText(status)
        current_button = self.__dict__.get(
            "sidebar_watchlist_current_button"
        )
        if current_button is not None:
            current_button.setEnabled(not is_current)

    def _sync_watchlist_calendar_visibility(self, source: dict) -> None:
        visible = source.get("type") == "watchlist"
        label = self.__dict__.get("sidebar_watchlist_date_label")
        controls = self.__dict__.get("sidebar_watchlist_date_controls")
        if label is not None:
            label.setVisible(visible)
        if controls is not None:
            controls.setVisible(visible)
        if visible:
            self._refresh_watchlist_calendar()

    def _select_current_watchlist_session(self) -> None:
        date_edit = self.__dict__.get("sidebar_watchlist_date_edit")
        if date_edit is None:
            return
        self._watchlist_calendar_follow_current = True
        current_qdate = self._qdate_from_date(
            self._current_watchlist_session_date()
        )
        if date_edit.date() == current_qdate:
            self._on_sidebar_watchlist_date_changed()
            return
        date_edit.setDate(current_qdate)

    def _on_sidebar_watchlist_date_changed(self) -> None:
        self._watchlist_calendar_follow_current = self._watchlist_view_is_current()
        self._sidebar_render_signature = None
        self._refresh_watchlist_calendar()
        self.refresh_stock_sidebar()

    def refresh_sidebar_sources(self, selected_source: Optional[dict] = None) -> None:
        """Refresh sidebar source options, keeping Universe as the default."""
        if not hasattr(self, "sidebar_source_combo"):
            return

        current_data = selected_source or self.sidebar_source_combo.currentData()
        self.sidebar_source_combo.blockSignals(True)
        self.sidebar_source_combo.clear()
        self.sidebar_source_combo.addItem("Universe", {"type": "universe"})
        for setup_name in sorted(self.scanner_setups.keys()):
            self.sidebar_source_combo.addItem(f"Scan: {setup_name}", {"type": "scan", "setup": setup_name})
        self.sidebar_source_combo.addItem("Watchlist", {"type": "watchlist"})
        self.sidebar_source_combo.addItem("Buylist", {"type": "buylist"})
        self.sidebar_source_combo.addItem("Buy Today", {"type": "buy_today"})

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
        if hasattr(self, "flush_stale_chart_views"):
            self.flush_stale_chart_views()
        if hasattr(self, "_flush_dirty_watchlist_and_dashboard"):
            self._flush_dirty_watchlist_and_dashboard()
        if not hasattr(self, "stock_sidebar"):
            return
        current_widget = self.tabs.currentWidget()
        self.stock_sidebar.setVisible(True)
        if current_widget is self.__dict__.get("health_widget"):
            refresh_health = getattr(self, "refresh_health_panel", None)
            if callable(refresh_health):
                refresh_health()
        if current_widget is self.__dict__.get("daily_summary_widget"):
            refresh_summary = getattr(self, "_refresh_daily_summary", None)
            if callable(refresh_summary):
                refresh_summary()
        if current_widget is self.__dict__.get("intraday_charts_widget"):
            self._set_sidebar_source_to_buylist()
        self.apply_sidebar_selection_to_current_tab()
    def _set_sidebar_source_to_buylist(self) -> None:
        if not hasattr(self, "sidebar_source_combo"):
            return
        for index in range(self.sidebar_source_combo.count()):
            data = self.sidebar_source_combo.itemData(index) or {}
            if data.get("type") == "buylist":
                if self.sidebar_source_combo.currentIndex() != index:
                    self.sidebar_source_combo.setCurrentIndex(index)
                return
    def _set_sidebar_source_to_universe(self) -> None:
        if not hasattr(self, "sidebar_source_combo"):
            return
        for index in range(self.sidebar_source_combo.count()):
            data = self.sidebar_source_combo.itemData(index) or {}
            if data.get("type") == "universe":
                if self.sidebar_source_combo.currentIndex() != index:
                    self.sidebar_source_combo.setCurrentIndex(index)
                return

    def _select_sidebar_universe_symbol(self, symbol: str, name: str = "") -> bool:
        """Select a symbol without inheriting a scan/watchlist restriction."""

        symbol = str(symbol or "").strip().upper()
        if not symbol or not hasattr(self, "sidebar_stock_list"):
            return False
        extras = self.__dict__.setdefault("_sidebar_universe_extra_symbols", set())
        extras.add(symbol)
        if name:
            self.__dict__.setdefault("_sidebar_universe_extra_names", {})[symbol] = str(
                name
            ).strip()
        self._set_sidebar_source_to_universe()
        source = self.sidebar_source_combo.currentData() or {}
        if source.get("type") != "universe":
            return False
        for row in range(self.sidebar_stock_list.count()):
            item = self.sidebar_stock_list.item(row)
            data = item.data(Qt.UserRole) or {}
            if str(data.get("symbol", "")).strip().upper() == symbol:
                self.sidebar_stock_list.setCurrentRow(row)
                return True
        # Setting the already-selected source doesn't emit currentIndexChanged.
        # Refresh only when the requested extra symbol isn't in the current list.
        self.refresh_stock_sidebar()
        for row in range(self.sidebar_stock_list.count()):
            item = self.sidebar_stock_list.item(row)
            data = item.data(Qt.UserRole) or {}
            if str(data.get("symbol", "")).strip().upper() == symbol:
                self.sidebar_stock_list.setCurrentRow(row)
                return True
        return False
    @staticmethod
    def _format_sidebar_added_date(added_date) -> str:
        """Format an item's added_date for the sidebar label, e.g. 2026/08/11."""
        if not added_date:
            return "?"
        try:
            return added_date.astimezone(KST_ZONE).strftime("%Y/%m/%d")
        except (TypeError, ValueError):
            return "?"

    def _sidebar_source_signature(self, source: dict) -> tuple:
        """Return every value that can affect the selected sidebar rows."""

        source_type = source.get("type")
        if source_type == "universe":
            symbols = {
                str(value or "").strip().upper()
                for value in self.__dict__.get("universe_tickers", ())
                if str(value or "").strip()
            }
            symbols.update(
                self.__dict__.get("_sidebar_universe_extra_symbols", set())
            )
            extra_names = self.__dict__.get("_sidebar_universe_extra_names", {})
            return (
                source_type,
                tuple(
                    (symbol, extra_names.get(symbol) or symbol)
                    for symbol in sorted(symbols)
                ),
            )
        if source_type == "scan":
            setup_name = source.get("setup", "")
            return (
                source_type,
                setup_name,
                tuple(
                    (
                        stock.get("symbol", ""),
                        stock.get("name", stock.get("symbol", "")),
                        stock.get("price"),
                    )
                    for stock in self.__dict__.get(
                        "scanner_results_by_setup", {}
                    ).get(setup_name, [])
                ),
            )
        if source_type == "buylist":
            return (
                source_type,
                tuple(
                    (
                        item.symbol,
                        self._format_sidebar_added_date(item.added_date),
                        item.name,
                        item.entry_price,
                        item.breakout_price,
                        item.stop_loss,
                        item.notes,
                        item.ai_summary,
                    )
                    for item in getattr(
                        self.__dict__.get("buylist_manager"), "items", ()
                    )
                ),
            )
        if source_type == "buy_today":
            projection_loader = getattr(
                self, "_buyboard_projection_values", None
            )
            values = (
                tuple(projection_loader() or ())
                if callable(projection_loader)
                else tuple(
                    self.__dict__.get("_buyboard_current_projections", ())
                    or ()
                )
            )
            rows = []
            for value in values:
                card = getattr(value, "card", value)
                if (
                    getattr(card, "board_status", None)
                    != BoardStatus.BUY_TODAY
                ):
                    continue
                rows.append(
                    (
                        str(getattr(card, "symbol", "") or "").strip().upper(),
                        str(getattr(card, "environment", "") or "")
                        .strip()
                        .upper(),
                        str(getattr(card, "account_no", "") or "").strip(),
                        getattr(card, "name", ""),
                        getattr(card, "market_data_last_trusted_price", None),
                        getattr(card, "entry_trigger", None),
                        getattr(card, "breakout_price", None),
                        getattr(card, "active_stop_price", None),
                        getattr(
                            getattr(card, "entry_runtime_status", None),
                            "value",
                            getattr(card, "entry_runtime_status", None),
                        ),
                    )
                )
            return source_type, tuple(rows)
        if source_type == "watchlist":
            return (
                source_type,
                self._selected_watchlist_session_date().isoformat(),
                tuple(
                    (
                        item.symbol,
                        self._format_sidebar_added_date(item.added_date),
                        item.name,
                        item.entry_price,
                        item.breakout_price,
                    )
                    for item in self._watchlist_items_for_sidebar()
                ),
            )
        return (source_type,)

    def refresh_stock_sidebar(self, *args) -> None:
        """Refresh the shared stock list from the selected planning source."""
        if not hasattr(self, "sidebar_stock_list"):
            return

        source = self.sidebar_source_combo.currentData() or {}
        self._sync_watchlist_calendar_visibility(source)
        signature = self._sidebar_source_signature(source)
        if signature == self.__dict__.get("_sidebar_render_signature"):
            # Watchlist membership can change while Universe rows remain the
            # same. Refresh action availability without rebuilding the list
            # or reapplying the symbol to an already-active chart.
            self._update_sidebar_watchlist_actions()
            return

        current_symbol = self._get_sidebar_selected_symbol()
        current_row = self.sidebar_stock_list.currentRow()
        self.sidebar_stock_list.clear()

        if source.get("type") == "universe":
            symbols = {
                str(value or "").strip().upper()
                for value in self.__dict__.get("universe_tickers", ())
                if str(value or "").strip()
            }
            symbols.update(
                self.__dict__.get("_sidebar_universe_extra_symbols", set())
            )
            extra_names = self.__dict__.get("_sidebar_universe_extra_names", {})
            for symbol in sorted(symbols):
                item = QListWidgetItem(symbol)
                item.setData(
                    Qt.UserRole,
                    {
                        "symbol": symbol,
                        "name": extra_names.get(symbol) or symbol,
                        "price": None,
                        "source": "universe",
                    },
                )
                self.sidebar_stock_list.addItem(item)
        elif source.get("type") == "scan":
            setup_name = source.get("setup", "")
            for stock in self.scanner_results_by_setup.get(setup_name, []):
                symbol = stock.get("symbol", "")
                price = stock.get("price")
                label = f"{symbol}"
                if price is not None:
                    label += f"  {float(price):.2f}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "symbol": symbol,
                    "name": stock.get("name", symbol),
                    "price": price,
                    "source": "scanner",
                    "setup": setup_name,
                })
                self.sidebar_stock_list.addItem(item)
        elif source.get("type") == "buylist":
            for buy_item in self.buylist_manager.items:
                label = f"{buy_item.symbol} ({self._format_sidebar_added_date(buy_item.added_date)})"
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
        elif source.get("type") == "buy_today":
            projection_loader = getattr(self, "_buyboard_projection_values", None)
            projection_values = (
                tuple(projection_loader() or ())
                if callable(projection_loader)
                else tuple(
                    self.__dict__.get("_buyboard_current_projections", ()) or ()
                )
            )
            for value in projection_values:
                card = getattr(value, "card", value)
                if getattr(card, "board_status", None) != BoardStatus.BUY_TODAY:
                    continue
                symbol = str(getattr(card, "symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                environment = str(
                    getattr(card, "environment", "") or ""
                ).strip().upper()
                account_no = str(getattr(card, "account_no", "") or "").strip()
                account_label = "/".join(
                    part for part in (environment, account_no) if part
                )
                label = symbol + (f" ({account_label})" if account_label else "")
                price = (
                    getattr(card, "market_data_last_trusted_price", None)
                    or getattr(card, "entry_trigger", None)
                    or getattr(card, "breakout_price", None)
                )
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "symbol": symbol,
                    "name": getattr(card, "name", "") or symbol,
                    "price": price,
                    "source": "buy_today",
                    "environment": environment,
                    "account_no": account_no,
                    "breakout_price": getattr(card, "breakout_price", None),
                    "stop_loss": getattr(card, "active_stop_price", None),
                    "entry_runtime_status": getattr(
                        getattr(card, "entry_runtime_status", None),
                        "value",
                        getattr(card, "entry_runtime_status", None),
                    ),
                })
                self.sidebar_stock_list.addItem(item)
        elif source.get("type") == "watchlist":
            session_date = self._selected_watchlist_session_date()
            is_current = self._watchlist_view_is_current()
            for watch_item in self._watchlist_items_for_sidebar():
                label = watch_item.symbol
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "symbol": watch_item.symbol,
                    "name": watch_item.name,
                    "price": watch_item.entry_price,
                    "source": "watchlist",
                    "breakout_price": watch_item.breakout_price,
                    "watchlist_session_date": session_date.isoformat(),
                    "watchlist_is_current": is_current,
                })
                self.sidebar_stock_list.addItem(item)
        if current_symbol:
            for row in range(self.sidebar_stock_list.count()):
                item = self.sidebar_stock_list.item(row)
                data = item.data(Qt.UserRole) or {}
                if data.get("symbol") == current_symbol:
                    self.sidebar_stock_list.setCurrentRow(row)
                    break
        if (
            self.sidebar_stock_list.currentRow() < 0
            and self.sidebar_stock_list.count() > 0
            and source.get("type") != "universe"
        ):
            # Symbol no longer in list (e.g. removed) — stay at same position rather than jumping to top
            restore = min(current_row, self.sidebar_stock_list.count() - 1) if current_row >= 0 else 0
            self.sidebar_stock_list.setCurrentRow(restore)
        self._sidebar_render_signature = signature
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
            self._update_sidebar_watchlist_actions()
            return

        symbol = data.get("symbol", "")
        self.selected_scan_symbol = symbol
        self._set_tradingview_symbol(symbol)
        self.sidebar_selected_label.setText(f"Selected: {symbol}")
        self._update_sidebar_watchlist_actions()
        self.apply_sidebar_selection_to_current_tab()

    def _update_sidebar_watchlist_actions(self) -> None:
        data = self._get_sidebar_selected_data()
        symbol = str((data or {}).get("symbol") or "").strip().upper()
        source = str((data or {}).get("source") or "").strip().lower()
        watchlist = self.__dict__.get("watchlist")
        in_watchlist = bool(
            symbol
            and watchlist is not None
            and getattr(watchlist, "get", lambda _symbol: None)(symbol) is not None
        )
        pending = bool(self.__dict__.get("_planning_membership_pending", False))
        visual_operation = getattr(
            self, "_planning_membership_visual_operation", lambda _symbol: ""
        )(symbol)

        add_button = self.__dict__.get("sidebar_add_watchlist_button")
        if add_button is not None:
            historical_watchlist = source == "watchlist" and not bool(
                (data or {}).get("watchlist_is_current", False)
            )
            if visual_operation == "add":
                add_text = "Added to Watchlist (Saving…)"
            elif visual_operation == "remove":
                add_text = "Removed from Watchlist (Saving…)"
            elif in_watchlist:
                add_text = "In Today's Watchlist"
            elif historical_watchlist:
                add_text = "Add to Today's Watchlist"
            else:
                add_text = "Add to Watchlist"
            add_button.setText(add_text)
            # Demotion is a versioned Buy Board command; never create dual
            # membership by locally adding an existing Buylist row.
            add_button.setEnabled(
                bool(symbol)
                and source not in {"buylist", "buy_today"}
                and not in_watchlist
                and not pending
            )
        move_button = self.__dict__.get("sidebar_move_buylist_button")
        if move_button is not None:
            move_button.setText(
                "Added to Buylist (Saving…)"
                if visual_operation == "promote"
                else "Add to Buylist"
            )
            move_button.setEnabled(
                bool(symbol)
                and source == "watchlist"
                and bool((data or {}).get("watchlist_is_current", False))
                and not pending
            )
        remove_button = self.__dict__.get("sidebar_remove_watchlist_button")
        if remove_button is not None:
            remove_button.setEnabled(
                bool(symbol)
                and source == "watchlist"
                and bool((data or {}).get("watchlist_is_current", False))
                and not pending
            )
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

        if hasattr(self, "trade_plan_widget") and current_widget is self.trade_plan_widget:
            self._seed_trade_plan_fields(symbol=symbol, price=price, name=name, overwrite=True)
        elif current_widget is self.intraday_charts_widget:
            self._set_intraday_symbol(symbol)
            self.plot_intraday_watchlist_symbol()
        elif current_widget is self.tradingview_widget:
            self._set_tradingview_symbol(symbol)
            if (
                hasattr(self, "tradingview_symbol_combo")
                and not self.__dict__.get("_suppress_sidebar_tradingview_load", False)
            ):
                schedule_load = getattr(
                    self, "_schedule_tradingview_navigation_load", None
                )
                if callable(schedule_load):
                    schedule_load()
                else:
                    self.load_tradingview_chart(force=True)
        elif current_widget is self.scanner_widget:
            self.scanner_selection_label.setText(f"Selected symbol: {symbol}")
            self.update_scanner_preview_chart(symbol)
    def sidebar_show_chart(self, *args) -> None:
        """Show the selected sidebar stock on the TradingView chart tab."""
        data = self._get_sidebar_selected_data()
        if not data:
            QMessageBox.warning(self, "No selection", "Select a stock from the sidebar first.")
            return

        symbol = data.get("symbol", "")
        if hasattr(self, "tradingview_symbol_combo"):
            self._set_tradingview_symbol(symbol)
        self.tabs.setCurrentWidget(self.tradingview_widget)
        self.load_tradingview_chart(force=True)

    def sidebar_add_selected_to_watchlist(self) -> None:
        """Add the selected scan/sidebar symbol to the passive Watchlist."""

        data = self._get_sidebar_selected_data()
        if not data:
            QMessageBox.warning(self, "No selection", "Select a stock first.")
            return
        if data.get("source") in {"buylist", "buy_today"}:
            QMessageBox.information(
                self,
                "Use Buy Board",
                "Use the Buy Board card actions to change an active planning-stage stock safely.",
            )
            return
        add_candidate = getattr(self, "_add_watchlist_candidate", None)
        if callable(add_candidate):
            add_candidate(
                data.get("symbol", ""),
                name=data.get("name") or data.get("symbol", ""),
                entry_price=data.get("price"),
                source="Sidebar",
            )

    def sidebar_move_selected_to_buylist(self) -> None:
        """Promote the selected passive Watchlist item to Buylist."""

        data = self._get_sidebar_selected_data()
        if not data or data.get("source") != "watchlist":
            QMessageBox.warning(
                self, "Watchlist selection required", "Select a Watchlist stock first."
            )
            return
        if not data.get("watchlist_is_current", False):
            QMessageBox.information(
                self,
                "Historical Watchlist",
                "Past watchlists are fixed. Add the symbol to today's "
                "watchlist before moving it to Buylist.",
            )
            return
        promote = getattr(self, "_promote_watchlist_candidate", None)
        if callable(promote):
            promote(str(data.get("symbol") or ""))

    def sidebar_remove_selected_from_watchlist(self) -> None:
        """Remove the selected passive candidate without touching active plans."""

        data = self._get_sidebar_selected_data()
        if not data or data.get("source") != "watchlist":
            QMessageBox.warning(
                self, "Watchlist selection required", "Select a Watchlist stock first."
            )
            return
        if not data.get("watchlist_is_current", False):
            QMessageBox.information(
                self,
                "Historical Watchlist",
                "Past watchlists are fixed and cannot be removed or edited.",
            )
            return
        remove_candidate = getattr(self, "_remove_watchlist_candidate", None)
        if callable(remove_candidate):
            remove_candidate(str(data.get("symbol") or ""))
