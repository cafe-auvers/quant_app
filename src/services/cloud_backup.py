"""Best-effort offsite backup of gitignored user-state files to Google Drive.

``data/*.json`` (watchlist, buylist, trade plans, scanner setups, chart
drawings, tab options, settings) is intentionally gitignored -- it's runtime
state, not code, and churns on every edit. That also means it exists nowhere
but the machine that wrote it: a dead disk or an accidental delete has no
recovery path.

This module copies those files into a local folder synced by Google Drive
for Desktop (or any similar sync client -- OneDrive, Dropbox, etc. all work
the same way since this only ever writes plain files to a local path). It
never talks to a cloud API directly; the sync client already running on the
machine is what actually uploads the copies. Two tiers are kept:

- ``current/`` -- always the latest copy of each file, overwritten in place.
- ``daily/<YYYY-MM-DD>/`` -- one snapshot per calendar day, pruned after
  ``keep_daily_snapshots`` days, so a bad edit noticed days later is still
  recoverable (the single ``.bak`` generation next to each file on disk only
  survives one bad write).

Safe by construction: every write is best-effort and wrapped by the caller,
a missing/unmounted Drive folder just means backups are skipped (never an
error the rest of the app has to handle), and nothing here is ever read back
by the running app -- it is pure write-only insurance for a human to recover
from later.
"""
from __future__ import annotations

import logging
import shutil
import string
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Optional

from src.utils.config import get_env_value

logger = logging.getLogger(__name__)

BACKUP_ENV_VAR = "QUANT_BACKUP_DIR"
BACKUP_SUBDIR_NAME = "quant_app_backup"
CURRENT_DIRNAME = "current"
DAILY_DIRNAME = "daily"
DEFAULT_KEEP_DAILY_SNAPSHOTS = 21

# Common local sync-folder locations, checked in order when no explicit
# QUANT_BACKUP_DIR is configured. Google Drive for Desktop's default
# "Mirror files" mode creates one of the drive-letter paths; older/renamed
# setups and other sync clients (OneDrive, Dropbox) tend to land under the
# user's home folder instead.
_DRIVE_LETTER_CANDIDATES = [f"{letter}:/My Drive" for letter in string.ascii_uppercase]


def _home_candidates() -> List[Path]:
    home = Path.home()
    return [
        home / "Google Drive" / "My Drive",
        home / "My Drive",
        home / "Google Drive",
    ]


def resolve_backup_root(explicit: Optional[str] = None) -> Optional[Path]:
    """Find the local sync folder to back up into, or None if none is set up.

    Checks ``explicit`` (or the ``QUANT_BACKUP_DIR`` env var / .env value)
    first -- an explicit path is trusted even if it doesn't exist yet (it
    will be created). Falls back to auto-detecting a mounted Google Drive
    for Desktop folder.
    """
    configured = explicit if explicit is not None else get_env_value(BACKUP_ENV_VAR)
    if configured:
        return Path(configured).expanduser()

    for candidate in [Path(p) for p in _DRIVE_LETTER_CANDIDATES] + _home_candidates():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


@dataclass
class BackupResult:
    success: bool
    root: Optional[Path] = None
    backed_up: List[str] = field(default_factory=list)
    daily_snapshot_created: bool = False
    error: str = ""


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_name(f".{dest.name}.tmp")
    shutil.copy2(src, tmp_dest)
    tmp_dest.replace(dest)


