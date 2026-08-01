"""Run historical.py's 1d and 1h refreshes, but only if there's a new US
trading session to fetch.

Meant to be called once per PC wake (see scripts/pc_morning_routine.ps1),
not on a repeating schedule -- unlike the old hourly-trigger design, this PC
is only ever on briefly after the US market has already closed, so there's
no "is the market open right now" question to ask, only "did a trading
session close since we last ran."

Gate: at KST 08:00, New York local time is still the evening of the trading
day whose session just closed a few hours earlier (the ~13-14h KST/NY offset
doesn't cross a NY calendar-day boundary at that hour). So checking today's
NY weekday/holiday status at run time is exactly the right gate, no date
arithmetic needed.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
US_MARKET_ZONE = ZoneInfo("America/New_York")


def _nyse_holidays(year: int) -> set:
    """NYSE observed holidays. Kept in sync by convention with the copy in
    src/ui/main_window.py -- see that file's docstring for why it's not a
    shared import."""

    def nearest_weekday(d: dt.date) -> dt.date:
        if d.weekday() == 5:
            return d - dt.timedelta(days=1)
        if d.weekday() == 6:
            return d + dt.timedelta(days=1)
        return d

    def easter(y: int) -> dt.date:
        a = y % 19
        b, c = divmod(y, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month, day = divmod(114 + h + l - 7 * m, 31)
        return dt.date(y, month, day + 1)

    def nth_weekday(y: int, month: int, weekday: int, n: int) -> dt.date:
        first = dt.date(y, month, 1)
        delta = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=delta + 7 * (n - 1))

    def last_weekday(y: int, month: int, weekday: int) -> dt.date:
        last = dt.date(y, month + 1, 1) - dt.timedelta(days=1)
        delta = (last.weekday() - weekday) % 7
        return last - dt.timedelta(days=delta)

    holidays = {
        nearest_weekday(dt.date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter(year) - dt.timedelta(days=2),
        last_weekday(year, 5, 0),
        nearest_weekday(dt.date(year, 6, 19)),
        nearest_weekday(dt.date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        nearest_weekday(dt.date(year, 12, 25)),
    }
    if dt.date(year, 12, 31).weekday() == 6:
        holidays.add(dt.date(year, 12, 31))
    return holidays


def _is_trading_day(date: dt.date) -> bool:
    return date.weekday() < 5 and date not in _nyse_holidays(date.year)


def _run_mode(mode: str) -> int:
    print(f"Running historical.py --mode {mode} ...", flush=True)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "historical.py"), "--mode", mode],
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f"historical.py --mode {mode} exited with code {result.returncode}", file=sys.stderr, flush=True)
    return result.returncode


def main() -> int:
    now_ny = dt.datetime.now(US_MARKET_ZONE)
    if not _is_trading_day(now_ny.date()):
        print(f"[{now_ny.isoformat()}] {now_ny.date()} (NY) was not a trading day -- nothing new to fetch, skipping.")
        return 0

    print(f"[{now_ny.isoformat()}] {now_ny.date()} (NY) was a trading day -- refreshing.")
    exit_1d = _run_mode("1d")
    exit_1h = _run_mode("1h")
    return exit_1d or exit_1h


if __name__ == "__main__":
    sys.exit(main())
