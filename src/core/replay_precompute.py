"""Vectorized, causal daily study artifacts and cross-sectional leadership scans."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from src.core.breakout_replay import clean_json, normalize_daily

PRECOMPUTE_VERSION = 1


def rules_key(rules):
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


def daily_study_artifact(symbol, history, spy):
    history, spy = normalize_daily(history), normalize_daily(spy)
    history = history.loc[history.Close > 0]
    close, high, low, volume = (history[name].astype(float) for name in ("Close", "High", "Low", "Volume"))
    previous = close.shift()
    averages = {n: close.rolling(n, min_periods=1).mean() for n in (7, 10, 20, 50, 65, 200)}
    ti65 = averages[7] / averages[65]
    ratio = close / spy.Close.reindex(history.index).replace(0, np.nan)
    valid = ratio.dropna()
    anchor = valid.iloc[0] if len(valid) else np.nan
    bars = history.rename(columns=str.lower).copy()
    bars["time"] = history.index.strftime("%Y-%m-%d")
    for n in (10, 20, 50):
        bars[f"ma{n}"] = averages[n]
    bars["rs"], bars["ti65"] = (ratio / anchor - 1) * 100, ti65
    bars["change_pct"] = (close / previous - 1) * 100
    metrics = pd.DataFrame(index=history.index)
    metrics["price"], metrics["volume"] = close, volume
    metrics["price_history_days"] = np.arange(1, len(history) + 1)
    metrics["dollar_volume"] = close * volume
    metrics["avg_volume_20d"] = volume.rolling(20, min_periods=1).mean()
    metrics["avg_dollar_volume_20d"] = (close * volume).rolling(20, min_periods=1).mean()
    metrics["adr_20_high_low"] = (high / low.where(low > 0) - 1).rolling(20, min_periods=20).mean() * 100
    metrics["adr_20"] = ((high - low) / previous).rolling(20, min_periods=5).mean() * 100
    metrics["adr"] = metrics.adr_20
    for label, n in (("1w", 5), ("1m", 21), ("3m", 63), ("6m", 126)):
        metrics[f"return_{label}"] = (close / close.shift(n).where(close.shift(n) > 0) - 1) * 100
    metrics["ti65"], metrics["change_pct"] = ti65, bars.change_pct
    metrics["sma_20"], metrics["sma_200"] = averages[20], averages[200]
    metrics["ema_50"] = close.ewm(span=50, adjust=False).mean()
    metrics["trend_intensity"] = np.clip(100 * np.tanh((close / metrics.ema_50 - 1) * 20), 0, 100)
    metrics["rs_slope_20d"] = (ratio / ratio.shift(20) - 1) * 100
    metrics["rs_above_sma_50"] = ratio > ratio.rolling(50, min_periods=1).mean()
    metrics["symbol"], metrics["name"] = symbol, symbol
    chart = {"bars": clean_json(bars.reset_index(drop=True).to_dict("records")),
             "rs_anchor": history.index[0].date().isoformat() if len(history) else None}
    values = metrics.replace([np.inf, -np.inf], np.nan)
    return {"version": PRECOMPUTE_VERSION, "symbol": symbol, "chart": chart,
            "metrics": dict(zip(history.index.strftime("%Y-%m-%d"), clean_json(values.to_dict("records"))))}, metrics


def rank_daily_universe(frames, days, rules):
    """Rank every eligible stock before filtering, independently on each session."""
    import operator

    index = pd.DatetimeIndex(days)
    stocks = {s: frame for s, frame in frames.items() if s != "SPY"}
    def matrix(field):
        return pd.DataFrame({s: f[field].reindex(index) for s, f in stocks.items()}, index=index)

    eligible = matrix("price_history_days") >= 2
    ranks = {}
    for period in ("1m", "3m", "6m"):
        values = matrix(f"return_{period}").where(eligible)
        ranks[f"momentum_rank_{period}"] = values.rank(axis=1, pct=True, method="average") * 100
        if period != "6m":
            ranks[f"growth_rank_{period}"] = values.rank(axis=1, pct=True, method="max") * 100
    ranks["momentum_rank_any"] = np.fmax(np.fmax(ranks["momentum_rank_1m"],
        ranks["momentum_rank_3m"]), ranks["momentum_rank_6m"])
    ranks["growth_rank"] = ranks["growth_rank_1m"]
    matches = eligible.copy()
    comparisons = {">=": operator.ge, "<=": operator.le, ">": operator.gt,
                   "<": operator.lt, "==": operator.eq, "!=": operator.ne}
    for rule in rules:
        field = rule["attribute"]
        values = ranks[field] if field in ranks else matrix(field)
        matches &= values.notna() & comparisons[rule["operator"]](values, float(rule["threshold"]))
    result = {}
    for day, stamp in zip(days, index):
        rows = []
        for symbol in matches.columns[matches.loc[stamp]]:
            row = clean_json(stocks[symbol].loc[stamp].to_dict())
            row.update(symbol=symbol, name=symbol)
            row.update({field: clean_json(float(values.at[stamp, symbol])) for field, values in ranks.items()})
            rows.append(row)
        rows.sort(key=lambda row: row.get("return_1m") or 0, reverse=True)
        result[day] = {"results": rows, "eligible": int(eligible.loc[stamp].sum()), "total": len(stocks)}
    return result
