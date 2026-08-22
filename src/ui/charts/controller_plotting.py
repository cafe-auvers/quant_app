"""Chart history loading and plot refresh orchestration."""

from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None

from src.api.kis_account_snapshot_dual import KisEnvironment, load_config
from src.infrastructure.database.repositories.market_bars import \
    save_symbol_history_to_db

KST_ZONE = ZoneInfo("Asia/Seoul")
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60


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
        """Apply viewport callbacks from the shared intraday chart renderer."""
        symbol = symbol.strip().upper()
        if not symbol:
            return
        self.chart_view_windows[symbol] = {
            "bars": max(20, int(visible_bars)),
            "end": max(1, int(visible_end)),
        }
        tabs = self.__dict__.get("tabs")
        if (
            tabs is not None
            and tabs.currentWidget() is self.__dict__.get("intraday_charts_widget")
            and self.__dict__.get("intraday_symbol_combo") is not None
            and self.intraday_symbol_combo.currentText().strip().upper() == symbol
        ):
            self.plot_intraday_watchlist_symbol(allow_fetch=False)

    def step_chart_symbol(self, direction: int) -> None:
        """Route the shared renderer callback to the active remaining chart."""
        tabs = self.__dict__.get("tabs")
        active_widget = tabs.currentWidget() if tabs is not None else None
        if active_widget is self.__dict__.get("tradingview_widget"):
            self.step_tradingview_watchlist_symbol(direction)
        elif active_widget is self.__dict__.get("intraday_charts_widget"):
            self.step_intraday_watchlist_symbol(direction)

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
        if is_intraday:
            self.chart_view_windows.pop(symbol, None)
            self._set_intraday_symbol(symbol)
            self.plot_intraday_watchlist_symbol(allow_fetch=False)
