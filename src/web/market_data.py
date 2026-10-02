from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .store import ValidationError, normalize_symbol


UTC = dt.timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
DEMO_CORE_SYMBOLS = (
    "AAPL",
    "AMD",
    "AMZN",
    "AVGO",
    "COIN",
    "CRWD",
    "GOOGL",
    "META",
    "MSFT",
    "NFLX",
    "NOW",
    "NVDA",
    "PLTR",
    "SHOP",
    "SMCI",
    "SNOW",
    "TSLA",
    "UBER",
    "VRT",
    "ZS",
)


class MarketDataUnavailable(RuntimeError):
    pass


class MarketDataSource(Protocol):
    source_name: str

    def scanner_snapshot(
        self, *, limit: int = 300, setup: str | None = None
    ) -> dict[str, Any]: ...

    def search(self, query: str, *, limit: int = 30) -> list[dict[str, Any]]: ...

    def chart_bundle(
        self, symbol: str, timeframe: str, *, daily_bars: int, hourly_months: int
    ) -> dict[str, Any]: ...

    def cache_revision(self) -> str: ...


def _demo_symbols() -> list[str]:
    generated = [f"Q{index:03d}" for index in range(1, 331)]
    return list(DEMO_CORE_SYMBOLS) + generated


def _seed(symbol: str, timeframe: str = "") -> int:
    digest = hashlib.sha256(f"{symbol}:{timeframe}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % (2**32)


def _latest_completed_session_date(now: dt.datetime | None = None) -> dt.date:
    current = (now or dt.datetime.now(UTC)).astimezone(NEW_YORK)
    day = current.date()
    if current.weekday() < 5 and current.time() < dt.time(16, 0):
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def _demo_frame(symbol: str, timeframe: str, count: int, index: pd.Index) -> pd.DataFrame:
    rng = np.random.default_rng(_seed(symbol, timeframe))
    base = 18.0 + (_seed(symbol) % 28000) / 100.0
    drift = 0.00035 if timeframe == "1D" else 0.00005
    returns = rng.normal(drift, 0.017 if timeframe == "1D" else 0.0045, count)
    close = base * np.exp(np.cumsum(returns))
    open_price = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, count))
    spread = np.abs(rng.normal(0.009 if timeframe == "1D" else 0.003, 0.003, count))
    high = np.maximum(open_price, close) * (1 + spread)
    low = np.minimum(open_price, close) * np.maximum(0.01, 1 - spread)
    volume = rng.integers(250_000, 9_000_000, count).astype(float)
    return pd.DataFrame(
        {
            "Open": open_price,
            "High": high,
            "Low": low,
            "Close": close,
            "Adj Close": close,
            "Volume": volume,
        },
        index=index,
    )


def _daily_index(count: int) -> pd.DatetimeIndex:
    end = _latest_completed_session_date()
    return pd.bdate_range(end=end, periods=count, tz="UTC")


def _hourly_index(months: int) -> pd.DatetimeIndex:
    end_day = _latest_completed_session_date()
    start_day = pd.Timestamp(end_day) - pd.DateOffset(months=months)
    values: list[pd.Timestamp] = []
    for day in pd.bdate_range(start=start_day, end=end_day):
        local_day = day.date()
        for hour in range(9, 16):
            minute = 30
            local = dt.datetime.combine(
                local_day, dt.time(hour, minute), tzinfo=NEW_YORK
            )
            values.append(pd.Timestamp(local.astimezone(UTC)))
    return pd.DatetimeIndex(values)


def _number(value: object, digits: int = 4) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return round(result, digits)


