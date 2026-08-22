"""Local chart HTML renderer."""

from __future__ import annotations

import html
import json
import math
from typing import Any, Iterable, List, Mapping, Optional

import pandas as pd

from src.core.chart_fundamentals import (
    EarningsEvent, EarningsLinePoint, StockProfile, UpcomingEarnings, canonical_symbol)
from src.core.market_alignment import MarketAlignmentSnapshot

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
try:
    from PyQt5.QtWebChannel import QWebChannel
except ImportError:
    QWebChannel = None

from .render_assets import _lightweight_charts_script_tag
from .render_earnings_assets import EARNINGS_CHART_CSS, EARNINGS_EVENT_RUNTIME_JS
from .render_measurement_assets import (
    RIGHT_DRAG_MEASUREMENT_CSS,
    RIGHT_DRAG_MEASUREMENT_JS,
)
from .render_alignment import (
    MARKET_ALIGNMENT_OVERLAY_CSS,
    MARKET_ALIGNMENT_OVERLAY_JS,
    build_market_alignment_overlay,
)
from .render_fundamentals import build_fundamental_render_payload
from .render_metrics import ChartRenderMetricsMixin
from .models import normalize_chart_interaction_settings
from .render_primitives import ChartRenderPrimitivesMixin
from .render_viewport import default_visible_bar_count


