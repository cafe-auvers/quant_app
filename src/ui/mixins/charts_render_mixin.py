from __future__ import annotations

import datetime as dt
import html
import json
import math
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QDockWidget, QLabel,
    QPushButton, QLineEdit, QFormLayout, QTableWidget, QTableWidgetItem,
    QListWidget, QListWidgetItem, QComboBox, QCheckBox, QSpinBox, QTextEdit,
    QProgressBar, QMessageBox, QGroupBox, QHeaderView, QAbstractItemView,
    QSizePolicy, QShortcut, QDialog, QKeySequenceEdit, QScrollArea,
    QTextBrowser, QSplitter, QSlider, QDialogButtonBox, QMenu
)
from PyQt5.QtCore import Qt, QThread, QTimer, QUrl
from PyQt5.QtGui import QColor, QKeySequence
try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from src.core.position_sizer import PositionSizer
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus, OPEN_ORDER_STATUSES
from src.core.orb import calculate_orb_range, evaluate_orb_entry_signal, resample_intraday_bars
from src.core.scanner import StockScanner, ComparisonOperator, ScanRule
from src.core.watchlist import Watchlist, TradePlanManager, TradePlan, BuylistManager, BuylistItem
from src.core.trade_reviewer import TradeReviewer, TradeSetup
from src.utils.data_loader import download_price_history, get_default_universe, _extract_symbol_history
from src.utils.db_loader import (
    init_mysql_engine, load_symbol_history_from_db, load_hourly_history_from_db,
    get_latest_price_history_date, get_latest_hourly_price_history_timestamp,
    load_chart_indicators_from_db, calculate_chart_indicators,
    refresh_chart_indicators_for_symbol, save_symbol_history_to_db,
    delete_intraday_history_for_symbol,
)
from src.utils.storage import load_json, save_json
from src.api.kis_account_snapshot_dual import KisEnvironment, discover_account_profiles, load_config
from src.services.app_state import (
    SCANNER_SETUPS_FILE, SETTINGS_FILE, load_buylist_state, load_chart_drawings_state,
    load_scanner_setups_state, load_tab_options_state, load_trade_plans_state,
    load_watchlist_state, save_app_state,
)
from src.services.intraday_data_service import format_intraday_source_label, load_best_intraday_history
from src.ui.chart_bridge import ChartBridge
from src.ui.dialogs import SettingsDialog, AddFilterDialog
from src.ui.filter_catalog import (
    DEFAULT_SCANNER_SETUPS, DEFAULT_SETTINGS, DEFAULT_TAB_OPTIONS,
    FILTER_CATALOG, SCANNER_METRICS_LABELS,
)
from src.ui.workers import (
    FxRateWorker, HourlyRefreshWorker, IntradayBulkFetchWorker, IntradayFetchWorker,
    KisAccountWorker, KisOrderWorker, KisStartupAccountsWorker, OrderReconciliationWorker,
    RefreshWorker, ScannerWorker, SingleStockAiWorker, WatchlistAiWorker,
)
from src.services.order_ledger import (
    append_order, find_open_orders, has_open_order, load_order_ledger,
    save_order_ledger, update_order,
)
from src.utils.intraday_helpers import (
    extract_latest_opening_bar as _extract_latest_opening_bar,
    intraday_cache_needs_backfill,
    utcnow_naive as _utcnow_naive,
)

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)



