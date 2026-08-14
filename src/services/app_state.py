"""Local JSON state loading and saving for the dashboard."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.core.watchlist import BuylistManager, TradePlanManager, Watchlist
from src.services.cloud_backup import (
    STATE_BACKUP_FILENAMES,
    BackupResult,
    backup_state_files,
    resolve_backup_root,
)
from src.services.runtime_status import (
    DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    MAIN_APP_PROCESS,
    get_runtime_process_status,
)
from src.services.state_sync import (
    BUYLIST_KEY,
    EXECUTION_QUEUE_KEY,
    PULL_ERROR,
    PULL_OK,
    PUSH_CONFLICT,
    PUSH_NOT_MAIN,
    PUSH_WRITTEN,
    SYNCED_STATE_KEYS,
    TRADE_PLANS_KEY,
    WATCHLIST_KEY,
    LocalDeviceRole,
    claim_main_device,
    claim_main_device_if_stale,
    claim_main_device_if_unclaimed,
    get_main_device,
    pull_state,
    push_state,
    release_main_device,
    set_local_device_main,
)
from src.utils.config import DATA_DIR
from src.utils.storage import load_json, save_json


logger = logging.getLogger(__name__)

WATCHLIST_FILE = DATA_DIR / "watchlist.json"
BUYLIST_FILE = DATA_DIR / "buylist.json"
TRADE_PLANS_FILE = DATA_DIR / "trade_plans.json"
# Same physical file as src/ui/buylist/constants.py's EXECUTION_QUEUE_FILE --
# defined independently here (like the three files above) rather than
# importing from src/ui so this services-layer module has no UI dependency.
EXECUTION_QUEUE_FILE = DATA_DIR / "execution_queue.json"
SCANNER_SETUPS_FILE = DATA_DIR / "scanner_setups.json"
CHART_DRAWINGS_FILE = DATA_DIR / "chart_drawings.json"
TAB_OPTIONS_FILE = DATA_DIR / "tab_options.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_METADATA_FILE = DATA_DIR / "state_metadata.json"
LEGACY_NON_PRODUCTION_BUYLIST_FILE = DATA_DIR / "legacy_non_prod_buylist.json"
LEGACY_NON_PRODUCTION_EXECUTION_QUEUE_FILE = (
    DATA_DIR / "legacy_non_prod_execution_queue.json"
)
# Every gitignored user-state file worth recovering after a lost disk --
# read fresh from disk at backup time rather than passed in-memory, so this
# stays correct regardless of which save path (StateSaveManager vs. a direct
# save_json elsewhere, e.g. settings.json) last wrote each one.
#
# orders.json (order ledger) and execution_queue.json (live execution
# queue) were missed in the first pass of this list -- both are real,
# actively-written trading state, not the cosmetic files they were
# initially grouped with. The legacy_non_prod_* archives are lower-value
# but still real historical records, so they're included too. Regenerable
# caches (sp500_tickers.csv, us_kis_tickers.csv -- re-fetched automatically
# when missing, see data_loader.py) and one-off debug snapshots
# (watchlist_snapshot_*.json) are deliberately left out, same reasoning as
# local_mirror.db: nothing there can't be recreated or wasn't optional in
# the first place.
CLOUD_BACKUP_FILES = tuple(DATA_DIR / name for name in STATE_BACKUP_FILENAMES)
CLOUD_BACKUP_THROTTLE_SECONDS = 600  # at most one backup pass per 10 minutes


def _rejection_collector() -> tuple[
    list[dict[str, Any]], Callable[[int, Any, Exception], None]
]:
    records: list[dict[str, Any]] = []

    def collect(index: int, raw_record: Any, error: Exception) -> None:
        records.append(
            {
                "index": index,
                "identity": (
                    str(raw_record.get("symbol") or raw_record.get("key") or index)
                    if isinstance(raw_record, dict)
                    else str(index)
                ),
                "error_type": type(error).__name__,
                "error": str(error),
                "record": raw_record,
            }
        )

    return records, collect


def quarantine_rejected_records(source: Path, records: list[dict[str, Any]]) -> Path | None:
    """Preserve rejected JSON records outside the normal save payload."""
    if not records:
        return None
    source = Path(source)
    quarantine_path = source.parent / "rejected" / f"{source.stem}.rejected.json"
    try:
        save_json(
            quarantine_path,
            {
                "source": str(source),
                "quarantined_at": datetime.now(timezone.utc).isoformat(),
                "records": records,
            },
        )
    except Exception:
        logger.exception(
            "Could not quarantine %d rejected state record(s) from %s",
            len(records),
            source,
        )
        return None
    logger.error(
        "Quarantined %d rejected state record(s) from %s to %s",
        len(records),
        source,
        quarantine_path,
    )
    return quarantine_path


@dataclass
class SaveResult:
    success: bool
    started_at: datetime
    finished_at: datetime | None
    error: str = ""
    files_written: list[str] = field(default_factory=list)


class StateSaveManager:
    """Track app-state saves and expose their latest result."""

    def __init__(
        self,
        *,
        metadata_file: Path | None = None,
        save_lock: threading.Lock | None = None,
    ) -> None:
        self.metadata_file = metadata_file
        self._save_lock = save_lock or threading.Lock()
        self._pending_threads: list[threading.Thread] = []
        self._pending_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._generation = 0
        self.last_save_status = "idle"
        self.last_save_error = ""
        self.last_save_started_at: datetime | None = None
        self.last_save_finished_at: datetime | None = None
        self.last_result: SaveResult | None = None
        self._binding_lock = threading.Lock()
        self._engine = None
        self._device_id = ""
        self._is_main_device = False
        self._backup_root: Path | None = None
        self._backup_destination_missing_logged = False
        self._last_backup_attempt_at: float | None = None

    def set_engine(
        self,
        engine,
        *,
        device_id: str = "",
        is_main_device: bool = False,
    ) -> None:
        """Attach the (optional) shared MySQL engine used for cross-machine sync.

        Called once the app's async DB-init worker finishes. Before that,
        or if the DB is unreachable, `engine` stays None and pushes are
        skipped -- saves still work locally exactly as before.
        """
        with self._binding_lock:
            self._engine = engine
            self._device_id = str(device_id or "")
            self._is_main_device = bool(is_main_device)

    def set_main_device(self, is_main_device: bool) -> None:
        """Update whether this process may attempt remote state writes."""
        with self._binding_lock:
            self._is_main_device = bool(is_main_device)

    def schedule_save(
        self,
        watchlist_dict: Dict[str, Any],
        buylist_dict: Dict[str, Any],
        trade_manager_dict: Dict[str, Any],
        scanner_setups_copy: Any,
        chart_drawings_copy: Dict[str, Any],
        tab_options_copy: Dict[str, Any],
        *,
        save_lock: Optional[threading.Lock] = None,
        append_log: Callable[[str], None] | None = None,
    ) -> threading.Thread:
        """Schedule a non-daemon background save and return its thread."""
        with self._pending_lock:
            self._generation += 1
            generation = self._generation

        thread = threading.Thread(
            target=self._scheduled_save_worker,
            args=(
                generation,
                watchlist_dict,
                buylist_dict,
                trade_manager_dict,
                scanner_setups_copy,
                chart_drawings_copy,
                tab_options_copy,
            ),
            kwargs={"save_lock": save_lock, "append_log": append_log},
            daemon=False,
            name="StateSaveManager",
        )
        with self._pending_lock:
            self._pending_threads.append(thread)
        thread.start()
        return thread

    def save_now(
        self,
        watchlist_dict: Dict[str, Any],
        buylist_dict: Dict[str, Any],
        trade_manager_dict: Dict[str, Any],
        scanner_setups_copy: Any,
        chart_drawings_copy: Dict[str, Any],
        tab_options_copy: Dict[str, Any],
        *,
        save_lock: Optional[threading.Lock] = None,
        append_log: Callable[[str], None] | None = None,
        lock_timeout: float | None = None,
        supersede_pending: bool = False,
        _scheduled_generation: int | None = None,
    ) -> SaveResult:
        """Save app state synchronously and capture any failure."""
        if supersede_pending:
            with self._pending_lock:
                self._generation += 1

        started_at = datetime.now(timezone.utc)
        lock = save_lock or self._save_lock
        acquired = self._acquire_lock(lock, lock_timeout)
        if not acquired:
            result = SaveResult(
                success=False,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                error="Timed out waiting for local state save lock.",
            )
            self._store_result(result)
            self._write_metadata(result, append_log=append_log)
            self._log_failure(result, append_log)
            return result

        try:
            if _scheduled_generation is not None and not self._is_current_generation(_scheduled_generation):
                return SaveResult(
                    success=True,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    error="Skipped superseded app-state save.",
                )

            with self._status_lock:
                self.last_save_status = "running"
                self.last_save_error = ""
                self.last_save_started_at = started_at
                self.last_save_finished_at = None

            files_written: list[str] = []
            try:
                for path, payload in self._state_files(
                    watchlist_dict,
                    buylist_dict,
                    trade_manager_dict,
                    scanner_setups_copy,
                    chart_drawings_copy,
                    tab_options_copy,
                ):
                    save_json(path, payload)
                    files_written.append(str(path))
            except Exception as exc:
                result = SaveResult(
                    success=False,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    error=f"{type(exc).__name__}: {exc}",
                    files_written=files_written,
                )
                logger.exception("Local app-state save failed")
            else:
                result = SaveResult(
                    success=True,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    files_written=files_written,
                )
                try:
                    self._push_to_remote(
                        watchlist_dict,
                        buylist_dict,
                        trade_manager_dict,
                        append_log=append_log,
                    )
                except Exception:
                    logger.debug("Remote state push failed", exc_info=True)
                    _append_sync_message(
                        append_log,
                        "Remote state save failed; the local copy remains safe.",
                    )
                try:
                    self._backup_to_cloud(append_log=append_log)
                except Exception:
                    logger.debug("Cloud backup failed", exc_info=True)

            self._store_result(result)
            self._write_metadata(result, append_log=append_log)
            if not result.success:
                self._log_failure(result, append_log)
            return result
        finally:
            lock.release()

    def _push_to_remote(
        self,
        watchlist_dict: Dict[str, Any],
        buylist_dict: Dict[str, Any],
        trade_manager_dict: Dict[str, Any],
        *,
        append_log: Callable[[str], None] | None = None,
    ) -> None:
        """Best-effort push of the just-saved state to the shared MySQL sync table.

        Silently does nothing if no engine is attached yet or the DB is
        unreachable -- local save has already succeeded regardless.
        """
        with self._binding_lock:
            engine = self._engine
            is_main_device = self._is_main_device
            device_id = self._device_id
        if engine is None or not is_main_device or not device_id:
            return
        payloads = {
            WATCHLIST_KEY: watchlist_dict,
            BUYLIST_KEY: buylist_dict,
            TRADE_PLANS_KEY: trade_manager_dict,
            # Not one of this method's explicit params (execution_queue.json
            # is saved independently by _save_execution_queue_state, not
            # through StateSaveManager) -- read fresh from disk here instead
            # of widening this method's signature and every one of its
            # call sites just to thread one more dict through.
            EXECUTION_QUEUE_KEY: load_json(EXECUTION_QUEUE_FILE, {}),
        }
        sync_entries = _read_sync_entries(self._metadata_path())
        written_entries: Dict[str, Dict[str, Any]] = {}

        for state_key, payload in payloads.items():
            entry = sync_entries.get(state_key)
            has_base, base_revision, base_hash = _base_sync_values(entry)
            if not has_base:
                # Startup reconciliation establishes the first safe base
                # revision.  Never guess and overwrite a pre-existing row.
                continue
            payload_hash = _state_payload_hash(payload)
            if payload_hash == base_hash:
                continue
            result = push_state(
                engine,
                state_key,
                payload,
                device_id=device_id,
                expected_revision=base_revision,
            )
            if result.status == PUSH_WRITTEN:
                written_entries[state_key] = _sync_entry(
                    result.revision,
                    payload_hash,
                    result.updated_at,
                )
                continue
            if result.status == PUSH_NOT_MAIN:
                with self._binding_lock:
                    if self._engine is engine and self._device_id == device_id:
                        self._is_main_device = False
                _append_sync_message(
                    append_log,
                    "Remote state save stopped because another device is now main.",
                )
                break
            if result.status == PUSH_CONFLICT:
                _append_sync_message(
                    append_log,
                    f"Remote {state_key} was not overwritten because it changed on another device.",
                )
            else:
                _append_sync_message(
                    append_log,
                    f"Remote {state_key} save failed; the local copy remains safe.",
                )

        if written_entries:
            _update_sync_entries(written_entries, self._metadata_path())

    def _backup_to_cloud(
        self,
        *,
        append_log: Callable[[str], None] | None = None,
    ) -> Optional[BackupResult]:
        """Best-effort copy of user-state files into a synced Drive folder.

        Runs after every successful local save, but throttled to at most
        once per `CLOUD_BACKUP_THROTTLE_SECONDS` -- the local write already
        happened regardless, this is purely extra offsite insurance and
        shouldn't spam the sync client on every keystroke-driven save.
        """
        now = time.monotonic()
        if (
            self._last_backup_attempt_at is not None
            and now - self._last_backup_attempt_at < CLOUD_BACKUP_THROTTLE_SECONDS
        ):
            return None
        self._last_backup_attempt_at = now

        if self._backup_root is None:
            self._backup_root = resolve_backup_root()
            if self._backup_root is None:
                if not self._backup_destination_missing_logged:
                    _append_sync_message(
                        append_log,
                        "Cloud backup destination not found (no synced Drive folder "
                        "detected); local saves remain safe but unmirrored offsite.",
                    )
                    self._backup_destination_missing_logged = True
            else:
                self._backup_destination_missing_logged = False
                _append_sync_message(
                    append_log,
                    f"Cloud backup destination: {self._backup_root}",
                )

        if self._backup_root is None:
            return None

        result = backup_state_files(CLOUD_BACKUP_FILES, self._backup_root)
        if not result.success:
            _append_sync_message(
                append_log,
                f"Cloud backup failed; the local copy remains safe: {result.error}",
            )
            # Re-resolve next time in case a streamed/mirrored Drive was
            # temporarily unmounted or its drive letter changed.
            self._backup_root = None
        elif result.backed_up:
            # Always goes to the persistent rotating log file (logger.info);
            # only echoed to the in-app panel on the (rarer) day a fresh
            # daily snapshot is cut, so routine 10-minute passes don't spam
            # the on-screen log the way every failure or the daily cut does.
            suffix = " (new daily snapshot)" if result.daily_snapshot_created else ""
            message = f"Cloud backup: {len(result.backed_up)} file(s) -> {result.root}{suffix}"
            logger.info(message)
            if result.daily_snapshot_created:
                _append_sync_message(append_log, message)
        return result

    def wait_for_pending_saves(self, timeout: float | None = None) -> bool:
        """Wait for currently scheduled saves, returning False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._pending_lock:
                self._pending_threads = [thread for thread in self._pending_threads if thread.is_alive()]
                threads = list(self._pending_threads)

            if not threads:
                return True

            for thread in threads:
                if thread is threading.current_thread():
                    continue
                if deadline is None:
                    thread.join()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                thread.join(remaining)
                if thread.is_alive():
                    return False

    def _scheduled_save_worker(
        self,
        generation: int,
        watchlist_dict: Dict[str, Any],
        buylist_dict: Dict[str, Any],
        trade_manager_dict: Dict[str, Any],
        scanner_setups_copy: Any,
        chart_drawings_copy: Dict[str, Any],
        tab_options_copy: Dict[str, Any],
        *,
        save_lock: Optional[threading.Lock],
        append_log: Callable[[str], None] | None,
    ) -> None:
        try:
            if self._is_current_generation(generation):
                self.save_now(
                    watchlist_dict,
                    buylist_dict,
                    trade_manager_dict,
                    scanner_setups_copy,
                    chart_drawings_copy,
                    tab_options_copy,
                    save_lock=save_lock,
                    append_log=append_log,
                    _scheduled_generation=generation,
                )
        finally:
            current = threading.current_thread()
            with self._pending_lock:
                self._pending_threads = [
                    thread for thread in self._pending_threads
                    if thread is not current and thread.is_alive()
                ]

    def _state_files(
        self,
        watchlist_dict: Dict[str, Any],
        buylist_dict: Dict[str, Any],
        trade_manager_dict: Dict[str, Any],
        scanner_setups_copy: Any,
        chart_drawings_copy: Dict[str, Any],
        tab_options_copy: Dict[str, Any],
    ) -> list[tuple[Path, Dict[str, Any]]]:
        return [
            (WATCHLIST_FILE, watchlist_dict),
            (BUYLIST_FILE, buylist_dict),
            (TRADE_PLANS_FILE, trade_manager_dict),
            (SCANNER_SETUPS_FILE, {"setups": scanner_setups_copy}),
            (CHART_DRAWINGS_FILE, chart_drawings_copy),
            (TAB_OPTIONS_FILE, {"tabs": tab_options_copy}),
        ]

    def _metadata_path(self) -> Path:
        return self.metadata_file or STATE_METADATA_FILE

    def _write_metadata(
        self,
        result: SaveResult,
        *,
        append_log: Callable[[str], None] | None = None,
    ) -> None:
        metadata_path = self._metadata_path()
        metadata = load_json(metadata_path, {})
        timestamp = result.finished_at.isoformat() if result.finished_at else ""
        if result.success:
            metadata["last_successful_save_at"] = timestamp
            metadata["last_error"] = ""
        else:
            metadata["last_failed_save_at"] = timestamp
            metadata["last_error"] = result.error
        metadata["files_written"] = list(result.files_written)

        try:
            save_json(metadata_path, metadata)
        except Exception as exc:
            message = f"State metadata save failed: {exc}"
            logger.warning(message)
            if append_log is not None:
                try:
                    append_log(message)
                except Exception:
                    logger.debug("append_log failed while reporting metadata save failure", exc_info=True)

    def _store_result(self, result: SaveResult) -> None:
        with self._status_lock:
            self.last_result = result
            self.last_save_status = "success" if result.success else "failed"
            self.last_save_error = result.error
            self.last_save_started_at = result.started_at
            self.last_save_finished_at = result.finished_at

    def _log_failure(
        self,
        result: SaveResult,
        append_log: Callable[[str], None] | None,
    ) -> None:
        message = f"Local app-state save failed: {result.error}"
        if append_log is not None:
            try:
                append_log(message)
            except Exception:
                logger.debug("append_log failed while reporting app-state save failure", exc_info=True)

    def _is_current_generation(self, generation: int) -> bool:
        with self._pending_lock:
            return generation == self._generation

    @staticmethod
    def _acquire_lock(lock: threading.Lock, timeout: float | None) -> bool:
        if timeout is None:
            lock.acquire()
            return True
        return lock.acquire(timeout=max(0.0, timeout))


