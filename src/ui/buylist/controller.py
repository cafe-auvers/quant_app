"""Pure buylist monitoring and exit-policy decisions."""
from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.ui.controllers.base import WindowController


KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class BuylistController(WindowController):
    """Own pure monitoring decisions previously embedded in Qt callbacks."""

    @staticmethod
    def auto_order_blocked(item: Any) -> bool:
        return bool(
            str(getattr(item, "auto_order_block_reason", "") or "").strip()
        )

    @staticmethod
    def set_attr_if_changed(item: Any, attr: str, value: Any) -> bool:
        if getattr(item, attr, None) == value:
            return False
        setattr(item, attr, value)
        return True

    @staticmethod
    def market_session_date(
        now: Optional[dt.datetime] = None,
    ) -> dt.date:
        market_now = now or dt.datetime.now(US_MARKET_ZONE)
        if market_now.tzinfo is None:
            market_now = market_now.replace(tzinfo=KST_ZONE)
        return market_now.astimezone(US_MARKET_ZONE).date()

    @classmethod
    def market_session_date_from_value(cls, value: Any) -> Optional[dt.date]:
        if isinstance(value, dt.datetime):
            timestamp = value
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=KST_ZONE)
            return timestamp.astimezone(US_MARKET_ZONE).date()
        if isinstance(value, dt.date):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return cls.market_session_date_from_value(
                    dt.datetime.fromisoformat(text)
                )
            except ValueError:
                try:
                    return dt.date.fromisoformat(text)
                except ValueError:
                    return None
        return None

    @staticmethod
    def partial_exit_quantity(shares_held: int) -> int:
        try:
            shares = int(shares_held or 0)
        except (TypeError, ValueError):
            shares = 0
        if shares <= 0:
            return 0
        return max(1, shares // 3)

    @staticmethod
    def momentum_exit_signal(item: Any) -> str:
        try:
            price = float(getattr(item, "_latest_daily_close", 0.0) or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return ""
        ema10 = float(getattr(item, "_ema10", 0.0) or 0.0)
        ema20 = float(getattr(item, "_ema20", 0.0) or 0.0)
        if ema10 > 0 and price < ema10:
            return "10 EMA"
        if ema20 > 0 and price < ema20:
            return "20 EMA"
        return ""

    @staticmethod
    def completed_daily_close_rows(
        daily_rows: Any,
        now: Optional[dt.datetime] = None,
    ) -> List[Tuple[dt.date, float]]:
        rows: List[Tuple[dt.date, float]] = []
        for session_date, close in list(daily_rows or []):
            if isinstance(session_date, dt.datetime):
                session_date = session_date.astimezone(US_MARKET_ZONE).date()
            if not isinstance(session_date, dt.date):
                continue
            try:
                close_value = float(close)
            except (TypeError, ValueError):
                continue
            rows.append((session_date, close_value))
        if not rows:
            return []

        market_now = now or dt.datetime.now(US_MARKET_ZONE)
        if market_now.tzinfo is None:
            market_now = market_now.replace(tzinfo=KST_ZONE)
        market_now = market_now.astimezone(US_MARKET_ZONE)
        session_today = market_now.date()
        today_is_complete = market_now.time() >= US_MARKET_CLOSE_TIME
        completed = [
            row
            for row in rows
            if row[0] < session_today
            or (row[0] == session_today and today_is_complete)
        ]
        return completed or rows

    @staticmethod
    def compute_ema(prices: list, period: int) -> float:
        if len(prices) < period:
            return 0.0
        k = 2.0 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = price * k + ema * (1.0 - k)
        return float(ema)
