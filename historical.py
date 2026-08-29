"""Standalone 1D/1H historical data refresh process.

Runs independently of main.py (the PyQt5 dashboard) so closing/reopening the
GUI has no effect on an in-flight refresh. Progress, completion, and error
state are written to data/refresh_status_{mode}.json instead of Qt signals;
see src/services/historical_refresh_control.py for the reader/launcher side
and docs/historical_refactor_plan.md for the full design.

Usage:
    python historical.py --mode 1d
    python historical.py --mode 1h [--backfill]
    @("AAPL", "MSFT") | python historical.py --mode 1d --symbols-stdin
    python historical.py --mode 1d --derived-only
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Deque, Dict, List, Optional

try:  # Windows production path.
    import msvcrt
except ImportError:  # POSIX test/development path.
    msvcrt = None
    import fcntl

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.config import install_repository_configuration

install_repository_configuration()

from src.services.historical_refresh_control import (
    MODE_1D, MODE_1H, is_derived_data_complete, lock_path, status_path,
)
from src.services.chart_fundamentals import (
    refresh_nasdaq_universe_stock_profiles,
    refresh_universe_earnings_history,
    refresh_universe_stock_profiles,
    refresh_universe_upcoming_earnings,
)
from src.services.market_alignment import (
    alignment_reference_tickers,
    refresh_market_alignment_to_db,
)
from src.utils.data_loader import get_default_universe
from src.utils.db_loader import (
    get_chart_indicator_refresh_plan,
    get_latest_hourly_price_history_timestamps,
    get_latest_price_history_dates,
    get_price_history_watermarks,
    is_scanner_metrics_snapshot_current,
    resolve_data_engine,
    refresh_chart_indicators_to_db,
    refresh_scanner_metrics_to_db,
    refresh_universe_history_to_db,
    refresh_universe_hourly_history_to_db,
)
from src.utils.market_calendar import expected_latest_market_data_date
from src.utils.storage import save_json

REFERENCE_SYMBOL = "SPY"
RECENT_LOG_LIMIT = 50
PROGRESS_WRITE_MIN_INTERVAL = 1.0  # seconds; throttles pure-progress-percent writes
PROFILE_REFRESH_BATCH_SIZE = max(
    1, min(2000, int(os.getenv("PROFILE_REFRESH_BATCH_SIZE", "500")))
)
EARNINGS_HISTORY_REFRESH_BATCH_SIZE = max(
    1, min(500, int(os.getenv("EARNINGS_HISTORY_REFRESH_BATCH_SIZE", "100")))
)
EARNINGS_CALENDAR_HORIZON_DAYS = max(
    14, min(180, int(os.getenv("EARNINGS_CALENDAR_HORIZON_DAYS", "100")))
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class RunState:
    """Tracks in-memory progress and flushes it to the status JSON file."""

    def __init__(self, mode: str, run_id: str, backfill: bool):
        self.mode = mode
        self.run_id = run_id
        self.backfill = backfill
        self.pid = os.getpid()
        self.started_at = _now_iso()
        self.phase = "starting"
        self.progress: Dict[str, object] = {}
        self.recent_log: Deque[str] = collections.deque(maxlen=RECENT_LOG_LIMIT)
        self.completed_phases: List[str] = []
        self.updated_count = 0
        self._last_write = 0.0

    def mark_started(self) -> None:
        self._write(force=True)

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self._write(force=True)

    def complete_phase(self, phase: str) -> None:
        """Record a phase as durably finished after its database commit."""
        if phase not in self.completed_phases:
            self.completed_phases.append(phase)
        self._write(force=True)

    def update_progress(self, symbol: str, processed: int, total: int, percent: int, eta_text: str) -> None:
        self.progress = {
            "processed": processed,
            "total": total,
            "percent": percent,
            "eta_text": eta_text,
            "current_symbol": symbol,
        }
        self._write(force=processed >= total)

    def log(self, message: str) -> None:
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, flush=True)
        self.recent_log.append(line)
        self._write(force=True)

    def _write(self, force: bool) -> None:
        now = time.monotonic()
        if not force and (now - self._last_write) < PROGRESS_WRITE_MIN_INTERVAL:
            return
        self._last_write = now
        save_json(status_path(self.mode), self._to_dict("running"))

    def finish(self, status: str, error_message: Optional[str] = None) -> None:
        save_json(status_path(self.mode), self._to_dict(status, error_message))

    def _to_dict(self, status: str, error_message: Optional[str] = None) -> dict:
        now = _now_iso()
        return {
            "schema_version": 2,
            "mode": self.mode,
            "status": status,
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": now,
            "finished_at": None if status == "running" else now,
            "backfill": self.backfill,
            "phase": self.phase,
            "progress": self.progress,
            "recent_log": list(self.recent_log),
            "completed_phases": list(self.completed_phases),
            "derived_data_complete": is_derived_data_complete(self.mode, self.completed_phases),
            "result": {"updated_count": self.updated_count, "error_message": error_message},
        }


class _ModeLock:
    """Exclusive per-mode OS lock released automatically after a hard kill."""

    def __init__(self, mode: str):
        self._path = lock_path(mode)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = None

    def acquire(self) -> bool:
        self._file = open(self._path, "a+b")
        try:
            if msvcrt is not None:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            return False
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if msvcrt is not None:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._file.close()
            self._file = None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone 1D/1H historical data refresh.")
    parser.add_argument("--mode", required=True, choices=[MODE_1D, MODE_1H])
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "1H mode only. Force a 200d re-pull for every symbol. Routine "
            "refreshes intentionally fetch only the rolling D-10 window."
        ),
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--universe-limit", type=int, default=None)
    selection.add_argument(
        "--symbols-stdin",
        action="store_true",
        help="Read the exact non-empty history-refresh symbol list from stdin.",
    )
    selection.add_argument(
        "--derived-only",
        action="store_true",
        help="Run 1D derived-cache checks without downloading price history.",
    )
    parser.add_argument(
        "--force-derived",
        action="store_true",
        help="Explicitly rebuild chart indicators and scanner metrics even when cached.",
    )
    parser.add_argument("--run-id", default=None)
    return parser.parse_args(argv)


def build_refresh_tickers(
    universe_limit: Optional[int], *, refresh: bool = True
) -> List[str]:
    universe_tickers = get_default_universe(
        max_symbols=universe_limit, refresh=refresh
    )
    normalized = [
        str(symbol).strip().upper()
        for symbol in universe_tickers
        if str(symbol).strip()
    ]
    return list(dict.fromkeys([REFERENCE_SYMBOL, *normalized]))


def build_daily_history_tickers(stock_tickers: List[str]) -> List[str]:
    """Add shared alignment proxies to the normal 1D batch download."""

    return list(
        dict.fromkeys([*stock_tickers, *alignment_reference_tickers()])
    )


def read_refresh_symbols_from_stdin() -> List[str]:
    symbols = [line.strip().upper() for line in sys.stdin.read().splitlines() if line.strip()]
    return list(dict.fromkeys(symbols))


def select_stale_history_symbols(engine, symbols: List[str], mode: str) -> List[str]:
    """Recheck a selective payload immediately before any network download."""
    expected_date = expected_latest_market_data_date()
    if mode == MODE_1D:
        latest_by_symbol = get_latest_price_history_dates(
            engine, symbols, interval="1d", strict=True
        )
    else:
        latest_by_symbol = get_latest_hourly_price_history_timestamps(
            engine, symbols, strict=True
        )

    stale = []
    for symbol in symbols:
        latest = latest_by_symbol.get(symbol)
        latest_date = latest.date() if hasattr(latest, "date") else latest
        if latest_date is None or latest_date < expected_date:
            stale.append(symbol)
    return stale


def run_1d(
    engine,
    tickers: List[str],
    state: RunState,
    universe_tickers: Optional[List[str]] = None,
    force_derived: bool = False,
) -> None:
    universe_tickers = universe_tickers or tickers
    state.set_phase("daily_history")
    if tickers:
        updated = refresh_universe_history_to_db(
            tickers,
            engine,
            period="1y",
            interval="1d",
            progress_callback=state.update_progress,
            log_callback=state.log,
        )
    else:
        updated = []
        state.log("Daily price history is already current -- skipping downloads.")
    state.updated_count = len(updated)
    state.complete_phase("daily_history")

    derived_watermarks = get_price_history_watermarks(
        engine, universe_tickers, interval="1d", strict=True
    )

    state.set_phase("chart_indicators")
    chart_refresh_plan = get_chart_indicator_refresh_plan(
        engine,
        universe_tickers,
        reference_symbol=REFERENCE_SYMBOL,
        force=force_derived,
        history_watermarks=derived_watermarks,
    )
    refresh_chart_indicators_to_db(
        universe_tickers,
        engine,
        reference_symbol=REFERENCE_SYMBOL,
        log_callback=state.log,
        force=force_derived,
        history_watermarks=derived_watermarks,
        refresh_plan=chart_refresh_plan,
    )
    remaining_chart_work = {}
    if chart_refresh_plan:
        remaining_chart_work = get_chart_indicator_refresh_plan(
            engine,
            list(chart_refresh_plan),
            reference_symbol=REFERENCE_SYMBOL,
            history_watermarks=derived_watermarks,
        )
    if remaining_chart_work:
        raise RuntimeError(
            f"Chart indicators remain incomplete for {len(remaining_chart_work)} symbol(s)."
        )
    state.complete_phase("chart_indicators")

    state.set_phase("scanner_metrics")
    refresh_scanner_metrics_to_db(
        universe_tickers,
        engine,
        log_callback=state.log,
        force=force_derived,
        history_watermarks=derived_watermarks,
    )
    if not is_scanner_metrics_snapshot_current(
        engine,
        universe_tickers,
        history_watermarks=derived_watermarks,
        strict=True,
    ):
        raise RuntimeError("Scanner metrics snapshot remains incomplete.")
    state.complete_phase("scanner_metrics")

    # Profiles are supplemental and never invalidate otherwise-current price
    # or scanner data. Seed rows are created during schema initialization;
    # this bounded phase progressively enriches sector/industry while a
    # seven-day negative cache rotates past unsupported/provider-failed rows.
    state.set_phase("stock_profiles")
    try:
        bulk_summary = refresh_nasdaq_universe_stock_profiles(
            engine,
            universe_tickers,
        )
        state.log(
            "Nasdaq stock profiles: "
            f"{bulk_summary['complete']} sector/industry profiles available, "
            f"{bulk_summary['changed']} changed."
        )
    except Exception as exc:
        state.log(f"Nasdaq stock profile refresh deferred: {exc}")
    try:
        refresh_universe_stock_profiles(
            engine,
            universe_tickers,
            max_symbols=PROFILE_REFRESH_BATCH_SIZE,
            progress_callback=state.update_progress,
            log_callback=state.log,
        )
    except Exception as exc:
        state.log(f"Stock profile enrichment deferred: {exc}")
    state.complete_phase("stock_profiles")

    state.set_phase("market_alignment")
    refresh_market_alignment_to_db(
        universe_tickers,
        engine,
        force=force_derived,
        log_callback=state.log,
    )
    state.complete_phase("market_alignment")

    state.set_phase("earnings_events")
    try:
        calendar_summary = refresh_universe_upcoming_earnings(
            engine,
            universe_tickers,
            horizon_days=EARNINGS_CALENDAR_HORIZON_DAYS,
        )
        state.log(
            "Nasdaq earnings calendar: "
            f"{calendar_summary['events']} events across "
            f"{calendar_summary['symbols']} symbols, "
            f"{calendar_summary['changed']} changed, "
            f"{calendar_summary['failed_dates']} dates deferred."
        )
    except Exception as exc:
        state.log(f"Nasdaq earnings calendar refresh deferred: {exc}")
    try:
        refresh_universe_earnings_history(
            engine,
            universe_tickers,
            max_symbols=EARNINGS_HISTORY_REFRESH_BATCH_SIZE,
            log_callback=state.log,
        )
    except Exception as exc:
        state.log(f"Yahoo earnings history enrichment deferred: {exc}")
    state.complete_phase("earnings_events")


def run_1h(engine, tickers: List[str], backfill: bool, state: RunState) -> None:
    state.set_phase("hourly_history")
    if not tickers:
        state.log("Hourly price history is already current -- skipping downloads.")
        state.updated_count = 0
        state.complete_phase("hourly_history")
        return
    updated = refresh_universe_hourly_history_to_db(
        tickers,
        engine,
        backfill=backfill,
        progress_callback=state.update_progress,
        log_callback=state.log,
    )
    state.updated_count = len(updated)
    state.complete_phase("hourly_history")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    mode = args.mode
    run_id = args.run_id or uuid.uuid4().hex

    lock = _ModeLock(mode)
    if not lock.acquire():
        print(f"Another {mode} refresh is already running; exiting.", file=sys.stderr)
        return 2

    state = RunState(mode=mode, run_id=run_id, backfill=bool(args.backfill))
    state.mark_started()

    try:
        resolution = resolve_data_engine()
        engine = resolution.engine
        if engine is None:
            raise RuntimeError("No PC MySQL or local market-data cache is available.")
        if resolution.source == "local_mirror":
            print(
                "PC MySQL unreachable; writing to this laptop's local data mirror instead.",
                flush=True,
            )

        if args.derived_only and mode != MODE_1D:
            raise ValueError("--derived-only is supported only for --mode 1d.")

        selective = bool(args.symbols_stdin or args.derived_only)
        universe_tickers = build_refresh_tickers(
            args.universe_limit, refresh=not selective
        )
        history_universe_tickers = (
            build_daily_history_tickers(universe_tickers)
            if mode == MODE_1D
            else universe_tickers
        )
        if args.symbols_stdin:
            requested_symbols = read_refresh_symbols_from_stdin()
            if not requested_symbols:
                raise ValueError(
                    "--symbols-stdin requires at least one symbol; use --derived-only for an empty 1D history plan."
                )
            universe_set = set(history_universe_tickers)
            tickers = [symbol for symbol in requested_symbols if symbol in universe_set]
            dropped = len(requested_symbols) - len(tickers)
            if dropped:
                state.log(f"Ignored {dropped} refresh symbol(s) no longer in the universe.")
            stale_tickers = select_stale_history_symbols(engine, tickers, mode)
            newly_current = len(tickers) - len(stale_tickers)
            tickers = stale_tickers
            if newly_current:
                state.log(
                    f"Skipped {newly_current} symbol(s) that became current before download."
                )
            state.log(
                f"Starting selective {mode} refresh for {len(tickers)} of "
                f"{len(history_universe_tickers)} daily input symbols..."
            )
        elif args.derived_only:
            tickers = []
            state.log(
                f"Starting derived-only 1d refresh for {len(universe_tickers)} universe symbols..."
            )
        else:
            tickers = history_universe_tickers
            state.log(f"Starting {mode} refresh for {len(tickers)} symbols...")

        if mode == MODE_1D:
            run_1d(
                engine,
                tickers,
                state,
                universe_tickers=universe_tickers,
                force_derived=bool(args.force_derived),
            )
        else:
            run_1h(engine, tickers, bool(args.backfill), state)

        state.set_phase("completed")
        state.log(f"{mode} refresh complete. {state.updated_count} symbols updated.")
        state.finish("completed")
        return 0
    except Exception as exc:
        # Best-effort status/log writes: a transient file lock (antivirus,
        # OneDrive sync, etc.) while recording the failure must not itself
        # crash the process, or the run leaves no terminal status behind and
        # looks like an unclean kill instead of a reported error.
        try:
            if mode == MODE_1D and "daily_history" in state.completed_phases and not is_derived_data_complete(mode, state.completed_phases):
                state.log(
                    "Price history was saved, but chart indicators/scanner metrics did not finish — "
                    "run the 1D refresh again to bring derived data back in sync."
                )
            state.log(f"{mode} refresh failed: {exc}")
        except Exception:
            print(f"{mode} refresh failed: {exc}", file=sys.stderr, flush=True)

        try:
            state.finish("error", error_message=str(exc))
        except Exception as finish_exc:
            print(f"Failed to write terminal error status: {finish_exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
