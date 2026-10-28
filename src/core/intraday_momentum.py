"""Explainable advisory momentum ranks; never used to authorize an order."""
from __future__ import annotations

import datetime as dt
import math
from statistics import median

import pandas as pd

from src.utils.market_calendar import (
    US_MARKET_ZONE, is_nyse_trading_day, nyse_regular_session_close_time,
)


MOMENTUM_VERSION = "INTRADAY_MOMENTUM_V2"
BENCHMARK = "SPY"
MIN_VOLUME_SESSIONS = 3
MAX_VOLUME_SESSIONS = 4
COMPONENT_WEIGHTS = {"relative_strength": 0.40, "relative_volume": 0.25,
                     "trend": 0.25, "entry": 0.10}
SECTOR_PROXIES = {
    "technology": "XLK", "informationtechnology": "XLK",
    "healthcare": "XLV", "financialservices": "XLF", "financials": "XLF", "finance": "XLF",
    "consumercyclical": "XLY", "consumerdiscretionary": "XLY",
    "consumerdefensive": "XLP", "consumerstaples": "XLP",
    "communicationservices": "XLC", "communications": "XLC", "telecommunications": "XLC",
    "energy": "XLE", "industrials": "XLI", "industrial": "XLI",
    "basicmaterials": "XLB", "materials": "XLB", "utilities": "XLU", "realestate": "XLRE",
}


def sector_proxy(sector: str | None) -> str | None:
    return SECTOR_PROXIES.get("".join(c for c in str(sector or "").lower() if c.isalnum()))


def best_passed_orb(row: dict) -> dict | None:
    plans = [plan for plan in row.get("orb", []) if all(
        plan.get(key) == "PASS" for key in
        ("price_status", "position_status", "liquidity_status")
    )]
    return max(plans, key=lambda plan: (
        plan.get("score") or 0, -{"1m": 1, "5m": 5, "30m": 30}.get(plan.get("window"), 99),
    ), default=None)


def unavailable_momentum(status: str, reason: str) -> dict:
    return {"version": MOMENTUM_VERSION, "benchmark": BENCHMARK,
            "status": status, "reason": reason, "score": None, "rank": None,
            "as_of": None, "candidate": False}


def _number(value) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def assess_momentum(momentum: dict) -> dict:
    """Rate observed conditions independently of the relative rank and score."""
    rs = _number(momentum.get("rs_open_pp"))
    rvol = _number(momentum.get("rvol"))
    ratings = {
        "rs": "UNKNOWN" if rs is None else "GOOD" if rs >= 0.5 else "BAD" if rs < 0 else "NORMAL",
        "rvol": "UNKNOWN" if rvol is None else "GOOD" if rvol >= 1.5 else "BAD" if rvol < 1 else "NORMAL",
    }
    values = {key: _number(momentum.get(key)) for key in (
        "stock_return_open_percent", "vwap_distance_percent", "range_location_percent",
        "entry_extension_adr", "breakout_distance_percent",
    )}
    bad, normal = [], []
    extension = values["entry_extension_adr"]
    if extension is not None and abs(extension) > 0.5:
        bad.append("More than 0.5 ADR above entry" if extension > 0 else "More than 0.5 ADR below entry")
    for key, reason in (
        ("stock_return_open_percent", "Not advancing from the open"),
        ("vwap_distance_percent", "At or below VWAP"),
    ):
        if values[key] is not None and values[key] <= 0:
            bad.append(reason)
    if values["breakout_distance_percent"] is not None and values["breakout_distance_percent"] < 0:
        bad.append("Below daily breakout")
    if rs is not None:
        if rs < 0:
            bad.append("Lagging SPY")
        elif rs < 0.5:
            normal.append("RS below +0.5 pp")
    if rvol is not None:
        if rvol < 1:
            bad.append("Volume below normal")
        elif rvol < 1.5:
            normal.append("RVOL below 1.5x")
    location = values["range_location_percent"]
    if location is not None:
        if location < 50:
            bad.append("In lower half of today's range")
        elif location < 75:
            normal.append("Below top quarter of today's range")
    missing = rs is None or rvol is None or any(value is None for value in values.values())
    uncertain = missing or momentum.get("quality") != "FULL"
    if momentum.get("signals_version"):
        sector = _number(momentum.get("rs_sector_pp"))
        efficiency = _number(momentum.get("directional_efficiency"))
        vwap_adr = _number(momentum.get("vwap_distance_adr"))
        ratings.update(
            sector="UNKNOWN" if sector is None else "GOOD" if sector >= .5 else "BAD" if sector < 0 else "NORMAL",
            efficiency="UNKNOWN" if efficiency is None else "GOOD" if efficiency >= .4 else "BAD" if efficiency < .2 else "NORMAL",
            vwap="UNKNOWN" if values["vwap_distance_percent"] is None or vwap_adr is None else "BAD" if values["vwap_distance_percent"] < 0 or vwap_adr > .5 else "GOOD" if values["vwap_distance_percent"] > 0 else "NORMAL",
        )
        if sector is not None and sector < 0:
            bad.append("Lagging sector ETF")
        elif sector is not None and sector < .5:
            normal.append("Sector excess below +0.5 pp")
        if efficiency is not None and efficiency < .2:
            bad.append("Downward or choppy recent price action")
        elif efficiency is not None and efficiency < .4:
            normal.append("Recent upward progress is only moderate")
        if vwap_adr is not None and vwap_adr > .5:
            bad.append("More than 0.5 ADR above VWAP")
        missing = missing or sector is None or efficiency is None or vwap_adr is None
        uncertain = uncertain or missing or bool(momentum.get("efficiency_warming_up"))
    if bad:
        label = "Extended" if (extension is not None and extension > 0.5) or (_number(momentum.get("vwap_distance_adr")) or 0) > .5 else "Weak"
        tone, reasons = "BAD", bad + normal
    elif missing:
        label, tone, reasons = "Check data", "UNKNOWN", ["Required condition data missing", *normal]
    elif normal:
        label, tone, reasons = "Mixed", "NORMAL", normal
    elif uncertain:
        label, tone, reasons = "Confirm", "UNKNOWN", ["Strong reported metrics; confirm estimated data or early-session window"]
    else:
        label, tone, reasons = "Strong", "GOOD", ["Strength, volume, trend and entry checks passed"]
    return {"label": label, "tone": tone, "reasons": reasons, "metrics": ratings,
            "data_uncertain": uncertain}


