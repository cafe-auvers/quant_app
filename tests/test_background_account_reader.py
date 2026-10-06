from datetime import datetime, timezone
import threading

import pytest

from src.services.background_account_reader import AccountReadRequest, BackgroundAccountReader


def test_single_bounded_read_and_shutdown_discards_late_completion():
    reader = BackgroundAccountReader()
    started, finish = threading.Event(), threading.Event()
    request = AccountReadRequest("account", True, datetime.now(timezone.utc), 1)

    def read():
        started.set()
        assert finish.wait(3)
        return "read-only-result"

    try:
        reader.submit(request, read)
        assert started.wait(1)
        assert reader.take_completed() is None
        with pytest.raises(RuntimeError):
            reader.submit(request, read)
        pending = reader._pending[1]
        reader.close()
        finish.set()
        assert pending.result(timeout=1) == "read-only-result"
        assert reader.take_completed() is None
        with pytest.raises(RuntimeError):
            reader.submit(request, read)
    finally:
        finish.set()
        reader.close()
