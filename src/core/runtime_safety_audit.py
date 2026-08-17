"""Process-scoped evidence audit for read-only qualification runs.

The Gate-2 process deliberately does not compose the execution engine, but it
still needs positive evidence that a destructive broker boundary was watched
and that the real entry-readiness gate rejected the controlled stale probe.
This module provides a small, thread-safe observer registry for those exact
production boundaries.  A zero count is certifiable only inside an initialized
session whose required boundary modules registered themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Iterable
from uuid import uuid4


BROKER_MUTATION_AUDIT_SOURCE = "KIS_BROKER_MUTATION_BOUNDARY"
ENTRY_READINESS_AUDIT_SOURCE = "KIS_ENTRY_READINESS_BOUNDARY"
GATE2_REQUIRED_AUDIT_SOURCES = frozenset(
    {BROKER_MUTATION_AUDIT_SOURCE, ENTRY_READINESS_AUDIT_SOURCE}
)


@dataclass(frozen=True)
class RuntimeSafetyAuditSnapshot:
    initialized: bool
    registered_sources: tuple[str, ...]
    broker_mutation_attempt_count: int
    entry_readiness_check_count: int
    stale_entry_readiness_check_count: int
    stale_entry_readiness_rejection_count: int
    stale_entry_readiness_allow_count: int


_registry_lock = threading.RLock()
_registered_sources: set[str] = set()
_active_sessions: dict[str, "RuntimeSafetyAuditSession"] = {}


def register_runtime_safety_audit_source(source: str) -> None:
    """Declare one instrumented production boundary at module import."""
    value = str(source or "").strip().upper()
    if not value:
        raise ValueError("runtime safety audit source cannot be blank")
    with _registry_lock:
        _registered_sources.add(value)


class RuntimeSafetyAuditSession:
    """One qualification run's counters, isolated from other sessions."""

    def __init__(self, *, token: str, required_sources: Iterable[str]) -> None:
        required = {str(item or "").strip().upper() for item in required_sources}
        with _registry_lock:
            registered = frozenset(_registered_sources)
        self._token = token
        self._registered_sources = registered
        self._initialized = bool(required) and required <= registered
        self._lock = threading.RLock()
        self._closed = False
        self._stale_probe_symbols: set[str] = set()
        self._broker_mutation_attempt_count = 0
        self._entry_readiness_check_count = 0
        self._stale_entry_readiness_check_count = 0
        self._stale_entry_readiness_rejection_count = 0
        self._stale_entry_readiness_allow_count = 0

    @property
    def initialized(self) -> bool:
        return self._initialized

    def __enter__(self) -> "RuntimeSafetyAuditSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def begin_stale_entry_probe(self, symbol: str) -> None:
        value = str(symbol or "").strip().upper()
        if not value:
            raise ValueError("stale-entry probe symbol cannot be blank")
        with self._lock:
            if self._closed:
                raise RuntimeError("runtime safety audit session is closed")
            self._stale_probe_symbols.add(value)

    def end_stale_entry_probe(self, symbol: str) -> None:
        with self._lock:
            self._stale_probe_symbols.discard(str(symbol or "").strip().upper())

    def _record_broker_mutation_attempt(self) -> None:
        with self._lock:
            if not self._closed:
                self._broker_mutation_attempt_count += 1

    def _record_entry_readiness(self, *, symbol: str, ready: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._entry_readiness_check_count += 1
            if str(symbol or "").strip().upper() not in self._stale_probe_symbols:
                return
            self._stale_entry_readiness_check_count += 1
            if ready:
                self._stale_entry_readiness_allow_count += 1
            else:
                self._stale_entry_readiness_rejection_count += 1

    def snapshot(self) -> RuntimeSafetyAuditSnapshot:
        with self._lock:
            return RuntimeSafetyAuditSnapshot(
                initialized=self._initialized,
                registered_sources=tuple(sorted(self._registered_sources)),
                broker_mutation_attempt_count=self._broker_mutation_attempt_count,
                entry_readiness_check_count=self._entry_readiness_check_count,
                stale_entry_readiness_check_count=(
                    self._stale_entry_readiness_check_count
                ),
                stale_entry_readiness_rejection_count=(
                    self._stale_entry_readiness_rejection_count
                ),
                stale_entry_readiness_allow_count=(
                    self._stale_entry_readiness_allow_count
                ),
            )

    def close(self) -> RuntimeSafetyAuditSnapshot:
        with _registry_lock:
            _active_sessions.pop(self._token, None)
        with self._lock:
            self._closed = True
        return self.snapshot()


def begin_runtime_safety_audit(
    *, required_sources: Iterable[str] = GATE2_REQUIRED_AUDIT_SOURCES
) -> RuntimeSafetyAuditSession:
    token = uuid4().hex
    session = RuntimeSafetyAuditSession(
        token=token, required_sources=required_sources
    )
    with _registry_lock:
        _active_sessions[token] = session
    return session


def _sessions() -> tuple[RuntimeSafetyAuditSession, ...]:
    with _registry_lock:
        return tuple(_active_sessions.values())


def record_broker_mutation_attempt() -> None:
    for session in _sessions():
        session._record_broker_mutation_attempt()


def record_entry_readiness(*, symbol: str, ready: bool) -> None:
    for session in _sessions():
        session._record_entry_readiness(symbol=symbol, ready=bool(ready))
