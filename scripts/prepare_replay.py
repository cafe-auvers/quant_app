"""Maintain daily study history on the PC and prepare studies on the laptop."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prepare", "history"), default="prepare")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2024, 1, 1))
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--daily-refresh-root", type=Path)
    args = parser.parse_args()
    from src.utils.config import DATA_DIR
    from src.infrastructure.database.engine import init_mysql_engine
    from src.infrastructure.database.repositories.breakout_replay import ReplayRepository
    from src.infrastructure.database.refresh import refresh_universe_history_to_db
    from src.services.replay_preparation import ReplayPreparation

    folder = DATA_DIR / "breakout_replay"
    folder.mkdir(parents=True, exist_ok=True)
    lock = open(folder / f"{args.role}.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"A {args.role} maintenance process is already running.", flush=True)
        return 0
    def log(message):
        print(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {message}", flush=True)
    while True:
        engine = None
        repo = None
        try:
            engine = init_mysql_engine(log_unavailable=False, ensure_schema=False)
            if engine is None:
                raise RuntimeError("Canonical MySQL is unavailable; preparation will retry without downloading data on the laptop.")
            repo = ReplayRepository(engine)
            if args.role == "history":
                repo.prepared.save_job("history", {"status": "running", "processed": 0, "total": 0,
                    "message": "Checking missing daily ranges", "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                daily_lock = None
                if args.daily_refresh_root:
                    path = args.daily_refresh_root / "data" / "refresh_lock_1d.lock"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    daily_lock = open(path, "a+b")
                    import msvcrt
                    try:
                        msvcrt.locking(daily_lock.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError:
                        daily_lock.close()
                        log("Waiting for the existing PC daily refresh to release its lock.")
                        repo.prepared.save_job("history", {"status": "running", "processed": 0, "total": 0,
                            "message": "Waiting for the existing daily refresh", "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                        engine.dispose()
                        engine = None
                        if not args.watch:
                            return 1
                        time.sleep(15)
                        continue
                try:
                    refresh_universe_history_to_db([], engine, interval="1d",
                        study_start_date=args.start - dt.timedelta(days=365), log_callback=log)
                finally:
                    if daily_lock:
                        daily_lock.close()
                history_state = repo.prepared.job("history") or {}
                repo.prepared.save_job("history", {**history_state,
                    "last_check": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "complete"})
            else:
                history_job = repo.prepared.job("history") or {}
                if history_job.get("status") in ("running", "retrying"):
                    repo.prepared.save_job("progress", {"phase": "daily_history", "processed": history_job.get("processed", 0),
                        "total": history_job.get("total", 0), "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
                    log(f"Waiting for the PC daily history extension: {history_job.get('processed', 0)}/{history_job.get('total', 0)} ranges.")
                    engine.dispose()
                    engine = None
                    if not args.watch:
                        return 1
                    time.sleep(15)
                    continue
                published = ReplayPreparation(repo, log).run(start=args.start, force=args.force)
                args.force = False
                from src.services.replay_performance import check_prepared_loads
                check_prepared_loads(repo, published, log)
            state = {"pid": os.getpid(), "role": args.role, "status": "waiting" if args.watch else "complete",
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        except Exception as exc:
            import traceback
            log(f"Preparation failed: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            state = {"pid": os.getpid(), "role": args.role, "status": "retrying" if args.watch else "failed",
                "error": str(exc), "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            if repo is not None:
                try:
                    repo.prepared.save_job("history" if args.role == "history" else "progress",
                        {**state, "phase": "daily_history" if args.role == "history" else "retrying"})
                except Exception:
                    log("Could not publish failure status; the local status file records this retry.")
            if not args.watch:
                return 1
        finally:
            if engine:
                engine.dispose()
        status = folder / f"{args.role}_status.json"
        temporary = status.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(status)
        if not args.watch:
            return 0
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