_default_state_save_manager = StateSaveManager()


def get_state_save_manager() -> StateSaveManager:
    return _default_state_save_manager


def load_watchlist_state() -> Watchlist:
    records, collect = _rejection_collector()
    watchlist = Watchlist.from_dict(
        load_json(WATCHLIST_FILE, {"name": "Default", "items": []}),
        on_rejected=collect,
    )
    quarantine_rejected_records(WATCHLIST_FILE, records)
    return watchlist


def archive_non_production_buylist_state(
    data: Dict[str, Any],
    *,
    archive_path: Path | None = None,
) -> int:
    """Preserve non-production buylist rows before the live-only loader filters them."""
    items = data.get("items", []) if isinstance(data, dict) else []
    archived_items = [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("environment") or "").strip().upper() != "PROD"
    ]
    if not archived_items:
        return 0
    save_json(
        archive_path or LEGACY_NON_PRODUCTION_BUYLIST_FILE,
        {"items": archived_items},
    )
    return len(archived_items)


def archive_non_production_execution_queue_state(
    data: Dict[str, Any],
    *,
    archive_path: Path | None = None,
) -> int:
    """Preserve non-production queue rows before the live-only loader filters them."""
    items = data.get("items", {}) if isinstance(data, dict) else {}
    if not isinstance(items, dict):
        return 0

    archived_items = {}
    for raw_key, item in items.items():
        if not isinstance(item, dict):
            continue
        key_environment = str(raw_key).split(":", 1)[0].strip().upper()
        environment = str(item.get("environment") or key_environment).strip().upper()
        if environment != "PROD":
            archived_items[str(raw_key)] = item

    if not archived_items:
        return 0
    save_json(
        archive_path or LEGACY_NON_PRODUCTION_EXECUTION_QUEUE_FILE,
        {
            "upgrade_margin": data.get("upgrade_margin"),
            "items": archived_items,
        },
    )
    return len(archived_items)


