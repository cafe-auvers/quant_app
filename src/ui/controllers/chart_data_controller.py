from __future__ import annotations

import pandas as pd

from src.core.orb import resample_intraday_bars
from src.ui.controllers.base import WindowController
from src.utils.data_loader import download_price_history
from src.utils.db_loader import load_hourly_history_from_db, load_symbol_history_from_db


class ChartDataController(WindowController):
    """Own chart data loading and cache/fallback selection workflows."""

    def load_history_for_timeframe(
        self,
        symbol: str,
        timeframe: str,
        use_live_fallback: bool = True,
        window_days: int = 7,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        timeframe = timeframe.strip().upper()
        symbol = symbol.strip().upper()
        window_days = max(1, min(7, int(window_days or 7)))
        if timeframe == "5M":
            cached = self._load_cached_intraday_5m(symbol, window_days=window_days)
            if cached is not None and not cached.empty:
                return cached
            if self._can_start_intraday_fetch(symbol, window_days):
                self.start_intraday_fetch(symbol, window_days=window_days)
            if use_live_fallback:
                return download_price_history([symbol], period=f"{window_days}d", interval="5m", max_symbols=1)
            return pd.DataFrame()

        if timeframe == "1H":
            hourly_history = pd.DataFrame()
            if self.db_enabled and self.db_engine is not None:
                hourly_history = load_hourly_history_from_db(symbol, self.db_engine)

            cached = self._load_cached_intraday_5m(symbol, window_days=window_days)
            cached_hourly = pd.DataFrame()
            if cached is not None and not cached.empty:
                cached_hourly = resample_intraday_bars(cached, "1h")

            history = self._merge_chart_histories(hourly_history, cached_hourly, symbol)
            if force_refresh and use_live_fallback:
                latest = download_price_history([symbol], period="730d", interval="1h", max_symbols=1)
                history = self._merge_chart_histories(history, latest, symbol)
            if not history.empty:
                return history

            if self._can_start_intraday_fetch(symbol, window_days):
                self.start_intraday_fetch(symbol, window_days=window_days)
            if use_live_fallback:
                return download_price_history([symbol], period="730d", interval="1h", max_symbols=1)
            return pd.DataFrame()

        history = pd.DataFrame()
        if self.db_enabled:
            history = load_symbol_history_from_db(symbol, self.db_engine, interval="1d")
        if force_refresh and use_live_fallback:
            latest = self._fetch_latest_daily_bar_for_chart(symbol)
            if history.empty or latest.empty:
                fallback = download_price_history([symbol], period="1mo", interval="1d", max_symbols=1)
                history = self._merge_chart_histories(history, fallback, symbol)
            if not latest.empty:
                history = self._merge_chart_histories(history, latest, symbol)
        if history.empty and use_live_fallback:
            history = download_price_history([symbol], period="1mo", interval="1d", max_symbols=1)
        return history
