"""Copy all PC hourly symbols to the mobile mirror in bounded transactions.

Run independently of main.py. The PC source is read-only; only the disposable
hourly mirror and its checkpoints change. No provider refresh or broker API is
used. Small groups leave readers available under SQLite WAL.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time


def ordered_symbols(source_symbols, priority_symbols=()):
    available = {str(symbol).strip().upper() for symbol in source_symbols if str(symbol).strip()}
    priority = list(dict.fromkeys(str(symbol).strip().upper() for symbol in priority_symbols))
    return [symbol for symbol in priority if symbol in available] + sorted(available-set(priority))


def sync_groups(pc_engine, local_engine, symbols, *, group_size=10, pause_seconds=0.2,
                cancelled=None, progress=None):
    from src.infrastructure.database.mirror_copy import sync_local_mirror_from_pc_checkpointed
    from src.infrastructure.database.mirror_engine import HOURLY_MIRROR_TABLES
    if group_size < 1:
        raise ValueError("group_size must be positive")
    copied = completed = 0
    for offset in range(0, len(symbols), group_size):
        if cancelled and cancelled():
            raise InterruptedError("Hourly mirror synchronization was cancelled")
        group = symbols[offset:offset+group_size]
        written = sync_local_mirror_from_pc_checkpointed(
            pc_engine, local_engine, tables=HOURLY_MIRROR_TABLES,
            hourly_symbols=group, cancellation_callback=cancelled)
        copied += int(written.get("hourly_price_history", 0))
        completed += len(group)
        if progress:
            progress(completed, len(symbols), copied, group)
        if pause_seconds:
            time.sleep(pause_seconds)
    return copied


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--priority", action="append", default=[])
    parser.add_argument("--group-size", type=int, default=10)
    parser.add_argument("--pause-seconds", type=float, default=0.2)
    parser.add_argument("--state-path", type=Path)
    args = parser.parse_args(argv)
    root = args.repository.expanduser().resolve()
    if not (root / "src/utils/config.py").is_file():
        parser.error("repository must name the existing PC checkout")
    if not 1 <= args.group_size <= 50 or not 0 <= args.pause_seconds <= 60:
        parser.error("invalid group size or pause")
    sys.path.insert(0, str(root))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.SetPriorityClass.restype = wintypes.BOOL
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x40):
            raise RuntimeError("Could not set idle CPU priority for the background repair")
    from src.utils.config import install_repository_configuration
    install_repository_configuration()
    from src.infrastructure.database.engine import init_mysql_engine
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.pool import NullPool
    mirror = (args.mirror or root / "data/local_mirror.db").resolve()
    if not mirror.is_file():
        parser.error("existing mobile mirror is required")
    pc = init_mysql_engine(ensure_schema=False)
    if pc is None:
        raise RuntimeError("PC MySQL source unavailable; no mirror changes performed")

    @event.listens_for(pc, "checkout")
    def source_read_only(connection, record, proxy):
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")

    local = create_engine("sqlite:///"+str(mirror), future=True, poolclass=NullPool,
                          connect_args={"timeout": 10, "check_same_thread": False})
    @event.listens_for(local, "connect")
    def mirror_settings(connection, record):
        cursor = connection.cursor()
        mode = str(cursor.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode != "wal":
            raise RuntimeError("Mobile mirror must already use SQLite WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    state_path = (args.state_path or root / "data/web_hourly_mirror_sync.json").resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "RUNNING",
             "source": "PC_MYSQL_READ_ONLY", "scope": "ALL_SOURCE_HOURLY_SYMBOLS",
             "broker_calls": 0, "canonical_trading_writes": 0, "executor_restarted": False}
    def report(completed=0, total=0, copied=0, group=()):
        state.update(updated_at=datetime.now(timezone.utc).isoformat(),
                     completed_symbols=completed, total_symbols=total, rows_copied=copied,
                     last_symbols=list(group))
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2)+"\n", encoding="utf-8")
        temporary.replace(state_path)
        if completed % 100 < args.group_size or completed == total:
            print(json.dumps(state), flush=True)
    try:
        with pc.connect() as conn:
            symbols = list(conn.execute(text("SELECT DISTINCT symbol FROM hourly_price_history ORDER BY symbol")).scalars())
        symbols = ordered_symbols(symbols, args.priority)
        report(total=len(symbols))
        sync_groups(pc, local, symbols, group_size=args.group_size,
                    pause_seconds=args.pause_seconds, progress=report)
        state["status"] = "COMPLETED"
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        report(state["completed_symbols"], len(symbols), state["rows_copied"])
        return 0
    except Exception as exc:
        state.update(status="FAILED", error_type=type(exc).__name__)
        report(state.get("completed_symbols", 0), state.get("total_symbols", 0), state.get("rows_copied", 0))
        print("Hourly mirror sync failed; inspect the private task log", file=sys.stderr)
        return 1
    finally:
        pc.dispose()
        local.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
