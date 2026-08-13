"""Chart metrics, indicator alignment, and SVG panels."""

from __future__ import annotations

import datetime as dt
import html
from typing import List, Optional, Tuple
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

from src.ui.charts.models import normalize_chart_options

REFERENCE_SYMBOL = "SPY"
KST_ZONE = ZoneInfo("Asia/Seoul")
US_MARKET_ZONE = ZoneInfo("America/New_York")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
LIVE_INTRADAY_REFRESH_INTERVAL_MS = 5 * 60 * 1000
TRADINGVIEW_REFRESH_INTERVAL_SECONDS = 5 * 60
KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS = 30 * 60
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)


class ChartRenderMetricsMixin:
    @staticmethod
    def _normalize_chart_options(options: Optional[dict]) -> dict:
        return normalize_chart_options(options)

    @staticmethod
    def _format_chart_header_metrics(history: pd.DataFrame, options: Optional[dict] = None) -> str:
        """Returns Close price metric for the top header row."""
        close = history["Close"].astype(float)
        latest_close = float(close.iloc[-1])
        return f"Close {latest_close:.2f}"

    @staticmethod
    def _format_chart_adr_metrics(history: pd.DataFrame, options: Optional[dict] = None) -> str:
        """Returns ADR and growth metrics as HTML chips for the second header row."""
        chips = []
        for label, value, value_color in ChartRenderMetricsMixin._chart_adr_metric_values(
            history,
            options,
        ):
            if label == "ADR":
                chips.append(
                    f'<span class="adr-chip"><span class="label">{label}</span>{value}</span>'
                )
            else:
                chips.append(
                    f'<span class="adr-chip"><span class="label">{label}</span>'
                    f'<span style="color:{value_color}">{value}</span></span>'
                )
        return "".join(chips)

    @staticmethod
    def _chart_adr_metric_values(
        history: pd.DataFrame,
        options: Optional[dict] = None,
    ) -> List[Tuple[str, str, str]]:
        """Return ADR/growth labels, formatted values, and display colors."""
        options = ChartRenderMetricsMixin._normalize_chart_options(options)
        close = history["Close"].astype(float)
        high = history["High"].astype(float)
        low = history["Low"].astype(float)
        metrics = []

        if options["show_adr"]:
            prev_close = close.shift(1)
            adr = ((high - low) / prev_close).replace([float("inf"), float("-inf")], pd.NA)
            adr_value = adr.rolling(20, min_periods=5).mean().iloc[-1] * 100
            value = ChartRenderMetricsMixin._format_percent_metric(adr_value)
            metrics.append(("ADR", value, "#e2e8f0"))

        growth_periods = [
            ("1M", 21, options["show_growth_1m"]),
            ("3M", 63, options["show_growth_3m"]),
            ("6M", 126, options["show_growth_6m"]),
        ]
        for label, bars, enabled in growth_periods:
            if not enabled:
                continue
            value = ChartRenderMetricsMixin._growth_percent(close, bars)
            formatted_value = ChartRenderMetricsMixin._format_percent_metric(value)
            color = "#22c55e" if value is not None and value >= 0 else "#ef4444"
            metrics.append((label, formatted_value, color))

        return metrics

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
