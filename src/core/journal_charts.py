"""Portable candle chart snapshots with explicit observation cutoffs."""
from __future__ import annotations

import datetime as dt
import html
import math

import pandas as pd

from src.core.trade_journal import NY_ZONE
from src.utils.market_calendar import nyse_regular_session_close_time


def snapshot_bars(frame: pd.DataFrame, session: dt.date, interval: str,
                  captured_at: dt.datetime) -> list[dict]:
    if frame.empty:
        return []
    result = []
    close = nyse_regular_session_close_time(session)
    for timestamp, values in frame.sort_index().iterrows():
        stamp = pd.Timestamp(timestamp)
        if interval == "1d":
            # Daily cache dates are session labels, even when stored at UTC midnight.
            day = stamp.date()
            if day > session:
                continue
            label = day.isoformat()
        else:
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            market_stamp = stamp.tz_convert(NY_ZONE)
            if market_stamp.date() != session or not dt.time(9, 30) <= market_stamp.time() < close:
                continue
            if stamp.to_pydatetime() + dt.timedelta(minutes=5) > captured_at:
                continue
            label = stamp.isoformat()
        try:
            row = {key.lower(): float(values[key]) for key in ("Open", "High", "Low", "Close", "Volume")}
        except (TypeError, ValueError, KeyError):
            continue
        if not all(math.isfinite(value) for value in row.values()):
            continue
        if row["low"] <= 0 or row["high"] < max(row["open"], row["close"], row["low"]):
            continue
        if row["low"] > min(row["open"], row["close"]) or row["volume"] < 0:
            continue
        result.append({"time": label, **row})
    return result[-120:] if interval == "1d" else result


def render_chart_svg(bars: list[dict], *, title: str, subtitle: str, plan: dict,
                     fill_price: float, interval: str, marker_time: str | None = None) -> str:
    width, height = 1200, 680
    left, top, right, bottom = 75, 90, 1040, 490
    fields = plan.get("fields", {})
    levels = [("Fill avg", fill_price), ("Breakout", fields.get("breakout_price")),
              ("ORB high", fields.get("entry_orb_high")), ("ORB low", fields.get("entry_orb_low"))]
    levels = [(label, float(price)) for label, price in levels if price and math.isfinite(float(price))]
    minimum = min([bar["low"] for bar in bars] + [price for _, price in levels])
    maximum = max([bar["high"] for bar in bars] + [price for _, price in levels])
    margin = max((maximum - minimum) * .08, maximum * .005)
    minimum, maximum = minimum - margin, maximum + margin
    def y(price):
        return bottom - (price - minimum) / (maximum - minimum) * (bottom - top)
    step = (right - left) / max(1, len(bars))
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="1200" height="680" fill="#101827"/>',
           '<g font-family="Arial,sans-serif" fill="#e5e7eb">',
           f'<text x="28" y="36" font-size="24">{html.escape(title)}</text>',
           f'<text x="28" y="63" font-size="13">{html.escape(subtitle)}</text>']
    for n in range(6):
        value = minimum + (maximum - minimum) * n / 5
        svg.append(f'<path d="M{left},{y(value):.1f} H{right}" stroke="#293448"/><text x="12" y="{y(value)+4:.1f}" font-size="12">{value:.2f}</text>')
    max_volume = max(1, max(bar["volume"] for bar in bars))
    for i, bar in enumerate(bars):
        x = left + (i + .5) * step
        color = "#34d399" if bar["close"] >= bar["open"] else "#fb7185"
        candle_width = max(1, step * .65)
        body_top = min(y(bar["open"]), y(bar["close"]))
        body_height = max(1, abs(y(bar["open"]) - y(bar["close"])))
        svg.append(f'<path d="M{x:.1f},{y(bar["high"]):.1f} V{y(bar["low"]):.1f}" stroke="{color}"/><rect x="{x-candle_width/2:.1f}" y="{body_top:.1f}" width="{candle_width:.1f}" height="{body_height:.1f}" fill="{color}"/>')
        volume_height = bar["volume"] / max_volume * 90
        svg.append(f'<rect x="{x-candle_width/2:.1f}" y="{605-volume_height:.1f}" width="{candle_width:.1f}" height="{volume_height:.1f}" fill="{color}" opacity=".6"/>')
        if i in {0, len(bars)//4, len(bars)//2, 3*len(bars)//4, len(bars)-1}:
            label = bar["time"] if interval == "1d" else pd.Timestamp(bar["time"]).tz_convert(NY_ZONE).strftime("%H:%M")
            svg.append(f'<text x="{x:.1f}" y="628" text-anchor="middle" font-size="11">{label}</text>')
    previous_label_y = top - 20
    for label, price in sorted(levels, key=lambda item: y(item[1])):
        label_y = max(y(price), previous_label_y + 18)
        previous_label_y = label_y
        svg.append(f'<path d="M{left},{y(price):.1f} H{right}" stroke="#93c5fd" stroke-dasharray="5,5"/><path d="M{right},{y(price):.1f} L1047,{label_y:.1f}" stroke="#93c5fd"/><text x="1050" y="{label_y+4:.1f}" font-size="12">{label} {price:.2f}</text>')
    if marker_time and interval == "5m":
        marker = pd.Timestamp(marker_time)
        index = next((i for i, bar in enumerate(bars) if pd.Timestamp(bar["time"]) <= marker < pd.Timestamp(bar["time"]) + pd.Timedelta(minutes=5)), None)
        if index is not None:
            x = left + (index + .5) * step
            svg.append(f'<path d="M{x:.1f},{top} V605" stroke="#fbbf24" stroke-dasharray="4,4"/><text x="{x+5:.1f}" y="84" font-size="11" fill="#fbbf24">Fill observed</text>')
    svg.append('<text x="28" y="660" font-size="12">Volume below. Times: New York. Dashed levels: recorded plan / cumulative fill average.</text></g></svg>')
    return "".join(svg)
