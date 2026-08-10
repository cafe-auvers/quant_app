"""Run the one-time D-200 1-hour history repair on the database PC.

This script is intentionally not part of ``pc_morning_routine.ps1``. Run it
manually once on the PC that hosts MySQL:

    python scripts/backfill_hourly_history_200d_once.py

Routine PC and laptop refreshes remain limited to the rolling D-10 window.
After a successful run, a marker in ``data/`` prevents accidental repeats.
Use ``--force`` only when a completed repair deliberately needs to be rerun.
"""
from __future__ import annotations

import argparse
import datetime as dt
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.runtime_status import database_server_hostname
from src.utils.config import DATA_DIR
from src.utils.db_loader import HOURLY_BACKFILL_PERIOD, resolve_data_engine
from src.utils.storage import save_json

COMPLETION_MARKER = DATA_DIR / "hourly_history_200d_backfill_completed.json"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-time, PC-only D-200 repair of 1-hour history."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rerun even when the successful-completion marker already exists.",
    )
    return parser.parse_args(argv)


def _short_hostname(value: str) -> str:
    return str(value or "").strip().lower().split(".", 1)[0]


def verify_running_on_database_pc() -> Tuple[str, str]:
    """Return local/database hostnames or raise when invoked from the laptop."""
    resolution = resolve_data_engine()
    engine = resolution.engine
    if engine is None or resolution.source != "pc":
        raise RuntimeError(
            "The authoritative PC MySQL database is unavailable; no backfill was started."
        )

    try:
        local_hostname = _short_hostname(platform.node())
        db_hostname = _short_hostname(database_server_hostname(engine))
    finally:
        engine.dispose()

    if not local_hostname or not db_hostname:
        raise RuntimeError(
            "Could not verify the local and MySQL server hostnames; no backfill was started."
        )
    if local_hostname != db_hostname:
        raise RuntimeError(
            "This repair must run directly on the database PC "
            f"(local host: {local_hostname}, MySQL host: {db_hostname})."
        )
    return local_hostname, db_hostname


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        local_hostname, db_hostname = verify_running_on_database_pc()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2

    if COMPLETION_MARKER.exists() and not args.force:
        print(
            f"D-200 hourly backfill already completed; marker: {COMPLETION_MARKER}",
            flush=True,
        )
        return 0

    command = [
        sys.executable,
        str(REPO_ROOT / "historical.py"),
        "--mode",
        "1h",
        "--backfill",
    ]
    print(
        f"Starting one-time {HOURLY_BACKFILL_PERIOD} hourly backfill on {local_hostname}...",
        flush=True,
    )
    result = subprocess.run(command, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        print(
            f"ERROR: D-200 hourly backfill exited with code {result.returncode}; "
            "no completion marker was written.",
            file=sys.stderr,
            flush=True,
        )
        return result.returncode

    save_json(
        COMPLETION_MARKER,
        {
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "period": HOURLY_BACKFILL_PERIOD,
            "local_hostname": local_hostname,
            "database_hostname": db_hostname,
        },
    )
    print(f"D-200 hourly backfill completed. Marker: {COMPLETION_MARKER}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
