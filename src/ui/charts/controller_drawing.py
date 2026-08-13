"""Interactive chart drawing and target commands."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, List
from zoneinfo import ZoneInfo

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QMessageBox

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


class ChartsDrawingMixin:
    def _active_chart_view(self):
        if (
            hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            return getattr(self, "tradingview_chart_view", None)
        if (
            hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.intraday_charts_widget
        ):
            return getattr(self, "intraday_chart_view", None)
        return getattr(self, "chart_view", None)

    def _active_chart_command_views(self) -> List[Any]:
        active_view = self._active_chart_view()
        if not (
            hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            return [active_view] if active_view is not None else []
        views = [getattr(self, "tradingview_chart_view", None)]
        split_view = getattr(self, "tradingview_split_chart_view", None)
        if split_view is not None and split_view.isVisible():
            views.append(split_view)
        return [view for view in views if view is not None]

    def _active_chart_symbol(self) -> str:
        if (
            hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            return (
                self.tradingview_symbol_combo.currentText().strip().upper()
                if hasattr(self, "tradingview_symbol_combo")
                else ""
            )
        if (
            hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.intraday_charts_widget
        ):
            return (
                self.intraday_symbol_combo.currentText().strip().upper()
                if hasattr(self, "intraday_symbol_combo")
                else ""
            )
        return self._get_chart_symbol() or (self.selected_scan_symbol or "")

    def _active_chart_buttons(self) -> dict:
        if (
            hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.tradingview_widget
        ):
            return {
                "target": getattr(self, "tradingview_set_target_button", None),
                "draw": getattr(self, "tradingview_line_tool_button", None),
                "erase": getattr(self, "tradingview_erase_line_button", None),
            }
        if (
            hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.intraday_charts_widget
        ):
            return {
                "target": getattr(self, "intraday_set_target_button", None),
                "draw": getattr(self, "intraday_draw_line_button", None),
                "erase": getattr(self, "intraday_erase_line_button", None),
            }
        return {
            "target": getattr(self, "chart_set_target_button", None),
            "draw": getattr(self, "chart_draw_line_button", None),
            "erase": getattr(self, "chart_erase_line_button", None),
        }

    @staticmethod
    def _set_button_state(button, text: str, active: bool = False) -> None:
        if button is None:
            return
        button.setText(text)
        button.setStyleSheet("font-weight: 600;" if active else "")

    def _reset_chart_mode_buttons(self) -> None:
        settings = self.__dict__.get("settings") or {}
        shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
        t_key = shortcuts.get("set_target", "T")
        d_key = shortcuts.get("draw_line", "D")
        e_key = shortcuts.get("erase_drawing", "E")
        for prefix in ["chart", "intraday"]:
            self._set_button_state(
                self.__dict__.get(f"{prefix}_set_target_button"),
                f"Set Breakout Price ({t_key})",
            )
            self._set_button_state(
                self.__dict__.get(f"{prefix}_draw_line_button"), f"Draw Line ({d_key})"
            )
            self._set_button_state(
                self.__dict__.get(f"{prefix}_erase_line_button"),
                f"Erase Drawing ({e_key})",
            )
        self._set_button_state(
            self.__dict__.get("tradingview_set_target_button"),
            f"Set Breakout Price ({t_key})",
        )
        self._set_button_state(
            self.__dict__.get("tradingview_line_tool_button"), f"Line Tool ({d_key})"
        )
        self.tradingview_line_tool_active = False

    def enable_chart_target_mode(self) -> None:
        if not self._active_chart_symbol():
            QMessageBox.information(
                self,
                "No chart symbol",
                "Plot a symbol before setting a breakout price.",
            )
            return
        active_views = self._active_chart_command_views()
        web_views = [
            view
            for view in active_views
            if QWebEngineView is not None and isinstance(view, QWebEngineView)
        ]
        if web_views:
            web_views[0].setFocus()
            buttons = self._active_chart_buttons()
            settings = self.__dict__.get("settings") or {}
            shortcuts = (
                settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            )
            d_key = shortcuts.get("draw_line", "D")
            e_key = shortcuts.get("erase_drawing", "E")
            draw_label = (
                f"Line Tool ({d_key})"
                if hasattr(self, "tabs")
                and self.tabs.currentWidget() is self.__dict__.get("tradingview_widget")
                else f"Draw Line ({d_key})"
            )
            self._set_button_state(
                buttons["target"], "Click chart to set breakout", active=True
            )
            self._set_button_state(buttons["draw"], draw_label)
            self._set_button_state(buttons["erase"], f"Erase Drawing ({e_key})")
            self.tradingview_line_tool_active = False
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableTargetMode && window.enableTargetMode();",
                    lambda result: None,
                )
            self.append_log(
                "Breakout price mode enabled. Click a price level on the chart."
            )
        else:
            self.append_log("Breakout price mode requires PyQtWebEngine chart view.")

    def enable_chart_drawing_mode(self) -> None:
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.__dict__.get(
            "tradingview_widget"
        ):
            self.enable_tradingview_line_tool_mode()
            return
        if not self._active_chart_symbol():
            QMessageBox.information(
                self, "No chart symbol", "Plot a symbol before drawing on the chart."
            )
            return
        active_views = self._active_chart_command_views()
        web_views = [
            view
            for view in active_views
            if QWebEngineView is not None and isinstance(view, QWebEngineView)
        ]
        if web_views:
            web_views[0].setFocus()
            buttons = self._active_chart_buttons()
            settings = self.__dict__.get("settings") or {}
            shortcuts = (
                settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            )
            t_key = shortcuts.get("set_target", "T")
            e_key = shortcuts.get("erase_drawing", "E")
            self._set_button_state(buttons["draw"], "Click start point", active=True)
            self._set_button_state(buttons["target"], f"Set Breakout Price ({t_key})")
            self._set_button_state(buttons["erase"], f"Erase Drawing ({e_key})")
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableDrawingMode && window.enableDrawingMode();",
                    lambda result: None,
                )
            self.append_log(
                "Drawing mode enabled. Click start and end points on the chart."
            )
        else:
            self.append_log("Drawing mode requires PyQtWebEngine chart view.")

    def toggle_tradingview_line_tool_mode(self) -> None:
        if getattr(self, "tradingview_line_tool_active", False):
            self.disable_tradingview_line_tool_mode()
        else:
            self.enable_tradingview_line_tool_mode()

    def disable_tradingview_line_tool_mode(self) -> None:
        if (
            not hasattr(self, "tabs")
            or self.tabs.currentWidget() is not self.tradingview_widget
        ):
            return
        symbol = self._active_chart_symbol()
        active_views = self._active_chart_command_views()
        web_views = [
            view
            for view in active_views
            if QWebEngineView is not None and isinstance(view, QWebEngineView)
        ]
        for view in web_views:
            view.page().runJavaScript(
                "window.disableLineToolMode && window.disableLineToolMode();",
                lambda result: None,
            )
        self.tradingview_line_tool_active = False
        settings = self.__dict__.get("settings") or {}
        shortcuts = settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
        d_key = shortcuts.get("draw_line", "D")
        self._set_button_state(
            getattr(self, "tradingview_line_tool_button", None), f"Line Tool ({d_key})"
        )
        if symbol:
            QTimer.singleShot(
                150,
                lambda symbol=symbol: self._sync_tradingview_drawings_after_tool_close(
                    symbol
                ),
            )
        self.append_log("TradingView line tool disabled.")

    def _sync_tradingview_drawings_after_tool_close(self, symbol: str) -> None:
        if (
            not hasattr(self, "tabs")
            or self.tabs.currentWidget() is not self.tradingview_widget
        ):
            return
        active_symbol = self._active_chart_symbol()
        if active_symbol and active_symbol == symbol.strip().upper():
            self.load_tradingview_chart(force=True, skip_split_view=True)

    def enable_tradingview_line_tool_mode(self) -> None:
        if (
            not hasattr(self, "tabs")
            or self.tabs.currentWidget() is not self.tradingview_widget
        ):
            return
        if not self._active_chart_symbol():
            QMessageBox.information(
                self, "No chart symbol", "Load a symbol before drawing on the chart."
            )
            return
        active_views = self._active_chart_command_views()
        web_views = [
            view
            for view in active_views
            if QWebEngineView is not None and isinstance(view, QWebEngineView)
        ]
        if web_views:
            web_views[0].setFocus()
            self.tradingview_line_tool_active = True
            settings = self.__dict__.get("settings") or {}
            shortcuts = (
                settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            )
            t_key = shortcuts.get("set_target", "T")
            self._set_button_state(
                getattr(self, "tradingview_line_tool_button", None),
                "Line Tool Active",
                active=True,
            )
            self._set_button_state(
                getattr(self, "tradingview_set_target_button", None),
                f"Set Breakout Price ({t_key})",
            )
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableLineToolMode && window.enableLineToolMode();",
                    lambda result: None,
                )
            self.append_log(
                "TradingView line tool enabled. Click a line to edit it, or click empty space to draw."
            )
        else:
            self.append_log("Line tool requires PyQtWebEngine chart view.")

    def enable_tradingview_edit_mode(self) -> None:
        if (
            not hasattr(self, "tabs")
            or self.tabs.currentWidget() is not self.tradingview_widget
        ):
            return
        if not self._active_chart_symbol():
            QMessageBox.information(
                self, "No chart symbol", "Load a symbol before editing drawings."
            )
            return
        active_views = self._active_chart_command_views()
        web_views = [
            view
            for view in active_views
            if QWebEngineView is not None and isinstance(view, QWebEngineView)
        ]
        if web_views:
            web_views[0].setFocus()
            self.tradingview_line_tool_active = True
            settings = self.__dict__.get("settings") or {}
            shortcuts = (
                settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            )
            t_key = shortcuts.get("set_target", "T")
            self._set_button_state(
                getattr(self, "tradingview_line_tool_button", None),
                "Line Tool Active",
                active=True,
            )
            self._set_button_state(
                getattr(self, "tradingview_set_target_button", None),
                f"Set Breakout Price ({t_key})",
            )
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableLineToolMode && window.enableLineToolMode();",
                    lambda result: None,
                )
            self.append_log(
                "TradingView line tool enabled. Click a line to edit it, or click empty space to draw."
            )
        else:
            self.append_log("Edit mode requires PyQtWebEngine chart view.")

    def enable_chart_erase_mode(self) -> None:
        if not self._active_chart_symbol():
            QMessageBox.information(
                self, "No chart symbol", "Plot a symbol before erasing drawings."
            )
            return
        active_views = self._active_chart_command_views()
        web_views = [
            view
            for view in active_views
            if QWebEngineView is not None and isinstance(view, QWebEngineView)
        ]
        if web_views:
            web_views[0].setFocus()
            buttons = self._active_chart_buttons()
            settings = self.__dict__.get("settings") or {}
            shortcuts = (
                settings.get("shortcuts", {}) if isinstance(settings, dict) else {}
            )
            t_key = shortcuts.get("set_target", "T")
            d_key = shortcuts.get("draw_line", "D")
            draw_label = (
                f"Line Tool ({d_key})"
                if hasattr(self, "tabs")
                and self.tabs.currentWidget() is self.__dict__.get("tradingview_widget")
                else f"Draw Line ({d_key})"
            )
            self._set_button_state(
                buttons["erase"], "Click drawing to erase", active=True
            )
            self._set_button_state(buttons["target"], f"Set Breakout Price ({t_key})")
            self._set_button_state(buttons["draw"], draw_label)
            for view in web_views:
                view.page().runJavaScript(
                    "window.enableEraseMode && window.enableEraseMode();",
                    lambda result: None,
                )
            self.append_log("Erase mode enabled. Click a drawing line to remove it.")
        else:
            self.append_log("Erase mode requires PyQtWebEngine chart view.")

    def _chart_pan_step_bars(self) -> int:
        settings = self.__dict__.get("settings") or {}
        try:
            step = (
                int(settings.get("chart_pan_step_bars", 1))
                if isinstance(settings, dict)
                else 1
            )
        except (TypeError, ValueError):
            step = 1
        return max(1, step)

    def pan_tradingview_chart_view(self, delta_bars: int) -> None:
        for view in self._active_chart_command_views():
            if QWebEngineView is not None and isinstance(view, QWebEngineView):
                view.page().runJavaScript(
                    f"window.panView && window.panView({int(delta_bars)});",
                    lambda result: None,
                )

    def set_chart_target_price(self, symbol: str, breakout_price: float) -> None:
        symbol = symbol.strip().upper()
        if not symbol or breakout_price <= 0:
            return

        item = self.watchlist.get(symbol)
        if item is None:
            item = self.watchlist.add(symbol=symbol, name=symbol)
        item.breakout_price = round(float(breakout_price), 2)
        self.mark_watchlist_and_dashboard_dirty()
        self._save_state()
        self._reset_chart_mode_buttons()
        self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Saved breakout price for {symbol}: {item.breakout_price:.2f}")

    def clear_chart_target_price(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return

        item = self.watchlist.get(symbol)
        if item is None or item.breakout_price is None:
            return

        env = (
            self.watchlist_env_combo.currentText()
            if hasattr(self, "watchlist_env_combo")
            else "PROD"
        )
        buylist_manager = getattr(self, "buylist_manager", None)
        buylist_item = (
            buylist_manager.get(symbol, env) if buylist_manager is not None else None
        )
        if buylist_item is not None and self._is_execution_queue_buylist_item(
            buylist_item
        ):
            if buylist_item.monitoring_status in (
                "BOUGHT",
                "BUY_SUBMITTED",
                "BUY_PARTIAL",
            ):
                QMessageBox.warning(
                    self,
                    "Active position",
                    f"{symbol} has an active position and cannot be dequeued here. Breakout price was not cleared.",
                )
                return
            buylist_manager.remove(symbol, env)
            self.populate_buylist_dashboard()
            self.append_log(
                f"[Chart] {symbol} removed from execution queue (breakout price cleared)."
            )

        item.breakout_price = None
        self.mark_watchlist_and_dashboard_dirty()
        self._save_state()
        self._reset_chart_mode_buttons()
        self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Removed breakout price for {symbol}.")

    def save_chart_drawing(self, symbol: str, drawing_json: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        try:
            drawing = json.loads(drawing_json)
            clean_drawing = {
                "id": str(
                    drawing.get("id") or f"{symbol}-{dt.datetime.now().timestamp()}"
                ),
                "type": "line",
                "start_date": str(drawing["start_date"]),
                "start_price": round(float(drawing["start_price"]), 2),
                "end_date": str(drawing["end_date"]),
                "end_price": round(float(drawing["end_price"]), 2),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return

        self.chart_drawings.setdefault(symbol, []).append(clean_drawing)
        self._save_state()
        if not self._is_active_tradingview_line_tool_symbol(symbol):
            self._reset_chart_mode_buttons()
            self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Saved chart line for {symbol}.")

    def update_chart_drawing(self, symbol: str, drawing_json: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        try:
            drawing = json.loads(drawing_json)
            drawing_id = str(drawing["id"])
            clean_drawing = {
                "id": drawing_id,
                "type": "line",
                "start_date": str(drawing["start_date"]),
                "start_price": round(float(drawing["start_price"]), 2),
                "end_date": str(drawing["end_date"]),
                "end_price": round(float(drawing["end_price"]), 2),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return

        drawings = self.chart_drawings.get(symbol, [])
        for index, existing in enumerate(drawings):
            if str(existing.get("id")) == drawing_id:
                drawings[index] = clean_drawing
                self.chart_drawings[symbol] = drawings
                self._save_state()
                if not self._is_active_tradingview_line_tool_symbol(symbol):
                    self.refresh_other_chart_views_for_symbol(symbol)
                self.append_log(f"Updated chart line for {symbol}.")
                return

    def delete_chart_drawing(self, symbol: str, drawing_id: str) -> None:
        symbol = symbol.strip().upper()
        drawing_id = str(drawing_id)
        drawings = self.chart_drawings.get(symbol, [])
        remaining = [
            drawing for drawing in drawings if str(drawing.get("id")) != drawing_id
        ]
        if len(remaining) == len(drawings):
            return
        if remaining:
            self.chart_drawings[symbol] = remaining
        else:
            self.chart_drawings.pop(symbol, None)
        self._save_state()
        if not self._is_active_tradingview_line_tool_symbol(symbol):
            self._reset_chart_mode_buttons()
            self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Removed chart drawing for {symbol}.")

    def _is_active_tradingview_line_tool_symbol(self, symbol: str) -> bool:
        if not self.__dict__.get("tradingview_line_tool_active", False):
            return False
        if not hasattr(
            self, "tabs"
        ) or self.tabs.currentWidget() is not self.__dict__.get("tradingview_widget"):
            return False
        return self._active_chart_symbol() == symbol.strip().upper()

    def clear_chart_drawings(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol or symbol not in self.chart_drawings:
            return
        self.chart_drawings.pop(symbol, None)
        self._save_state()
        self._reset_chart_mode_buttons()
        self.refresh_other_chart_views_for_symbol(symbol)
        self.append_log(f"Removed all chart drawings for {symbol}.")

    def clear_current_chart_drawings(self) -> None:
        symbol = self._active_chart_symbol()
        if not symbol:
            QMessageBox.information(
                self, "No chart symbol", "Plot a symbol before erasing drawings."
            )
            return
        for active_view in self._active_chart_command_views():
            if QWebEngineView is not None and isinstance(active_view, QWebEngineView):
                active_view.page().runJavaScript(
                    "window.clearAllDrawings && window.clearAllDrawings();"
                )
        self.clear_chart_drawings(symbol)

    def clear_current_chart_target(self) -> None:
        symbol = self._active_chart_symbol()
        if not symbol:
            QMessageBox.information(
                self,
                "No chart symbol",
                "Plot a symbol before clearing the breakout price.",
            )
            return
        for active_view in self._active_chart_command_views():
            if QWebEngineView is not None and isinstance(active_view, QWebEngineView):
                active_view.page().runJavaScript(
                    "window.clearTargetPrice && window.clearTargetPrice();"
                )
        self.clear_chart_target_price(symbol)

    def _get_chart_options(self) -> dict:
        return {
            "show_volume": self.chart_show_volume_checkbox.isChecked(),
            "show_rs": self.chart_show_rs_checkbox.isChecked(),
            "show_ema": self.chart_show_ema_checkbox.isChecked(),
            "show_adr": self.chart_show_adr_checkbox.isChecked(),
            "show_growth_1m": self.chart_show_growth_1m_checkbox.isChecked(),
            "show_growth_3m": self.chart_show_growth_3m_checkbox.isChecked(),
            "show_growth_6m": self.chart_show_growth_6m_checkbox.isChecked(),
        }

    def _get_chart_navigation_state(self) -> dict:
        symbol = self._get_chart_symbol() or (self.selected_scan_symbol or "")
        return self.chart_view_windows.get(symbol.strip().upper(), {}).copy()

    def _get_chart_render_options(self) -> dict:
        options = self._get_chart_options()
        navigation = self._get_chart_navigation_state()
        if "bars" in navigation:
            options["visible_bars"] = navigation["bars"]
        if "end" in navigation:
            options["visible_end"] = navigation["end"]
        return options

    def _get_chart_render_options_for_timeframe(self, timeframe: str) -> dict:
        options = self._get_chart_render_options()
        timeframe = timeframe.strip().upper()
        if timeframe == "1H":
            options.update(
                {
                    "show_rs": False,
                    "show_adr": False,
                    "show_growth_1m": False,
                    "show_growth_3m": False,
                    "show_growth_6m": False,
                    "intraday_chart": True,
                    "max_history_bars": 2000,
                }
            )
        return options
