from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, text

from src.services.external_alerting import (
    AlertIncidentStatus,
    CriticalAlertType,
    ExternalAlertingService,
    LocalAlertSpool,
    WebhookAlertDeliveryProvider,
    build_external_alerting_service,
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
        local_spool=LocalAlertSpool(tmp_path / "alert-spool.jsonl"),
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


def test_acknowledged_incident_recurrence_uses_next_lifetime_attempt(tmp_path):
    provider = FakeProvider()
    service, _ = _service(tmp_path, provider=provider)
    incident = service.raise_alert(
        CriticalAlertType.DATABASE_UNAVAILABLE,
        "recurring-db",
        "database unavailable",
    )
    assert service.process_due() == 1
    assert service.acknowledge(incident.incident_id, acknowledged_by="operator")

    reopened = service.raise_alert(
        CriticalAlertType.DATABASE_UNAVAILABLE,
        "recurring-db",
        "database unavailable again",
    )
    assert reopened.delivery_attempt_count == 1
    assert service.process_due() == 1

    attempts = service.delivery_attempts(incident.incident_id)
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert [attempt["status"] for attempt in attempts] == [
        "DELIVERED",
        "DELIVERED",
    ]
    assert len(provider.deliveries) == 2


def test_acknowledged_incident_preserves_all_prior_attempt_history(tmp_path):
    provider = FakeProvider()
    provider.delivery_failures = 2
    service, now = _service(
        tmp_path,
        provider=provider,
        escalation_every_attempts=2,
    )
    incident = service.raise_alert(
        CriticalAlertType.EXECUTION_LEASE_LOST,
        "lease-main",
        "execution lease lost",
    )
    assert service.process_due() == 1
    now[0] += timedelta(seconds=1)
    assert service.process_due() == 1
    now[0] += timedelta(seconds=2)
    assert service.process_due() == 1
    prior_attempts = service.delivery_attempts(incident.incident_id)
    assert [attempt["attempt_number"] for attempt in prior_attempts] == [1, 2, 3]

    assert service.acknowledge(incident.incident_id, acknowledged_by="operator")
    reopened = service.raise_alert(
        CriticalAlertType.EXECUTION_LEASE_LOST,
        "lease-main",
        "execution lease lost again",
    )
    assert reopened.delivery_attempt_count == 3
    assert reopened.escalation_level == 1
    assert service.process_due() == 1

    attempts = service.delivery_attempts(incident.incident_id)
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2, 3, 4]
    assert attempts[:3] == prior_attempts
    assert attempts[-1]["status"] == "DELIVERED"


def test_acknowledged_recurrence_retains_incident_id_and_counts_occurrence(tmp_path):
    service, _ = _service(tmp_path)
    first = service.raise_alert(
        CriticalAlertType.STALE_CRITICAL_SYMBOL,
        "MSFT",
        "symbol is stale",
    )
    assert service.acknowledge(first.incident_id, acknowledged_by="operator")

    recurrence = service.raise_alert(
        CriticalAlertType.STALE_CRITICAL_SYMBOL,
        "MSFT",
        "symbol is stale again",
    )

    assert recurrence.incident_id == first.incident_id
    assert recurrence.occurrence_count == 2
    assert recurrence.status == AlertIncidentStatus.OPEN


def test_recovered_incident_stops_retrying_and_reopens_on_recurrence(tmp_path):
    service, _ = _service(tmp_path)
    first = service.raise_alert(
        CriticalAlertType.MARKET_DATA_OUTAGE,
        "PROD:1:STIM:MARKET_DATA_OUTAGE_LOW",
        "market-data outage",
    )

    assert service.resolve_alert(
        CriticalAlertType.MARKET_DATA_OUTAGE,
        "PROD:1:STIM:MARKET_DATA_OUTAGE_LOW",
        resolved_by="test-recovery",
    )
    assert service.due_incidents() == []

    recurrence = service.raise_alert(
        CriticalAlertType.MARKET_DATA_OUTAGE,
        "PROD:1:STIM:MARKET_DATA_OUTAGE_LOW",
        "market-data outage again",
    )

    assert recurrence.incident_id == first.incident_id
    assert recurrence.status == AlertIncidentStatus.OPEN
    assert recurrence.occurrence_count == 2


