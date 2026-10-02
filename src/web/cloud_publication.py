from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol


class CloudPublicationError(RuntimeError):
    pass


class CloudSnapshotUpdating(CloudPublicationError):
    pass


class CloudSnapshotUnavailable(CloudPublicationError):
    pass


class CloudSnapshotBackend(Protocol):
    def begin_publication(
        self, symbol: str, timeframe: str, metadata: dict[str, Any]
    ) -> int: ...

    def upload_current(
        self, symbol: str, timeframe: str, content: bytes, checksum: str
    ) -> None: ...

    def finish_publication(
        self,
        symbol: str,
        timeframe: str,
        revision: int,
        *,
        state: str,
        checksum: str,
        compressed_bytes: int,
    ) -> bool: ...

    def manifest(self, symbol: str, timeframe: str) -> dict[str, Any] | None: ...

    def download_current(
        self, symbol: str, timeframe: str, *, cache_bust: str
    ) -> bytes: ...


@dataclass(frozen=True)
class PublishedCloudSnapshot:
    symbol: str
    timeframe: str
    revision: int
    checksum: str
    compressed_bytes: int


class CloudSnapshotPublisher:
    """Serialize one-current-object publication and fail closed between steps."""

    def __init__(self, backend: CloudSnapshotBackend, *, verification_attempts: int = 3):
        if verification_attempts < 1 or verification_attempts > 10:
            raise ValueError("verification_attempts must be between 1 and 10")
        self.backend = backend
        self.verification_attempts = verification_attempts
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, symbol: str, timeframe: str) -> threading.Lock:
        key = f"{symbol.upper()}:{timeframe.upper()}"
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def publish(
        self,
        symbol: str,
        timeframe: str,
        content: bytes,
        metadata: dict[str, Any],
    ) -> PublishedCloudSnapshot:
        symbol = symbol.strip().upper()
        timeframe = timeframe.strip().upper()
        if not symbol or timeframe not in {"1D", "1H"}:
            raise CloudPublicationError("Invalid chart object identity")
        checksum = hashlib.sha256(content).hexdigest()
        lock = self._lock_for(symbol, timeframe)
        with lock:
            revision = self.backend.begin_publication(symbol, timeframe, metadata)
            try:
                self.backend.upload_current(
                    symbol, timeframe, content, checksum
                )
                verified = False
                for attempt in range(self.verification_attempts):
                    returned = self.backend.download_current(
                        symbol,
                        timeframe,
                        cache_bust=f"verify-{revision}-{attempt}",
                    )
                    if hashlib.sha256(returned).hexdigest() == checksum:
                        verified = True
                        break
                if not verified:
                    raise CloudPublicationError(
                        "Cloud object remained stale or mismatched after bounded verification"
                    )
            except Exception:
                self.backend.finish_publication(
                    symbol,
                    timeframe,
                    revision,
                    state="FAILED",
                    checksum=checksum,
                    compressed_bytes=len(content),
                )
                raise
            if not self.backend.finish_publication(
                symbol,
                timeframe,
                revision,
                state="READY",
                checksum=checksum,
                compressed_bytes=len(content),
            ):
                raise CloudPublicationError(
                    "Publication revision changed before READY could be committed"
                )
        return PublishedCloudSnapshot(
            symbol, timeframe, revision, checksum, len(content)
        )


class CloudSnapshotReader:
    """Accept bytes only when a READY manifest matches the bytes returned."""

    def __init__(self, backend: CloudSnapshotBackend, *, freshness_attempts: int = 3):
        if freshness_attempts < 1 or freshness_attempts > 10:
            raise ValueError("freshness_attempts must be between 1 and 10")
        self.backend = backend
        self.freshness_attempts = freshness_attempts

    def read(self, symbol: str, timeframe: str) -> dict[str, Any]:
        manifest = self.backend.manifest(symbol, timeframe)
        if not manifest:
            raise CloudSnapshotUnavailable("No chart manifest exists")
        state = str(manifest.get("publication_state") or "").upper()
        if state == "UPDATING":
            raise CloudSnapshotUpdating("Chart snapshot is updating")
        if state != "READY":
            raise CloudSnapshotUnavailable("Chart snapshot is not READY")
        checksum = str(manifest.get("checksum_sha256") or "")
        revision = int(manifest.get("revision") or 0)
        for attempt in range(self.freshness_attempts):
            content = self.backend.download_current(
                symbol,
                timeframe,
                cache_bust=f"read-{revision}-{attempt}-{time.time_ns()}",
            )
            if hashlib.sha256(content).hexdigest() != checksum:
                continue
            try:
                payload = json.loads(gzip.decompress(content))
            except (gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise CloudSnapshotUnavailable("Chart object is corrupt") from exc
            return payload
        raise CloudSnapshotUpdating(
            "Manifest is current but object storage still serves stale bytes"
        )