def load_buylist_state() -> BuylistManager:
    data = load_json(BUYLIST_FILE, {"items": []})
    archive_non_production_buylist_state(data)
    records, collect = _rejection_collector()
    manager = BuylistManager.from_dict(data, on_rejected=collect)
    quarantine_rejected_records(BUYLIST_FILE, records)
    return manager


def load_trade_plans_state() -> TradePlanManager:
    records, collect = _rejection_collector()
    manager = TradePlanManager.from_dict(
        load_json(TRADE_PLANS_FILE, {"plans": []}),
        on_rejected=collect,
    )
    quarantine_rejected_records(TRADE_PLANS_FILE, records)
    return manager


def load_scanner_setups_state(defaults: Dict[str, Any]) -> Dict[str, Any]:
    return load_json(SCANNER_SETUPS_FILE, {"setups": defaults})


def load_tab_options_state(defaults: Dict[str, Any]) -> Dict[str, Any]:
    return load_json(TAB_OPTIONS_FILE, {"tabs": defaults})


def load_chart_drawings_state() -> Dict[str, Any]:
    data = load_json(CHART_DRAWINGS_FILE, {})
    if not isinstance(data, dict):
        return {}

    normalized = {}
    rejected: list[dict[str, Any]] = []
    for symbol_index, (symbol, drawings) in enumerate(data.items()):
        symbol_key = str(symbol).strip().upper()
        if not symbol_key or not isinstance(drawings, list):
            rejected.append(
                {
                    "index": symbol_index,
                    "identity": symbol_key or str(symbol_index),
                    "error_type": "ValueError",
                    "error": "symbol is empty or drawings is not a list",
                    "record": {"symbol": symbol, "drawings": drawings},
                }
            )
            continue
        clean_drawings = []
        for index, drawing in enumerate(drawings):
            if not isinstance(drawing, dict) or drawing.get("type") != "line":
                rejected.append(
                    {
                        "index": index,
                        "identity": f"{symbol_key}:{index}",
                        "error_type": "ValueError",
                        "error": "drawing is not a supported line object",
                        "record": drawing,
                    }
                )
                continue
            try:
                clean_drawings.append({
                    "id": str(drawing.get("id") or f"{symbol_key}-{index}"),
                    "type": "line",
                    "start_date": str(drawing["start_date"]),
                    "start_price": float(drawing["start_price"]),
                    "end_date": str(drawing["end_date"]),
                    "end_price": float(drawing["end_price"]),
                })
            except (KeyError, TypeError, ValueError) as exc:
                rejected.append(
                    {
                        "index": index,
                        "identity": f"{symbol_key}:{index}",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "record": drawing,
                    }
                )
                continue
        if clean_drawings:
            normalized[symbol_key] = clean_drawings
    quarantine_rejected_records(CHART_DRAWINGS_FILE, rejected)
    return normalized


