"""Shared JavaScript helpers for chart drawing timeframe synchronization."""

from bisect import bisect_left
from collections.abc import Callable, Sequence

import pandas as pd


def snap_daily_drawing_date(value: object, available_dates: list[str]) -> str:
    """Return an axis-backed daily date, advancing closed-market dates."""
    day = str(value or "")[:10]
    if not available_dates:
        return day
    index = bisect_left(available_dates, day)
    return available_dates[min(index, len(available_dates) - 1)]


def build_future_chart_times(
    chart_index: pd.DatetimeIndex,
    chart_timeframe: str,
    uses_intraday_time: bool,
    chart_time_value: Callable[[object], str | int],
) -> list[str | int]:
    """Build future axis points, keeping 1H aligned to daily sessions."""
    last_timestamp = chart_index[-1]
    if uses_intraday_time:
        if chart_timeframe == "1H":
            session_offsets_by_day = {}
            for timestamp in chart_index:
                normalized_day = timestamp.normalize()
                session_offsets_by_day.setdefault(normalized_day, []).append(
                    timestamp - normalized_day
                )
            full_session_size = max(
                len(offsets) for offsets in session_offsets_by_day.values()
            )
            full_session_day = max(
                day
                for day, offsets in session_offsets_by_day.items()
                if len(offsets) == full_session_size
            )
            session_offsets = sorted(set(session_offsets_by_day[full_session_day]))
            values = []
            current_day = last_timestamp.normalize()
            sessions_added = 0
            while sessions_added < 120:
                current_day += pd.Timedelta(days=1)
                if current_day.weekday() >= 5:
                    continue
                values.extend(
                    chart_time_value(current_day + offset)
                    for offset in session_offsets
                )
                sessions_added += 1
            return sorted(set(values))
        if chart_timeframe.endswith("M") and chart_timeframe[:-1].isdigit():
            step = pd.Timedelta(minutes=int(chart_timeframe[:-1]))
        else:
            step = pd.Timedelta(hours=1)
        return [
            chart_time_value(last_timestamp + step * offset)
            for offset in range(1, 501)
        ]

    values = []
    current = last_timestamp
    while len(values) < 120:
        current += pd.Timedelta(days=1)
        if current.weekday() >= 5:
            continue
        values.append(chart_time_value(current))
    return values


def snap_intraday_drawing_time(
    value: object,
    available_times: Sequence[int],
    *,
    prefer: str = "first",
) -> int:
    """Resolve a drawing endpoint to a timestamp present on an intraday axis."""
    axis_times = list(available_times)
    axis_times_by_date = {}
    for time_value in axis_times:
        date_key = pd.Timestamp(time_value, unit="s", tz="UTC").strftime("%Y-%m-%d")
        axis_times_by_date.setdefault(date_key, []).append(time_value)

    text = str(value)
    day = text[:10]
    day_matches = axis_times_by_date.get(day, ())
    if len(text) <= 10:
        if day_matches:
            return day_matches[-1] if prefer == "last" else day_matches[0]
        next_day = next(
            (date_key for date_key in sorted(axis_times_by_date) if date_key >= day),
            None,
        )
        return axis_times_by_date[next_day][0] if next_day else axis_times[-1]

    parsed_timestamp = pd.Timestamp(value)
    if parsed_timestamp.tzinfo is None:
        parsed_timestamp = parsed_timestamp.tz_localize("UTC")
    else:
        parsed_timestamp = parsed_timestamp.tz_convert("UTC")
    parsed_time = int(parsed_timestamp.timestamp())
    if parsed_time in set(axis_times):
        return parsed_time
    if day_matches:
        return min(day_matches, key=lambda item: abs(item - parsed_time))
    next_time = next((item for item in axis_times if item >= parsed_time), None)
    return next_time if next_time is not None else axis_times[-1]


