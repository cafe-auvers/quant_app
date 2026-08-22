"""Pure Market Pulse domain models, calculations, and ranking."""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import pandas as pd


MARKET_SEGMENTS = "market_segments"
SECTORS = "sectors"
INDUSTRIES_THEMES = "industries_themes"

SECTION_LABELS: Mapping[str, str] = {
    MARKET_SEGMENTS: "Market Segments",
    SECTORS: "Sectors",
    INDUSTRIES_THEMES: "Industries & Themes",
}
SECTION_ORDER = tuple(SECTION_LABELS)


@dataclass(frozen=True)
class MarketPulseInstrument:
    section: str
    display_name: str
    ticker: str
    display_order: int
    is_active: bool = True


@dataclass(frozen=True)
class MarketPulseMetrics:
    close: Optional[float] = None
    daily_return: Optional[float] = None
    weekly_return: Optional[float] = None
    monthly_return: Optional[float] = None
    pct_above_52w_low: Optional[float] = None
    pct_below_52w_high: Optional[float] = None


@dataclass(frozen=True)
class MarketPulseRow:
    section: str
    display_name: str
    ticker: str
    display_order: int
    rank: int
    close: Optional[float]
    daily_return: Optional[float]
    weekly_return: Optional[float]
    monthly_return: Optional[float]
    pct_above_52w_low: Optional[float]
    pct_below_52w_high: Optional[float]
    status: str = "available"
    error: str = ""
    source_session_date: Optional[dt.date] = None


@dataclass(frozen=True)
class MarketPulseSnapshot:
    as_of_date: dt.date
    refreshed_at: dt.datetime
    source: str
    rows: tuple[MarketPulseRow, ...]
    failures: Mapping[str, str]
    stale: bool = False