def _state_payload_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_sync_entries(metadata_path: Path | None = None) -> Dict[str, Dict[str, Any]]:
    metadata = load_json(metadata_path or STATE_METADATA_FILE, {})
    entries = metadata.get("state_sync")
    if not isinstance(entries, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in entries.items()
        if isinstance(value, dict)
    }


def _sync_entry(
    revision: int | None,
    content_hash: str,
    updated_at: datetime | None,
) -> Dict[str, Any]:
    return {
        "revision": int(revision or 0),
        "content_hash": content_hash,
        "updated_at": updated_at.isoformat() if updated_at else "",
    }


def _base_sync_values(entry: Any) -> tuple[bool, int, str]:
    if not isinstance(entry, dict):
        return False, 0, ""
    try:
        revision = int(entry.get("revision") or 0)
    except (TypeError, ValueError):
        return False, 0, ""
    content_hash = str(entry.get("content_hash") or "")
    return bool(revision > 0 and content_hash), revision, content_hash


def _update_sync_entries(
    updates: Dict[str, Dict[str, Any]],
    metadata_path: Path | None = None,
) -> None:
    path = metadata_path or STATE_METADATA_FILE
    metadata = load_json(path, {})
    entries = metadata.get("state_sync")
    entries = dict(entries) if isinstance(entries, dict) else {}
    entries.update(updates)
    metadata["state_sync"] = entries
    try:
        save_json(path, metadata)
    except Exception:
        logger.debug("Could not persist state-sync bookkeeping", exc_info=True)


