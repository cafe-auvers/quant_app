"""Opt-in PostgreSQL checks, always in newly created disposable test schemas."""
import json
import os
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text

from src.infrastructure.database.coordination_engine import (
    coordination_server_now, create_coordination_connection_engine,
    normalize_coordination_database_config,
)
from src.services.coordination_store_migration import (
    export_coordination_snapshot, provision_private_coordination_schema,
    provision_coordination_application_role,
    restore_coordination_snapshot,
)


@pytest.fixture
def postgres_store():
    raw = os.environ.get("QUANT_POSTGRES_TEST_CONFIG")
    if not raw:
        pytest.skip("Set private PostgreSQL test connection settings to run integration checks")
    values = json.loads(raw)
    values["COORD_DB_PASSWORD"] = os.environ["QUANT_POSTGRES_TEST_PASSWORD"]
    values["COORD_DB_BACKEND"] = "postgresql"
    schema = "quant_migration_qa_" + uuid.uuid4().hex[:16]
    values["COORD_DB_SCHEMA"] = schema
    engine = create_coordination_connection_engine(
        normalize_coordination_database_config(values), read_only=False, pool_size=3
    )
    try:
        provision_private_coordination_schema(engine, schema)
        yield engine
    finally:
        # Only this fixture's freshly generated test schema may be removed.
        assert schema.startswith("quant_migration_qa_") and len(schema) == 35
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_postgresql_server_clock_and_private_access(postgres_store):
    with postgres_store.begin() as connection:
        before = connection.execute(select(coordination_server_now(postgres_store))).scalar_one()
        connection.execute(text("SELECT pg_sleep(0.02)"))
        after = connection.execute(select(coordination_server_now(postgres_store))).scalar_one()
        assert after > before
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        for role in ("anon", "authenticated"):
            assert connection.execute(
                text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"),
                {"role": role, "schema": schema},
            ).scalar_one() is False


def test_postgresql_concurrent_first_account_reservation_serializes(postgres_store):
    from src.services.capital_reservation_repository import _lock_account_reservation_scope
    scope = SimpleNamespace(environment="SIM", account_no="migration-test")
    first_locked, second_started, release = threading.Event(), threading.Event(), threading.Event()
    def first():
        with postgres_store.begin() as connection:
            _lock_account_reservation_scope(connection, scope)
            first_locked.set()
            assert release.wait(8)
    def second():
        with postgres_store.begin() as connection:
            second_started.set()
            _lock_account_reservation_scope(connection, scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first)
        assert first_locked.wait(5)
        two = pool.submit(second)
        try:
            assert second_started.wait(5)
            assert not two.done()
        finally:
            release.set()
        one.result(timeout=5)
        two.result(timeout=5)


def test_postgresql_stale_executor_lease_is_fenced_after_reclaim(postgres_store):
    from src.services import state_sync as ss
    pc = ss.LocalDeviceRole("migration-pc", "migration-pc", True)
    first = ss.claim_main_device(postgres_store, pc)
    assert first.success
    stale = {"expected_lease_token": first.main_device.lease_token, "expected_lease_epoch": first.main_device.lease_epoch}
    assert ss.release_main_device(postgres_store, pc, **stale).success
    second = ss.claim_main_device_if_unclaimed(postgres_store, pc)
    assert second.success
    assert second.main_device.lease_epoch > first.main_device.lease_epoch
    assert ss.release_main_device(postgres_store, pc, **stale).success is False
    assert ss.get_main_device(postgres_store).main_device.lease_token == second.main_device.lease_token