def test_open_incident_key_sweep_is_one_read_without_commit(tmp_path):
    service, _ = _service(tmp_path)
    service.raise_alert(
        CriticalAlertType.MARKET_DATA_OUTAGE,
        "PROD:1:AAPL:MARKET_DATA_OUTAGE_LOW",
        "market-data outage",
    )
    service.raise_alert(
        CriticalAlertType.DATABASE_UNAVAILABLE,
        "PROD:database",
        "database unavailable",
    )
    statements = []
    commits = []
    event.listen(
        service.engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(
            " ".join(statement.upper().split())
        ),
    )
    event.listen(service.engine, "commit", lambda _connection: commits.append(True))

    keys = service.open_incident_keys(
        [CriticalAlertType.MARKET_DATA_OUTAGE, CriticalAlertType.STALE_CRITICAL_SYMBOL]
    )

    assert keys == {
        (
            CriticalAlertType.MARKET_DATA_OUTAGE.value,
            "PROD:1:AAPL:MARKET_DATA_OUTAGE_LOW",
        )
    }
    assert sum("FROM EXTERNAL_ALERT_INCIDENTS" in item for item in statements) == 1
    assert commits == []


def test_empty_due_incident_poll_is_read_only(tmp_path):
    service, _ = _service(tmp_path)
    commits = []
    event.listen(service.engine, "commit", lambda _connection: commits.append(True))

    assert service.due_incidents() == []

    assert commits == []


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


def test_async_heartbeat_is_nonblocking_and_uses_local_cadence(tmp_path):
    published = threading.Event()

    class BlockingProvider(FakeProvider):
        def publish_heartbeat(self, payload):
            self.heartbeats.append(payload)
            published.set()
            return "async-heartbeat"

    provider = BlockingProvider()
    service, now = _service(tmp_path, provider=provider)

    assert service.publish_heartbeat_async_if_due() is True
    assert published.wait(1.0)
    service._heartbeat_thread.join(1.0)
    assert service.publish_heartbeat_async_if_due() is False
    now[0] += timedelta(seconds=10)
    published.clear()
    assert service.publish_heartbeat_async_if_due() is True
    assert published.wait(1.0)
    service._heartbeat_thread.join(1.0)
    assert len(provider.heartbeats) == 2


def test_successful_heartbeat_audit_is_compacted_while_webhook_stays_frequent(
    tmp_path,
):
    provider = FakeProvider()
    service, now = _service(
        tmp_path,
        provider=provider,
        heartbeat_audit_interval_seconds=60,
    )

    assert service.publish_heartbeat_if_due() is True
    now[0] += timedelta(seconds=10)
    assert service.publish_heartbeat_if_due() is True

    assert len(service.heartbeat_attempts()) == 0
    now[0] += timedelta(seconds=50)
    assert service.publish_heartbeat_if_due() is True

    assert len(provider.heartbeats) == 3
    assert len(service.heartbeat_attempts()) == 1


def test_heartbeat_publication_failure_is_durable_and_retried(tmp_path):
    provider = FakeProvider()
    provider.heartbeat_failures = 1
    service, _ = _service(tmp_path, provider=provider)

    assert service.publish_heartbeat_if_due() is False
    assert service.publish_heartbeat_if_due() is True
    assert [row["status"] for row in service.local_heartbeat_attempts()] == [
        "FAILED",
        "PUBLISHED",
    ]


def test_heartbeat_status_flaps_do_not_commit_to_database(tmp_path):
    provider = FakeProvider()
    provider.heartbeat_failures = 1
    service, now = _service(
        tmp_path,
        provider=provider,
        heartbeat_audit_interval_seconds=60,
    )
    statements = []
    commits = []
    event.listen(
        service.engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(
            " ".join(statement.upper().split())
        ),
    )
    event.listen(service.engine, "commit", lambda _connection: commits.append(True))

    assert service.publish_heartbeat_if_due() is False
    now[0] += timedelta(seconds=10)
    assert service.publish_heartbeat_if_due() is True

    assert not any("APPLICATION_HEARTBEAT_ATTEMPTS" in item for item in statements)
    assert commits == []
    assert [row["status"] for row in service.local_heartbeat_attempts()] == [
        "FAILED",
        "PUBLISHED",
    ]


