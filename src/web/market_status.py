from __future__ import annotations

import datetime as dt
from typing import Any

from src.utils.market_calendar import (
    US_MARKET_OPEN_TIME,
    US_MARKET_ZONE,
    current_or_next_nyse_session_date,
    is_nyse_trading_day,
    next_nyse_trading_day,
    nyse_regular_session_close_time,
)


def nyse_market_status(now: dt.datetime | None = None) -> dict[str, Any]:
    """Return a compact, display-ready NYSE regular-session status."""

    moment = now or dt.datetime.now(US_MARKET_ZONE)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=US_MARKET_ZONE)
    moment = moment.astimezone(US_MARKET_ZONE)
    day = moment.date()
    current_time = moment.time().replace(tzinfo=None)

    if not is_nyse_trading_day(day):
        phase = "WEEKEND" if day.weekday() >= 5 else "HOLIDAY"
        suffix = "Weekend" if phase == "WEEKEND" else "Holiday"
        label = f"Market Status: Closed ({suffix})"
        compact_label = f"Market Closed · {'Wknd' if phase == 'WEEKEND' else 'Hol'}"
        state = "CLOSED"
    else:
        close_time = nyse_regular_session_close_time(day)
        if current_time < US_MARKET_OPEN_TIME:
            phase = "PRE_MARKET"
            label = "Market Status: Closed (Pre-Market)"
            compact_label = "Market Closed · Pre"
            state = "CLOSED"
        elif current_time < close_time:
            phase = "REGULAR"
            label = "Market Status: Open"
            compact_label = "Market Open"
            state = "OPEN"
        else:
            phase = "AFTER_HOURS"
            label = "Market Status: Closed (After Hours)"
            compact_label = "Market Closed · AH"
            state = "CLOSED"

    session_day = current_or_next_nyse_session_date(moment)
    next_session_day = next_nyse_trading_day(session_day + dt.timedelta(days=1))

    def session_bound(day: dt.date, time: dt.time) -> str:
        return dt.datetime.combine(day, time, tzinfo=US_MARKET_ZONE).isoformat()

    return {
        "state": state,
        "phase": phase,
        "label": label,
        "compact_label": compact_label,
        "watchlist_session_date": session_day.isoformat(),
        "session_open": session_bound(session_day, US_MARKET_OPEN_TIME),
        "session_close": session_bound(session_day, nyse_regular_session_close_time(session_day)),
        "next_session_open": session_bound(next_session_day, US_MARKET_OPEN_TIME),
        "next_session_close": session_bound(next_session_day, nyse_regular_session_close_time(next_session_day)),
        "as_of": moment.isoformat(timespec="seconds"),
        "timezone": "America/New_York",
    }