def load_market_pulse_instruments(path: Path) -> tuple[MarketPulseInstrument, ...]:
    """Load and validate the presentation-independent ETF universe."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_records = payload.get("instruments", []) if isinstance(payload, dict) else []
    if not isinstance(raw_records, list):
        raise ValueError("Market Pulse configuration must contain an instruments list")

    instruments = []
    seen = set()
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise ValueError("Every Market Pulse instrument must be an object")
        section = str(raw.get("section") or "").strip()
        display_name = str(raw.get("display_name") or "").strip()
        ticker = str(raw.get("ticker") or "").strip().upper()
        if section not in SECTION_LABELS:
            raise ValueError(f"Unknown Market Pulse section: {section!r}")
        if not display_name or not ticker:
            raise ValueError("Market Pulse display_name and ticker are required")
        key = (section, ticker)
        if key in seen:
            raise ValueError(f"Duplicate Market Pulse instrument: {section}/{ticker}")
        seen.add(key)
        instruments.append(
            MarketPulseInstrument(
                section=section,
                display_name=display_name,
                ticker=ticker,
                display_order=max(0, int(raw.get("display_order", 0))),
                is_active=bool(raw.get("is_active", True)),
            )
        )
    return tuple(
        sorted(
            instruments,
            key=lambda item: (
                SECTION_ORDER.index(item.section),
                item.display_order,
                item.ticker,
            ),
        )
    )


def _finite_number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalize_daily_frame(history: pd.DataFrame, as_of_date: dt.date) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame()
    frame = history.copy()
    try:
        index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    except (TypeError, ValueError):
        return pd.DataFrame()
    # Daily bars represent exchange session dates. Preserve that calendar date
    # instead of shifting it through UTC (which is especially error-prone in KST).
    frame.index = pd.DatetimeIndex([pd.Timestamp(value.date()) for value in index])
    frame = frame.loc[frame.index.date <= as_of_date]
    if frame.empty:
        return frame
    if frame.index.has_duplicates:
        # ``last`` coalesces duplicate provider/cache rows column-by-column and
        # deterministically favors the most recently supplied non-null value.
        frame = frame.groupby(level=0, sort=True).last()
    return frame.sort_index()


def _reference_column(frame: pd.DataFrame) -> str:
    """Select one reliable series for every metric in this calculation."""

    if "Adj Close" not in frame.columns:
        return "Close"
    adjusted = pd.to_numeric(frame["Adj Close"], errors="coerce")
    relevant = adjusted.iloc[-min(252, len(adjusted)) :]
    if len(relevant) >= 2 and relevant.notna().all():
        return "Adj Close"
    return "Close"


def calculate_market_pulse_metrics(
    history: pd.DataFrame,
    as_of_date: dt.date,
) -> MarketPulseMetrics:
    """Calculate decimal EOD returns without skipping missing sessions.

    Returns remain decimals (``0.0127`` means ``1.27%``). Each metric is
    independently nullable when its required positional observations are not
    present. Only rows on or before ``as_of_date`` participate, preventing
    look-ahead.
    """

    frame = _normalize_daily_frame(history, as_of_date)
    if frame.empty or "Close" not in frame.columns:
        return MarketPulseMetrics()
    as_of_timestamp = pd.Timestamp(as_of_date)
    if as_of_timestamp not in frame.index:
        return MarketPulseMetrics()

    close_series = pd.to_numeric(frame["Close"], errors="coerce")
    reference_column = _reference_column(frame)
    reference = pd.to_numeric(frame[reference_column], errors="coerce")

    current_close = _finite_number(close_series.iloc[-1])
    current = _finite_number(reference.iloc[-1])
    if current is None or current <= 0:
        return MarketPulseMetrics(close=current_close)

    def positional_return(offset: int) -> Optional[float]:
        if len(reference) <= offset:
            return None
        prior = _finite_number(reference.iloc[-(offset + 1)])
        if prior is None or prior <= 0:
            return None
        return current / prior - 1.0

    pct_above_low = None
    pct_below_high = None
    if len(reference) >= 252:
        window = reference.iloc[-252:]
        if window.notna().all():
            low = _finite_number(window.min())
            high = _finite_number(window.max())
            if low is not None and low > 0:
                pct_above_low = current / low - 1.0
            if high is not None and high > 0:
                pct_below_high = current / high - 1.0

    return MarketPulseMetrics(
        close=current_close,
        daily_return=positional_return(1),
        weekly_return=positional_return(5),
        monthly_return=positional_return(21),
        pct_above_52w_low=pct_above_low,
        pct_below_52w_high=pct_below_high,
    )


def latest_valid_session_date(
    history: pd.DataFrame,
    not_after: dt.date,
) -> Optional[dt.date]:
    frame = _normalize_daily_frame(history, not_after)
    if frame.empty:
        return None
    column = _reference_column(frame)
    if column not in frame.columns:
        return None
    valid = pd.to_numeric(frame[column], errors="coerce").dropna()
    if valid.empty:
        return None
    return pd.Timestamp(valid.index[-1]).date()


def rank_market_pulse_rows(rows: Iterable[MarketPulseRow]) -> tuple[MarketPulseRow, ...]:
    """Assign deterministic daily-performance ranks independently by section."""

    result = []
    row_values = tuple(rows)
    for section in SECTION_ORDER:
        section_rows = [row for row in row_values if row.section == section]
        ordered = sorted(
            section_rows,
            key=lambda row: (
                row.daily_return is None,
                -row.daily_return if row.daily_return is not None else 0.0,
                row.ticker,
            ),
        )
        result.extend(replace(row, rank=index) for index, row in enumerate(ordered, 1))
    return tuple(result)


def snapshot_to_dict(snapshot: MarketPulseSnapshot) -> dict:
    return {
        "version": 1,
        "as_of_date": snapshot.as_of_date.isoformat(),
        "refreshed_at": snapshot.refreshed_at.isoformat(),
        "source": snapshot.source,
        "stale": snapshot.stale,
        "failures": dict(snapshot.failures),
        "rows": [
            {
                "section": row.section,
                "display_name": row.display_name,
                "ticker": row.ticker,
                "display_order": row.display_order,
                "rank": row.rank,
                "close": row.close,
                "daily_return": row.daily_return,
                "weekly_return": row.weekly_return,
                "monthly_return": row.monthly_return,
                "pct_above_52w_low": row.pct_above_52w_low,
                "pct_below_52w_high": row.pct_below_52w_high,
                "status": row.status,
                "error": row.error,
                "source_session_date": (
                    row.source_session_date.isoformat()
                    if row.source_session_date is not None
                    else None
                ),
            }
            for row in snapshot.rows
        ],
    }


def snapshot_from_dict(payload: Mapping) -> Optional[MarketPulseSnapshot]:
    try:
        rows = tuple(
            MarketPulseRow(
                section=str(raw["section"]),
                display_name=str(raw["display_name"]),
                ticker=str(raw["ticker"]).upper(),
                display_order=int(raw.get("display_order", 0)),
                rank=int(raw.get("rank", 0)),
                close=_finite_number(raw.get("close")),
                daily_return=_finite_number(raw.get("daily_return")),
                weekly_return=_finite_number(raw.get("weekly_return")),
                monthly_return=_finite_number(raw.get("monthly_return")),
                pct_above_52w_low=_finite_number(raw.get("pct_above_52w_low")),
                pct_below_52w_high=_finite_number(raw.get("pct_below_52w_high")),
                status=str(raw.get("status") or "available"),
                error=str(raw.get("error") or ""),
                source_session_date=(
                    dt.date.fromisoformat(str(raw["source_session_date"]))
                    if raw.get("source_session_date")
                    else None
                ),
            )
            for raw in payload.get("rows", [])
        )
        if not rows:
            return None
        refreshed_at = dt.datetime.fromisoformat(str(payload["refreshed_at"]))
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=dt.timezone.utc)
        return MarketPulseSnapshot(
            as_of_date=dt.date.fromisoformat(str(payload["as_of_date"])),
            refreshed_at=refreshed_at,
            source=str(payload.get("source") or "unknown"),
            rows=rank_market_pulse_rows(rows),
            failures=dict(payload.get("failures") or {}),
            stale=bool(payload.get("stale", False)),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
