"""Verify absent advisory minute bars against completed aggregate candles."""
from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_left

import pandas as pd

from src.utils.market_calendar import US_MARKET_ZONE, nyse_regular_session_close_time


def regular_gaps(frame: pd.DataFrame, now: dt.datetime) -> int:
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        return 0
    index = frame.index.tz_convert(US_MARKET_ZONE)
    now = now.astimezone(US_MARKET_ZONE)
    count = 0
    for day in sorted(set(index.date)):
        if day > now.date():
            continue
        start = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 30), US_MARKET_ZONE))
        close = pd.Timestamp(dt.datetime.combine(day, nyse_regular_session_close_time(day), US_MARKET_ZONE))
        elapsed = max(0, int((pd.Timestamp(now)-pd.Timestamp(dt.datetime.combine(now.date(), dt.time(9, 30), US_MARKET_ZONE))).total_seconds()//60))
        end = min(close, start+pd.Timedelta(minutes=elapsed)) - pd.Timedelta(minutes=1)
        if end >= start:
            count += len(pd.date_range(start, end, freq='min').difference(index))
    return count


def repair_zero_volume_minutes(
    minutes: pd.DataFrame, aggregates: pd.DataFrame, *, interval_minutes: int,
    now: dt.datetime,
) -> tuple[pd.DataFrame, int]:
    """Fill only gaps proved to contain zero eligible volume.

    A completed aggregate must exactly reconcile reported volume and OHLC.
    Positive unallocated volume, a price discrepancy, an invalid observation,
    or a missing same-session anchor leaves the gap untouched. This function
    is advisory-only and is never called by the execution data provider.
    """
    columns = ['Open', 'High', 'Low', 'Close', 'Volume']
    if (minutes.empty or aggregates.empty or interval_minutes not in (5, 60)
            or not isinstance(minutes.index, pd.DatetimeIndex) or not isinstance(aggregates.index, pd.DatetimeIndex)
            or minutes.index.tz is None or aggregates.index.tz is None
            or not set(columns) <= set(minutes) or not set(columns) <= set(aggregates)
            or minutes.index.has_duplicates or aggregates.index.has_duplicates):
        return minutes, 0
    result = minutes.copy()
    result.index = result.index.tz_convert(US_MARKET_ZONE)
    aggregate = aggregates.copy()
    aggregate.index = aggregate.index.tz_convert(US_MARKET_ZONE)
    now = pd.Timestamp(now).tz_convert(US_MARKET_ZONE)
    reported = {stamp: tuple(float(v) for v in row) for stamp, row in
                zip(result.index, result[columns].apply(pd.to_numeric, errors='coerce').to_numpy())}
    by_day = {}
    for stamp in reported:
        if stamp.time() >= dt.time(9, 30):
            by_day.setdefault(stamp.date(), []).append(stamp)
    for stamps in by_day.values():
        stamps.sort()
    additions = {}
    for stamp, raw in zip(aggregate.index, aggregate[columns].apply(pd.to_numeric, errors='coerce').to_numpy()):
        close = pd.Timestamp(dt.datetime.combine(stamp.date(), nyse_regular_session_close_time(stamp.date()), US_MARKET_ZONE))
        end = min(stamp + pd.Timedelta(minutes=interval_minutes), close)
        if stamp.time() < dt.time(9, 30) or stamp >= close or end > now or (stamp.hour*60+stamp.minute-570) % interval_minutes:
            continue
        expected = [stamp+pd.Timedelta(minutes=i) for i in range(int((end-stamp).total_seconds()//60))]
        missing = [m for m in expected if m not in reported]
        if not missing:
            continue
        values = tuple(float(v) for v in raw)
        if (not all(math.isfinite(v) for v in values) or values[4] < 0
                or min(values[:4]) <= 0):
            continue
        part = [reported[m] for m in expected if m in reported]
        if not part:
            if values[4] != 0 or len(set(values[:4])) != 1:
                continue
        else:
            if any(not all(math.isfinite(v) for v in row) or row[4] < 0 or min(row[:4]) <= 0 for row in part):
                continue
            observed = [part[0][0], max(r[1] for r in part), min(r[2] for r in part), part[-1][3], sum(r[4] for r in part)]
            if not all(math.isclose(float(left), float(right), rel_tol=0, abs_tol=1e-8)
                       for left, right in zip(observed, values)):
                continue
        for minute in missing:
            if minute > minutes.index[-1]:
                continue  # Repairs must never create a newer quote timestamp.
            stamps = by_day.get(minute.date(), [])
            prior = bisect_left(stamps, minute)-1
            if prior < 0:
                continue  # Never invent the regular-session opening price.
            price = reported[stamps[prior]][3]
            if not math.isfinite(price) or price <= 0:
                continue
            if not part and not math.isclose(price, values[3], rel_tol=0, abs_tol=1e-8):
                continue
            additions[minute] = [price, price, price, price, 0.0]
    if additions:
        filled = pd.DataFrame.from_dict(additions, orient='index', columns=columns)
        result = pd.concat([result, filled]).sort_index()
    return result, len(additions)
