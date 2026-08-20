from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from src.core.runtime_readiness import RuntimeDeviceState
from src.core.schema_version import CURRENT_EXECUTION_SCHEMA_VERSION
from src.core.capital_reservation import CapitalReservation
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
)
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.core.trade_card_state import TradeCardState
from src.services.capital_reservation_repository import fetch_reservation
from src.services.execution_order_repository import (
    fetch_execution_order,
    record_execution_order,
)
from src.core.execution_mode import ExecutionLease
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.order_ledger import save_orders
from src.services.runtime_device_state_repository import (
    require_compatible_runtime_schema,
    save_runtime_device_state,
)
from src.services import runtime_status
from src.services.state_sync import (
    LocalDeviceRole,
    claim_main_device,
    claim_main_device_if_stale,
)
from src.services.schema_migration import (
    MigrationEntriesBlockedError,
    MigrationCutoverOwnershipError,
    MigrationPhase,
    MigrationRollbackForbiddenError,
    SchemaMigrationManager,
)
from src.services.trade_card_repository import (
    create_trade_card,
    ensure_trade_cards_table,
    get_trade_card,
    save_local_trade_cards_snapshot,
)
from src.utils.storage import save_json


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'migration.db'}", future=True)


def _manager(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(
        "src.services.schema_migration.ORDERS_FILE",
        tmp_path / "missing-orders.json",
    )
    monkeypatch.setattr(
        "src.services.schema_migration.LOCAL_TRADE_CARDS_FILE",
        tmp_path / "missing-trade-cards.json",
    )
    monkeypatch.setattr(
        "src.services.schema_migration.RESERVATIONS_FILE",
        tmp_path / "missing-reservations.json",
    )
    kwargs.setdefault(
        "lease_protocol",
        FakeExecutionLeaseProtocol(
            current=ExecutionLease("pc-main", "token-8", 8)
        ),
    )
    return SchemaMigrationManager(
        _engine(tmp_path),
        backup_path=tmp_path / "migration-backup.json",
        legacy_paths=kwargs.pop("legacy_paths", ()),
        **kwargs,
    )


def _prepare(manager):
    return manager.prepare_cutover(
        device_id="pc-main", lease_token="token-8", lease_epoch=8
    )


def test_migration_is_idempotent(tmp_path, monkeypatch):
    calls = []
    manager = _manager(
        tmp_path,
        monkeypatch,
        migration_callback=lambda: calls.append("migrate"),
    )

    first = _prepare(manager)
    second = _prepare(manager)

    assert first.phase == MigrationPhase.AWAITING_RECONCILIATION
    assert second.phase == MigrationPhase.AWAITING_RECONCILIATION
    assert calls == ["migrate"]
    assert manager.mark_reconciliation_complete().phase == MigrationPhase.READY
    assert _prepare(manager).phase == MigrationPhase.READY


def test_migration_converts_local_order_card_and_reservation_records(
    tmp_path, monkeypatch
):
    order_path = tmp_path / "orders.json"
    card_path = tmp_path / "trade_cards.json"
    reservation_path = tmp_path / "capital_reservations.json"
    order = BrokerOrder(
        client_order_id="legacy-order-1",
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        quantity_requested=3,
        limit_price=99.0,
        exchange="NASD",
        status=OrderStatus.WORKING,
        broker_order_id="BR-LEGACY",
    )
    card = TradeCardState(
        environment="PROD", account_no="12345678-01", symbol="AAPL"
    )
    reservation = CapitalReservation.create(
        environment="PROD",
        account_no="12345678-01",
        symbol="MSFT",
        attempt_group_id="legacy-group",
        requested_notional=1000,
    )
    save_orders([order], path=order_path)
    save_local_trade_cards_snapshot([card], path=card_path)
    save_json(
        reservation_path,
        {"reservations": [reservation.to_dict()]},
    )
    monkeypatch.setattr("src.services.schema_migration.ORDERS_FILE", order_path)
    monkeypatch.setattr(
        "src.services.schema_migration.LOCAL_TRADE_CARDS_FILE", card_path
    )
    monkeypatch.setattr(
        "src.services.schema_migration.RESERVATIONS_FILE", reservation_path
    )
    engine = _engine(tmp_path)
    manager = SchemaMigrationManager(
        engine,
        backup_path=tmp_path / "migration-backup.json",
        legacy_paths=(order_path, card_path, reservation_path),
        lease_protocol=FakeExecutionLeaseProtocol(
            current=ExecutionLease("pc-main", "token-8", 8)
        ),
    )

    _prepare(manager)

    migrated_order = fetch_execution_order(engine, order.client_order_id)
    assert migrated_order is not None
    assert (
        migrated_order.recovery_state
        == OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN
    )
    assert get_trade_card(engine, "PROD", "12345678-01", "AAPL") is not None
    assert fetch_reservation(engine, reservation.reservation_id) is not None


def test_migration_keeps_rejected_order_without_broker_id_terminal(
    tmp_path, monkeypatch
):
    order_path = tmp_path / "orders.json"
    rejected = BrokerOrder(
        client_order_id="legacy-rejected",
        environment="PROD",
        account_no="12345678-01",
        symbol="OMH",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=20,
        limit_price=0.9,
        exchange="NASD",
        status=OrderStatus.REJECTED,
        broker_order_id="",
    )
    save_orders([rejected], path=order_path)
    monkeypatch.setattr("src.services.schema_migration.ORDERS_FILE", order_path)
    engine = _engine(tmp_path)
    manager = SchemaMigrationManager(
        engine,
        backup_path=tmp_path / "migration-backup.json",
        legacy_paths=(order_path,),
        lease_protocol=FakeExecutionLeaseProtocol(
            current=ExecutionLease("pc-main", "token-8", 8)
        ),
    )

    _prepare(manager)

    migrated = fetch_execution_order(engine, rejected.client_order_id)
    assert migrated.status == ExecutionOrderStatus.REJECTED
    assert (
        migrated.broker_identity_status
        == BrokerIdentityStatus.NO_BROKER_ORDER_CONFIRMED
    )
    assert migrated.recovery_state == OrderRecoveryState.NONE


def test_ready_migration_repairs_rejected_order_previously_marked_unknown(
    tmp_path, monkeypatch
):
    order_path = tmp_path / "orders.json"
    rejected = BrokerOrder(
        client_order_id="legacy-rejected",
        environment="PROD",
        account_no="12345678-01",
        symbol="OMH",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=20,
        limit_price=0.9,
        exchange="NASD",
        status=OrderStatus.REJECTED,
        broker_order_id="",
    )
    save_orders([rejected], path=order_path)
    monkeypatch.setattr("src.services.schema_migration.ORDERS_FILE", order_path)
    engine = _engine(tmp_path)
    manager = SchemaMigrationManager(
        engine,
        backup_path=tmp_path / "migration-backup.json",
        legacy_paths=(order_path,),
        lease_protocol=FakeExecutionLeaseProtocol(
            current=ExecutionLease("pc-main", "token-8", 8)
        ),
    )
    record_execution_order(
        engine,
        ExecutionOrderRecord(
            environment=rejected.environment,
            account_no=rejected.account_no,
            symbol=rejected.symbol,
            side=rejected.side,
            intent=rejected.intent,
            client_order_id=rejected.client_order_id,
            submitted_quantity=rejected.quantity_requested,
            submitted_limit_price=rejected.limit_price,
            owner_device_id="migration",
            status=ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
            broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
            recovery_state=OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN,
        ),
    )
    _prepare(manager)
    manager.mark_reconciliation_complete()

    assert _prepare(manager).phase == MigrationPhase.READY
    repaired = fetch_execution_order(engine, rejected.client_order_id)
    assert repaired.status == ExecutionOrderStatus.REJECTED
    assert (
        repaired.broker_identity_status
        == BrokerIdentityStatus.NO_BROKER_ORDER_CONFIRMED
    )
    assert repaired.recovery_state == OrderRecoveryState.NONE


def test_first_launch_after_migration_blocks_entries_until_reconciliation_completes(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    _prepare(manager)

    with pytest.raises(MigrationEntriesBlockedError):
        manager.require_entries_ready()

    manager.mark_reconciliation_complete()
    manager.require_entries_ready()


def test_migration_crash_after_backup_resumes_without_replacing_backup(
    tmp_path, monkeypatch
):
    def crash(point):
        if point == "after_backup":
            raise RuntimeError("crash before cutover")

    manager = _manager(tmp_path, monkeypatch, fault_hook=crash)
    with pytest.raises(RuntimeError, match="before cutover"):
        _prepare(manager)
    checksum = manager.state.backup_checksum
    assert manager.state.phase == MigrationPhase.BACKED_UP

    resumed = _manager(tmp_path, monkeypatch)
    assert _prepare(resumed).phase == MigrationPhase.AWAITING_RECONCILIATION
    assert resumed.state.backup_checksum == checksum


def test_migration_crash_after_cutover_resumes_forward(
    tmp_path, monkeypatch
):
    def crash(point):
        if point == "after_cutover":
            raise RuntimeError("crash after cutover")

    manager = _manager(tmp_path, monkeypatch, fault_hook=crash)
    with pytest.raises(RuntimeError, match="after cutover"):
        _prepare(manager)
    assert manager.state.phase == MigrationPhase.AWAITING_RECONCILIATION

    resumed = _manager(tmp_path, monkeypatch)
    assert _prepare(resumed).phase == MigrationPhase.AWAITING_RECONCILIATION
    assert resumed.mark_reconciliation_complete().phase == MigrationPhase.READY


def test_rollback_allowed_before_any_post_migration_broker_mutation(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("before", encoding="utf-8")
    manager = _manager(
        tmp_path,
        monkeypatch,
        legacy_paths=(legacy,),
        migration_callback=lambda: legacy.write_text("after", encoding="utf-8"),
    )
    _prepare(manager)

    manager.rollback_direct(
        device_id="pc-main", lease_token="token-8", lease_epoch=8
    )

    assert legacy.read_text(encoding="utf-8") == "before"
    assert manager.state.phase == MigrationPhase.NOT_STARTED


def test_direct_rollback_restores_datetime_rows_exactly(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    ensure_trade_cards_table(engine)
    create_trade_card(engine, TradeCardState("PROD", "1", "AAPL"))
    table = Table("trade_cards", MetaData(), autoload_with=engine)
    original_updated_at = datetime(2026, 1, 2, 3, 4, 5, 678901)
    with engine.begin() as conn:
        conn.execute(table.update().values(updated_at=original_updated_at))
        original = dict(conn.execute(select(table)).one()._mapping)

    manager = _manager(tmp_path, monkeypatch)
    _prepare(manager)
    migrated_table = Table("trade_cards", MetaData(), autoload_with=manager.engine)
    with manager.engine.begin() as conn:
        conn.execute(
            migrated_table.update().values(
                updated_at=datetime(2030, 6, 7, 8, 9, 10),
                payload='{"mutated":true}',
            )
        )

    manager.rollback_direct(
        device_id="pc-main", lease_token="token-8", lease_epoch=8
    )

    restored_table = Table("trade_cards", MetaData(), autoload_with=manager.engine)
    with manager.engine.connect() as conn:
        restored = dict(conn.execute(select(restored_table)).one()._mapping)
    assert restored == original
    assert restored["updated_at"] == original_updated_at


def test_direct_rollback_drops_tables_that_were_absent_before_migration(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    target_tables = set(manager._DATABASE_TABLES)
    assert target_tables.isdisjoint(inspect(manager.engine).get_table_names())

    _prepare(manager)
    assert target_tables.issubset(inspect(manager.engine).get_table_names())

    manager.rollback_direct(
        device_id="pc-main", lease_token="token-8", lease_epoch=8
    )

    assert target_tables.isdisjoint(inspect(manager.engine).get_table_names())

    # The repository-level ensure caches must also be invalidated so a later
    # retry can recreate the tables on this same long-lived Engine.
    _prepare(manager)
    assert target_tables.issubset(inspect(manager.engine).get_table_names())


def test_direct_restore_refused_after_a_post_migration_broker_mutation(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    _prepare(manager)
    manager.mark_post_migration_broker_mutation()

    with pytest.raises(MigrationRollbackForbiddenError, match="reconcile forward"):
        manager.rollback_direct(
            device_id="pc-main", lease_token="token-8", lease_epoch=8
        )


def test_startup_refuses_schema_mismatch_with_another_live_device(tmp_path):
    engine = _engine(tmp_path)
    claimed = claim_main_device(
        engine, LocalDeviceRole(device_id="old-pc", hostname="old", is_main=True)
    )
    assert claimed.success
    save_runtime_device_state(
        engine,
        device_id="old-pc",
        hostname="old",
        state=RuntimeDeviceState.ACTIVE,
        schema_version=CURRENT_EXECUTION_SCHEMA_VERSION - 1,
    )
    runtime_status.record_runtime_heartbeat(engine, hostname="old", pid=1)
    stale_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=5
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runtime_device_state SET updated_at = :stale_at "
                "WHERE device_id = 'old-pc'"
            ),
            {"stale_at": stale_at},
        )

    with pytest.raises(RuntimeError, match="current lease holder"):
        require_compatible_runtime_schema(
            engine,
            device_id="new-pc",
            schema_version=CURRENT_EXECUTION_SCHEMA_VERSION,
        )


def test_fresh_old_schema_standby_blocks_current_main_runtime(tmp_path):
    engine = _engine(tmp_path)
    main_claim = claim_main_device(
        engine, LocalDeviceRole(device_id="main-pc", hostname="main", is_main=True)
    )
    assert main_claim.success
    save_runtime_device_state(
        engine,
        device_id="main-pc",
        hostname="main",
        state=RuntimeDeviceState.ACTIVE,
        schema_version=CURRENT_EXECUTION_SCHEMA_VERSION,
    )
    save_runtime_device_state(
        engine,
        device_id="old-standby",
        hostname="standby",
        state=RuntimeDeviceState.STANDBY_READY,
        schema_version=CURRENT_EXECUTION_SCHEMA_VERSION - 1,
    )

    with pytest.raises(RuntimeError, match="fresh running peer"):
        require_compatible_runtime_schema(engine, device_id="main-pc")


def test_stale_old_schema_standby_does_not_block_current_main_runtime(tmp_path):
    engine = _engine(tmp_path)
    main_claim = claim_main_device(
        engine, LocalDeviceRole(device_id="main-pc", hostname="main", is_main=True)
    )
    assert main_claim.success
    save_runtime_device_state(
        engine,
        device_id="old-standby",
        hostname="standby",
        state=RuntimeDeviceState.STANDBY_READY,
        schema_version=CURRENT_EXECUTION_SCHEMA_VERSION - 1,
    )

    require_compatible_runtime_schema(
        engine,
        device_id="main-pc",
        now=datetime.now(timezone.utc) + timedelta(seconds=61),
    )


def test_stale_old_schema_owner_allows_real_generation_fenced_handoff(tmp_path):
    engine = _engine(tmp_path)
    old_role = LocalDeviceRole(device_id="old-pc", hostname="OLD", is_main=True)
    new_role = LocalDeviceRole(device_id="new-pc", hostname="NEW", is_main=False)
    old_claim = claim_main_device(engine, old_role)
    assert old_claim.success
    save_runtime_device_state(
        engine,
        device_id=old_role.device_id,
        hostname=old_role.hostname,
        state=RuntimeDeviceState.ACTIVE,
        schema_version=CURRENT_EXECUTION_SCHEMA_VERSION - 1,
    )
    runtime_status.record_runtime_heartbeat(
        engine, hostname=old_role.hostname, pid=1
    )
    stale_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=5
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runtime_device_state SET updated_at = :stale_at "
                "WHERE device_id = :device_id"
            ),
            {"stale_at": stale_at, "device_id": old_role.device_id},
        )
        conn.execute(
            text(
                "UPDATE app_runtime_status SET heartbeat_at = :stale_at "
                "WHERE hostname = :hostname AND process_name = 'main.py'"
            ),
            {"stale_at": stale_at, "hostname": old_role.hostname.lower()},
        )
    standby = save_runtime_device_state(
        engine,
        device_id=new_role.device_id,
        hostname=new_role.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
        schema_version=CURRENT_EXECUTION_SCHEMA_VERSION,
    )

    # Startup is allowed far enough to publish readiness because the old
    # mismatched owner is genuinely stale; authority has not been bypassed.
    require_compatible_runtime_schema(engine, device_id=new_role.device_id)
    takeover = claim_main_device_if_stale(
        engine,
        new_role,
        expected_owner_device_id=old_role.device_id,
        heartbeat_cutoff_seconds=60,
        expected_standby_generation=standby.readiness_generation,
        standby_max_age_seconds=60,
    )

    assert takeover.success
    assert takeover.main_device.device_id == new_role.device_id
    assert takeover.main_device.lease_epoch > old_claim.main_device.lease_epoch
    require_compatible_runtime_schema(engine, device_id=new_role.device_id)


def test_stopped_device_on_an_old_schema_does_not_block_startup(tmp_path):
    engine = _engine(tmp_path)
    save_runtime_device_state(
        engine,
        device_id="old-pc",
        hostname="old",
        state=RuntimeDeviceState.STOPPED,
        schema_version=CURRENT_EXECUTION_SCHEMA_VERSION - 1,
    )

    require_compatible_runtime_schema(engine, device_id="new-pc")


def test_cutover_rechecks_live_lease_before_migration_mutation(tmp_path, monkeypatch):
    protocol = FakeExecutionLeaseProtocol(
        current=ExecutionLease("pc-main", "token-8", 8)
    )

    def supersede_after_backup(point):
        if point == "after_backup":
            protocol.grant(ExecutionLease("pc-other", "token-9", 9))

    manager = _manager(
        tmp_path,
        monkeypatch,
        lease_protocol=protocol,
        fault_hook=supersede_after_backup,
    )

    with pytest.raises(MigrationCutoverOwnershipError):
        _prepare(manager)


def test_completed_migration_is_reusable_by_a_new_verified_main_lease(
    tmp_path, monkeypatch
):
    original = ExecutionLease("pc-main", "token-8", 8)
    successor = ExecutionLease("pc-main", "token-9", 9)
    protocol = FakeExecutionLeaseProtocol(current=original)
    manager = _manager(tmp_path, monkeypatch, lease_protocol=protocol)
    awaiting = _prepare(manager)
    assert awaiting.phase == MigrationPhase.AWAITING_RECONCILIATION
    ready = manager.mark_reconciliation_complete()
    assert ready.phase == MigrationPhase.READY

    protocol.grant(successor)
    recovered = manager.prepare_cutover(
        device_id=successor.device_id,
        lease_token=successor.lease_token,
        lease_epoch=successor.lease_epoch,
    )

    assert recovered.phase == MigrationPhase.READY
    assert recovered.reconciliation_complete is True
    assert recovered.cutover_lease_token == original.lease_token
    assert recovered.cutover_lease_epoch == original.lease_epoch


def test_incomplete_migration_remains_fenced_to_its_exact_cutover_lease(
    tmp_path, monkeypatch
):
    original = ExecutionLease("pc-main", "token-8", 8)
    successor = ExecutionLease("pc-main", "token-9", 9)
    protocol = FakeExecutionLeaseProtocol(current=original)
    manager = _manager(tmp_path, monkeypatch, lease_protocol=protocol)
    awaiting = _prepare(manager)
    assert awaiting.phase == MigrationPhase.AWAITING_RECONCILIATION

    protocol.grant(successor)
    with pytest.raises(
        MigrationCutoverOwnershipError,
        match="different exact lease",
    ):
        manager.prepare_cutover(
            device_id=successor.device_id,
            lease_token=successor.lease_token,
            lease_epoch=successor.lease_epoch,
        )


def test_stale_lease_cannot_directly_restore_cutover_backup(tmp_path, monkeypatch):
    protocol = FakeExecutionLeaseProtocol(
        current=ExecutionLease("pc-main", "token-8", 8)
    )
    manager = _manager(tmp_path, monkeypatch, lease_protocol=protocol)
    _prepare(manager)
    protocol.grant(ExecutionLease("pc-main", "token-9", 9))

    with pytest.raises(MigrationCutoverOwnershipError):
        manager.rollback_direct(
            device_id="pc-main", lease_token="token-8", lease_epoch=8
        )


def test_post_mutation_recovery_reconciles_fresh_snapshot_then_transforms(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    _prepare(manager)
    manager.mark_post_migration_broker_mutation()
    calls = []

    state = manager.reconcile_forward_to_compatibility(
        device_id="pc-main",
        lease_token="token-8",
        lease_epoch=8,
        broker_snapshot_provider=lambda: calls.append("snapshot") or {"fresh": True},
        full_reconciliation=lambda snapshot: calls.append("reconcile") or snapshot["fresh"],
        compatibility_transform=lambda snapshot: calls.append("transform"),
    )

    assert calls == ["snapshot", "reconcile", "transform"]
    assert state.phase == MigrationPhase.COMPATIBILITY_READY
    assert state.reconciliation_complete is True
