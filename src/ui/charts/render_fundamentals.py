"""Prepare stock-profile and earnings payloads for chart rendering."""

from __future__ import annotations

import html
import json
import math
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from src.core.chart_fundamentals import (
    EarningsEvent,
    EarningsLinePoint,
    EventStatus,
    StockProfile,
    UpcomingEarnings,
    canonical_symbol,
    clean_optional_text,
    format_compact_growth_pair,
    growth_display_token,
)


POSITIVE_EARNINGS_COLOR = "#089981"
NEGATIVE_EARNINGS_COLOR = "#f23645"
NEUTRAL_EARNINGS_COLOR = "#787b86"
EXPECTED_EARNINGS_COLOR = "#f59e0b"


def _reported_earnings_color(event: EarningsEvent) -> str:
    """Return a TradingView-style surprise color without inferring missing data."""

    try:
        reported = float(event.reported_eps)
        estimated = float(event.estimated_eps)
    except (TypeError, ValueError, OverflowError):
        return NEUTRAL_EARNINGS_COLOR
    if not math.isfinite(reported) or not math.isfinite(estimated):
        return NEUTRAL_EARNINGS_COLOR
    if reported > estimated:
        return POSITIVE_EARNINGS_COLOR
    if reported < estimated:
        return NEGATIVE_EARNINGS_COLOR
    return NEUTRAL_EARNINGS_COLOR


