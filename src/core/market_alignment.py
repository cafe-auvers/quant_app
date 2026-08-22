"""Pure Leadership and Market Context calculations.

The chart consumes immutable daily snapshots produced by the batch service;
none of the functions in this module perform I/O.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Mapping, Optional, Sequence

import pandas as pd


ALIGNMENT_FEATURE_VERSION = "1.0"
ALIGNMENT_BENCHMARK = "SPY"

LEADERSHIP_WEIGHTS: Mapping[str, float] = {
    "market_rs": 0.60,
    "industry_peer_rs": 0.40,
}
LEADERSHIP_LABEL_BOUNDARIES: tuple[tuple[int, str], ...] = (
    (80, "STRONG"),
    (60, "MODERATE"),
    (0, "WEAK"),
)
CONTEXT_LABEL_BOUNDARIES: tuple[tuple[float, str], ...] = (
    (7.0, "STRONG"),
    (5.0, "SUPPORTIVE"),
    (3.0, "MIXED"),
    (0.0, "WEAK"),
)


class ContextState(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    UNKNOWN = "UNKNOWN"


CONTEXT_STATE_POINTS: Mapping[ContextState, int] = {
    ContextState.GREEN: 2,
    ContextState.YELLOW: 1,
    ContextState.RED: 0,
}


@dataclass(frozen=True)
class AlignmentConfig:
    """Centralized initial policy; UI code must not duplicate these values."""

    feature_version: str = ALIGNMENT_FEATURE_VERSION
    benchmark: str = ALIGNMENT_BENCHMARK
    minimum_peer_count: int = 5
    minimum_industry_constituents: int = 5
    industry_coverage_ratio: float = 0.60
    minimum_volume: float = 40_000.0
    minimum_dollar_volume: float = 35_000.0
    market_rs_source_field: str = "growth_rank_1m"
    segment_thresholds: tuple[tuple[float, str, str], ...] = (
        (200_000_000_000.0, "Mega-Cap", "MGK"),
        (10_000_000_000.0, "Large-Cap", "SPYG"),
        (2_000_000_000.0, "Mid-Cap", "MDYG"),
        (300_000_000.0, "Small-Cap", "IWO"),
        (0.0, "Micro-Cap", "IWC"),
    )


DEFAULT_ALIGNMENT_CONFIG = AlignmentConfig()


@dataclass(frozen=True)
class DailySeriesMetrics:
    close: Optional[float] = None
    sma20: Optional[float] = None
    sma50: Optional[float] = None
    return_5d: Optional[float] = None
    return_20d: Optional[float] = None
    source_date: Optional[dt.date] = None


@dataclass(frozen=True)
class ContextResult:
    state: ContextState
    conditions: tuple[Optional[bool], Optional[bool], Optional[bool]]

    @property
    def conditions_passed(self) -> Optional[int]:
        if any(value is None for value in self.conditions):
            return None
        return sum(bool(value) for value in self.conditions)


@dataclass(frozen=True)
class OverallContext:
    label: str
    points: Optional[float]
    normalized_points: Optional[float]
    available_components: int
    is_provisional: bool


@dataclass(frozen=True)
class MarketAlignmentSnapshot:
    symbol: str
    as_of_date: dt.date
    feature_version: str
    market_rs: Optional[float]
    market_rs_source: str
    industry_peer_rs: Optional[float]
    peer_basis: str
    peer_count: int
    peer_group_id: Optional[str]
    peer_group_name: Optional[str]
    leadership_score: Optional[float]
    leadership_label: str
    market_state: ContextState
    market_conditions_passed: Optional[int]
    segment_name: Optional[str]
    segment_proxy: Optional[str]
    segment_state: ContextState
    segment_conditions_passed: Optional[int]
    sector_name: Optional[str]
    sector_proxy: Optional[str]
    sector_state: ContextState
    sector_conditions_passed: Optional[int]
    industry_name: Optional[str]
    industry_proxy_or_index: Optional[str]
    industry_state: ContextState
    industry_conditions_passed: Optional[int]
    context_points: Optional[float]
    context_available_components: int
    context_label: str
    is_provisional: bool
    classification_source: str
    calculated_at: dt.datetime
    calculation_details: Mapping[str, object] = field(default_factory=dict)
    market_cap: Optional[float] = None
    market_cap_as_of_date: Optional[dt.date] = None
    is_stale: bool = False

    @property
    def displayed_score(self) -> Optional[int]:
        return round_score(self.leadership_score)

    def with_stale(self, stale: bool) -> "MarketAlignmentSnapshot":
        return replace(self, is_stale=bool(stale))


def finite_number(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def round_score(value: object) -> Optional[int]:
    number = finite_number(value)
    if number is None:
        return None
    return int(Decimal(str(number)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def leadership_label(score: object) -> str:
    displayed = round_score(score)
    if displayed is None:
        return "N/A"
    displayed = min(100, max(0, displayed))
    for minimum, label in LEADERSHIP_LABEL_BOUNDARIES:
        if displayed >= minimum:
            return label
    return "WEAK"


def calculate_leadership_score(
    market_rs: object,
    industry_peer_rs: object,
) -> Optional[float]:
    market_value = finite_number(market_rs)
    peer_value = finite_number(industry_peer_rs)
    if market_value is None or peer_value is None:
        return None
    return (
        LEADERSHIP_WEIGHTS["market_rs"] * market_value
        + LEADERSHIP_WEIGHTS["industry_peer_rs"] * peer_value
    )


def deterministic_percentile(values: Mapping[str, object]) -> dict[str, float]:
    """Average-rank percentiles with stable symbol ordering and tie handling."""

    normalized = {
        str(symbol).strip().upper(): finite_number(value)
        for symbol, value in values.items()
        if str(symbol).strip()
    }
    valid = {symbol: value for symbol, value in normalized.items() if value is not None}
    if not valid:
        return {}
    ordered = pd.Series(
        [valid[symbol] for symbol in sorted(valid)],
        index=sorted(valid),
        dtype=float,
    )
    ranked = ordered.rank(method="average", pct=True) * 100.0
    return {str(symbol): float(value) for symbol, value in ranked.items()}


def industry_peer_rankings(
    market_rs: Mapping[str, object],
    classifications: Mapping[str, Mapping[str, object]],
    *,
    minimum_peer_count: int = 5,
) -> dict[str, dict[str, object]]:
    """Rank within industry, falling back to sector without fabricating peers."""

    valid_rs = {
        str(symbol).upper(): value
        for symbol, raw in market_rs.items()
        if (value := finite_number(raw)) is not None
    }
    industries: dict[str, dict[str, float]] = {}
    sectors: dict[str, dict[str, float]] = {}
    for symbol, value in valid_rs.items():
        classification = classifications.get(symbol, {})
        industry = str(classification.get("industry_id") or "").strip()
        sector = str(classification.get("sector_id") or "").strip()
        if industry:
            industries.setdefault(industry, {})[symbol] = value
        if sector:
            sectors.setdefault(sector, {})[symbol] = value

    industry_ranks = {
        key: deterministic_percentile(values)
        for key, values in industries.items()
        if len(values) >= minimum_peer_count
    }
    sector_ranks = {
        key: deterministic_percentile(values)
        for key, values in sectors.items()
        if len(values) >= minimum_peer_count
    }

    result: dict[str, dict[str, object]] = {}
    for symbol in valid_rs:
        classification = classifications.get(symbol, {})
        industry = str(classification.get("industry_id") or "").strip()
        sector = str(classification.get("sector_id") or "").strip()
        if industry in industry_ranks and symbol in industry_ranks[industry]:
            result[symbol] = {
                "industry_peer_rs": industry_ranks[industry][symbol],
                "peer_basis": "industry",
                "peer_count": len(industries[industry]),
                "peer_group_id": industry,
                "peer_group_name": classification.get("industry_name") or industry,
            }
        elif sector in sector_ranks and symbol in sector_ranks[sector]:
            result[symbol] = {
                "industry_peer_rs": sector_ranks[sector][symbol],
                "peer_basis": "sector_fallback",
                "peer_count": len(sectors[sector]),
                "peer_group_id": sector,
                "peer_group_name": classification.get("sector_name") or sector,
            }
        else:
            result[symbol] = {
                "industry_peer_rs": None,
                "peer_basis": "unavailable",
                "peer_count": max(
                    len(industries.get(industry, {})),
                    len(sectors.get(sector, {})),
                ),
                "peer_group_id": industry or sector or None,
                "peer_group_name": (
                    classification.get("industry_name")
                    or classification.get("sector_name")
                ),
            }
    return result


def assign_market_segment(
    market_cap: object,
    config: AlignmentConfig = DEFAULT_ALIGNMENT_CONFIG,
) -> tuple[Optional[str], Optional[str]]:
    value = finite_number(market_cap)
    if value is None or value < 0:
        return None, None
    for minimum, name, proxy in config.segment_thresholds:
        if value >= minimum:
            return name, proxy
    return None, None


def context_state(conditions: Sequence[Optional[bool]]) -> ContextState:
    if len(conditions) != 3 or any(value is None for value in conditions):
        return ContextState.UNKNOWN
    passed = sum(bool(value) for value in conditions)
    if passed == 3:
        return ContextState.GREEN
    if passed == 2:
        return ContextState.YELLOW
    return ContextState.RED


def evaluate_market_context(metrics: DailySeriesMetrics) -> ContextResult:
    conditions = (
        _greater(metrics.close, metrics.sma20),
        _greater(metrics.close, metrics.sma50),
        _greater(metrics.return_5d, 0.0),
    )
    return ContextResult(context_state(conditions), conditions)


def evaluate_segment_context(
    metrics: DailySeriesMetrics,
    spy_return_5d: object,
) -> ContextResult:
    conditions = (
        _greater(metrics.close, metrics.sma20),
        _greater(metrics.return_5d, 0.0),
        _greater(metrics.return_5d, spy_return_5d),
    )
    return ContextResult(context_state(conditions), conditions)


def evaluate_sector_context(
    metrics: DailySeriesMetrics,
    spy_return_5d: object,
    performance_percentile_20d: object,
) -> ContextResult:
    conditions = (
        _greater(metrics.close, metrics.sma20),
        _greater(metrics.return_5d, spy_return_5d),
        _at_least(performance_percentile_20d, 70.0),
    )
    return ContextResult(context_state(conditions), conditions)


def evaluate_industry_context(
    metrics: DailySeriesMetrics,
    sector_return_5d: object,
    performance_percentile_20d: object,
) -> ContextResult:
    conditions = (
        _greater(metrics.close, metrics.sma20),
        _greater(metrics.return_5d, sector_return_5d),
        _at_least(performance_percentile_20d, 70.0),
    )
    return ContextResult(context_state(conditions), conditions)


def calculate_overall_context(
    market: ContextState,
    segment: ContextState,
    sector: ContextState,
    industry: ContextState,
) -> OverallContext:
    states = (market, segment, sector, industry)
    available = [state for state in states if state is not ContextState.UNKNOWN]
    if len(available) < 3:
        return OverallContext("UNKNOWN", None, None, len(available), False)

    raw_points = float(sum(CONTEXT_STATE_POINTS[state] for state in available))
    provisional = len(available) != len(states)
    normalized = raw_points * 8.0 / (2.0 * len(available))
    label = _context_label(normalized)
    if market is ContextState.RED and label in {"STRONG", "SUPPORTIVE"}:
        label = "MIXED"
    return OverallContext(label, raw_points, normalized, len(available), provisional)


def daily_series_metrics(
    history: pd.DataFrame,
    as_of_date: dt.date,
) -> DailySeriesMetrics:
    """Derive completed-session proxy features, using adjusted close when sound."""

    frame = _normalize_history(history, as_of_date)
    if frame.empty or pd.Timestamp(as_of_date) not in frame.index:
        return DailySeriesMetrics()
    if "Close" not in frame.columns:
        return DailySeriesMetrics()
    close = pd.to_numeric(frame["Close"], errors="coerce")
    reference = close
    if "Adj Close" in frame.columns:
        adjusted = pd.to_numeric(frame["Adj Close"], errors="coerce")
        relevant = adjusted.iloc[-max(51, min(253, len(adjusted))) :]
        if relevant.notna().all() and (relevant > 0).all():
            reference = adjusted
    current = finite_number(reference.iloc[-1])
    display_close = finite_number(close.iloc[-1])
    if current is None or current <= 0:
        return DailySeriesMetrics(close=display_close, source_date=as_of_date)

    def window_mean(size: int) -> Optional[float]:
        if len(reference) < size:
            return None
        window = reference.iloc[-size:]
        if window.isna().any():
            return None
        return finite_number(window.mean())

    def positional_return(offset: int) -> Optional[float]:
        if len(reference) <= offset:
            return None
        prior = finite_number(reference.iloc[-(offset + 1)])
        if prior is None or prior <= 0:
            return None
        return current / prior - 1.0

    return DailySeriesMetrics(
        # Keep close and moving averages on the same basis. When adjusted
        # history is reliable, comparing an unadjusted close with an adjusted
        # SMA would produce a false trend condition around splits/dividends.
        close=current,
        sma20=window_mean(20),
        sma50=window_mean(50),
        return_5d=positional_return(5),
        return_20d=positional_return(20),
        source_date=as_of_date,
    )


def calculate_fallback_market_rs(
    histories: Mapping[str, pd.DataFrame],
    symbols: Sequence[str],
    as_of_date: dt.date,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Requested 63/126/252-session cross-sectional fallback Market RS."""

    returns_by_period: dict[int, dict[str, float]] = {63: {}, 126: {}, 252: {}}
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        frame = _normalize_history(histories.get(symbol), as_of_date)
        if frame.empty or pd.Timestamp(as_of_date) not in frame.index:
            continue
        column = "Adj Close" if "Adj Close" in frame.columns else "Close"
        values = pd.to_numeric(frame[column], errors="coerce")
        if column == "Adj Close" and values.iloc[-253:].isna().any():
            values = pd.to_numeric(frame.get("Close"), errors="coerce")
        current = finite_number(values.iloc[-1]) if not values.empty else None
        if current is None or current <= 0:
            continue
        for period in returns_by_period:
            if len(values) <= period:
                continue
            prior = finite_number(values.iloc[-(period + 1)])
            if prior is not None and prior > 0:
                returns_by_period[period][symbol] = current / prior - 1.0

    percentiles = {
        period: deterministic_percentile(values)
        for period, values in returns_by_period.items()
    }
    scores: dict[str, float] = {}
    components: dict[str, dict[str, float]] = {}
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        if not all(symbol in percentiles[period] for period in (63, 126, 252)):
            continue
        values = {
            "return_63_percentile": percentiles[63][symbol],
            "return_126_percentile": percentiles[126][symbol],
            "return_252_percentile": percentiles[252][symbol],
        }
        scores[symbol] = (
            0.50 * values["return_63_percentile"]
            + 0.30 * values["return_126_percentile"]
            + 0.20 * values["return_252_percentile"]
        )
        components[symbol] = values
    return scores, components


def _normalize_history(
    history: Optional[pd.DataFrame],
    as_of_date: dt.date,
) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame()
    frame = history.copy()
    try:
        index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    except (TypeError, ValueError):
        return pd.DataFrame()
    if index.tz is not None:
        index = index.tz_localize(None)
    frame.index = pd.DatetimeIndex([pd.Timestamp(value.date()) for value in index])
    frame = frame.loc[frame.index.date <= as_of_date]
    if frame.index.has_duplicates:
        frame = frame.groupby(level=0, sort=True).last()
    return frame.sort_index()


def _greater(left: object, right: object) -> Optional[bool]:
    left_value = finite_number(left)
    right_value = finite_number(right)
    if left_value is None or right_value is None:
        return None
    return left_value > right_value


def _at_least(left: object, right: object) -> Optional[bool]:
    left_value = finite_number(left)
    right_value = finite_number(right)
    if left_value is None or right_value is None:
        return None
    return left_value >= right_value


def _context_label(points: float) -> str:
    for minimum, label in CONTEXT_LABEL_BOUNDARIES:
        if points >= minimum:
            return label
    return "WEAK"