def _append_sync_message(
    append_log: Callable[[str], None] | None,
    message: str,
) -> None:
    logger.info(message)
    if append_log is None:
        return
    try:
        append_log(message)
    except Exception:
        logger.debug("append_log failed while reporting state sync", exc_info=True)


def _synced_key_to_file() -> Dict[str, Path]:
    # Read the module-level FILE constants at call time (rather than caching
    # them at import time) so tests that monkeypatch e.g. app_state.WATCHLIST_FILE
    # are respected.
    return {
        WATCHLIST_KEY: WATCHLIST_FILE,
        BUYLIST_KEY: BUYLIST_FILE,
        TRADE_PLANS_KEY: TRADE_PLANS_FILE,
        EXECUTION_QUEUE_KEY: EXECUTION_QUEUE_FILE,
    }


@dataclass
class StateReconcileResult:
    updated_keys: set[str] = field(default_factory=set)
    conflict_keys: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    is_main_device: bool = False
    main_device_hostname: str = ""
    local_role: LocalDeviceRole | None = None
    # Populated whenever ``is_main_device`` is True -- the current
    # main-device lease token, cached by MainWindow and threaded into every
    # live order submission so ExecutionAuthority can re-verify it at the
    # actual broker boundary. Empty whenever this device isn't main.
    lease_token: str = ""


