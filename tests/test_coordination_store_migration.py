from datetime import date, datetime, time

import pytest
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Numeric, Time, create_engine, select

from src.services.coordination_store_migration import (
    CoordinationStoreMigrationError, coordination_metadata,
    export_coordination_snapshot, restore_coordination_snapshot,
    validate_coordination_snapshot,
)


def _populated_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    metadata = coordination_metadata()
    metadata.create_all(engine)
    with engine.begin() as connection:
        for table in metadata.tables.values():
            values = {}
            for column in table.columns:
                if isinstance(column.type, Boolean):
                    values[column.name] = True
                elif isinstance(column.type, DateTime):
                    values[column.name] = datetime(2026, 10, 5, 1, 2, 3, 456789)
                elif isinstance(column.type, Date):
                    values[column.name] = date(2026, 10, 5)
                elif isinstance(column.type, Time):
                    values[column.name] = time(1, 2, 3)
                elif isinstance(column.type, Integer):
                    values[column.name] = 7
                elif isinstance(column.type, (Float, Numeric)):
                    values[column.name] = 17.125
                else:
                    values[column.name] = 'x'
            connection.execute(table.insert().values(**values))
    return engine


def test_backup_restores_every_table_with_exact_versions_ids_and_timestamps(tmp_path):
    source = _populated_store(tmp_path)
    snapshot = export_coordination_snapshot(source, writers_stopped=True)
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    coordination_metadata().create_all(target)
    result = restore_coordination_snapshot(target, snapshot)
    assert result["verified"]
    assert all(count == 1 for count in result["rows"].values())
    assert export_coordination_snapshot(target, writers_stopped=True)["payload"] == snapshot["payload"]
    source.dispose()
    target.dispose()


def test_export_requires_source_writers_to_be_stopped(tmp_path):
    with pytest.raises(CoordinationStoreMigrationError, match="Stop all"):
        export_coordination_snapshot(create_engine("sqlite://"), writers_stopped=False)


def test_restore_clears_cached_payloads_when_versions_and_timestamps_are_reused(tmp_path):
    from src.services.coordination_snapshot import read_versioned_rows

    engine = _populated_store(tmp_path)
    metadata = coordination_metadata()
    table = metadata.tables["app_state_sync"]

    def read():
        return read_versioned_rows(
            engine, table, cache_key=("restore-test",),
            key_columns=("state_key",), revision_column="revision",
        )[0].payload

    assert read() == "x"
    with engine.begin() as connection:
        connection.execute(table.update().values(payload='{"restored":true}'))
    backup = export_coordination_snapshot(engine, writers_stopped=True)
    with engine.begin() as connection:
        for item in metadata.tables.values():
            connection.execute(item.delete())
    restore_coordination_snapshot(engine, backup)
    assert read() == '{"restored":true}'
    engine.dispose()


def test_backup_rejects_tampering_before_restoration(tmp_path):
    snapshot = export_coordination_snapshot(_populated_store(tmp_path), writers_stopped=True)
    snapshot["payload"]["tables"]["trade_cards"][0]["version"] += 1
    with pytest.raises(CoordinationStoreMigrationError, match="checksum"):
        validate_coordination_snapshot(snapshot)


def test_restore_refuses_nonempty_target_without_changing_it(tmp_path):
    source = _populated_store(tmp_path)
    snapshot = export_coordination_snapshot(source, writers_stopped=True)
    with pytest.raises(CoordinationStoreMigrationError, match="refusing to overwrite"):
        restore_coordination_snapshot(source, snapshot)
    assert export_coordination_snapshot(source, writers_stopped=True)["payload"] == snapshot["payload"]


def test_export_rejects_missing_or_unknown_coordination_tables(tmp_path):
    source = _populated_store(tmp_path)
    with source.begin() as connection:
        coordination_metadata().tables["operator_commands"].drop(connection)
    with pytest.raises(CoordinationStoreMigrationError, match="Source table set"):
        export_coordination_snapshot(source, writers_stopped=True)


def test_restoration_rolls_back_when_row_verification_fails(tmp_path, monkeypatch):
    from src.services import coordination_store_migration as migration
    snapshot = export_coordination_snapshot(_populated_store(tmp_path), writers_stopped=True)
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    metadata = coordination_metadata()
    metadata.create_all(target)
    monkeypatch.setattr(migration, "_table_rows", lambda *_args: [])
    with pytest.raises(CoordinationStoreMigrationError, match="verification failed"):
        restore_coordination_snapshot(target, snapshot)
    with target.connect() as connection:
        assert all(connection.execute(select(table)).first() is None for table in metadata.tables.values())
