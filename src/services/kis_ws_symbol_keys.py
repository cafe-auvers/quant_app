"""Hot-reloadable, local KIS WebSocket subscription-key configuration.

The symbol-to-key map changes with the operator's active trading universe, so
it is runtime state rather than process environment. The canonical file is
gitignored and updated atomically. Readers retain the last-known-good map when
a manual edit is incomplete, malformed, or temporarily unavailable. Process
environment values are deliberately never consulted for symbols.
"""
from __future__ import annotations

import csv
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Dict, Iterator, Mapping, Optional

from src.utils.config import DATA_DIR
from src.utils.storage import save_json


logger = logging.getLogger(__name__)

DEFAULT_KIS_WS_SYMBOL_KEYS_FILE = DATA_DIR / "kis_ws_symbol_keys.json"
DEFAULT_KIS_US_TICKERS_FILE = DATA_DIR / "us_kis_tickers.csv"
MAX_SYMBOL_KEYS_FILE_BYTES = 1024 * 1024

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_REGULAR_SESSION_PREFIX_BY_EXCHANGE = {
    "AMS": "DAMS",
    "AMEX": "DAMS",
    "NAS": "DNAS",
    "NASD": "DNAS",
    "NASDAQ": "DNAS",
    "NYS": "DNYS",
    "NYSE": "DNYS",
}
_WRITE_LOCK_TIMEOUT_SECONDS = 5.0
_STALE_WRITE_LOCK_SECONDS = 30.0


class KisWsSymbolKeysError(RuntimeError):
    """The local symbol-key configuration is unreadable or invalid."""


@dataclass(frozen=True)
class KisWsSymbolKeysSnapshot:
    keys: Mapping[str, str]
    source: str
    generation: int
    sha256: str
    path: Path
    last_error: str = ""


def normalize_symbol_keys(raw: object) -> Dict[str, str]:
    """Validate and normalize one plain ``{symbol: verified_key}`` object."""

    if not isinstance(raw, dict):
        raise KisWsSymbolKeysError("KIS WebSocket symbol-key file must contain a JSON object")

    normalized: Dict[str, str] = {}
    key_owners: Dict[str, str] = {}
    for raw_symbol, raw_key in raw.items():
        symbol = str(raw_symbol or "").strip().upper()
        key = str(raw_key or "").strip()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise KisWsSymbolKeysError(f"invalid KIS WebSocket symbol: {raw_symbol!r}")
        if not key or len(key) > 128 or any(char.isspace() for char in key):
            raise KisWsSymbolKeysError(
                f"invalid KIS WebSocket subscription key for {symbol}"
            )
        if any(ord(char) < 32 for char in key):
            raise KisWsSymbolKeysError(
                f"invalid control character in subscription key for {symbol}"
            )
        existing = normalized.get(symbol)
        if existing is not None and existing != key:
            raise KisWsSymbolKeysError(
                f"conflicting KIS WebSocket keys for normalized symbol {symbol}"
            )
        prior_owner = key_owners.get(key)
        if prior_owner is not None and prior_owner != symbol:
            raise KisWsSymbolKeysError(
                f"KIS WebSocket key is assigned to both {prior_owner} and {symbol}"
            )
        normalized[symbol] = key
        key_owners[key] = symbol
    return dict(sorted(normalized.items()))


def derive_symbol_key_from_kis_master(
    symbol: str,
    path: Path = DEFAULT_KIS_US_TICKERS_FILE,
) -> str:
    """Build one regular-session key from the official KIS symbol master.

    The exchange prefixes are protocol-level values verified for all three
    supported US exchanges. The symbol suffix comes from ``KisSymbol``
    rather than the Yahoo/display spelling, which keeps class-share and other
    broker-specific symbols exact. Ambiguous, missing, or unsupported master
    rows fail closed instead of guessing an exchange.
    """

    normalized_symbol = str(symbol or "").strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized_symbol):
        raise KisWsSymbolKeysError(f"invalid KIS WebSocket symbol: {symbol!r}")
    lookup_symbol = normalized_symbol.replace("/", "-").replace(".", "-")
    target = Path(path)
    try:
        handle = target.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise KisWsSymbolKeysError(
            f"KIS US symbol master cannot be read: {target}"
        ) from exc

    matches: set[str] = set()
    matched_rows = 0
    with handle:
        reader = csv.DictReader(handle)
        required = {"Symbol", "KisSymbol", "Exchange"}
        if not required.issubset(set(reader.fieldnames or ())):
            raise KisWsSymbolKeysError(
                f"KIS US symbol master is missing required columns: {target}"
            )
        for row in reader:
            row_symbol = str(row.get("Symbol") or "").strip().upper()
            row_lookup = row_symbol.replace("/", "-").replace(".", "-")
            if row_lookup != lookup_symbol:
                continue
            matched_rows += 1
            kis_symbol = str(row.get("KisSymbol") or "").strip().upper()
            exchange = str(row.get("Exchange") or "").strip().upper()
            prefix = _REGULAR_SESSION_PREFIX_BY_EXCHANGE.get(exchange)
            if not kis_symbol or prefix is None:
                continue
            candidate = f"{prefix}{kis_symbol}"
            matches.add(
                normalize_symbol_keys({normalized_symbol: candidate})[
                    normalized_symbol
                ]
            )

    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        raise KisWsSymbolKeysError(
            "KIS US symbol master has conflicting exchange mappings for "
            f"{normalized_symbol}"
        )
    if matched_rows:
        raise KisWsSymbolKeysError(
            "KIS US symbol master has no supported US exchange mapping for "
            f"{normalized_symbol}"
        )
    raise KisWsSymbolKeysError(
        f"{normalized_symbol} is not present in the KIS US symbol master: {target}"
    )