def test_database_outage_spools_and_directly_delivers_critical_alert(tmp_path):
    provider = FakeProvider()
    service, _ = _service(tmp_path, provider=provider)
    healthy_engine = service.engine

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    service.sink(
        CriticalAlertType.DATABASE_UNAVAILABLE.value,
        "prod-db",
        "canonical database unavailable",
    )

    assert len(provider.deliveries) == 1
    assert provider.deliveries[0]["offline_spool"] is True
    assert len(service.local_spool.pending_alerts()) == 1

    service.engine = healthy_engine
    service.process_due()
    assert service.local_spool.pending_alerts() == []
    assert len(provider.deliveries) == 1
    incident = service.raise_alert(
        CriticalAlertType.DATABASE_UNAVAILABLE,
        "prod-db",
        "canonical database unavailable",
    )
    assert [row["status"] for row in service.delivery_attempts(incident.incident_id)] == [
        "DELIVERED"
    ]


def test_known_offline_alert_skips_a_second_database_attempt(tmp_path, monkeypatch):
    provider = FakeProvider()
    service, _ = _service(tmp_path, provider=provider)
    monkeypatch.setattr(
        service,
        "raise_alert",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("known outage must not retry the database")
        ),
    )

    service.sink_offline(
        CriticalAlertType.DATABASE_UNAVAILABLE,
        "prod-db",
        "canonical database unavailable",
    )

    assert len(provider.deliveries) == 1
    assert provider.deliveries[0]["offline_spool"] is True
    assert len(service.local_spool.pending_alerts()) == 1


def test_execution_thread_can_spool_known_outage_without_waiting_on_delivery(
    tmp_path,
):
    provider = FakeProvider()
    service, _ = _service(tmp_path, provider=provider)

    service.sink_offline(
        CriticalAlertType.DATABASE_UNAVAILABLE,
        "prod-db-fast-path",
        "canonical database unavailable",
        deliver_directly=False,
    )

    assert provider.deliveries == []
    assert len(service.local_spool.pending_alerts()) == 1


def test_spool_failure_does_not_suppress_direct_critical_alert_delivery(tmp_path):
    provider = FakeProvider()
    service, _ = _service(tmp_path, provider=provider)

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    class FailedSpool:
        def append(self, *_args, **_kwargs):
            raise OSError("local disk unavailable")

    service.engine = UnavailableEngine()
    service.local_spool = FailedSpool()

    service.sink(
        CriticalAlertType.DATABASE_UNAVAILABLE.value,
        "journal-write-failed",
        "emergency journal could not be written",
    )

    assert len(provider.deliveries) == 1
    assert provider.deliveries[0]["alert_type"] == "DATABASE_UNAVAILABLE"


def test_failed_offline_alert_retries_and_escalates_before_database_recovers(
    tmp_path,
):
    provider = FakeProvider()
    provider.delivery_failures = 2
    service, now = _service(
        tmp_path,
        provider=provider,
        escalation_every_attempts=2,
    )
    healthy_engine = service.engine

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    service.sink(
        CriticalAlertType.DATABASE_UNAVAILABLE.value,
        "sustained-outage",
        "database remains unavailable",
    )
    assert len(provider.deliveries) == 1

    now[0] += timedelta(seconds=1)
    assert service.process_due() == 1
    assert len(provider.deliveries) == 2

    now[0] += timedelta(seconds=2)
    assert service.process_due() == 1
    assert len(provider.deliveries) == 3
    assert provider.deliveries[-1]["escalation_level"] == 1
    offline_attempts = service.local_spool.pending_alerts()[0][
        "delivery_attempts"
    ]
    assert [attempt["status"] for attempt in offline_attempts] == [
        "FAILED",
        "FAILED",
        "DELIVERED",
    ]

    service.engine = healthy_engine
    service.process_due()

    assert service.local_spool.pending_alerts() == []
    assert len(provider.deliveries) == 3
    incident = service.raise_alert(
        CriticalAlertType.DATABASE_UNAVAILABLE,
        "sustained-outage",
        "database remains unavailable",
    )
    canonical_attempts = service.delivery_attempts(incident.incident_id)
    assert [attempt["status"] for attempt in canonical_attempts] == [
        "FAILED",
        "FAILED",
        "DELIVERED",
    ]
    assert canonical_attempts[-1]["escalation_level"] == 1


def test_offline_spool_deduplicates_ten_identical_occurrences(tmp_path):
    provider = FakeProvider()
    service, _ = _service(tmp_path, provider=provider)

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    for occurrence in range(10):
        service.sink(
            CriticalAlertType.DATABASE_UNAVAILABLE.value,
            "same-outage",
            f"database unavailable occurrence {occurrence + 1}",
        )

    pending = service.local_spool.pending_alerts()
    assert len(pending) == 1
    assert pending[0]["occurrence_count"] == 10
    assert pending[0]["message"] == "database unavailable occurrence 10"
    assert len(provider.deliveries) == 1


