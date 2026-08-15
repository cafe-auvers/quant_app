"""Best-effort, append-only audit journal for live trading events.

Journal failures are deliberately isolated from the trading state machine: an
observability problem must never cause a second order attempt or change an
order outcome.  Secrets and full account numbers are removed before anything
is written to disk.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from uuid import uuid4

from src.utils.config import DATA_DIR
from src.utils.redaction import mask_account_number, redact_payload, scrub_sensitive_text

EVENT_JOURNAL_FILE = DATA_DIR / "event_journal.jsonl"
MAX_JOURNAL_BYTES = 25 * 1024 * 1024
MAX_JOURNAL_ARCHIVES = 20
_LOCK_TIMEOUT_SECONDS = 2.0
_STALE_LOCK_SECONDS = 30.0
_THREAD_LOCK = threading.RLock()
_STATUS_LOCK = threading.Lock()
_RUNTIME_STATUS: Dict[str, Dict[str, str]] = {}
logger = logging.getLogger(__name__)


class EventType(str, Enum):
    SIGNAL_CREATED = "SIGNAL_CREATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_INTENT_CREATED = "ORDER_INTENT_CREATED"
    ORDER_RESERVED = "ORDER_RESERVED"
    ORDER_SUBMISSION_STARTED = "ORDER_SUBMISSION_STARTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_SUBMISSION_UNKNOWN = "ORDER_SUBMISSION_UNKNOWN"
    ORDER_WORKING = "ORDER_WORKING"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCEL_REQUESTED = "ORDER_CANCEL_REQUESTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_CLOSED = "POSITION_CLOSED"
    RECONCILIATION_STARTED = "RECONCILIATION_STARTED"
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    RECONCILIATION_WARNING = "RECONCILIATION_WARNING"


@dataclass(frozen=True)
class EventJournalStatus:
    path: Path
    directory_writable: bool
    lock_available: bool
    lock_stale: bool
    last_write_at: str
    last_error: str
    last_error_at: str
    latest_event_at: str
    active_file_size: int
    archive_count: int
    archive_bytes: int
    available_disk_space: int
    inspection_error: str = ""


# mask_account_number/scrub_sensitive_text are re-exported (imported above)
# from src.utils.redaction (revision 3.2 of
# docs/kanban_production_readiness.md) -- moved there so core-layer modules
# don't need to import a private function from this services-layer module.
# _safe_payload is kept here as a thin alias to the shared redact_payload so
# every existing internal call site below is unaffected by the move.
_safe_payload = redact_payload


def _status_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _record_write_success(path: Path, timestamp: str) -> None:
    with _STATUS_LOCK:
        status = _RUNTIME_STATUS.setdefault(_status_key(path), {})
        status["last_write_at"] = timestamp
        status["last_error"] = ""
        status["last_error_at"] = ""


def _record_write_error(
    path: Path, error: BaseException | str, *, account_no: Any = ""
) -> None:
    with _STATUS_LOCK:
        status = _RUNTIME_STATUS.setdefault(_status_key(path), {})
        status["last_error"] = scrub_sensitive_text(error, account_no=account_no)
        status["last_error_at"] = dt.datetime.now(dt.timezone.utc).isoformat()


def reset_event_journal_runtime_status_for_tests() -> None:
    with _STATUS_LOCK:
        _RUNTIME_STATUS.clear()


@contextmanager
def _exclusive_journal_lock(path: Path) -> Iterator[None]:
    lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    descriptor: Optional[int] = None
    with _THREAD_LOCK:
        while descriptor is None:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
            except (FileExistsError, PermissionError) as exc:
                if isinstance(exc, PermissionError) and not lock_path.exists():
                    raise
                try:
                    stale = (
                        time.time() - lock_path.stat().st_mtime > _STALE_LOCK_SECONDS
                    )
                except OSError:
                    stale = False
                if stale:
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for event-journal lock: {lock_path}"
                    )
                time.sleep(0.025)
        try:
            yield
        finally:
            os.close(descriptor)
            try:
                lock_path.unlink()
            except OSError:
                pass


def _archive_paths(path: Path, *, newest_first: bool = True) -> List[Path]:
    archive_name = re.compile(
        rf"^{re.escape(path.stem)}\.\d{{8}}T\d{{6}}-\d+{re.escape(path.suffix)}$"
    )
    parent = path.parent.resolve(strict=False)
    candidates = [
        candidate
        for candidate in path.parent.glob(f"{path.stem}.*{path.suffix}")
        if archive_name.fullmatch(candidate.name)
        and candidate.resolve(strict=False).parent == parent
    ]
    return sorted(
        candidates,
        key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
        reverse=newest_first,
    )


def _rotate_if_needed(path: Path, incoming_bytes: int) -> bool:
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        return False
    if current_size + incoming_bytes <= MAX_JOURNAL_BYTES:
        return False
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = path.with_name(f"{path.stem}.{stamp}-{time.time_ns()}{path.suffix}")
    os.replace(path, archive)
    return True


def _enforce_archive_retention(path: Path) -> None:
    """Keep newest archives only; never delete outside the journal directory."""
    archive_limit = max(0, int(MAX_JOURNAL_ARCHIVES))
    parent = path.parent.resolve(strict=False)
    for archive in _archive_paths(path)[archive_limit:]:
        if archive.resolve(strict=False).parent != parent:
            raise OSError(f"Refusing to prune journal archive outside {parent}")
        archive.unlink()


def append_event(
    event_type: EventType | str,
    *,
    strategy_id: str = "",
    symbol: str = "",
    signal_id: str = "",
    order_id: str = "",
    broker_order_id: str = "",
    environment: str = "",
    account_no: str = "",
    price: Optional[float] = None,
    quantity: Optional[int] = None,
    reason: str = "",
    payload: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one validated event and return the exact redacted record."""
    path = Path(path or EVENT_JOURNAL_FILE)
    event_name = (
        (event_type.value if isinstance(event_type, EventType) else str(event_type))
        .strip()
        .upper()
    )
    if not event_name:
        raise ValueError("event_type is required")
    safe_account = mask_account_number(account_no)
    record: Dict[str, Any] = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event_id": uuid4().hex,
        "event_type": event_name,
        "strategy_id": scrub_sensitive_text(strategy_id, account_no=account_no),
        "symbol": str(symbol or "").strip().upper(),
        "signal_id": scrub_sensitive_text(signal_id, account_no=account_no),
        "order_id": scrub_sensitive_text(order_id, account_no=account_no),
        "broker_order_id": scrub_sensitive_text(broker_order_id, account_no=account_no),
        "environment": str(environment or "").strip().upper(),
        "account": safe_account,
        "price": price,
        "quantity": quantity,
        "reason": scrub_sensitive_text(reason, account_no=account_no),
        "payload": _safe_payload(payload or {}, account_no=account_no),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        with _exclusive_journal_lock(path):
            _rotate_if_needed(path, len(encoded))
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _record_write_success(path, record["timestamp"])
            # Retention runs only after the replacement active file has been
            # opened, written, and fsynced successfully.
            try:
                _enforce_archive_retention(path)
            except OSError as exc:
                _record_write_error(path, exc)
                logger.exception("Could not prune old event-journal archives")
    except Exception as exc:
        _record_write_error(path, exc, account_no=account_no)
        raise
    return record


