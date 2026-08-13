"""Chart history loading and plot refresh orchestration."""

from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtWidgets import QComboBox, QMessageBox

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.api.kis_account_snapshot_dual import KisEnvironment, load_config
from src.infrastructure.database.repositories.chart_indicators import (
    load_chart_indicators_from_db, refresh_chart_indicators_for_symbol)
from src.infrastructure.database.repositories.market_bars import \
    save_symbol_history_to_db

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class ChartsPlottingMixin:
    def _load_chart_history_for_timeframe(
        self,
        symbol: str,
        timeframe: str,
        use_live_fallback: bool = True,
        window_days: int = 7,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        from src.ui.charts.data_service import ChartDataController
        from src.ui.controllers.base import get_controller

        controller = get_controller(self, "chart_data_controller", ChartDataController)
        return controller.load_history_for_timeframe(
            symbol,
            timeframe,
            use_live_fallback=use_live_fallback,
            window_days=window_days,
            force_refresh=force_refresh,
        )

    def _fetch_latest_daily_bar_for_chart(self, symbol: str) -> pd.DataFrame:
        """Fetch the latest available daily OHLCV bar from KIS for chart refresh."""
        symbol = symbol.strip().upper()
        if not symbol:
            return pd.DataFrame()
        now = dt.datetime.now(dt.timezone.utc)
        config, profile_key = self._chart_kis_daily_config()
        unavailable_until = self.__dict__.get("kis_daily_chart_unavailable_until")
        unavailable_key = self.__dict__.get("kis_daily_chart_unavailable_key", "")
        if (
            unavailable_until is not None
            and profile_key == unavailable_key
            and now < unavailable_until
        ):
            return pd.DataFrame()
        try:
            from src.api.kis_fetch_all_daily import (
                KISClient, fetch_watchlist_overseas_daily_bars)

            if config is None and profile_key != "legacy":
                raise RuntimeError(
                    f"Selected KIS chart profile is not configured: {profile_key}"
                )
            target_yyyymmdd = dt.datetime.now(KST_ZONE).strftime("%Y%m%d")
            client = None
            exchanges = ("NAS", "NYS", "AMS")
            if config is not None:
                client = KISClient(
                    app_key=config.app_key,
                    app_secret=config.app_secret,
                    base_url=config.base_url,
                )
                exchanges = self._chart_kis_daily_exchanges(config.overseas_exchanges)
            records = fetch_watchlist_overseas_daily_bars(
                [symbol],
                target_yyyymmdd=target_yyyymmdd,
                client=client,
                exchanges=exchanges,
            )
            if not records:
                return pd.DataFrame()
            row = records[0]
            date_value = pd.to_datetime(
                str(row.get("date", "")), format="%Y%m%d", errors="coerce"
            )
            if pd.isna(date_value):
                return pd.DataFrame()
            frame = pd.DataFrame(
                {
                    "Open": [float(row.get("open", 0) or 0)],
                    "High": [float(row.get("high", 0) or 0)],
                    "Low": [float(row.get("low", 0) or 0)],
                    "Close": [float(row.get("close", 0) or 0)],
                    "Volume": [float(row.get("volume", 0) or 0)],
                },
                index=[pd.Timestamp(date_value)],
            )
            if self.db_enabled and self.db_engine is not None:
                save_symbol_history_to_db(symbol, frame, self.db_engine, interval="1d")
            return frame
        except Exception as exc:
            self.kis_daily_chart_unavailable_until = now + dt.timedelta(
                seconds=KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS
            )
            self.kis_daily_chart_unavailable_key = profile_key
            error_text = str(exc)
            last_error = self.__dict__.get("kis_daily_chart_last_error", "")
            if error_text != last_error:
                self.kis_daily_chart_last_error = error_text
                self.append_log(
                    f"KIS daily chart refresh unavailable ({error_text}). "
                    "Using yfinance fallback for chart loads."
                )
            return pd.DataFrame()

    def _chart_kis_daily_config(self):
        profile = None
        kis_account_combo = self.__dict__.get("kis_account_combo")
        kis_environment_combo = self.__dict__.get("kis_environment_combo")
        if kis_account_combo is not None and kis_environment_combo is not None:
            selected_profile = kis_account_combo.currentData()
            if selected_profile:
                profile = {
                    "environment": kis_environment_combo.currentText(),
                    "account_no": selected_profile.get("account_no", ""),
                    "label": selected_profile.get("label", ""),
                }

        trade_account_combo = self.__dict__.get("trade_kis_account_combo")
        trade_environment_combo = self.__dict__.get("trade_kis_environment_combo")
        if (
            profile is None
            and trade_account_combo is not None
            and trade_environment_combo is not None
        ):
            trade_profile = trade_account_combo.currentData()
            if trade_profile:
                profile = {
                    "environment": trade_environment_combo.currentText(),
                    "account_no": trade_profile.get("account_no", ""),
                    "label": trade_profile.get("label", ""),
                }
        if profile is None:
            return None, "legacy"

        environment = str(profile.get("environment") or "PROD").strip().upper()
        account_no = str(profile.get("account_no") or "").strip()
        profile_key = f"{environment}:{account_no or 'default'}"
        try:
            return (
                load_config(
                    KisEnvironment(environment), account_no_override=account_no or None
                ),
                profile_key,
            )
        except Exception:
            return None, profile_key

    @staticmethod
    def _chart_kis_daily_exchanges(exchanges) -> tuple[str, ...]:
        aliases = {
            "NASD": "NAS",
            "NASDAQ": "NAS",
            "NYSE": "NYS",
            "AMEX": "AMS",
        }
        normalized = []
        for exchange in exchanges or ():
            code = aliases.get(
                str(exchange).strip().upper(), str(exchange).strip().upper()
            )
            if code and code not in normalized:
                normalized.append(code)
        return tuple(normalized) or ("NAS", "NYS", "AMS")

    def update_chart_window(
        self, symbol: str, visible_bars: int, visible_end: int
    ) -> None:
        symbol = symbol.strip().upper()
        if not symbol:
            return
        self.chart_view_windows[symbol] = {
            "bars": max(20, int(visible_bars)),
            "end": max(1, int(visible_end)),
        }
        self._set_chart_symbol(symbol)
        self.plot_selected_symbol(show_warnings=False)

    def pan_chart_window(self, delta_bars: int) -> None:
        symbol = self._get_chart_symbol() or (self.selected_scan_symbol or "")
        symbol = symbol.strip().upper()
        if not symbol:
            return

        state = self.chart_view_windows.get(symbol, {"bars": 90})
        visible_bars = max(20, int(state.get("bars", 90)))
        visible_end = int(state.get("end", 0))
        max_end = visible_end
        timeframe = (
            self.chart_timeframe_combo.currentText().strip().upper()
            if hasattr(self, "chart_timeframe_combo")
            and not (
                hasattr(self, "chart_split_screen_checkbox")
                and self.chart_split_screen_checkbox.isChecked()
            )
            else "1D"
        )
        if self.db_enabled and self.db_engine is not None:
            history = self._load_chart_history_for_timeframe(
                symbol, timeframe, use_live_fallback=False
            )
            chart_history = self._normalize_chart_history(history, symbol)
            if not chart_history.empty:
                max_end = len(chart_history) + min(30, max(0, visible_bars - 5))
                if visible_end <= 0:
                    visible_end = len(chart_history)
        if max_end <= 0:
            return

        next_end = max(1, min(max_end, visible_end + int(delta_bars)))
        self.update_chart_window(symbol, visible_bars, next_end)

    def step_chart_symbol(self, direction: int) -> None:
        if not isinstance(self.chart_symbol_input, QComboBox):
            return
        if self.chart_symbol_input.count() == 0:
            self.populate_chart_symbol_combo()
        count = self.chart_symbol_input.count()
        if count == 0:
            return

        current_symbol = self._get_chart_symbol()
        symbols = [
            self.chart_symbol_input.itemText(index).strip().upper()
            for index in range(count)
        ]
        try:
            current_index = symbols.index(current_symbol)
        except ValueError:
            current_index = 0 if direction > 0 else count - 1

        next_index = max(0, min(count - 1, current_index + int(direction)))
        self.chart_symbol_input.setCurrentIndex(next_index)
        self._set_chart_symbol(self.chart_symbol_input.itemText(next_index))
        self.plot_selected_symbol(show_warnings=False)

    def reset_chart_full_view(self, symbol: Optional[str] = None) -> None:
        tabs = self.__dict__.get("tabs")
        is_intraday = tabs is not None and tabs.currentWidget() is self.__dict__.get(
            "intraday_charts_widget"
        )
        is_tradingview = tabs is not None and tabs.currentWidget() is self.__dict__.get(
            "tradingview_widget"
        )
        symbol = (symbol or self._active_chart_symbol() or "").strip().upper()
        if not symbol:
            return
        if is_tradingview:
            for active_view in self._active_chart_command_views():
                if QWebEngineView is not None and isinstance(
                    active_view, QWebEngineView
                ):
                    active_view.page().runJavaScript(
                        "window.resetFullView && window.resetFullView();"
                    )
            return
        self.chart_view_windows.pop(symbol, None)
        if is_intraday:
            self._set_intraday_symbol(symbol)
            self.plot_intraday_watchlist_symbol()
            return
        self._set_chart_symbol(symbol)
        self.plot_selected_symbol(show_warnings=False)

    def plot_selected_symbol(
        self,
        checked: bool = False,
        show_warnings: bool = True,
        use_live_fallback: bool = False,
    ) -> None:
        """Plot a symbol's price history using a local in-app chart."""
        symbol = self._get_chart_symbol()
        if not symbol and self.selected_scan_symbol:
            symbol = self.selected_scan_symbol

        if not symbol:
            if show_warnings:
                QMessageBox.warning(
                    self, "No symbol", "Enter or select a symbol to plot."
                )
            return

        split_enabled = (
            hasattr(self, "chart_split_screen_checkbox")
            and self.chart_split_screen_checkbox.isChecked()
        )
        timeframes = (
            ["1D", "1H"]
            if split_enabled
            else [
                (
                    self.chart_timeframe_combo.currentText().strip().upper()
                    if hasattr(self, "chart_timeframe_combo")
                    else "1D"
                )
            ]
        )

        histories = {
            timeframe: self._load_chart_history_for_timeframe(
                symbol, timeframe, use_live_fallback=use_live_fallback
            )
            for timeframe in timeframes
        }
        if all(history.empty for history in histories.values()):
            if show_warnings:
                QMessageBox.warning(
                    self,
                    "No data",
                    f"Unable to validate {symbol}. Symbol may not exist.",
                )
            else:
                self._set_html_or_text(
                    self.chart_view,
                    self._generate_message_html(symbol, "No chart data found."),
                    f"{symbol}: no chart data found.",
                )
            return

        chart_histories = {
            timeframe: self._normalize_chart_history(
                history,
                symbol,
                max_rows=self._get_chart_render_options_for_timeframe(timeframe).get(
                    "max_history_bars", 180
                ),
            )
            for timeframe, history in histories.items()
        }
        if all(history.empty for history in chart_histories.values()):
            if show_warnings:
                QMessageBox.warning(
                    self, "No data", f"Unable to build a chart for {symbol}."
                )
            else:
                self._set_html_or_text(
                    self.chart_view,
                    self._generate_message_html(
                        symbol, "Unable to build chart from available data."
                    ),
                    f"{symbol}: unable to build chart from available data.",
                )
            return

        indicators = pd.DataFrame()
        if "1D" in timeframes and self.db_enabled and self.db_engine is not None:
            indicators = load_chart_indicators_from_db(symbol, self.db_engine)
            if indicators.empty and refresh_chart_indicators_for_symbol(
                symbol, self.db_engine, reference_symbol=REFERENCE_SYMBOL
            ):
                indicators = load_chart_indicators_from_db(symbol, self.db_engine)

        watchlist_item = self.watchlist.get(symbol)
        target_price = (
            watchlist_item.breakout_price if watchlist_item is not None else None
        )
        primary_timeframe = timeframes[0]
        drawings = self._build_combined_drawings(symbol, primary_timeframe)
        primary_history = chart_histories.get(primary_timeframe, pd.DataFrame())
        primary_options = self._get_chart_render_options_for_timeframe(
            primary_timeframe
        )
        primary_window_start, primary_window_end = self._get_visible_time_window(
            primary_history, primary_options
        )
        if primary_history.empty:
            primary_html = self._generate_message_html(
                symbol, f"No {primary_timeframe} chart data available."
            )
        else:
            primary_html = self._generate_local_chart_html(
                symbol,
                primary_history,
                indicators=indicators if primary_timeframe == "1D" else pd.DataFrame(),
                options=primary_options,
                target_price=target_price,
                drawings=drawings,
            )
        self._set_html_or_text(
            self.chart_view,
            primary_html,
            (
                f"{symbol} chart data loaded.\n\n"
                f"Latest close: {float(primary_history['Close'].iloc[-1]):.2f}"
                if not primary_history.empty
                else f"{symbol}: no {primary_timeframe} chart data."
            ),
        )

        if split_enabled:
            self.chart_split_view.setVisible(True)
            split_history = chart_histories.get("1H", pd.DataFrame())
            split_options = self._get_chart_render_options_for_timeframe("1H")
            if primary_window_start is not None and primary_window_end is not None:
                split_options["visible_start_time"] = primary_window_start
                split_options["visible_end_time"] = primary_window_end
                split_options["visible_end_time_is_date"] = primary_timeframe == "1D"
            split_drawings = self._build_combined_drawings(symbol, "1H")
            split_html = (
                self._generate_message_html(
                    symbol,
                    "No 1H chart data available. Update Watchlist Intraday or wait for background fetch.",
                )
                if split_history.empty
                else self._generate_local_chart_html(
                    symbol,
                    split_history,
                    indicators=pd.DataFrame(),
                    options=split_options,
                    target_price=target_price,
                    drawings=split_drawings,
                )
            )
            self._set_html_or_text(
                self.chart_split_view,
                split_html,
                (
                    f"{symbol} 1H chart loaded."
                    if not split_history.empty
                    else f"{symbol}: no 1H chart data."
                ),
            )
        else:
            self.chart_split_view.setVisible(False)
        if not primary_history.empty:
            self.statusBar().showMessage(
                f"{symbol} {primary_timeframe} chart loaded. "
                f"Indicator cache: {'loaded' if not indicators.empty else 'not available'}."
            )

    def _draw_placeholder_chart(self) -> None:
        """Display placeholder chart."""
        placeholder_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Chart Placeholder</title>
            <style>
                body {
                    margin: 0;
                    padding: 0;
                    background-color: #1e1e1e;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    height: 100vh;
                    color: white;
                    font-family: Arial, sans-serif;
                }
                .placeholder {
                    text-align: center;
                    font-size: 18px;
                    color: #888;
                }
            </style>
        </head>
        <body>
            <div class="placeholder">
                <p>Select a symbol and click "Plot Selected Symbol" to view the local chart</p>
            </div>
        </body>
        </html>
        """
        if QWebEngineView is not None:
            self.chart_view.setHtml(placeholder_html)
        else:
            self.chart_view.setPlainText(
                "Select a symbol and click Plot Selected Symbol."
            )
