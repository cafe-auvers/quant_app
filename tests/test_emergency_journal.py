from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.execution_mode import ExecutionLease
from src.services.emergency_journal import (
    DuplicateEmergencyCommandError,
    EmergencyJournal,
    EmergencyJournalIntegrityError,
    EmergencyLeaseAllowance,
    EmergencyLeaseAllowanceError,
)
from src.services.execution_command_repository import (
    get_command_by_idempotency_key,
)
from src.services.execution_order_repository import fetch_execution_order


def _lease():
    return ExecutionLease("pc", "token-11", 11)


def _request(journal: EmergencyJournal, *, key: str = "SUBMIT:cid-1"):
    return journal.append_requested(
        idempotency_key=key,
        command_type="submit",
        environment="PROD",
        account_no="12345678",
        symbol="AAPL",
        lease=_lease(),
        source="KANBAN_BOARD",
        order_payload={
            "client_order_id": "cid-1",
            "side": "SELL",
            "intent": "STOP_LOSS",
            "quantity": 10,
            "limit_price": 99.0,
            "exchange": "NASD",
            "attempt_group_id": "group-1",
            "attempt_number": 1,
        },
    )


def test_emergency_journal_fsyncs_before_reporting_requested(monkeypatch, tmp_path):
    fsync_calls = []
    monkeypatch.setattr(
        "src.services.emergency_journal.os.fsync",
        lambda descriptor: fsync_calls.append(descriptor),
    )
    journal = EmergencyJournal(tmp_path / "emergency.jsonl")

    record = _request(journal)

    assert record["event_type"] == "REQUESTED"
    assert fsync_calls
    assert journal.pending_requests()[0]["account_no"] == "12345678"


def test_emergency_journal_replay_protection_rejects_duplicate_identity(tmp_path):
    journal = EmergencyJournal(tmp_path / "emergency.jsonl")
    _request(journal)

    with pytest.raises(DuplicateEmergencyCommandError):
        _request(journal)


def test_emergency_journal_checksum_corruption_fails_closed(tmp_path):
    path = tmp_path / "emergency.jsonl"
    journal = EmergencyJournal(path)
    _request(journal)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["symbol"] = "MSFT"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    with pytest.raises(EmergencyJournalIntegrityError):
        journal.load_entries()


def test_emergency_lease_allowance_expires_monotonically():
    now = [100.0]
    allowance = EmergencyLeaseAllowance(max_seconds=10, monotonic=lambda: now[0])
    assert allowance.record_verified(
        _lease(), verified_current=True, handoff_pending=False
    )
    assert allowance.begin_outage(
        _lease(), verified_current=True, handoff_pending=False
    )
    allowance.require_valid(_lease())

    now[0] = 110.0
    with pytest.raises(EmergencyLeaseAllowanceError, match="expired"):
        allowance.require_valid(_lease())


def test_handoff_pending_when_outage_begins_refuses_emergency_allowance():
    allowance = EmergencyLeaseAllowance(max_seconds=10)

    assert allowance.record_verified(
        _lease(), verified_current=True, handoff_pending=False
    )
    assert allowance.begin_outage(
        _lease(), verified_current=True, handoff_pending=True
    ) is False
    with pytest.raises(EmergencyLeaseAllowanceError):
        allowance.require_valid(_lease())


def test_outage_detection_never_extends_last_verified_lease_allowance():
    now = [0.0]
    allowance = EmergencyLeaseAllowance(max_seconds=30, monotonic=lambda: now[0])
    allowance.record_verified(
        _lease(), verified_current=True, handoff_pending=False
    )
    now[0] = 29.0
    assert allowance.begin_outage(
        _lease(), verified_current=True, handoff_pending=False
    )
    assert allowance.snapshot.verified_at_monotonic == 0.0
    assert allowance.snapshot.expires_at_monotonic == 30.0
    now[0] = 30.0
    with pytest.raises(EmergencyLeaseAllowanceError, match="expired"):
        allowance.require_valid(_lease())


def test_emergency_journal_reconciles_into_canonical_state_on_recovery(tmp_path):
    journal = EmergencyJournal(tmp_path / "emergency.jsonl", journal_id="device-pc")
    request = _request(journal)
    journal.append_outcome(
        requested_sequence=request["sequence"],
        idempotency_key=request["idempotency_key"],
        status="ACKNOWLEDGED",
        broker_response={"broker_order_id": "BR-1"},
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'canonical.db'}",
        future=True,
        poolclass=NullPool,
    )

    assert journal.reconcile_into_canonical(engine) == 1
    assert journal.reconcile_into_canonical(engine) == 0

    command = get_command_by_idempotency_key(engine, "SUBMIT:cid-1")
    assert command is not None
    assert command.status == "ACKNOWLEDGED"
    order = fetch_execution_order(engine, "cid-1")
    assert order is not None
    assert order.broker_order_id == "BR-1"
    assert journal.pending_requests() == []
