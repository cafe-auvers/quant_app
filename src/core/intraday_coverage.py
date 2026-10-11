"""Proof that sparse KIS history covers an opening window without filling bars."""
from __future__ import annotations

import copy
import hashlib
import json
import math

import pandas as pd

KIS_COVERAGE_ATTR = "kis_session_coverage"
KIS_INVALID_ROWS_ATTR = "kis_invalid_row_count"
MARKET_TIMEZONE = "America/New_York"


def _local_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_convert(MARKET_TIMEZONE).tz_localize(None)
    return index


def _opening_hash(frame: pd.DataFrame, session_date: str) -> str:
    try:
        index = _local_index(frame)
        start = pd.Timestamp(session_date) + pd.Timedelta(hours=9, minutes=30)
        selected = frame[(index >= start) & (index < start + pd.Timedelta(minutes=30))]
        selected_index = index[(index >= start) & (index < start + pd.Timedelta(minutes=30))]
        records = []
        for timestamp, (_, row) in zip(selected_index, selected.iterrows()):
            values = [float(row[column]) for column in ("Open", "High", "Low", "Close", "Volume")]
            if not all(math.isfinite(value) for value in values):
                return ""
            if min(values[:4]) <= 0 or values[4] < 0 or values[2] > values[1]:
                return ""
            records.append([timestamp.isoformat(), *values])
        body = json.dumps(sorted(records), separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    except (KeyError, TypeError, ValueError, OverflowError):
        return ""


def annotate_kis_history_coverage(frame: pd.DataFrame, symbol: str) -> None:
    """Bind backward-paged, normalized broker history to its opening rows.

    Only sessions whose open is preceded by an actually downloaded row can
    use this proof. A page limit that stops after the open proves nothing.
    """
    if frame.empty:
        return
    index = _local_index(frame)
    if index.hasnans:
        return
    hashes = {str(day): _opening_hash(frame, str(day)) for day in sorted(set(index.date))}
    frame.attrs[KIS_COVERAGE_ATTR] = {
        "schema_version": 1,
        "source": "kis",
        "symbol": str(symbol).strip().upper(),
        "timezone": MARKET_TIMEZONE,
        "data_valid": not bool(frame.attrs.get(KIS_INVALID_ROWS_ATTR, 0)),
        "covered_from": index.min().isoformat(),
        "covered_through": index.max().isoformat(),
        "opening_hashes": hashes,
    }


def kis_market_timezone(frame: pd.DataFrame, *, symbol: str = "") -> str | None:
    """Explicit KIS local-time provenance takes precedence over UTC heuristics."""
    coverage = frame.attrs.get(KIS_COVERAGE_ATTR)
    if not isinstance(coverage, dict):
        return None
    if coverage.get("source") != "kis" or coverage.get("timezone") != MARKET_TIMEZONE:
        return None
    if symbol and coverage.get("symbol") != str(symbol).strip().upper():
        return None
    return MARKET_TIMEZONE


def kis_history_has_invalid_rows(frame: pd.DataFrame) -> bool:
    coverage = frame.attrs.get(KIS_COVERAGE_ATTR)
    return isinstance(coverage, dict) and coverage.get("data_valid") is False


def rebind_kis_coverage_after_resample(source: pd.DataFrame, result: pd.DataFrame) -> None:
    coverage = source.attrs.get(KIS_COVERAGE_ATTR)
    if not isinstance(coverage, dict):
        return
    coverage = copy.deepcopy(coverage)
    coverage["opening_hashes"] = {
        day: _opening_hash(result, day) for day in coverage.get("opening_hashes", {})
    }
    result.attrs[KIS_COVERAGE_ATTR] = coverage


def has_verified_opening_coverage(
    frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, *, symbol: str = ""
) -> bool:
    """Reject truncated, changed, wrong-session or wrong-symbol cached history."""
    coverage = frame.attrs.get(KIS_COVERAGE_ATTR)
    if not isinstance(coverage, dict):
        return False
    if coverage.get("schema_version") != 1 or coverage.get("source") != "kis":
        return False
    if coverage.get("data_valid") is not True:
        return False
    if coverage.get("timezone") != MARKET_TIMEZONE:
        return False
    if symbol and coverage.get("symbol") != str(symbol).strip().upper():
        return False
    try:
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        if start.tzinfo is not None:
            start = start.tz_convert(MARKET_TIMEZONE).tz_localize(None)
            end = end.tz_convert(MARKET_TIMEZONE).tz_localize(None)
        covered_from = pd.Timestamp(coverage["covered_from"])
        covered_through = pd.Timestamp(coverage["covered_through"])
        if pd.isna(covered_from) or pd.isna(covered_through):
            return False
        if not covered_from < start:
            return False
        if covered_through < end:
            return False
        expected = coverage["opening_hashes"].get(start.date().isoformat())
        return bool(expected and expected == _opening_hash(frame, start.date().isoformat()))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False
