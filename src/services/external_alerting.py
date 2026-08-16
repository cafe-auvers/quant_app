"""Durable delivery for safety-critical alerts and watchdog heartbeats.

This process only publishes heartbeats. Detecting a missing heartbeat must
run outside this application so a crashed or disconnected process cannot be
responsible for reporting its own absence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol
from uuid import uuid4

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
        resolved_type = self._normalize_type(alert_type)
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("Critical alert requires a dedupe_key")
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
                        occurrence_count=1,
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
                    "occurrence_count": int(row.occurrence_count) + 1,
                    "updated_at": now,
                    "version": int(row.version) + 1,
                }
                if row.status == AlertIncidentStatus.ACKNOWLEDGED.value:
                    values.update(
                        status=AlertIncidentStatus.OPEN.value,
                        delivery_attempt_count=0,
                        escalation_level=0,
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
        self.raise_alert(alert_class, dedupe_key, message)

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
        processed = 0
        for incident in self.due_incidents():
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

    def publish_heartbeat(self) -> bool:
        now = _as_utc(self._clock())
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
        return succeeded

    def publish_heartbeat_if_due(self) -> bool:
        table = _heartbeat_table(MetaData())
        now = _as_utc(self._clock())
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