def build_fundamental_render_payload(
    *,
    canonical_display_symbol: str,
    stock_profile: Optional[StockProfile],
    options: Mapping[str, Any],
    upcoming_earnings: Optional[UpcomingEarnings],
    earnings_events: Optional[Iterable[EarningsEvent]],
    earnings_line: Optional[Iterable[EarningsLinePoint]],
    first_chart_date,
    last_chart_date,
    uses_intraday_time: bool,
    chart_time_value: Callable[[Any], Any],
    candles: Sequence[Mapping[str, Any]],
    future_values: Sequence[Any],
) -> dict[str, str]:
    """Return escaped HTML and JSON used by the lightweight chart template."""

    profile = stock_profile
    if profile is not None and canonical_symbol(profile.symbol) != canonical_display_symbol:
        profile = None
    watermark_lines = [("watermark-symbol", canonical_display_symbol)]
    if profile is not None:
        company_name = clean_optional_text(profile.company_name)
        if company_name and company_name.upper() != canonical_display_symbol:
            watermark_lines.append(("watermark-company", company_name))
        sector_name = clean_optional_text(profile.sector_name)
        industry_name = clean_optional_text(profile.industry_name)
        if sector_name:
            watermark_lines.append((
                "watermark-sector",
                sector_name if sector_name.lower().endswith(" sector") else f"{sector_name} Sector",
            ))
        if industry_name:
            watermark_lines.append(("watermark-industry", industry_name))
        if not sector_name and not industry_name:
            quote_type = clean_optional_text(profile.quote_type)
            category = clean_optional_text(profile.category)
            if quote_type:
                watermark_lines.append(("watermark-sector", quote_type.upper()))
            if category:
                watermark_lines.append(("watermark-industry", category))
    watermark_content = "".join(
        f'<div class="{css_class}">{html.escape(line)}</div>'
        for css_class, line in watermark_lines
        if clean_optional_text(line)
    )
    watermark_html = (
        f'<div id="stock-profile-watermark">{watermark_content}</div>'
        if bool(options.get("show_stock_profile_watermark", True))
        else ""
    )

    upcoming_badge_html = ""
    if upcoming_earnings is not None and upcoming_earnings.has_earnings_within_14d:
        estimated_title = " (estimated date)" if upcoming_earnings.is_date_estimated else ""
        upcoming_badge_html = (
            '<div id="earnings-upcoming-badge" title="Next earnings: '
            f'{html.escape(upcoming_earnings.next_earnings_date.isoformat() + estimated_title)}">'
            f"{html.escape(upcoming_earnings.badge_text)}</div>"
        )

    earnings_markers = []
    earnings_tooltips = []
    show_earnings_events = bool(options.get("show_earnings_events", True))
    for event in earnings_events or ():
        if canonical_symbol(event.symbol) != canonical_display_symbol:
            continue
        if event.event_status is not EventStatus.REPORTED:
            continue
        if event.report_date > last_chart_date:
            continue
        time_value = chart_time_value(event.report_date)
        compact = format_compact_growth_pair(event)
        marker_text = f"E {compact}" if compact else "E"
        tooltip_lines = ["Earnings", f"Report date: {event.report_date.isoformat()}"]
        if event.fiscal_period_end is not None:
            tooltip_lines.append(f"Fiscal period: {event.fiscal_period_end.isoformat()}")
        if event.reported_eps is not None:
            tooltip_lines.append(f"Reported EPS: {event.reported_eps:.2f}")
        if event.estimated_eps is not None:
            tooltip_lines.append(f"Estimated EPS: {event.estimated_eps:.2f}")
        if event.eps_surprise_pct is not None:
            tooltip_lines.append(f"Surprise: {event.eps_surprise_pct:.1f}%")
        current_growth = growth_display_token(event.eps_yoy_growth_pct, event.eps_growth_status)
        previous_growth = growth_display_token(
            event.previous_eps_yoy_growth_pct,
            event.previous_eps_growth_status,
        )
        if current_growth != "N/A" or previous_growth != "N/A":
            tooltip_lines.append(
                f"EPS growth: {current_growth}% / {previous_growth}%"
                .replace("TURN%", "TURN")
                .replace("LOSS%", "LOSS")
                .replace("N/M%", "N/M")
                .replace("N/A%", "N/A")
            )
        if event.revenue_yoy_growth_pct is not None:
            revenue_text = f"{event.revenue_yoy_growth_pct:.1f}%"
            if event.previous_revenue_yoy_growth_pct is not None:
                revenue_text += f" / {event.previous_revenue_yoy_growth_pct:.1f}%"
            tooltip_lines.append(f"Revenue growth: {revenue_text}")
        if event.ttm_eps is not None:
            tooltip_lines.append(f"TTM EPS: {event.ttm_eps:.2f}")
        earnings_tooltips.append({
            "time": time_value,
            "lines": tooltip_lines,
            "reported": True,
        })
        if show_earnings_events and first_chart_date <= event.report_date:
            earnings_markers.append({
                "time": time_value,
                "color": _reported_earnings_color(event),
                "label": "E",
                "detailText": marker_text,
                "future": False,
                "reported": True,
            })

    if (
        show_earnings_events
        and not uses_intraday_time
        and upcoming_earnings is not None
        and upcoming_earnings.has_earnings_within_14d
    ):
        upcoming_time = chart_time_value(upcoming_earnings.next_earnings_date)
        earnings_markers.append({
            "time": upcoming_time,
            "color": EXPECTED_EARNINGS_COLOR,
            "label": "E",
            "detailText": upcoming_earnings.badge_text,
            "future": upcoming_earnings.next_earnings_date > last_chart_date,
            "reported": False,
        })
        earnings_tooltips.append({
            "time": upcoming_time,
            "reported": False,
            "lines": [line for line in [
                "Expected earnings",
                f"Report date: {upcoming_earnings.next_earnings_date.isoformat()}",
                (
                    f"Timing: {upcoming_earnings.report_timing.value}"
                    if upcoming_earnings.report_timing.value != "UNKNOWN"
                    else None
                ),
                "Date is estimated" if upcoming_earnings.is_date_estimated else "Date is confirmed",
            ] if line is not None],
        })

    base_time_keys = {str(point["time"]) for point in candles}
    future_time_keys = {str(value) for value in future_values}
    earnings_whitespace = [
        {"time": marker["time"]}
        for marker in earnings_markers
        if str(marker["time"]) not in base_time_keys
        and str(marker["time"]) not in future_time_keys
    ]
    earnings_line_points = []
    if bool(options.get("show_earnings_line", True)) and not uses_intraday_time:
        for point in earnings_line or ():
            try:
                value = float(point.value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                earnings_line_points.append({
                    "time": chart_time_value(point.date),
                    "value": value,
                })

    return {
        "watermark_html": watermark_html,
        "upcoming_badge_html": upcoming_badge_html,
        "earnings_markers_json": json.dumps(earnings_markers),
        "earnings_tooltips_json": json.dumps(earnings_tooltips),
        "earnings_whitespace_json": json.dumps(earnings_whitespace),
        "earnings_line_json": json.dumps(earnings_line_points),
    }