def test_postgresql_runtime_role_is_restricted_and_connects_through_pooler(postgres_store):
    with postgres_store.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    role = "quant_migration_qa_" + uuid.uuid4().hex[:16]
    values = json.loads(os.environ["QUANT_POSTGRES_TEST_CONFIG"])
    suffix = values["COORD_DB_USER"].partition(".")[2]
    values.update(COORD_DB_BACKEND="postgresql", COORD_DB_SCHEMA=schema,
        COORD_DB_USER=role + ("." + suffix if suffix else ""), COORD_DB_PASSWORD=secrets.token_urlsafe(36))
    engine = None
    try:
        provision_coordination_application_role(postgres_store, schema, role, values["COORD_DB_PASSWORD"])
        engine = create_coordination_connection_engine(normalize_coordination_database_config(values), read_only=False)
        with engine.begin() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM trade_cards")).scalar_one() == 0
            flags = connection.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
            assert all(value is False for value in flags)
            assert connection.execute(text("SELECT has_schema_privilege(current_user, current_schema(), 'CREATE')")).scalar_one() is False
            from src.services.capital_reservation_repository import _lock_account_reservation_scope
            _lock_account_reservation_scope(connection, SimpleNamespace(environment="SIM", account_no="role-test"))
        with pytest.raises(Exception):
            with engine.begin() as connection:
                connection.execute(text("SELECT * FROM auth.users LIMIT 1"))
    finally:
        if engine is not None:
            engine.dispose()
        assert role.startswith("quant_migration_qa_") and len(role) == 35
        with postgres_store.begin() as connection:
            connection.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" FROM "{role}"'))
            connection.execute(text(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA "{schema}" FROM "{role}"'))
            connection.execute(text(f'REVOKE ALL ON SCHEMA "{schema}" FROM "{role}"'))
            connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" REVOKE ALL ON TABLES FROM "{role}"'))
            connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" REVOKE ALL ON SEQUENCES FROM "{role}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


def test_postgresql_restore_preserves_journal_ids_and_advances_sequence(postgres_store, tmp_path):
    from src.services.coordination_schema import ensure_coordination_schema
    from src.services.execution_command_repository import ExecutionCommand, insert_command
    source = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    ensure_coordination_schema(source)
    with source.begin() as connection:
        insert_command(connection, ExecutionCommand(
            idempotency_key="migration-journal-1", command_type="submit",
            environment="SIM", account_no="test", symbol="TEST", lease_epoch=1,
        ))
    backup = export_coordination_snapshot(source, writers_stopped=True)
    result = restore_coordination_snapshot(postgres_store, backup)
    assert result["verified"]
    assert export_coordination_snapshot(postgres_store, writers_stopped=True)["payload"] == backup["payload"]
    with postgres_store.begin() as connection:
        new = insert_command(connection, ExecutionCommand(
            idempotency_key="migration-journal-2", command_type="submit",
            environment="SIM", account_no="test", symbol="TEST", lease_epoch=1,
        ))
    assert new.command_id == 2
    source.dispose()


def test_postgresql_command_idempotency_remains_unique(postgres_store):
    from src.services.execution_command_repository import DuplicateCommandError, ExecutionCommand, insert_command
    def command():
        return ExecutionCommand(idempotency_key="one-command", command_type="cancel",
            environment="SIM", account_no="test", symbol="TEST", lease_epoch=1)
    with postgres_store.begin() as connection:
        insert_command(connection, command())
    with pytest.raises(DuplicateCommandError):
        with postgres_store.begin() as connection:
            insert_command(connection, command())


def test_postgresql_mobile_removal_and_stale_edit_rejection(postgres_store, tmp_path, monkeypatch):
    from src.core.trade_card_state import BoardStatus, StopType, TradeCardState
    from src.services.trade_card_repository import create_trade_card, get_trade_card
    from src.services.state_sync import LocalDeviceRole, save_local_device_role, set_operator_control
    from src.web.canonical_planning import CanonicalPlanningSource
    from src.web.connected_planning import ConnectedPlanningService
    from src.web.config import WebConfig
    from src.web.operator_identity import mobile_web_role
    from src.web.store import ConflictError
    from src.services import pc_remote_control
    monkeypatch.setattr(pc_remote_control, "notify_pc_coordination_change", lambda *_args, **_kwargs: True)
    config = WebConfig(mode="CONNECTED", canonical_planning_reads=True,
        canonical_planning_writes=True, canonical_account_no="migration-test",
        connected_passive_operations=("remove_buylist",), pc_repository_path=str(tmp_path))
    save_local_device_role(LocalDeviceRole(device_id="migration-pc", hostname="migration-pc", is_main=False), path=tmp_path / "data" / "device_role.json")
    assert set_operator_control(postgres_store, mobile_web_role(config), mobile_web_role(config)).success
    card = TradeCardState(environment="PROD", account_no="migration-test", symbol="BLKB",
        board_status=BoardStatus.BUYLIST, buylist_member=True, warnings=["migrated_from_buylist"],
        stop_type=StopType.MANUAL_PRICE, active_stop_price=44.54, stop_quantity=0)
    create_trade_card(postgres_store, card, local_snapshot_path=tmp_path / "cards.json", record_summary=False)
    source = CanonicalPlanningSource(enabled=True, engine=postgres_store, account_no="migration-test", cache_seconds=0)
    service = ConnectedPlanningService(config, source, engine=postgres_store, unavailable_reason="")
    result = service.apply(command_id=str(uuid.uuid4()), operation="remove_buylist", symbol="BLKB", expected_revision=1, breakout_price=None)
    assert result["card"]["buylist_member"] is False
    stored = get_trade_card(postgres_store, "PROD", "migration-test", "BLKB")
    assert stored.version == 2
    assert stored.active_stop_price is None
    with pytest.raises(ConflictError):
        service.apply(command_id=str(uuid.uuid4()), operation="remove_buylist", symbol="BLKB", expected_revision=1, breakout_price=None)
