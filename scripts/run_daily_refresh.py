"""Run historical.py's 1d and 1h refreshes, but only if the database is
actually behind the most recent trading session -- not a day-of-week guess.

Meant to be called once per PC wake (see scripts/pc_morning_routine.ps1).

Reuses the same "is market data stale" check the running app already shows
on the dashboard (src/ui/mixins/dashboard_mixin.py's
_format_market_data_status / _expected_latest_market_data_date): compare the
DB's actual MAX(date) in price_history against the date we'd expect to have
by now, given the current KST time. This means:
  - An empty database ("no cached data") always triggers a fetch.
  - A multi-day gap (PC missed a wake, a prior run errored out, etc.)
    self-heals on the next run -- historical.py fetches a wide window
    (1y for daily), not just "yesterday," so it catches up fully in one go.
  - The refresh gate and what the dashboard displays as "Needs refresh"
    can never disagree, since they're the same comparison.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.db_loader import get_latest_price_history_date, init_mysql_engine

KST_ZONE = ZoneInfo("Asia/Seoul")
MARKET_DATA_READY_TIME_KST = dt.time(7, 0)


def _previous_weekday(day: dt.date) -> dt.date:
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def _expected_latest_market_data_date(now: Optional[dt.datetime] = None) -> dt.date:
    """Mirrors DashboardMixin._expected_latest_market_data_date exactly, so
    this script's refresh gate and the dashboard's "Needs refresh" label
    always agree on what "up to date" means."""
    kst_now = now.astimezone(KST_ZONE) if now else dt.datetime.now(KST_ZONE)
    candidate = kst_now.date() - dt.timedelta(days=1)
    if kst_now.time() < MARKET_DATA_READY_TIME_KST:
        candidate -= dt.timedelta(days=1)
    return _previous_weekday(candidate)


def _needs_refresh() -> Tuple[bool, str]:
    """Compares the DB's actual latest stored market date against the
    expected one. Returns (should_fetch, human-readable reason)."""
    engine = init_mysql_engine()
    if engine is None:
        return False, "MySQL is not reachable -- skipping (historical.py would fail the same way)."

    latest_date = get_latest_price_history_date(engine)
    expected_date = _expected_latest_market_data_date()

    if latest_date is None:
        return True, f"price_history is empty -- fetching (expected latest: {expected_date})."

    latest_market_date = latest_date.date() if hasattr(latest_date, "date") else latest_date
    if latest_market_date < expected_date:
        return True, f"DB's latest date is {latest_market_date}, expected {expected_date} -- fetching."

    return False, f"DB's latest date is {latest_market_date}, already up to date (expected {expected_date}) -- skipping."


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
    should_fetch, reason = _needs_refresh()
    print(f"[{dt.datetime.now(KST_ZONE).isoformat()}] {reason}")
    if not should_fetch:
        return 0

    exit_1d = _run_mode("1d")
    exit_1h = _run_mode("1h")
    return exit_1d or exit_1h


if __name__ == "__main__":
    sys.exit(main())