def record_event(event_type: EventType | str, **kwargs: Any) -> bool:
    """Best-effort adapter for execution paths; never affects their outcome."""
    try:
        append_event(event_type, **kwargs)
        return True
    except Exception as exc:
        _record_write_error(
            Path(kwargs.get("path") or EVENT_JOURNAL_FILE),
            exc,
            account_no=kwargs.get("account_no", ""),
        )
        logger.exception("Could not append %s to the trading event journal", event_type)
        return False


def load_recent_events(
    *,
    limit: int = 250,
    symbol: str = "",
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Load the newest valid events, newest first, ignoring torn lines."""
    limit = max(0, min(int(limit), 5000))
    if limit == 0:
        return []
    wanted_symbol = str(symbol or "").strip().upper()
    path = Path(path or EVENT_JOURNAL_FILE)
    archives = sorted(
        path.parent.glob(f"{path.stem}.*{path.suffix}"),
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    )
    paths = [path, *archives]
    events: List[Dict[str, Any]] = []
    for candidate in paths:
        if not candidate.exists():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            logger.exception("Could not read event journal %s", candidate)
            continue
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or not event.get("event_type"):
                continue
            if (
                wanted_symbol
                and str(event.get("symbol") or "").upper() != wanted_symbol
            ):
                continue
            events.append(event)
            if len(events) >= limit:
                return events
    return events


def inspect_event_journal(path: Optional[Path] = None) -> EventJournalStatus:
    """Return read-only storage and runtime-write health for the journal."""
    path = Path(path or EVENT_JOURNAL_FILE)
    inspection_errors: List[str] = []
    try:
        active_file_size = path.stat().st_size
    except FileNotFoundError:
        active_file_size = 0
    except OSError as exc:
        active_file_size = 0
        inspection_errors.append(str(exc))

    try:
        archives = _archive_paths(path)
        archive_bytes = sum(archive.stat().st_size for archive in archives)
    except OSError as exc:
        archives = []
        archive_bytes = 0
        inspection_errors.append(str(exc))

    existing_parent = path.parent
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    directory_writable = bool(
        existing_parent.is_dir() and os.access(existing_parent, os.W_OK)
    )
    try:
        available_disk_space = shutil.disk_usage(existing_parent).free
    except OSError as exc:
        available_disk_space = 0
        inspection_errors.append(str(exc))

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_stale = False
    lock_available = True
    if lock_path.exists():
        try:
            lock_stale = time.time() - lock_path.stat().st_mtime > _STALE_LOCK_SECONDS
            lock_available = lock_stale
        except OSError as exc:
            lock_available = False
            inspection_errors.append(str(exc))

    latest_events = load_recent_events(limit=1, path=path)
    latest_event_at = (
        str(latest_events[0].get("timestamp") or "") if latest_events else ""
    )
    with _STATUS_LOCK:
        runtime = dict(_RUNTIME_STATUS.get(_status_key(path), {}))
    return EventJournalStatus(
        path=path,
        directory_writable=directory_writable,
        lock_available=lock_available,
        lock_stale=lock_stale,
        last_write_at=str(runtime.get("last_write_at") or ""),
        last_error=str(runtime.get("last_error") or ""),
        last_error_at=str(runtime.get("last_error_at") or ""),
        latest_event_at=latest_event_at,
        active_file_size=max(0, int(active_file_size)),
        archive_count=len(archives),
        archive_bytes=max(0, int(archive_bytes)),
        available_disk_space=max(0, int(available_disk_space)),
        inspection_error="; ".join(inspection_errors),
    )


EventRecorder = Callable[..., bool]
