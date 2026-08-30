"""Interactive chart drawing and target commands."""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any, List

from PyQt5.QtWidgets import QMessageBox
from zoneinfo import ZoneInfo

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
        return None

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

    def _active_web_views(self) -> List[Any]:
        if QWebEngineView is None:
            return []
        return [
            view
            for view in self._active_chart_command_views()
            if isinstance(view, QWebEngineView)
        ]

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
        return ""

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
        return {"target": None, "draw": None, "erase": None}

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
        for prefix in ["intraday"]:
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
        web_views = self._active_web_views()
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
        web_views = self._active_web_views()
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
        web_views = self._active_web_views()
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
        self.append_log("TradingView line tool disabled.")

    def enable_tradingview_line_tool_mode(self) -> None:
        self._enable_tradingview_line_tool_mode(
            missing_symbol_message="Load a symbol before drawing on the chart.",
            unavailable_message="Line tool requires PyQtWebEngine chart view.",
        )

    def _enable_tradingview_line_tool_mode(
        self,
        *,
        missing_symbol_message: str,
        unavailable_message: str,
    ) -> None:
        if (
            not hasattr(self, "tabs")
            or self.tabs.currentWidget() is not self.tradingview_widget
        ):
            return
        if not self._active_chart_symbol():
            QMessageBox.information(self, "No chart symbol", missing_symbol_message)
            return
        web_views = self._active_web_views()
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
            self.append_log(unavailable_message)

    def enable_tradingview_edit_mode(self) -> None:
        self._enable_tradingview_line_tool_mode(
            missing_symbol_message="Load a symbol before editing drawings.",
            unavailable_message="Edit mode requires PyQtWebEngine chart view.",
        )

    def enable_chart_erase_mode(self) -> None:
        if not self._active_chart_symbol():
            QMessageBox.information(
                self, "No chart symbol", "Plot a symbol before erasing drawings."
            )
            return
        web_views = self._active_web_views()
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

    def _run_tradingview_split_javascript(
        self, symbol: str, script: str, *, exclude_view: str = ""
    ) -> None:
        """Apply a lightweight state update to the visible 1D/1H pair."""
        symbol = symbol.strip().upper()
        if not symbol or QWebEngineView is None:
            return
        tabs = self.__dict__.get("tabs")
        if (
            tabs is None
            or tabs.currentWidget() is not self.__dict__.get("tradingview_widget")
        ):
            return
        symbol_combo = self.__dict__.get("tradingview_symbol_combo")
        if (
            symbol_combo is None
            or symbol_combo.currentText().strip().upper() != symbol
        ):
            return
        split_view = self.__dict__.get("tradingview_split_chart_view")
        if split_view is None or not split_view.isVisible():
            return

        views = (
            ("left", self.__dict__.get("tradingview_chart_view")),
            ("right", split_view),
        )
        for view_key, view in views:
            if view_key == exclude_view:
                continue
            if isinstance(view, QWebEngineView):
                view.page().runJavaScript(script, lambda result: None)

    def sync_tradingview_crosshair(
        self,
        symbol: str,
        source_view: str,
        chart_time: str,
        price: float,
        visible: bool,
    ) -> None:
        """Mirror actual cursor time/price without coupling either chart's range."""
        source_view = str(source_view).strip().lower()
        if source_view not in {"left", "right"}:
            return
        if visible:
            try:
                price_value = float(price)
            except (TypeError, ValueError):
                return
            time_value = str(chart_time).strip()
            if not time_value or not math.isfinite(price_value) or price_value <= 0:
                return
            script = (
                "window.showSyncedCrosshair && "
                f"window.showSyncedCrosshair({json.dumps(time_value)}, "
                f"{price_value!r});"
            )
        else:
            script = "window.clearSyncedCrosshair && window.clearSyncedCrosshair();"
        self._run_tradingview_split_javascript(
            symbol, script, exclude_view=source_view
        )

    def _sync_tradingview_target_price(self, symbol: str, price: float | None) -> None:
        price_json = "null" if price is None else json.dumps(float(price))
        self._run_tradingview_split_javascript(
            symbol,
            "window.applySyncedTargetPrice && "
            f"window.applySyncedTargetPrice({price_json});",
        )

    @staticmethod
    def _normalize_drawing_timeframe(timeframe: str | None) -> str:
        if timeframe is None:
            return ""
        normalized = str(timeframe).strip().upper().replace(" ", "")
        if not normalized:
            return ""
        if normalized.endswith("MIN"):
            normalized = normalized[:-3] + "M"
        return normalized

    @staticmethod
    def _infer_drawing_timeframe_from_dates(
        start_date: object, end_date: object
    ) -> str:
        for value in (start_date, end_date):
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            if (
                len(text) > 10
                or " " in text
                or "T" in text
                or "Z" in text
            ):
                return "INTRADAY"
        return "1D"

    @staticmethod
    def _is_intraday_drawing_timeframe(timeframe: str | None) -> bool:
        return bool(timeframe) and timeframe != "1D"

    def _resolve_drawing_timeframe(self, drawing: dict, default: str | None = None) -> str:
        raw_timeframe = None
        if isinstance(drawing, dict):
            raw_timeframe = drawing.get("timeframe")
        resolved = self._normalize_drawing_timeframe(
            raw_timeframe if raw_timeframe is not None else default
        )
        if resolved:
            return resolved
        if isinstance(drawing, dict):
            inferred = self._infer_drawing_timeframe_from_dates(
                drawing.get("start_date"), drawing.get("end_date")
            )
            if inferred:
                return inferred
        return self._normalize_drawing_timeframe(default)

    def _drawing_timeframes_match(self, requested: str, existing: str) -> bool:
        requested_tf = self._normalize_drawing_timeframe(requested)
        existing_tf = self._normalize_drawing_timeframe(existing)
        if not requested_tf or not existing_tf:
            return True
        if requested_tf == existing_tf:
            return True
        shared_daily_hourly_timeframes = {"1D", "1H", "INTRADAY"}
        if {
            requested_tf,
            existing_tf,
        }.issubset(shared_daily_hourly_timeframes):
            return True
        return (
            requested_tf != "1D"
            and existing_tf != "1D"
            and self._is_intraday_drawing_timeframe(requested_tf)
            and self._is_intraday_drawing_timeframe(existing_tf)
        )

    def _active_chart_drawing_timeframe(self) -> str:
        # ``MainWindow.__new__`` is used by pure controller tests. Accessing a
        # missing Qt attribute through ``hasattr`` can invoke sip before the
        # QWidget base is initialized, so inspect the instance dictionary.
        tabs = self.__dict__.get("tabs")
        if (
            tabs is not None
            and tabs.currentWidget() is self.__dict__.get("tradingview_widget")
            and self.__dict__.get("tradingview_timeframe_combo") is not None
        ):
            return self._normalize_drawing_timeframe(
                self.tradingview_timeframe_combo.currentText()
            )
        if (
            tabs is not None
            and tabs.currentWidget() is self.__dict__.get("intraday_charts_widget")
            and self.__dict__.get("intraday_interval_combo") is not None
        ):
            return self._normalize_drawing_timeframe(
                self.intraday_interval_combo.currentText()
        )
        return "1D"

    def _sync_tradingview_drawing(self, symbol: str, drawing: dict) -> None:
        drawing = dict(drawing or {})
        drawing["timeframe"] = self._resolve_drawing_timeframe(
            drawing, default=self._active_chart_drawing_timeframe()
        )
        drawing_json = json.dumps(drawing, separators=(",", ":"))
        symbol_json = json.dumps(symbol.strip().upper())
        self._run_tradingview_drawing_javascript(
            symbol,
            "window.upsertSyncedDrawing && "
            f"window.upsertSyncedDrawing({drawing_json}, {symbol_json});",
        )

    def _run_tradingview_drawing_javascript(
        self, symbol: str, script: str
    ) -> None:
        """Push drawing state to every currently active TradingView chart."""
        symbol = symbol.strip().upper()
        if not symbol or QWebEngineView is None:
            return
        tabs = self.__dict__.get("tabs")
        if (
            tabs is None
            or tabs.currentWidget() is not self.__dict__.get("tradingview_widget")
        ):
            return
        symbol_combo = self.__dict__.get("tradingview_symbol_combo")
        if (
            symbol_combo is None
            or symbol_combo.currentText().strip().upper() != symbol
        ):
            return

        views = [self.__dict__.get("tradingview_chart_view")]
        split_view = self.__dict__.get("tradingview_split_chart_view")
        split_checkbox = self.__dict__.get("tradingview_split_screen_checkbox")
        split_enabled = False
        if split_checkbox is not None:
            split_enabled = bool(split_checkbox.isChecked())
        elif split_view is not None:
            split_enabled = bool(split_view.isVisible())
        if split_enabled:
            views.append(split_view)

        for view in views:
            if isinstance(view, QWebEngineView):
                view.page().runJavaScript(script, lambda result: None)

    def _tradingview_drawing_snapshot_script(self, symbol: str) -> str:
        symbol = symbol.strip().upper()
        drawings = [
            drawing
            for drawing in self.chart_drawings.get(symbol, [])
            if isinstance(drawing, dict)
        ]
        return (
            "window.replaceSyncedDrawings && "
            f"window.replaceSyncedDrawings({json.dumps(symbol)}, "
            f"{json.dumps(drawings, separators=(',', ':'))});"
        )

    def _resync_tradingview_drawings_in_view(self, view) -> None:
        """Reconcile a freshly loaded page with the latest persisted drawings."""
        if QWebEngineView is None or not isinstance(view, QWebEngineView):
            return
        symbol_combo = self.__dict__.get("tradingview_symbol_combo")
        if symbol_combo is None:
            return
        symbol = symbol_combo.currentText().strip().upper()
        if not symbol:
            return
        view.page().runJavaScript(
            self._tradingview_drawing_snapshot_script(symbol),
            lambda result: None,
        )

    def _sync_all_tradingview_drawings(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        self._run_tradingview_drawing_javascript(
            symbol, self._tradingview_drawing_snapshot_script(symbol)
        )

    def _remove_tradingview_drawing(
        self, symbol: str, drawing_id: str, timeframe: str | None = None
    ) -> None:
        payload = {
            "id": str(drawing_id),
        }
        resolved_timeframe = self._resolve_drawing_timeframe(
            {}, default=self._normalize_drawing_timeframe(timeframe or None)
        )
        if resolved_timeframe:
            payload["timeframe"] = resolved_timeframe
        drawing_id_json = json.dumps(payload, separators=(",", ":"))
        symbol_json = json.dumps(symbol.strip().upper())
        self._run_tradingview_drawing_javascript(
            symbol,
            "window.removeSyncedDrawing && "
            f"window.removeSyncedDrawing({drawing_id_json}, {symbol_json});",
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
        self._sync_tradingview_target_price(symbol, item.breakout_price)
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
        self._sync_tradingview_target_price(symbol, None)
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
            active_timeframe = self._active_chart_drawing_timeframe()
            drawing_timeframe = self._resolve_drawing_timeframe(
                drawing, default=active_timeframe
            )
            clean_drawing = {
                "id": str(
                    drawing.get("id") or f"{symbol}-{dt.datetime.now().timestamp()}"
                ),
                "type": "line",
                "start_date": str(drawing["start_date"]),
                "start_price": round(float(drawing["start_price"]), 2),
                "end_date": str(drawing["end_date"]),
                "end_price": round(float(drawing["end_price"]), 2),
                "timeframe": drawing_timeframe,
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return

        self.chart_drawings.setdefault(symbol, []).append(clean_drawing)
        self._sync_tradingview_drawing(symbol, clean_drawing)
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
            active_timeframe = self._active_chart_drawing_timeframe()
            drawing_timeframe = self._resolve_drawing_timeframe(
                drawing, default=active_timeframe
            )
            clean_drawing = {
                "id": drawing_id,
                "type": "line",
                "start_date": str(drawing["start_date"]),
                "start_price": round(float(drawing["start_price"]), 2),
                "end_date": str(drawing["end_date"]),
                "end_price": round(float(drawing["end_price"]), 2),
                "timeframe": drawing_timeframe,
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return

        drawings = self.chart_drawings.get(symbol, [])
        for index, existing in enumerate(drawings):
            if str(existing.get("id")) != drawing_id:
                continue
            existing_timeframe = self._resolve_drawing_timeframe(
                existing, default=active_timeframe
            )
            if not self._drawing_timeframes_match(
                drawing_timeframe, existing_timeframe
            ):
                continue
            drawings[index] = clean_drawing
            self.chart_drawings[symbol] = drawings
            self._sync_tradingview_drawing(symbol, clean_drawing)
            self._save_state()
            if not self._is_active_tradingview_line_tool_symbol(symbol):
                self.refresh_other_chart_views_for_symbol(symbol)
            self.append_log(f"Updated chart line for {symbol}.")
            return

    def delete_chart_drawing(
        self, symbol: str, drawing_id: str, timeframe: str | None = None
    ) -> None:
        symbol = symbol.strip().upper()
        requested_timeframe = self._resolve_drawing_timeframe(
            {"timeframe": timeframe},
            default=self._active_chart_drawing_timeframe(),
        )
        drawing_id = str(drawing_id)
        drawings = self.chart_drawings.get(symbol, [])
        remaining = []
        removed = False
        for drawing in drawings:
            if not isinstance(drawing, dict):
                continue
            if str(drawing.get("id")) == drawing_id:
                drawing_timeframe = self._resolve_drawing_timeframe(
                    drawing, default=requested_timeframe
                )
                if self._drawing_timeframes_match(
                    requested_timeframe, drawing_timeframe
                ):
                    removed = True
                    continue
            remaining.append(drawing)
        if not removed:
            return
        if remaining:
            self.chart_drawings[symbol] = remaining
        else:
            self.chart_drawings.pop(symbol, None)
        self._remove_tradingview_drawing(
            symbol, drawing_id, timeframe=requested_timeframe
        )
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
        self._sync_all_tradingview_drawings(symbol)
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
