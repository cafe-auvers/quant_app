"""Local SVG chart HTML generation."""

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

from .models import normalize_chart_interaction_settings
from .render_metrics import ChartRenderMetricsMixin
from .render_primitives import ChartRenderPrimitivesMixin


class ChartLocalRenderMixin:
    @staticmethod
    def _generate_local_chart_html(
        symbol: str,
        history: pd.DataFrame,
        compact: bool = False,
        indicators: Optional[pd.DataFrame] = None,
        options: Optional[dict] = None,
        target_price: Optional[float] = None,
        drawings: Optional[List[dict]] = None,
        interaction_settings: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Generate a local SVG chart from OHLCV data."""
        options = ChartRenderMetricsMixin._normalize_chart_options(options)
        shortcuts, pan_step_bars = normalize_chart_interaction_settings(
            interaction_settings,
            renderer="local",
        )
        target_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("set_target", "T"))
        draw_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("draw_line", "D"))
        erase_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("erase_drawing", "E"))
        full_view_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("full_view", "F"))
        prev_symbol_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("prev_symbol", "Up"))
        next_symbol_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("next_symbol", "Down"))
        pan_left_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("pan_left", "Left"))
        pan_right_cond_js = ChartRenderPrimitivesMixin._get_js_key_condition(shortcuts.get("pan_right", "Right"))
        chart_history = ChartRenderPrimitivesMixin._normalize_chart_history(
            history,
            symbol,
            max_rows=options.get("max_history_bars", 180),
        )
        visible_start_time = ChartRenderPrimitivesMixin._coerce_timestamp_for_index(options.get("visible_start_time"), chart_history.index)
        visible_end_time = ChartRenderPrimitivesMixin._coerce_timestamp_for_index(options.get("visible_end_time"), chart_history.index)
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
            return ChartRenderPrimitivesMixin._generate_message_html(symbol, "No chart data available.")

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
        future_drawing_dates = [] if uses_intraday_keys else ChartRenderMetricsMixin._future_weekday_dates(full_chart_history.index[-1], days=5)
        full_drawing_dates = full_dates + future_drawing_dates
        closes = close.tolist()
        volumes = volume.tolist()
        indicator_history = (
            ChartRenderMetricsMixin._align_chart_indicators(chart_history, indicators)
            if not compact and options["show_rs"]
            else pd.DataFrame()
        )
        has_indicators = not indicator_history.empty
        width = 1180
        height = 360 if compact else (840 if has_indicators else 620)
        left = 62 if compact else 72
        right = 28 if compact else 190
        top = 38 if compact else 64
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
        line_color = "#4ade80" if change >= 0 else "#f87171"
        header_metrics = ChartRenderMetricsMixin._format_chart_header_metrics(chart_history, options)
        adr_metrics_svg = ""
        if not compact:
            metric_x = left
            metric_elements = []
            for label, value, value_color in ChartRenderMetricsMixin._chart_adr_metric_values(
                chart_history,
                options,
            ):
                metric_elements.append(
                    f'<text x="{metric_x}" y="50" font-size="13" font-weight="600">'
                    f'<tspan fill="#94a3b8">{html.escape(label)}</tspan>'
                    f'<tspan fill="{value_color}"> {html.escape(value)}</tspan>'
                    f'</text>'
                )
                metric_x += max(92, (len(label) + len(value) + 3) * 8)
            if metric_elements:
                adr_metrics_svg = f'<g id="adr-metrics">{"".join(metric_elements)}</g>'
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
        indicator_panel = ChartRenderMetricsMixin._generate_indicator_panel_svg(
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
                    {adr_metrics_svg}
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
                        updateChartWindow(visibleState.end - {pan_step_bars}, visibleState.visibleBars);
                        return;
                    }}
                    if ({pan_right_cond_js}) {{
                        event.preventDefault();
                        updateChartWindow(visibleState.end + {pan_step_bars}, visibleState.visibleBars);
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