class ChartsRenderMixin:
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
            base.index = ChartsRenderMixin._normalize_chart_merge_index(base.index)
            return base.sort_index()
        update = update.copy()
        update.index = ChartsRenderMixin._normalize_chart_merge_index(update.index)

        base = _extract_symbol_history(base_history, symbol) if not base_history.empty else None
        if base is None or base.empty:
            return update.sort_index()
        base = base.copy()
        base.index = ChartsRenderMixin._normalize_chart_merge_index(base.index)

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
        safe_symbol = ChartsRenderMixin._to_tradingview_symbol(symbol)
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
        safe_symbol = ChartsRenderMixin._to_tradingview_symbol(symbol)
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
    @staticmethod
    def _generate_tradingview_lightweight_chart_html(
        symbol: str,
        history: pd.DataFrame,
        options: Optional[dict] = None,
        drawings: Optional[List[dict]] = None,
        storage_symbol: Optional[str] = None,
        indicators: Optional[pd.DataFrame] = None,
        target_price: Optional[float] = None,
        buy_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> str:
        """Generate a stable TradingView Lightweight Charts page from local OHLCV data."""
        options = options or {}
        settings = load_json(Path("data/settings.json"), {})
        shortcuts = settings.get("shortcuts", {
            "set_target": "T",
            "draw_line": "D",
            "erase_drawing": "E",
            "full_view": "A"
        })
        target_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("set_target", "T"))
        draw_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("draw_line", "D"))
        erase_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("erase_drawing", "E"))
        full_view_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("full_view", "A"))

        chart_history = ChartsRenderMixin._normalize_chart_history(history, symbol, max_rows=260)
        if chart_history.empty:
            return ChartsRenderMixin._generate_message_html(symbol, "No chart data available.")

        candles = []
        volumes = []
        date_labels = [pd.Timestamp(item).strftime("%Y-%m-%d") for item in chart_history.index]
        uses_intraday_time = bool(options.get("timeframe", "").upper() == "1H") or len(set(date_labels)) < len(date_labels)
        time_visible = "true" if uses_intraday_time else "false"

        def chart_time_value(timestamp) -> str | int:
            timestamp = pd.Timestamp(timestamp)
            if uses_intraday_time:
                if timestamp.tzinfo is None:
                    timestamp = timestamp.tz_localize("UTC")
                else:
                    timestamp = timestamp.tz_convert("UTC")
                return int(timestamp.timestamp())
            return timestamp.strftime("%Y-%m-%d")

        chart_time_lookup = {
            pd.Timestamp(timestamp).strftime("%Y-%m-%d"): chart_time_value(timestamp)
            for timestamp in chart_history.index
        }
        first_chart_time = chart_time_value(chart_history.index[0])
        last_chart_time = chart_time_value(chart_history.index[-1])

        def drawing_time_value(value, prefer: str = "first") -> str | int:
            text = str(value)
            if uses_intraday_time and len(text) <= 10:
                day_matches = [
                    chart_time_value(timestamp)
                    for timestamp in chart_history.index
                    if pd.Timestamp(timestamp).strftime("%Y-%m-%d") == text[:10]
                ]
                if day_matches:
                    return day_matches[-1] if prefer == "last" else day_matches[0]
                date_keys = sorted(chart_time_lookup.keys())
                if date_keys and text[:10] <= date_keys[0]:
                    return first_chart_time
                if date_keys and text[:10] >= date_keys[-1]:
                    return chart_time_value(text[:10])
            return chart_time_value(value)

        def future_time_values() -> List[str | int]:
            timeframe = str(options.get("timeframe", "1D")).strip().upper()
            last_timestamp = pd.Timestamp(chart_history.index[-1])
            if uses_intraday_time:
                step = pd.Timedelta(minutes=5) if timeframe == "5M" else pd.Timedelta(hours=1)
                return [
                    chart_time_value(last_timestamp + step * offset)
                    for offset in range(1, 501)
                ]

            values = []
            current = last_timestamp
            while len(values) < 120:
                current += pd.Timedelta(days=1)
                if current.weekday() >= 5:
                    continue
                values.append(chart_time_value(current))
            return values

        for timestamp, row in chart_history.iterrows():
            time_value = chart_time_value(timestamp)
            open_price = float(row["Open"])
            high_price = float(row["High"])
            low_price = float(row["Low"])
            close_price = float(row["Close"])
            volume = 0.0 if pd.isna(row["Volume"]) else float(row["Volume"])
            candles.append({
                "time": time_value,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
            })
            volumes.append({
                "time": time_value,
                "value": volume,
                "color": "rgba(14, 203, 129, 0.35)" if close_price >= open_price else "rgba(239, 68, 68, 0.35)",
            })

        latest = chart_history.iloc[-1]
        safe_symbol = html.escape(symbol)
        header_metrics = ChartsRenderMixin._format_chart_header_metrics(chart_history, options)
        candles_json = json.dumps(candles)
        volumes_json = json.dumps(volumes)
        future_whitespace_json = json.dumps([{"time": value} for value in future_time_values()])
        latest_close = float(latest["Close"])
        drawing_lines = []
        for drawing in drawings or []:
            if not isinstance(drawing, dict) or drawing.get("type") != "line":
                continue
            try:
                start_date = str(drawing["start_date"])
                end_date = str(drawing["end_date"])
                if not uses_intraday_time:
                    start_date = start_date[:10]
                    end_date = end_date[:10]
                else:
                    start_date = drawing_time_value(start_date, prefer="first")
                    end_date = drawing_time_value(end_date, prefer="last")
                entry = {
                    "id": str(drawing.get("id", f"drawing-{len(drawing_lines)}")),
                    "start": {"time": start_date, "value": float(drawing["start_price"])},
                    "end": {"time": end_date, "value": float(drawing["end_price"])},
                }
                if drawing.get("color"):
                    entry["color"] = str(drawing["color"])
                if drawing.get("dash"):
                    entry["dash"] = list(drawing["dash"])
                if drawing.get("readonly"):
                    entry["readonly"] = True
                drawing_lines.append(entry)
            except (KeyError, TypeError, ValueError):
                continue
        drawings_json = json.dumps(drawing_lines)
        try:
            target_value = float(target_price) if target_price is not None and float(target_price) > 0 else None
        except (TypeError, ValueError):
            target_value = None
        target_price_json = json.dumps(target_value)
        try:
            buy_price_value = float(buy_price) if buy_price is not None and float(buy_price) > 0 else None
        except (TypeError, ValueError):
            buy_price_value = None
        buy_price_json = json.dumps(buy_price_value)
        try:
            stop_loss_value = float(stop_loss) if stop_loss is not None and float(stop_loss) > 0 else None
        except (TypeError, ValueError):
            stop_loss_value = None
        stop_loss_json = json.dumps(stop_loss_value)
        ema_series = {}
        if bool(options.get("show_ema", True)):
            close = chart_history["Close"].astype(float)
            for span, color in [(10, "#f59e0b"), (20, "#38bdf8"), (50, "#a78bfa")]:
                ema = close.ewm(span=span, adjust=False).mean()
                ema_series[f"EMA {span}"] = {
                    "color": color,
                    "data": [
                        {"time": chart_time_value(timestamp), "value": float(value)}
                        for timestamp, value in ema.items()
                        if not pd.isna(value)
                    ],
                }
        ema_json = json.dumps(ema_series)
        indicator_history = ChartsRenderMixin._align_chart_indicators(chart_history, indicators)
        rs_points = []
        rs_sma_points = []
        rs_markers = []
        ti65_background = []
        score_summary = "RS Score N/A"
        if bool(options.get("show_rs", True)) and not indicator_history.empty:
            indicator_lookup = indicator_history.to_dict("index")
            for timestamp in chart_history.index:
                lookup_timestamp = pd.Timestamp(timestamp)
                if lookup_timestamp.tzinfo is not None:
                    lookup_timestamp = lookup_timestamp.tz_convert(None)
                row = indicator_lookup.get(lookup_timestamp)
                if row is None:
                    continue
                time_value = chart_time_value(timestamp)
                rs_value = row.get("relative_strength")
                sma_value = row.get("rs_sma_50")
                if pd.notna(rs_value):
                    rs_points.append({"time": time_value, "value": float(rs_value)})
                    if bool(row.get("is_plus_4pct_change")):
                        rs_markers.append({"time": time_value, "position": "aboveBar", "color": "#22c55e", "shape": "circle", "text": "+4%"})
                    if bool(row.get("is_minus_4pct_change")):
                        rs_markers.append({"time": time_value, "position": "belowBar", "color": "#ef4444", "shape": "circle", "text": "-4%"})
                if pd.notna(sma_value):
                    rs_sma_points.append({"time": time_value, "value": float(sma_value)})
                if bool(row.get("is_ti65_bullish")):
                    ti65_background.append({"time": time_value, "value": 1, "color": "rgba(34, 197, 94, 0.18)"})
                elif bool(row.get("is_ti65_bearish")):
                    ti65_background.append({"time": time_value, "value": 1, "color": "rgba(239, 68, 68, 0.18)"})

            latest_scores = (
                indicator_history.dropna(subset=["rs_score_current"]).tail(1)
                if "rs_score_current" in indicator_history.columns
                else pd.DataFrame()
            )
            if not latest_scores.empty:
                latest_score = latest_scores.iloc[-1]
                def score_text(value) -> str:
                    return "N/A" if pd.isna(value) else str(int(round(float(value))))
                score_summary = (
                    f"RS Score C {score_text(latest_score.get('rs_score_current'))} | "
                    f"Y {score_text(latest_score.get('rs_score_yesterday'))} | "
                    f"W {score_text(latest_score.get('rs_score_week'))} | "
                    f"M {score_text(latest_score.get('rs_score_month'))}"
                )
        rs_points_json = json.dumps(rs_points)
        rs_sma_points_json = json.dumps(rs_sma_points)
        rs_markers_json = json.dumps(rs_markers)
        ti65_background_json = json.dumps(ti65_background)
        show_rs_panel = bool(options.get("show_rs", True))
        price_panel_height = "70%" if show_rs_panel else "100%"
        rs_panel_display = "block" if show_rs_panel else "none"
        rs_panel_height = "30%" if show_rs_panel else "0"
        rs_empty_display = "none" if rs_points else "flex"
        volume_js = ""
        if bool(options.get("show_volume", True)):
            volume_js = """
                const volumeSeries = chart.addHistogramSeries({
                    priceFormat: { type: 'volume' },
                    priceScaleId: '',
                    scaleMargins: { top: 0.82, bottom: 0 }
                });
                volumeSeries.setData(volumes);
            """
        bridge_enabled = QWebEngineView is not None and QWebChannel is not None
        bridge_script = '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>' if bridge_enabled else ""
        symbol_json = json.dumps((storage_symbol or symbol).strip().upper())
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                html, body {{
                    width: 100%;
                    height: 100%;
                    margin: 0;
                    background: #0f1419;
                    color: #d1d5db;
                    font-family: Arial, sans-serif;
                    overflow: hidden;
                }}
                #header {{
                    height: 38px;
                    display: flex;
                    align-items: center;
                    gap: 14px;
                    padding: 0 12px;
                    box-sizing: border-box;
                    border-bottom: 1px solid #263241;
                    background: #111827;
                }}
                #symbol {{
                    color: #f9fafb;
                    font-size: 15px;
                    font-weight: 600;
                }}
                #metrics {{
                    color: #9ca3af;
                    font-size: 12px;
                }}
                #chart-area {{
                    width: 100%;
                    height: calc(100% - 38px);
                }}
                #price-panel {{
                    width: 100%;
                    height: {price_panel_height};
                    position: relative;
                }}
                #chart {{
                    width: 100%;
                    height: 100%;
                }}
                #rs-chart {{
                    display: {rs_panel_display};
                    width: 100%;
                    height: {rs_panel_height};
                    border-top: 1px solid #263241;
                    position: relative;
                }}
                #rs-empty {{
                    display: {rs_empty_display};
                    align-items: center;
                    justify-content: center;
                    width: 100%;
                    height: 100%;
                    color: #9ca3af;
                    font-size: 12px;
                }}
                #drawing-overlay {{
                    position: absolute;
                    inset: 0;
                    width: 100%;
                    height: 100%;
                    z-index: 5;
                    pointer-events: none;
                }}
            </style>
            {bridge_script}
        </head>
        <body>
            <div id="header">
                <div id="symbol">{safe_symbol}</div>
                <div id="metrics">{html.escape(header_metrics)} | {html.escape(str(options.get("timeframe", "1D")))} | {html.escape(str(options.get("data_latest_text", "")))} | {html.escape(score_summary)} | TradingView Lightweight Charts</div>
            </div>
            <div id="chart-area">
                <div id="price-panel">
                    <div id="chart"></div>
                    <canvas id="drawing-overlay"></canvas>
                </div>
                <div id="rs-chart"><div id="rs-empty">RS/TI65 data unavailable for this timeframe.</div></div>
            </div>
            <script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
            <script>
                const candles = {candles_json};
                const volumes = {volumes_json};
                const futureWhitespace = {future_whitespace_json};
                const emaSeries = {ema_json};
                const rsPoints = {rs_points_json};
                const rsSmaPoints = {rs_sma_points_json};
                const rsMarkers = {rs_markers_json};
                const ti65Background = {ti65_background_json};
                const savedDrawings = {drawings_json};
                const symbolName = {symbol_json};
                const container = document.getElementById('chart');
                const rsContainer = document.getElementById('rs-chart');
                let chartBridge = null;
                let drawingMode = false;
                let eraseMode = false;
                let editMode = false;
                let lineToolMode = false;
                let targetMode = false;
                let drawingStart = null;
                let drawingPreview = null;
                const drawingSeries = new Map();
                let targetPrice = {target_price_json};
                let targetLine = null;
                const buyPrice = {buy_price_json};
                const stopLossPrice = {stop_loss_json};
                if (typeof QWebChannel !== "undefined" && typeof qt !== "undefined") {{
                    new QWebChannel(qt.webChannelTransport, function(channel) {{
                        chartBridge = channel.objects.chartBridge;
                    }});
                }}
                const chart = LightweightCharts.createChart(container, {{
                    autoSize: true,
                    layout: {{
                        background: {{ type: 'solid', color: '#0f1419' }},
                        textColor: '#9ca3af'
                    }},
                    grid: {{
                        vertLines: {{ color: '#1f2937' }},
                        horzLines: {{ color: '#1f2937' }}
                    }},
                    rightPriceScale: {{ borderColor: '#374151' }},
                    localization: {{
                        timeFormatter: (time) => {{
                            if (typeof time !== 'number') return String(time);
                            const d = new Date((time + 32400) * 1000);
                            const yyyy = d.getUTCFullYear(), mm = String(d.getUTCMonth()+1).padStart(2,'0'), dd = String(d.getUTCDate()).padStart(2,'0');
                            const h = String(d.getUTCHours()).padStart(2,'0'), m = String(d.getUTCMinutes()).padStart(2,'0');
                            return `${{yyyy}}-${{mm}}-${{dd}} ${{h}}:${{m}} KST`;
                        }}
                    }},
                    timeScale: {{
                        borderColor: '#374151',
                        timeVisible: {time_visible},
                        fixLeftEdge: false,
                        fixRightEdge: false,
                        rightOffset: 40,
                        rightBarStaysOnScroll: false,
                        tickMarkFormatter: (time, tickMarkType) => {{
                            if (typeof time !== 'number') return time.year + '-' + String(time.month).padStart(2,'0') + '-' + String(time.day).padStart(2,'0');
                            const d = new Date((time + 32400) * 1000);
                            const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                            if (tickMarkType === 0) return String(d.getUTCFullYear());
                            if (tickMarkType === 1) return months[d.getUTCMonth()];
                            if (tickMarkType === 2) return String(d.getUTCDate());
                            return String(d.getUTCHours()).padStart(2,'0') + ':' + String(d.getUTCMinutes()).padStart(2,'0');
                        }}
                    }},
                    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }}
                }});
                const candleSeries = chart.addCandlestickSeries({{
                    upColor: '#0ecb81',
                    downColor: '#ef4444',
                    borderUpColor: '#0ecb81',
                    borderDownColor: '#ef4444',
                    wickUpColor: '#0ecb81',
                    wickDownColor: '#ef4444'
                }});
                candleSeries.setData(candles.concat(futureWhitespace));
                function formatPrice(value) {{
                    return Number(value).toFixed(2);
                }}

                function renderTargetLine(price) {{
                    if (targetLine) {{
                        candleSeries.removePriceLine(targetLine);
                        targetLine = null;
                    }}
                    if (price === null || price === undefined || !Number.isFinite(Number(price)) || Number(price) <= 0) return;
                    targetPrice = Number(price);
                    targetLine = candleSeries.createPriceLine({{
                        price: targetPrice,
                        color: '#f97316',
                        lineWidth: 2,
                        lineStyle: LightweightCharts.LineStyle.Dashed,
                        axisLabelVisible: true,
                        title: `Breakout ${{formatPrice(targetPrice)}}`
                    }});
                }}

                renderTargetLine(targetPrice);

                function renderBuyLine(price) {{
                    if (price === null || price === undefined || !Number.isFinite(Number(price)) || Number(price) <= 0) return;
                    candleSeries.createPriceLine({{
                        price: Number(price),
                        color: '#0ecb81',
                        lineWidth: 1,
                        lineStyle: LightweightCharts.LineStyle.Solid,
                        axisLabelVisible: true,
                        title: `Buy ${{formatPrice(price)}}`
                    }});
                }}

                function renderStopLossLine(price) {{
                    if (price === null || price === undefined || !Number.isFinite(Number(price)) || Number(price) <= 0) return;
                    candleSeries.createPriceLine({{
                        price: Number(price),
                        color: '#ef4444',
                        lineWidth: 1,
                        lineStyle: LightweightCharts.LineStyle.Dashed,
                        axisLabelVisible: true,
                        title: `Stop ${{formatPrice(price)}}`
                    }});
                }}

                renderBuyLine(buyPrice);
                renderStopLossLine(stopLossPrice);
                {volume_js}
                Object.entries(emaSeries).forEach(([title, series]) => {{
                    const lineSeries = chart.addLineSeries({{
                        color: series.color,
                        lineWidth: 2,
                        priceLineVisible: false,
                        baseLineVisible: false,
                        lastValueVisible: false
                    }});
                    lineSeries.setData(series.data);
                }});
                let rsChart = null;
                if (rsPoints.length > 0 && rsContainer) {{
                    rsChart = LightweightCharts.createChart(rsContainer, {{
                        autoSize: true,
                        layout: {{
                            background: {{ type: 'solid', color: '#0f1419' }},
                            textColor: '#9ca3af'
                        }},
                        grid: {{
                            vertLines: {{ color: '#1f2937' }},
                            horzLines: {{ color: '#1f2937' }}
                        }},
                        rightPriceScale: {{ borderColor: '#374151' }},
                        localization: {{
                            timeFormatter: (time) => {{
                                if (typeof time !== 'number') return String(time);
                                const d = new Date((time + 32400) * 1000);
                                const yyyy = d.getUTCFullYear(), mm = String(d.getUTCMonth()+1).padStart(2,'0'), dd = String(d.getUTCDate()).padStart(2,'0');
                                const h = String(d.getUTCHours()).padStart(2,'0'), m = String(d.getUTCMinutes()).padStart(2,'0');
                                return `${{yyyy}}-${{mm}}-${{dd}} ${{h}}:${{m}} KST`;
                            }}
                        }},
                        timeScale: {{
                            borderColor: '#374151',
                        timeVisible: {time_visible},
                        fixLeftEdge: false,
                        fixRightEdge: false,
                            rightOffset: 40,
                            rightBarStaysOnScroll: false,
                            tickMarkFormatter: (time, tickMarkType) => {{
                                if (typeof time !== 'number') return time.year + '-' + String(time.month).padStart(2,'0') + '-' + String(time.day).padStart(2,'0');
                                const d = new Date((time + 32400) * 1000);
                                const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                                if (tickMarkType === 0) return String(d.getUTCFullYear());
                                if (tickMarkType === 1) return months[d.getUTCMonth()];
                                if (tickMarkType === 2) return String(d.getUTCDate());
                                return String(d.getUTCHours()).padStart(2,'0') + ':' + String(d.getUTCMinutes()).padStart(2,'0');
                            }}
                        }},
                        crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }}
                    }});
                    const rsBackground = rsChart.addHistogramSeries({{
                        priceFormat: {{ type: 'volume' }},
                        lastValueVisible: false,
                        priceLineVisible: false,
                        priceScaleId: ''
                    }});
                    rsBackground.priceScale().applyOptions({{
                        scaleMargins: {{ top: 0, bottom: 0 }}
                    }});
                    rsBackground.setData(ti65Background.concat(futureWhitespace));
                    const rsSeries = rsChart.addLineSeries({{
                        title: 'RS vs SPY',
                        color: '#22c55e',
                        lineWidth: 2,
                        priceLineVisible: false
                    }});
                    rsSeries.setData(rsPoints.concat(futureWhitespace));
                    rsSeries.setMarkers(rsMarkers);
                    const rsSmaSeries = rsChart.addLineSeries({{
                        title: 'RS SMA 50',
                        color: '#e5e7eb',
                        lineWidth: 1,
                        priceLineVisible: false
                    }});
                    rsSmaSeries.setData(rsSmaPoints.concat(futureWhitespace));
                    let syncingRange = false;
                    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {{
                        if (syncingRange || !range) return;
                        syncingRange = true;
                        rsChart.timeScale().setVisibleLogicalRange(range);
                        syncingRange = false;
                    }});
                    rsChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {{
                        if (syncingRange || !range) return;
                        syncingRange = true;
                        chart.timeScale().setVisibleLogicalRange(range);
                        syncingRange = false;
                    }});
                }}

                function normalizeTimeForSave(time) {{
                    if (typeof time === 'string') return time;
                    if (typeof time === 'number') return new Date(time * 1000).toISOString().slice(0, 19).replace('T', ' ');
                    if (time && typeof time === 'object' && 'year' in time) {{
                        return `${{time.year}}-${{String(time.month).padStart(2, '0')}}-${{String(time.day).padStart(2, '0')}}`;
                    }}
                    return String(time || '');
                }}

                const overlay = document.getElementById('drawing-overlay');
                const overlayContext = overlay.getContext('2d');
                let activeEdit = null;
                let pointerPreview = null;
                let selectedDrawingId = null;

                function resizeOverlay() {{
                    const rect = overlay.getBoundingClientRect();
                    const ratio = window.devicePixelRatio || 1;
                    overlay.width = Math.max(1, Math.floor(rect.width * ratio));
                    overlay.height = Math.max(1, Math.floor(rect.height * ratio));
                    overlayContext.setTransform(ratio, 0, 0, ratio, 0, 0);
                    renderDrawings();
                }}

                function drawingToScreen(drawing) {{
                    const x1 = chart.timeScale().timeToCoordinate(drawing.start.time);
                    const x2 = chart.timeScale().timeToCoordinate(drawing.end.time);
                    const y1 = candleSeries.priceToCoordinate(Number(drawing.start.value));
                    const y2 = candleSeries.priceToCoordinate(Number(drawing.end.value));
                    if (x1 == null || x2 == null || y1 == null || y2 == null) return null;
                    return {{ x1, y1, x2, y2 }};
                }}

                function renderDrawings() {{
                    const rect = overlay.getBoundingClientRect();
                    overlayContext.clearRect(0, 0, rect.width, rect.height);
                    drawingSeries.forEach((drawing) => {{
                        const points = drawingToScreen(drawing);
                        if (!points) return;
                        overlayContext.save();
                        const selected = drawing.id === selectedDrawingId && (editMode || drawingMode || eraseMode || lineToolMode);
                        overlayContext.strokeStyle = selected ? '#f97316' : (drawing.color || '#60a5fa');
                        overlayContext.lineWidth = selected ? 3 : (drawing.readonly ? 1.5 : 2);
                        if (drawing.dash) {{ overlayContext.setLineDash(drawing.dash); }} else {{ overlayContext.setLineDash([]); }}
                        overlayContext.beginPath();
                        overlayContext.moveTo(points.x1, points.y1);
                        overlayContext.lineTo(points.x2, points.y2);
                        overlayContext.stroke();
                        if (selected || editMode || drawingMode || eraseMode) {{
                            overlayContext.fillStyle = '#0f172a';
                            overlayContext.strokeStyle = '#bfdbfe';
                            overlayContext.lineWidth = 2;
                            for (const point of [{{ x: points.x1, y: points.y1 }}, {{ x: points.x2, y: points.y2 }}]) {{
                                overlayContext.beginPath();
                                overlayContext.arc(point.x, point.y, 5, 0, Math.PI * 2);
                                overlayContext.fill();
                                overlayContext.stroke();
                            }}
                        }}
                        overlayContext.restore();
                    }});
                    if (drawingMode && drawingStart && pointerPreview) {{
                        const startX = chart.timeScale().timeToCoordinate(drawingStart.time);
                        const startY = candleSeries.priceToCoordinate(Number(drawingStart.value));
                        if (startX != null && startY != null) {{
                            overlayContext.save();
                            overlayContext.strokeStyle = '#93c5fd';
                            overlayContext.lineWidth = 2;
                            overlayContext.setLineDash([5, 4]);
                            overlayContext.beginPath();
                            overlayContext.moveTo(startX, startY);
                            overlayContext.lineTo(pointerPreview.x, pointerPreview.y);
                            overlayContext.stroke();
                            overlayContext.restore();
                        }}
                    }}
                }}

                function addDrawingLine(drawing, persist) {{
                    if (!drawing || !drawing.start || !drawing.end) return;
                    const normalized = {{
                        id: drawing.id,
                        start: {{ time: drawing.start.time, value: Number(drawing.start.value) }},
                        end: {{ time: drawing.end.time, value: Number(drawing.end.value) }},
                        color: drawing.color || null,
                        dash: drawing.dash || null,
                        readonly: drawing.readonly || false,
                    }};
                    if (!Number.isFinite(normalized.start.value) || !Number.isFinite(normalized.end.value)) return;
                    drawingSeries.set(normalized.id, normalized);
                    if (persist) {{
                        selectedDrawingId = normalized.id;
                    }}
                    renderDrawings();
                    if (persist && chartBridge && chartBridge.saveChartDrawing) {{
                        chartBridge.saveChartDrawing(symbolName, JSON.stringify({{
                            id: normalized.id,
                            type: 'line',
                            start_date: normalizeTimeForSave(normalized.start.time),
                            start_price: normalized.start.value,
                            end_date: normalizeTimeForSave(normalized.end.time),
                            end_price: normalized.end.value
                        }}));
                    }}
                }}

                function updateDrawingLine(drawing) {{
                    drawingSeries.set(drawing.id, drawing);
                    selectedDrawingId = drawing.id;
                    renderDrawings();
                    if (chartBridge && chartBridge.updateChartDrawing) {{
                        chartBridge.updateChartDrawing(symbolName, JSON.stringify({{
                            id: drawing.id,
                            type: 'line',
                            start_date: normalizeTimeForSave(drawing.start.time),
                            start_price: Number(drawing.start.value),
                            end_date: normalizeTimeForSave(drawing.end.time),
                            end_price: Number(drawing.end.value)
                        }}));
                    }}
                }}

                function removeDrawingLine(drawingId, persist) {{
                    if (!drawingSeries.has(drawingId)) return;
                    if (drawingSeries.get(drawingId)?.readonly) return;
                    drawingSeries.delete(drawingId);
                    if (selectedDrawingId === drawingId) selectedDrawingId = null;
                    renderDrawings();
                    if (persist && chartBridge && chartBridge.deleteChartDrawing) {{
                        chartBridge.deleteChartDrawing(symbolName, drawingId);
                    }}
                }}

                function pointDistanceToSegment(point, start, end) {{
                    const dx = end.x - start.x;
                    const dy = end.y - start.y;
                    if (dx === 0 && dy === 0) {{
                        return Math.hypot(point.x - start.x, point.y - start.y);
                    }}
                    const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy)));
                    const projection = {{ x: start.x + t * dx, y: start.y + t * dy }};
                    return Math.hypot(point.x - projection.x, point.y - projection.y);
                }}

                function hitTestDrawing(point) {{
                    let best = null;
                    drawingSeries.forEach((drawing, drawingId) => {{
                        if (drawing.readonly) return;
                        const screen = drawingToScreen(drawing);
                        if (!screen) return;
                        const startDistance = Math.hypot(point.x - screen.x1, point.y - screen.y1);
                        const endDistance = Math.hypot(point.x - screen.x2, point.y - screen.y2);
                        const lineDistance = pointDistanceToSegment(point, {{ x: screen.x1, y: screen.y1 }}, {{ x: screen.x2, y: screen.y2 }});
                        const candidates = [
                            {{ drawingId, part: 'start', distance: startDistance }},
                            {{ drawingId, part: 'end', distance: endDistance }},
                            {{ drawingId, part: 'line', distance: lineDistance }}
                        ];
                        candidates.forEach((candidate) => {{
                            const limit = candidate.part === 'line' ? 10 : 12;
                            if (candidate.distance <= limit && (!best || candidate.distance < best.distance)) {{
                                best = candidate;
                            }}
                        }});
                    }});
                    return best;
                }}

                function eventPoint(event) {{
                    const rect = overlay.getBoundingClientRect();
                    return {{ x: event.clientX - rect.left, y: event.clientY - rect.top }};
                }}

                function chartPointFromEvent(event) {{
                    const point = eventPoint(event);
                    const time = chart.timeScale().coordinateToTime(point.x);
                    const price = candleSeries.coordinateToPrice(point.y);
                    if (time == null || price == null || !Number.isFinite(Number(price))) return null;
                    return {{ time, value: Number(price), x: point.x, y: point.y }};
                }}

                savedDrawings.forEach((drawing) => addDrawingLine(drawing, false));
                setTimeout(resizeOverlay, 0);
                window.addEventListener('resize', resizeOverlay);
                chart.timeScale().subscribeVisibleTimeRangeChange(renderDrawings);

                function setOverlayInteractive(enabled) {{
                    overlay.style.pointerEvents = enabled ? 'auto' : 'none';
                    if (!enabled) overlay.style.cursor = 'default';
                }}

                window.enableTargetMode = function() {{
                    targetMode = true;
                    drawingMode = false;
                    eraseMode = false;
                    editMode = false;
                    lineToolMode = false;
                    drawingStart = null;
                    pointerPreview = null;
                    selectedDrawingId = null;
                    setOverlayInteractive(false);
                    renderDrawings();
                }};
                window.enableLineToolMode = function() {{
                    lineToolMode = true;
                    drawingMode = true;
                    editMode = true;
                    eraseMode = false;
                    targetMode = false;
                    drawingStart = null;
                    pointerPreview = null;
                    setOverlayInteractive(true);
                    renderDrawings();
                }};
                window.disableLineToolMode = function() {{
                    lineToolMode = false;
                    drawingMode = false;
                    editMode = false;
                    eraseMode = false;
                    targetMode = false;
                    drawingStart = null;
                    pointerPreview = null;
                    selectedDrawingId = null;
                    setOverlayInteractive(false);
                    renderDrawings();
                }};
                window.enableDrawingMode = function() {{
                    window.enableLineToolMode();
                }};
                window.enableEraseMode = function() {{
                    eraseMode = true;
                    drawingMode = false;
                    editMode = false;
                    lineToolMode = false;
                    targetMode = false;
                    drawingStart = null;
                    pointerPreview = null;
                    selectedDrawingId = null;
                    setOverlayInteractive(true);
                    renderDrawings();
                }};
                window.enableEditMode = function() {{
                    editMode = true;
                    drawingMode = false;
                    eraseMode = false;
                    lineToolMode = false;
                    targetMode = false;
                    drawingStart = null;
                    pointerPreview = null;
                    setOverlayInteractive(true);
                    renderDrawings();
                }};
                window.clearTargetPrice = function() {{
                    targetPrice = null;
                    if (targetLine) {{
                        candleSeries.removePriceLine(targetLine);
                        targetLine = null;
                    }}
                    if (chartBridge && chartBridge.clearChartTarget) {{
                        chartBridge.clearChartTarget(symbolName);
                    }}
                }};
                window.clearAllDrawings = function() {{
                    drawingSeries.clear();
                    renderDrawings();
                }};
                window.resetFullView = function() {{
                    const futureBars = Math.min(40, futureWhitespace.length);
                    const visibleBars = Math.min(120, candles.length + futureBars);
                    const visibleTo = Math.max(0, candles.length - 1 + futureBars);
                    const range = {{
                        from: Math.max(0, visibleTo - visibleBars),
                        to: visibleTo
                    }};
                    chart.timeScale().setVisibleLogicalRange(range);
                    if (rsChart) rsChart.timeScale().setVisibleLogicalRange(range);
                    renderDrawings();
                }};

                document.addEventListener('keydown', (event) => {{
                    if ({target_cond_js}) {{
                        event.preventDefault();
                        window.enableTargetMode();
                        return;
                    }}
                    if ({draw_cond_js}) {{
                        event.preventDefault();
                        window.enableDrawingMode();
                        return;
                    }}
                    if ({erase_cond_js}) {{
                        event.preventDefault();
                        window.enableEraseMode();
                        return;
                    }}
                    if ({full_view_cond_js}) {{
                        event.preventDefault();
                        window.resetFullView();
                        return;
                    }}
                    if (event.key === 'Escape') {{
                        targetMode = false;
                        drawingMode = false;
                        eraseMode = false;
                        editMode = false;
                        lineToolMode = false;
                        drawingStart = null;
                        pointerPreview = null;
                        selectedDrawingId = null;
                        setOverlayInteractive(false);
                        renderDrawings();
                        return;
                    }}
                    if ((event.key === 'Delete' || event.key === 'Backspace') && selectedDrawingId) {{
                        event.preventDefault();
                        removeDrawingLine(selectedDrawingId, true);
                        return;
                    }}
                }});

                overlay.addEventListener('mousedown', (event) => {{
                    const point = eventPoint(event);
                    const chartPoint = chartPointFromEvent(event);
                    if (eraseMode) {{
                        const hit = hitTestDrawing(point);
                        if (hit) {{
                            removeDrawingLine(hit.drawingId, true);
                            eraseMode = false;
                            setOverlayInteractive(false);
                        }}
                        return;
                    }}
                    if (drawingMode) {{
                        const hit = hitTestDrawing(point);
                        if (!hit) {{
                            if (selectedDrawingId !== null) {{
                                selectedDrawingId = null;
                                renderDrawings();
                            }}
                            return;
                        }}
                        if (!chartPoint) return;
                        event.preventDefault();
                        drawingStart = null;
                        pointerPreview = null;
                        selectedDrawingId = hit.drawingId;
                        const drawing = drawingSeries.get(hit.drawingId);
                        activeEdit = {{
                            drawingId: hit.drawingId,
                            part: hit.part,
                            original: JSON.parse(JSON.stringify(drawing)),
                            startPoint: chartPoint
                        }};
                        overlay.style.cursor = hit.part === 'line' ? 'move' : 'crosshair';
                        return;
                    }}
                    if (!editMode) return;
                    const hit = hitTestDrawing(point);
                    if (!hit || !chartPoint) return;
                    event.preventDefault();
                    selectedDrawingId = hit.drawingId;
                    const drawing = drawingSeries.get(hit.drawingId);
                    activeEdit = {{
                        drawingId: hit.drawingId,
                        part: hit.part,
                        original: JSON.parse(JSON.stringify(drawing)),
                        startPoint: chartPoint
                    }};
                    overlay.style.cursor = hit.part === 'line' ? 'move' : 'crosshair';
                }});

                overlay.addEventListener('mousemove', (event) => {{
                    const point = eventPoint(event);
                    const chartPoint = chartPointFromEvent(event);
                    if (drawingMode && drawingStart) {{
                        pointerPreview = point;
                        renderDrawings();
                    }}
                    if (!activeEdit || !chartPoint) {{
                        const hit = editMode || eraseMode ? hitTestDrawing(point) : null;
                        overlay.style.cursor = hit ? (hit.part === 'line' ? 'move' : 'crosshair') : drawingMode ? 'crosshair' : 'default';
                        return;
                    }}
                    event.preventDefault();
                    const next = JSON.parse(JSON.stringify(activeEdit.original));
                    if (activeEdit.part === 'start') {{
                        next.start = {{ time: chartPoint.time, value: chartPoint.value }};
                    }} else if (activeEdit.part === 'end') {{
                        next.end = {{ time: chartPoint.time, value: chartPoint.value }};
                    }} else {{
                        const startPriceDelta = Number(chartPoint.value) - Number(activeEdit.startPoint.value);
                        next.start.value = Number(activeEdit.original.start.value) + startPriceDelta;
                        next.end.value = Number(activeEdit.original.end.value) + startPriceDelta;
                    }}
                    drawingSeries.set(activeEdit.drawingId, next);
                    renderDrawings();
                }});

                overlay.addEventListener('mouseup', (event) => {{
                    if (!activeEdit) return;
                    const drawing = drawingSeries.get(activeEdit.drawingId);
                    activeEdit = null;
                    overlay.style.cursor = 'default';
                    if (drawing) updateDrawingLine(drawing);
                }});

                overlay.addEventListener('mouseleave', () => {{
                    if (!activeEdit) return;
                    const drawing = drawingSeries.get(activeEdit.drawingId);
                    activeEdit = null;
                    overlay.style.cursor = 'default';
                    if (drawing) updateDrawingLine(drawing);
                }});

                overlay.addEventListener('click', (event) => {{
                    if (!drawingMode) return;
                    const hit = hitTestDrawing(eventPoint(event));
                    if (hit || activeEdit) return;
                    const point = chartPointFromEvent(event);
                    if (!point) return;
                    event.preventDefault();
                    if (!drawingStart) {{
                        drawingStart = {{ time: point.time, value: point.value }};
                        pointerPreview = {{ x: point.x, y: point.y }};
                        renderDrawings();
                        return;
                    }}
                    const drawing = {{
                        id: `line-${{Date.now()}}-${{Math.round(Math.random() * 100000)}}`,
                        start: drawingStart,
                        end: {{ time: point.time, value: point.value }}
                    }};
                    addDrawingLine(drawing, true);
                    drawingMode = lineToolMode;
                    editMode = lineToolMode;
                    drawingStart = null;
                    pointerPreview = null;
                    setOverlayInteractive(lineToolMode);
                    renderDrawings();
                }});
                chart.subscribeClick((param) => {{
                    if (!targetMode || !param || !param.point) return;
                    const price = candleSeries.coordinateToPrice(param.point.y);
                    if (price === null || price === undefined || !Number.isFinite(Number(price)) || Number(price) <= 0) return;
                    renderTargetLine(price);
                    targetMode = false;
                    if (chartBridge && chartBridge.setChartTarget) {{
                        chartBridge.setChartTarget(symbolName, Number(price));
                    }}
                }});
                window.resetFullView();
            </script>
        </body>
        </html>
        """
    @staticmethod
    def _generate_local_chart_html(
        symbol: str,
        history: pd.DataFrame,
        compact: bool = False,
        indicators: Optional[pd.DataFrame] = None,
        options: Optional[dict] = None,
        target_price: Optional[float] = None,
        drawings: Optional[List[dict]] = None,
    ) -> str:
        """Generate a local SVG chart from OHLCV data."""
        options = ChartsRenderMixin._normalize_chart_options(options)
        settings = load_json(Path("data/settings.json"), {})
        shortcuts = settings.get("shortcuts", {
            "set_target": "T",
            "draw_line": "D",
            "erase_drawing": "E",
            "full_view": "A",
            "prev_symbol": "Up",
            "next_symbol": "Down",
            "pan_left": "Left",
            "pan_right": "Right"
        })
        target_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("set_target", "T"))
        draw_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("draw_line", "D"))
        erase_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("erase_drawing", "E"))
        full_view_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("full_view", "A"))
        prev_symbol_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("prev_symbol", "Up"))
        next_symbol_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("next_symbol", "Down"))
        pan_left_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("pan_left", "Left"))
        pan_right_cond_js = ChartsRenderMixin._get_js_key_condition(shortcuts.get("pan_right", "Right"))
        chart_history = ChartsRenderMixin._normalize_chart_history(
            history,
            symbol,
            max_rows=options.get("max_history_bars", 180),
        )
        visible_start_time = ChartsRenderMixin._coerce_timestamp_for_index(options.get("visible_start_time"), chart_history.index)
        visible_end_time = ChartsRenderMixin._coerce_timestamp_for_index(options.get("visible_end_time"), chart_history.index)
        if visible_start_time is not None or visible_end_time is not None:
            filtered_history = chart_history
            if visible_start_time is not None:
                filtered_history = filtered_history[filtered_history.index >= visible_start_time]
            if visible_end_time is not None:
                end_time = visible_end_time
                if bool(options.get("visible_end_time_is_date")) or not bool(options.get("intraday_chart")):
                    end_time = end_time + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
                filtered_history = filtered_history[filtered_history.index <= end_time]
            if not filtered_history.empty:
                chart_history = filtered_history
                options["visible_bars"] = len(chart_history)
                options["visible_end"] = len(chart_history)
        safe_symbol = html.escape(symbol)
        if chart_history.empty:
            return ChartsRenderMixin._generate_message_html(symbol, "No chart data available.")

        full_chart_history = chart_history.copy()
        visible_bars = int(options.get("visible_bars", 90))
        future_padding_bars = min(int(options.get("future_padding_bars", 30)), max(0, visible_bars - 5))
        visible_end = options.get("visible_end")
        if visible_end is None:
            visible_end = len(full_chart_history)
        max_visible_end = len(full_chart_history) + max(0, future_padding_bars)
        visible_end = max(1, min(int(visible_end), max_visible_end))
        visible_start = max(0, visible_end - max(20, visible_bars))
        data_end = min(visible_end, len(full_chart_history))
        chart_history = full_chart_history.iloc[visible_start:data_end].copy()
        data_slot_offset = 0
        visible_state = {
            "total": len(full_chart_history),
            "maxEnd": max_visible_end,
            "start": visible_start,
            "end": visible_end,
            "visibleBars": max(1, visible_end - visible_start),
            "dataEnd": data_end,
        }

        high = chart_history["High"].astype(float)
        low = chart_history["Low"].astype(float)
        close = chart_history["Close"].astype(float)
        volume = chart_history["Volume"].fillna(0).astype(float)
        date_labels = [item.strftime("%Y-%m-%d") for item in chart_history.index]
        uses_intraday_keys = bool(options.get("intraday_chart")) or len(set(date_labels)) < len(date_labels)
        if uses_intraday_keys:
            def _kst(ts: pd.Timestamp) -> pd.Timestamp:
                return (ts.tz_localize("UTC") if ts.tzinfo is None else ts).tz_convert(KST_ZONE)
            dates = [_kst(item).strftime("%Y-%m-%d %H:%M:%S") for item in chart_history.index]
            full_dates = [_kst(item).strftime("%Y-%m-%d %H:%M:%S") for item in full_chart_history.index]
        else:
            dates = [item.strftime("%Y-%m-%d") for item in chart_history.index]
            full_dates = [item.strftime("%Y-%m-%d") for item in full_chart_history.index]
        future_drawing_dates = [] if uses_intraday_keys else ChartsRenderMixin._future_weekday_dates(full_chart_history.index[-1], days=5)
        full_drawing_dates = full_dates + future_drawing_dates
        closes = close.tolist()
        volumes = volume.tolist()
        indicator_history = (
            ChartsRenderMixin._align_chart_indicators(chart_history, indicators)
            if not compact and options["show_rs"]
            else pd.DataFrame()
        )
        has_indicators = not indicator_history.empty
        width = 1180
        height = 360 if compact else (840 if has_indicators else 620)
        left = 62 if compact else 72
        right = 28 if compact else 190
        top = 38 if compact else 46
        chart_bottom = 230 if compact else 420
        volume_top = 254 if compact else 458
        bottom = 322 if compact else (602 if has_indicators else 580)
        if not compact and not options["show_volume"]:
            chart_bottom = 602 if has_indicators else 560
            volume_top = chart_bottom
            bottom = chart_bottom
        plot_width = width - left - right
        price_height = chart_bottom - top
        volume_height = bottom - volume_top
        target_label_x = (width - 150) if compact else (width - right + 10)
        target_label_width = 132
        target_delete_x = target_label_x + target_label_width + 8
        target_delete_text_x = target_delete_x + 11
        target_text_x = target_label_x + target_label_width / 2
        crosshair_bottom = 790 if has_indicators else bottom
        hover_box_height = 122 if has_indicators else 106

        ema_10 = close.ewm(span=10, adjust=False).mean()
        ema_20 = close.ewm(span=20, adjust=False).mean()
        ema_50 = close.ewm(span=50, adjust=False).mean()
        overlay_series = [high, low]
        if options["show_ema"]:
            overlay_series.extend([ema_10, ema_20, ema_50])
        overlay_values = pd.concat(overlay_series).dropna().astype(float).tolist()
        min_price = min(overlay_values)
        max_price = max(overlay_values)
        if min_price == max_price:
            min_price *= 0.98
            max_price *= 1.02
        padding = (max_price - min_price) * 0.08
        min_price -= padding
        max_price += padding
        max_volume = max(volumes) if volumes else 0

        def x_for(index: int) -> float:
            if visible_state["visibleBars"] <= 1:
                return left + plot_width
            return left + (index / (visible_state["visibleBars"] - 1)) * plot_width

        def y_for(price: float) -> float:
            return chart_bottom - ((price - min_price) / (max_price - min_price)) * price_height

        def line_points(series: pd.Series) -> str:
            return " ".join(
                f"{x_for(data_slot_offset + index):.1f},{y_for(float(value)):.1f}"
                for index, value in enumerate(series)
                if pd.notna(value)
            )

        grid_lines = []
        price_labels = []
        for step in range(5):
            y = top + (step / 4) * price_height
            price = max_price - (step / 4) * (max_price - min_price)
            grid_lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#333" />')
            price_labels.append(f'<text x="12" y="{y + 4:.1f}" fill="#aaa" font-size="12">{price:.2f}</text>')

        candle_elements = []
        candle_width = max(3.0, plot_width / max(len(closes), 1) * 0.58)
        for index, row in chart_history.iterrows():
            x = x_for(data_slot_offset + len(candle_elements))
            open_value = float(row["Open"])
            high_value = float(row["High"])
            low_value = float(row["Low"])
            close_value = float(row["Close"])
            up_day = close_value >= open_value
            candle_color = "#22c55e" if up_day else "#ef4444"
            body_top = y_for(max(open_value, close_value))
            body_bottom = y_for(min(open_value, close_value))
            body_height = max(1.2, body_bottom - body_top)
            candle_elements.append(
                f'<line x1="{x:.1f}" y1="{y_for(high_value):.1f}" x2="{x:.1f}" y2="{y_for(low_value):.1f}" stroke="{candle_color}" stroke-width="1.4" />'
                f'<rect x="{x - candle_width / 2:.1f}" y="{body_top:.1f}" width="{candle_width:.1f}" height="{body_height:.1f}" fill="{candle_color}" opacity="0.9" />'
            )

        volume_bars = []
        bar_width = max(2.0, plot_width / max(len(volumes), 1) * 0.55)
        if options["show_volume"]:
            for index, raw_volume in enumerate(volumes):
                bar_height = 0 if max_volume <= 0 else (raw_volume / max_volume) * volume_height
                x = x_for(data_slot_offset + index) - bar_width / 2
                y = bottom - bar_height
                volume_bars.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="#4a90a4" opacity="0.65" />'
                )

        first_close = closes[0]
        last_close = closes[-1]
        change = last_close - first_close
        change_percent = (change / first_close * 100) if first_close else 0.0
        line_color = "#4ade80" if change >= 0 else "#f87171"
        header_metrics = ChartsRenderMixin._format_chart_header_metrics(chart_history, options)
        label_indices = sorted({0, len(dates) // 2, len(dates) - 1})
        date_labels = [
            f'<text x="{x_for(data_slot_offset + index):.1f}" y="{height - 18}" fill="#aaa" font-size="12" text-anchor="middle">{html.escape(dates[index])}</text>'
            for index in label_indices
        ]
        chart_points = [
            {
                "slot": data_slot_offset + index,
                "date": dates[index],
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0,
                "relative_strength": None,
                "rs_sma_50": None,
                "rs_score_current": None,
            }
            for index, (_, row) in enumerate(chart_history.iterrows())
        ]
        if has_indicators:
            for index, (_, row) in enumerate(indicator_history.iterrows()):
                if index >= len(chart_points):
                    break
                for column in ["relative_strength", "rs_sma_50", "rs_score_current"]:
                    value = row.get(column)
                    chart_points[index][column] = None if pd.isna(value) else float(value)
        chart_points_json = json.dumps(chart_points)
        visible_state_json = json.dumps(visible_state)
        drawing_dates_json = json.dumps(full_drawing_dates)
        symbol_json = json.dumps(symbol)
        navigator_y = height - 34
        navigator_start_x = left + (visible_state["start"] / max(visible_state["maxEnd"], 1)) * plot_width
        navigator_end_x = left + (visible_state["end"] / max(visible_state["maxEnd"], 1)) * plot_width
        navigator_width = max(8, navigator_end_x - navigator_start_x)
        navigator_label = ""
        if full_dates and dates:
            navigator_label = f'{dates[0]} to {dates[-1]} ({visible_state["visibleBars"]} bars)'
        navigator_svg = "" if compact else f"""
                    <g id="range-navigator">
                        <text x="{left}" y="{height - 55}" fill="#aaa" font-size="12">{html.escape(full_dates[0] if full_dates else "")}</text>
                        <text x="{width - right}" y="{height - 55}" fill="#aaa" font-size="12" text-anchor="end">{html.escape(full_dates[-1] if full_dates else "")}</text>
                        <text id="navigator-label" x="{left + plot_width / 2:.1f}" y="{height - 55}" fill="#e5e7eb" font-size="12" text-anchor="middle">{html.escape(navigator_label)}</text>
                        <rect id="navigator-track" x="{left}" y="{navigator_y - 4}" width="{plot_width}" height="8" fill="#374151" rx="4" style="cursor:pointer;" />
                        <rect id="navigator-window" x="{navigator_start_x:.1f}" y="{navigator_y - 8}" width="{navigator_width:.1f}" height="16" fill="#60a5fa" opacity="0.35" stroke="#93c5fd" rx="3" style="cursor:grab;" />
                        <rect id="navigator-left-handle" x="{navigator_start_x - 3:.1f}" y="{navigator_y - 12}" width="7" height="24" fill="#93c5fd" rx="2" style="cursor:ew-resize;" />
                        <rect id="navigator-right-handle" x="{navigator_end_x - 4:.1f}" y="{navigator_y - 12}" width="7" height="24" fill="#93c5fd" rx="2" style="cursor:ew-resize;" />
                    </g>
        """
        saved_drawing_elements = []
        date_to_index = {date: index for index, date in enumerate(full_drawing_dates)}
        date_only_to_first_index = {}
        date_only_to_last_index = {}
        for index, date_key in enumerate(full_drawing_dates):
            date_only = str(date_key)[:10]
            date_only_to_first_index.setdefault(date_only, index)
            date_only_to_last_index[date_only] = index

        def drawing_date_index(value, prefer: str = "first") -> Optional[int]:
            text = str(value)
            exact = date_to_index.get(text)
            if exact is not None:
                return exact
            date_only = text[:10]
            if date_only in date_to_index:
                return date_to_index[date_only]
            if prefer == "last":
                mapped = date_only_to_last_index.get(date_only)
            else:
                mapped = date_only_to_first_index.get(date_only)
            if mapped is not None:
                return mapped
            if options.get("intraday_chart") and len(text) <= 10 and full_drawing_dates:
                first_day = str(full_drawing_dates[0])[:10]
                last_day = str(full_drawing_dates[-1])[:10]
                if date_only < first_day:
                    return 0
                if date_only > last_day:
                    return len(full_drawing_dates) - 1
            return None

        for drawing in drawings or []:
            if not isinstance(drawing, dict) or drawing.get("type") != "line":
                continue
            start_index = drawing_date_index(drawing.get("start_date"), prefer="first")
            end_index = drawing_date_index(drawing.get("end_date"), prefer="last")
            if start_index is None or end_index is None:
                continue
            try:
                drawing_id_raw = str(drawing.get("id", f"drawing-{len(saved_drawing_elements)}"))
                drawing_id = html.escape(drawing_id_raw)
                start_date_raw = str(drawing.get("start_date"))
                end_date_raw = str(drawing.get("end_date"))
                start_price = float(drawing.get("start_price"))
                end_price = float(drawing.get("end_price"))
            except (TypeError, ValueError):
                continue
            if max(start_index, end_index) < visible_start or min(start_index, end_index) >= visible_end:
                continue

            def price_at(index: int) -> float:
                if end_index == start_index:
                    return start_price
                ratio = (index - start_index) / (end_index - start_index)
                return start_price + ratio * (end_price - start_price)

            clipped_start = max(visible_start, min(visible_end - 1, start_index))
            clipped_end = max(visible_start, min(visible_end - 1, end_index))
            x1 = x_for(clipped_start - visible_start)
            y1 = y_for(price_at(clipped_start))
            x2 = x_for(clipped_end - visible_start)
            y2 = y_for(price_at(clipped_end))
            is_readonly = bool(drawing.get("readonly", False))
            line_stroke = str(drawing.get("color") or "#60a5fa")
            raw_dash = drawing.get("dash")
            dash_attr = f'stroke-dasharray="{" ".join(str(v) for v in raw_dash)}"' if raw_dash else ""
            if is_readonly:
                saved_drawing_elements.append(
                    f'<g class="saved-drawing saved-drawing-readonly" data-drawing-id="{drawing_id}">'
                    f'<line class="saved-drawing-line" x1="{x1:.1f}" y1="{y1:.1f}" '
                    f'x2="{x2:.1f}" y2="{y2:.1f}" stroke="{line_stroke}" stroke-width="1.8" {dash_attr} />'
                    f'</g>'
                )
            else:
                start_handle = (
                    f'<circle class="drawing-endpoint drawing-start-endpoint" cx="{x1:.1f}" cy="{y1:.1f}" r="6" '
                    f'fill="#f8fafc" stroke="#2563eb" stroke-width="2" style="visibility:hidden;cursor:grab;pointer-events:all;" data-endpoint="start" />'
                )
                end_handle = (
                    f'<circle class="drawing-endpoint drawing-end-endpoint" cx="{x2:.1f}" cy="{y2:.1f}" r="6" '
                    f'fill="#f8fafc" stroke="#2563eb" stroke-width="2" style="visibility:hidden;cursor:grab;pointer-events:all;" data-endpoint="end" />'
                )
                saved_drawing_elements.append(
                    f'<g class="saved-drawing" data-drawing-id="{drawing_id}" '
                    f'data-start-date="{html.escape(start_date_raw)}" data-start-price="{start_price:.4f}" '
                    f'data-end-date="{html.escape(end_date_raw)}" data-end-price="{end_price:.4f}">'
                    f'<line class="saved-drawing-line" x1="{x1:.1f}" y1="{y1:.1f}" '
                    f'x2="{x2:.1f}" y2="{y2:.1f}" stroke="{line_stroke}" stroke-width="2.2" {dash_attr} />'
                    f'<line class="drawing-hit-line" x1="{x1:.1f}" y1="{y1:.1f}" '
                    f'x2="{x2:.1f}" y2="{y2:.1f}" stroke="transparent" stroke-width="14" style="cursor:pointer;pointer-events:stroke;" />'
                    f'{start_handle}{end_handle}'
                    f'</g>'
                )
        initial_target_price = target_price if target_price and min_price <= target_price <= max_price else None
        initial_target_y = y_for(float(initial_target_price)) if initial_target_price is not None else top
        initial_target_display = "block" if initial_target_price is not None else "none"
        initial_target_text = f"{float(initial_target_price):.2f}" if initial_target_price is not None else ""
        bridge_enabled = QWebEngineView is not None and QWebChannel is not None
        bridge_script = '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>' if bridge_enabled else ""
        indicator_panel = ChartsRenderMixin._generate_indicator_panel_svg(
            indicator_history=indicator_history,
            x_for=x_for,
            width=width,
            left=left,
            right=right,
            top=632,
            bottom=790,
        ) if has_indicators else ""
        ema_elements = ""
        if options["show_ema"]:
            ema_elements = f"""
                    <polyline points="{line_points(ema_10)}" fill="none" stroke="#facc15" stroke-width="2.0" stroke-linejoin="round" stroke-linecap="round" />
                    <polyline points="{line_points(ema_20)}" fill="none" stroke="#38bdf8" stroke-width="2.0" stroke-linejoin="round" stroke-linecap="round" />
                    <polyline points="{line_points(ema_50)}" fill="none" stroke="#c084fc" stroke-width="2.0" stroke-linejoin="round" stroke-linecap="round" />
                    <text x="{width - 260}" y="28" fill="#facc15" font-size="13">EMA 10</text>
                    <text x="{width - 190}" y="28" fill="#38bdf8" font-size="13">EMA 20</text>
                    <text x="{width - 120}" y="28" fill="#c084fc" font-size="13">EMA 50</text>
            """
        volume_label = f'<text x="{left}" y="{volume_top - 12}" fill="#aaa" font-size="13">Volume</text>' if options["show_volume"] else ""

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{safe_symbol} Chart</title>
            {bridge_script}
            <style>
                body {{
                    margin: 0;
                    background-color: #1e1e1e;
                    color: #ddd;
                    font-family: Arial, sans-serif;
                }}
                .wrap {{
                    width: 100%;
                    min-height: 100vh;
                    display: flex;
                    align-items: stretch;
                    justify-content: center;
                    padding: 12px;
                    box-sizing: border-box;
                }}
                svg {{
                    width: 100%;
                    height: calc(100vh - 24px);
                    min-height: {300 if compact else 520}px;
                    background: #202020;
                }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <svg viewBox="0 0 {width} {height}" role="img" aria-label="{safe_symbol} price chart">
                    <text x="{left}" y="28" fill="#f5f5f5" font-size="{18 if compact else 22}" font-weight="600">{safe_symbol}</text>
                    <text x="{left + (90 if compact else 120)}" y="28" fill="{line_color}" font-size="{13 if compact else 16}">
                        {html.escape(header_metrics)}
                    </text>
                    {''.join(grid_lines)}
                    {''.join(price_labels)}
                    <g id="pan-preview-layer">
                        {''.join(candle_elements)}
                        {ema_elements}
                        <line x1="{left}" y1="{chart_bottom}" x2="{width - right}" y2="{chart_bottom}" stroke="#555" />
                        {volume_label}
                        {''.join(volume_bars)}
                        {indicator_panel}
                        {''.join(date_labels)}
                    </g>
                    {navigator_svg}
                    <g id="crosshair" style="display:none;pointer-events:none;">
                        <line id="crosshair-x" x1="{left}" y1="{top}" x2="{left}" y2="{crosshair_bottom}" stroke="#d1d5db" stroke-width="1" stroke-dasharray="4 4" opacity="0.75" />
                        <line id="crosshair-y" x1="{left}" y1="{top}" x2="{width - right}" y2="{top}" stroke="#d1d5db" stroke-width="1" stroke-dasharray="4 4" opacity="0.75" />
                        <rect id="hover-box-bg" x="{left + 10}" y="{top + 10}" width="308" height="{hover_box_height}" fill="#020617" opacity="0.88" stroke="#475569" rx="4" />
                        <text id="hover-box-text" x="{left + 22}" y="{top + 32}" fill="#f8fafc" font-size="12"></text>
                    </g>
                    <g id="target-layer" style="display:{initial_target_display};">
                        <line id="target-line" x1="{left}" y1="{initial_target_y:.1f}" x2="{width - right}" y2="{initial_target_y:.1f}" stroke="#f97316" stroke-width="2.2" stroke-dasharray="8 5" pointer-events="none" />
                        <line id="target-drag-hit" x1="{left}" y1="{initial_target_y:.1f}" x2="{width - right}" y2="{initial_target_y:.1f}" stroke="transparent" stroke-width="14" style="cursor:ns-resize;" />
                        <rect id="target-label-bg" x="{target_label_x:.1f}" y="{initial_target_y - 11:.1f}" width="{target_label_width}" height="22" fill="#f97316" rx="3" pointer-events="none" />
                        <text id="target-label" x="{target_text_x:.1f}" y="{initial_target_y + 5:.1f}" fill="#111827" font-size="12" font-weight="600" text-anchor="middle" pointer-events="none">Breakout Price: {initial_target_text}</text>
                        <rect id="target-delete-bg" x="{target_delete_x:.1f}" y="{initial_target_y - 11:.1f}" width="22" height="22" fill="#7f1d1d" rx="3" style="cursor:pointer;" />
                        <text id="target-delete-label" x="{target_delete_text_x:.1f}" y="{initial_target_y + 5:.1f}" fill="#fff" font-size="13" font-weight="700" text-anchor="middle" pointer-events="none">X</text>
                    </g>
                    <rect id="chart-hit-area" x="{left}" y="{top}" width="{plot_width}" height="{price_height}" fill="transparent" style="cursor:crosshair;" />
                    <g id="drawing-layer">
                        {''.join(saved_drawing_elements)}
                    </g>
                </svg>
            </div>
            <script>
                const chartData = {chart_points_json};
                const visibleState = {visible_state_json};
                const drawingDates = {drawing_dates_json};
                const bounds = {{
                    left: {left},
                    right: {width - right},
                    top: {top},
                    bottom: {chart_bottom},
                    minPrice: {min_price:.8f},
                    maxPrice: {max_price:.8f},
                    plotWidth: {plot_width}
                }};
                const svg = document.querySelector("svg");
                const hitArea = document.getElementById("chart-hit-area");
                const crosshair = document.getElementById("crosshair");
                const crosshairX = document.getElementById("crosshair-x");
                const crosshairY = document.getElementById("crosshair-y");
                const hoverText = document.getElementById("hover-box-text");
                const targetLayer = document.getElementById("target-layer");
                const targetLine = document.getElementById("target-line");
                const targetDragHit = document.getElementById("target-drag-hit");
                const targetLabel = document.getElementById("target-label");
                const targetLabelBg = document.getElementById("target-label-bg");
                const targetDeleteBg = document.getElementById("target-delete-bg");
                const targetDeleteLabel = document.getElementById("target-delete-label");
                const panPreviewLayer = document.getElementById("pan-preview-layer");
                const drawingLayer = document.getElementById("drawing-layer");
                const navigatorTrack = document.getElementById("navigator-track");
                const navigatorWindow = document.getElementById("navigator-window");
                const navigatorLeftHandle = document.getElementById("navigator-left-handle");
                const navigatorRightHandle = document.getElementById("navigator-right-handle");
                let chartBridge = null;
                let isDraggingTarget = false;
                let targetMode = false;
                let drawingMode = false;
                let eraseMode = false;
                let drawingStart = null;
                let drawingPreview = null;
                let selectedDrawing = null;
                let activeDrawingHandle = null;
                let isPanningChart = false;
                let panStartClientX = 0;
                let panStartVisibleEnd = visibleState.end;
                let wheelZoomTimer = null;
                let pendingWheelBars = visibleState.visibleBars;
                let navigatorDragMode = null;
                let navigatorStartClientX = 0;
                let navigatorStartState = null;

                function updateChartWindow(visibleEnd, visibleBars = visibleState.visibleBars) {{
                    const minBars = 20;
                    const nextBars = Math.max(minBars, Math.min(visibleState.maxEnd, Math.round(visibleBars)));
                    const nextEnd = Math.max(1, Math.min(visibleState.maxEnd, Math.round(visibleEnd)));
                    if (chartBridge && chartBridge.updateChartWindow) {{
                        chartBridge.updateChartWindow({symbol_json}, nextBars, nextEnd);
                    }} else {{
                        const url = new URL(window.location.href);
                        url.hash = `bars=${{nextBars}}&end=${{nextEnd}}`;
                        window.location.replace(url.toString());
                    }}
                }}

                function setPanPreview(deltaPixels) {{
                    const transform = `translate(${{deltaPixels}}, 0)`;
                    if (panPreviewLayer) panPreviewLayer.setAttribute("transform", transform);
                    if (drawingLayer) drawingLayer.setAttribute("transform", transform);
                }}

                function clearPanPreview() {{
                    if (panPreviewLayer) panPreviewLayer.removeAttribute("transform");
                    if (drawingLayer) drawingLayer.removeAttribute("transform");
                }}

                function barsForPixels(deltaPixels) {{
                    return Math.round((deltaPixels / bounds.plotWidth) * visibleState.maxEnd);
                }}

                function slotForNavigatorX(clientX) {{
                    const point = svgPoint({{ clientX: clientX, clientY: 0 }});
                    const ratio = clamp((point.x - bounds.left) / bounds.plotWidth, 0, 1);
                    return Math.round(ratio * visibleState.maxEnd);
                }}

                if (typeof QWebChannel !== "undefined" && typeof qt !== "undefined") {{
                    new QWebChannel(qt.webChannelTransport, function(channel) {{
                        chartBridge = channel.objects.chartBridge;
                    }});
                }}

                function svgPoint(event) {{
                    const point = svg.createSVGPoint();
                    point.x = event.clientX;
                    point.y = event.clientY;
                    return point.matrixTransform(svg.getScreenCTM().inverse());
                }}

                function clamp(value, min, max) {{
                    return Math.max(min, Math.min(max, value));
                }}

                function priceForY(y) {{
                    const ratio = (bounds.bottom - y) / (bounds.bottom - bounds.top);
                    return bounds.minPrice + ratio * (bounds.maxPrice - bounds.minPrice);
                }}

                function yForPrice(price) {{
                    return bounds.bottom - ((price - bounds.minPrice) / (bounds.maxPrice - bounds.minPrice)) * (bounds.bottom - bounds.top);
                }}

                function nearestIndex(x) {{
                    if (chartData.length <= 1) return 0;
                    const ratio = clamp((x - bounds.left) / bounds.plotWidth, 0, 1);
                    const slot = Math.round(ratio * (visibleState.visibleBars - 1));
                    let bestIndex = 0;
                    let bestDistance = Math.abs(chartData[0].slot - slot);
                    for (let index = 1; index < chartData.length; index += 1) {{
                        const distance = Math.abs(chartData[index].slot - slot);
                        if (distance < bestDistance) {{
                            bestDistance = distance;
                            bestIndex = index;
                        }}
                    }}
                    return bestIndex;
                }}

                function formatVolume(value) {{
                    if (value >= 1000000000) return (value / 1000000000).toFixed(2) + "B";
                    if (value >= 1000000) return (value / 1000000).toFixed(2) + "M";
                    if (value >= 1000) return (value / 1000).toFixed(1) + "K";
                    return value.toFixed(0);
                }}

                function formatOptional(value, decimals = 2) {{
                    if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
                    return Number(value).toFixed(decimals);
                }}

                function setHoverText(lines) {{
                    hoverText.textContent = "";
                    lines.forEach((line, index) => {{
                        const tspan = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
                        tspan.setAttribute("x", "{left + 22}");
                        tspan.setAttribute("dy", index === 0 ? "0" : "16");
                        tspan.textContent = line;
                        hoverText.appendChild(tspan);
                    }});
                }}

                window.enableTargetMode = function() {{
                    targetMode = true;
                    drawingMode = false;
                    eraseMode = false;
                    drawingStart = null;
                    hitArea.style.pointerEvents = "auto";
                    hitArea.style.cursor = "copy";
                }};

                window.enableDrawingMode = function() {{
                    drawingMode = true;
                    targetMode = false;
                    eraseMode = false;
                    drawingStart = null;
                    hitArea.style.pointerEvents = "auto";
                    hitArea.style.cursor = "crosshair";
                }};

                window.enableEraseMode = function() {{
                    eraseMode = true;
                    drawingMode = false;
                    targetMode = false;
                    drawingStart = null;
                    hitArea.style.pointerEvents = "none";
                    hitArea.style.cursor = "not-allowed";
                }};

                window.clearAllDrawings = function() {{
                    drawingLayer.querySelectorAll(".saved-drawing").forEach((node) => node.remove());
                }};

                document.addEventListener("keydown", (event) => {{
                    if ({pan_left_cond_js}) {{
                        event.preventDefault();
                        updateChartWindow(visibleState.end - 5, visibleState.visibleBars);
                        return;
                    }}
                    if ({pan_right_cond_js}) {{
                        event.preventDefault();
                        updateChartWindow(visibleState.end + 5, visibleState.visibleBars);
                        return;
                    }}
                    if ({prev_symbol_cond_js}) {{
                        event.preventDefault();
                        if (chartBridge && chartBridge.stepChartSymbol) chartBridge.stepChartSymbol(-1);
                        return;
                    }}
                    if ({next_symbol_cond_js}) {{
                        event.preventDefault();
                        if (chartBridge && chartBridge.stepChartSymbol) chartBridge.stepChartSymbol(1);
                        return;
                    }}
                    if ({full_view_cond_js}) {{
                        event.preventDefault();
                        if (chartBridge && chartBridge.resetChartFullView) chartBridge.resetChartFullView({symbol_json});
                        return;
                    }}
                    if ({target_cond_js}) {{
                        window.enableTargetMode();
                    }}
                    if ({draw_cond_js}) {{
                        window.enableDrawingMode();
                    }}
                    if ({erase_cond_js}) {{
                        window.enableEraseMode();
                    }}
                    if (event.key === "Escape") {{
                        targetMode = false;
                        drawingMode = false;
                        eraseMode = false;
                        drawingStart = null;
                        if (drawingPreview) {{
                            drawingPreview.remove();
                            drawingPreview = null;
                        }}
                        hitArea.style.pointerEvents = "auto";
                        hitArea.style.cursor = "crosshair";
                    }}
                }});

                function saveTarget(price) {{
                    if (chartBridge && chartBridge.setChartTarget) {{
                        chartBridge.setChartTarget("{safe_symbol}", price);
                    }}
                }}

                function clearTarget() {{
                    targetLayer.style.display = "none";
                    if (chartBridge && chartBridge.clearChartTarget) {{
                        chartBridge.clearChartTarget("{safe_symbol}");
                    }}
                }}

                function saveDrawing(drawing) {{
                    if (chartBridge && chartBridge.saveChartDrawing) {{
                        chartBridge.saveChartDrawing({symbol_json}, JSON.stringify(drawing));
                    }}
                }}

                function deleteDrawing(drawingId) {{
                    if (chartBridge && chartBridge.deleteChartDrawing) {{
                        chartBridge.deleteChartDrawing({symbol_json}, drawingId);
                    }}
                }}

                function updateDrawing(group) {{
                    if (!group || !chartBridge || !chartBridge.updateChartDrawing) return;
                    chartBridge.updateChartDrawing({symbol_json}, JSON.stringify({{
                        id: group.getAttribute("data-drawing-id"),
                        type: "line",
                        start_date: group.getAttribute("data-start-date"),
                        start_price: Number(group.getAttribute("data-start-price")),
                        end_date: group.getAttribute("data-end-date"),
                        end_price: Number(group.getAttribute("data-end-price"))
                    }}));
                }}

                function clearDrawingSelection() {{
                    drawingLayer.querySelectorAll(".saved-drawing").forEach((node) => {{
                        node.classList.remove("selected-drawing");
                        const line = node.querySelector(".saved-drawing-line");
                        if (line) {{
                            line.setAttribute("stroke", "#60a5fa");
                            line.setAttribute("stroke-width", "2.2");
                        }}
                        node.querySelectorAll(".drawing-endpoint").forEach((handle) => {{
                            handle.style.visibility = "hidden";
                        }});
                    }});
                    selectedDrawing = null;
                }}

                function selectDrawing(group) {{
                    if (!group) return;
                    clearDrawingSelection();
                    selectedDrawing = group;
                    group.classList.add("selected-drawing");
                    const line = group.querySelector(".saved-drawing-line");
                    if (line) {{
                        line.setAttribute("stroke", "#f97316");
                        line.setAttribute("stroke-width", "2.8");
                    }}
                    group.querySelectorAll(".drawing-endpoint").forEach((handle) => {{
                        handle.style.visibility = "visible";
                    }});
                }}

                function createEndpointHandle(point, endpoint) {{
                    const handle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
                    handle.setAttribute("class", `drawing-endpoint drawing-${{endpoint}}-endpoint`);
                    handle.setAttribute("cx", point.x);
                    handle.setAttribute("cy", point.y);
                    handle.setAttribute("r", "6");
                    handle.setAttribute("fill", "#f8fafc");
                    handle.setAttribute("stroke", "#2563eb");
                    handle.setAttribute("stroke-width", "2");
                    handle.setAttribute("data-endpoint", endpoint);
                    handle.style.visibility = "hidden";
                    handle.style.cursor = "grab";
                    handle.style.pointerEvents = "all";
                    return handle;
                }}

                function setDrawingEndpoint(group, endpoint, point) {{
                    const line = group.querySelector(".saved-drawing-line");
                    const hitLine = group.querySelector(".drawing-hit-line");
                    const handle = group.querySelector(`.drawing-${{endpoint}}-endpoint`);
                    const xAttr = endpoint === "start" ? "x1" : "x2";
                    const yAttr = endpoint === "start" ? "y1" : "y2";
                    if (line) {{
                        line.setAttribute(xAttr, point.x);
                        line.setAttribute(yAttr, point.y);
                    }}
                    if (hitLine) {{
                        hitLine.setAttribute(xAttr, point.x);
                        hitLine.setAttribute(yAttr, point.y);
                    }}
                    if (handle) {{
                        handle.setAttribute("cx", point.x);
                        handle.setAttribute("cy", point.y);
                    }}
                    group.setAttribute(`data-${{endpoint}}-date`, point.date);
                    group.setAttribute(`data-${{endpoint}}-price`, point.price.toFixed(4));
                }}

                function pointFromEvent(event) {{
                    const point = svgPoint(event);
                    const x = clamp(point.x, bounds.left, bounds.right);
                    const y = clamp(point.y, bounds.top, bounds.bottom);
                    const visibleSlot = Math.round(clamp((x - bounds.left) / bounds.plotWidth, 0, 1) * Math.max(visibleState.visibleBars - 1, 1));
                    const absoluteSlot = visibleState.start + visibleSlot;
                    const drawingMaxSlot = Math.max(0, drawingDates.length - 1);
                    const clampedSlot = Math.max(0, Math.min(drawingMaxSlot, absoluteSlot));
                    const snappedX = chartData.length <= 1
                        ? bounds.right
                        : bounds.left + (visibleSlot / Math.max(visibleState.visibleBars - 1, 1)) * bounds.plotWidth;
                    return {{
                        x: snappedX,
                        y: y,
                        date: drawingDates[clampedSlot],
                        price: priceForY(y)
                    }};
                }}

                function createDrawingLine(start, end) {{
                    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
                    line.setAttribute("x1", start.x);
                    line.setAttribute("y1", start.y);
                    line.setAttribute("x2", end.x);
                    line.setAttribute("y2", end.y);
                    line.setAttribute("stroke", "#60a5fa");
                    line.setAttribute("stroke-width", "2.2");
                    line.setAttribute("class", "saved-drawing-line");
                    return line;
                }}

                function setTargetAtY(y, persist = true) {{
                    const clampedY = clamp(y, bounds.top, bounds.bottom);
                    const targetPrice = priceForY(clampedY);
                    targetLayer.style.display = "block";
                    targetLine.setAttribute("y1", clampedY);
                    targetLine.setAttribute("y2", clampedY);
                    targetDragHit.setAttribute("y1", clampedY);
                    targetDragHit.setAttribute("y2", clampedY);
                    targetLabelBg.setAttribute("y", clampedY - 11);
                    targetLabel.setAttribute("y", clampedY + 5);
                    targetDeleteBg.setAttribute("y", clampedY - 11);
                    targetDeleteLabel.setAttribute("y", clampedY + 5);
                    targetLabel.textContent = `Breakout Price: ${{targetPrice.toFixed(2)}}`;
                    if (persist) saveTarget(targetPrice);
                }}

                hitArea.addEventListener("mousemove", (event) => {{
                    const point = svgPoint(event);
                    const x = clamp(point.x, bounds.left, bounds.right);
                    const y = clamp(point.y, bounds.top, bounds.bottom);
                    const cursorPrice = priceForY(y);
                    const index = nearestIndex(x);
                    const bar = chartData[index];
                    const barX = chartData.length <= 1
                        ? bounds.right
                        : bounds.left + (bar.slot / Math.max(visibleState.visibleBars - 1, 1)) * bounds.plotWidth;

                    crosshair.style.display = "block";
                    crosshairX.setAttribute("x1", barX);
                    crosshairX.setAttribute("x2", barX);
                    crosshairY.setAttribute("y1", y);
                    crosshairY.setAttribute("y2", y);
                    const hoverLines = [
                        `${{bar.date}}    Price ${{cursorPrice.toFixed(2)}}`,
                        `O ${{bar.open.toFixed(2)}}  H ${{bar.high.toFixed(2)}}  L ${{bar.low.toFixed(2)}}`,
                        `C ${{bar.close.toFixed(2)}}  Volume ${{formatVolume(bar.volume)}}`,
                        `RS ${{formatOptional(bar.relative_strength, 4)}}  RS SMA ${{formatOptional(bar.rs_sma_50, 4)}}  Score ${{formatOptional(bar.rs_score_current, 0)}}`
                    ];
                    setHoverText(hoverLines);

                    if (drawingMode && drawingStart && drawingPreview) {{
                        drawingPreview.setAttribute("x2", barX);
                        drawingPreview.setAttribute("y2", y);
                    }}
                }});

                hitArea.addEventListener("mouseleave", () => {{
                    crosshair.style.display = "none";
                }});

                hitArea.addEventListener("click", (event) => {{
                    if (isDraggingTarget) return;
                    if (targetMode) {{
                        event.preventDefault();
                        event.stopPropagation();
                        const point = svgPoint(event);
                        setTargetAtY(point.y);
                        targetMode = false;
                        hitArea.style.cursor = "crosshair";
                        return;
                    }}
                    if (drawingMode) {{
                        event.preventDefault();
                        event.stopPropagation();
                        const drawingPoint = pointFromEvent(event);
                        if (!drawingStart) {{
                            drawingStart = drawingPoint;
                            drawingPreview = createDrawingLine(drawingStart, drawingStart);
                            drawingPreview.setAttribute("stroke-dasharray", "5 4");
                            drawingLayer.appendChild(drawingPreview);
                            return;
                        }}

                        if (drawingPreview) {{
                            drawingPreview.remove();
                            drawingPreview = null;
                        }}
                        const finalLine = createDrawingLine(drawingStart, drawingPoint);
                        const drawingId = `line-${{Date.now()}}-${{Math.round(Math.random() * 100000)}}`;
                        const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
                        group.setAttribute("class", "saved-drawing");
                        group.setAttribute("data-drawing-id", drawingId);
                        group.setAttribute("data-start-date", drawingStart.date);
                        group.setAttribute("data-start-price", drawingStart.price.toFixed(4));
                        group.setAttribute("data-end-date", drawingPoint.date);
                        group.setAttribute("data-end-price", drawingPoint.price.toFixed(4));
                        finalLine.setAttribute("class", "saved-drawing-line");
                        const hitLine = createDrawingLine(drawingStart, drawingPoint);
                        hitLine.setAttribute("class", "drawing-hit-line");
                        hitLine.setAttribute("stroke", "transparent");
                        hitLine.setAttribute("stroke-width", "14");
                        hitLine.style.cursor = "pointer";
                        hitLine.style.pointerEvents = "stroke";
                        group.appendChild(finalLine);
                        group.appendChild(hitLine);
                        group.appendChild(createEndpointHandle(drawingStart, "start"));
                        group.appendChild(createEndpointHandle(drawingPoint, "end"));
                        drawingLayer.appendChild(group);
                        selectDrawing(group);
                        saveDrawing({{
                            id: drawingId,
                            type: "line",
                            start_date: drawingStart.date,
                            start_price: drawingStart.price,
                            end_date: drawingPoint.date,
                            end_price: drawingPoint.price
                        }});
                        drawingStart = null;
                        drawingMode = false;
                        hitArea.style.cursor = "crosshair";
                    }}
                }});

                drawingLayer.addEventListener("click", (event) => {{
                    const group = event.target.closest(".saved-drawing");
                    if (!group) return;
                    event.preventDefault();
                    event.stopPropagation();
                    if (!eraseMode) {{
                        selectDrawing(group);
                        return;
                    }}
                    const drawingId = group.getAttribute("data-drawing-id");
                    group.remove();
                    eraseMode = false;
                    hitArea.style.pointerEvents = "auto";
                    hitArea.style.cursor = "crosshair";
                    if (drawingId) deleteDrawing(drawingId);
                }});

                drawingLayer.addEventListener("mousedown", (event) => {{
                    const handle = event.target.closest(".drawing-endpoint");
                    if (!handle) return;
                    const group = handle.closest(".saved-drawing");
                    if (!group) return;
                    event.preventDefault();
                    event.stopPropagation();
                    selectDrawing(group);
                    activeDrawingHandle = {{
                        group: group,
                        endpoint: handle.getAttribute("data-endpoint")
                    }};
                    handle.style.cursor = "grabbing";
                }});

                targetDragHit.addEventListener("mousedown", (event) => {{
                    event.preventDefault();
                    event.stopPropagation();
                    isDraggingTarget = true;
                }});

                svg.addEventListener("mousemove", (event) => {{
                    if (!isDraggingTarget) return;
                    const point = svgPoint(event);
                    setTargetAtY(point.y, false);
                }});

                document.addEventListener("mouseup", (event) => {{
                    if (!isDraggingTarget) return;
                    isDraggingTarget = false;
                    const point = svgPoint(event);
                    setTargetAtY(point.y, true);
                }});

                targetDeleteBg.addEventListener("click", (event) => {{
                    event.preventDefault();
                    event.stopPropagation();
                    clearTarget();
                }});

                hitArea.addEventListener("mousedown", (event) => {{
                    if (targetMode || drawingMode || eraseMode || isDraggingTarget) return;
                    event.preventDefault();
                    isPanningChart = true;
                    panStartClientX = event.clientX;
                    panStartVisibleEnd = visibleState.end;
                    hitArea.style.cursor = "grabbing";
                }});

                document.addEventListener("mousemove", (event) => {{
                    if (activeDrawingHandle) {{
                        event.preventDefault();
                        const point = pointFromEvent(event);
                        setDrawingEndpoint(activeDrawingHandle.group, activeDrawingHandle.endpoint, point);
                        return;
                    }}
                    if (!isPanningChart) return;
                    event.preventDefault();
                    const deltaPixels = event.clientX - panStartClientX;
                    setPanPreview(deltaPixels);
                    const pixelsPerBar = bounds.plotWidth / Math.max(visibleState.visibleBars - 1, 1);
                    const deltaBars = Math.round((panStartClientX - event.clientX) / pixelsPerBar);
                    const nextEnd = Math.max(1, Math.min(visibleState.maxEnd, panStartVisibleEnd + deltaBars));
                    const start = Math.max(0, nextEnd - visibleState.visibleBars);
                    const end = nextEnd;
                    crosshair.style.display = "none";
                    setHoverText([`Viewing bars ${{start + 1}}-${{end}} of ${{visibleState.total}}`]);
                }});

                document.addEventListener("mouseup", (event) => {{
                    if (activeDrawingHandle) {{
                        event.preventDefault();
                        updateDrawing(activeDrawingHandle.group);
                        activeDrawingHandle.group.querySelectorAll(".drawing-endpoint").forEach((handle) => {{
                            handle.style.cursor = "grab";
                        }});
                        activeDrawingHandle = null;
                        return;
                    }}
                    if (!isPanningChart) return;
                    isPanningChart = false;
                    hitArea.style.cursor = "crosshair";
                    clearPanPreview();
                    const pixelsPerBar = bounds.plotWidth / Math.max(visibleState.visibleBars - 1, 1);
                    const deltaBars = Math.round((panStartClientX - event.clientX) / pixelsPerBar);
                    const nextEnd = Math.max(1, Math.min(visibleState.maxEnd, panStartVisibleEnd + deltaBars));
                    if (nextEnd !== visibleState.end) {{
                        updateChartWindow(nextEnd, visibleState.visibleBars);
                    }}
                }});

                hitArea.addEventListener("wheel", (event) => {{
                    event.preventDefault();
                    if (targetMode || drawingMode || eraseMode) return;
                    const zoomFactor = event.deltaY < 0 ? 0.85 : 1.18;
                    pendingWheelBars = Math.max(20, Math.min(visibleState.maxEnd, pendingWheelBars * zoomFactor));
                    if (wheelZoomTimer) clearTimeout(wheelZoomTimer);
                    wheelZoomTimer = setTimeout(() => {{
                        updateChartWindow(visibleState.end, pendingWheelBars);
                    }}, 120);
                }}, {{ passive: false }});

                function startNavigatorDrag(event, mode) {{
                    event.preventDefault();
                    event.stopPropagation();
                    navigatorDragMode = mode;
                    navigatorStartClientX = event.clientX;
                    navigatorStartState = {{
                        start: visibleState.start,
                        end: visibleState.end,
                        bars: visibleState.visibleBars
                    }};
                }}

                if (navigatorWindow) {{
                    navigatorWindow.addEventListener("mousedown", (event) => startNavigatorDrag(event, "window"));
                }}
                if (navigatorLeftHandle) {{
                    navigatorLeftHandle.addEventListener("mousedown", (event) => startNavigatorDrag(event, "left"));
                }}
                if (navigatorRightHandle) {{
                    navigatorRightHandle.addEventListener("mousedown", (event) => startNavigatorDrag(event, "right"));
                }}
                if (navigatorTrack) {{
                    navigatorTrack.addEventListener("click", (event) => {{
                        if (navigatorDragMode) return;
                        const center = slotForNavigatorX(event.clientX);
                        const nextEnd = Math.max(visibleState.visibleBars, Math.min(visibleState.maxEnd, center + Math.round(visibleState.visibleBars / 2)));
                        updateChartWindow(nextEnd, visibleState.visibleBars);
                    }});
                }}

                document.addEventListener("mouseup", (event) => {{
                    if (!navigatorDragMode || !navigatorStartState) return;
                    const deltaBars = barsForPixels(event.clientX - navigatorStartClientX);
                    if (navigatorDragMode === "window") {{
                        updateChartWindow(navigatorStartState.end + deltaBars, navigatorStartState.bars);
                    }} else if (navigatorDragMode === "left") {{
                        const nextStart = Math.max(0, Math.min(navigatorStartState.end - 20, navigatorStartState.start + deltaBars));
                        updateChartWindow(navigatorStartState.end, navigatorStartState.end - nextStart);
                    }} else if (navigatorDragMode === "right") {{
                        const nextEnd = Math.max(navigatorStartState.start + 20, Math.min(visibleState.maxEnd, navigatorStartState.end + deltaBars));
                        updateChartWindow(nextEnd, nextEnd - navigatorStartState.start);
                    }}
                    navigatorDragMode = null;
                    navigatorStartState = null;
                }});
            </script>
        </body>
        </html>
        """
    @staticmethod
    def _normalize_chart_options(options: Optional[dict]) -> dict:
        defaults = {
            "show_volume": True,
            "show_rs": True,
            "show_ema": True,
            "show_adr": True,
            "show_growth_1m": True,
            "show_growth_3m": True,
            "show_growth_6m": False,
        }
        if options:
            defaults.update(options)
        return defaults
    @staticmethod
    def _format_chart_header_metrics(history: pd.DataFrame, options: Optional[dict] = None) -> str:
        options = ChartsRenderMixin._normalize_chart_options(options)
        close = history["Close"].astype(float)
        high = history["High"].astype(float)
        low = history["Low"].astype(float)
        latest_close = float(close.iloc[-1])
        metrics = [f"Close {latest_close:.2f}"]

        if options["show_adr"]:
            prev_close = close.shift(1)
            adr = ((high - low) / prev_close).replace([float("inf"), float("-inf")], pd.NA)
            adr_value = adr.rolling(20, min_periods=5).mean().iloc[-1] * 100
            metrics.append(f"ADR {ChartsRenderMixin._format_percent_metric(adr_value)}")

        growth_periods = [
            ("1M", 21, options["show_growth_1m"]),
            ("3M", 63, options["show_growth_3m"]),
            ("6M", 126, options["show_growth_6m"]),
        ]
        for label, bars, enabled in growth_periods:
            if not enabled:
                continue
            value = ChartsRenderMixin._growth_percent(close, bars)
            metrics.append(f"{label} {ChartsRenderMixin._format_percent_metric(value)}")

        return " | ".join(metrics)
    @staticmethod
    def _growth_percent(close: pd.Series, bars: int) -> Optional[float]:
        if len(close) <= bars:
            return None
        base = float(close.iloc[-bars - 1])
        if base == 0:
            return None
        return (float(close.iloc[-1]) / base - 1) * 100
    @staticmethod
    def _format_percent_metric(value: Optional[float]) -> str:
        if value is None or pd.isna(value):
            return "N/A"
        return f"{float(value):+.2f}%"
    @staticmethod
    def _future_weekday_dates(last_date, days: int = 5) -> List[str]:
        current = pd.Timestamp(last_date).date()
        dates = []
        while len(dates) < days:
            current += dt.timedelta(days=1)
            if current.weekday() >= 5:
                continue
            dates.append(current.strftime("%Y-%m-%d"))
        return dates
    @staticmethod
    def _align_chart_indicators(chart_history: pd.DataFrame, indicators: Optional[pd.DataFrame]) -> pd.DataFrame:
        if indicators is None or indicators.empty:
            return pd.DataFrame()

        aligned = indicators.copy()
        source_dates = aligned["date"] if "date" in aligned.columns else aligned.index
        aligned_index = pd.DatetimeIndex(pd.to_datetime(source_dates))
        if aligned_index.tz is not None:
            aligned_index = aligned_index.tz_convert(None)
        else:
            aligned_index = aligned_index.tz_localize(None)
        chart_index = pd.DatetimeIndex(pd.to_datetime(chart_history.index))
        if chart_index.tz is not None:
            chart_index = chart_index.tz_convert(None)
        else:
            chart_index = chart_index.tz_localize(None)
        aligned.index = aligned_index
        aligned = aligned.reindex(chart_index)
        required = ["relative_strength", "rs_sma_50"]
        if any(column not in aligned.columns for column in required):
            return pd.DataFrame()
        return aligned.dropna(subset=required)
    @staticmethod
    def _generate_indicator_panel_svg(
        indicator_history: pd.DataFrame,
        x_for,
        width: int,
        left: int,
        right: int,
        top: int,
        bottom: int,
    ) -> str:
        rs = indicator_history["relative_strength"].astype(float)
        rs_sma = indicator_history["rs_sma_50"].astype(float)
        values = pd.concat([rs, rs_sma]).dropna()
        if values.empty:
            return ""

        panel_height = bottom - top
        min_value = float(values.min())
        max_value = float(values.max())
        if min_value == max_value:
            min_value *= 0.98
            max_value *= 1.02
        padding = (max_value - min_value) * 0.12
        min_value -= padding
        max_value += padding

        def y_for(value: float) -> float:
            return bottom - ((value - min_value) / (max_value - min_value)) * panel_height

        def bool_at(row: pd.Series, column: str) -> bool:
            value = row.get(column)
            return bool(value) if pd.notna(value) else False

        points_rs = [
            (index, x_for(index), y_for(float(value)))
            for index, value in enumerate(rs)
            if pd.notna(value)
        ]
        points_sma = [
            (index, x_for(index), y_for(float(value)))
            for index, value in enumerate(rs_sma)
            if pd.notna(value)
        ]
        rs_line = " ".join(f"{x:.1f},{y:.1f}" for _, x, y in points_rs)
        sma_line = " ".join(f"{x:.1f},{y:.1f}" for _, x, y in points_sma)
        fill_points = " ".join(f"{x:.1f},{y:.1f}" for _, x, y in points_rs + list(reversed(points_sma)))

        marker_elements = []
        background_elements = []
        row_count = len(indicator_history)
        band_width = max(2.0, (width - left - right) / max(row_count, 1))
        for index, (_, row) in enumerate(indicator_history.iterrows()):
            x = x_for(index)
            rs_value = row.get("relative_strength")
            if bool_at(row, "is_ti65_bullish"):
                background_elements.append(
                    f'<rect x="{x - band_width / 2:.1f}" y="{top}" width="{band_width:.1f}" height="{panel_height}" fill="#22c55e" opacity="0.18" />'
                )
            elif bool_at(row, "is_ti65_bearish"):
                background_elements.append(
                    f'<rect x="{x - band_width / 2:.1f}" y="{top}" width="{band_width:.1f}" height="{panel_height}" fill="#ef4444" opacity="0.18" />'
                )

            if pd.isna(rs_value):
                continue
            y = y_for(float(rs_value))
            if bool_at(row, "is_plus_4pct_change"):
                marker_elements.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2" fill="#16a34a" stroke="#dcfce7" stroke-width="1" />')
            if bool_at(row, "is_minus_4pct_change"):
                marker_elements.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2" fill="#dc2626" stroke="#fee2e2" stroke-width="1" />')
            if bool_at(row, "is_9m_volume"):
                marker_elements.append(f'<rect x="{x - 3:.1f}" y="{bottom - 7:.1f}" width="6" height="6" fill="#111827" stroke="#e5e7eb" stroke-width="0.8" />')

        latest = indicator_history.dropna(subset=["rs_score_current"]).tail(1)
        table = ""
        if not latest.empty:
            row = latest.iloc[-1]
            score_items = [
                ("Current", row.get("rs_score_current")),
                ("Yesterday", row.get("rs_score_yesterday")),
                ("1 Week", row.get("rs_score_week")),
                ("1 Month", row.get("rs_score_month")),
            ]
            rows = []
            for offset, (label, value) in enumerate(score_items):
                score_text = "N/A" if pd.isna(value) else f"{float(value):.0f}"
                score_color = "#16a34a" if pd.notna(value) and float(value) > 70 else "#dc2626" if pd.notna(value) and float(value) < 30 else "#f59e0b"
                y = top + 20 + offset * 18
                rows.append(f'<text x="{left + 16}" y="{y}" fill="#f8fafc" font-size="12">{html.escape(label)}</text>')
                rows.append(f'<rect x="{left + 96}" y="{y - 13}" width="44" height="16" rx="2" fill="{score_color}" opacity="0.95" />')
                rows.append(f'<text x="{left + 118}" y="{y}" fill="#fff" font-size="12" text-anchor="middle">{score_text}</text>')
            table = (
                f'<rect x="{left + 8}" y="{top + 4}" width="142" height="86" fill="#020617" opacity="0.72" stroke="#475569" />'
                + "".join(rows)
            )

        return f"""
            <text x="{left}" y="{top - 14}" fill="#e5e7eb" font-size="14" font-weight="600">Relative Strength vs SPY</text>
            <text x="{width - 280}" y="{top - 14}" fill="#16a34a" font-size="12">RS above SMA</text>
            <text x="{width - 176}" y="{top - 14}" fill="#ef4444" font-size="12">RS below SMA</text>
            <line x1="{left}" y1="{bottom}" x2="{width - right}" y2="{bottom}" stroke="#555" />
            <line x1="{left}" y1="{top}" x2="{width - right}" y2="{top}" stroke="#333" />
            <line x1="{left}" y1="{top + panel_height / 2:.1f}" x2="{width - right}" y2="{top + panel_height / 2:.1f}" stroke="#333" />
            {''.join(background_elements)}
            <polygon points="{fill_points}" fill="#22c55e" opacity="0.14" />
            <polyline points="{sma_line}" fill="none" stroke="#e5e7eb" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
            <polyline points="{rs_line}" fill="none" stroke="#22c55e" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" />
            {''.join(marker_elements)}
            {table}
        """
    def _build_price_series(self, current_price: float, periods: int = 20) -> List[float]:
        base = current_price * 0.9
        step = (current_price - base) / max(periods - 1, 1)
        return [base + i * step for i in range(periods)]