def _demote_after_lost_ownership(
    role: LocalDeviceRole,
    result: StateReconcileResult,
) -> LocalDeviceRole:
    result.is_main_device = False
    result.lease_token = ""
    try:
        role = set_local_device_main(role, False)
    except Exception as exc:
        result.errors.append(f"Could not persist pull-only device role: {exc}")
        role = LocalDeviceRole(role.device_id, role.hostname, False)
    result.local_role = role
    return role


def _apply_remote_state(
    state_key: str,
    path: Path,
    remote,
    result: StateReconcileResult,
    metadata_updates: Dict[str, Dict[str, Any]],
) -> None:
    try:
        save_json(path, remote.payload)
    except Exception as exc:
        logger.exception("Failed writing synced %s state to %s", state_key, path)
        result.errors.append(f"Could not save pulled {state_key}: {exc}")
        return
    payload_hash = _state_payload_hash(remote.payload)
    metadata_updates[state_key] = _sync_entry(
        remote.revision,
        payload_hash,
        remote.updated_at,
    )
    result.updated_keys.add(state_key)


def reconcile_state_with_remote(
    engine,
    role: LocalDeviceRole,
    *,
    save_lock: threading.Lock | None = None,
    ownership_only_when_main: bool = False,
) -> StateReconcileResult:
    """Reconcile local files using ownership, hashes, and conditional revisions."""
    result = StateReconcileResult(local_role=role)
    if engine is None:
        result.errors.append("State sync database is unavailable.")
        return result

    lock = save_lock
    if lock is not None:
        lock.acquire()
    try:
        ownership = get_main_device(engine)
        if not ownership.success:
            result.errors.append(ownership.error or "Could not read main-device ownership.")
            return result

        main_device = ownership.main_device
        if main_device is None and role.is_main:
            ownership = claim_main_device(engine, role)
            if not ownership.success:
                result.errors.append(ownership.error or "Could not claim main-device ownership.")
                return result
            main_device = ownership.main_device

        is_main = bool(main_device and main_device.device_id == role.device_id)
        if role.is_main != is_main:
            try:
                role = set_local_device_main(role, is_main)
            except Exception as exc:
                result.errors.append(f"Could not persist this device's sync role: {exc}")
                role = LocalDeviceRole(role.device_id, role.hostname, is_main)
        result.local_role = role
        result.is_main_device = is_main
        result.main_device_hostname = main_device.hostname if main_device else ""
        result.lease_token = (
            main_device.lease_token if is_main and main_device else ""
        )

        # Once startup reconciliation has established the base revisions, the
        # exclusive writer only needs to verify that it still owns the lease.
        # Normal saves already push changed keys.  Avoiding redundant pulls
        # also avoids racing a user edit made while the background poll runs.
        if is_main and ownership_only_when_main:
            return result

        sync_entries = _read_sync_entries()
        metadata_updates: Dict[str, Dict[str, Any]] = {}
        key_to_file = _synced_key_to_file()

        for state_key in SYNCED_STATE_KEYS:
            path = key_to_file[state_key]
            local_payload = load_json(path, {})
            local_hash = _state_payload_hash(local_payload)
            base_entry = sync_entries.get(state_key)
            has_base, base_revision, base_hash = _base_sync_values(base_entry)
            local_dirty = has_base and local_hash != base_hash

            pulled = pull_state(engine, state_key)
            if pulled.status == PULL_ERROR:
                result.errors.append(
                    f"Could not read remote {state_key}: {pulled.error or 'unknown error'}"
                )
                continue
            remote = pulled.state if pulled.status == PULL_OK else None

            if not is_main:
                if remote is None:
                    continue
                if not has_base:
                    _apply_remote_state(
                        state_key, path, remote, result, metadata_updates
                    )
                    continue
                if remote.revision == base_revision:
                    continue
                if local_dirty:
                    result.conflict_keys.add(state_key)
                    continue
                _apply_remote_state(state_key, path, remote, result, metadata_updates)
                continue

            if not has_base:
                expected_revision = remote.revision if remote is not None else 0
                pushed = push_state(
                    engine,
                    state_key,
                    local_payload,
                    device_id=role.device_id,
                    expected_revision=expected_revision,
                )
                if pushed.status == PUSH_WRITTEN:
                    metadata_updates[state_key] = _sync_entry(
                        pushed.revision,
                        local_hash,
                        pushed.updated_at,
                    )
                elif pushed.status == PUSH_NOT_MAIN:
                    role = _demote_after_lost_ownership(role, result)
                    result.errors.append(pushed.error)
                    break
                elif pushed.status == PUSH_CONFLICT:
                    result.conflict_keys.add(state_key)
                else:
                    result.errors.append(
                        f"Could not publish local {state_key}: {pushed.error}"
                    )
                continue

            if remote is None:
                # A remotely deleted row is a conflict, not permission to
                # recreate it from a possibly stale local snapshot.
                result.conflict_keys.add(state_key)
                continue

            remote_changed = remote.revision != base_revision
            if remote_changed and local_dirty:
                result.conflict_keys.add(state_key)
            elif remote_changed:
                _apply_remote_state(state_key, path, remote, result, metadata_updates)
            elif local_dirty:
                pushed = push_state(
                    engine,
                    state_key,
                    local_payload,
                    device_id=role.device_id,
                    expected_revision=base_revision,
                )
                if pushed.status == PUSH_WRITTEN:
                    metadata_updates[state_key] = _sync_entry(
                        pushed.revision,
                        local_hash,
                        pushed.updated_at,
                    )
                elif pushed.status == PUSH_CONFLICT:
                    result.conflict_keys.add(state_key)
                else:
                    if pushed.status == PUSH_NOT_MAIN:
                        role = _demote_after_lost_ownership(role, result)
                    result.errors.append(
                        f"Could not publish local {state_key}: {pushed.error}"
                    )
                    if pushed.status == PUSH_NOT_MAIN:
                        break

        if metadata_updates:
            _update_sync_entries(metadata_updates)
        return result
    finally:
        if lock is not None:
            lock.release()


