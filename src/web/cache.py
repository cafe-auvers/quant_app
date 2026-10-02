from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import anyio

from .market_data import MarketDataSource
from .store import ValidationError, normalize_symbol


EXPECTED_PAYLOAD_SCHEMA_VERSION = 6


@dataclass(frozen=True)
class CachedBundle:
    payload: dict[str, Any]
    checksum: str
    compressed_bytes: int
    cache_hit: bool


@dataclass(frozen=True)
class PublicationArtifact:
    content: bytes
    metadata: dict[str, Any]


class ChartBundleCache:
    """One validated current gzip bundle per instrument and timeframe."""

    def __init__(self, directory: str | Path, *, max_symbols: int = 350):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_symbols = max_symbols
        self._lock = threading.RLock()

    @staticmethod
    def _key(symbol: str, timeframe: str) -> str:
        symbol = normalize_symbol(symbol)
        timeframe = timeframe.strip().upper()
        if timeframe not in {"1D", "1H"}:
            raise ValueError("timeframe must be 1D or 1H")
        return f"{symbol}.{timeframe}"

    def _paths(self, symbol: str, timeframe: str) -> tuple[Path, Path]:
        key = self._key(symbol, timeframe)
        return self.directory / f"{key}.json.gz", self.directory / f"{key}.manifest.json"

    @staticmethod
    def _source_matches(actual: object, expected: str) -> bool:
        actual_text = str(actual or "")
        expected_text = str(expected or "")
        return not expected_text or actual_text == expected_text or actual_text.startswith(
            f"{expected_text}:"
        )

    def write(
        self, payload: dict[str, Any], *, generation_key: str = ""
    ) -> CachedBundle:
        symbol = str(payload["symbol"])
        timeframe = str(payload["timeframe"])
        data = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        checksum = hashlib.sha256(data).hexdigest()
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        compressed_checksum = hashlib.sha256(compressed).hexdigest()
        bundle_path, manifest_path = self._paths(symbol, timeframe)
        manifest = {
            "schema_version": 2,
            "payload_schema_version": int(payload.get("schema_version", 1)),
            "symbol": normalize_symbol(symbol),
            "timeframe": timeframe.upper(),
            "checksum": checksum,
            "compressed_checksum": compressed_checksum,
            "compressed_bytes": len(compressed),
            "uncompressed_bytes": len(data),
            "source": payload.get("coverage", {}).get("source", "UNKNOWN"),
            "source_revision": payload.get("coverage", {}).get(
                "source_revision", ""
            ),
            "adjustment_mode": payload.get("coverage", {}).get(
                "adjustment_mode", "UNKNOWN"
            ),
            "session_policy": payload.get("coverage", {}).get(
                "session_policy", "UNKNOWN"
            ),
            "generation_key": str(generation_key or ""),
            "published_at_epoch": time.time(),
            "state": "READY",
        }
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with self._lock:
            self._atomic_replace(bundle_path, compressed)
            self._atomic_replace(manifest_path, manifest_bytes)
        return CachedBundle(payload, checksum, len(compressed), False)

    @staticmethod
    def _atomic_replace(path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def read(
        self,
        symbol: str,
        timeframe: str,
        *,
        expected_source: str = "",
        expected_source_revision: str = "",
        expected_adjustment_mode: str = "",
        expected_session_policy: str = "",
        expected_generation_key: str = "",
    ) -> CachedBundle | None:
        bundle_path, manifest_path = self._paths(symbol, timeframe)
        with self._lock:
            if not bundle_path.is_file() or not manifest_path.is_file():
                return None
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not self._source_matches(manifest.get("source"), expected_source):
                    return None
                if (
                    expected_source_revision
                    and manifest.get("source_revision") != expected_source_revision
                ):
                    return None
                if (
                    expected_adjustment_mode
                    and manifest.get("adjustment_mode") != expected_adjustment_mode
                ):
                    return None
                if (
                    expected_session_policy
                    and manifest.get("session_policy") != expected_session_policy
                ):
                    return None
                if (
                    expected_generation_key
                    and manifest.get("generation_key") != expected_generation_key
                ):
                    return None
                compressed = bundle_path.read_bytes()
                raw = gzip.decompress(compressed)
                checksum = hashlib.sha256(raw).hexdigest()
                if checksum != manifest.get("checksum"):
                    return None
                payload = json.loads(raw)
                if int(payload.get("schema_version", 1)) != EXPECTED_PAYLOAD_SCHEMA_VERSION:
                    return None
            except (OSError, ValueError, gzip.BadGzipFile, json.JSONDecodeError):
                return None
            now = time.time()
            os.utime(bundle_path, (now, now))
            os.utime(manifest_path, (now, now))
        return CachedBundle(payload, checksum, len(compressed), True)

    def publication_artifact(
        self,
        symbol: str,
        timeframe: str,
        *,
        expected_source: str = "",
        expected_source_revision: str = "",
        expected_adjustment_mode: str = "",
        expected_session_policy: str = "",
        expected_generation_key: str = "",
    ) -> PublicationArtifact | None:
        """Return validated compressed bytes without creating another generation."""

        bundle_path, manifest_path = self._paths(symbol, timeframe)
        with self._lock:
            if not bundle_path.is_file() or not manifest_path.is_file():
                return None
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not self._source_matches(manifest.get("source"), expected_source):
                    return None
                if (
                    expected_source_revision
                    and manifest.get("source_revision") != expected_source_revision
                ):
                    return None
                if (
                    expected_adjustment_mode
                    and manifest.get("adjustment_mode") != expected_adjustment_mode
                ):
                    return None
                if (
                    expected_session_policy
                    and manifest.get("session_policy") != expected_session_policy
                ):
                    return None
                if (
                    expected_generation_key
                    and manifest.get("generation_key") != expected_generation_key
                ):
                    return None
                if int(manifest.get("payload_schema_version", 1)) != EXPECTED_PAYLOAD_SCHEMA_VERSION:
                    return None
                compressed = bundle_path.read_bytes()
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            if len(compressed) != int(manifest.get("compressed_bytes", -1)):
                return None
            compressed_checksum = hashlib.sha256(compressed).hexdigest()
            expected_compressed_checksum = manifest.get("compressed_checksum")
            if expected_compressed_checksum:
                if compressed_checksum != expected_compressed_checksum:
                    return None
            else:
                # Validate a legacy v1 artifact once, then upgrade its manifest so
                # future HTTP cache hits never decompress or parse the payload.
                try:
                    raw = gzip.decompress(compressed)
                except (OSError, gzip.BadGzipFile):
                    return None
                if hashlib.sha256(raw).hexdigest() != manifest.get("checksum"):
                    return None
                manifest["schema_version"] = 2
                manifest["compressed_checksum"] = compressed_checksum
                manifest_bytes = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                self._atomic_replace(manifest_path, manifest_bytes)
        return PublicationArtifact(compressed, manifest)

    def evict(self, *, pinned_symbols: set[str]) -> list[str]:
        pinned = {normalize_symbol(symbol) for symbol in pinned_symbols}
        bundles = list(self.directory.glob("*.json.gz"))
        symbols = {path.name.split(".", 1)[0] for path in bundles}
        excess = max(0, len(symbols) - self.max_symbols)
        if excess == 0:
            return []
        candidates = sorted(
            (
                path
                for path in bundles
                if path.name.split(".", 1)[0] not in pinned
            ),
            key=lambda path: path.stat().st_mtime,
        )
        removed: list[str] = []
        for path in candidates:
            symbol = path.name.split(".", 1)[0]
            if symbol in removed:
                continue
            for target in self.directory.glob(f"{symbol}.*"):
                if target.is_file():
                    target.unlink()
            removed.append(symbol)
            if len(removed) >= excess:
                break
        return removed


class ChartLoadCoordinator:
    """Bounded, deduplicated chart preparation outside the event loop."""

    def __init__(
        self,
        source: MarketDataSource,
        cache: ChartBundleCache,
        *,
        daily_bars: int,
        hourly_months: int,
        worker_limit: int = 4,
        pinned_symbols: Callable[[], set[str]] | None = None,
    ) -> None:
        self.source = source
        self.cache = cache
        self.daily_bars = daily_bars
        self.hourly_months = hourly_months
        self._pinned_symbols = pinned_symbols or (lambda: set())
        self._runtime_pins: set[str] = set()
        self._pin_lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(worker_limit)
        self._inflight: dict[str, asyncio.Task[CachedBundle]] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self.request_count = 0

    def pin_symbols(self, symbols: list[str] | tuple[str, ...] | set[str]) -> None:
        normalized: set[str] = set()
        for symbol in symbols:
            try:
                normalized.add(normalize_symbol(symbol))
            except ValidationError:
                continue
        with self._pin_lock:
            self._runtime_pins.update(normalized)

    def _all_pins(self) -> set[str]:
        with self._pin_lock:
            runtime = set(self._runtime_pins)
        return runtime | set(self._pinned_symbols())

    def _cache_expectations(self) -> dict[str, str]:
        return {
            "expected_source": self.source.source_name,
            "expected_source_revision": self.source.cache_revision(),
            "expected_adjustment_mode": str(
                getattr(self.source, "adjustment_mode", "") or ""
            ),
            "expected_session_policy": str(
                getattr(self.source, "session_policy", "") or ""
            ),
            "expected_generation_key": (
                f"schema={EXPECTED_PAYLOAD_SCHEMA_VERSION};"
                f"daily_bars={self.daily_bars};hourly_months={self.hourly_months}"
            ),
        }

    @staticmethod
    def _key(symbol: str, timeframe: str) -> str:
        return f"{normalize_symbol(symbol)}:{timeframe.strip().upper()}"

    def status(self, symbol: str, timeframe: str) -> dict[str, Any]:
        key = self._key(symbol, timeframe)
        return dict(self._states.get(key, {"state": "NOT_CACHED"}))

    async def get(
        self, symbol: str, timeframe: str, *, refresh: bool = False
    ) -> CachedBundle:
        symbol = normalize_symbol(symbol)
        timeframe = timeframe.strip().upper()
        self.request_count += 1
        return await self._get_payload(symbol, timeframe, refresh=refresh)

    async def get_compressed(
        self, symbol: str, timeframe: str, *, refresh: bool = False
    ) -> tuple[PublicationArtifact, bool]:
        """Return the publish-ready gzip file without JSON decode/re-encode."""

        symbol = normalize_symbol(symbol)
        timeframe = timeframe.strip().upper()
        self.request_count += 1
        if not refresh:
            artifact = await anyio.to_thread.run_sync(
                lambda: self.cache.publication_artifact(
                    symbol,
                    timeframe,
                    **self._cache_expectations(),
                )
            )
            if artifact is not None:
                self._states[self._key(symbol, timeframe)] = {
                    "state": "READY",
                    "cache": "HIT",
                }
                return artifact, True
        await self._get_payload(symbol, timeframe, refresh=True)
        artifact = await anyio.to_thread.run_sync(
            lambda: self.cache.publication_artifact(
                symbol,
                timeframe,
                **self._cache_expectations(),
            )
        )
        if artifact is None:
            raise RuntimeError("Chart bundle was generated but could not be published")
        return artifact, False

    async def _get_payload(
        self, symbol: str, timeframe: str, *, refresh: bool
    ) -> CachedBundle:
        if not refresh:
            cached = await anyio.to_thread.run_sync(
                lambda: self.cache.read(
                    symbol,
                    timeframe,
                    **self._cache_expectations(),
                )
            )
            if cached is not None:
                self._states[self._key(symbol, timeframe)] = {
                    "state": "READY",
                    "cache": "HIT",
                }
                return cached
        key = self._key(symbol, timeframe)
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                self._states[key] = {"state": "QUEUED", "cache": "MISS"}
                task = asyncio.create_task(self._load(symbol, timeframe, key))
                self._inflight[key] = task
        try:
            return await task
        finally:
            async with self._lock:
                if self._inflight.get(key) is task and task.done():
                    self._inflight.pop(key, None)

    async def _load(self, symbol: str, timeframe: str, key: str) -> CachedBundle:
        async with self._semaphore:
            self._states[key] = {"state": "LOADING", "cache": "MISS"}
            try:
                payload = await anyio.to_thread.run_sync(
                    lambda: self.source.chart_bundle(
                        symbol,
                        timeframe,
                        daily_bars=self.daily_bars,
                        hourly_months=self.hourly_months,
                    )
                )
                result = await anyio.to_thread.run_sync(
                    lambda: self.cache.write(
                        payload,
                        generation_key=self._cache_expectations()[
                            "expected_generation_key"
                        ],
                    )
                )
                await anyio.to_thread.run_sync(
                    lambda: self.cache.evict(
                        pinned_symbols={
                            *self._all_pins(),
                            symbol,
                        }
                    )
                )
            except Exception as exc:
                self._states[key] = {
                    "state": "FAILED",
                    "reason": str(exc)[:300],
                }
                raise
            self._states[key] = {
                "state": payload.get("coverage", {}).get("completeness", "READY"),
                "cache": "MISS",
                "checksum": result.checksum,
            }
            return result
