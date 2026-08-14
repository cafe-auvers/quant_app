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
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from uuid import uuid4

from src.utils.config import DATA_DIR

EVENT_JOURNAL_FILE = DATA_DIR / "event_journal.jsonl"
MAX_JOURNAL_BYTES = 25 * 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 2.0
_STALE_LOCK_SECONDS = 30.0
_THREAD_LOCK = threading.RLock()
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


_SENSITIVE_KEY_PARTS = (
    "access_token",
    "authorization",
    "app_secret",
    "password",
    "passwd",
    "secret",
    "token",
)


def mask_account_number(account_no: Any) -> str:
    """Return a useful correlation label without persisting the full account."""
    text = str(account_no or "").strip()
    if not text:
        return ""
    compact = text.replace("-", "")
    if len(compact) <= 4:
        return "*" * len(compact)
    return f"{compact[:2]}{'*' * max(4, len(compact) - 4)}{compact[-2:]}"


def _safe_payload(value: Any, *, key: str = "", depth: int = 0) -> Any:
    normalized_key = str(key).strip().lower()
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if depth > 6:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(item_key): _safe_payload(item, key=str(item_key), depth=depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_payload(item, depth=depth + 1) for item in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


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


def _rotate_if_needed(path: Path, incoming_bytes: int) -> None:
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        return
    if current_size + incoming_bytes <= MAX_JOURNAL_BYTES:
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = path.with_name(f"{path.stem}.{stamp}-{time.time_ns()}{path.suffix}")
    os.replace(path, archive)


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
    event_name = (
        (event_type.value if isinstance(event_type, EventType) else str(event_type))
        .strip()
        .upper()
    )
    if not event_name:
        raise ValueError("event_type is required")
    record: Dict[str, Any] = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event_id": uuid4().hex,
        "event_type": event_name,
        "strategy_id": str(strategy_id or ""),
        "symbol": str(symbol or "").strip().upper(),
        "signal_id": str(signal_id or ""),
        "order_id": str(order_id or ""),
        "broker_order_id": str(broker_order_id or ""),
        "environment": str(environment or "").strip().upper(),
        "account": mask_account_number(account_no),
        "price": price,
        "quantity": quantity,
        "reason": str(reason or ""),
        "payload": _safe_payload(payload or {}),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    path = Path(path or EVENT_JOURNAL_FILE)
    with _exclusive_journal_lock(path):
        _rotate_if_needed(path, len(encoded))
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return record


def record_event(event_type: EventType | str, **kwargs: Any) -> bool:
    """Best-effort adapter for execution paths; never affects their outcome."""
    try:
        append_event(event_type, **kwargs)
        return True
    except Exception:
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


EventRecorder = Callable[..., bool]