def activate_device_as_main(
    engine,
    role: LocalDeviceRole,
    *,
    save_lock: threading.Lock | None = None,
) -> StateReconcileResult:
    """Explicitly transfer main-device ownership, then reconcile safely."""
    if engine is None:
        return StateReconcileResult(
            errors=["State sync database is unavailable."],
            local_role=role,
        )

    # Establish a remote base before taking ownership.  A device that missed
    # its startup pull must never turn an explicit activation into a blind
    # overwrite of an existing remote row.
    pull_only_role = LocalDeviceRole(role.device_id, role.hostname, False)
    prepared = reconcile_state_with_remote(
        engine,
        pull_only_role,
        save_lock=save_lock,
    )
    if prepared.errors or prepared.conflict_keys:
        return prepared

    ownership = claim_main_device(engine, role)
    if not ownership.success:
        return StateReconcileResult(
            errors=[ownership.error or "Could not activate this device as main."],
            local_role=role,
        )
    try:
        role = set_local_device_main(role, True)
    except Exception as exc:
        return StateReconcileResult(
            errors=[f"Main ownership changed, but the local role could not be saved: {exc}"],
            is_main_device=True,
            main_device_hostname=role.hostname,
            local_role=LocalDeviceRole(role.device_id, role.hostname, True),
        )
    return reconcile_state_with_remote(engine, role, save_lock=save_lock)


def should_auto_claim_main(
    engine,
    role: LocalDeviceRole,
    *,
    other_hostname: str = "",
    max_heartbeat_age_seconds: int = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
) -> tuple[bool, str, str]:
    """Decide whether this (unattended) device should auto-claim main status.

    Returns ``(should_claim, expected_owner_device_id, reason)``. This is a
    read-only observation, not the claim itself -- the caller must pass
    ``expected_owner_device_id`` into ``claim_main_device_if_stale`` so the
    actual claim re-verifies both facts atomically instead of trusting what
    was true at observation time (closing the check-then-claim race).
    """
    if role.is_main:
        return False, "", ""
    ownership = get_main_device(engine)
    if not ownership.success:
        # Unknown state (DB hiccup) -- do not act on it.
        return False, "", ""
    main_device = ownership.main_device
    if main_device is None:
        return True, "", "ownership released (clean handoff)"
    if main_device.device_id == role.device_id:
        # Defensive: shouldn't happen if role.is_main was accurate.
        return False, "", ""
    status = get_runtime_process_status(
        engine,
        main_device.hostname or other_hostname,
        process_name=MAIN_APP_PROCESS,
        max_age_seconds=max_heartbeat_age_seconds,
    )
    if status.observed and not status.active:
        age = f"{status.age_seconds:.0f}s" if status.age_seconds is not None else "unknown"
        return True, main_device.device_id, f"stale heartbeat ({age})"
    return False, "", ""


