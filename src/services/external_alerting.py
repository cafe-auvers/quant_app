"""Durable delivery for safety-critical alerts and watchdog heartbeats.

This process only publishes heartbeats. Detecting a missing heartbeat must
run outside this application so a crashed or disconnected process cannot be
responsible for reporting its own absence.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol
from uuid import uuid4
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.engine import Engine

from src.utils.config import DATA_DIR


EXTERNAL_ALERT_SPOOL_FILE = DATA_DIR / "external_alert_spool.jsonl"


class WebhookAlertDeliveryProvider:
    """Production HTTPS delivery adapter for alerts and watchdog heartbeats."""

    def __init__(
        self,
        *,
        alert_url: str,
        heartbeat_url: str,
        bearer_token: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        for name, value in (("alert_url", alert_url), ("heartbeat_url", heartbeat_url)):
            if not str(value or "").lower().startswith("https://"):
                raise ValueError(f"{name} must be an HTTPS URL")
        self.alert_url = str(alert_url)
        self.heartbeat_url = str(heartbeat_url)
        self.bearer_token = str(bearer_token or "")
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    def _post(self, url: str, payload: Dict[str, Any]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            if int(response.status) < 200 or int(response.status) >= 300:
                raise RuntimeError(f"alert webhook returned HTTP {response.status}")
            return str(response.headers.get("X-Delivery-Id") or uuid4().hex)

    def deliver(self, payload: Dict[str, Any]) -> str:
        return self._post(self.alert_url, payload)

    def publish_heartbeat(self, payload: Dict[str, Any]) -> str:
        return self._post(self.heartbeat_url, payload)


class LocalAlertSpool:
    """Append-only, fsynced delivery evidence independent of canonical DB."""

    def __init__(self, path: Path = EXTERNAL_ALERT_SPOOL_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _append_locked(
        self, event_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        record = {
            "event_id": uuid4().hex,
            "event_type": str(event_type).upper(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("Short external-alert spool append")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return record

    def append(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            return self._append_locked(event_type, payload)

    def _read_entries_locked(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _fold_pending_alerts(
        entries: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        reconciled = {
            str(entry.get("pending_event_id") or "")
            for entry in entries
            if entry.get("event_type") == "ALERT_RECONCILED"
        }
        attempts_by_pending: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            if entry.get("event_type") != "ALERT_DELIVERY_ATTEMPT":
                continue
            pending_event_id = str(entry.get("pending_event_id") or "")
            attempts_by_pending.setdefault(pending_event_id, []).append(entry)
        occurrences_by_pending: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            if entry.get("event_type") != "ALERT_OCCURRENCE":
                continue
            pending_event_id = str(entry.get("pending_event_id") or "")
            occurrences_by_pending.setdefault(pending_event_id, []).append(entry)
        pending = []
        for entry in entries:
            if (
                entry.get("event_type") != "ALERT_PENDING"
                or entry.get("event_id") in reconciled
            ):
                continue
            item = dict(entry)
            occurrences = occurrences_by_pending.get(
                str(entry.get("event_id") or ""), []
            )
            item["occurrence_count"] = max(
                1, int(item.get("occurrence_count") or 1)
            ) + len(occurrences)
            if occurrences:
                latest_occurrence = occurrences[-1]
                item["message"] = str(
                    latest_occurrence.get("message") or item.get("message") or ""
                )
            attempts = attempts_by_pending.get(str(entry.get("event_id") or ""), [])
            if attempts:
                item["delivery_attempts"] = [dict(attempt) for attempt in attempts]
                item["delivery_attempt"] = dict(attempts[-1])
            pending.append(item)
        return pending

    def pending_alerts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._fold_pending_alerts(self._read_entries_locked())

    def record_alert_occurrence(
        self,
        *,
        alert_type: str,
        dedupe_key: str,
        message: str,
        database_error: str,
    ) -> tuple[Dict[str, Any], bool]:
        """Append one occurrence and return its correlated pending incident.

        The read/correlate/append sequence is protected by the same lock as
        every append, so concurrent callers in this process cannot create two
        pending incidents for one canonical deduplication key.
        """

        normalized_type = str(alert_type or "").upper()
        normalized_key = str(dedupe_key or "").strip()
        with self._lock:
            entries = self._read_entries_locked()
            pending_alerts = self._fold_pending_alerts(entries)
            existing = next(
                (
                    pending
                    for pending in pending_alerts
                    if pending.get("alert_type") == normalized_type
                    and pending.get("dedupe_key") == normalized_key
                ),
                None,
            )
            if existing is not None:
                self._append_locked(
                    "ALERT_OCCURRENCE",
                    {
                        "pending_event_id": existing["event_id"],
                        "message": str(message),
                        "database_error": str(database_error),
                    },
                )
                correlated = dict(existing)
                correlated["occurrence_count"] = (
                    int(existing.get("occurrence_count") or 1) + 1
                )
                correlated["message"] = str(message)
                return correlated, False

            created = self._append_locked(
                "ALERT_PENDING",
                {
                    "alert_type": normalized_type,
                    "dedupe_key": normalized_key,
                    "message": str(message),
                    "database_error": str(database_error),
                    "occurrence_count": 1,
                },
            )
            return created, True


class CriticalAlertType(str, Enum):
    MARKET_DATA_OUTAGE = "MARKET_DATA_OUTAGE"
    STALE_CRITICAL_SYMBOL = "STALE_CRITICAL_SYMBOL"
    EXECUTION_LEASE_LOST = "EXECUTION_LEASE_LOST"
    ACCOUNT_RECONCILIATION_FAILED = "ACCOUNT_RECONCILIATION_FAILED"
    UNKNOWN_SUBMISSION_STATE = "UNKNOWN_SUBMISSION_STATE"
    DISCOVERED_EXTERNAL_ORDER = "DISCOVERED_EXTERNAL_ORDER"
    CANCEL_CONFIRMATION_TIMEOUT = "CANCEL_CONFIRMATION_TIMEOUT"
    EMERGENCY_LIQUIDATION_ATTEMPTED = "EMERGENCY_LIQUIDATION_ATTEMPTED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    APPLICATION_HEARTBEAT_MISSING = "APPLICATION_HEARTBEAT_MISSING"


class AlertIncidentStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AlertDeliveryProvider(Protocol):
    def deliver(self, payload: Dict[str, Any]) -> str: ...

    def publish_heartbeat(self, payload: Dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class AlertIncident:
    incident_id: str
    alert_type: CriticalAlertType
    dedupe_key: str
    message: str
    status: AlertIncidentStatus
    occurrence_count: int
    delivery_attempt_count: int
    escalation_level: int
    next_attempt_at: datetime
    version: int


def _incident_table(metadata: MetaData) -> Table:
    return Table(
        "external_alert_incidents",
        metadata,
        Column("incident_id", String(64), primary_key=True),
        Column("alert_type", String(64), nullable=False),
        Column("dedupe_key", String(255), nullable=False),
        Column("message", Text, nullable=False),
        Column("status", String(32), nullable=False),
        Column("occurrence_count", Integer, nullable=False),
        Column("delivery_attempt_count", Integer, nullable=False),
        Column("escalation_level", Integer, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        Column("next_attempt_at", DateTime, nullable=False),
        Column("acknowledged_at", DateTime, nullable=True),
        Column("acknowledged_by", String(255), nullable=False, default=""),
        Column("version", Integer, nullable=False),
        UniqueConstraint("alert_type", "dedupe_key", name="uq_external_alert_dedupe"),
    )


def _attempt_table(metadata: MetaData) -> Table:
    return Table(
        "external_alert_delivery_attempts",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("incident_id", String(64), nullable=False),
        Column("attempt_number", Integer, nullable=False),
        Column("escalation_level", Integer, nullable=False),
        Column("attempted_at", DateTime, nullable=False),
        Column("status", String(32), nullable=False),
        Column("provider_delivery_id", String(255), nullable=False, default=""),
        Column("error", Text, nullable=False, default=""),
        UniqueConstraint(
            "incident_id", "attempt_number", name="uq_external_alert_attempt"
        ),
    )


def _spool_import_table(metadata: MetaData) -> Table:
    return Table(
        "external_alert_spool_imports",
        metadata,
        Column("pending_event_id", String(64), primary_key=True),
        Column("incident_id", String(64), nullable=False),
        Column("imported_at", DateTime, nullable=False),
    )


def _heartbeat_table(metadata: MetaData) -> Table:
    return Table(
        "application_heartbeat_attempts",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("device_id", String(255), nullable=False),
        Column("attempted_at", DateTime, nullable=False),
        Column("status", String(32), nullable=False),
        Column("provider_delivery_id", String(255), nullable=False, default=""),
        Column("error", Text, nullable=False, default=""),
    )


def ensure_external_alert_tables(engine: Engine) -> None:
    metadata = MetaData()
    _incident_table(metadata)
    _attempt_table(metadata)
    _spool_import_table(metadata)
    _heartbeat_table(metadata)
    metadata.create_all(engine)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _db_datetime(value: datetime) -> datetime:
    return _as_utc(value).replace(tzinfo=None)


def _row_to_incident(row) -> AlertIncident:
    return AlertIncident(
        incident_id=row.incident_id,
        alert_type=CriticalAlertType(row.alert_type),
        dedupe_key=row.dedupe_key,
        message=row.message,
        status=AlertIncidentStatus(row.status),
        occurrence_count=int(row.occurrence_count),
        delivery_attempt_count=int(row.delivery_attempt_count),
        escalation_level=int(row.escalation_level),
        next_attempt_at=_as_utc(row.next_attempt_at),
        version=int(row.version),
    )


class ExternalAlertingService:
    """Incident-scoped dedupe with durable delivery attempts and escalation."""

    watchdog_is_external = True

    def __init__(
        self,
        engine: Engine,
        provider: AlertDeliveryProvider,
        *,
        device_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        retry_base_seconds: float = 30.0,
        acknowledgement_timeout_seconds: float = 300.0,
        escalation_every_attempts: int = 2,
        max_escalation_level: int = 3,
        heartbeat_interval_seconds: float = 30.0,
        local_spool: Optional[LocalAlertSpool] = None,
        spool_import_fault_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.engine = engine
        self.provider = provider
        self.device_id = str(device_id or "unknown-device")
        self._clock = clock
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.acknowledgement_timeout_seconds = max(
            0.0, float(acknowledgement_timeout_seconds)
        )
        self.escalation_every_attempts = max(1, int(escalation_every_attempts))
        self.max_escalation_level = max(0, int(max_escalation_level))
        self.heartbeat_interval_seconds = max(
            0.001, float(heartbeat_interval_seconds)
        )
        self.local_spool = local_spool or LocalAlertSpool()
        self._spool_import_fault_hook = spool_import_fault_hook or (
            lambda _point: None
        )
        self._last_heartbeat_attempt_at: Optional[datetime] = None
        ensure_external_alert_tables(engine)

    @staticmethod
    def _normalize_type(alert_type: CriticalAlertType | str) -> CriticalAlertType:
        return (
            alert_type
            if isinstance(alert_type, CriticalAlertType)
            else CriticalAlertType(str(alert_type or "").upper())
        )

    def raise_alert(
        self,
        alert_type: CriticalAlertType | str,
        dedupe_key: str,
        message: str,
    ) -> AlertIncident:
        return self._raise_alert_occurrences(
            alert_type,
            dedupe_key,
            message,
            occurrence_count=1,
        )

    def _raise_alert_occurrences(
        self,
        alert_type: CriticalAlertType | str,
        dedupe_key: str,
        message: str,
        *,
        occurrence_count: int,
    ) -> AlertIncident:
        resolved_type = self._normalize_type(alert_type)
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("Critical alert requires a dedupe_key")
        occurrences = int(occurrence_count)
        if occurrences <= 0:
            raise ValueError("Critical alert occurrence_count must be positive")
        now = _db_datetime(self._clock())
        table = _incident_table(MetaData())
        with self.engine.begin() as conn:
            row = conn.execute(
                select(table).where(
                    table.c.alert_type == resolved_type.value,
                    table.c.dedupe_key == key,
                )
            ).first()
            if row is None:
                incident_id = uuid4().hex
                conn.execute(
                    table.insert().values(
                        incident_id=incident_id,
                        alert_type=resolved_type.value,
                        dedupe_key=key,
                        message=str(message),
                        status=AlertIncidentStatus.OPEN.value,
                        occurrence_count=occurrences,
                        delivery_attempt_count=0,
                        escalation_level=0,
                        created_at=now,
                        updated_at=now,
                        next_attempt_at=now,
                        acknowledged_at=None,
                        acknowledged_by="",
                        version=1,
                    )
                )
            else:
                incident_id = row.incident_id
                values = {
                    "message": str(message),
                    "occurrence_count": int(row.occurrence_count) + occurrences,
                    "updated_at": now,
                    "version": int(row.version) + 1,
                }
                if row.status != AlertIncidentStatus.OPEN.value:
                    # Delivery attempt numbers are lifetime-monotonic for an
                    # incident because historical rows remain durable under
                    # UNIQUE (incident_id, attempt_number).
                    values.update(
                        status=AlertIncidentStatus.OPEN.value,
                        next_attempt_at=now,
                        acknowledged_at=None,
                        acknowledged_by="",
                    )
                conn.execute(
                    table.update()
                    .where(
                        table.c.incident_id == incident_id,
                        table.c.version == row.version,
                    )
                    .values(**values)
                )
            current = conn.execute(
                select(table).where(table.c.incident_id == incident_id)
            ).one()
        return _row_to_incident(current)

    def sink(self, alert_class: str, dedupe_key: str, message: str) -> None:
        resolved_type = self._normalize_type(alert_class).value
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("Critical alert requires a dedupe_key")
        try:
            self.raise_alert(resolved_type, key, message)
            return
        except Exception as exc:
            database_error = str(exc)
        self._deliver_offline_incident(
            resolved_type,
            key,
            str(message),
            database_error=database_error,
        )

    def sink_offline(
        self,
        alert_class: str,
        dedupe_key: str,
        message: str,
        *,
        database_error: str = "Canonical database is unavailable",
        deliver_directly: bool = True,
    ) -> None:
        """Spool/deliver an incident when the caller already proved DB loss.

        Retrying the same unavailable database before writing the local spool
        adds another connection timeout at the worst possible moment. This
        seam preserves the same durable and external-delivery behavior while
        skipping that known-futile retry.
        """

        resolved_type = self._normalize_type(alert_class).value
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("Critical alert requires a dedupe_key")
        self._deliver_offline_incident(
            resolved_type,
            key,
            str(message),
            database_error=str(database_error),
            deliver_directly=bool(deliver_directly),
        )

    def _deliver_offline_incident(
        self,
        resolved_type: str,
        key: str,
        message: str,
        *,
        database_error: str,
        deliver_directly: bool = True,
    ) -> None:
        pending = None
        is_new_pending = False
        spool_error: Optional[Exception] = None
        try:
            pending, is_new_pending = self.local_spool.record_alert_occurrence(
                alert_type=resolved_type,
                dedupe_key=key,
                message=message,
                database_error=database_error,
            )
        except Exception as exc:
            # Local durability and external delivery are independent failure
            # domains.  A full disk must never suppress the network alert.
            spool_error = exc
        if pending is not None and not is_new_pending:
            return
        if not deliver_directly:
            if pending is None:
                raise RuntimeError(
                    "Critical alert could not be written to the offline spool: "
                    f"{spool_error}"
                )
            return
        offline_event_id = (
            str(pending["event_id"]) if pending is not None else uuid4().hex
        )
        payload = {
            "incident_id": f"offline-{offline_event_id}",
            "alert_type": resolved_type,
            "dedupe_key": key,
            "message": message,
            "device_id": self.device_id,
            "requires_acknowledgement": True,
            "offline_spool": pending is not None,
            "attempt_number": 1,
            "escalation_level": 0,
        }
        attempted_at = _as_utc(self._clock())
        try:
            delivery_id = str(self.provider.deliver(payload) or "")
            status, error = "DELIVERED", ""
        except Exception as exc:
            delivery_id, status, error = "", "FAILED", str(exc)
        if pending is not None:
            try:
                self.local_spool.append(
                    "ALERT_DELIVERY_ATTEMPT",
                    {
                        "pending_event_id": pending["event_id"],
                        "status": status,
                        "provider_delivery_id": delivery_id,
                        "error": error,
                        "attempt_number": 1,
                        "escalation_level": 0,
                        "attempted_at": attempted_at.isoformat(),
                        "next_attempt_at": self._offline_next_attempt_at(
                            attempted_at, attempt_number=1, status=status
                        ).isoformat(),
                    },
                )
            except Exception:
                # Delivery was still attempted.  The caller's critical-log
                # fallback is useful only when neither independent channel
                # retained or delivered the incident.
                if status != "DELIVERED":
                    raise
        elif status != "DELIVERED":
            raise RuntimeError(
                "Critical alert could be neither spooled nor delivered: "
                f"spool={spool_error}; provider={error}"
            )

    def _offline_next_attempt_at(
        self, now: datetime, *, attempt_number: int, status: str
    ) -> datetime:
        delay = (
            self.acknowledgement_timeout_seconds
            if str(status).upper() == "DELIVERED"
            else self.retry_base_seconds * (2 ** min(attempt_number - 1, 6))
        )
        return _as_utc(now) + timedelta(seconds=delay)

    @staticmethod
    def _spool_datetime(value: Any, *, fallback: datetime) -> datetime:
        if not value:
            return _as_utc(fallback)
        try:
            return _as_utc(datetime.fromisoformat(str(value)))
        except (TypeError, ValueError):
            return _as_utc(fallback)

    def _import_spooled_incident(self, pending: Dict[str, Any]) -> None:
        """Atomically import one local incident and its durable receipt."""

        pending_event_id = str(pending.get("event_id") or "").strip()
        if not pending_event_id:
            raise ValueError("Spool import requires a pending event ID")
        resolved_type = self._normalize_type(pending.get("alert_type") or "")
        dedupe_key = str(pending.get("dedupe_key") or "").strip()
        if not dedupe_key:
            raise ValueError("Spool import requires a dedupe key")
        occurrence_count = max(1, int(pending.get("occurrence_count") or 1))
        attempts = list(pending.get("delivery_attempts") or [])
        now = _as_utc(self._clock())
        incident_table = _incident_table(MetaData())
        attempt_table = _attempt_table(MetaData())
        import_table = _spool_import_table(MetaData())
        with self.engine.begin() as conn:
            receipt = conn.execute(
                select(import_table.c.incident_id).where(
                    import_table.c.pending_event_id == pending_event_id
                )
            ).first()
            if receipt is not None:
                return

            row = conn.execute(
                select(incident_table).where(
                    incident_table.c.alert_type == resolved_type.value,
                    incident_table.c.dedupe_key == dedupe_key,
                )
            ).first()
            if row is None:
                incident_id = uuid4().hex
                starting_attempt_count = 0
                incident_version = 1
                conn.execute(
                    incident_table.insert().values(
                        incident_id=incident_id,
                        alert_type=resolved_type.value,
                        dedupe_key=dedupe_key,
                        message=str(pending.get("message") or ""),
                        status=AlertIncidentStatus.OPEN.value,
                        occurrence_count=occurrence_count,
                        delivery_attempt_count=0,
                        escalation_level=0,
                        created_at=_db_datetime(now),
                        updated_at=_db_datetime(now),
                        next_attempt_at=_db_datetime(now),
                        acknowledged_at=None,
                        acknowledged_by="",
                        version=incident_version,
                    )
                )
            else:
                incident_id = str(row.incident_id)
                starting_attempt_count = int(row.delivery_attempt_count)
                incident_version = int(row.version) + 1
                updated = conn.execute(
                    incident_table.update()
                    .where(
                        incident_table.c.incident_id == incident_id,
                        incident_table.c.version == row.version,
                    )
                    .values(
                        message=str(pending.get("message") or ""),
                        status=AlertIncidentStatus.OPEN.value,
                        occurrence_count=(
                            int(row.occurrence_count) + occurrence_count
                        ),
                        updated_at=_db_datetime(now),
                        next_attempt_at=(
                            _db_datetime(now)
                            if row.status != AlertIncidentStatus.OPEN.value
                            else row.next_attempt_at
                        ),
                        acknowledged_at=None,
                        acknowledged_by="",
                        version=incident_version,
                    )
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        "Canonical alert changed during spool import"
                    )

            # Fault injection here proves that the occurrence upsert cannot
            # commit independently of attempts and the durable receipt.
            self._spool_import_fault_hook("after_occurrence")

            if attempts:
                final_attempt_count = starting_attempt_count + len(attempts)
                final_escalation = int(
                    attempts[-1].get("escalation_level") or 0
                )
                final_next_attempt_at = self._spool_datetime(
                    attempts[-1].get("next_attempt_at"), fallback=now
                )
                updated = conn.execute(
                    incident_table.update()
                    .where(
                        incident_table.c.incident_id == incident_id,
                        incident_table.c.version == incident_version,
                        incident_table.c.status
                        == AlertIncidentStatus.OPEN.value,
                    )
                    .values(
                        delivery_attempt_count=final_attempt_count,
                        escalation_level=final_escalation,
                        next_attempt_at=_db_datetime(final_next_attempt_at),
                        updated_at=_db_datetime(now),
                        version=incident_version + 1,
                    )
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        "Canonical alert changed while importing attempts"
                    )
                for offset, attempt in enumerate(attempts, start=1):
                    conn.execute(
                        attempt_table.insert().values(
                            incident_id=incident_id,
                            attempt_number=starting_attempt_count + offset,
                            escalation_level=int(
                                attempt.get("escalation_level") or 0
                            ),
                            attempted_at=_db_datetime(
                                self._spool_datetime(
                                    attempt.get("attempted_at"), fallback=now
                                )
                            ),
                            status=str(
                                attempt.get("status") or "FAILED"
                            ).upper(),
                            provider_delivery_id=str(
                                attempt.get("provider_delivery_id") or ""
                            ),
                            error=str(attempt.get("error") or ""),
                        )
                    )

            conn.execute(
                import_table.insert().values(
                    pending_event_id=pending_event_id,
                    incident_id=incident_id,
                    imported_at=_db_datetime(now),
                )
            )

    def _retry_spooled_alert_if_due(self, pending: Dict[str, Any]) -> int:
        attempts = list(pending.get("delivery_attempts") or [])
        latest = attempts[-1] if attempts else None
        now = _as_utc(self._clock())
        if latest is not None:
            next_attempt_at = self._spool_datetime(
                latest.get("next_attempt_at"), fallback=now
            )
            if now < next_attempt_at:
                return 0
        attempt_number = max(
            len(attempts) + 1,
            int(latest.get("attempt_number") or 0) + 1 if latest else 1,
        )
        escalation_level = min(
            self.max_escalation_level,
            (attempt_number - 1) // self.escalation_every_attempts,
        )
        payload = {
            "incident_id": f"offline-{pending['event_id']}",
            "alert_type": pending["alert_type"],
            "dedupe_key": pending["dedupe_key"],
            "message": pending["message"],
            "device_id": self.device_id,
            "requires_acknowledgement": True,
            "offline_spool": True,
            "attempt_number": attempt_number,
            "escalation_level": escalation_level,
        }
        try:
            provider_delivery_id = str(self.provider.deliver(payload) or "")
            status, error = "DELIVERED", ""
        except Exception as exc:
            provider_delivery_id, status, error = "", "FAILED", str(exc)
        self.local_spool.append(
            "ALERT_DELIVERY_ATTEMPT",
            {
                "pending_event_id": pending["event_id"],
                "status": status,
                "provider_delivery_id": provider_delivery_id,
                "error": error,
                "attempt_number": attempt_number,
                "escalation_level": escalation_level,
                "attempted_at": now.isoformat(),
                "next_attempt_at": self._offline_next_attempt_at(
                    now, attempt_number=attempt_number, status=status
                ).isoformat(),
            },
        )
        return 1

    def _drain_local_spool(self) -> int:
        processed = 0
        for pending in self.local_spool.pending_alerts():
            try:
                self._import_spooled_incident(pending)
                self.local_spool.append(
                    "ALERT_RECONCILED", {"pending_event_id": pending["event_id"]}
                )
                processed += 1
            except Exception:
                # Canonical persistence may remain unavailable for hours.
                # Retry directly from the fsynced local state during that
                # entire window instead of waiting for database recovery.
                processed += self._retry_spooled_alert_if_due(pending)
                continue
        return processed

    def due_incidents(self) -> List[AlertIncident]:
        now = _db_datetime(self._clock())
        table = _incident_table(MetaData())
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(table)
                .where(
                    table.c.status == AlertIncidentStatus.OPEN.value,
                    table.c.next_attempt_at <= now,
                )
                .order_by(table.c.next_attempt_at, table.c.created_at)
            ).fetchall()
        return [_row_to_incident(row) for row in rows]

    def process_due(self) -> int:
        processed = self._drain_local_spool()
        try:
            due_incidents = self.due_incidents()
        except Exception:
            return processed
        for incident in due_incidents:
            now = _as_utc(self._clock())
            attempt_number = incident.delivery_attempt_count + 1
            escalation_level = min(
                self.max_escalation_level,
                (attempt_number - 1) // self.escalation_every_attempts,
            )
            payload = {
                "incident_id": incident.incident_id,
                "alert_type": incident.alert_type.value,
                "dedupe_key": incident.dedupe_key,
                "message": incident.message,
                "occurrence_count": incident.occurrence_count,
                "attempt_number": attempt_number,
                "escalation_level": escalation_level,
                "device_id": self.device_id,
                "requires_acknowledgement": True,
            }
            provider_id = ""
            error = ""
            try:
                provider_id = str(self.provider.deliver(payload) or "")
                attempt_status = "DELIVERED"
                delay = self.acknowledgement_timeout_seconds
            except Exception as exc:
                attempt_status = "FAILED"
                error = str(exc)
                delay = self.retry_base_seconds * (2 ** min(attempt_number - 1, 6))
            incident_table = _incident_table(MetaData())
            attempt_table = _attempt_table(MetaData())
            with self.engine.begin() as conn:
                updated = conn.execute(
                    incident_table.update()
                    .where(
                        incident_table.c.incident_id == incident.incident_id,
                        incident_table.c.version == incident.version,
                        incident_table.c.status == AlertIncidentStatus.OPEN.value,
                    )
                    .values(
                        delivery_attempt_count=attempt_number,
                        escalation_level=escalation_level,
                        next_attempt_at=_db_datetime(now + timedelta(seconds=delay)),
                        updated_at=_db_datetime(now),
                        version=incident.version + 1,
                    )
                )
                if updated.rowcount != 1:
                    continue
                conn.execute(
                    attempt_table.insert().values(
                        incident_id=incident.incident_id,
                        attempt_number=attempt_number,
                        escalation_level=escalation_level,
                        attempted_at=_db_datetime(now),
                        status=attempt_status,
                        provider_delivery_id=provider_id,
                        error=error,
                    )
                )
            processed += 1
        return processed

    def acknowledge(self, incident_id: str, *, acknowledged_by: str) -> bool:
        now = _db_datetime(self._clock())
        table = _incident_table(MetaData())
        with self.engine.begin() as conn:
            row = conn.execute(
                select(table).where(table.c.incident_id == incident_id)
            ).first()
            if row is None:
                return False
            if row.status == AlertIncidentStatus.ACKNOWLEDGED.value:
                return True
            result = conn.execute(
                table.update()
                .where(
                    table.c.incident_id == incident_id,
                    table.c.version == row.version,
                )
                .values(
                    status=AlertIncidentStatus.ACKNOWLEDGED.value,
                    acknowledged_at=now,
                    acknowledged_by=str(acknowledged_by or ""),
                    updated_at=now,
                    version=int(row.version) + 1,
                )
            )
            return result.rowcount == 1

    def resolve_alert(
        self,
        alert_type: CriticalAlertType | str,
        dedupe_key: str,
        *,
        resolved_by: str = "system-recovery",
    ) -> bool:
        """Close an OPEN incident because its monitored condition recovered.

        This is distinct from an operator acknowledgement.  A later
        recurrence reopens the same durable incident and preserves its
        lifetime delivery-attempt sequence.
        """

        resolved_type = self._normalize_type(alert_type)
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("Critical alert resolution requires a dedupe_key")
        now = _db_datetime(self._clock())
        table = _incident_table(MetaData())
        with self.engine.begin() as conn:
            row = conn.execute(
                select(table).where(
                    table.c.alert_type == resolved_type.value,
                    table.c.dedupe_key == key,
                )
            ).first()
            if row is None:
                return False
            if row.status in {
                AlertIncidentStatus.ACKNOWLEDGED.value,
                AlertIncidentStatus.RESOLVED.value,
            }:
                return True
            result = conn.execute(
                table.update()
                .where(
                    table.c.incident_id == row.incident_id,
                    table.c.version == row.version,
                    table.c.status == AlertIncidentStatus.OPEN.value,
                )
                .values(
                    status=AlertIncidentStatus.RESOLVED.value,
                    acknowledged_at=now,
                    acknowledged_by=str(resolved_by or "system-recovery"),
                    updated_at=now,
                    version=int(row.version) + 1,
                )
            )
            return result.rowcount == 1

    def publish_heartbeat(self) -> bool:
        now = _as_utc(self._clock())
        self._last_heartbeat_attempt_at = now
        payload = {
            "device_id": self.device_id,
            "published_at": now.isoformat(),
            "watchdog_dependency": "external",
        }
        provider_id = ""
        error = ""
        try:
            provider_id = str(self.provider.publish_heartbeat(payload) or "")
            status = "PUBLISHED"
            succeeded = True
        except Exception as exc:
            status = "FAILED"
            error = str(exc)
            succeeded = False
        table = _heartbeat_table(MetaData())
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    table.insert().values(
                        device_id=self.device_id,
                        attempted_at=_db_datetime(now),
                        status=status,
                        provider_delivery_id=provider_id,
                        error=error,
                    )
                )
        except Exception:
            self.local_spool.append(
                "HEARTBEAT_ATTEMPT",
                {
                    "device_id": self.device_id,
                    "attempted_at": now.isoformat(),
                    "status": status,
                    "provider_delivery_id": provider_id,
                    "error": error,
                },
            )
        return succeeded

    def publish_heartbeat_if_due(self) -> bool:
        table = _heartbeat_table(MetaData())
        now = _as_utc(self._clock())
        try:
            with self.engine.begin() as conn:
                last = conn.execute(
                    select(table.c.attempted_at)
                    .where(
                        table.c.device_id == self.device_id,
                        table.c.status == "PUBLISHED",
                    )
                    .order_by(table.c.attempted_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
        except Exception:
            last = self._last_heartbeat_attempt_at
        if last is not None and (
            now - _as_utc(last)
        ).total_seconds() < self.heartbeat_interval_seconds:
            return False
        return self.publish_heartbeat()

    def delivery_attempts(self, incident_id: str) -> List[Dict[str, Any]]:
        table = _attempt_table(MetaData())
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(table)
                .where(table.c.incident_id == incident_id)
                .order_by(table.c.attempt_number)
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def heartbeat_attempts(self) -> List[Dict[str, Any]]:
        table = _heartbeat_table(MetaData())
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(table)
                .where(table.c.device_id == self.device_id)
                .order_by(table.c.attempted_at, table.c.id)
            ).fetchall()
        return [dict(row._mapping) for row in rows]


def build_external_alerting_service(
    engine: Engine, *, device_id: str
) -> ExternalAlertingService:
    """Construct the required production publisher for an enabled runtime."""

    alert_url = os.getenv("EXTERNAL_ALERT_WEBHOOK_URL", "").strip()
    heartbeat_url = os.getenv("EXTERNAL_HEARTBEAT_WEBHOOK_URL", "").strip()
    if not alert_url or not heartbeat_url:
        raise ValueError(
            "Enabled Buy Board runtime requires both external alert and heartbeat URLs"
        )
    provider = WebhookAlertDeliveryProvider(
        alert_url=alert_url,
        heartbeat_url=heartbeat_url,
        bearer_token=os.getenv("EXTERNAL_ALERT_WEBHOOK_TOKEN", "").strip(),
    )
    return ExternalAlertingService(engine, provider, device_id=device_id)
