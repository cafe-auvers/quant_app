from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from src.services.external_alerting import (
    AlertIncidentStatus,
    CriticalAlertType,
    ExternalAlertingService,
)


class FakeProvider:
    def __init__(self) -> None:
        self.deliveries = []
        self.heartbeats = []
        self.delivery_failures = 0
        self.heartbeat_failures = 0

    def deliver(self, payload):
        self.deliveries.append(payload)
        if self.delivery_failures:
            self.delivery_failures -= 1
            raise RuntimeError("provider unavailable")
        return f"delivery-{len(self.deliveries)}"

    def publish_heartbeat(self, payload):
        self.heartbeats.append(payload)
        if self.heartbeat_failures:
            self.heartbeat_failures -= 1
            raise RuntimeError("heartbeat endpoint unavailable")
        return f"heartbeat-{len(self.heartbeats)}"


def _service(tmp_path, *, provider=None, now=None, **kwargs):
    clock = now or [datetime(2026, 8, 16, tzinfo=timezone.utc)]
    service = ExternalAlertingService(
        create_engine(f"sqlite:///{tmp_path / 'alerts.db'}", future=True),
        provider or FakeProvider(),
        device_id="pc-main",
        clock=lambda: clock[0],
        retry_base_seconds=1,
        acknowledgement_timeout_seconds=5,
        heartbeat_interval_seconds=10,
        **kwargs,
    )
    return service, clock


@pytest.mark.parametrize("alert_type", list(CriticalAlertType))
def test_every_contract_critical_alert_type_is_independently_durable(
    tmp_path, alert_type
):
    service, _ = _service(tmp_path)

    incident = service.raise_alert(alert_type, f"key-{alert_type.value}", "critical")

    assert incident.alert_type == alert_type
    assert incident.status == AlertIncidentStatus.OPEN


def test_alert_delivery_failure_retries_and_escalates(tmp_path):
    provider = FakeProvider()
    provider.delivery_failures = 2
    service, now = _service(
        tmp_path,
        provider=provider,
        escalation_every_attempts=2,
    )
    incident = service.raise_alert(
        CriticalAlertType.DATABASE_UNAVAILABLE, "db-prod", "database unavailable"
    )

    assert service.process_due() == 1
    now[0] += timedelta(seconds=1)
    assert service.process_due() == 1
    now[0] += timedelta(seconds=2)
    assert service.process_due() == 1

    attempts = service.delivery_attempts(incident.incident_id)
    assert [item["status"] for item in attempts] == [
        "FAILED",
        "FAILED",
        "DELIVERED",
    ]
    assert attempts[-1]["escalation_level"] == 1


def test_alert_deduplication_and_acknowledgement_are_incident_scoped(tmp_path):
    service, _ = _service(tmp_path)
    first = service.raise_alert(
        CriticalAlertType.STALE_CRITICAL_SYMBOL, "AAPL", "first"
    )
    second = service.raise_alert(
        CriticalAlertType.STALE_CRITICAL_SYMBOL, "AAPL", "still stale"
    )

    assert second.incident_id == first.incident_id
    assert second.occurrence_count == 2
    assert service.acknowledge(first.incident_id, acknowledged_by="operator")
    assert service.process_due() == 0


def test_heartbeat_is_published_on_the_expected_cadence(tmp_path):
    provider = FakeProvider()
    service, now = _service(tmp_path, provider=provider)

    assert service.publish_heartbeat_if_due() is True
    now[0] += timedelta(seconds=9)
    assert service.publish_heartbeat_if_due() is False
    now[0] += timedelta(seconds=1)
    assert service.publish_heartbeat_if_due() is True
    assert len(provider.heartbeats) == 2
    assert service.watchdog_is_external is True


def test_heartbeat_publication_failure_is_durable_and_retried(tmp_path):
    provider = FakeProvider()
    provider.heartbeat_failures = 1
    service, _ = _service(tmp_path, provider=provider)

    assert service.publish_heartbeat_if_due() is False
    assert service.publish_heartbeat_if_due() is True
    assert [row["status"] for row in service.heartbeat_attempts()] == [
        "FAILED",
        "PUBLISHED",
    ]
