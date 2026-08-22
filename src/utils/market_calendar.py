"""US market-calendar helpers shared by refresh automation and the UI."""
from __future__ import annotations

import datetime as dt
from typing import Optional, Set
from zoneinfo import ZoneInfo


KST_ZONE = ZoneInfo("Asia/Seoul")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)
US_MARKET_ZONE = ZoneInfo("America/New_York")
US_MARKET_OPEN_TIME = dt.time(9, 30)
US_MARKET_CLOSE_TIME = dt.time(16, 0)
US_MARKET_EARLY_CLOSE_TIME = dt.time(13, 0)


def _nearest_weekday(day: dt.date) -> dt.date:
    """Return the weekday on which a fixed-date holiday is observed."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def _easter_sunday(year: int) -> dt.date:
    """Return Gregorian Easter Sunday using the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(114 + h + l - 7 * m, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=delta + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    last = next_month - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def nyse_holidays(year: int) -> Set[dt.date]:
    """Return regular NYSE full-day closures observed in ``year``.

    This intentionally covers the recurring exchange closures used by the
    daily cache gate.  Exceptional one-off closures still cause a harmless
    retry, rather than incorrectly treating an expected trading day as closed.
    """
    holidays = {
        _nearest_weekday(dt.date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - dt.timedelta(days=2),
        _last_weekday(year, 5, 0),
        _nearest_weekday(dt.date(year, 6, 19)),
        _nearest_weekday(dt.date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _nearest_weekday(dt.date(year, 12, 25)),
    }

    # New Year's Day for the following year may be observed on Dec. 31.
    next_new_year_observed = _nearest_weekday(dt.date(year + 1, 1, 1))
    if next_new_year_observed.year == year:
        holidays.add(next_new_year_observed)

    return {day for day in holidays if day.year == year}


def is_nyse_trading_day(day: Optional[dt.date] = None) -> bool:
    """Return whether ``day`` has a regular NYSE session."""

    resolved_day = day or _as_us_market_time(None).date()
    return (
        resolved_day.weekday() < 5
        and resolved_day not in nyse_holidays(resolved_day.year)
    )


def next_nyse_trading_day(day: dt.date) -> dt.date:
    """Roll ``day`` forward until it is a regular NYSE trading day."""

    while not is_nyse_trading_day(day):
        day += dt.timedelta(days=1)
    return day


def previous_nyse_trading_day(day: dt.date) -> dt.date:
    """Roll ``day`` backward until it is a regular NYSE trading day."""
    while not is_nyse_trading_day(day):
        day -= dt.timedelta(days=1)
    return day


def _as_us_market_time(now: Optional[dt.datetime]) -> dt.datetime:
    moment = now or dt.datetime.now(US_MARKET_ZONE)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=US_MARKET_ZONE)
    return moment.astimezone(US_MARKET_ZONE)


def nyse_regular_session_close_time(day: dt.date) -> dt.time:
    """Return the regular-session close, including recurring early closes."""
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    day_after_thanksgiving = thanksgiving + dt.timedelta(days=1)
    christmas_eve = dt.date(day.year, 12, 24)
    july_third = dt.date(day.year, 7, 3)
    early_close = day == day_after_thanksgiving or (
        day == christmas_eve and day.weekday() < 5
    )
    # July 3 is an early close only when it remains a trading day (rather
    # than the observed full-day Independence Day closure).
    early_close = early_close or (
        day == july_third
        and day.weekday() < 5
        and day not in nyse_holidays(day.year)
    )
    return US_MARKET_EARLY_CLOSE_TIME if early_close else US_MARKET_CLOSE_TIME


def is_regular_session_open(now: Optional[dt.datetime] = None) -> bool:
    """True during NYSE regular trading hours (9:30-16:00 America/New_York),
    Monday-Friday, excluding the holidays :func:`nyse_holidays` already
    tracks.

    Several UI modules already copy the plain ``US_MARKET_OPEN_TIME <= t <
    US_MARKET_CLOSE_TIME`` check inline (without holiday awareness); this is
    the one holiday-aware version, added for the Kanban execution engine's
    market-session hooks (code review finding P0-8) so ``TradingEngine``
    never has to guess "is the market open" from a hardcoded ``True``.
    """
    moment = _as_us_market_time(now)
    if not is_nyse_trading_day(moment.date()):
        return False
    return US_MARKET_OPEN_TIME <= moment.time() < nyse_regular_session_close_time(
        moment.date()
    )


def current_or_next_nyse_session_date(
    now: Optional[dt.datetime] = None,
) -> dt.date:
    """Return the session a newly activated Buy Today card should target.

    Premarket and regular-session activations target the current trading day.
    Post-close, weekend, and holiday activations target the next trading day.
    """

    moment = _as_us_market_time(now)
    day = moment.date()
    if (
        is_nyse_trading_day(day)
        and moment.time() < nyse_regular_session_close_time(day)
    ):
        return day
    return next_nyse_trading_day(day + dt.timedelta(days=1))


def next_nyse_regular_session_open(
    now: Optional[dt.datetime] = None,
) -> dt.datetime:
    """Return the next NYSE regular-session open in New York time.

    If the session has not opened yet today, today's 09:30 is returned.
    During or after today's session, the next eligible trading day is used.
    This is an operator-facing ETA helper, not an exchange-order calendar;
    exceptional one-off closures retain the same conservative limitation as
    :func:`nyse_holidays`.
    """

    moment = _as_us_market_time(now)
    candidate = moment.date()
    for _ in range(370):
        if is_nyse_trading_day(candidate):
            opens_at = dt.datetime.combine(
                candidate,
                US_MARKET_OPEN_TIME,
                tzinfo=US_MARKET_ZONE,
            )
            if moment < opens_at:
                return opens_at
        candidate += dt.timedelta(days=1)
    raise RuntimeError("Could not resolve the next NYSE regular-session open")


def seconds_until_nyse_regular_session_open(
    now: Optional[dt.datetime] = None,
) -> float:
    """Seconds until :func:`next_nyse_regular_session_open`."""

    moment = _as_us_market_time(now)
    return max(0.0, (next_nyse_regular_session_open(moment) - moment).total_seconds())


def seconds_until_regular_session_close(now: Optional[dt.datetime] = None) -> float:
    """Seconds from ``now`` until 16:00 America/New_York *today's date*.

    Deliberately does not check whether the market is open right now (a
    caller checking "are we within N seconds of the close" wants this to
    stay small/negative after the close, not jump to tomorrow) -- pair with
    :func:`is_regular_session_open` when session-open-ness also matters.
    """
    moment = _as_us_market_time(now)
    close_time = nyse_regular_session_close_time(moment.date())
    close_at = moment.replace(
        hour=close_time.hour,
        minute=close_time.minute,
        second=0,
        microsecond=0,
    )
    return (close_at - moment).total_seconds()


def expected_latest_market_data_date(now: Optional[dt.datetime] = None) -> dt.date:
    """Return the latest completed US market session expected in the cache."""
    if now is None:
        kst_now = dt.datetime.now(KST_ZONE)
    elif now.tzinfo is None:
        kst_now = now.replace(tzinfo=KST_ZONE)
    else:
        kst_now = now.astimezone(KST_ZONE)

    candidate = kst_now.date() - dt.timedelta(days=1)
    if kst_now.time() < MARKET_DATA_READY_TIME_KST:
        candidate -= dt.timedelta(days=1)
    return previous_nyse_trading_day(candidate)
