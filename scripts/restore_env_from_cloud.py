"""Decrypt and restore .env from the Google Drive cloud backup folder.

Run this on a new/replacement machine to recover MySQL/KIS/OpenAI
credentials, before the app can fully function (MySQL, KIS trading, and AI
review are all dead without .env). Needs the same passphrase that was used
in scripts/backup_env_to_cloud.py -- there is no recovery without it, the
encryption doesn't have a backdoor.

Usage:
    python scripts/restore_env_from_cloud.py
    python scripts/restore_env_from_cloud.py --backup-dir "G:\\My Drive"
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.cloud_backup import resolve_backup_root
from src.services.env_backup import env_backup_exists, restore_env_file
from src.utils.config import ENV_FILE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup-dir", default=None, help='Explicit synced-folder path; auto-detected if omitted')
    args = parser.parse_args()

    backup_root = resolve_backup_root(args.backup_dir)
    if backup_root is None:
        print(
            "Could not find a synced Drive folder. Make sure Google Drive for "
            "Desktop is installed and signed into the account the backup was "
            'written from, or pass --backup-dir explicitly, e.g. '
            '--backup-dir "G:\\My Drive".',
            file=sys.stderr,
        )
        return 1

    if not env_backup_exists(backup_root):
        print(f"No encrypted .env backup found under {backup_root}.", file=sys.stderr)
        return 1

    passphrase = getpass.getpass("Passphrase used to encrypt this .env backup: ")
    result = restore_env_file(backup_root, ENV_FILE, passphrase)
    if not result.success:
        print(f"Restore failed: {result.error}", file=sys.stderr)
        return 1

    print(f"Restored .env to: {result.restored_path}")
    if result.preserved_original:
        print(f"Previous local .env preserved at: {result.preserved_original}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
