"""Causal calculations for historical breakout practice."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from src.core.scanner import ComparisonOperator, ScanRule, StockScanner, add_momentum_ranks
from src.utils.data_loader import compute_stock_metrics
from src.utils.market_calendar import is_nyse_trading_day, nyse_regular_session_close_time as market_close_time

STUDY_START = dt.date(2024, 1, 1)
REPLAY_CLOSURES = {dt.date(2025, 1, 9)}  # NYSE national day of mourning.


def clean_json(value):
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean_json(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def scanner_setups(path: Path) -> dict:
    from src.ui.filter_catalog import DEFAULT_SCANNER_SETUPS

    setups = dict(DEFAULT_SCANNER_SETUPS)
    if path.is_file():
        setups.update(json.loads(path.read_text(encoding="utf-8-sig")).get("setups", {}))
    return setups


def scanner_rules(path: Path, setup: str = "Setup 1") -> list[dict]:
    setups = scanner_setups(path)
    if setup not in setups:
        raise ValueError("Choose a configured scanner setup")
    return setups[setup]["rules"]


def scanner_one_rules(path: Path) -> list[dict]:
    return scanner_rules(path)


def scanner_from_rules(rules: list[dict]) -> StockScanner:
    scanner = StockScanner()
    for rule in rules:
        scanner.add_rule(ScanRule(
            name=rule["attribute"], attribute=rule["attribute"],
            operator=ComparisonOperator(rule["operator"]), threshold=float(rule["threshold"]),
        ))
    return scanner


def normalize_daily(history: pd.DataFrame) -> pd.DataFrame:
    history = history.copy()
    index = pd.DatetimeIndex(history.index)
    # Daily cache keys denote a session date, not an intraday UTC instant.
    history.index = index.tz_localize(None).normalize()
    history = history.replace([np.inf, -np.inf], np.nan).dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    return history.sort_index().loc[lambda frame: ~frame.index.duplicated(keep="last")]


def metrics_as_of(symbol: str, history: pd.DataFrame, spy: pd.DataFrame, day: dt.date) -> dict | None:
    history = normalize_daily(history).loc[:pd.Timestamp(day)]
    spy = normalize_daily(spy).loc[:pd.Timestamp(day)]
    if history.empty or history.index[-1].date() != day:
        return None
    result = compute_stock_metrics(symbol, history, spy_history=spy)
    if result is None:
        return None
    close = history["Close"]
    for name, period in (("return_1m", 21), ("return_3m", 63), ("return_6m", 126)):
        if len(close) <= period:
            result[name] = None
    result["ti65"] = float(close.rolling(7, min_periods=1).mean().iloc[-1] / close.rolling(65, min_periods=1).mean().iloc[-1])
    result["change_pct"] = float(close.pct_change().iloc[-1] * 100)
    return clean_json(result)


def rank_and_scan(metrics: list[dict], rules: list[dict]) -> list[dict]:
    # Same max-tie percentile ranking as the production scanner; no future rows.
    for field, rank_name in (("return_1m", "growth_rank_1m"), ("return_3m", "growth_rank_3m")):
        ranks = pd.Series([m.get(field) for m in metrics], dtype=float).rank(pct=True, method="max") * 100
        for row, rank in zip(metrics, ranks):
            row[rank_name] = clean_json(float(rank))
            if field == "return_1m":
                row["growth_rank"] = row[rank_name]
    add_momentum_ranks(metrics)
    return sorted(scanner_from_rules(rules).scan(metrics), key=lambda row: row["return_1m"] or 0, reverse=True)


def next_session(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while not is_nyse_trading_day(day) or day in REPLAY_CLOSURES:
        day += dt.timedelta(days=1)
    return day


def daily_chart(history: pd.DataFrame, spy: pd.DataFrame, day: dt.date) -> dict:
    history = normalize_daily(history).loc[:pd.Timestamp(day)]
    spy = normalize_daily(spy).loc[:pd.Timestamp(day)]
    close = history["Close"].astype(float)
    ratio = close / spy["Close"].reindex(history.index)
    valid = ratio.dropna()
    anchor = float(valid.iloc[0]) if not valid.empty else None
    ti = close.rolling(7, min_periods=1).mean() / close.rolling(65, min_periods=1).mean()
    averages = {n: close.rolling(n, min_periods=1).mean() for n in (10, 20, 50)}
    changes = close.pct_change() * 100
    rows = []
    for i, (stamp, row) in enumerate(history.iterrows()):
        rows.append(clean_json({
            "time": stamp.date().isoformat(),
            **{name.lower(): float(row[name]) for name in ("Open", "High", "Low", "Close", "Volume")},
            **{f"ma{n}": float(series.iloc[i]) for n, series in averages.items()},
            "rs": (float(ratio.iloc[i]) / anchor - 1) * 100 if anchor else None,
            "ti65": float(ti.iloc[i]), "change_pct": float(changes.iloc[i]),
        }))
    return {"bars": rows, "rs_anchor": history.index[0].date().isoformat() if len(history) else None}


def regular_session(history: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    if history.empty:
        return history
    frame = history.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    frame.index = index.tz_convert("America/New_York")
    close_time = market_close_time(day)
    frame = frame.loc[(frame.index.date == day) & (frame.index.time >= dt.time(9, 30)) & (frame.index.time < close_time)]
    return frame.sort_index().loc[lambda f: ~f.index.duplicated(keep="last")]


def _orb_trade(bars, trigger, entry, stop, stop_valid):
    result = {"entry_taken": False, "entry_time": None, "stop_hit": False,
        "stop_time": None, "uncertain_stop_time": None, "trade_status": "no_entry"}
    if stop_valid is not True:
        result["no_entry_reason"] = "Previous-session ADR unavailable" if stop_valid is None else "Stop/ADR outside configured bounds"
        return result
    if trigger is None:
        result["no_entry_reason"] = "ORB entry level was not reached in the available bars"
        return result
    result.update(entry_taken=True, entry_time=trigger.isoformat(), trade_status="entered", no_entry_reason=None)
    after_entry = bars.loc[trigger:]
    entry_bar = after_entry.iloc[0]
    if entry_bar["Low"] <= stop and entry_bar["Open"] < entry:
        # The low may precede the breakout within this candle.
        result.update(stop_hit=None, uncertain_stop_time=trigger.isoformat(), trade_status="ambiguous")
        return result
    stopped = after_entry.loc[after_entry["Low"] <= stop]
    if not stopped.empty:
        result.update(stop_hit=True, stop_time=stopped.index[0].isoformat(), trade_status="stopped")
    return result


def opening_moves(history: pd.DataFrame, day: dt.date) -> dict:
    bars = regular_session(history, day)
    start = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 30)), tz="America/New_York")
    result = {"basis": "09:30 New York session open", "windows": {}}
    for minutes in (5, 30):
        end = start + pd.Timedelta(minutes=minutes)
        window = bars.loc[(bars.index >= start) & (bars.index < end)]
        expected = pd.date_range(start, end, freq="5min", inclusive="left")
        if not window.index.equals(expected):
            result["windows"][str(minutes)] = {"available": False,
                "reason": f"Missing five-minute bars in the opening {minutes}-minute window"}
            continue
        opening = float(window.iloc[0]["Open"])
        if opening <= 0:
            result["windows"][str(minutes)] = {"available": False, "reason": "Session opening price unavailable"}
            continue
        closing, peak = float(window.iloc[-1]["Close"]), float(window["High"].max())
        result["windows"][str(minutes)] = {"available": True, "open": opening, "close": closing,
            "high": peak, "close_pct": (closing / opening - 1) * 100,
            "high_pct": (peak / opening - 1) * 100, "volume": float(window["Volume"].sum()),
            "start_time": start.isoformat(), "end_time": end.isoformat()}
    return clean_json(result)


def orb_check(history: pd.DataFrame, day: dt.date, adr: float | None, settings: dict, buffer_pct: float = 0.1,
        opening_minutes: int = 5) -> dict:
    if opening_minutes not in (5, 30):
        raise ValueError("Choose a 5- or 30-minute opening range")
    bars = regular_session(history, day)
    start = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 30)), tz="America/New_York")
    end = start + pd.Timedelta(minutes=opening_minutes)
    opening = bars.loc[(bars.index >= start) & (bars.index < end)]
    expected_opening = pd.date_range(start, end, freq="5min", inclusive="left")
    if not opening.index.equals(expected_opening):
        return {"available": False, "entry_taken": False, "trade_status": "unavailable",
            "opening_minutes": opening_minutes,
            "opening_end": end.isoformat(),
            "reason": f"The {opening_minutes}-minute opening range has missing bars. No ORB verdict."}
    high = float(opening["High"].max())
    entry = high * (1 + buffer_pct / 100)
    stop = float(opening["Low"].min())
    sl_pct = (entry - stop) / entry * 100
    stop_adr = sl_pct / adr * 100 if adr and adr > 0 else None
    lower = float(settings.get("stop_adr_min_percent", 15))
    upper = float(settings.get("stop_adr_max_percent", 90))
    after_opening = bars.loc[bars.index >= end]
    triggered = after_opening.loc[after_opening["High"] >= entry]
    trigger = triggered.index[0] if not triggered.empty else None
    stop_valid = lower <= stop_adr <= upper if stop_adr is not None else None
    trade = _orb_trade(bars, trigger, entry, stop, stop_valid)
    gaps = sum((bars.index.to_series().diff().dropna() != pd.Timedelta(minutes=5)))
    expected_bars = int((dt.datetime.combine(day, market_close_time(day)) - dt.datetime.combine(day, dt.time(9, 30))).total_seconds() / 300)
    return clean_json({
        "available": True, "or_high": high, "or_low": stop,
        "opening_minutes": opening_minutes, "opening_end": end.isoformat(),
        "entry": entry, "stop": stop, "sl_pct": sl_pct, "stop_adr_pct": stop_adr,
        "adr_basis": "Previous completed daily session", "buffer_pct": buffer_pct,
        "min_stop_adr_pct": lower, "max_stop_adr_pct": upper,
        "stop_valid": stop_valid,
        "trigger_time": trigger.isoformat() if trigger is not None else None,
        "triggered": trigger is not None, "bars": len(bars), "expected_bars": expected_bars,
        "complete": len(bars) == expected_bars and gaps == 0,
        "capital_ignored": True,
        **trade,
        "note": "Opening-range low stop; 5-minute OHLC cannot determine within-bar execution order. A missing bar is not filled in.",
    })
