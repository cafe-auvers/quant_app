"""Tamper-evident append-only runtime evidence for activation gates.

The report validators intentionally accept ordinary mappings so they remain
easy to review and archive.  Qualification runners must not manufacture those
mappings from unchecked booleans, though.  This module is the shared durable
source for Gates 3 and 4: every observation is bound to one gate, one exact
commit and the preceding row's digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from activation_gates.evidence import valid_git_commit_sha


ZERO_SHA256 = "0" * 64


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _event_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("event_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


@dataclass(frozen=True)
class EvidenceJournalEvent:
    schema_version: int
    event_id: str
    gate: str
    commit_sha: str
    event_type: str
    observed_at: str
    previous_event_sha256: str
    payload: dict[str, Any]
    event_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceJournalAudit:
    row_count: int
    event_counts: dict[str, int]
    parse_error_count: int
    duplicate_event_id_count: int
    hash_chain_error_count: int
    identity_mismatch_count: int
    unknown_event_type_count: int
    sha256: str

    @property
    def passed(self) -> bool:
        return not (
            self.parse_error_count
            or self.duplicate_event_id_count
            or self.hash_chain_error_count
            or self.identity_mismatch_count
            or self.unknown_event_type_count
        )


class AppendOnlyEvidenceJournal:
    """An fsync'd JSONL hash chain for one gate and exact release.

    Existing data is fully audited before the first append and whenever the
    file changes outside this instance. Subsequent in-process appends extend a
    cached, validated tail in constant time. Final evidence generation audits
    the complete file again, so corruption remains fail-closed without making
    a live collector rescan an ever-growing journal for every quote.
    """

    def __init__(
        self,
        path: Path,
        *,
        gate: str,
        commit_sha: str,
        allowed_event_types: Iterable[str],
    ) -> None:
        resolved = Path(path).expanduser().resolve()
        if resolved.suffixes[-2:] != [".evidence", ".jsonl"]:
            raise ValueError("Evidence journal path must end with .evidence.jsonl")
        normalized_gate = str(gate or "").strip().upper()
        normalized_commit = str(commit_sha or "").strip().lower()
        allowed = frozenset(str(item or "").strip().upper() for item in allowed_event_types)
        if not normalized_gate or not valid_git_commit_sha(normalized_commit):
            raise ValueError("Evidence journal requires a gate and complete Git SHA")
        if not allowed or "" in allowed:
            raise ValueError("Evidence journal requires explicit event types")
        self.path = resolved
        self.gate = normalized_gate
        self.commit_sha = normalized_commit
        self.allowed_event_types = allowed
        self._lock = threading.RLock()
        self._append_cache_initialized = False
        self._append_file_identity: tuple[int, int, int] | None = None
        self._append_tail_sha256 = ZERO_SHA256

    def _file_identity(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _validated_append_tail(self) -> str:
        identity = self._file_identity()
        if self._append_cache_initialized and identity == self._append_file_identity:
            return self._append_tail_sha256

        audit = self.audit()
        if not audit.passed:
            raise RuntimeError("Refusing to extend a corrupt evidence journal")
        rows = self.read_all()
        self._append_tail_sha256 = (
            rows[-1].event_sha256 if rows else ZERO_SHA256
        )
        self._append_file_identity = identity
        self._append_cache_initialized = True
        return self._append_tail_sha256

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        observed_at: datetime | None = None,
        event_id: str | None = None,
    ) -> EvidenceJournalEvent:
        kind = str(event_type or "").strip().upper()
        if kind not in self.allowed_event_types:
            raise ValueError(f"Unsupported {self.gate} evidence event: {kind!r}")
        if not isinstance(payload, Mapping):
            raise TypeError("Evidence event payload must be a mapping")
        timestamp = observed_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Evidence event timestamps must be timezone-aware")

        with self._lock:
            previous = self._validated_append_tail()
            unsigned = {
                "schema_version": 1,
                "event_id": str(event_id or uuid4().hex),
                "gate": self.gate,
                "commit_sha": self.commit_sha,
                "event_type": kind,
                "observed_at": timestamp.astimezone(timezone.utc).isoformat(),
                "previous_event_sha256": previous,
                "payload": dict(payload),
            }
            event = EvidenceJournalEvent(
                **unsigned,
                event_sha256=_event_digest(unsigned),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = _canonical_json(event.to_dict()).decode("utf-8")
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._append_tail_sha256 = event.event_sha256
            self._append_file_identity = self._file_identity()
            return event

    def read_all(self) -> list[EvidenceJournalEvent]:
        if not self.path.exists():
            return []
        events: list[EvidenceJournalEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(EvidenceJournalEvent(**json.loads(line)))
        return events

    def sha256(self) -> str:
        content = self.path.read_bytes() if self.path.exists() else b""
        return hashlib.sha256(content).hexdigest()

    def audit(self) -> EvidenceJournalAudit:
        counts = {kind: 0 for kind in sorted(self.allowed_event_types)}
        parse_errors = 0
        duplicates = 0
        chain_errors = 0
        identity_errors = 0
        unknown_types = 0
        row_count = 0
        previous = ZERO_SHA256
        seen_ids: set[str] = set()
        lines = (
            self.path.read_text(encoding="utf-8").splitlines()
            if self.path.exists()
            else []
        )
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("journal row is not an object")
                event = EvidenceJournalEvent(**raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                parse_errors += 1
                continue
            row_count += 1
            if event.event_id in seen_ids:
                duplicates += 1
            seen_ids.add(event.event_id)
            if event.previous_event_sha256 != previous:
                chain_errors += 1
            if event.event_sha256 != _event_digest(raw):
                chain_errors += 1
            previous = event.event_sha256
            if event.gate != self.gate or event.commit_sha != self.commit_sha:
                identity_errors += 1
            if event.event_type not in self.allowed_event_types:
                unknown_types += 1
            else:
                counts[event.event_type] += 1
        return EvidenceJournalAudit(
            row_count=row_count,
            event_counts=counts,
            parse_error_count=parse_errors,
            duplicate_event_id_count=duplicates,
            hash_chain_error_count=chain_errors,
            identity_mismatch_count=identity_errors,
            unknown_event_type_count=unknown_types,
            sha256=self.sha256(),
        )


__all__ = [
    "AppendOnlyEvidenceJournal",
    "EvidenceJournalAudit",
    "EvidenceJournalEvent",
    "ZERO_SHA256",
]
