"""Bounded per-card execution with a separate protective worker pool."""
from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor
import logging
import threading

logger = logging.getLogger(__name__)


class BackgroundSymbolExecutor:
    def __init__(self, *, ordinary_workers=2, protective_workers=8):
        self._ordinary = ThreadPoolExecutor(max_workers=ordinary_workers, thread_name_prefix="entry-execution")
        self._protective = ThreadPoolExecutor(max_workers=protective_workers, thread_name_prefix="protective-execution")
        self._lock = threading.RLock()
        self._active = {}
        self._pending = {}
        self._closed = False

    def dispatch(self, key, operation, *, protective=False, critical=False):
        with self._lock:
            if self._closed:
                return False
            if key in self._active:
                previous = self._pending.get(key)
                priority = 2 if critical else 1 if protective else 0
                if previous is None or priority >= previous[2]:
                    self._pending[key] = (operation, protective, priority)
                future, active_protective = self._active[key]
                if protective and not active_protective and future is not None:
                    # Cancel only work that has not started. A broker call
                    # already in progress must finish and retain its identity.
                    future.cancel()
                return False
            self._start(key, operation, protective, 2 if critical else 1 if protective else 0)
            return True

    def _start(self, key, operation, protective, priority):
        # Install the fence before a new worker can finish.
        self._active[key] = (None, protective)
        pool = self._protective if protective else self._ordinary
        future = pool.submit(operation)
        self._active[key] = (future, protective)
        future.add_done_callback(lambda completed: self._finished(key, completed))

    def _finished(self, key, future):
        with self._lock:
            active = self._active.get(key)
            if active is None or active[0] is not future:
                return
            self._active.pop(key, None)
            next_job = self._pending.pop(key, None)
            if next_job and not self._closed:
                self._start(key, *next_job)
        try:
            future.result()
        except CancelledError:
            pass
        except Exception:
            logger.exception("Symbol execution task failed; durable identities will be reconciled")

    def close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()
        # Keep the execution lease until in-flight operations finish; cancel
        # queued jobs before they have entered a broker boundary.
        self._ordinary.shutdown(wait=True, cancel_futures=True)
        self._protective.shutdown(wait=True, cancel_futures=True)
