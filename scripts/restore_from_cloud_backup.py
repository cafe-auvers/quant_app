"""Restore data/*.json user-state files from the Google Drive cloud backup.

Run this after a laptop crash, a fresh install, or a new/replacement machine
to recover watchlist.json, buylist.json, trade_plans.json, scanner_setups.json,
chart_drawings.json, tab_options.json, and settings.json from whatever this
machine's synced Google Drive folder last received (see docs/cloud_backup.md
for how those backups are produced).

Usage:
    python scripts/restore_from_cloud_backup.py                  # latest (current/)
    python scripts/restore_from_cloud_backup.py --list           # show available daily snapshots
    python scripts/restore_from_cloud_backup.py --snapshot 2026-08-05
    python scripts/restore_from_cloud_backup.py --backup-dir "G:\\My Drive" --yes

Any file already present in data/ is preserved first (copied aside into a
timestamped pre_restore_backup_<...> folder) before being overwritten --
this never destroys local data on its way to restoring data.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.cloud_backup import (
    CURRENT_DIRNAME,
    list_daily_snapshots,
    resolve_backup_root,
    restore_state_files,
)
from src.utils.config import DATA_DIR


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--snapshot",
        default=CURRENT_DIRNAME,
        help="'current' (default, latest) or a YYYY-MM-DD daily snapshot",
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help='Explicit synced-folder path (e.g. "G:\\My Drive"); auto-detected if omitted',
    )
    parser.add_argument(
        "--list", action="store_true", help="List available daily snapshots and exit"
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )
    args = parser.parse_args()

    backup_root = resolve_backup_root(args.backup_dir)
    if backup_root is None:
        print(
            "Could not find a synced Drive folder. Make sure Google Drive for "
            "Desktop is installed and signed in, or pass --backup-dir explicitly, "
            'e.g. --backup-dir "G:\\My Drive".',
            file=sys.stderr,
        )
        return 1
    print(f"Backup source: {backup_root}")

    if args.list:
        snapshots = list_daily_snapshots(backup_root)
        if not snapshots:
            print("No daily snapshots found (only 'current' may be available).")
        else:
            print("Available daily snapshots:")
            for day in snapshots:
                print(f"  {day}")
        return 0

    print(f"Restoring snapshot '{args.snapshot}' into {DATA_DIR} ...")
    if not args.yes:
        reply = input(
            "Any existing local files with the same names will be preserved "
            "in a pre_restore_backup_<timestamp> folder, then overwritten. "
            "Continue? [y/N] "
        ).strip().lower()
        if reply != "y":
            print("Aborted.")
            return 1

    result = restore_state_files(backup_root, DATA_DIR, snapshot=args.snapshot)
    if not result.success:
        print(f"Restore failed: {result.error}", file=sys.stderr)
        return 1

    if not result.restored:
        print("Nothing to restore -- the snapshot folder was empty.")
        return 0

    print(f"Restored {len(result.restored)} file(s): {', '.join(result.restored)}")
    if result.preserved_originals_dir:
        print(f"Pre-restore local copies preserved at: {result.preserved_originals_dir}")

    print(
        "\nNext steps:\n"
        "  1. Run the app normally. watchlist/buylist/trade_plans also live in\n"
        "     the shared PC MySQL database, so if it's reachable the app will\n"
        "     reconcile against it too -- that's expected, and it merges by\n"
        "     revision rather than blindly overwriting what you just restored.\n"
        "  2. scanner_setups/chart_drawings/tab_options/settings are NOT stored\n"
        "     in MySQL at all -- this restore is their only recovery path, and\n"
        "     nothing else will overwrite them afterward.\n"
        "  3. If this machine should become the primary editor again, click\n"
        "     'Become Main Device' in the app once you've confirmed the\n"
        "     restored data looks right."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
