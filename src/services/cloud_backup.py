"""Best-effort offsite backup of gitignored user-state files to Google Drive.

The allowlisted ``data/*.json`` files are intentionally gitignored -- they
are runtime state, not code, and churn on every edit. That also means most of
them exist nowhere but the machine that wrote them: a dead disk or an
accidental delete otherwise has no recovery path.

This module copies those files into a local folder synced by Google Drive
for Desktop (or any similar sync client -- OneDrive, Dropbox, etc. all work
the same way since this only ever writes plain files to a local path). It
never talks to a cloud API directly; the sync client already running on the
machine is what actually uploads the copies. Two tiers are kept:

- ``current/`` -- always the latest copy of each file, overwritten in place.
- ``daily/<YYYY-MM-DD>/`` -- one snapshot per calendar day, retaining the
  latest ``keep_daily_snapshots`` snapshot directories, so a bad edit is
  still recoverable (the single ``.bak`` generation next to each file on disk
  only survives one bad write).

Automatic writes are best-effort and wrapped by the caller, so a missing or
unmounted Drive folder never breaks the preceding local save. Reads happen
only during an explicit restore requested by the user.
"""
from __future__ import annotations

import json
import logging
import shutil
import string
import tempfile
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

# The only plaintext files this feature may copy to or restore from the
# synced folder. Keeping the allowlist here makes both backup and restore use
# the same boundary and prevents an unexpected file placed in Drive from
# being written into data/ during recovery.
STATE_BACKUP_FILENAMES = (
    "watchlist.json",
    "buylist.json",
    "trade_plans.json",
    "scanner_setups.json",
    "chart_drawings.json",
    "tab_options.json",
    "settings.json",
    "orders.json",
    "execution_queue.json",
    "legacy_non_prod_buylist.json",
    "legacy_non_prod_execution_queue.json",
)
_STATE_BACKUP_FILENAME_SET = frozenset(STATE_BACKUP_FILENAMES)

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
    failed: List[str] = field(default_factory=list)
    daily_snapshot_created: bool = False
    error: str = ""


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dest.parent,
            prefix=f".{dest.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_dest = Path(tmp_file.name)
        shutil.copy2(src, tmp_dest)
        tmp_dest.replace(dest)
    except Exception:
        if tmp_dest is not None:
            try:
                tmp_dest.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _is_snapshot_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def _validate_json_object(path: Path) -> None:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON value is not an object")


def _prune_daily_snapshots(daily_root: Path, keep: int) -> None:
    if not daily_root.is_dir():
        return
    snapshot_dirs = sorted(
        (
            entry
            for entry in daily_root.iterdir()
            if entry.is_dir() and _is_snapshot_date(entry.name)
        ),
        key=lambda entry: entry.name,
    )
    excess = len(snapshot_dirs) - max(keep, 0)
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
    daily_snapshot_created = not today_dir.is_dir()

    backed_up: List[str] = []
    failures: List[str] = []
    seen_names: set[str] = set()
    for src in files:
        src = Path(src)
        if (
            src.name not in _STATE_BACKUP_FILENAME_SET
            or src.name in seen_names
            or not src.is_file()
        ):
            continue
        seen_names.add(src.name)
        try:
            _validate_json_object(src)
            _atomic_copy(src, current_dir / src.name)
            # A first attempt can be interrupted after creating only part of
            # today's directory. Fill any missing files on later attempts,
            # while never changing files already captured earlier that day.
            if not (today_dir / src.name).is_file():
                _atomic_copy(src, today_dir / src.name)
            backed_up.append(src.name)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            message = f"{src.name}: {exc}"
            failures.append(message)
            logger.warning("Cloud backup skipped invalid/unreadable state file %s", message)

    try:
        _prune_daily_snapshots(root / DAILY_DIRNAME, keep_daily_snapshots)
    except OSError as exc:
        failures.append(f"snapshot pruning: {exc}")
        logger.info("Cloud backup pruning failed: %s", exc)

    return BackupResult(
        success=not failures,
        root=root,
        backed_up=backed_up,
        failed=failures,
        daily_snapshot_created=daily_snapshot_created and bool(backed_up),
        error="; ".join(failures),
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
    try:
        return sorted(
            entry.name
            for entry in daily_root.iterdir()
            if entry.is_dir() and _is_snapshot_date(entry.name)
        )
    except OSError:
        return []


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
    if snapshot == CURRENT_DIRNAME:
        source_dir = root / CURRENT_DIRNAME
    elif _is_snapshot_date(snapshot):
        source_dir = root / DAILY_DIRNAME / snapshot
    else:
        return RestoreResult(
            success=False,
            error="Snapshot must be 'current' or a valid YYYY-MM-DD date.",
        )
    if not source_dir.is_dir():
        return RestoreResult(success=False, source=source_dir, error=f"No backup found at {source_dir}")

    return restore_state_directory(
        source_dir,
        target_dir,
        preserve_existing=preserve_existing,
    )


def restore_state_directory(
    source_dir: Path,
    target_dir: Path,
    *,
    preserve_existing: bool = True,
) -> RestoreResult:
    """Restore an already-selected snapshot directory into ``target_dir``.

    This is also used by the UI's staged-restore flow: it first copies and
    validates the Drive snapshot into a temporary local directory, shuts the
    running app down cleanly, then applies that immutable staged copy.
    """
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        return RestoreResult(
            success=False,
            source=source_dir,
            error=f"No backup found at {source_dir}",
        )

    target_dir = Path(target_dir)
    restored: List[str] = []
    preserved_dir: Optional[Path] = None

    try:
        source_files = sorted(
            p
            for p in source_dir.iterdir()
            if p.is_file() and p.name in _STATE_BACKUP_FILENAME_SET
        )
        # Validate the complete selection before touching local state. One
        # corrupt cloud file should not produce a silently mixed restore.
        for source_file in source_files:
            _validate_json_object(source_file)

        target_dir.mkdir(parents=True, exist_ok=True)

        if preserve_existing:
            existing = [target_dir / p.name for p in source_files if (target_dir / p.name).exists()]
            if existing:
                preserved_dir = target_dir / f"pre_restore_backup_{datetime.now():%Y%m%d_%H%M%S_%f}"
                for existing_file in existing:
                    _atomic_copy(existing_file, preserved_dir / existing_file.name)

        for src in source_files:
            _atomic_copy(src, target_dir / src.name)
            restored.append(src.name)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
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