def test_offline_spool_keeps_different_dedupe_keys_independent(tmp_path):
    provider = FakeProvider()
    service, _ = _service(tmp_path, provider=provider)

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    service.sink(
        CriticalAlertType.DATABASE_UNAVAILABLE.value,
        "primary-db",
        "primary database unavailable",
    )
    service.sink(
        CriticalAlertType.DATABASE_UNAVAILABLE.value,
        "replica-db",
        "replica database unavailable",
    )

    pending = service.local_spool.pending_alerts()
    assert len(pending) == 2
    assert {item["dedupe_key"] for item in pending} == {
        "primary-db",
        "replica-db",
    }
    assert len(provider.deliveries) == 2


def test_repeated_offline_occurrences_do_not_bypass_retry_schedule(tmp_path):
    provider = FakeProvider()
    provider.delivery_failures = 1
    service, _ = _service(tmp_path, provider=provider)

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    service.sink(
        CriticalAlertType.DATABASE_UNAVAILABLE.value,
        "retry-schedule",
        "database unavailable",
    )
    for _ in range(9):
        service.sink(
            CriticalAlertType.DATABASE_UNAVAILABLE.value,
            "retry-schedule",
            "database still unavailable",
        )

    pending = service.local_spool.pending_alerts()
    assert len(provider.deliveries) == 1
    assert len(pending) == 1
    assert pending[0]["occurrence_count"] == 10
    assert len(pending[0]["delivery_attempts"]) == 1


def test_offline_incident_retries_exactly_once_when_schedule_becomes_due(tmp_path):
    provider = FakeProvider()
    provider.delivery_failures = 1
    service, now = _service(tmp_path, provider=provider)

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    service.sink(
        CriticalAlertType.DATABASE_UNAVAILABLE.value,
        "due-retry",
        "database unavailable",
    )

    now[0] += timedelta(seconds=1)
    assert service.process_due() == 1
    assert service.process_due() == 0
    assert len(provider.deliveries) == 2
    assert [
        attempt["status"]
        for attempt in service.local_spool.pending_alerts()[0][
            "delivery_attempts"
        ]
    ] == ["FAILED", "DELIVERED"]


def test_offline_recovery_folds_one_incident_with_occurrences_and_attempts(
    tmp_path,
):
    provider = FakeProvider()
    provider.delivery_failures = 1
    service, now = _service(tmp_path, provider=provider)
    healthy_engine = service.engine

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    for occurrence in range(3):
        service.sink(
            CriticalAlertType.DATABASE_UNAVAILABLE.value,
            "recover-deduped",
            f"database unavailable occurrence {occurrence + 1}",
        )
    now[0] += timedelta(seconds=1)
    assert service.process_due() == 1

    service.engine = healthy_engine
    assert service.process_due() == 1

    assert service.local_spool.pending_alerts() == []
    assert len(provider.deliveries) == 2
    with healthy_engine.connect() as conn:
        incident = conn.execute(
            text(
                "SELECT incident_id, occurrence_count, message "
                "FROM external_alert_incidents "
                "WHERE alert_type = :alert_type AND dedupe_key = :dedupe_key"
            ),
            {
                "alert_type": CriticalAlertType.DATABASE_UNAVAILABLE.value,
                "dedupe_key": "recover-deduped",
            },
        ).mappings().one()
    assert incident["occurrence_count"] == 3
    assert incident["message"] == "database unavailable occurrence 3"
    assert [
        attempt["status"]
        for attempt in service.delivery_attempts(incident["incident_id"])
    ] == ["FAILED", "DELIVERED"]


