"""One-off / repeatable PC -> laptop market-data mirror sync.

Connects to the PC's shared MySQL database and to (or creates) this laptop's
local SQLite mirror (data/local_mirror.db), prints a before/after comparison,
then copies every row the PC has that the local mirror doesn't yet (see
MIRRORED_TABLES in src/utils/db_loader.py for exactly which tables and why).

Safe to re-run any time the PC is reachable -- each table's sync is an
incremental upsert keyed off that table's own watermark column, so an
already-current mirror does almost no work.

Usage:
    python scripts/sync_local_mirror_from_pc.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.db_loader import (
    LOCAL_MIRROR_DB_PATH,
    MIRRORED_TABLES,
    init_local_mirror_engine,
    init_mysql_engine,
    mirror_table_stats,
    sync_local_mirror_from_pc,
)


def _print_stats_table(title: str, engine) -> None:
    print(f"\n{title}")
    print(f"{'table':<28}{'rows':>12}  latest")
    print("-" * 70)
    for table_name, watermark_column in MIRRORED_TABLES:
        stats = mirror_table_stats(engine, table_name, watermark_column)
        error = stats.get("error")
        if error:
            print(f"{table_name:<28}{'ERROR':>12}  {error}")
        else:
            print(f"{table_name:<28}{stats['row_count']:>12}  {stats['latest']}")


def main() -> int:
    print("Connecting to PC MySQL ...")
    pc_engine = init_mysql_engine()
    if pc_engine is None:
        print(
            "Could not reach the PC's MySQL database (checked MYSQL_HOST from .env). "
            "Nothing to sync from -- is the PC on and Tailscale/LAN reachable?",
            file=sys.stderr,
        )
        return 1
    print(f"Connected: {pc_engine.url.host}:{pc_engine.url.port}/{pc_engine.url.database}")

    print(f"Opening local mirror at {LOCAL_MIRROR_DB_PATH} ...")
    local_engine = init_local_mirror_engine()
    if local_engine is None:
        print("Could not open/create the local SQLite mirror.", file=sys.stderr)
        pc_engine.dispose()
        return 1

    try:
        _print_stats_table("PC (source)", pc_engine)
        _print_stats_table("Local mirror (before sync)", local_engine)

        print("\nSyncing PC -> local mirror ...")
        errors = []
        written = sync_local_mirror_from_pc(
            pc_engine,
            local_engine,
            log_callback=print,
            error_callback=errors.append,
        )
        total = sum(written.values())
        print(f"\nDone. {total} row(s) written across {len(written)} table(s).")

        _print_stats_table("Local mirror (after sync)", local_engine)
        if errors:
            print(
                f"Mirror sync completed with {len(errors)} table error(s).",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        local_engine.dispose()
        pc_engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