class ChartLightweightRenderMixin:
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
        interaction_settings: Optional[Mapping[str, Any]] = None,
        stock_profile: Optional[StockProfile] = None,
        earnings_events: Optional[Iterable[EarningsEvent]] = None,
        earnings_line: Optional[Iterable[EarningsLinePoint]] = None,
        upcoming_earnings: Optional[UpcomingEarnings] = None,
        alignment_snapshot: Optional[MarketAlignmentSnapshot] = None,
    ) -> str:
        """Generate a stable TradingView Lightweight Charts page from local OHLCV data."""
        options = options or {}
        shortcuts, pan_step_bars = normalize_chart_interaction_settings(
            interaction_settings,
            renderer="lightweight",
        )
        target_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("set_target", "T"))
        draw_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("draw_line", "D"))
        erase_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("erase_drawing", "E"))
        full_view_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("full_view", "F"))
        pan_left_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("pan_left", "Left"))
        pan_right_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("pan_right", "Right"))
        if bool(options.get("history_is_normalized", False)):
            chart_history = history
        else:
            chart_history = ChartRenderPrimitivesMixin._normalize_chart_history(
                history, symbol, max_rows=options.get("max_history_bars", 260)
            )
        if chart_history.empty:
            return ChartRenderPrimitivesMixin._generate_message_html(symbol, "No chart data available.")

        candles = []
        volumes = []
        chart_index = pd.DatetimeIndex(chart_history.index)
        date_labels = chart_index.strftime("%Y-%m-%d").tolist()
        chart_timeframe = str(options.get("timeframe", "1D")).strip().upper()
        uses_intraday_time = bool(chart_timeframe and chart_timeframe != "1D") or len(
            set(date_labels)
        ) < len(date_labels)
        time_visible = "true" if uses_intraday_time else "false"

        if uses_intraday_time:
            utc_index = (
                chart_index.tz_localize("UTC")
                if chart_index.tz is None
                else chart_index.tz_convert("UTC")
            )
            chart_times = (utc_index.asi8 // 1_000_000_000).tolist()
        else:
            chart_times = date_labels

        def chart_time_value(timestamp) -> str | int:
            timestamp = pd.Timestamp(timestamp)
            if uses_intraday_time:
                if timestamp.tzinfo is None:
                    timestamp = timestamp.tz_localize("UTC")
                else:
                    timestamp = timestamp.tz_convert("UTC")
                return int(timestamp.timestamp())
            return timestamp.strftime("%Y-%m-%d")

        chart_times_by_date = {}
        for date_label, time_value in zip(date_labels, chart_times):
            chart_times_by_date.setdefault(date_label, []).append(time_value)
        chart_time_lookup = {
            date_label: values[-1]
            for date_label, values in chart_times_by_date.items()
        }
        normalized_chart_index = (
            chart_index.tz_convert(None)
            if chart_index.tz is not None
            else chart_index.tz_localize(None)
        )
        chart_time_by_timestamp = dict(zip(normalized_chart_index, chart_times))
        first_chart_time = chart_times[0]

        def drawing_time_value(value, prefer: str = "first") -> str | int:
            text = str(value)
            if uses_intraday_time and len(text) <= 10:
                day_matches = chart_times_by_date.get(text[:10], ())
                if day_matches:
                    return day_matches[-1] if prefer == "last" else day_matches[0]
                date_keys = sorted(chart_time_lookup.keys())
                if date_keys and text[:10] <= date_keys[0]:
                    return first_chart_time
                if date_keys and text[:10] >= date_keys[-1]:
                    return chart_time_value(text[:10])
            return chart_time_value(value)

        def future_time_values() -> List[str | int]:
            last_timestamp = chart_index[-1]
            if uses_intraday_time:
                if chart_timeframe == "1H":
                    step = pd.Timedelta(hours=1)
                elif chart_timeframe.endswith("M") and chart_timeframe[:-1].isdigit():
                    step = pd.Timedelta(minutes=int(chart_timeframe[:-1]))
                else:
                    step = pd.Timedelta(hours=1)
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

        price_columns = chart_history[["Open", "High", "Low", "Close", "Volume"]]
        for time_value, row in zip(
            chart_times,
            price_columns.itertuples(index=False, name=None),
        ):
            open_price, high_price, low_price, close_price, raw_volume = row
            open_price = float(open_price)
            high_price = float(high_price)
            low_price = float(low_price)
            close_price = float(close_price)
            volume = 0.0 if pd.isna(raw_volume) else float(raw_volume)
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

        canonical_display_symbol = canonical_symbol(storage_symbol or symbol)
        safe_symbol = html.escape(symbol)
        header_metrics = ChartRenderMetricsMixin._format_chart_header_metrics(chart_history, options)
        adr_chips = ChartRenderMetricsMixin._format_chart_adr_metrics(chart_history, options)
        candles_json = json.dumps(candles)
        volumes_json = json.dumps(volumes)
        future_values = future_time_values()
        future_whitespace_json = json.dumps([{"time": value} for value in future_values])

        first_chart_date = chart_index[0].date()
        last_chart_date = chart_index[-1].date()
        fundamental_payload = build_fundamental_render_payload(
            canonical_display_symbol=canonical_display_symbol,
            stock_profile=stock_profile,
            options=options,
            upcoming_earnings=upcoming_earnings,
            earnings_events=earnings_events,
            earnings_line=earnings_line,
            first_chart_date=first_chart_date,
            last_chart_date=last_chart_date,
            uses_intraday_time=uses_intraday_time,
            chart_time_value=chart_time_value,
            candles=candles,
            future_values=future_values,
        )
        watermark_html = fundamental_payload["watermark_html"]
        alignment_overlay_html = build_market_alignment_overlay(alignment_snapshot)
        upcoming_badge_html = fundamental_payload["upcoming_badge_html"]
        earnings_markers_json = fundamental_payload["earnings_markers_json"]
        earnings_tooltips_json = fundamental_payload["earnings_tooltips_json"]
        earnings_whitespace_json = fundamental_payload["earnings_whitespace_json"]
        earnings_line_json = fundamental_payload["earnings_line_json"]
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
                        {"time": time_value, "value": float(value)}
                        for time_value, value in zip(chart_times, ema.to_numpy())
                        if not pd.isna(value)
                    ],
                }
        ema_json = json.dumps(ema_series)
        indicator_payload = (
            ChartRenderMetricsMixin._build_relative_indicator_payload(
                chart_history, indicators, chart_time_by_timestamp
            )
            if bool(options.get("show_rs", True))
            else {}
        )
        rs_points = indicator_payload.get("rs_points", [])
        rs_sma_points = indicator_payload.get("rs_sma_points", [])
        rs_markers = indicator_payload.get("rs_markers", [])
        ti65_background = indicator_payload.get("ti65_background", [])
        relative_summary = indicator_payload.get("relative_summary", "vs SPY N/A")
        score_summary = indicator_payload.get("score_summary", "RS Score N/A")
        rs_points_json = json.dumps(rs_points)
        rs_sma_points_json = json.dumps(rs_sma_points)
        rs_markers_json = json.dumps(rs_markers)
        ti65_background_json = json.dumps(ti65_background)
        show_rs_panel = bool(options.get("show_rs", True)) and bool(rs_points)
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
        default_visible_data_bars = default_visible_bar_count(
            chart_history,
            uses_intraday_time=uses_intraday_time,
        )
        bridge_enabled = QWebEngineView is not None and QWebChannel is not None
        bridge_script = '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>' if bridge_enabled else ""
        symbol_json = json.dumps((storage_symbol or symbol).strip().upper())
        chart_timeframe_json = json.dumps(chart_timeframe)
        view_key_json = json.dumps(str(options.get("view_key", "single")))
        sync_crosshair_json = json.dumps(bool(options.get("sync_crosshair", False)))
        uses_intraday_time_json = json.dumps(uses_intraday_time)
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
                    display: flex;
                    flex-direction: column;
                    padding: 4px 12px 4px 12px;
                    box-sizing: border-box;
                    border-bottom: 1px solid #263241;
                    background: #111827;
                    gap: 2px;
                }}
                #header-row1 {{
                    display: flex;
                    align-items: center;
                    gap: 14px;
                }}
                {EARNINGS_CHART_CSS}
                #header-row2 {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
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
                #adr-metrics {{
                    display: flex;
                    gap: 12px;
                }}
                .adr-chip {{
                    color: #e2e8f0;
                    font-size: 13.5px;
                    font-weight: 600;
                    letter-spacing: 0.01em;
                }}
                .adr-chip span.label {{
                    color: #94a3b8;
                    font-weight: 500;
                    font-size: 12px;
                    margin-right: 3px;
                }}
                #chart-area {{
                    width: 100%;
                    height: calc(100% - 56px);
                    position: relative;
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
                #stock-profile-watermark {{
                    position: absolute;
                    left: 50%;
                    top: 48%;
                    transform: translate(-50%, -50%);
                    z-index: 2;
                    max-width: 70%;
                    padding: clamp(8px, 1.6vw, 14px) clamp(14px, 3vw, 24px);
                    border: 1px solid rgba(156, 163, 175, 0.20);
                    border-radius: 4px;
                    background: rgba(107, 114, 128, 0.30);
                    color: rgba(229, 231, 235, 0.74);
                    text-align: center;
                    pointer-events: none;
                    user-select: none;
                    line-height: 1.15;
                }}
                .watermark-symbol {{
                    color: rgba(249, 250, 251, 0.78);
                    font-size: clamp(26px, 5vw, 40px);
                    font-weight: 800;
                    letter-spacing: 0.04em;
                }}
                .watermark-company {{
                    margin-top: 3px;
                    font-size: clamp(16px, 2.7vw, 22px);
                    font-weight: 600;
                }}
                .watermark-sector, .watermark-industry {{
                    margin-top: 3px;
                    font-size: clamp(13px, 2.2vw, 18px);
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
                #linked-crosshair-vertical {{
                    display: none;
                    position: absolute;
                    top: 0;
                    bottom: 0;
                    width: 1px;
                    z-index: 6;
                    pointer-events: none;
                    background: repeating-linear-gradient(
                        to bottom,
                        rgba(209, 213, 219, 0.78) 0,
                        rgba(209, 213, 219, 0.78) 4px,
                        transparent 4px,
                        transparent 8px
                    );
                }}
                #drawing-overlay {{
                    position: absolute;
                    inset: 0;
                    width: 100%;
                    height: 100%;
                    z-index: 5;
                    pointer-events: none;
                }}
                {RIGHT_DRAG_MEASUREMENT_CSS}
                {MARKET_ALIGNMENT_OVERLAY_CSS}
            </style>
            {bridge_script}
        </head>
        <body>
            <div id="header">
                <div id="header-row1">
                    <div id="symbol">{safe_symbol}</div>
                    <div id="metrics">{html.escape(header_metrics)} | {html.escape(str(options.get("timeframe", "1D")))} | {relative_summary} | {score_summary}</div>
                    {upcoming_badge_html}
                </div>
                <div id="header-row2">
                    <div id="adr-metrics">{adr_chips}</div>
                </div>
            </div>
            <div id="chart-area">
                <div id="price-panel">
                    <div id="chart"></div>
                    {alignment_overlay_html}
                    {watermark_html}
                    <div id="earnings-event-layer" aria-label="Earnings events"></div>
                    <div id="chart-tooltip"></div>
                    <canvas id="drawing-overlay"></canvas>
                    <canvas id="measurement-overlay" aria-label="Percentage measurement"></canvas>
                </div>
                <div id="rs-chart"><div id="rs-empty">RS/TI65 data unavailable for this timeframe.</div></div>
                <div id="linked-crosshair-vertical" aria-hidden="true"></div>
            </div>
            {_lightweight_charts_script_tag()}
            <script>
                const candles = {candles_json};
                const volumes = {volumes_json};
                const futureWhitespace = {future_whitespace_json};
                const earningsWhitespace = {earnings_whitespace_json};
                const earningsMarkers = {earnings_markers_json};
                const earningsTooltips = {earnings_tooltips_json};
                const earningsLinePoints = {earnings_line_json};
                const emaSeries = {ema_json};
                const rsPoints = {rs_points_json};
                const rsSmaPoints = {rs_sma_points_json};
                const rsMarkers = {rs_markers_json};
                const ti65Background = {ti65_background_json};
                function alignIndicatorSeries(points) {{
                    const pointsByTime = new Map(
                        points.map(point => [String(point.time), point])
                    );
                    return candles.map(candle =>
                        pointsByTime.get(String(candle.time)) || {{ time: candle.time }}
                    );
                }}
                const alignedRsPoints = alignIndicatorSeries(rsPoints);
                const alignedRsSmaPoints = alignIndicatorSeries(rsSmaPoints);
                const alignedTi65Background = alignIndicatorSeries(ti65Background);
                const savedDrawings = {drawings_json};
                const symbolName = {symbol_json};
                const chartViewKey = {view_key_json};
                const crosshairSyncEnabled = {sync_crosshair_json};
                const usesIntradayTime = {uses_intraday_time_json};
                const chartTimeframe = {chart_timeframe_json};
                const container = document.getElementById('chart');
                const rsContainer = document.getElementById('rs-chart');
                const pricePanel = document.getElementById('price-panel');
                const chartArea = document.getElementById('chart-area');
                const linkedCrosshairVertical = document.getElementById('linked-crosshair-vertical');
                const chartTooltip = document.getElementById('chart-tooltip');
                const earningsEventLayer = document.getElementById('earnings-event-layer');
                let chartBridge = null;
                let drawingMode = false;
                let eraseMode = false;
                let editMode = false;
                let lineToolMode = false;
                let targetMode = false;
                let drawingStart = null;
                let drawingPreview = null;
                {MARKET_ALIGNMENT_OVERLAY_JS}
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
                            if (typeof time === 'string') return time;
                            if (typeof time !== 'number') {{
                                const y = time.year || '', mo = String(time.month || '').padStart(2,'0'), d = String(time.day || '').padStart(2,'0');
                                return `${{y}}-${{mo}}-${{d}}`;
                            }}
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
                            const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                            if (typeof time === 'number') {{
                                const d = new Date((time + 32400) * 1000);
                                if (tickMarkType === 0) return String(d.getUTCFullYear());
                                if (tickMarkType === 1) return months[d.getUTCMonth()];
                                if (tickMarkType === 2) return String(d.getUTCDate());
                                return String(d.getUTCHours()).padStart(2,'0') + ':' + String(d.getUTCMinutes()).padStart(2,'0');
                            }}
                            let y, mo, dy;
                            if (typeof time === 'string') {{
                                const p = time.split('-'); y = p[0]; mo = parseInt(p[1]); dy = parseInt(p[2]);
                            }} else {{
                                y = time.year; mo = time.month; dy = time.day;
                            }}
                            if (tickMarkType === 0) return String(y);
                            if (tickMarkType === 1) return months[(mo || 1) - 1] || String(mo);
                            return String(mo).padStart(2,'0') + '-' + String(dy).padStart(2,'0');
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
                function mergeSeriesData(...groups) {{
                    const byTime = new Map();
                    groups.forEach(group => group.forEach(point => {{
                        const key = String(point.time);
                        const existing = byTime.get(key);
                        if (!existing || Object.keys(point).length > Object.keys(existing).length) {{
                            byTime.set(key, point);
                        }}
                    }}));
                    return Array.from(byTime.values()).sort((left, right) => {{
                        if (typeof left.time === 'number' && typeof right.time === 'number') {{
                            return left.time - right.time;
                        }}
                        return String(left.time).localeCompare(String(right.time));
                    }});
                }}
                candleSeries.setData(
                    mergeSeriesData(futureWhitespace, earningsWhitespace, candles)
                );

                let earningsLineSeries = null;
                if (earningsLinePoints.length > 0) {{
                    earningsLineSeries = chart.addLineSeries({{
                        title: 'TTM EPS',
                        color: '#c084fc',
                        lineWidth: 2,
                        lineType: LightweightCharts.LineType.WithSteps,
                        priceScaleId: 'earnings',
                        priceLineVisible: false,
                        lastValueVisible: false,
                        crosshairMarkerVisible: true
                    }});
                    chart.priceScale('earnings').applyOptions({{
                        visible: false,
                        autoScale: true,
                        scaleMargins: {{ top: 0.58, bottom: 0.10 }}
                    }});
                    earningsLineSeries.setData(earningsLinePoints);
                }}

                {EARNINGS_EVENT_RUNTIME_JS}
                {RIGHT_DRAG_MEASUREMENT_JS}

                function formatPrice(value) {{
                    return Number(value).toFixed(2);
                }}

                function renderTargetLine(price) {{
                    if (targetLine) {{
                        candleSeries.removePriceLine(targetLine);
                        targetLine = null;
                    }}
                    if (price === null || price === undefined || !Number.isFinite(Number(price)) || Number(price) <= 0) {{
                        targetPrice = null;
                        return;
                    }}
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
                                if (typeof time === 'string') return time;
                                if (typeof time !== 'number') {{
                                    const y = time.year || '', mo = String(time.month || '').padStart(2,'0'), d = String(time.day || '').padStart(2,'0');
                                    return `${{y}}-${{mo}}-${{d}}`;
                                }}
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
                                const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                                if (typeof time === 'number') {{
                                    const d = new Date((time + 32400) * 1000);
                                    if (tickMarkType === 0) return String(d.getUTCFullYear());
                                    if (tickMarkType === 1) return months[d.getUTCMonth()];
                                    if (tickMarkType === 2) return String(d.getUTCDate());
                                    return String(d.getUTCHours()).padStart(2,'0') + ':' + String(d.getUTCMinutes()).padStart(2,'0');
                                }}
                                let y, mo, dy;
                                if (typeof time === 'string') {{
                                    const p = time.split('-'); y = p[0]; mo = parseInt(p[1]); dy = parseInt(p[2]);
                                }} else {{
                                    y = time.year; mo = time.month; dy = time.day;
                                }}
                                if (tickMarkType === 0) return String(y);
                                if (tickMarkType === 1) return months[(mo || 1) - 1] || String(mo);
                                return String(mo).padStart(2,'0') + '-' + String(dy).padStart(2,'0');
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
                    rsBackground.setData(alignedTi65Background.concat(futureWhitespace));
                    const rsSeries = rsChart.addLineSeries({{
                        title: 'Relative vs SPY',
                        color: '#9ca3af',
                        lineWidth: 2,
                        priceLineVisible: false,
                        priceFormat: {{
                            type: 'custom',
                            minMove: 0.1,
                            formatter: value => `${{value >= 0 ? '+' : ''}}${{value.toFixed(1)}}%`
                        }}
                    }});
                    rsSeries.setData(alignedRsPoints.concat(futureWhitespace));
                    const rsBaselineSeries = rsChart.addLineSeries({{
                        title: 'SPY baseline',
                        color: '#94a3b8',
                        lineWidth: 1,
                        lineStyle: LightweightCharts.LineStyle.Dashed,
                        lastValueVisible: false,
                        priceLineVisible: false,
                        priceFormat: {{
                            type: 'custom',
                            minMove: 0.1,
                            formatter: value => `${{value >= 0 ? '+' : ''}}${{value.toFixed(1)}}%`
                        }}
                    }});
                    const alignedRsBaselinePoints = alignedRsPoints.map(point =>
                        Object.prototype.hasOwnProperty.call(point, 'value')
                            ? {{ time: point.time, value: 0 }}
                            : {{ time: point.time }}
                    );
                    rsBaselineSeries.setData(alignedRsBaselinePoints.concat(futureWhitespace));

                    // Custom fixed-size spike marker primitive (dots on the RS line, zoom-invariant)
                    class RsSpikePrimitive {{
                        constructor(markers) {{
                            this._markers = markers;
                            this._series = null;
                            this._chart = null;
                        }}
                        attached({{ series, chart }}) {{
                            this._series = series;
                            this._chart = chart;
                        }}
                        detached() {{
                            this._series = null;
                            this._chart = null;
                        }}
                        updateAllViews() {{}}
                        paneViews() {{
                            const self = this;
                            return [{{
                                renderer() {{
                                    return {{
                                        draw(target) {{
                                            if (!self._series || !self._chart) return;
                                            target.useBitmapCoordinateSpace(scope => {{
                                                const ctx = scope.context;
                                                const ratio = scope.bitmapSize.width / scope.mediaSize.width;
                                                const RADIUS = 5 * ratio;
                                                const FONT_SIZE = Math.round(10 * ratio);
                                                ctx.font = `bold ${{FONT_SIZE}}px sans-serif`;
                                                ctx.textAlign = 'center';
                                                for (const m of self._markers) {{
                                                    const x = self._chart.timeScale().timeToCoordinate(m.time);
                                                    const y = self._series.priceToCoordinate(m.value);
                                                    if (x == null || y == null) continue;
                                                    const bx = x * ratio;
                                                    const by = y * ratio;
                                                    // Draw filled circle
                                                    ctx.beginPath();
                                                    ctx.arc(bx, by, RADIUS, 0, 2 * Math.PI);
                                                    ctx.fillStyle = m.color;
                                                    ctx.fill();
                                                    // Draw label centered above the dot
                                                    ctx.fillStyle = m.color;
                                                    ctx.fillText(m.text, bx, by - RADIUS - 3 * ratio);
                                                }}
                                            }});
                                        }}
                                    }};
                                }}
                            }}];
                        }}
                    }}

                    // Build primitive markers: look up the RS value for each marker time
                    const rsPointMap = {{}};
                    for (const p of rsPoints) {{ rsPointMap[String(p.time)] = p.value; }}
                    const primitiveMarkers = rsMarkers.map(m => ({{
                        time: m.time,
                        value: rsPointMap[String(m.time)] ?? null,
                        color: m.color,
                        text: m.text
                    }})).filter(m => m.value !== null);
                    if (primitiveMarkers.length > 0) {{
                        const spikePrimitive = new RsSpikePrimitive(primitiveMarkers);
                        rsSeries.attachPrimitive(spikePrimitive);
                    }}
                    const rsSmaSeries = rsChart.addLineSeries({{
                        title: 'Relative SMA 50',
                        color: '#e5e7eb',
                        lineWidth: 1,
                        priceLineVisible: false,
                        priceFormat: {{
                            type: 'custom',
                            minMove: 0.1,
                            formatter: value => `${{value >= 0 ? '+' : ''}}${{value.toFixed(1)}}%`
                        }}
                    }});
                    rsSmaSeries.setData(alignedRsSmaPoints.concat(futureWhitespace));
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
                    chart.subscribeCrosshairMove((param) => {{
                        updateLinkedVerticalCrosshair(container, param);
                    }});
                    rsChart.subscribeCrosshairMove((param) => {{
                        updateLinkedVerticalCrosshair(rsContainer, param);
                    }});
                }}

                function hideLinkedVerticalCrosshair() {{
                    if (linkedCrosshairVertical) {{
                        linkedCrosshairVertical.style.display = 'none';
                    }}
                }}

                function updateLinkedVerticalCrosshair(sourceContainer, param) {{
                    if (
                        !chartArea
                        || !linkedCrosshairVertical
                        || !sourceContainer
                        || !param
                        || !param.point
                        || !Number.isFinite(Number(param.point.x))
                    ) {{
                        hideLinkedVerticalCrosshair();
                        return;
                    }}
                    const areaRect = chartArea.getBoundingClientRect();
                    const sourceRect = sourceContainer.getBoundingClientRect();
                    const x = sourceRect.left - areaRect.left + Number(param.point.x);
                    if (x < 0 || x > areaRect.width) {{
                        hideLinkedVerticalCrosshair();
                        return;
                    }}
                    linkedCrosshairVertical.style.left = `${{Math.round(x)}}px`;
                    linkedCrosshairVertical.style.display = 'block';
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

                function incomingDrawingTime(value, prefer) {{
                    if (!usesIntradayTime) return String(value || '').slice(0, 10);
                    if (typeof value === 'number') return value;
                    const text = String(value || '');
                    const day = text.slice(0, 10);
                    const dayMatches = candles
                        .map(candle => candle.time)
                        .filter(time => normalizeTimeForSave(time).slice(0, 10) === day);
                    if (text.length <= 10 && dayMatches.length > 0) {{
                        return prefer === 'last'
                            ? dayMatches[dayMatches.length - 1]
                            : dayMatches[0];
                    }}
                    const parsed = Date.parse(text.replace(' ', 'T') + (text.includes('Z') ? '' : 'Z'));
                    if (Number.isFinite(parsed)) return Math.floor(parsed / 1000);
                    return dayMatches.length > 0
                        ? (prefer === 'last' ? dayMatches[dayMatches.length - 1] : dayMatches[0])
                        : null;
                }}

                function normalizeDrawingTimeframe(drawing, startValue, endValue) {{
                    const provided = String(drawing?.timeframe || "").toUpperCase();
                    if (provided) return provided;
                    const startText = String(startValue || "").toUpperCase();
                    const endText = String(endValue || "").toUpperCase();
                    if (
                        startText.length > 10 ||
                        endText.length > 10 ||
                        startText.includes(' ') ||
                        endText.includes(' ') ||
                        startText.includes('T') ||
                        endText.includes('T')
                    ) {{
                        return "INTRADAY";
                    }}
                    return "1D";
                }}

                function drawingTimeframesMatch(drawingTimeframe) {{
                    if (!drawingTimeframe) return true;
                    if (chartTimeframe === drawingTimeframe) return true;
                    if (chartTimeframe === "1D" || drawingTimeframe === "1D") return false;
                    return true;
                }}

                function normalizeIncomingDrawing(drawing) {{
                    if (!drawing) return null;
                    const normalizedTimeframe = normalizeDrawingTimeframe(
                        drawing,
                        drawing.start?.time,
                        drawing.end?.time
                    );
                    if (drawing.start && drawing.end) {{
                        drawing.timeframe = normalizedTimeframe;
                        return drawing;
                    }}
                    const startTime = incomingDrawingTime(drawing.start_date, "first");
                    const endTime = incomingDrawingTime(drawing.end_date, "last");
                    if (startTime == null || endTime == null) return null;
                    return {{
                        id: String(drawing.id || ''),
                        start: {{ time: startTime, value: Number(drawing.start_price) }},
                        end: {{ time: endTime, value: Number(drawing.end_price) }},
                        color: drawing.color || null,
                        dash: drawing.dash || null,
                        readonly: drawing.readonly || false,
                        timeframe: normalizedTimeframe
                    }};
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
                        timeframe: drawing.timeframe || chartTimeframe,
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
                            end_price: normalized.end.value,
                            timeframe: normalized.timeframe || chartTimeframe
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
                            end_price: Number(drawing.end.value),
                            timeframe: drawing.timeframe || chartTimeframe
                        }}));
                    }}
                }}

                function removeDrawingLine(drawingId, persist) {{
                    const normalizedId = String(drawingId);
                    if (!drawingSeries.has(normalizedId)) return;
                    if (drawingSeries.get(normalizedId)?.readonly) return;
                    drawingSeries.delete(normalizedId);
                    if (selectedDrawingId === normalizedId) selectedDrawingId = null;
                    renderDrawings();
                    if (persist && chartBridge && chartBridge.deleteChartDrawing) {{
                        chartBridge.deleteChartDrawing(
                            symbolName,
                            JSON.stringify({{ id: normalizedId, timeframe: chartTimeframe }})
                        );
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

                window.applySyncedTargetPrice = function(price) {{
                    targetMode = false;
                    renderTargetLine(price);
                }};
                window.upsertSyncedDrawing = function(drawing) {{
                    const normalized = normalizeIncomingDrawing(drawing);
                    if (!normalized || !drawingTimeframesMatch(normalized.timeframe)) return;
                    if (normalized) addDrawingLine(normalized, false);
                }};
                window.removeSyncedDrawing = function(payload) {{
                    const drawingPayload = payload && typeof payload === 'object'
                        ? payload
                        : {{ id: payload, timeframe: chartTimeframe }};
                    const incomingTimeframe = normalizeDrawingTimeframe(
                        drawingPayload,
                        drawingPayload.start_date,
                        drawingPayload.end_date
                    );
                    if (!drawingTimeframesMatch(incomingTimeframe)) return;
                    const drawingId = String(drawingPayload.id || '');
                    if (!drawingId || !drawingSeries.has(drawingId)) return;
                    drawingSeries.delete(drawingId);
                    if (selectedDrawingId === drawingId) selectedDrawingId = null;
                    renderDrawings();
                }};
                function resolveSyncedTime(value) {{
                    const text = String(value || '');
                    const day = text.slice(0, 10);
                    const allTimes = candles
                        .concat(futureWhitespace)
                        .map(point => point.time);
                    const dayMatches = allTimes.filter(
                        time => normalizeTimeForSave(time).slice(0, 10) === day
                    );
                    if (!usesIntradayTime) {{
                        return dayMatches.length > 0 ? dayMatches[0] : null;
                    }}
                    if (text.length > 10) {{
                        const parsed = Date.parse(
                            text.replace(' ', 'T') + (text.includes('Z') ? '' : 'Z')
                        );
                        if (Number.isFinite(parsed)) {{
                            const exactTime = Math.floor(parsed / 1000);
                            if (allTimes.some(time => Number(time) === exactTime)) {{
                                return exactTime;
                            }}
                        }}
                    }}
                    return dayMatches.length > 0 ? dayMatches[0] : null;
                }}
                window.showSyncedCrosshair = function(chartTime, price) {{
                    const resolvedTime = resolveSyncedTime(chartTime);
                    const resolvedPrice = Number(price);
                    if (
                        resolvedTime == null
                        || !Number.isFinite(resolvedPrice)
                        || resolvedPrice <= 0
                    ) {{
                        chart.clearCrosshairPosition();
                        return;
                    }}
                    chart.setCrosshairPosition(
                        resolvedPrice, resolvedTime, candleSeries
                    );
                }};
                window.clearSyncedCrosshair = function() {{
                    chart.clearCrosshairPosition();
                }};

                let crosshairPublishFrame = null;
                let pendingCrosshair = null;
                function publishCrosshair(visible, event) {{
                    if (!crosshairSyncEnabled) return;
                    if (!visible) {{
                        pendingCrosshair = null;
                    }} else {{
                        const rect = pricePanel.getBoundingClientRect();
                        if (!rect.width || !rect.height) return;
                        const x = event.clientX - rect.left;
                        const y = event.clientY - rect.top;
                        const time = chart.timeScale().coordinateToTime(x);
                        const price = candleSeries.coordinateToPrice(y);
                        pendingCrosshair = (
                            time != null
                            && price != null
                            && Number.isFinite(Number(price))
                            && Number(price) > 0
                        ) ? {{ time: normalizeTimeForSave(time), price: Number(price) }} : null;
                    }}
                    if (crosshairPublishFrame !== null) return;
                    crosshairPublishFrame = requestAnimationFrame(() => {{
                        crosshairPublishFrame = null;
                        if (!chartBridge || !chartBridge.syncChartCrosshair) return;
                        const point = pendingCrosshair;
                        chartBridge.syncChartCrosshair(
                            symbolName,
                            chartViewKey,
                            point ? point.time : '',
                            point ? point.price : 0,
                            Boolean(point)
                        );
                    }});
                }}
                pricePanel.addEventListener('mousemove', event => publishCrosshair(true, event));
                pricePanel.addEventListener('mouseleave', event => publishCrosshair(false, event));

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
                    const visibleDataBars = Math.min({default_visible_data_bars}, candles.length);
                    const visibleTo = Math.max(0, candles.length - 1 + futureBars);
                    const range = {{
                        from: Math.max(0, candles.length - visibleDataBars),
                        to: visibleTo
                    }};
                    chart.timeScale().setVisibleLogicalRange(range);
                    if (rsChart) rsChart.timeScale().setVisibleLogicalRange(range);
                    renderDrawings();
                    renderEarningsEventBadges();
                }};
                window.panView = function(deltaBars) {{
                    const current = chart.timeScale().getVisibleLogicalRange();
                    if (!current) return;
                    const range = {{ from: current.from + deltaBars, to: current.to + deltaBars }};
                    chart.timeScale().setVisibleLogicalRange(range);
                    if (rsChart) rsChart.timeScale().setVisibleLogicalRange(range);
                    renderDrawings();
                    renderEarningsEventBadges();
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
                    if ({pan_left_cond_js}) {{
                        event.preventDefault();
                        window.panView(-{pan_step_bars});
                        return;
                    }}
                    if ({pan_right_cond_js}) {{
                        event.preventDefault();
                        window.panView({pan_step_bars});
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
                    if (event.button !== 0) return;
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
