"""Local JSON state loading and saving for the dashboard."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.core.watchlist import BuylistManager, TradePlanManager, Watchlist
from src.utils.storage import load_json, save_json


logger = logging.getLogger(__name__)

WATCHLIST_FILE = Path("data/watchlist.json")
BUYLIST_FILE = Path("data/buylist.json")
TRADE_PLANS_FILE = Path("data/trade_plans.json")
SCANNER_SETUPS_FILE = Path("data/scanner_setups.json")
CHART_DRAWINGS_FILE = Path("data/chart_drawings.json")
TAB_OPTIONS_FILE = Path("data/tab_options.json")
SETTINGS_FILE = Path("data/settings.json")
STATE_METADATA_FILE = Path("data/state_metadata.json")


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
        with self._status_lock:
            self.last_save_status = "running"
            self.last_save_error = ""
            self.last_save_started_at = started_at
            self.last_save_finished_at = None

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
        finally:
            lock.release()

        self._store_result(result)
        self._write_metadata(result, append_log=append_log)
        if not result.success:
            self._log_failure(result, append_log)
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
    return Watchlist.from_dict(load_json(WATCHLIST_FILE, {"name": "Default", "items": []}))


def load_buylist_state() -> BuylistManager:
    return BuylistManager.from_dict(load_json(BUYLIST_FILE, {"items": []}))


def load_trade_plans_state() -> TradePlanManager:
    return TradePlanManager.from_dict(load_json(TRADE_PLANS_FILE, {"plans": []}))


def load_scanner_setups_state(defaults: Dict[str, Any]) -> Dict[str, Any]:
    return load_json(SCANNER_SETUPS_FILE, {"setups": defaults})


def load_tab_options_state(defaults: Dict[str, Any]) -> Dict[str, Any]:
    return load_json(TAB_OPTIONS_FILE, {"tabs": defaults})


def load_chart_drawings_state() -> Dict[str, Any]:
    data = load_json(CHART_DRAWINGS_FILE, {})
    if not isinstance(data, dict):
        return {}

    normalized = {}
    for symbol, drawings in data.items():
        symbol_key = str(symbol).strip().upper()
        if not symbol_key or not isinstance(drawings, list):
            continue
        clean_drawings = []
        for index, drawing in enumerate(drawings):
            if not isinstance(drawing, dict) or drawing.get("type") != "line":
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
            except (KeyError, TypeError, ValueError):
                continue
        if clean_drawings:
            normalized[symbol_key] = clean_drawings
    return normalized


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