def _prune_daily_snapshots(daily_root: Path, keep: int) -> None:
    if not daily_root.is_dir() or keep <= 0:
        return
    snapshot_dirs = sorted(
        (entry for entry in daily_root.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
    )
    excess = len(snapshot_dirs) - keep
    for stale_dir in snapshot_dirs[:max(excess, 0)]:
        try:
            shutil.rmtree(stale_dir)
        except OSError as exc:
            logger.warning("Could not prune stale backup snapshot %s: %s", stale_dir, exc)


def backup_state_files(
    files: Iterable[Path],
    backup_root: Path,
    *,
    keep_daily_snapshots: int = DEFAULT_KEEP_DAILY_SNAPSHOTS,
) -> BackupResult:
    """Copy ``files`` into ``backup_root``'s current/ and today's daily/ folders.

    Missing source files are skipped rather than treated as an error --
    not every install has every optional state file yet. Any filesystem
    failure (Drive folder temporarily unmounted mid-sync, permission issue)
    is caught and reported in the result rather than raised, since a failed
    backup attempt must never interrupt the local save it follows.
    """
    root = Path(backup_root) / BACKUP_SUBDIR_NAME
    current_dir = root / CURRENT_DIRNAME
    today_dir = root / DAILY_DIRNAME / date.today().isoformat()
    daily_snapshot_created = not today_dir.exists()

    backed_up: List[str] = []
    try:
        for src in files:
            src = Path(src)
            if not src.is_file():
                continue
            _atomic_copy(src, current_dir / src.name)
            if daily_snapshot_created:
                _atomic_copy(src, today_dir / src.name)
            backed_up.append(src.name)

        _prune_daily_snapshots(root / DAILY_DIRNAME, keep_daily_snapshots)
    except OSError as exc:
        logger.info("Cloud backup failed: %s", exc)
        return BackupResult(
            success=False,
            root=root,
            backed_up=backed_up,
            daily_snapshot_created=daily_snapshot_created and bool(backed_up),
            error=str(exc),
        )

    return BackupResult(
        success=True,
        root=root,
        backed_up=backed_up,
        daily_snapshot_created=daily_snapshot_created and bool(backed_up),
    )


# --- Restore (crash recovery) -----------------------------------------------
#
# Read-back side of this module. Never called by the running app itself --
# only by scripts/restore_from_cloud_backup.py, run by a human after a lost
# or replaced machine.


def list_daily_snapshots(backup_root: Path) -> List[str]:
    """Return available daily-snapshot dates (``YYYY-MM-DD``), oldest first."""
    daily_root = Path(backup_root) / BACKUP_SUBDIR_NAME / DAILY_DIRNAME
    if not daily_root.is_dir():
        return []
    return sorted(entry.name for entry in daily_root.iterdir() if entry.is_dir())


@dataclass
class RestoreResult:
    success: bool
    source: Optional[Path] = None
    restored: List[str] = field(default_factory=list)
    preserved_originals_dir: Optional[Path] = None
    error: str = ""


def restore_state_files(
    backup_root: Path,
    target_dir: Path,
    *,
    snapshot: str = CURRENT_DIRNAME,
    preserve_existing: bool = True,
) -> RestoreResult:
    """Copy a backed-up snapshot back into ``target_dir`` (normally ``data/``).

    ``snapshot`` is ``"current"`` (the latest copy of each file, the default)
    or a ``YYYY-MM-DD`` string naming one of the daily snapshots. Any file
    already at the destination is preserved first under a timestamped
    ``pre_restore_backup_<...>`` folder rather than being silently clobbered
    -- a crash-recovery tool overwriting data on the way to recovering data
    would be its own kind of data loss.
    """
    root = Path(backup_root) / BACKUP_SUBDIR_NAME
    source_dir = root / CURRENT_DIRNAME if snapshot == CURRENT_DIRNAME else root / DAILY_DIRNAME / snapshot
    if not source_dir.is_dir():
        return RestoreResult(success=False, source=source_dir, error=f"No backup found at {source_dir}")

    target_dir = Path(target_dir)
    restored: List[str] = []
    preserved_dir: Optional[Path] = None

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        source_files = sorted(p for p in source_dir.iterdir() if p.is_file())

        if preserve_existing:
            existing = [target_dir / p.name for p in source_files if (target_dir / p.name).exists()]
            if existing:
                preserved_dir = target_dir / f"pre_restore_backup_{datetime.now():%Y%m%d_%H%M%S}"
                for existing_file in existing:
                    _atomic_copy(existing_file, preserved_dir / existing_file.name)

        for src in source_files:
            _atomic_copy(src, target_dir / src.name)
            restored.append(src.name)
    except OSError as exc:
        logger.info("Cloud restore failed: %s", exc)
        return RestoreResult(
            success=False,
            source=source_dir,
            restored=restored,
            preserved_originals_dir=preserved_dir,
            error=str(exc),
        )

    return RestoreResult(
        success=True,
        source=source_dir,
        restored=restored,
        preserved_originals_dir=preserved_dir,
    )
