"""P&L equity-curve chart HTML for the Health tab's P&L Dashboard sub-tab.

Renders a single-series baseline chart (green above zero, red below) using
the same vendored Lightweight Charts build the TradingView/Intraday tabs use,
so no extra dependency or network fetch is introduced.
"""
from __future__ import annotations

import html
import json
from typing import List, Optional, Sequence

from src.services.pnl_history import PnlDailySnapshot
# Shared vendored-asset helper used across every embedded chart in the app.
from src.ui.charts.render_assets import _lightweight_charts_script_tag

SERIES_COMBINED = "combined"
SERIES_REALIZED = "realized"
SERIES_UNREALIZED = "unrealized"

UNIT_USD = "usd"
UNIT_KRW = "krw"
UNIT_PCT = "pct"

VIEW_CUMULATIVE = "cumulative"
VIEW_DAILY = "daily"

_SERIES_LABELS = {
    SERIES_COMBINED: "Total P&L (Realized + Unrealized)",
    SERIES_REALIZED: "Realized P&L",
    SERIES_UNREALIZED: "Unrealized P&L",
}
_UNIT_LABELS = {UNIT_USD: "USD ($)", UNIT_KRW: "KRW (₩)", UNIT_PCT: "% of account"}
_VIEW_LABELS = {VIEW_CUMULATIVE: "Cumulative", VIEW_DAILY: "Daily change"}

# Validated status colors (dataviz palette): good/critical, dark chart surface.
_COLOR_GOOD = "#0ca30c"
_COLOR_CRITICAL = "#d03b3b"
_COLOR_SURFACE = "#1a1a19"
_COLOR_TEXT_PRIMARY = "#ffffff"
_COLOR_TEXT_SECONDARY = "#c3c2b7"
_COLOR_GRID = "#2c2c2a"
_COLOR_AXIS = "#383835"


def _series_usd_value(snapshot: PnlDailySnapshot, series: str) -> float:
    if series == SERIES_REALIZED:
        return snapshot.realized_usd
    if series == SERIES_UNREALIZED:
        return snapshot.unrealized_usd
    return snapshot.total_usd


def _convert_value(
    usd_value: float, snapshot: PnlDailySnapshot, unit: str
) -> Optional[float]:
    if unit == UNIT_KRW:
        if not snapshot.fx_rate:
            return None
        return usd_value * snapshot.fx_rate
    if unit == UNIT_PCT:
        if not snapshot.capital_base_usd:
            return None
        return usd_value / snapshot.capital_base_usd * 100.0
    return usd_value


def pnl_chart_points(
    snapshots: Sequence[PnlDailySnapshot],
    *,
    series: str,
    unit: str,
    view: str = VIEW_CUMULATIVE,
) -> List[dict]:
    """Pure helper (no HTML) so the table-view fallback can reuse the same data."""
    points = []
    previous_usd: Optional[float] = None
    for snapshot in snapshots:
        cumulative_usd = _series_usd_value(snapshot, series)
        usd_value = cumulative_usd
        if view == VIEW_DAILY:
            usd_value = (
                cumulative_usd
                if previous_usd is None
                else cumulative_usd - previous_usd
            )
        previous_usd = cumulative_usd
        value = _convert_value(usd_value, snapshot, unit)
        if value is None:
            continue
        points.append({"time": snapshot.date, "value": round(value, 4)})
    return points


def generate_pnl_chart_html(
    snapshots: Sequence[PnlDailySnapshot],
    *,
    series: str = SERIES_COMBINED,
    unit: str = UNIT_USD,
    view: str = VIEW_CUMULATIVE,
) -> str:
    points = pnl_chart_points(snapshots, series=series, unit=unit, view=view)
    series_label = _SERIES_LABELS.get(series, series)
    unit_label = _UNIT_LABELS.get(unit, unit)
    view_label = _VIEW_LABELS.get(view, view)

    if not points:
        if not snapshots:
            message = "No P&L history yet. It starts building the next time this tab refreshes."
        else:
            message = (
                f"No data yet for {unit_label} — needs a live USD/KRW rate or "
                "account size to be fetched at least once."
                if unit != UNIT_USD
                else "No data yet for this series."
            )
        return _message_html(message)

    data_json = json.dumps(points)
    title = html.escape(f"{view_label} · {series_label} · {unit_label}")

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
  html, body {{ margin:0; padding:0; height:100%; background:{_COLOR_SURFACE}; }}
  #container {{ width:100%; height:100%; }}
  #legend {{
    position:absolute; top:8px; left:12px; z-index:2; pointer-events:none;
    color:{_COLOR_TEXT_PRIMARY};
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    font-size:13px;
  }}
  #legend .sub {{ color:{_COLOR_TEXT_SECONDARY}; font-size:11px; }}
