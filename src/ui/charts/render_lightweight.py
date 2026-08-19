"""Lightweight Charts HTML generation."""

from __future__ import annotations

import datetime as dt
import html
import json
from typing import Any, List, Mapping, Optional
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

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)
from .render_assets import _lightweight_charts_script_tag
from .render_metrics import ChartRenderMetricsMixin
from .models import normalize_chart_interaction_settings
from .render_primitives import ChartRenderPrimitivesMixin


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
        chart_history = ChartRenderPrimitivesMixin._normalize_chart_history(
            history, symbol, max_rows=options.get("max_history_bars", 260)
        )
        if chart_history.empty:
            return ChartRenderPrimitivesMixin._generate_message_html(symbol, "No chart data available.")

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

        safe_symbol = html.escape(symbol)
        header_metrics = ChartRenderMetricsMixin._format_chart_header_metrics(chart_history, options)
        adr_chips = ChartRenderMetricsMixin._format_chart_adr_metrics(chart_history, options)
        candles_json = json.dumps(candles)
        volumes_json = json.dumps(volumes)
        future_whitespace_json = json.dumps([{"time": value} for value in future_time_values()])
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
        indicator_history = ChartRenderMetricsMixin._align_chart_indicators(chart_history, indicators)
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
                        pct_val = row.get("pct_change_today")
                        pct_label = f"+{round(float(pct_val))}%" if pd.notna(pct_val) else "+4%"
                        rs_markers.append({"time": time_value, "position": "aboveBar", "color": "#22c55e", "shape": "circle", "text": pct_label})
                    if bool(row.get("is_minus_4pct_change")):
                        pct_val = row.get("pct_change_today")
                        pct_label = f"{round(float(pct_val))}%" if pd.notna(pct_val) else "-4%"
                        rs_markers.append({"time": time_value, "position": "belowBar", "color": "#ef4444", "shape": "circle", "text": pct_label})
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
                def score_span(label, value) -> str:
                    txt = score_text(value)
                    try:
                        is_high = not pd.isna(value) and float(value) > 85
                    except (TypeError, ValueError):
                        is_high = False
                    color = ' style="color:#22c55e"' if is_high else ''
                    return f'{label} <span{color}>{txt}</span>'
                score_summary = (
                    f"RS Score {score_span('C', latest_score.get('rs_score_current'))} | "
                    f"{score_span('W', latest_score.get('rs_score_week'))} | "
                    f"{score_span('M', latest_score.get('rs_score_month'))} | "
                    f"{score_span('Y', latest_score.get('rs_score_yesterday'))}"
                )
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
        # The daily default historically showed 81 candles plus 40 future slots.
        # Count actual sessions for intraday data so split 1H starts on the same
        # market date instead of guessing how many hourly bars make a session.
        default_visible_sessions = 81
        if uses_intraday_time:
            session_labels = [
                pd.Timestamp(timestamp).strftime("%Y-%m-%d")
                for timestamp in chart_history.index
            ]
            recent_sessions = list(dict.fromkeys(session_labels))[
                -default_visible_sessions:
            ]
            first_visible_session = recent_sessions[0] if recent_sessions else ""
            default_visible_data_bars = sum(
                label >= first_visible_session for label in session_labels
            )
        else:
            default_visible_data_bars = min(
                default_visible_sessions, len(chart_history)
            )
        bridge_enabled = QWebEngineView is not None and QWebChannel is not None
        bridge_script = '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>' if bridge_enabled else ""
        symbol_json = json.dumps((storage_symbol or symbol).strip().upper())
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
                <div id="header-row1">
                    <div id="symbol">{safe_symbol}</div>
                    <div id="metrics">{html.escape(header_metrics)} | {html.escape(str(options.get("timeframe", "1D")))} | {score_summary}</div>
                </div>
                <div id="header-row2">
                    <div id="adr-metrics">{adr_chips}</div>
                </div>
            </div>
            <div id="chart-area">
                <div id="price-panel">
                    <div id="chart"></div>
                    <canvas id="drawing-overlay"></canvas>
                </div>
                <div id="rs-chart"><div id="rs-empty">RS/TI65 data unavailable for this timeframe.</div></div>
            </div>
            {_lightweight_charts_script_tag()}
            <script>
                const candles = {candles_json};
                const volumes = {volumes_json};
                const futureWhitespace = {future_whitespace_json};
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
                const container = document.getElementById('chart');
                const rsContainer = document.getElementById('rs-chart');
                const pricePanel = document.getElementById('price-panel');
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
                candleSeries.setData(candles.concat(futureWhitespace));
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
                        title: 'RS vs SPY',
                        color: '#22c55e',
                        lineWidth: 2,
                        priceLineVisible: false
                    }});
                    rsSeries.setData(alignedRsPoints.concat(futureWhitespace));

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
                        title: 'RS SMA 50',
                        color: '#e5e7eb',
                        lineWidth: 1,
                        priceLineVisible: false
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

                function normalizeIncomingDrawing(drawing) {{
                    if (!drawing) return null;
                    if (drawing.start && drawing.end) return drawing;
                    const startTime = incomingDrawingTime(drawing.start_date, 'first');
                    const endTime = incomingDrawingTime(drawing.end_date, 'last');
                    if (startTime == null || endTime == null) return null;
                    return {{
                        id: String(drawing.id || ''),
                        start: {{ time: startTime, value: Number(drawing.start_price) }},
                        end: {{ time: endTime, value: Number(drawing.end_price) }},
                        color: drawing.color || null,
                        dash: drawing.dash || null,
                        readonly: drawing.readonly || false
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

                window.applySyncedTargetPrice = function(price) {{
                    targetMode = false;
                    renderTargetLine(price);
                }};
                window.upsertSyncedDrawing = function(drawing) {{
                    const normalized = normalizeIncomingDrawing(drawing);
                    if (normalized) addDrawingLine(normalized, false);
                }};
                window.removeSyncedDrawing = function(drawingId) {{
                    if (!drawingSeries.has(String(drawingId))) return;
                    drawingSeries.delete(String(drawingId));
                    if (selectedDrawingId === String(drawingId)) selectedDrawingId = null;
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
                }};
                window.panView = function(deltaBars) {{
                    const current = chart.timeScale().getVisibleLogicalRange();
                    if (!current) return;
                    const range = {{ from: current.from + deltaBars, to: current.to + deltaBars }};
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
