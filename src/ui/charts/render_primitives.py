"""Pure chart history and TradingView rendering primitives."""

from __future__ import annotations

import datetime as dt
import html
import json
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.utils.data_loader import _extract_symbol_history

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class ChartRenderPrimitivesMixin:
    @staticmethod
    def _normalize_chart_history(history: pd.DataFrame, symbol: str, max_rows: Optional[int] = 180) -> pd.DataFrame:
        """Return a single-symbol OHLCV frame for chart rendering."""
        if history.empty:
            return history

        if isinstance(history.columns, pd.MultiIndex):
            if symbol in history.columns.levels[0]:
                history = history[symbol].copy()
            else:
                first_symbol = history.columns.levels[0][0]
                history = history[first_symbol].copy()

        required_columns = ["Open", "High", "Low", "Close", "Volume"]
        missing = [column for column in required_columns if column not in history.columns]
        if missing:
            return pd.DataFrame()

        chart_history = history[required_columns].dropna(subset=["Close"]).copy()
        if max_rows is not None and max_rows > 0:
            chart_history = chart_history.tail(max_rows)
        chart_history.index = pd.to_datetime(chart_history.index)
        return chart_history

    @staticmethod
    def _coerce_timestamp_for_index(value: Any, index: pd.Index) -> Optional[pd.Timestamp]:
        if value is None:
            return None
        timestamp = pd.Timestamp(value)
        index_tz = getattr(index, "tz", None)
        if index_tz is not None:
            if timestamp.tzinfo is None:
                return timestamp.tz_localize(index_tz)
            return timestamp.tz_convert(index_tz)
        if timestamp.tzinfo is not None:
            return timestamp.tz_convert("UTC").tz_localize(None)
        return timestamp

    @staticmethod
    def _get_visible_time_window(history: pd.DataFrame, options: dict) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
        if history.empty:
            return None, None
        visible_bars = max(20, int(options.get("visible_bars", 90)))
        visible_end = options.get("visible_end")
        if visible_end is None:
            visible_end = len(history)
        visible_end = max(1, min(int(visible_end), len(history)))
        visible_start = max(0, visible_end - visible_bars)
        visible = history.iloc[visible_start:visible_end]
        if visible.empty:
            return None, None
        return pd.Timestamp(visible.index[0]), pd.Timestamp(visible.index[-1])

    @staticmethod
    def _merge_chart_histories(base_history: pd.DataFrame, update_history: pd.DataFrame, symbol: str) -> pd.DataFrame:
        update = _extract_symbol_history(update_history, symbol) if not update_history.empty else None
        if update is None or update.empty:
            base = _extract_symbol_history(base_history, symbol) if not base_history.empty else None
            if base is None or base.empty:
                return pd.DataFrame()
            base = base.copy()
            base.index = ChartRenderPrimitivesMixin._normalize_chart_merge_index(base.index)
            return base.sort_index()
        update = update.copy()
        update.index = ChartRenderPrimitivesMixin._normalize_chart_merge_index(update.index)

        base = _extract_symbol_history(base_history, symbol) if not base_history.empty else None
        if base is None or base.empty:
            return update.sort_index()
        base = base.copy()
        base.index = ChartRenderPrimitivesMixin._normalize_chart_merge_index(base.index)

        merged = pd.concat([base, update]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        return merged

    @staticmethod
    def _normalize_chart_merge_index(index: pd.Index) -> pd.DatetimeIndex:
        normalized = pd.DatetimeIndex(pd.to_datetime(index))
        if normalized.tz is not None:
            normalized = normalized.tz_convert(None)
        return normalized

    @staticmethod
    def _generate_message_html(title: str, message: str) -> str:
        """Generate simple local HTML for chart-panel messages."""
        return f"""
        <!DOCTYPE html>
        <html>
        <body style="margin:0;background:#1e1e1e;color:#ddd;font-family:Arial,sans-serif;">
            <div style="height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:16px;box-sizing:border-box;">
                <div>
                    <div style="font-size:16px;font-weight:600;margin-bottom:8px;">{html.escape(title)}</div>
                    <div style="font-size:13px;color:#aaa;">{html.escape(message)}</div>
                </div>
            </div>
        </body>
        </html>
        """

    @staticmethod
    def _to_tradingview_symbol(symbol: str) -> str:
        """Convert an app ticker into a TradingView widget symbol."""
        symbol = symbol.strip().upper()
        if not symbol:
            return ""
        if ":" in symbol:
            return symbol
        if symbol.endswith(".KS"):
            return f"KRX:{symbol[:-3]}"
        if symbol.endswith(".KQ"):
            return f"KOSDAQ:{symbol[:-3]}"
        return symbol

    @staticmethod
    def _tradingview_refresh_due(
        last_refresh: Optional[dt.datetime],
        now: Optional[dt.datetime] = None,
        interval_seconds: int = TRADINGVIEW_REFRESH_INTERVAL_SECONDS,
    ) -> bool:
        """Return whether a passive TradingView chart refresh is due."""
        if last_refresh is None:
            return True
        if now is None:
            now = dt.datetime.now(dt.timezone.utc)
        if last_refresh.tzinfo is None:
            last_refresh = last_refresh.replace(tzinfo=dt.timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.timezone.utc)
        return (now - last_refresh).total_seconds() >= interval_seconds

    @staticmethod
    def _generate_tradingview_widget_html(symbol: str) -> str:
        """Generate a standalone TradingView Advanced Chart widget page."""
        safe_symbol = ChartRenderPrimitivesMixin._to_tradingview_symbol(symbol)
        config = {
            "autosize": True,
            "symbol": safe_symbol,
            "interval": "D",
            "timezone": "Asia/Seoul",
            "theme": "dark",
            "style": "1",
            "locale": "en",
            "allow_symbol_change": True,
            "calendar": False,
            "support_host": "https://www.tradingview.com",
        }
        config_json = json.dumps(config)
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                html, body, .tradingview-widget-container, .tradingview-widget-container__widget {{
                    height: 100%;
                    width: 100%;
                    margin: 0;
                    background: #0f1419;
                    overflow: hidden;
                }}
            </style>
        </head>
        <body>
            <div class="tradingview-widget-container">
                <div class="tradingview-widget-container__widget"></div>
                <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
                {config_json}
                </script>
            </div>
        </body>
        </html>
        """

    @staticmethod
    def _generate_tradingview_chart_url(symbol: str) -> str:
        """Generate a first-party TradingView chart URL for a symbol."""
        safe_symbol = ChartRenderPrimitivesMixin._to_tradingview_symbol(symbol)
        return f"https://www.tradingview.com/chart/?symbol={quote(safe_symbol, safe='')}"

    @staticmethod
    def _get_js_key_condition(qt_key_str: str) -> str:
        """Convert a Qt key string like 'Ctrl+T' or 'T' into a JS event condition."""
        if not qt_key_str:
            return "false"
        parts = qt_key_str.split('+')
        conds = []
        main_key = ""
        for part in parts:
            part = part.strip().lower()
            if part == "ctrl":
                conds.append("event.ctrlKey")
            elif part == "shift":
                conds.append("event.shiftKey")
            elif part == "alt":
                conds.append("event.altKey")
            elif part == "meta":
                conds.append("event.metaKey")
            else:
                main_key = part
        
        special_keys = {
            "up": "arrowup",
            "down": "arrowdown",
            "left": "arrowleft",
            "right": "arrowright",
            "esc": "escape",
            "escape": "escape",
            "del": "delete",
            "delete": "delete",
            "backspace": "backspace"
        }
        js_key = special_keys.get(main_key, main_key)
        conds.append(f"event.key && event.key.toLowerCase() === '{js_key}'")
        return " && ".join(conds)
