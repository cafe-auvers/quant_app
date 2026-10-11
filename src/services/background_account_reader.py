"""One bounded read-only broker job; results are applied by the owner thread."""
from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass(frozen=True)
class AccountReadRequest:
    account_no: str
    reconcile: bool
    started_at: datetime
    generation: object


class BackgroundAccountReader:
    def __init__(self) -> None:
        self._pending: tuple[AccountReadRequest, Future] | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._pending is not None

    def submit(self, request: AccountReadRequest, read: Callable[[], object]) -> None:
        if self._closed or self.busy:
            raise RuntimeError("Account reader is closed or already busy")
        future = Future()
        self._pending = (request, future)

        def fetch() -> None:
            try:
                future.set_result(read())
            except Exception as exc:
                future.set_exception(exc)

        threading.Thread(target=fetch, name="kis-account-reader", daemon=True).start()

    def take_completed(self) -> tuple[AccountReadRequest, Future] | None:
        if self._closed or self._pending is None or not self._pending[1].done():
            return None
        result, self._pending = self._pending, None
        return result

    def close(self) -> None:
        # An in-flight network read may finish, but cannot publish/apply state.
        self._closed = True
        self._pending = None
