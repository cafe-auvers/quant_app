import gzip
import hashlib
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine

from src.services.cloud_coordination_backup import cloud_archive_to_snapshot
from src.services.coordination_store_migration import (
    CoordinationStoreMigrationError, backup_coordination_snapshot,
    coordination_metadata, restore_coordination_snapshot,
)


def _archive(payload, *, payload_checksum=None):
    raw = json.dumps(payload, separators=(",", ":"))
    content = gzip.compress(json.dumps({
        "format": "quant-cloud-coordination-backup-v1",
        "created_at": "2026-10-06T12:00:00.000Z",
        "payload_text": raw,
        "payload_sha256": payload_checksum or hashlib.sha256(raw.encode()).hexdigest(),
    }).encode())
    return content, hashlib.sha256(content).hexdigest()


@pytest.fixture
def source():
    engine = create_engine("sqlite://")
    metadata = coordination_metadata()
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(metadata.tables["trade_cards"].insert().values(
            id=73, environment="SIM", account_no="fixture", symbol="AAPL",
            board_status="WATCHLIST", version=42, payload='{"symbol":"AAPL","note":"시장 확인 · café"}',
            updated_at=datetime(2026, 10, 6, 1, 2, 3, 456789),
        ))
    yield engine
    engine.dispose()


def test_cloud_archive_restores_exact_ids_versions_payloads_and_timestamps(source):
    original = backup_coordination_snapshot(source)
    content, checksum = _archive(original["payload"])
    restored = cloud_archive_to_snapshot(content, checksum)
    target = create_engine("sqlite://")
    try:
        coordination_metadata().create_all(target)
        result = restore_coordination_snapshot(target, restored)
        assert result["verified"]
        assert result["rows"]["trade_cards"] == 1
        assert backup_coordination_snapshot(target)["payload"] == original["payload"]
    finally:
        target.dispose()


def test_cloud_archive_rejects_corrupted_stored_bytes(source):
    content, checksum = _archive(backup_coordination_snapshot(source)["payload"])
    with pytest.raises(CoordinationStoreMigrationError, match="checksum"):
        cloud_archive_to_snapshot(content[:-1] + b"x", checksum)


def test_cloud_archive_rejects_inner_payload_tampering(source):
    content, checksum = _archive(backup_coordination_snapshot(source)["payload"], payload_checksum="0" * 64)
    with pytest.raises(CoordinationStoreMigrationError, match="payload checksum"):
        cloud_archive_to_snapshot(content, checksum)


@pytest.mark.parametrize("mutation", ("table", "column", "timestamp"))
def test_cloud_archive_fails_closed_on_schema_or_type_drift(source, mutation):
    payload = backup_coordination_snapshot(source)["payload"]
    if mutation == "table":
        del payload["tables"]["execution_orders"]
    elif mutation == "column":
        del payload["tables"]["trade_cards"][0]["version"]
    else:
        payload["tables"]["trade_cards"][0]["updated_at"]["value"] = "invalid"
    content, checksum = _archive(payload)
    with pytest.raises(CoordinationStoreMigrationError):
        cloud_archive_to_snapshot(content, checksum)


def test_cloud_archive_bounds_decompression(monkeypatch):
    import src.services.cloud_coordination_backup as module
    monkeypatch.setattr(module, "MAX_ARCHIVE_BYTES", 100)
    content = gzip.compress(b" " * 101)
    with pytest.raises(CoordinationStoreMigrationError, match="budget"):
        cloud_archive_to_snapshot(content, hashlib.sha256(content).hexdigest())


@pytest.mark.parametrize("schedule", ("* * * * *", "*/5 * * * *", "0 */2 * * *", "60 0 * * *", "0 24 * * *"))
def test_free_backup_scheduler_rejects_frequent_or_invalid_runs(schedule):
    from scripts.schedule_cloud_coordination_backup import validate_daily_schedule
    with pytest.raises(ValueError, match="daily"):
        validate_daily_schedule(schedule)


def test_free_backup_scheduler_normalizes_one_daily_run():
    from scripts.schedule_cloud_coordination_backup import validate_daily_schedule
    assert validate_daily_schedule("15 0 * * *") == "15 0 * * *"
    assert validate_daily_schedule("05 09 * * *") == "5 9 * * *"