def read_symbol_keys_file(path: Path = DEFAULT_KIS_WS_SYMBOL_KEYS_FILE) -> Dict[str, str]:
    """Strictly read the local mapping; never silently substitute a backup."""

    target = Path(path)
    try:
        size = target.stat().st_size
        if size > MAX_SYMBOL_KEYS_FILE_BYTES:
            raise KisWsSymbolKeysError(
                f"KIS WebSocket symbol-key file exceeds {MAX_SYMBOL_KEYS_FILE_BYTES} bytes"
            )
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise KisWsSymbolKeysError(
            f"KIS WebSocket symbol-key file is not valid JSON: {target}"
        ) from exc
    except OSError as exc:
        raise KisWsSymbolKeysError(
            f"KIS WebSocket symbol-key file cannot be read: {target}"
        ) from exc
    return normalize_symbol_keys(raw)


def _mapping_digest(mapping: Mapping[str, str]) -> str:
    payload = json.dumps(
        dict(sorted(mapping.items())),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class KisWsSymbolKeyStore:
    """Thread-safe file watcher with a last-known-good in-memory snapshot."""

    def __init__(
        self,
        path: Path = DEFAULT_KIS_WS_SYMBOL_KEYS_FILE,
        *,
        universe_path: Path = DEFAULT_KIS_US_TICKERS_FILE,
        auto_provision: bool = False,
    ) -> None:
        import threading

        self.path = Path(path)
        self.universe_path = Path(universe_path)
        self._auto_provision = bool(auto_provision)
        self._lock = threading.RLock()
        self._keys: Dict[str, str] = {}
        self._source = "UNINITIALIZED"
        self._generation = 0
        self._sha256 = _mapping_digest({})
        self._initialized = False
        self._loaded_signature: Optional[tuple[int, int, int]] = None
        self._failed_signature: Optional[tuple[int, int, int]] = None
        self._last_error = ""
        self._missing_after_file_load = False

    def _signature(self) -> Optional[tuple[int, int, int]]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise KisWsSymbolKeysError(
                f"KIS WebSocket symbol-key file cannot be inspected: {self.path}"
            ) from exc
        return int(stat.st_mtime_ns), int(stat.st_ctime_ns), int(stat.st_size)

    def _install(self, keys: Mapping[str, str], *, source: str) -> None:
        normalized = normalize_symbol_keys(dict(keys))
        digest = _mapping_digest(normalized)
        changed = (
            normalized != self._keys
            or source != self._source
            or not self._initialized
        )
        self._keys = normalized
        self._source = source
        self._sha256 = digest
        self._initialized = True
        self._last_error = ""
        self._failed_signature = None
        self._missing_after_file_load = False
        if changed:
            self._generation += 1

    def _note_error(
        self,
        message: str,
        signature: Optional[tuple[int, int, int]],
    ) -> None:
        if message != self._last_error:
            logger.warning("%s", message)
        self._last_error = message
        self._failed_signature = signature
        self._initialized = True

    def refresh_if_changed(self) -> KisWsSymbolKeysSnapshot:
        """Reload a complete changed file, retaining prior keys on failure."""

        with self._lock:
            try:
                signature = self._signature()
            except KisWsSymbolKeysError as exc:
                self._note_error(str(exc), None)
                return self._snapshot_locked()

            if signature is None:
                if not self._initialized:
                    self._install({}, source="EMPTY")
                elif self._source == "FILE" and not self._missing_after_file_load:
                    self._missing_after_file_load = True
                    self._note_error(
                        "KIS WebSocket symbol-key file is missing; retaining the "
                        f"last-known-good map from {self.path}",
                        None,
                    )
                return self._snapshot_locked()

            if (
                self._source == "FILE"
                and signature == self._loaded_signature
                and not self._last_error
            ):
                return self._snapshot_locked()
            if signature == self._failed_signature:
                return self._snapshot_locked()

            try:
                keys = read_symbol_keys_file(self.path)
            except (KisWsSymbolKeysError, FileNotFoundError) as exc:
                self._note_error(
                    f"{exc}; retaining the last-known-good KIS WebSocket symbol map",
                    signature,
                )
                return self._snapshot_locked()

            previous_digest = self._sha256
            self._install(keys, source="FILE")
            self._loaded_signature = signature
            if self._sha256 != previous_digest:
                logger.info(
                    "Reloaded %d KIS WebSocket symbol key(s) from %s (generation %d)",
                    len(self._keys),
                    self.path,
                    self._generation,
                )
            return self._snapshot_locked()

    def _snapshot_locked(self) -> KisWsSymbolKeysSnapshot:
        return KisWsSymbolKeysSnapshot(
            keys=MappingProxyType(dict(self._keys)),
            source=self._source,
            generation=self._generation,
            sha256=self._sha256,
            path=self.path,
            last_error=self._last_error,
        )

    def snapshot(self) -> KisWsSymbolKeysSnapshot:
        return self.refresh_if_changed()

    def resolve(self, symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        with self._lock:
            snapshot = self.refresh_if_changed()
            key = str(snapshot.keys.get(normalized, "") or "").strip()
            provision_error = ""
            if not key and self._auto_provision and not snapshot.last_error:
                try:
                    key = derive_symbol_key_from_kis_master(
                        normalized,
                        self.universe_path,
                    )
                    # A missing file may still be serving a legacy environment
                    # snapshot. Seed every known value so provisioning one
                    # symbol never discards that last-known-good map.
                    additions = dict(snapshot.keys) if not self.path.exists() else {}
                    additions[normalized] = key
                    update_symbol_keys_file(
                        set_values=additions,
                        path=self.path,
                        refuse_conflicts=True,
                    )
                    refreshed = self.refresh_if_changed()
                    key = str(refreshed.keys.get(normalized, "") or "").strip()
                    if not key:
                        raise KisWsSymbolKeysError(
                            "the atomically updated symbol-key file did not reload"
                        )
                    logger.info(
                        "Provisioned KIS WebSocket symbol key for %s from %s",
                        normalized,
                        self.universe_path,
                    )
                except (KisWsSymbolKeysError, OSError, TimeoutError) as exc:
                    key = ""
                    provision_error = f"; KIS-master provisioning failed: {exc}"
            if not key:
                detail = f" ({snapshot.last_error})" if snapshot.last_error else ""
                raise RuntimeError(
                    "No live-verified or KIS-master-derived WebSocket subscription "
                    "key configured for "
                    f"{normalized} in {snapshot.path}{detail}{provision_error}"
                )
            return key


@contextmanager
def _exclusive_update_lock(path: Path) -> Iterator[None]:
    lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _WRITE_LOCK_TIMEOUT_SECONDS
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > _STALE_WRITE_LOCK_SECONDS
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
                    f"Timed out waiting for KIS WebSocket symbol-key update lock: {lock_path}"
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except OSError:
            pass


def write_symbol_keys_file(
    keys: Mapping[str, str],
    path: Path = DEFAULT_KIS_WS_SYMBOL_KEYS_FILE,
) -> Dict[str, str]:
    """Validate and atomically replace the local mapping with a backup."""

    target = Path(path)
    normalized = normalize_symbol_keys(dict(keys))
    with _exclusive_update_lock(target):
        save_json(target, normalized)
    return normalized


def update_symbol_keys_file(
    *,
    set_values: Optional[Mapping[str, str]] = None,
    remove_symbols: tuple[str, ...] = (),
    path: Path = DEFAULT_KIS_WS_SYMBOL_KEYS_FILE,
    refuse_conflicts: bool = False,
) -> Dict[str, str]:
    """Atomically apply a small edit without losing concurrent CLI updates."""

    target = Path(path)
    with _exclusive_update_lock(target):
        current = read_symbol_keys_file(target) if target.exists() else {}
        updated = dict(current)
        for symbol, key in (set_values or {}).items():
            normalized_pair = normalize_symbol_keys({symbol: key})
            normalized_symbol, normalized_key = next(iter(normalized_pair.items()))
            if (
                refuse_conflicts
                and normalized_symbol in updated
                and updated[normalized_symbol] != normalized_key
            ):
                raise KisWsSymbolKeysError(
                    f"refusing to replace the existing verified key for {normalized_symbol}"
                )
            updated.update(normalized_pair)
        for symbol in remove_symbols:
            normalized_symbol = str(symbol or "").strip().upper()
            if not _SYMBOL_PATTERN.fullmatch(normalized_symbol):
                raise KisWsSymbolKeysError(f"invalid KIS WebSocket symbol: {symbol!r}")
            updated.pop(normalized_symbol, None)
        normalized = normalize_symbol_keys(updated)
        save_json(target, normalized)
    return normalized
