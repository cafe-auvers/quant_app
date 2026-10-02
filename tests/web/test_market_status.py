from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from src.web.market_status import nyse_market_status


NEW_YORK = ZoneInfo("America/New_York")


def at(hour: int, minute: int = 0, *, day: int = 2) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=NEW_YORK)


def test_market_status_distinguishes_regular_pre_and_after_hours():
    premarket = nyse_market_status(at(8))
    regular = nyse_market_status(at(10))
    after_hours = nyse_market_status(at(17))

    assert premarket["label"] == "Market Status: Closed (Pre-Market)"
    assert premarket["compact_label"] == "Market Closed · Pre"
    assert regular["state"] == "OPEN"
    assert regular["label"] == "Market Status: Open"
    assert after_hours["phase"] == "AFTER_HOURS"
    assert after_hours["label"] == "Market Status: Closed (After Hours)"
    assert after_hours["compact_label"] == "Market Closed · AH"
    assert premarket["watchlist_session_date"] == "2026-10-02"
    assert regular["watchlist_session_date"] == "2026-10-02"
    assert after_hours["watchlist_session_date"] == "2026-10-05"


def test_market_status_identifies_weekends_and_holidays():
    weekend = nyse_market_status(at(12, day=3))
    thanksgiving = nyse_market_status(
        dt.datetime(2026, 11, 26, 12, tzinfo=NEW_YORK)
    )

    assert weekend["phase"] == "WEEKEND"
    assert thanksgiving["phase"] == "HOLIDAY"
    assert weekend["state"] == thanksgiving["state"] == "CLOSED"
    assert weekend["watchlist_session_date"] == "2026-10-05"
    assert thanksgiving["watchlist_session_date"] == "2026-11-27"
