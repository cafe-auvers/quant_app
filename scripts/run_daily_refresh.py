"""Run only the historical refresh modes whose cache data is stale.

Meant to be called once per PC wake (see scripts/pc_morning_routine.ps1).

Uses the same KST cutoff and NYSE holiday calendar as the dashboard. It
checks every symbol that historical.py will refresh, plus both the daily and
hourly tables. This means:
  - An empty or partially refreshed cache always triggers the missing mode.
  - A multi-day gap self-heals on the next run: historical.py requests a
    wide window (one year for daily) rather than only "yesterday."
  - A prior 1H-only failure is retried even when daily data is current.
  - US full-market holidays do not cause an unnecessary refresh.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.data_loader import get_default_universe
from src.utils.db_loader import (
    CHRONIC_FAILURE_THRESHOLD,
    get_chart_indicator_refresh_plan,
    get_chronically_failing_symbols,
    get_latest_hourly_price_history_timestamps,
    get_price_history_watermarks,
    init_mysql_engine,
    is_scanner_metrics_snapshot_current,
)
from src.utils.market_calendar import KST_ZONE, expected_latest_market_data_date

REFERENCE_SYMBOL = "SPY"


def _refresh_tickers() -> List[str]:
    """Match historical.py's ticker set exactly for a meaningful stale check."""
    universe_tickers = get_default_universe(refresh=True)
    normalized = [str(symbol).strip().upper() for symbol in universe_tickers if str(symbol).strip()]
    return list(dict.fromkeys([REFERENCE_SYMBOL, *normalized]))


def _stale_symbols(
    latest_by_symbol: Dict[str, object], tickers: List[str], expected_date: dt.date
) -> List[str]:
    stale = []
    for symbol in tickers:
        latest = latest_by_symbol.get(symbol)
        latest_date = latest.date() if hasattr(latest, "date") else latest
        if latest_date is None or latest_date < expected_date:
            stale.append(symbol)
    return stale


def _describe_stale(mode: str, symbols: List[str], expected_date: dt.date) -> str:
    sample = ", ".join(symbols[:5])
    suffix = "" if len(symbols) <= 5 else ", ..."
    return (
        f"{mode} data is stale for {len(symbols)} of the refresh symbols "
        f"(expected through {expected_date}: {sample}{suffix})."
    )


def _refresh_targets_needed() -> Tuple[Dict[str, List[str]], str]:
    """Return exact per-mode history targets and a human-readable explanation.

    A global MAX(date) is insufficient here: one successfully written batch
    can make the database look current while other symbols or the 1H table
    are still stale after a partial refresh.

    Symbols that have remained stale after several refresh runs in a row
    (see get_chronically_failing_symbols / record_symbol_refresh_outcomes in
    db_loader.py) are excluded from gating: a delisted ticker or an
    unsupported preferred-share class can never become "current," so letting
    it count here would trigger a full 5000+-symbol refresh on every single
    PC restart forever, even once every fetchable symbol is already caught
    up. historical.py still retries them opportunistically whenever a
    refresh runs for any other reason -- they just can't force one on their
    own past the threshold. SPY remains an always-actionable canary so a broad
    provider outage cannot quarantine the entire universe permanently.

    Derived 1D artifacts are checked independently. An interrupted indicator
    or scanner phase can therefore resume without downloading already-current
    price history.
    """
    engine = init_mysql_engine()
    if engine is None:
        return {}, "MySQL is not reachable -- skipping (historical.py would fail the same way)."

    tickers = _refresh_tickers()
    expected_date = expected_latest_market_data_date()
    chronic_daily = get_chronically_failing_symbols(engine, interval="1d")
    chronic_hourly = get_chronically_failing_symbols(engine, interval="1h")

    daily_watermarks = get_price_history_watermarks(engine, tickers, interval="1d")
    daily_latest = {
        symbol: watermark[0]
        for symbol, watermark in daily_watermarks.items()
    }
    daily_stale_all = _stale_symbols(daily_latest, tickers, expected_date)
    hourly_stale_all = _stale_symbols(
        get_latest_hourly_price_history_timestamps(engine, tickers), tickers, expected_date
    )
    daily_stale = [
        symbol
        for symbol in daily_stale_all
        if symbol == REFERENCE_SYMBOL or symbol not in chronic_daily
    ]
    hourly_stale = [
        symbol
        for symbol in hourly_stale_all
        if symbol == REFERENCE_SYMBOL or symbol not in chronic_hourly
    ]

    chart_refresh_plan = get_chart_indicator_refresh_plan(
        engine, tickers, reference_symbol=REFERENCE_SYMBOL
    )
    scanner_current = is_scanner_metrics_snapshot_current(
        engine, tickers, history_watermarks=daily_watermarks
    )

    targets: Dict[str, List[str]] = {}
    reasons = []
    derived_daily_needed = bool(chart_refresh_plan) or not scanner_current
    if daily_stale or derived_daily_needed:
        targets["1d"] = daily_stale_all
    if daily_stale:
        reasons.append(_describe_stale("1D", daily_stale, expected_date))
    if chart_refresh_plan:
        reasons.append(
            f"Chart indicators need refresh for {len(chart_refresh_plan)} symbol(s)."
        )
    if not scanner_current:
        reasons.append("Scanner metrics snapshot is missing or stale.")
    if hourly_stale:
        targets["1h"] = hourly_stale_all
        reasons.append(_describe_stale("1H", hourly_stale, expected_date))

    excluded_daily = len(daily_stale_all) - len(daily_stale)
    excluded_hourly = len(hourly_stale_all) - len(hourly_stale)
    if excluded_daily or excluded_hourly:
        reasons.append(
            f"({excluded_daily} 1D / {excluded_hourly} 1H chronic symbol(s) cannot trigger "
            f"a run alone after {CHRONIC_FAILURE_THRESHOLD}+ stale runs; they are retried "
            "when that mode runs for another reason.)"
        )

    if not targets:
        return {}, f"All {len(tickers)} refresh symbols and derived caches are current through {expected_date} -- skipping." + (
            f" {reasons[-1]}" if reasons else ""
        )
    return targets, " ".join(reasons)


def _refresh_modes_needed() -> Tuple[List[str], str]:
    """Compatibility wrapper returning only the ordered mode names."""
    targets, reason = _refresh_targets_needed()
    return list(targets), reason


def _run_mode(mode: str, symbols: List[str]) -> int:
    print(
        f"Running historical.py --mode {mode} for {len(symbols)} stale history symbol(s) ...",
        flush=True,
    )
    command = [sys.executable, str(REPO_ROOT / "historical.py"), "--mode", mode]
    input_text = None
    if symbols:
        command.append("--symbols-stdin")
        input_text = "".join(f"{symbol}\n" for symbol in symbols)
    elif mode == "1d":
        command.append("--derived-only")
    else:
        raise ValueError("An hourly selective refresh requires at least one stale symbol.")

    result = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        input=input_text,
        text=True,
    )
    if result.returncode != 0:
        print(f"historical.py --mode {mode} exited with code {result.returncode}", file=sys.stderr, flush=True)
    return result.returncode


def main() -> int:
    targets, reason = _refresh_targets_needed()
    print(f"[{dt.datetime.now(KST_ZONE).isoformat()}] {reason}")
    if not targets:
        return 0

    exit_codes = [_run_mode(mode, symbols) for mode, symbols in targets.items()]
    return next((code for code in exit_codes if code != 0), 0)


if __name__ == "__main__":
    sys.exit(main())