def test_spool_import_crash_after_occurrence_rolls_back_before_restart(tmp_path):
    provider = FakeProvider()
    provider.delivery_failures = 1
    service, now = _service(tmp_path, provider=provider)
    healthy_engine = service.engine

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    for occurrence in range(3):
        service.sink(
            CriticalAlertType.DATABASE_UNAVAILABLE.value,
            "crash-mid-import",
            f"database unavailable occurrence {occurrence + 1}",
        )
    now[0] += timedelta(seconds=1)
    assert service.process_due() == 1
    pending = service.local_spool.pending_alerts()[0]
    service.engine = healthy_engine

    def crash_after_occurrence(point):
        if point == "after_occurrence":
            raise RuntimeError("simulated process crash")

    service._spool_import_fault_hook = crash_after_occurrence
    with pytest.raises(RuntimeError, match="simulated process crash"):
        service._import_spooled_incident(pending)

    with healthy_engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM external_alert_incidents "
                "WHERE dedupe_key = 'crash-mid-import'"
            )
        ).scalar_one() == 0
        assert conn.execute(
            text("SELECT COUNT(*) FROM external_alert_spool_imports")
        ).scalar_one() == 0

    restarted, _ = _service(tmp_path, provider=provider, now=now)
    assert restarted.process_due() == 1
    assert restarted.local_spool.pending_alerts() == []
    with restarted.engine.connect() as conn:
        incident = conn.execute(
            text(
                "SELECT incident_id, occurrence_count "
                "FROM external_alert_incidents "
                "WHERE dedupe_key = 'crash-mid-import'"
            )
        ).mappings().one()
        assert conn.execute(
            text("SELECT COUNT(*) FROM external_alert_spool_imports")
        ).scalar_one() == 1
    assert incident["occurrence_count"] == 3
    assert [
        attempt["status"]
        for attempt in restarted.delivery_attempts(incident["incident_id"])
    ] == ["FAILED", "DELIVERED"]
    assert len(provider.deliveries) == 2


def test_spool_import_receipt_prevents_replay_after_commit_before_marker(tmp_path):
    provider = FakeProvider()
    provider.delivery_failures = 1
    service, now = _service(tmp_path, provider=provider)
    healthy_engine = service.engine

    class UnavailableEngine:
        def begin(self):
            raise RuntimeError("canonical DB unavailable")

    service.engine = UnavailableEngine()
    for occurrence in range(3):
        service.sink(
            CriticalAlertType.DATABASE_UNAVAILABLE.value,
            "crash-before-marker",
            f"database unavailable occurrence {occurrence + 1}",
        )
    now[0] += timedelta(seconds=1)
    assert service.process_due() == 1
    pending = service.local_spool.pending_alerts()[0]
    service.engine = healthy_engine

    # The canonical transaction commits, then the process dies before it can
    # append ALERT_RECONCILED to the local spool.
    service._import_spooled_incident(pending)
    assert len(service.local_spool.pending_alerts()) == 1
    with healthy_engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT incident_id, occurrence_count, delivery_attempt_count "
                "FROM external_alert_incidents "
                "WHERE dedupe_key = 'crash-before-marker'"
            )
        ).mappings().one()
        receipt = conn.execute(
            text(
                "SELECT pending_event_id, incident_id "
                "FROM external_alert_spool_imports"
            )
        ).mappings().one()
    assert before["occurrence_count"] == 3
    assert before["delivery_attempt_count"] == 2
    assert receipt["pending_event_id"] == pending["event_id"]
    assert receipt["incident_id"] == before["incident_id"]

    restarted, _ = _service(tmp_path, provider=provider, now=now)
    assert restarted.process_due() == 1
    assert restarted.local_spool.pending_alerts() == []
    with restarted.engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT incident_id, occurrence_count, delivery_attempt_count "
                "FROM external_alert_incidents "
                "WHERE dedupe_key = 'crash-before-marker'"
            )
        ).mappings().one()
        assert conn.execute(
            text("SELECT COUNT(*) FROM external_alert_spool_imports")
        ).scalar_one() == 1
    assert dict(after) == dict(before)
    assert [
        attempt["status"]
        for attempt in restarted.delivery_attempts(after["incident_id"])
    ] == ["FAILED", "DELIVERED"]
    assert len(provider.deliveries) == 2


def test_enabled_runtime_composition_requires_real_external_provider_urls(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("EXTERNAL_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_HEARTBEAT_WEBHOOK_URL", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'factory.db'}", future=True)

    with pytest.raises(ValueError, match="requires both"):
        build_external_alerting_service(engine, device_id="pc-main")

    monkeypatch.setenv("EXTERNAL_ALERT_WEBHOOK_URL", "https://alerts.example.test")
    monkeypatch.setenv(
        "EXTERNAL_HEARTBEAT_WEBHOOK_URL", "https://heartbeat.example.test"
    )
    service = build_external_alerting_service(engine, device_id="pc-main")
    assert isinstance(service.provider, WebhookAlertDeliveryProvider)
    assert service.acknowledgement_timeout_seconds == 21600.0
    assert service.heartbeat_interval_seconds == 5.0
    assert service.heartbeat_audit_interval_seconds == 3600.0
