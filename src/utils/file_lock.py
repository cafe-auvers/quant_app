"""Generic cross-process exclusive file lock.

Extracted from :mod:`src.services.order_ledger`'s original
``_exclusive_ledger_lock`` (still re-exported there under that name for
backward compatibility) so other durable JSON-ledger-style stores --
currently :mod:`src.services.order_ledger` and
:mod:`src.services.capital_allocator` -- can share one implementation
instead of each reinventing file locking.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_LOCK_TIMEOUT_SECONDS = 5.0
_STALE_LOCK_SECONDS = 30.0


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = _STALE_LOCK_SECONDS,
) -> Iterator[None]:
    """Serialize short read-modify-write transactions on ``path`` across
    threads and processes using an ``O_CREAT|O_EXCL`` lock file."""
    lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
        except (FileExistsError, PermissionError) as exc:
            # Windows can surface an exclusive-create collision as
            # PermissionError while another process still owns the file.
            # Only treat it as contention when the lock actually exists;
            # genuine directory/ACL failures must remain visible.
            if isinstance(exc, PermissionError) and not lock_path.exists():
                raise
            try:
                stale = time.time() - lock_path.stat().st_mtime > stale_seconds
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for file lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except OSError:
            pass