</style>
{_lightweight_charts_script_tag()}
</head>
<body>
<div id="legend"><div>{title}</div><div class="sub" id="legend-value"></div></div>
<div id="container"></div>
<script>
  const container = document.getElementById('container');
  const legendValue = document.getElementById('legend-value');
  const points = {data_json};
  const chart = LightweightCharts.createChart(container, {{
    width: container.clientWidth || 800,
    height: container.clientHeight || 400,
    layout: {{ background: {{ color: '{_COLOR_SURFACE}' }}, textColor: '{_COLOR_TEXT_SECONDARY}' }},
    grid: {{
      vertLines: {{ color: '{_COLOR_GRID}' }},
      horzLines: {{ color: '{_COLOR_GRID}' }}
    }},
    rightPriceScale: {{
      borderColor: '{_COLOR_AXIS}',
      autoScale: true,
      minimumWidth: 88,
      scaleMargins: {{ top: 0.16, bottom: 0.16 }},
    }},
    timeScale: {{
      borderColor: '{_COLOR_AXIS}',
      rightOffset: 0.75,
      barSpacing: 28,
      minBarSpacing: 8,
      fixLeftEdge: true,
      fixRightEdge: true,
      timeVisible: false,
    }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
  }});
  const series = chart.addBaselineSeries({{
    baseValue: {{ type: 'price', price: 0 }},
    topLineColor: '{_COLOR_GOOD}',
    topFillColor1: 'rgba(12,163,12,0.28)',
    topFillColor2: 'rgba(12,163,12,0.03)',
    bottomLineColor: '{_COLOR_CRITICAL}',
    bottomFillColor1: 'rgba(208,59,59,0.03)',
    bottomFillColor2: 'rgba(208,59,59,0.28)',
    lineWidth: 2,
    priceLineVisible: true,
    lastValueVisible: true,
    priceFormat: {{ type: 'price', precision: 2, minMove: 0.01 }},
  }});
  series.setData(points);
  chart.timeScale().fitContent();
  if (points.length) {{
    const last = points[points.length - 1];
    legendValue.innerText = last.time + ':  ' + last.value.toLocaleString(undefined, {{maximumFractionDigits: 2}});
  }}
  chart.subscribeCrosshairMove(function(param) {{
    if (!param || !param.time || !param.seriesData) {{
      if (points.length) {{
        const last = points[points.length - 1];
        legendValue.innerText = last.time + ':  ' + last.value.toLocaleString(undefined, {{maximumFractionDigits: 2}});
      }}
      return;
    }}
    const point = param.seriesData.get(series);
    if (point && typeof point.value === 'number') {{
      legendValue.innerText = param.time + ':  ' + point.value.toLocaleString(undefined, {{maximumFractionDigits: 2}});
    }}
  }});
  function resizeChart() {{
    chart.applyOptions({{ width: container.clientWidth, height: container.clientHeight }});
  }}
  window.addEventListener('resize', resizeChart);
  if (typeof ResizeObserver !== 'undefined') {{
    new ResizeObserver(resizeChart).observe(container);
  }}
</script>
</body>
</html>
"""


def _message_html(message: str) -> str:
    safe = html.escape(message)
    return f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8" /></head>
<body style="margin:0;background:{_COLOR_SURFACE};color:{_COLOR_TEXT_SECONDARY};
font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;text-align:center;padding:0 24px;">
<div>{safe}</div>
</body></html>
"""
