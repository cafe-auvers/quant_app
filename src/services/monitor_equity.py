"""Small, read-only equity projection for the separate web monitor process."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.utils.storage import save_json
from src.utils.config import DATA_DIR


_lock = threading.Lock()
_published: dict[tuple[str, str], tuple[float, str]] = {}
MONITOR_EQUITY_FILE = DATA_DIR / "monitor_equity.json"
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor-equity")


def queue_monitor_equity(snapshot) -> None:
    """Keep file I/O off the Qt callback and preserve the true fetch time."""
    def publish():
        try:
            publish_monitor_equity(path, snapshot)
        except OSError:
            logging.getLogger(__name__).warning("Monitor equity projection unavailable", exc_info=True)

    path = MONITOR_EQUITY_FILE
    _writer.submit(publish)


def _account_key(environment: str, account_no: str) -> str:
    return hashlib.sha256(f"{environment.upper()}:{account_no}".encode()).hexdigest()


def publish_monitor_equity(path: Path, snapshot) -> None:
    """Publish only when the real account fetch timestamp/value changes."""
    if not snapshot.account_no or not math.isfinite(snapshot.total_equity_usd):
        return
    key = _account_key(snapshot.environment, snapshot.account_no)
    value = (snapshot.total_equity_usd, snapshot.received_at.isoformat())
    identity = (str(path), key)
    with _lock:
        if _published.get(identity) == value:
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = {}
        if not isinstance(document, dict):
            document = {}
        document[key] = {"equity_usd": value[0], "received_at": value[1]}
        save_json(path, document)
        _published[identity] = value


def read_monitor_equity(
    path: Path, environment: str, account_no: str, now: dt.datetime,
    *, max_age_seconds: float = 900,
) -> tuple[float | None, str]:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))[_account_key(environment, account_no)]
        equity = float(row["equity_usd"])
        fetched = dt.datetime.fromisoformat(row["received_at"])
        age = (now - fetched).total_seconds()
        if not math.isfinite(equity) or equity <= 0 or not 0 <= age <= max_age_seconds:
            return None, "Account equity is stale or unavailable; refresh the desktop account"
        return equity, ""
    except (OSError, ValueError, KeyError, TypeError):
        return None, "Account equity unavailable; open or refresh the desktop account"