def _bars(frame: pd.DataFrame, now: dt.datetime) -> pd.DataFrame:
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        return pd.DataFrame()
    frame = frame.copy()
    frame.index = frame.index.tz_convert(US_MARKET_ZONE)
    # Minute stamps identify the start of the bar. Ignore the forming minute.
    frame = frame[frame.index + pd.Timedelta(minutes=1) <= now].sort_index()
    return frame if not frame.index.has_duplicates else pd.DataFrame()


def _regular(frame: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[(frame.index.date == day) & (frame.index.time >= dt.time(9, 30))
                 & (frame.index.time < nyse_regular_session_close_time(day))]


def _clip(value: float) -> float:
    return max(0.0, min(100.0, value))


def _valid_observations(frame: pd.DataFrame, columns: list[str]) -> bool:
    if frame.empty or not set(columns) <= set(frame.columns):
        return False
    values = frame[columns].apply(pd.to_numeric, errors="coerce")
    return bool(values.notna().all().all() and values.map(math.isfinite).all().all()
                and all((values[column] >= 0 if column == "Volume" else values[column] > 0).all()
                        for column in columns))


def directional_efficiency(
    stock: pd.DataFrame, start: pd.Timestamp, cutoff: pd.Timestamp, *, horizon_minutes: int = 30,
) -> dict:
    """Signed net progress / total close-to-close travel for completed bars."""
    if horizon_minutes not in (5, 30):
        raise ValueError("Price-path efficiency horizon must be 5 or 30 minutes")
    minimum = 5
    minutes = min(horizon_minutes, int((cutoff - start).total_seconds() // 60) + 1)
    result = {"directional_efficiency": None, "efficiency_minutes": minutes,
              "efficiency_warming_up": minutes < horizon_minutes,
              "efficiency_reason": f"Waiting for {minimum} completed minute(s)"}
    if minutes < minimum:
        return result
    first = cutoff - pd.Timedelta(minutes=minutes - 1)
    # Use the preceding close as the anchor once the window rolls.
    anchor = first - pd.Timedelta(minutes=1) if first > start else start
    expected = pd.date_range(anchor, cutoff, freq="min")
    if not expected.isin(stock.index).all() or not _valid_observations(stock.loc[expected], ["Close"]):
        result["efficiency_reason"] = "Missing or invalid minute closes in efficiency window; gaps are not filled"
        return result
    closes = stock.loc[expected, "Close"].astype(float).tolist()
    if first == start:
        opened = _number(stock.loc[start, "Open"])
        if opened is None or opened <= 0:
            return result
        closes.insert(0, opened)
    travel = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
    result.update(directional_efficiency=max(-1., min(1., (closes[-1] - closes[0]) / travel)) if travel else 0.,
                  efficiency_reason="Signed net change / total absolute minute-close changes; "
                  + ("early-session window" if minutes < horizon_minutes
                     else f"last {horizon_minutes} completed minutes"))
    return result


def candle_efficiency(stock: pd.DataFrame, cutoff: pd.Timestamp) -> dict:
    """Signed body/range proxy for the last completed one-minute candle."""
    result = {"value": None, "minutes": 1, "warming_up": False, "estimated": True,
              "reason": "Missing or invalid completed one-minute candle"}
    if cutoff not in stock.index or not _valid_observations(stock.loc[[cutoff]], ["Open", "High", "Low", "Close"]):
        return result
    opened, high, low, closed = (float(stock.loc[cutoff, key]) for key in ("Open", "High", "Low", "Close"))
    if low > min(opened, closed) or high < max(opened, closed):
        return result
    result.update(value=round((closed - opened) / (high - low), 12) if high > low else 0.,
                  reason="One-minute candle estimate: (close - open) / (high - low); intraminute price path unavailable")
    return result


def additional_signals(stock: pd.DataFrame, sector: pd.DataFrame, result: dict, *,
                       start: pd.Timestamp, cutoff: pd.Timestamp, now: dt.datetime,
                       sector_symbol: str | None, sector_name: str | None, sector_failed: bool, adr: float) -> dict:
    signals = {"signals_version": 1, "sector_symbol": sector_symbol, "sector_name": sector_name,
               "rs_sector_pp": None, "sector_return_open_percent": None,
               "sector_reason": "Sector classification unavailable", "sector_as_of": None,
               "vwap_distance_adr": result["vwap_distance_percent"] / adr if result["vwap_distance_percent"] is not None else None}
    if sector_symbol:
        market = _regular(_bars(sector, now), now.date())
        signals["sector_reason"] = "Sector ETF unavailable at the same opening/latest minute"
        if not (sector_failed and not result["session_closed"]) and all(stamp in market.index for stamp in (start, cutoff)):
            endpoints = market.loc[[start, cutoff]]
            if _valid_observations(endpoints, ["Open", "Close"]):
                change = (float(endpoints.Close.iloc[-1]) / float(endpoints.Open.iloc[0]) - 1) * 100
                signals.update(rs_sector_pp=result["stock_return_open_percent"] - change,
                               sector_return_open_percent=change, sector_as_of=result["as_of"], sector_reason="")
    signals["efficiency_windows"] = {"1m": candle_efficiency(stock, cutoff)}
    for minutes in (5, 30):
        window = directional_efficiency(stock, start, cutoff, horizon_minutes=minutes)
        if minutes == 30:
            signals.update(window)
        signals["efficiency_windows"][f"{minutes}m"] = {
            "value": window["directional_efficiency"], "minutes": window["efficiency_minutes"],
            "warming_up": window["efficiency_warming_up"], "reason": window["efficiency_reason"],
            "estimated": False,
        }
    return signals


def evaluate_momentum(
    row: dict, frame: pd.DataFrame, benchmark: pd.DataFrame, *,
    now: dt.datetime, adr: float | None, stale_seconds: float = 180,
    benchmark_failed: bool = False,
    sector_benchmark: pd.DataFrame | None = None, sector_symbol: str | None = None,
    sector_name: str | None = None, sector_failed: bool = False,
) -> dict:
    """Compare synchronized completed RTH bars, excluding overnight gaps."""
    now = now.astimezone(US_MARKET_ZONE)
    day = now.date()
    close = dt.datetime.combine(day, nyse_regular_session_close_time(day), US_MARKET_ZONE)
    session_closed = now >= close
    if not is_nyse_trading_day(day) or now.time() < dt.time(9, 30):
        return unavailable_momentum("CLOSED", "Momentum ranking is available during the regular session")
    plan = best_passed_orb(row)
    if plan is None:
        return unavailable_momentum("NOT_PASSED", "Waiting for a passing ORB")
    if not session_closed and row.get("quote_status") != "CURRENT":
        return unavailable_momentum("STALE", "Stock quote is stale or unavailable")
    history, market = _bars(frame, now), _bars(benchmark, now)
    stock, market = _regular(history, day), _regular(market, day)
    if (benchmark_failed and not session_closed) or market.empty:
        return unavailable_momentum("UNAVAILABLE", "Current SPY minute bars unavailable")
    if stock.empty:
        return unavailable_momentum("UNAVAILABLE", "Completed stock minute bars unavailable")
    common = stock.index.intersection(market.index)
    if common.empty:
        return unavailable_momentum("UNAVAILABLE", "Stock and SPY have no synchronized minute bars")
    cutoff = common[-1]
    as_of = cutoff + pd.Timedelta(minutes=1)
    reference_time = close if session_closed else now
    if (reference_time - as_of).total_seconds() > stale_seconds:
        return unavailable_momentum("STALE", "Synchronized stock/SPY bars are stale")
    start = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 30), US_MARKET_ZONE))
    count = int((cutoff - start).total_seconds() // 60) + 1
    stock, market = stock.loc[:cutoff], market.loc[:cutoff]
    if count < 2:
        return unavailable_momentum("FORMING", "Waiting for two completed regular-session minutes")
    # A return needs a verified open and synchronized endpoint, not every
    # intervening minute. Never forward-fill prices or missing volume.
    if start not in stock.index or not _valid_observations(stock.loc[[start, cutoff]], ["Open", "Close"]):
        return unavailable_momentum("UNAVAILABLE", "Stock opening or latest price is unavailable")
    if start not in market.index or not _valid_observations(market.loc[[start, cutoff]], ["Open", "Close"]):
        return unavailable_momentum("UNAVAILABLE", "SPY opening or latest price is unavailable")
    adr, entry = _number(adr), _number(plan.get("execution_price"))
    breakout = _number(row.get("breakout_price"))
    if any(value is None or value <= 0 for value in (adr, entry, breakout)):
        return unavailable_momentum("UNAVAILABLE", "Daily ADR or planned ORB entry unavailable")
    prior_volumes = []
    prior_coverages = []
    prior_days = sorted({date for date in history.index.date if date < day}, reverse=True)
    for prior_day in prior_days[:MAX_VOLUME_SESSIONS]:
        if not is_nyse_trading_day(prior_day):
            continue
        prior = _regular(history, prior_day)
        prior_start = pd.Timestamp(dt.datetime.combine(prior_day, dt.time(9, 30), US_MARKET_ZONE))
        prior_cutoff = prior_start + pd.Timedelta(minutes=count - 1)
        # Bars after the comparison time establish that the history extends
        # through it. An early-close or truncated download is not a baseline.
        if prior.empty or prior_start not in prior.index or prior.index[-1] < prior_cutoff:
            continue
        prior = prior.loc[:prior_cutoff]
        if not _valid_observations(prior, ["Volume"]):
            continue
        volume = float(pd.to_numeric(prior.Volume).sum())
        if math.isfinite(volume) and volume > 0:
            prior_volumes.append(volume)
            prior_coverages.append(min(100.0, len(prior) / count * 100))
    stock = stock.apply(pd.to_numeric, errors="coerce")
    market = market.apply(pd.to_numeric, errors="coerce")
    price = float(stock.Close.iloc[-1])
    stock_return = (price / float(stock.Open.iloc[0]) - 1) * 100
    market_return = (float(market.Close.iloc[-1]) / float(market.Open.iloc[0]) - 1) * 100
    excess = stock_return - market_return
    coverage = min(100.0, len(stock) / count * 100)
    volume_valid = _valid_observations(stock, ["Volume"])
    volume = float(stock.Volume.sum()) if volume_valid else None
    volume_valid = volume is not None and math.isfinite(volume) and volume > 0
    rvol = volume / median(prior_volumes) if volume_valid and len(prior_volumes) >= MIN_VOLUME_SESSIONS else None
    vwap = None
    high = low = location = None
    if volume_valid and _valid_observations(stock, ["High", "Low", "Close"]):
        vwap = float((((stock.High + stock.Low + stock.Close) / 3) * stock.Volume).sum() / volume)
        high, low = float(stock.High.max()), float(stock.Low.min())
        location = _clip((price - low) / (high - low) * 100) if high > low else 0.0
    extension_adr = (price / entry - 1) * 100 / adr
    components = {
        "relative_strength": _clip(excess / adr * 200),
        "relative_volume": _clip((rvol - 1) * 50) if rvol is not None else None,
        "trend": ((100.0 if price > vwap else 0.0) * 0.5 + location * 0.5) if vwap else None,
        "entry": _clip(100 * (1 - abs(extension_adr) / 0.5)),
    }
    flags = []
    for condition, reason in (
        (stock_return <= 0, "Not advancing from the open"),
        (excess <= 0, "Underperforming SPY from the open"),
        (rvol is None, "RVOL unavailable: need valid volume and at least 3 prior sessions"),
        (rvol is not None and rvol < 1.5, "RVOL below 1.5x"),
        (vwap is None, "VWAP unavailable: reported bars or volume are invalid"),
        (vwap is not None and price <= vwap, "At or below VWAP"),
        (location is not None and location < 75, "Below the top quarter of today's range"),
        (price < breakout, "Below the daily breakout"),
        (extension_adr > 0.5, "More than 0.5 ADR above planned entry"),
        (extension_adr < -0.5, "More than 0.5 ADR below planned entry"),
    ):
        if condition:
            flags.append(reason)
    warnings = []
    if coverage < 100:
        warnings.append(f"Reported minute coverage {coverage:.1f}%; volume/VWAP are estimates")
    rvol_estimated = rvol is not None and (coverage < 100 or any(value < 100 for value in prior_coverages))
    if rvol_estimated:
        warnings.append("RVOL estimated from reported bars; missing minutes are not filled")
    available_weight = sum(weight for key, weight in COMPONENT_WEIGHTS.items() if components[key] is not None)
    missing = [key for key, value in components.items() if value is None]
    quality = "PARTIAL" if missing else "ESTIMATED" if warnings else "FULL"
    candidate = not flags and not warnings
    reasons = flags + warnings
    result = {
        "version": MOMENTUM_VERSION, "benchmark": BENCHMARK,
        "status": "PARTIAL" if missing else "LEADING" if candidate else "WATCH", "candidate": candidate,
        "reason": "; ".join(reasons) if reasons else "Leading SPY with volume support and a nearby planned entry",
        "score": round(sum(components[key] * weight for key, weight in COMPONENT_WEIGHTS.items()
                           if components[key] is not None) / available_weight, 1),
        "rank": None, "as_of": as_of.isoformat(), "session_date": day.isoformat(), "signal_price": price,
        "stock_return_open_percent": stock_return, "benchmark_return_open_percent": market_return,
        "rs_open_pp": excess, "rvol": rvol, "rvol_sessions": len(prior_volumes),
        "vwap": vwap, "vwap_distance_percent": (price / vwap - 1) * 100 if vwap else None,
        "range_location_percent": location, "high_distance_percent": (price / high - 1) * 100 if high else None,
        "entry_extension_adr": extension_adr, "entry_window": plan["window"],
        "breakout_distance_percent": (price / breakout - 1) * 100,
        "components": {key: round(value, 1) if value is not None else None for key, value in components.items()},
        "quality": quality, "missing_components": missing, "component_coverage_percent": round(available_weight * 100),
        "bar_coverage_percent": round(coverage, 1), "rvol_estimated": rvol_estimated,
        "rvol_bar_coverage_percent": round(min([coverage, *prior_coverages]), 1) if prior_coverages else None,
        "session_closed": session_closed,
    }
    result.update(additional_signals(stock, sector_benchmark if sector_benchmark is not None else pd.DataFrame(), result,
                                    start=start, cutoff=cutoff, now=now, sector_symbol=sector_symbol,
                                    sector_name=sector_name, sector_failed=sector_failed, adr=adr))
    result["assessment"] = assess_momentum(result)
    return result


def assign_momentum_ranks(rows: list[dict]) -> None:
    """Rank leading candidates first, then watch candidates; missing data stays unranked."""
    for row in rows:
        row.get("momentum", {})["rank"] = None
    ranked = [row for row in rows if row.get("momentum", {}).get("score") is not None]
    ranked.sort(key=lambda row: (
        not row["momentum"]["candidate"], -row["momentum"]["score"], row["symbol"],
    ))
    for rank, row in enumerate(ranked, 1):
        row["momentum"]["rank"] = rank
