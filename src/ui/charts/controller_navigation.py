"""Chart symbol, queue, and activation navigation."""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QMessageBox


class ChartsNavigationMixin:
    def _refresh_active_chart_for_symbol(self, symbol: str) -> None:
        """Force-refresh the current chart view if it matches symbol."""
        symbol = symbol.strip().upper()
        tabs = self.__dict__.get("tabs")
        active_widget = tabs.currentWidget() if tabs is not None else None
        if active_widget is self.__dict__.get("tradingview_widget"):
            active = (
                self.tradingview_symbol_combo.currentText().strip().upper()
                if hasattr(self, "tradingview_symbol_combo")
                else ""
            )
            if active == symbol:
                QTimer.singleShot(50, lambda: self.load_tradingview_chart(force=True))
        elif active_widget is self.__dict__.get("intraday_charts_widget"):
            intraday_symbol = (
                self.intraday_symbol_combo.currentText().strip().upper()
                if hasattr(self, "intraday_symbol_combo")
                else ""
            )
            if intraday_symbol == symbol:
                QTimer.singleShot(
                    50,
                    lambda: self.plot_intraday_watchlist_symbol(allow_fetch=False),
                )

    def _active_chart_timeframe(self) -> str:
        """Return the timeframe currently selected on the active chart tab."""
        tabs = self.__dict__.get("tabs")
        active_widget = tabs.currentWidget() if tabs is not None else None
        if active_widget is self.__dict__.get("tradingview_widget"):
            return (
                self.tradingview_timeframe_combo.currentText().strip().upper()
                if hasattr(self, "tradingview_timeframe_combo")
                else "1D"
            )
        if active_widget is self.__dict__.get("intraday_charts_widget"):
            return (
                self.intraday_interval_combo.currentText().strip().upper()
                if hasattr(self, "intraday_interval_combo")
                else "1H"
            )
        return "1D"

    def _get_chart_symbol_universe(self) -> set:
        symbols = set(self.universe_tickers)
        symbols.update(
            self.__dict__.get("_sidebar_universe_extra_symbols", set())
        )
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
        self._suppress_sidebar_tradingview_load = True
        try:
            if hasattr(self, "sidebar_stock_list"):
                for row in range(self.sidebar_stock_list.count()):
                    item = self.sidebar_stock_list.item(row)
                    data = item.data(Qt.UserRole) or {}
                    if str(data.get("symbol", "")).strip().upper() == next_symbol:
                        self.sidebar_stock_list.setCurrentRow(row)
                        break
        finally:
            self._suppress_sidebar_tradingview_load = False
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
            from src.core.trade_card_state import BoardStatus
            from src.ui.buyboard.board import _command_kwargs, _lookup_projection
            from src.ui.buyboard.card import card_drag_payload
            from src.ui.buyboard.drag_commands import CancelEntry, MoveToWatchlist

            account_no = self._selected_order_account_for_item(item, env)
            projection = _lookup_projection(self, env, account_no, symbol)
            if projection is None:
                self.refresh_buyboard()
                QMessageBox.information(
                    self,
                    "Buy Board is refreshing",
                    f"{symbol}'s Buy Board card is still loading. Try again in a moment.",
                )
                return
            card = projection.card
            if max(0, int(card.broker_quantity or 0)) > 0:
                QMessageBox.warning(
                    self,
                    "Active position",
                    f"{symbol} has an active position and cannot be removed here.",
                )
                return
            payload = card_drag_payload(projection)
            common = _command_kwargs(payload)
            if card.board_status == BoardStatus.BUYLIST:
                command = MoveToWatchlist(**common)
            elif card.board_status in {
                BoardStatus.BUY_TODAY,
                BoardStatus.ENTRY_PENDING,
            }:
                command = CancelEntry(**common)
            else:
                QMessageBox.information(
                    self,
                    "Buy Board",
                    f"{symbol} cannot be removed from its current board state.",
                )
                return
            self._buyboard_dispatch_command(
                command,
                interaction_fingerprint=payload["state_fingerprint"],
            )
            self.append_log(f"[Chart] Requested Buy Board removal for {symbol}.")
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
        from src.core.trade_card_state import BoardStatus

        card = self._chart_buyboard_card(symbol, env)
        return card is not None and card.board_status in {
            BoardStatus.BUY_TODAY,
            BoardStatus.ENTRY_PENDING,
        }

    def _chart_buyboard_card(self, symbol: str, env: str):
        symbol = str(symbol or "").strip().upper()
        env = str(env or "PROD").strip().upper()
        if not symbol:
            return None
        buylist_manager = getattr(self, "buylist_manager", None)
        item = buylist_manager.get(symbol, env) if buylist_manager is not None else None
        account_no = str(getattr(item, "kis_account_no", "") or "")
        for value in tuple(
            getattr(self, "_buyboard_current_projections", ()) or ()
        ):
            card = getattr(value, "card", value)
            if (
                str(getattr(card, "environment", "")).upper() == env
                and str(getattr(card, "symbol", "")).upper() == symbol
                and (
                    not account_no
                    or str(getattr(card, "account_no", "")) == account_no
                )
            ):
                return card
        return None

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
            QMessageBox.information(
                self,
                "Not in queue",
                f"{symbol} is not queued. Use 'Queue for Buy' first.",
            )
            return

        from src.core.trade_card_state import BoardStatus
        from src.ui.buyboard.board import _command_kwargs, _lookup_projection
        from src.ui.buyboard.card import card_drag_payload
        from src.ui.buyboard.drag_commands import ActivateForToday, CancelEntry

        account_no = self._selected_order_account_for_item(item, env)
        if not account_no:
            self._warn_order_account_unavailable(item, env)
            return
        projection = _lookup_projection(self, env, account_no, symbol)
        if projection is None:
            self.refresh_buyboard()
            QMessageBox.information(
                self,
                "Buy Board is refreshing",
                f"{symbol} is queued, but its Buy Board card is still loading. Try again in a moment.",
            )
            return

        payload = card_drag_payload(projection)
        common = _command_kwargs(payload)
        card = projection.card
        if card.board_status == BoardStatus.BUYLIST:
            command = ActivateForToday(**common)
            message = f"[Chart] Requested Buy Today activation for {symbol}."
        elif card.board_status in {BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING}:
            command = CancelEntry(**common)
            message = f"[Chart] Requested entry cancellation/removal for {symbol}."
        elif max(0, int(card.broker_quantity or 0)) > 0:
            QMessageBox.information(
                self,
                "Position already open",
                f"{symbol} is already managed as an open position on Buy Board.",
            )
            return
        else:
            QMessageBox.information(
                self,
                "Buy Board",
                f"{symbol} cannot be activated from its current board state.",
            )
            return

        self._buyboard_dispatch_command(
            command,
            interaction_fingerprint=payload["state_fingerprint"],
        )
        self.append_log(message)
        if card.board_status == BoardStatus.BUYLIST:
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
        card = self._chart_buyboard_card(symbol, env)
        is_active = self._is_symbol_monitor_active(symbol, env)
        btn.setEnabled(in_queue)
        if card is not None and max(0, int(card.broker_quantity or 0)) > 0:
            btn.setText("Position Open")
            btn.setEnabled(False)
            btn.setStyleSheet("")
            return
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
