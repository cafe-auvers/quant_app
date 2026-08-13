"""Chart symbol, queue, and activation navigation."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QComboBox, QMessageBox

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None


REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class ChartsNavigationMixin:
    def _refresh_active_chart_for_symbol(self, symbol: str) -> None:
        """Force-refresh the current chart view if it matches symbol."""
        symbol = symbol.strip().upper()
        if (
            hasattr(self, "tabs")
            and hasattr(self, "tradingview_widget")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            active = (
                self.tradingview_symbol_combo.currentText().strip().upper()
                if hasattr(self, "tradingview_symbol_combo")
                else ""
            )
            if active == symbol:
                QTimer.singleShot(50, lambda: self.load_tradingview_chart(force=True))
        else:
            chart_sym = (
                self._get_chart_symbol() if hasattr(self, "chart_symbol_input") else ""
            )
            if chart_sym and chart_sym.strip().upper() == symbol:
                QTimer.singleShot(
                    50, lambda: self.plot_selected_symbol(show_warnings=False)
                )

    def _active_chart_timeframe(self) -> str:
        """Return the timeframe currently selected on the active chart tab."""
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.__dict__.get(
            "tradingview_widget"
        ):
            return (
                self.tradingview_timeframe_combo.currentText().strip().upper()
                if hasattr(self, "tradingview_timeframe_combo")
                else "1D"
            )
        return (
            self.chart_timeframe_combo.currentText().strip().upper()
            if hasattr(self, "chart_timeframe_combo")
            else "1D"
        )

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
        if not hasattr(self, "chart_symbol_input") or not isinstance(
            self.chart_symbol_input, QComboBox
        ):
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
        filtered = self._filter_symbols_by_prefix(
            self._get_chart_symbol_universe(), prefix
        )

        self.chart_symbol_input.blockSignals(True)
        self.chart_symbol_input.clear()
        self.chart_symbol_input.addItems(filtered)
        self.chart_symbol_input.setEditText(prefix)
        self.chart_symbol_input.blockSignals(False)
        self.chart_symbol_input.showPopup()

    def _get_chart_symbol_universe(self) -> set:
        symbols = set(self.universe_tickers)
        symbols.update(item.symbol for item in self.watchlist.items)
        symbols.update(
            stock["symbol"] for stock in self.scanner_results if stock.get("symbol")
        )
        symbols.update(plan.symbol for plan in self.trade_manager.get_active_plans())
        return symbols

    def populate_intraday_watchlist_symbols(self) -> None:
        if not hasattr(self, "intraday_symbol_combo"):
            return
        current_text = self.intraday_symbol_combo.currentText().strip().upper()
        self.intraday_symbol_combo.blockSignals(True)
        self.intraday_symbol_combo.clear()
        self.intraday_symbol_combo.addItems(
            [item.symbol for item in self.watchlist.items]
        )
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
        filtered = self._filter_symbols_by_prefix(
            self._get_chart_symbol_universe(), prefix
        )

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

    def refresh_intraday_chart_if_symbol(
        self, symbol: str, allow_fetch: bool = False
    ) -> None:
        intraday_symbol_combo = self.__dict__.get("intraday_symbol_combo")
        if intraday_symbol_combo is None:
            return
        if (
            intraday_symbol_combo.currentText().strip().upper()
            == symbol.strip().upper()
        ):
            self.plot_intraday_watchlist_symbol(allow_fetch=allow_fetch)

    def refresh_chart_views_for_symbol(
        self, symbol: str, allow_fetch: bool = False
    ) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        chart_symbol = (
            self._get_chart_symbol()
            if self.__dict__.get("chart_symbol_input") is not None
            else ""
        )
        if chart_symbol and chart_symbol.strip().upper() == symbol:
            self.plot_selected_symbol(show_warnings=False)
        self.refresh_intraday_chart_if_symbol(symbol, allow_fetch=allow_fetch)

    def refresh_other_chart_views_for_symbol(self, symbol: str) -> None:
        """Keep other (currently hidden) chart tabs in sync with an edit made
        on the active tab (breakout price set, drawing added/changed).

        These tabs aren't visible right now, so rebuilding their chart HTML
        and calling setHtml() immediately is pure wasted work in the middle
        of an interaction the user IS looking at. Instead, mark them stale;
        flush_stale_chart_views() (called from on_tab_changed) does the
        actual refresh the moment the user switches into that tab -- same
        end result, no work paid for tabs nobody is looking at.
        """
        symbol = symbol.strip().upper()
        if not symbol:
            return
        active_widget = (
            self.__dict__.get("tabs").currentWidget()
            if self.__dict__.get("tabs") is not None
            else None
        )
        chart_symbol = (
            self._get_chart_symbol()
            if self.__dict__.get("chart_symbol_input") is not None
            else ""
        )
        if (
            active_widget is not self.__dict__.get("charts_widget")
            and chart_symbol
            and chart_symbol.strip().upper() == symbol
        ):
            self._charts_tab_chart_stale = True
        intraday_symbol_combo = self.__dict__.get("intraday_symbol_combo")
        if (
            active_widget is not self.__dict__.get("intraday_charts_widget")
            and intraday_symbol_combo is not None
            and intraday_symbol_combo.currentText().strip().upper() == symbol
        ):
            self._intraday_tab_chart_stale = True
        tradingview_symbol_combo = self.__dict__.get("tradingview_symbol_combo")
        if (
            active_widget is not self.__dict__.get("tradingview_widget")
            and tradingview_symbol_combo is not None
            and tradingview_symbol_combo.currentText().strip().upper() == symbol
        ):
            self._tradingview_tab_chart_stale = True

    def flush_stale_chart_views(self) -> None:
        """Refresh whichever chart tab just became active, if a prior edit on
        another tab marked it stale. Called from on_tab_changed."""
        active_widget = (
            self.__dict__.get("tabs").currentWidget()
            if self.__dict__.get("tabs") is not None
            else None
        )
        if active_widget is None:
            return
        if (
            self.__dict__.get("_charts_tab_chart_stale")
            and active_widget is self.__dict__.get("charts_widget")
        ):
            self._charts_tab_chart_stale = False
            self.plot_selected_symbol(show_warnings=False)
        if (
            self.__dict__.get("_intraday_tab_chart_stale")
            and active_widget is self.__dict__.get("intraday_charts_widget")
        ):
            self._intraday_tab_chart_stale = False
            self.plot_intraday_watchlist_symbol(allow_fetch=False)
        if (
            self.__dict__.get("_tradingview_tab_chart_stale")
            and active_widget is self.__dict__.get("tradingview_widget")
        ):
            self._tradingview_tab_chart_stale = False
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
        symbol = (
            self.tradingview_symbol_combo.currentText().strip().upper()
            if hasattr(self, "tradingview_symbol_combo")
            else ""
        )
        if not symbol:
            QMessageBox.information(
                self, "No symbol", "Load a symbol before adding it to the watchlist."
            )
            return
        existing = self.watchlist.get(symbol)
        if existing is not None:
            self.watchlist.remove(symbol)
            self.mark_watchlist_and_dashboard_dirty()
            self._save_state()
            self.append_log(f"Removed {symbol} from watchlist from TradingView.")
            self._update_tradingview_watchlist_btn()
            return
        name = symbol
        selected = self._get_sidebar_selected_data()
        if selected and str(selected.get("symbol", "")).strip().upper() == symbol:
            name = selected.get("name", symbol) or symbol
        self.watchlist.add(symbol=symbol, name=name)
        self.mark_watchlist_and_dashboard_dirty()
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
        in_watchlist = (
            symbol and watchlist is not None and watchlist.get(symbol) is not None
        )
        if in_watchlist:
            btn.setText("Remove from Watchlist (W)")
            btn.setStyleSheet(
                "background-color: #c0392b; color: white; font-weight: 600;"
            )
        else:
            btn.setText("Add to Watchlist (W)")
            btn.setStyleSheet(
                "background-color: #27ae60; color: white; font-weight: 600;"
            )

    def _tradingview_queue_toggle(self) -> None:
        symbol = (
            self.tradingview_symbol_combo.currentText().strip().upper()
            if hasattr(self, "tradingview_symbol_combo")
            else ""
        )
        if not symbol:
            return
        self._chart_queue_toggle(symbol)
        self._update_tradingview_queue_btn()

    def _update_tradingview_queue_btn(self, _text: str = "") -> None:
        btn = getattr(self, "tradingview_queue_btn", None)
        if btn is None:
            return
        symbol = (
            self.tradingview_symbol_combo.currentText().strip().upper()
            if hasattr(self, "tradingview_symbol_combo")
            else ""
        )
        self._apply_chart_queue_btn_state(symbol, btn)

    def _chart_queue_toggle(self, symbol: str) -> None:
        if not symbol:
            return
        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        buylist_manager = getattr(self, "buylist_manager", None)
        item = buylist_manager.get(symbol, env) if buylist_manager is not None else None
        in_queue = item is not None and self._is_execution_queue_buylist_item(item)
        if in_queue:
            if item.monitoring_status in ("BOUGHT", "BUY_SUBMITTED", "BUY_PARTIAL"):
                from PyQt5.QtWidgets import QMessageBox

                QMessageBox.warning(
                    self,
                    "Active position",
                    f"{symbol} has an active position and cannot be removed here.",
                )
                return
            buylist_manager.remove(symbol, env)
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(f"[Chart] {symbol} removed from execution queue.")
        else:
            watch_item = (
                self.watchlist.get(symbol) if hasattr(self, "watchlist") else None
            )
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
        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        buylist_manager = getattr(self, "buylist_manager", None)
        item = buylist_manager.get(symbol, env) if buylist_manager is not None else None
        in_queue = item is not None and self._is_execution_queue_buylist_item(item)
        if in_queue:
            btn.setText("Remove from Queue")
            btn.setStyleSheet(
                "background-color: #c0392b; color: white; font-weight: 600;"
            )
        else:
            btn.setText("Queue for Buy (Q)")
            btn.setStyleSheet(
                "background-color: #27ae60; color: white; font-weight: 600;"
            )

    def _is_symbol_monitor_active(self, symbol: str, env: str) -> bool:
        buylist_manager = getattr(self, "buylist_manager", None)
        if buylist_manager is None or not symbol:
            return False
        item = buylist_manager.get(symbol, env)
        if item is None:
            return False
        if self._is_execution_queue_buylist_item(item):
            return bool(getattr(item, "orb_monitor_enabled", False))
        return str(getattr(item, "monitoring_status", "")).upper() in (
            "ACTIVE",
            "BOUGHT",
        )

    def _chart_activate_toggle(self, symbol: str, start_monitor: bool = False) -> None:
        if not symbol:
            return
        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        buylist_manager = getattr(self, "buylist_manager", None)
        if buylist_manager is None:
            return
        item = buylist_manager.get(symbol, env)
        if item is None:
            from PyQt5.QtWidgets import QMessageBox

            QMessageBox.information(
                self,
                "Not in queue",
                f"{symbol} is not queued. Use 'Queue for Buy' first.",
            )
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

                QMessageBox.information(
                    self, "Already bought", f"{symbol} is already in a BOUGHT position."
                )
                return
            if self._is_execution_queue_buylist_item(item):
                item.orb_monitor_enabled = True
                self._ensure_buylist_monitor_running(env)
            else:
                bought_count = sum(
                    1
                    for it in buylist_manager.items
                    if str(getattr(it, "monitoring_status", "")).upper() == "BOUGHT"
                    and it.environment == env
                )
                if bought_count >= 30:
                    from PyQt5.QtWidgets import QMessageBox

                    QMessageBox.warning(
                        self, "Max positions", "Already holding 30 positions."
                    )
                    return
                item.monitoring_status = "ACTIVE"
                self._clear_buylist_auto_order_block(item)
                if start_monitor:
                    self._ensure_buylist_monitor_running(env)
            self._save_state()
            self.populate_buylist_dashboard()
            self.append_log(f"[Chart] {symbol} monitoring activated.")
            # Pre-load intraday data so the Intraday tab is ready
            self._set_intraday_symbol(symbol)
            self.prefetch_intraday_cache_for_symbol(symbol)

    def _apply_chart_activate_btn_state(self, symbol: str, btn) -> None:
        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        buylist_manager = getattr(self, "buylist_manager", None)
        item = buylist_manager.get(symbol, env) if buylist_manager and symbol else None
        in_queue = item is not None
        is_active = self._is_symbol_monitor_active(symbol, env)
        btn.setEnabled(in_queue)
        if is_active:
            btn.setText("Deactivate (A)")
            btn.setStyleSheet(
                "background-color: #c0392b; color: white; font-weight: 600;"
            )
        elif in_queue:
            btn.setText("Activate (A)")
            btn.setStyleSheet(
                "background-color: #27ae60; color: white; font-weight: 600;"
            )
        else:
            btn.setText("Activate (A)")
            btn.setStyleSheet("")

    def _update_tradingview_activate_btn(self, _text: str = "") -> None:
        btn = getattr(self, "tradingview_activate_btn", None)
        if btn is None:
            return
        symbol = (
            self.tradingview_symbol_combo.currentText().strip().upper()
            if hasattr(self, "tradingview_symbol_combo")
            else ""
        )
        self._apply_chart_activate_btn_state(symbol, btn)

    def _tradingview_activate_toggle(self) -> None:
        symbol = (
            self.tradingview_symbol_combo.currentText().strip().upper()
            if hasattr(self, "tradingview_symbol_combo")
            else ""
        )
        if not symbol:
            return
        self._chart_activate_toggle(symbol, start_monitor=True)
        self._update_tradingview_activate_btn()

    def _update_intraday_activate_btn(self, _text: str = "") -> None:
        btn = getattr(self, "intraday_activate_btn", None)
        if btn is None:
            return
        symbol = (
            self.intraday_symbol_combo.currentText().strip().upper()
            if hasattr(self, "intraday_symbol_combo")
            else ""
        )
        self._apply_chart_activate_btn_state(symbol, btn)

    def _intraday_activate_toggle(self) -> None:
        symbol = (
            self.intraday_symbol_combo.currentText().strip().upper()
            if hasattr(self, "intraday_symbol_combo")
            else ""
        )
        if not symbol:
            return
        self._chart_activate_toggle(symbol)
        self._update_intraday_activate_btn()