DRAWING_TIMEFRAME_SYNC_JS = r"""
                function snapDailyDrawingTimeToAxis(value, prefer) {
                    const day = normalizeTimeForSave(value).slice(0, 10);
                    const availableTimes = candles.concat(futureWhitespace)
                        .map(candle => candle.time)
                        .filter(time => time != null);
                    if (availableTimes.length === 0) return null;
                    const dayMatches = availableTimes.filter(
                        time => normalizeTimeForSave(time).slice(0, 10) === day
                    );
                    if (dayMatches.length > 0) {
                        return prefer === 'last'
                            ? dayMatches[dayMatches.length - 1]
                            : dayMatches[0];
                    }
                    const nextTime = availableTimes.find(
                        time => normalizeTimeForSave(time).slice(0, 10) >= day
                    );
                    if (nextTime != null) return nextTime;
                    return availableTimes[availableTimes.length - 1];
                }

                function incomingDrawingTime(value, prefer) {
                    if (!usesIntradayTime) return snapDailyDrawingTimeToAxis(value, prefer);
                    const text = String(value || '');
                    const day = text.slice(0, 10);
                    const availableTimes = candles.concat(futureWhitespace)
                        .map(candle => candle.time)
                        .filter(time => time != null);
                    if (availableTimes.length === 0) return null;
                    const dayMatches = availableTimes
                        .filter(time => normalizeTimeForSave(time).slice(0, 10) === day);
                    if (typeof value !== 'number' && text.length <= 10) {
                        if (dayMatches.length > 0) {
                            return prefer === 'last'
                                ? dayMatches[dayMatches.length - 1]
                                : dayMatches[0];
                        }
                        const nextTime = availableTimes.find(
                            time => normalizeTimeForSave(time).slice(0, 10) >= day
                        );
                        return nextTime != null
                            ? nextTime
                            : availableTimes[availableTimes.length - 1];
                    }
                    const parsed = typeof value === 'number'
                        ? Number(value) * 1000
                        : Date.parse(text.replace(' ', 'T') + (text.includes('Z') ? '' : 'Z'));
                    if (!Number.isFinite(parsed)) {
                        return dayMatches.length > 0
                            ? (prefer === 'last' ? dayMatches[dayMatches.length - 1] : dayMatches[0])
                            : null;
                    }
                    const parsedSeconds = Math.floor(parsed / 1000);
                    const exactTime = availableTimes.find(
                        time => Number(time) === parsedSeconds
                    );
                    if (exactTime != null) return exactTime;
                    if (dayMatches.length > 0) {
                        return dayMatches.reduce((closest, time) =>
                            Math.abs(Number(time) - parsedSeconds)
                                < Math.abs(Number(closest) - parsedSeconds)
                                ? time
                                : closest
                        );
                    }
                    const nextTime = availableTimes.find(
                        time => Number(time) >= parsedSeconds
                    );
                    return nextTime != null
                        ? nextTime
                        : availableTimes[availableTimes.length - 1];
                }

                function normalizeDrawingTimeframe(drawing, startValue, endValue) {
                    const provided = String(drawing?.timeframe || "").toUpperCase();
                    if (provided) return provided;
                    const startText = String(startValue || "").toUpperCase();
                    const endText = String(endValue || "").toUpperCase();
                    if (
                        startText.length > 10 ||
                        endText.length > 10 ||
                        startText.includes(' ') ||
                        endText.includes(' ') ||
                        startText.includes('T') ||
                        endText.includes('T')
                    ) {
                        return "INTRADAY";
                    }
                    return "1D";
                }

                function drawingTimeframesMatch(drawingTimeframe) {
                    if (!drawingTimeframe) return true;
                    if (chartTimeframe === drawingTimeframe) return true;
                    const sharedDailyHourly = new Set(["1D", "1H", "INTRADAY"]);
                    if (sharedDailyHourly.has(chartTimeframe) && sharedDailyHourly.has(drawingTimeframe)) return true;
                    if (chartTimeframe === "1D" || drawingTimeframe === "1D") return false;
                    return true;
                }
"""
