"""Encrypt and back up .env to the Google Drive cloud backup folder.

Run this by hand whenever you change .env (rotate a KIS key, change the
MySQL password, etc.) -- unlike the JSON state files, .env is never backed
up automatically, since that would mean prompting for a passphrase from a
background thread. See docs/cloud_backup.md for the full design rationale.

The passphrase you enter here is never stored anywhere -- not in .env, not
alongside the backup, not in this repo. Losing it means the encrypted
backup is unrecoverable; write it down somewhere durable (a password
manager), separately from this machine.

Usage:
    python scripts/backup_env_to_cloud.py
    python scripts/backup_env_to_cloud.py --backup-dir "G:\\My Drive"
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
from src.services.env_backup import MIN_RECOMMENDED_PASSPHRASE_LENGTH, backup_env_file
from src.utils.config import ENV_FILE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup-dir", default=None, help='Explicit synced-folder path; auto-detected if omitted')
    args = parser.parse_args()

    if not ENV_FILE.is_file():
        print(f"No .env found at {ENV_FILE} -- nothing to back up.", file=sys.stderr)
        return 1

    backup_root = resolve_backup_root(args.backup_dir)
    if backup_root is None:
        print(
            "Could not find a synced Drive folder. Pass --backup-dir explicitly, "
            'e.g. --backup-dir "G:\\My Drive".',
            file=sys.stderr,
        )
        return 1

    passphrase = getpass.getpass("Passphrase to encrypt .env with (not stored anywhere -- remember it): ")
    if len(passphrase) < MIN_RECOMMENDED_PASSPHRASE_LENGTH:
        print(
            f"Passphrase must be at least "
            f"{MIN_RECOMMENDED_PASSPHRASE_LENGTH} characters. Aborted.",
            file=sys.stderr,
        )
        return 1
    confirm = getpass.getpass("Confirm passphrase: ")
    if passphrase != confirm:
        print("Passphrases did not match. Aborted.", file=sys.stderr)
        return 1

    result = backup_env_file(ENV_FILE, backup_root, passphrase)
    if not result.success:
        print(f"Backup failed: {result.error}", file=sys.stderr)
        return 1

    print(f"Encrypted .env backed up to: {result.destination}")
    print("Remember the passphrase -- it is required to restore this backup and is not stored anywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