def auto_claim_main_device_if_stale(
    engine,
    role: LocalDeviceRole,
    *,
    expected_owner_device_id: str,
    heartbeat_cutoff_seconds: int = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    save_lock: threading.Lock | None = None,
) -> StateReconcileResult:
    """Automatic, fenced equivalent of ``activate_device_as_main``.

    Used only for unattended handoff (``should_auto_claim_main`` said yes) --
    the manual "Use This Device as Main" button keeps using
    ``activate_device_as_main``/plain ``claim_main_device`` unchanged. The
    claim itself goes through a fenced primitive that re-verifies the
    expected state atomically inside the same row lock, instead of trusting
    the caller's earlier observation:

    - ``expected_owner_device_id`` empty (clean handoff -- ownership was
      released) uses ``claim_main_device_if_unclaimed``, which still
      re-checks the row is genuinely empty at claim time rather than
      blindly overwriting whatever might have claimed it in the interim.
    - Non-empty (stale-heartbeat fallback) uses ``claim_main_device_if_stale``,
      re-verifying both the expected owner and heartbeat staleness.
    """
    if engine is None:
        return StateReconcileResult(
            errors=["State sync database is unavailable."],
            local_role=role,
        )

    # Same reasoning as activate_device_as_main: a device that missed its
    # startup pull must never turn an automatic claim into a blind overwrite
    # of newer remote buylist/execution-queue state.
    pull_only_role = LocalDeviceRole(role.device_id, role.hostname, False)
    prepared = reconcile_state_with_remote(engine, pull_only_role, save_lock=save_lock)
    if prepared.errors or prepared.conflict_keys:
        return prepared

    expected_owner_device_id = str(expected_owner_device_id or "").strip()
    if expected_owner_device_id:
        ownership = claim_main_device_if_stale(
            engine,
            role,
            expected_owner_device_id=expected_owner_device_id,
            heartbeat_cutoff_seconds=heartbeat_cutoff_seconds,
        )
    else:
        ownership = claim_main_device_if_unclaimed(engine, role)
    if not ownership.success:
        return StateReconcileResult(
            errors=[ownership.error or "Could not auto-claim main-device ownership."],
            local_role=role,
        )
    try:
        role = set_local_device_main(role, True)
    except Exception as exc:
        return StateReconcileResult(
            errors=[f"Main ownership changed, but the local role could not be saved: {exc}"],
            is_main_device=True,
            main_device_hostname=role.hostname,
            lease_token=ownership.main_device.lease_token if ownership.main_device else "",
            local_role=LocalDeviceRole(role.device_id, role.hostname, True),
        )
    return reconcile_state_with_remote(engine, role, save_lock=save_lock)


def release_main_device_and_demote(
    engine,
    role: LocalDeviceRole,
    *,
    disable_remote_writer: Callable[[], None] | None = None,
) -> tuple[bool, LocalDeviceRole, str]:
    """Persist pull-only state, fence local writes, then release ownership.

    The ordering is safety-critical. If local demotion or writer fencing fails,
    the remote ownership row is deliberately retained so another device must
    use the stale-heartbeat takeover path instead of observing a clean release.
    """
    if not role.is_main:
        return True, role, ""

    owning_role = role
    try:
        role = set_local_device_main(role, False)
    except Exception as exc:
        return False, owning_role, f"Could not persist pull-only device role: {exc}"

    if disable_remote_writer is not None:
        try:
            disable_remote_writer()
        except Exception as exc:
            return False, role, f"Could not disable remote state writer: {exc}"

    result = release_main_device(engine, owning_role)
    if not result.success:
        return False, role, result.error or "Could not release main-device ownership."
    return True, role, ""


def publish_handoff_snapshot(
    engine,
    role: LocalDeviceRole,
    buylist_dict: Dict[str, Any],
    execution_queue_dict: Dict[str, Any],
    *,
    metadata_path: Path | None = None,
) -> bool:
    """Strictly push buylist + execution-queue state before releasing ownership.

    Unlike ``StateSaveManager._push_to_remote`` (best-effort -- a failed
    remote push there is logged and the overall local save still reports
    success), this returns False unless every push that was attempted
    actually committed. Callers must treat a False return as an *unclean*
    handoff: the next device can still pick it up via the stale-heartbeat
    fallback, but should hold a stricter reconciliation bar before trusting
    the synced snapshot, since it might not reflect this device's last
    moments before shutdown.
    """
    if engine is None:
        return False
    path = metadata_path or STATE_METADATA_FILE
    sync_entries = _read_sync_entries(path)
    payloads = {
        BUYLIST_KEY: buylist_dict,
        EXECUTION_QUEUE_KEY: execution_queue_dict,
    }
    updates: Dict[str, Dict[str, Any]] = {}
    for state_key, payload in payloads.items():
        entry = sync_entries.get(state_key)
        has_base, base_revision, base_hash = _base_sync_values(entry)
        payload_hash = _state_payload_hash(payload)
        if has_base and payload_hash == base_hash:
            continue  # Unchanged -- already published, nothing to do.
        expected_revision = base_revision if has_base else 0
        result = push_state(
            engine,
            state_key,
            payload,
            device_id=role.device_id,
            expected_revision=expected_revision,
        )
        if result.status != PUSH_WRITTEN:
            return False
        updates[state_key] = _sync_entry(result.revision, payload_hash, result.updated_at)
    if updates:
        _update_sync_entries(updates, path)
    return True


def save_app_state(
    watchlist_dict: Dict[str, Any],
    buylist_dict: Dict[str, Any],
    trade_manager_dict: Dict[str, Any],
    scanner_setups_copy: Any,
    chart_drawings_copy: Dict[str, Any],
    tab_options_copy: Dict[str, Any],
    *,
    save_lock: Optional[threading.Lock] = None,
    append_log: Callable[[str], None] | None = None,
) -> threading.Thread:
    return get_state_save_manager().schedule_save(
        watchlist_dict,
        buylist_dict,
        trade_manager_dict,
        scanner_setups_copy,
        chart_drawings_copy,
        tab_options_copy,
        save_lock=save_lock,
        append_log=append_log,
    )