def _growth_percent(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    latest = _number(close.iloc[-1], 8)
    base = _number(close.iloc[-bars - 1], 8)
    if latest is None or base is None or base == 0:
        return None
    return round((latest / base - 1.0) * 100.0, 2)


def _daily_header_metrics(frame: pd.DataFrame) -> dict[str, float | None]:
    close = pd.to_numeric(frame["Close"], errors="coerce")
    high = pd.to_numeric(frame["High"], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    previous_close = close.shift(1).replace(0, np.nan)
    adr_series = ((high - low) / previous_close).replace(
        [np.inf, -np.inf], np.nan
    )
    adr_series = adr_series.rolling(20, min_periods=5).mean() * 100.0
    valid_adr = adr_series.dropna()
    return {
        "adr_20": round(float(valid_adr.iloc[-1]), 2) if not valid_adr.empty else None,
        "return_1m": _growth_percent(close, 21),
        "return_3m": _growth_percent(close, 63),
    }


def _profile(symbol: str) -> dict[str, Any]:
    sectors = (
        ("Technology", "Software"),
        ("Consumer Cyclical", "Internet Retail"),
        ("Industrials", "Electrical Equipment"),
        ("Healthcare", "Biotechnology"),
        ("Financial Services", "Capital Markets"),
    )
    sector, industry = sectors[_seed(symbol) % len(sectors)]
    return {
        "symbol": symbol,
        "company": f"{symbol} Demonstration Company",
        "exchange": "DEMO",
        "sector": sector,
        "industry": industry,
    }


def _alignment_number(value: object) -> str:
    number = _number(value, 1)
    if number is None:
        return "N/A"
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def _alignment_percent(value: object) -> str:
    number = _number(value, 6)
    return "N/A" if number is None else f"{number * 100:+.1f}%"


def _alignment_text(value: object) -> str:
    text = str(value or "").strip()
    return text or "N/A"


def _alignment_condition(values: object, name: str) -> str:
    if not isinstance(values, dict):
        return "N/A"
    conditions = values.get("conditions")
    if not isinstance(conditions, list):
        return "N/A"
    for condition in conditions:
        if not isinstance(condition, dict) or condition.get("name") != name:
            continue
        result = condition.get("result")
        return "Yes" if result is True else "No" if result is False else "N/A"
    return "N/A"


def _alignment_payload(
    values: dict[str, Any],
    calculation_details: dict[str, Any],
    *,
    stale: bool,
) -> dict[str, Any]:
    score_value = _number(values.get("leadership_score"), 4)
    score = int(math.floor(score_value + 0.5)) if score_value is not None else None
    score = min(100, max(0, score)) if score is not None else None
    market = calculation_details.get("market", {})
    segment = calculation_details.get("segment", {})
    sector = calculation_details.get("sector", {})
    industry = calculation_details.get("industry", {})
    metadata = calculation_details.get("metadata", {})
    peer_basis = {
        "industry": "Industry",
        "sector_fallback": "Sector fallback",
    }.get(str(values.get("peer_basis") or ""), "N/A")
    data_status = (
        "Stale"
        if stale
        else "Provisional (incomplete data)"
        if bool(values.get("is_provisional"))
        else "Complete"
    )
    detail_sections = [
        {
            "title": "Leadership",
            "rows": [
                ["Leadership Score", f"{score} / 100" if score is not None else "N/A"],
                ["Market RS", _alignment_number(values.get("market_rs"))],
                ["Market RS source", _alignment_text(values.get("market_rs_source"))],
                ["Industry Peer RS", _alignment_number(values.get("industry_peer_rs"))],
                ["Peer Group", _alignment_text(values.get("peer_group_name"))],
                ["Peer Count", str(int(values.get("peer_count") or 0)) if values.get("peer_count") else "N/A"],
                ["Peer Basis", peer_basis],
            ],
        },
        {
            "title": "Broad market",
            "rows": [
                ["Benchmark", _alignment_text(market.get("benchmark") or "SPY")],
                ["Close above SMA20", _alignment_condition(market, "close_above_sma20")],
                ["Close above SMA50", _alignment_condition(market, "close_above_sma50")],
                ["Five-day return", _alignment_percent(market.get("return_5d"))],
                ["State", _alignment_text(values.get("market_state")).title()],
            ],
        },
        {
            "title": "Market segment",
            "rows": [
                ["Segment", _alignment_text(values.get("segment_name"))],
                ["Proxy", _alignment_text(values.get("segment_proxy"))],
                ["Five-day return", _alignment_percent(segment.get("return_5d"))],
                ["SPY five-day return", _alignment_percent(segment.get("spy_return_5d"))],
                ["Close above SMA20", _alignment_condition(segment, "close_above_sma20")],
                ["State", _alignment_text(values.get("segment_state")).title()],
            ],
        },
        {
            "title": "Sector",
            "rows": [
                ["Sector", _alignment_text(values.get("sector_name"))],
                ["Proxy", _alignment_text(values.get("sector_proxy"))],
                ["Five-day return", _alignment_percent(sector.get("return_5d"))],
                ["Twenty-day percentile", _alignment_number(sector.get("performance_percentile_20d"))],
                ["Outperforming SPY", _alignment_condition(sector, "outperforming_spy_5d")],
                ["State", _alignment_text(values.get("sector_state")).title()],
            ],
        },
        {
            "title": "Industry",
            "rows": [
                ["Industry", _alignment_text(values.get("industry_name"))],
                ["Proxy/Index", _alignment_text(values.get("industry_proxy_or_index"))],
                ["Five-day return", _alignment_percent(industry.get("return_5d"))],
                ["Sector five-day return", _alignment_percent(industry.get("sector_return_5d"))],
                ["Twenty-day percentile", _alignment_number(industry.get("performance_percentile_20d"))],
                ["State", _alignment_text(values.get("industry_state")).title()],
            ],
        },
        {
            "title": "Metadata",
            "rows": [
                ["EOD as of", _alignment_text(values.get("as_of_date"))],
                ["Calculated at", _alignment_text(values.get("calculated_at"))],
                ["Feature version", _alignment_text(values.get("feature_version"))],
                ["Classification source", _alignment_text(values.get("classification_source"))],
                ["Data status", data_status],
                *(
                    [["Themes", ", ".join(str(item) for item in metadata["themes"])]]
                    if isinstance(metadata, dict)
                    and isinstance(metadata.get("themes"), list)
                    and metadata["themes"]
                    else []
                ),
            ],
        },
    ]
    return {
        "score": score,
        "leadership_label": _alignment_text(values.get("leadership_label")),
        "context_label": _alignment_text(values.get("context_label")),
        "states": {
            "MKT": str(values.get("market_state") or "UNKNOWN").upper(),
            "SEG": str(values.get("segment_state") or "UNKNOWN").upper(),
            "SEC": str(values.get("sector_state") or "UNKNOWN").upper(),
            "IND": str(values.get("industry_state") or "UNKNOWN").upper(),
        },
        "stale": bool(stale),
        "provisional": bool(values.get("is_provisional")),
        "details": detail_sections,
    }


def _demo_market_alignment(symbol: str, profile: dict[str, Any]) -> dict[str, Any]:
    score = _seed(symbol, "alignment-score") % 101
    leadership_label = "STRONG" if score >= 80 else "MODERATE" if score >= 60 else "WEAK"
    palette = ("GREEN", "YELLOW", "RED")
    states = {
        label: palette[_seed(symbol, f"alignment-{label}") % len(palette)]
        for label in ("MKT", "SEG", "SEC", "IND")
    }
    state_points = {"GREEN": 2, "YELLOW": 1, "RED": 0}
    points = sum(state_points[state] for state in states.values())
    context_label = "STRONG" if points >= 7 else "SUPPORTIVE" if points >= 5 else "MIXED" if points >= 3 else "WEAK"
    if states["MKT"] == "RED" and context_label in {"STRONG", "SUPPORTIVE"}:
        context_label = "MIXED"
    completed = _latest_completed_session_date()

    def conditions(state: str, names: tuple[str, str, str]) -> list[dict[str, Any]]:
        passed = {"GREEN": 3, "YELLOW": 2, "RED": 1}[state]
        return [
            {"name": name, "result": index < passed}
            for index, name in enumerate(names)
        ]

    details = {
        "market": {
            "benchmark": "SPY",
            "return_5d": ((_seed(symbol, "market-return") % 1200) - 600) / 10000,
            "conditions": conditions(states["MKT"], ("close_above_sma20", "close_above_sma50", "return_5d_positive")),
        },
        "segment": {
            "return_5d": ((_seed(symbol, "segment-return") % 1200) - 600) / 10000,
            "spy_return_5d": ((_seed(symbol, "market-return") % 1200) - 600) / 10000,
            "conditions": conditions(states["SEG"], ("close_above_sma20", "return_5d_positive", "outperforming_spy_5d")),
        },
        "sector": {
            "return_5d": ((_seed(symbol, "sector-return") % 1200) - 600) / 10000,
            "performance_percentile_20d": _seed(symbol, "sector-percentile") % 101,
            "conditions": conditions(states["SEC"], ("close_above_sma20", "outperforming_spy_5d", "performance_percentile_20d_at_least_70")),
        },
        "industry": {
            "return_5d": ((_seed(symbol, "industry-return") % 1200) - 600) / 10000,
            "sector_return_5d": ((_seed(symbol, "sector-return") % 1200) - 600) / 10000,
            "performance_percentile_20d": _seed(symbol, "industry-percentile") % 101,
        },
        "metadata": {"themes": ["DEMO"]},
    }
    values = {
        "leadership_score": float(score),
        "leadership_label": leadership_label,
        "context_label": context_label,
        "market_state": states["MKT"],
        "segment_state": states["SEG"],
        "sector_state": states["SEC"],
        "industry_state": states["IND"],
        "market_rs": float(_seed(symbol, "market-rs") % 101),
        "market_rs_source": "deterministic demo",
        "industry_peer_rs": float(_seed(symbol, "peer-rs") % 101),
        "peer_group_name": profile.get("industry"),
        "peer_count": 20,
        "peer_basis": "industry",
        "segment_name": "Demo segment",
        "segment_proxy": "SPY",
        "sector_name": profile.get("sector"),
        "sector_proxy": "SPY",
        "industry_name": profile.get("industry"),
        "industry_proxy_or_index": "Demo basket",
        "as_of_date": completed.isoformat(),
        "calculated_at": f"{completed.isoformat()}T21:00:00+00:00",
        "feature_version": "DEMO-1.0",
        "classification_source": "deterministic demo",
        "is_provisional": False,
    }
    return _alignment_payload(values, details, stale=False)


def _demo_earnings(index: pd.DatetimeIndex) -> list[dict[str, Any]]:
    sessions = pd.DatetimeIndex(index).normalize().unique().sort_values()
    events: list[dict[str, Any]] = []
    for position, session in enumerate(sessions[20::63]):
        estimated_eps = 1.0
        reported_eps = 0.8 if position % 4 == 1 else 1.2
        events.append(
            {
                "date": pd.Timestamp(session).date().isoformat(),
                "timing": "AMC" if position % 2 == 0 else "BMO",
                "status": "REPORTED",
                "growth_status": "NORMAL" if position % 3 else "TURNAROUND",
                "estimated": False,
                "reported_eps": reported_eps,
                "estimated_eps": estimated_eps,
                "eps_surprise_pct": (reported_eps - estimated_eps) * 100.0,
                "eps_yoy_growth_pct": 20.0 if reported_eps > estimated_eps else -20.0,
            }
        )
    if len(sessions):
        next_session = pd.bdate_range(
            start=pd.Timestamp(sessions[-1]) + pd.Timedelta(days=1), periods=20
        )[-1]
        events.append(
            {
                "date": next_session.date().isoformat(),
                "timing": "AMC",
                "status": "EXPECTED",
                "growth_status": "MISSING",
                "estimated": True,
            }
        )
    return events


def _bundle_from_frame(
    *,
    symbol: str,
    timeframe: str,
    frame: pd.DataFrame,
    source: str,
    requested_count: int | None,
    profile: dict[str, Any],
    earnings: list[dict[str, Any]] | None = None,
    adjustment_mode: str = "unadjusted",
    session_policy: str = "regular-hours",
    benchmark_close: pd.Series | None = None,
    market_alignment: dict[str, Any] | None = None,
    header_metrics: dict[str, float | None] | None = None,
    source_revision: str = "",
) -> dict[str, Any]:
    if frame.empty:
        raise MarketDataUnavailable(f"{timeframe} history is unavailable for {symbol}")
    frame = frame.copy()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    close = pd.to_numeric(frame["Close"], errors="coerce")
    ema_values = {
        span: close.ewm(span=span, adjust=False).mean() for span in (10, 20, 50)
    }
    relative_strength = pd.Series(index=frame.index, dtype=float)
    relative_sma_50 = pd.Series(index=frame.index, dtype=float)
    percent_change = close.pct_change(fill_method=None) * 100.0
    supplied_metrics = dict(header_metrics or {})
    calculated_metrics: dict[str, float | None] = {}
    if timeframe == "1D":
        calculated_metrics = _daily_header_metrics(frame)
    display_metrics = {
        key: calculated_metrics.get(key)
        if calculated_metrics.get(key) is not None
        else _number(supplied_metrics.get(key), 2)
        for key in ("adr_20", "return_1m", "return_3m")
    }
    if benchmark_close is not None:
        benchmark = pd.to_numeric(benchmark_close, errors="coerce").reindex(frame.index)
        benchmark = benchmark.ffill().replace(0, np.nan)
        relative_strength = close / benchmark
        relative_sma_50 = relative_strength.rolling(50, min_periods=1).mean()
    if requested_count:
        frame = frame.tail(requested_count)
        ema_values = {span: values.reindex(frame.index) for span, values in ema_values.items()}
        relative_strength = relative_strength.reindex(frame.index)
        relative_sma_50 = relative_sma_50.reindex(frame.index)
        percent_change = percent_change.reindex(frame.index)

    valid_relative = relative_strength.replace([np.inf, -np.inf], np.nan).dropna()
    if not valid_relative.empty and float(valid_relative.iloc[0]) != 0:
        relative_anchor = float(valid_relative.iloc[0])
        relative_strength = (relative_strength / relative_anchor - 1.0) * 100.0
        relative_sma_50 = (relative_sma_50 / relative_anchor - 1.0) * 100.0
    else:
        relative_strength = pd.Series(index=frame.index, dtype=float)
        relative_sma_50 = pd.Series(index=frame.index, dtype=float)

    bars: list[dict[str, Any]] = []
    volume: list[dict[str, Any]] = []
    indicators: dict[str, list[dict[str, Any]]] = {
        "ema10": [],
        "ema20": [],
        "ema50": [],
        "relative_strength": [],
        "rs_sma50": [],
        "rs_regime": [],
        "rs_markers": [],
    }
    index = pd.DatetimeIndex(frame.index)
    utc_index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    if timeframe == "1D":
        time_values: list[str | int] = utc_index.strftime("%Y-%m-%d").tolist()
    else:
        time_values = (utc_index.asi8 // 1_000_000_000).tolist()

    opens = pd.to_numeric(frame["Open"], errors="coerce").to_numpy(dtype=float)
    highs = pd.to_numeric(frame["High"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(frame["Low"], errors="coerce").to_numpy(dtype=float)
    closes = pd.to_numeric(frame["Close"], errors="coerce").to_numpy(dtype=float)
    volume_source = frame.get("Volume", pd.Series(0.0, index=frame.index))
    volumes = pd.to_numeric(volume_source, errors="coerce").to_numpy(dtype=float)
    ema_arrays = {
        span: values.to_numpy(dtype=float) for span, values in ema_values.items()
    }
    relative_values = relative_strength.to_numpy(dtype=float)
    relative_sma_values = relative_sma_50.to_numpy(dtype=float)
    percent_changes = percent_change.to_numpy(dtype=float)

    for position, time_value in enumerate(time_values):
        price_values = (
            opens[position],
            highs[position],
            lows[position],
            closes[position],
        )
        if not np.isfinite(price_values).all():
            continue
        item = {
            "time": time_value,
            "open": round(float(opens[position]), 4),
            "high": round(float(highs[position]), 4),
            "low": round(float(lows[position]), 4),
            "close": round(float(closes[position]), 4),
        }
        bars.append(item)
        raw_volume = volumes[position]
        volume.append(
            {
                "time": time_value,
                "value": max(0.0, round(float(raw_volume), 0))
                if math.isfinite(raw_volume)
                else 0.0,
                "color": "#24b58b66"
                if closes[position] >= opens[position]
                else "#f05a6766",
            }
        )
        for span in (10, 20, 50):
            value = ema_arrays[span][position]
            if math.isfinite(value):
                indicators[f"ema{span}"].append(
                    {"time": time_value, "value": round(float(value), 4)}
                )
        relative_value = relative_values[position]
        relative_sma_value = relative_sma_values[position]
        if math.isfinite(relative_value):
            indicators["relative_strength"].append(
                {
                    "time": time_value,
                    "value": round(float(relative_value), 4),
                    "color": "#22c55e" if relative_value >= 0 else "#ef5350",
                }
            )
            if math.isfinite(relative_sma_value):
                indicators["rs_sma50"].append(
                    {"time": time_value, "value": round(float(relative_sma_value), 4)}
                )
                indicators["rs_regime"].append(
                    {
                        "time": time_value,
                        "value": 1,
                        "color": "rgba(34, 197, 94, 0.18)"
                        if relative_value >= relative_sma_value
                        else "rgba(239, 68, 68, 0.18)",
                    }
                )
            daily_change = percent_changes[position]
            if math.isfinite(daily_change) and abs(daily_change) >= 4:
                indicators["rs_markers"].append(
                    {
                        "time": time_value,
                        "position": "aboveBar" if daily_change > 0 else "belowBar",
                        "color": "#22c55e" if daily_change > 0 else "#ef5350",
                        "shape": "circle",
                        "text": f"{daily_change:+.0f}%",
                    }
                )

    if not bars:
        raise MarketDataUnavailable(f"{timeframe} history contains no valid bars for {symbol}")
    start = pd.Timestamp(frame.index[0])
    end = pd.Timestamp(frame.index[-1])
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    return {
        "schema_version": 6,
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "READY",
        "bars": bars,
        "volume": volume,
        "indicators": indicators,
        "earnings": earnings or [],
        "profile": profile,
        "context": {
            "relative_strength": round(40 + (_seed(symbol) % 6000) / 100.0, 1),
            **display_metrics,
            "label": "DEMO context" if source == "DEMO" else "stored context",
        },
        "market_alignment": market_alignment,
        "coverage": {
            "requested": requested_count,
            "actual_start": start.isoformat(),
            "actual_end": end.isoformat(),
            "bar_count": len(bars),
            "source": source,
            "source_revision": str(source_revision or ""),
            "adjustment_mode": adjustment_mode,
            "session_policy": session_policy,
            "latest_completed_bar": end.isoformat(),
            "unfinished_bar": None,
            "completeness": "COMPLETE"
            if requested_count is None or len(bars) >= requested_count
            else "PARTIAL",
        },
    }


class DemoMarketDataSource:
    source_name = "DEMO"

    def __init__(self) -> None:
        self.symbols = _demo_symbols()

    def health(self) -> dict[str, Any]:
        return {"state": "DEMO", "source": self.source_name}

    def cache_revision(self) -> str:
        return "demo-v1"

    def scanner_snapshot(
        self, *, limit: int = 300, setup: str | None = None
    ) -> dict[str, Any]:
        session = _latest_completed_session_date().isoformat()
        rows = []
        for rank, symbol in enumerate(self.symbols[: max(1, limit)], start=1):
            profile = _profile(symbol)
            rows.append(
                {
                    "rank": rank,
                    "symbol": symbol,
                    "name": profile["company"],
                    "price": round(18 + (_seed(symbol) % 28000) / 100, 2),
                    "score": round(100 - ((rank - 1) % 100) * 0.61, 2),
                    "rs_score_252": round(99 - ((rank - 1) % 90) * 0.8, 1),
                    "return_1m": round(((_seed(symbol, "ret") % 3000) - 1000) / 100, 2),
                    "setup": "DEMO — deterministic fixtures",
                }
            )
        return {
            "rows": rows,
            "setup": "DEMO — deterministic fixtures",
            "available_setups": ["DEMO — deterministic fixtures"],
            "total_matches": len(rows),
            "source": "DEMO",
            "snapshot_date": session,
            "expected_snapshot_date": session,
            "stale_market_sessions": 0,
            "freshness": "DEMO",
        }

    def search(self, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
        query = query.strip().upper()
        if not query:
            return []
        results = []
        for symbol in self.symbols:
            profile = _profile(symbol)
            if query in symbol or query in profile["company"].upper():
                results.append(profile)
            if len(results) >= limit:
                break
        return results

    def chart_bundle(
        self, symbol: str, timeframe: str, *, daily_bars: int, hourly_months: int
    ) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        if symbol not in self.symbols:
            raise MarketDataUnavailable(f"{symbol} is outside the DEMO universe")
        timeframe = timeframe.strip().upper()
        if timeframe == "1D":
            warmup_count = daily_bars + 250
            index = _daily_index(warmup_count)
            frame = _demo_frame(symbol, timeframe, len(index), index)
            benchmark_close = _demo_frame("SPY", timeframe, len(index), index)["Close"]
            requested = daily_bars
            header_metrics = None
        elif timeframe == "1H":
            index = _hourly_index(hourly_months)
            warmup = pd.bdate_range(end=index[0] - pd.Timedelta(days=1), periods=60)
            warmup_values: list[pd.Timestamp] = []
            for day in warmup:
                for hour in range(9, 16):
                    local = dt.datetime.combine(
                        day.date(), dt.time(hour, 30), tzinfo=NEW_YORK
                    )
                    warmup_values.append(pd.Timestamp(local.astimezone(UTC)))
            all_index = pd.DatetimeIndex(warmup_values).append(index)
            frame = _demo_frame(symbol, timeframe, len(all_index), all_index)
            frame = frame.loc[index[0] :]
            benchmark_close = _demo_frame(
                "SPY", timeframe, len(all_index), all_index
            ).loc[index[0] :, "Close"]
            requested = None
            daily_metric_index = _daily_index(daily_bars + 250)
            daily_metric_frame = _demo_frame(
                symbol, "1D", len(daily_metric_index), daily_metric_index
            )
            header_metrics = _daily_header_metrics(daily_metric_frame)
        else:
            raise ValidationError("timeframe must be 1D or 1H")
        return _bundle_from_frame(
            symbol=symbol,
            timeframe=timeframe,
            frame=frame,
            source="DEMO",
            requested_count=requested,
            profile=_profile(symbol),
            benchmark_close=benchmark_close,
            earnings=_demo_earnings(pd.DatetimeIndex(frame.index)),
            market_alignment=_demo_market_alignment(symbol, _profile(symbol)),
            header_metrics=header_metrics,
            source_revision=self.cache_revision(),
        )


class ReadOnlyMirrorMarketDataSource:
    source_name = "LOCAL_SQLITE_MIRROR"

    def __init__(
        self,
        path: str | Path,
        *,
        scanner_setups_path: str | Path | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise MarketDataUnavailable(f"SQLite mirror does not exist: {self.path}")
        self.scanner_setups_path = (
            Path(scanner_setups_path).expanduser().resolve()
            if scanner_setups_path
            else None
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=8.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def cache_revision(self) -> str:
        parts: list[str] = []
        for candidate in (self.path, Path(f"{self.path}-wal")):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            parts.append(f"{candidate.name}:{stat.st_mtime_ns}:{stat.st_size}")
        return "|".join(parts)

    def _tables(self, connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    def health(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
            with self._connect() as connection:
                tables = self._tables(connection)
                snapshot_date = None
                if "scanner_metric_snapshots" in tables:
                    row = connection.execute(
                        "SELECT MAX(snapshot_date) FROM scanner_metric_snapshots "
                        "WHERE metric_count>0"
                    ).fetchone()
                    snapshot_date = str(row[0])[:10] if row and row[0] else None
                elif "scanner_metrics" in tables:
                    row = connection.execute(
                        "SELECT MAX(date) FROM scanner_metrics WHERE price_history_days>=1"
                    ).fetchone()
                    snapshot_date = str(row[0])[:10] if row and row[0] else None
            expected = _latest_completed_session_date().isoformat()
            return {
                "state": "AVAILABLE",
                "source": self.source_name,
                "snapshot_date": snapshot_date,
                "expected_snapshot_date": expected,
                "freshness": "CURRENT" if snapshot_date == expected else "STALE",
                "modified_at": dt.datetime.fromtimestamp(
                    stat.st_mtime, tz=UTC
                ).isoformat(),
            }
        except (OSError, sqlite3.Error) as exc:
            return {
                "state": "UNAVAILABLE",
                "source": self.source_name,
                "reason": f"Mirror health check failed ({type(exc).__name__})",
            }

    def _scanner_setups(self) -> dict[str, dict[str, Any]]:
        path = self.scanner_setups_path
        if path is None:
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MarketDataUnavailable(
                "Saved scanner setups are unavailable on the PC"
            ) from exc
        raw = payload.get("setups", payload) if isinstance(payload, dict) else {}
        setups = {
            str(name).strip(): value
            for name, value in raw.items()
            if str(name).strip() and isinstance(value, dict)
        }
        if not setups:
            raise MarketDataUnavailable("No saved scanner setup is available")
        return setups

    @staticmethod
    def _setup_rules(setup: dict[str, Any]) -> list[dict[str, Any]]:
        rules = [row for row in setup.get("rules", []) if isinstance(row, dict)]
        if rules:
            return rules
        return [
            {"attribute": "volume", "operator": ">=", "threshold": setup.get("min_volume", 40000.0)},
            {"attribute": "dollar_volume", "operator": ">=", "threshold": setup.get("min_dollar_volume", 35000.0)},
            {"attribute": "adr_20", "operator": ">=", "threshold": setup.get("min_adr", 2.4)},
            {"attribute": "growth_rank_1m", "operator": ">=", "threshold": setup.get("min_growth_rank", 97.04)},
            {"attribute": "trend_intensity", "operator": ">=", "threshold": setup.get("min_trend_intensity", 90.0)},
        ]

    @staticmethod
    def _rule_threshold(value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        text = str(value).strip()
        if text.lower() in {"true", "yes"}:
            return 1
        if text.lower() in {"false", "no"}:
            return 0
        try:
            return float(text)
        except (TypeError, ValueError):
            return text

    @staticmethod
    def _score_expression(columns: set[str]) -> str:
        def coalesce(*names: str) -> str:
            available = [f'm."{name}"' for name in names if name in columns]
            if not available:
                return "0.0"
            return f"COALESCE({', '.join(available + ['0.0'])})"

        growth = coalesce("growth_rank", "growth_rank_1m")
        trend = coalesce("trend_intensity")
        adr = coalesce("adr", "adr_20")
        return f"((({growth}) / 100.0) + (({trend}) / 100.0) + (({adr}) / 5.0)) / 3.0"

    def scanner_snapshot(
        self, *, limit: int = 300, setup: str | None = None
    ) -> dict[str, Any]:
        with self._connect() as connection:
            tables = self._tables(connection)
            if "scanner_metrics" not in tables:
                raise MarketDataUnavailable("scanner_metrics is absent from the mirror")
            if "scanner_metric_snapshots" in tables:
                snapshot = connection.execute(
                    """
                    SELECT snapshot_date, completed_at FROM scanner_metric_snapshots
                    WHERE metric_count>0 ORDER BY snapshot_date DESC LIMIT 1
                    """
                ).fetchone()
                snapshot_date = str(snapshot[0]) if snapshot else None
            else:
                snapshot = connection.execute(
                    "SELECT MAX(date) FROM scanner_metrics WHERE price_history_days>=1"
                ).fetchone()
                snapshot_date = str(snapshot[0]) if snapshot and snapshot[0] else None
            if not snapshot_date:
                raise MarketDataUnavailable("No completed scanner snapshot is available")
            has_profiles = "stock_profiles" in tables
            join = (
                "LEFT JOIN stock_profiles p ON p.symbol=m.symbol"
                if has_profiles
                else ""
            )
            name = "COALESCE(p.company_name, m.symbol)" if has_profiles else "m.symbol"
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(scanner_metrics)")
            }
            setups = self._scanner_setups()
            selected_setup = ""
            total_matches: int | None = None
            where_sql = "m.date=? AND m.price_history_days>=1"
            parameters: list[Any] = [snapshot_date]
            score_sql = "COALESCE(m.score, 0.0)"
            if setups:
                available_setups = list(setups)
                requested = str(setup or available_setups[0]).strip()
                selected_setup = next(
                    (name for name in available_setups if name.casefold() == requested.casefold()),
                    "",
                )
                if not selected_setup:
                    raise ValidationError(f"Unknown scanner setup: {requested}")
                operators = {">": ">", "<": "<", "==": "=", "=": "=", ">=": ">=", "<=": "<=", "!=": "!="}
                clauses: list[str] = []
                for rule in self._setup_rules(setups[selected_setup]):
                    attribute = str(rule.get("attribute") or "").strip()
                    operator = operators.get(str(rule.get("operator") or ">=").strip())
                    if not attribute or operator is None:
                        raise ValidationError(
                            f"Invalid rule in scanner setup {selected_setup}"
                        )
                    if attribute == "name" and has_profiles:
                        expression = "p.company_name"
                    elif attribute in columns:
                        expression = f'm."{attribute}"'
                    else:
                        raise ValidationError(
                            f"Scanner setup {selected_setup} uses unavailable metric {attribute}"
                        )
                    clauses.append(f"{expression} {operator} ?")
                    parameters.append(self._rule_threshold(rule.get("threshold", "")))
                if clauses:
                    where_sql += " AND " + " AND ".join(clauses)
                score_sql = self._score_expression(columns)
                total_matches = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM scanner_metrics m {join} WHERE {where_sql}",
                        tuple(parameters),
                    ).fetchone()[0]
                )
            else:
                available_setups = []
            secondary_order = (
                "m.growth_rank_1m DESC, " if "growth_rank_1m" in columns else ""
            )
            rows = connection.execute(
                f"""
                SELECT m.symbol, {name} AS name, m.price, {score_sql} AS score,
                       m.rs_score_252, m.return_1m
                FROM scanner_metrics m {join}
                WHERE {where_sql}
                ORDER BY score DESC, {secondary_order}m.symbol LIMIT ?
                """,
                tuple(parameters + [int(limit)]),
            ).fetchall()
        values = []
        for rank, row in enumerate(rows, start=1):
            values.append(
                {
                    "rank": rank,
                    "symbol": str(row["symbol"]),
                    "name": str(row["name"]),
                    "price": _number(row["price"], 2),
                    "score": _number(row["score"], 2),
                    "rs_score_252": _number(row["rs_score_252"], 1),
                    "return_1m": _number(row["return_1m"], 2),
                    "setup": selected_setup or "Stored scanner snapshot",
                }
            )
        snapshot_day = snapshot_date[:10]
        expected = _latest_completed_session_date()
        try:
            actual = dt.date.fromisoformat(snapshot_day)
            stale_days = max(0, (expected - actual).days)
        except ValueError:
            stale_days = None
        return {
            "rows": values,
            "setup": selected_setup or "Stored scanner snapshot",
            "available_setups": available_setups,
            "total_matches": total_matches if total_matches is not None else len(values),
            "source": self.source_name,
            "snapshot_date": snapshot_day,
            "expected_snapshot_date": expected.isoformat(),
            "stale_calendar_days": stale_days,
            "freshness": "CURRENT" if stale_days == 0 else "STALE",
        }

    def search(self, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
        query = query.strip().upper()
        if not query:
            return []
        with self._connect() as connection:
            tables = self._tables(connection)
            if "stock_profiles" in tables:
                rows = connection.execute(
                    """
                    SELECT symbol, company_name, exchange, sector_name, industry_name
                    FROM stock_profiles
                    WHERE UPPER(symbol) LIKE ? OR UPPER(company_name) LIKE ?
                    ORDER BY CASE WHEN UPPER(symbol)=? THEN 0 ELSE 1 END, symbol
                    LIMIT ?
                    """,
                    (f"%{query}%", f"%{query}%", query, int(limit)),
                ).fetchall()
                return [
                    {
                        "symbol": str(row["symbol"]),
                        "company": str(row["company_name"] or row["symbol"]),
                        "exchange": str(row["exchange"] or "UNAVAILABLE"),
                        "sector": str(row["sector_name"] or "UNAVAILABLE"),
                        "industry": str(row["industry_name"] or "UNAVAILABLE"),
                    }
                    for row in rows
                ]
            if "price_history" not in tables:
                return []
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM price_history WHERE symbol LIKE ? ORDER BY symbol LIMIT ?",
                (f"%{query}%", int(limit)),
            ).fetchall()
        return [
            {
                "symbol": str(row[0]),
                "company": str(row[0]),
                "exchange": "UNAVAILABLE",
                "sector": "UNAVAILABLE",
                "industry": "UNAVAILABLE",
            }
            for row in rows
        ]

    def _profile(self, connection: sqlite3.Connection, symbol: str) -> dict[str, Any]:
        if "stock_profiles" not in self._tables(connection):
            return {
                "symbol": symbol,
                "company": symbol,
                "exchange": "UNAVAILABLE",
                "sector": "UNAVAILABLE",
                "industry": "UNAVAILABLE",
            }
        row = connection.execute(
            """
            SELECT company_name, exchange, sector_name, industry_name
            FROM stock_profiles WHERE symbol=?
            """,
            (symbol,),
        ).fetchone()
        return {
            "symbol": symbol,
            "company": str(row[0] or symbol) if row else symbol,
            "exchange": str(row[1] or "UNAVAILABLE") if row else "UNAVAILABLE",
            "sector": str(row[2] or "UNAVAILABLE") if row else "UNAVAILABLE",
            "industry": str(row[3] or "UNAVAILABLE") if row else "UNAVAILABLE",
        }

    def _header_metrics(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        tables: set[str],
    ) -> dict[str, float | None]:
        metrics: dict[str, float | None] = {}
        if "scanner_metrics" in tables:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(scanner_metrics)"
                ).fetchall()
            }
            selected = [
                column
                for column in ("adr_20", "return_1m", "return_3m")
                if column in columns
            ]
            if selected:
                row = connection.execute(
                    f"SELECT {', '.join(selected)} FROM scanner_metrics "
                    "WHERE symbol=? ORDER BY date DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
                if row is not None:
                    metrics.update(
                        {
                            column: _number(row[column], 2)
                            for column in selected
                        }
                    )
        required = {"adr_20", "return_1m", "return_3m"}
        if any(metrics.get(key) is None for key in required) and "price_history" in tables:
            rows = connection.execute(
                """
                SELECT date, high, low, close
                FROM price_history
                WHERE symbol=? AND interval='1d'
                ORDER BY date DESC LIMIT 90
                """,
                (symbol,),
            ).fetchall()
            if rows:
                ordered = list(reversed(rows))
                frame = pd.DataFrame(
                    {
                        "High": [row["high"] for row in ordered],
                        "Low": [row["low"] for row in ordered],
                        "Close": [row["close"] for row in ordered],
                    },
                    index=pd.to_datetime(
                        [row["date"] for row in ordered], utc=True
                    ),
                )
                calculated = _daily_header_metrics(frame)
                for key in required:
                    if metrics.get(key) is None:
                        metrics[key] = calculated.get(key)
        return metrics

    def _market_alignment(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        tables: set[str],
    ) -> dict[str, Any] | None:
        required = {"stock_market_alignment_daily", "market_alignment_batches"}
        if not required.issubset(tables):
            return None
        row = connection.execute(
            """
            SELECT snapshot.*
            FROM stock_market_alignment_daily snapshot
            JOIN market_alignment_batches batch
              ON batch.as_of_date=snapshot.as_of_date
             AND batch.feature_version=snapshot.feature_version
             AND UPPER(batch.status)='PUBLISHED'
            WHERE snapshot.symbol=?
            ORDER BY snapshot.as_of_date DESC, snapshot.feature_version DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        try:
            details = json.loads(str(values.get("calculation_details_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        as_of_text = str(values.get("as_of_date") or "")[:10]
        try:
            stale = dt.date.fromisoformat(as_of_text) < _latest_completed_session_date()
        except ValueError:
            stale = True
        return _alignment_payload(values, details, stale=stale)

    def chart_bundle(
        self, symbol: str, timeframe: str, *, daily_bars: int, hourly_months: int
    ) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        timeframe = timeframe.strip().upper()
        with self._connect() as connection:
            tables = self._tables(connection)
            profile = self._profile(connection, symbol)
            market_alignment = self._market_alignment(connection, symbol, tables)
            header_metrics = self._header_metrics(connection, symbol, tables)
            benchmark_rows: list[sqlite3.Row] = []
            if timeframe == "1D":
                if "price_history" not in tables:
                    raise MarketDataUnavailable("price_history is absent from the mirror")
                rows = connection.execute(
                    """
                    SELECT date, open, high, low, close, adj_close, volume
                    FROM price_history
                    WHERE symbol=? AND interval='1d'
                    ORDER BY date DESC LIMIT ?
                    """,
                    (symbol, int(daily_bars) + 250),
                ).fetchall()
                benchmark_rows = connection.execute(
                    """
                    SELECT date, close FROM price_history
                    WHERE symbol='SPY' AND interval='1d'
                    ORDER BY date DESC LIMIT ?
                    """,
                    (int(daily_bars) + 250,),
                ).fetchall()
                time_column = "date"
                requested = daily_bars
                source = self.source_name
            elif timeframe == "1H":
                if "hourly_price_history" not in tables:
                    raise MarketDataUnavailable(
                        "hourly_price_history is absent from the mirror"
                    )
                start = (
                    pd.Timestamp.now(tz="UTC") - pd.DateOffset(months=hourly_months)
                ).tz_localize(None)
                rows = connection.execute(
                    """
                    SELECT timestamp, open, high, low, close, adj_close, volume, source
                    FROM hourly_price_history
                    WHERE symbol=? AND timestamp>=?
                    ORDER BY timestamp
                    """,
                    (symbol, start.isoformat(sep=" ")),
                ).fetchall()
                benchmark_rows = connection.execute(
                    """
                    SELECT timestamp, close FROM hourly_price_history
                    WHERE symbol='SPY' AND timestamp>=?
                    ORDER BY timestamp
                    """,
                    (start.isoformat(sep=" "),),
                ).fetchall()
                time_column = "timestamp"
                requested = None
                sources = {str(row["source"]) for row in rows if row["source"]}
                source = (
                    f"{self.source_name}:{','.join(sorted(sources))}"
                    if sources
                    else self.source_name
                )
            else:
                raise ValidationError("timeframe must be 1D or 1H")

            earnings: list[dict[str, Any]] = []
            if "earnings_events" in tables:
                earnings_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(earnings_events)"
                    ).fetchall()
                }
                event_status = (
                    "event_status" if "event_status" in earnings_columns
                    else "'REPORTED'"
                )
                estimated = (
                    "is_date_estimated" if "is_date_estimated" in earnings_columns
                    else "0"
                )
                reported_eps = (
                    "reported_eps" if "reported_eps" in earnings_columns else "NULL"
                )
                estimated_eps = (
                    "estimated_eps" if "estimated_eps" in earnings_columns else "NULL"
                )
                surprise_pct = (
                    "eps_surprise_pct"
                    if "eps_surprise_pct" in earnings_columns
                    else "NULL"
                )
                growth_pct = (
                    "eps_yoy_growth_pct"
                    if "eps_yoy_growth_pct" in earnings_columns
                    else "NULL"
                )
                earnings_rows = connection.execute(
                    f"""
                    SELECT report_date, report_timing, {event_status} AS event_status,
                           eps_growth_status, {estimated} AS is_date_estimated,
                           {reported_eps} AS reported_eps,
                           {estimated_eps} AS estimated_eps,
                           {surprise_pct} AS eps_surprise_pct,
                           {growth_pct} AS eps_yoy_growth_pct
                    FROM earnings_events WHERE symbol=? ORDER BY report_date
                    """,
                    (symbol,),
                ).fetchall()
                earnings = [
                    {
                        "date": str(row[0]),
                        "timing": str(row[1] or "UNKNOWN"),
                        "status": str(row[2] or "REPORTED"),
                        "growth_status": str(row[3] or "UNAVAILABLE"),
                        "estimated": bool(row[4]),
                        "reported_eps": row[5],
                        "estimated_eps": row[6],
                        "eps_surprise_pct": row[7],
                        "eps_yoy_growth_pct": row[8],
                    }
                    for row in earnings_rows
                ]
        if not rows:
            raise MarketDataUnavailable(f"{timeframe} history is unavailable for {symbol}")
        ordered = list(reversed(rows)) if timeframe == "1D" else list(rows)
        index = pd.to_datetime([row[time_column] for row in ordered], utc=True)
        frame = pd.DataFrame(
            {
                "Open": [row["open"] for row in ordered],
                "High": [row["high"] for row in ordered],
                "Low": [row["low"] for row in ordered],
                "Close": [row["close"] for row in ordered],
                "Adj Close": [row["adj_close"] for row in ordered],
                "Volume": [row["volume"] for row in ordered],
            },
            index=index,
        )
        benchmark_close = None
        if benchmark_rows:
            benchmark_ordered = (
                list(reversed(benchmark_rows))
                if timeframe == "1D"
                else list(benchmark_rows)
            )
            benchmark_index = pd.to_datetime(
                [row[time_column] for row in benchmark_ordered], utc=True
            )
            benchmark_close = pd.Series(
                [row["close"] for row in benchmark_ordered],
                index=benchmark_index,
                dtype=float,
            )
        return _bundle_from_frame(
            symbol=symbol,
            timeframe=timeframe,
            frame=frame,
            source=source,
            requested_count=requested,
            profile=profile,
            earnings=earnings,
            adjustment_mode="stored adj_close; candles unadjusted",
            benchmark_close=benchmark_close,
            market_alignment=market_alignment,
            header_metrics=header_metrics,
            source_revision=self.cache_revision(),
        )


def build_market_data_source(
    local_mirror_path: str,
    *,
    scanner_setups_path: str | Path | None = None,
) -> MarketDataSource:
    if local_mirror_path:
        return ReadOnlyMirrorMarketDataSource(
            local_mirror_path,
            scanner_setups_path=scanner_setups_path,
        )
    return DemoMarketDataSource()
