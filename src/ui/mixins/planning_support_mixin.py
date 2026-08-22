"""Shared planning helpers independent of the removed dedicated Watchlist tab.

The persisted ``Watchlist`` remains a passive planning stage exposed through
lightweight sidebar and symbol actions.  This mixin intentionally contains no
Watchlist table, AI analysis, or table projection code.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.risk.orb_position import (
    calculate_orb_position_values,
    is_orb_position_plan_valid,
    score_orb_position_recommendation,
)
from src.services.intraday_data_service import load_best_intraday_history
from src.utils.intraday_helpers import utcnow_naive as _utcnow_naive

US_MARKET_ZONE = ZoneInfo("America/New_York")
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class PlanningSupportMixin:
    """Neutral account, market-data, and ORB helpers shared by active views."""

    def _get_account_balance_for_env(self, env: str) -> float:
        """Return usable account equity, failing closed for configured accounts."""
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
            value = self._parse_float(self.account_size_input, 0.0)
            if value > 0:
                return value

        if hasattr(self, "manual_account_sizes"):
            value = self.manual_account_sizes.get(env, 0.0)
            if value > 0:
                return value

        return 10000.0 if env == "PROD" else 100000.0

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

    @staticmethod
    def _is_us_regular_market_open(now: Optional[dt.datetime] = None) -> bool:
        """Return whether ``now`` falls within the NYSE regular time window."""
        if now is None:
            market_now = dt.datetime.now(US_MARKET_ZONE)
        elif now.tzinfo is None:
            market_now = now.replace(tzinfo=US_MARKET_ZONE)
        else:
            market_now = now.astimezone(US_MARKET_ZONE)
        if market_now.weekday() >= 5:
            return False
        return US_MARKET_OPEN_TIME <= market_now.time() < US_MARKET_CLOSE_TIME

    @staticmethod
    def _orb_risk_cases(selected_risk_percent: float) -> List[float]:
        cases = [0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02]
        if selected_risk_percent > 0 and all(
            abs(selected_risk_percent - case) > 0.00001 for case in cases
        ):
            cases.append(selected_risk_percent)
        return sorted(cases)

    @staticmethod
    def _orb_position_plan_is_valid(
        sizing: dict, adr_percent: Optional[float]
    ) -> bool:
        return is_orb_position_plan_valid(sizing, adr_percent)

    @staticmethod
    def _score_orb_position_recommendation(
        sizing: dict, risk_percent: float
    ) -> float:
        return score_orb_position_recommendation(sizing, risk_percent)

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

    def mark_watchlist_and_dashboard_dirty(self) -> None:
        """Compatibility no-op now that the Dashboard summary is retired."""
        return None

    def _flush_dirty_watchlist_and_dashboard(self) -> None:
        """Compatibility no-op now that the Dashboard summary is retired."""
        return None

    def update_trade_prices_from_latest(self, symbol: str, latest_price: float) -> None:
        """Keep the shared current-price cache synchronized after chart fetches."""
        symbol = symbol.strip().upper()
        try:
            price = float(latest_price)
        except (TypeError, ValueError, OverflowError):
            return
        if symbol and math.isfinite(price) and price > 0:
            self.latest_intraday_prices[symbol] = price
