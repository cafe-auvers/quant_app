"""Standalone 1D/1H historical data refresh process.

Runs independently of main.py (the PyQt5 dashboard) so closing/reopening the
GUI has no effect on an in-flight refresh. Progress, completion, and error
state are written to data/refresh_status_{mode}.json instead of Qt signals;
see src/services/historical_refresh_control.py for the reader/launcher side
and docs/historical_refactor_plan.md for the full design.

Usage:
    python historical.py --mode 1d
    python historical.py --mode 1h [--backfill]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import msvcrt
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Deque, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.historical_refresh_control import (
    MODE_1D, MODE_1H, is_derived_data_complete, lock_path, status_path,
)
from src.utils.data_loader import get_default_universe
from src.utils.db_loader import (
    init_mysql_engine,
    refresh_chart_indicators_to_db,
    refresh_scanner_metrics_to_db,
    refresh_universe_history_to_db,
    refresh_universe_hourly_history_to_db,
)
from src.utils.storage import save_json

REFERENCE_SYMBOL = "SPY"
RECENT_LOG_LIMIT = 50
PROGRESS_WRITE_MIN_INTERVAL = 1.0  # seconds; throttles pure-progress-percent writes


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
        """Record a phase as durably finished (its own work is already committed to MySQL)."""
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
    """Exclusive per-mode lock via msvcrt; the OS releases it even on a hard kill."""

    def __init__(self, mode: str):
        self._path = lock_path(mode)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = None

    def acquire(self) -> bool:
        self._file = open(self._path, "a+b")
        try:
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
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
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._file.close()
            self._file = None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone 1D/1H historical data refresh.")
    parser.add_argument("--mode", required=True, choices=[MODE_1D, MODE_1H])
    parser.add_argument("--backfill", action="store_true", help="Full 730d backfill (1H mode only).")
    parser.add_argument("--universe-limit", type=int, default=None)
    parser.add_argument("--run-id", default=None)
    return parser.parse_args(argv)


def build_refresh_tickers(universe_limit: Optional[int]) -> List[str]:
    universe_tickers = get_default_universe(max_symbols=universe_limit, refresh=True)
    return list(dict.fromkeys([REFERENCE_SYMBOL, *universe_tickers]))


def run_1d(engine, tickers: List[str], state: RunState) -> None:
    state.set_phase("daily_history")
    updated = refresh_universe_history_to_db(
        tickers,
        engine,
        period="1y",
        interval="1d",
        progress_callback=state.update_progress,
        log_callback=state.log,
    )
    state.updated_count = len(updated)
    state.complete_phase("daily_history")

    state.set_phase("chart_indicators")
    refresh_chart_indicators_to_db(
        tickers, engine, reference_symbol=REFERENCE_SYMBOL, log_callback=state.log,
    )
    state.complete_phase("chart_indicators")

    state.set_phase("scanner_metrics")
    refresh_scanner_metrics_to_db(tickers, engine, log_callback=state.log)
    state.complete_phase("scanner_metrics")


def run_1h(engine, tickers: List[str], backfill: bool, state: RunState) -> None:
    state.set_phase("hourly_history")
    updated = refresh_universe_hourly_history_to_db(
        tickers,
        engine,
        full_period="730d",
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
        engine = init_mysql_engine()
        if engine is None:
            raise RuntimeError("MySQL cache is not configured or cannot be reached.")

        tickers = build_refresh_tickers(args.universe_limit)
        state.log(f"Starting {mode} refresh for {len(tickers)} symbols...")

        if mode == MODE_1D:
            run_1d(engine, tickers, state)
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
