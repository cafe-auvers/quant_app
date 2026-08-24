"""Shared JavaScript helpers for chart drawing timeframe synchronization."""

from bisect import bisect_left


def snap_daily_drawing_date(value: object, available_dates: list[str]) -> str:
    """Return an axis-backed daily date, advancing closed-market dates."""
    day = str(value or "")[:10]
    if not available_dates:
        return day
    index = bisect_left(available_dates, day)
    return available_dates[min(index, len(available_dates) - 1)]


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
                    if (typeof value === 'number') return value;
                    const text = String(value || '');
                    const day = text.slice(0, 10);
                    const availableTimes = candles.concat(futureWhitespace)
                        .map(candle => candle.time)
                        .filter(time => time != null);
                    const dayMatches = availableTimes
                        .filter(time => normalizeTimeForSave(time).slice(0, 10) === day);
                    if (text.length <= 10 && dayMatches.length > 0) {
                        return prefer === 'last'
                            ? dayMatches[dayMatches.length - 1]
                            : dayMatches[0];
                    }
                    if (text.length <= 10 && availableTimes.length > 0) {
                        const firstDay = normalizeTimeForSave(availableTimes[0]).slice(0, 10);
                        if (day <= firstDay) return availableTimes[0];
                    }
                    const parsed = Date.parse(text.replace(' ', 'T') + (text.includes('Z') ? '' : 'Z'));
                    if (Number.isFinite(parsed)) return Math.floor(parsed / 1000);
                    return dayMatches.length > 0
                        ? (prefer === 'last' ? dayMatches[dayMatches.length - 1] : dayMatches[0])
                        : null;
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
