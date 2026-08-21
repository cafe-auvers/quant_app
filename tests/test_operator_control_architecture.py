from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from src.core.board_workflow import ActivateForToday, RequestPartialSell
from src.core.runtime_readiness import RuntimeDeviceState
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import trade_card_repository as card_repo
from src.services.control_ownership import (
    check_executor_readiness,
    switch_execution_owner,
)
from src.services.operator_command_service import (
    enqueue_board_operator_command,
    process_next_board_operator_command,
)
from src.services.operator_commands import (
    ExecutionOwnerMismatchError,
    OperatorCommandStatus,
    OperatorCommandType,
    OperatorControlNotOwnedError,
    claim_next_operator_command,
    finish_operator_command,
    get_operator_command,
    list_operator_commands,
    start_operator_command,
    submit_operator_command,
)
from src.services import operator_commands
from src.services import runtime_device_state_repository
from src.services.runtime_device_state_repository import save_runtime_device_state
from src.services.runtime_status import record_runtime_heartbeat
from src.services.state_sync import (
    LocalDeviceRole,
    claim_main_device,
    get_main_device,
    get_operator_control,
    publish_planning_snapshot,
    set_live_trading_control,
    set_operator_control,
)
from src.ui.main_window import MainWindow
from src.utils.device_identity import (
    DEVICE_KIND_LAPTOP,
    DEVICE_KIND_PC,
    detect_local_device_kind,
)


@pytest.fixture
def engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'operator-control.db'}",
        future=True,
        poolclass=NullPool,
    )


@pytest.fixture(autouse=True)
def isolate_card_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(
        card_repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "trade_cards.json"
    )


@pytest.fixture
def roles():
    return (
        LocalDeviceRole("pc-id", "TRADING-PC", False),
        LocalDeviceRole("laptop-id", "TRADING-LAPTOP", False),
    )


def _details(**overrides):
    values = {
        "main_py_alive": True,
        "db_connected": True,
        "kis_ready": True,
        "account_environment_ready": True,
        "market_data_ready": True,
        "command_consumer_ready": True,
        "order_reconciliation_ready": True,
        "latest_watchlist_revision": 0,
        "latest_buylist_revision": 0,
        "latest_trade_plans_revision": 0,
        "latest_execution_queue_revision": 0,
        "state_revisions_current": True,
        "no_stale_local_state": True,
        "power_state": "AWAKE",
        "sleep_blocker_active": True,
        "executor_ready": True,
        "executor_not_ready_reason": "",
    }
    values.update(overrides)
    return values


def _pending(engine, requester, key="command-1"):
    return submit_operator_command(
        engine,
        requester,
        OperatorCommandType.SELL_ALL,
        symbol="AAPL",
        payload={"reason": "operator"},
        idempotency_key=key,
    )


def test_local_device_kind_uses_hardware_instead_of_desktop_hostname():
    assert (
        detect_local_device_kind("DESKTOP-T5V57VV", has_system_battery=True)
        == DEVICE_KIND_LAPTOP
    )
    assert (
        detect_local_device_kind("DESKTOP-E42GSKJ", has_system_battery=False)
        == DEVICE_KIND_PC
    )


def test_laptop_owner_target_uses_published_device_kind():
    laptop = SimpleNamespace(
        device_id="laptop-id",
        hostname="DESKTOP-T5V57VV",
        state=RuntimeDeviceState.STANDBY_READY,
        updated_at=datetime.now(timezone.utc),
        details={"device_kind": "Laptop", "executor_ready": True},
    )
    window = MainWindow.__new__(MainWindow)
    window._runtime_device_records = (laptop,)
    window.state_sync_role = LocalDeviceRole("pc-id", "DESKTOP-E42GSKJ", False)

    target = window._control_target_role("Laptop")

    assert target is not None
    assert target.device_id == laptop.device_id
    assert target.hostname == laptop.hostname


def test_stopped_historical_identity_is_not_an_owner_target():
    stale_pc = SimpleNamespace(
        device_id="old-pc-id",
        hostname="DESKTOP-OLD",
        state=RuntimeDeviceState.STOPPED,
        updated_at=datetime.now(timezone.utc),
        details={"device_kind": "PC"},
    )
    window = MainWindow.__new__(MainWindow)
    window._runtime_device_records = (stale_pc,)
    window.state_sync_role = LocalDeviceRole(
        "laptop-id", "DESKTOP-T5V57VV", False
    )

    assert window._control_target_role("PC") is None


def test_new_text_columns_have_no_mysql_server_defaults():
    runtime_ddl = str(
        CreateTable(runtime_device_state_repository._table(MetaData())).compile(
            dialect=mysql.dialect()
        )
    )
    command_ddl = str(
        CreateTable(operator_commands._table(MetaData())).compile(
            dialect=mysql.dialect()
        )
    )
    details_line = next(
        line for line in runtime_ddl.splitlines() if "details_json" in line
    )
    error_line = next(
        line for line in command_ddl.splitlines() if "error_message" in line
    )

    assert "DEFAULT" not in details_line.upper()
    assert "DEFAULT" not in error_line.upper()


def test_runtime_details_migration_accepts_existing_rows(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-runtime.db'}",
        future=True,
        poolclass=NullPool,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE runtime_device_state ("
                "device_id VARCHAR(64) PRIMARY KEY, "
                "hostname VARCHAR(255) NOT NULL, "
                "state VARCHAR(32) NOT NULL, "
                "handoff_confirmed BOOLEAN NOT NULL DEFAULT 0, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO runtime_device_state "
                "(device_id, hostname, state, handoff_confirmed, updated_at) "
                "VALUES ('legacy', 'OLD-PC', 'STANDBY', 0, CURRENT_TIMESTAMP)"
            )
        )

    runtime_device_state_repository.ensure_runtime_device_state_table(engine)

    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("runtime_device_state")
    }
    with engine.connect() as conn:
        details = conn.execute(
            text(
                "SELECT details_json FROM runtime_device_state "
                "WHERE device_id = 'legacy'"
            )
        ).scalar_one()

    assert "details_json" in columns
    assert details is None


def test_laptop_cannot_submit_when_pc_owns_operator_control(engine, roles):
    pc, laptop = roles
    assert set_operator_control(engine, laptop, pc).success

    with pytest.raises(OperatorControlNotOwnedError):
        _pending(engine, laptop)


def test_laptop_can_submit_when_laptop_owns_operator_control(engine, roles):
    _pc, laptop = roles
    assert set_operator_control(engine, laptop, laptop).success

    inserted = _pending(engine, laptop)

    assert inserted.created is True
    assert inserted.command.status == OperatorCommandStatus.PENDING


def test_locked_operator_control_rejects_without_recording_a_command(engine, roles):
    pc, laptop = roles
    assert set_operator_control(engine, pc, None).success

    with pytest.raises(OperatorControlNotOwnedError, match="Locked"):
        _pending(engine, laptop)

    assert list_operator_commands(engine) == []


def test_command_can_be_retried_after_operator_control_is_assigned(engine, roles):
    pc, laptop = roles
    assert set_operator_control(engine, pc, None).success
    with pytest.raises(OperatorControlNotOwnedError):
        _pending(engine, laptop)

    assert set_operator_control(engine, pc, laptop).success
    inserted = _pending(engine, laptop)

    assert inserted.created is True
    assert inserted.command.status == OperatorCommandStatus.PENDING


def test_non_execution_owner_cannot_claim_pending_command(engine, roles):
    pc, laptop = roles
    set_operator_control(engine, laptop, laptop)
    claim_main_device(engine, pc)
    _pending(engine, laptop)

    with pytest.raises(ExecutionOwnerMismatchError):
        claim_next_operator_command(engine, laptop)


def test_execution_owner_claims_and_completes_command_exactly_once(engine, roles):
    pc, laptop = roles
    set_operator_control(engine, laptop, laptop)
    claim_main_device(engine, pc)
    first = _pending(engine, laptop)
    duplicate = _pending(engine, laptop)

    assert duplicate.created is False
    assert duplicate.command.command_id == first.command.command_id
    accepted = claim_next_operator_command(engine, pc)
    assert accepted.status == OperatorCommandStatus.ACCEPTED
    assert claim_next_operator_command(engine, pc) is None
    executing = start_operator_command(engine, pc, accepted.command_id)
    completed = finish_operator_command(
        engine, pc, executing.command_id, OperatorCommandStatus.COMPLETED
    )

    assert completed.status == OperatorCommandStatus.COMPLETED
    assert claim_next_operator_command(engine, pc) is None


def test_accepted_command_survives_operator_owner_switch(engine, roles):
    pc, laptop = roles
    set_operator_control(engine, laptop, laptop)
    claim_main_device(engine, pc)
    _pending(engine, laptop)
    accepted = claim_next_operator_command(engine, pc)

    switched = set_operator_control(engine, pc, pc)
    executing = start_operator_command(engine, pc, accepted.command_id)
    completed = finish_operator_command(
        engine, pc, executing.command_id, OperatorCommandStatus.COMPLETED
    )

    assert switched.success
    assert completed.status == OperatorCommandStatus.COMPLETED


def test_operator_switch_does_not_change_execution_owner(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)

    changed = set_operator_control(engine, pc, laptop)

    assert changed.success
    assert get_main_device(engine).main_device.device_id == pc.device_id
    assert get_operator_control(engine).control.device_id == laptop.device_id


def test_execution_switch_fails_when_laptop_main_heartbeat_is_stale(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    save_runtime_device_state(
        engine,
        device_id=laptop.device_id,
        hostname=laptop.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
        details=_details(),
    )

    result = switch_execution_owner(
        engine, initiated_by=pc, target_device_id=laptop.device_id
    )

    assert result.success is False
    assert "heartbeat" in result.error.lower()
    assert get_main_device(engine).main_device.device_id == pc.device_id


def test_execution_switch_fails_when_kis_session_not_ready(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    record_runtime_heartbeat(engine, hostname=laptop.hostname, pid=12)
    save_runtime_device_state(
        engine,
        device_id=laptop.device_id,
        hostname=laptop.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
        details=_details(kis_ready=False, executor_ready=False),
    )

    readiness = check_executor_readiness(
        engine, target_device_id=laptop.device_id
    )

    assert readiness.ready is False
    assert any("KIS session" in reason for reason in readiness.reasons)


def test_execution_switch_to_ready_pc_succeeds(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, laptop)
    record_runtime_heartbeat(engine, hostname=pc.hostname, pid=13)
    save_runtime_device_state(
        engine,
        device_id=pc.device_id,
        hostname=pc.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
        details=_details(),
    )

    result = switch_execution_owner(
        engine, initiated_by=laptop, target_device_id=pc.device_id
    )

    assert result.success is True
    assert get_main_device(engine).main_device.device_id == pc.device_id


def test_execution_switch_waits_for_an_accepted_operator_command(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    record_runtime_heartbeat(engine, hostname=laptop.hostname, pid=14)
    save_runtime_device_state(
        engine,
        device_id=laptop.device_id,
        hostname=laptop.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
        details=_details(),
    )
    _pending(engine, laptop)
    accepted = claim_next_operator_command(engine, pc)

    result = switch_execution_owner(
        engine, initiated_by=pc, target_device_id=laptop.device_id
    )

    assert accepted.status == OperatorCommandStatus.ACCEPTED
    assert result.success is False
    assert "in flight" in result.error
    assert get_main_device(engine).main_device.device_id == pc.device_id


def test_pending_operator_command_follows_a_safe_execution_owner_switch(
    engine, roles
):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    record_runtime_heartbeat(engine, hostname=laptop.hostname, pid=15)
    save_runtime_device_state(
        engine,
        device_id=laptop.device_id,
        hostname=laptop.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
        details=_details(),
    )
    pending = _pending(engine, laptop)

    result = switch_execution_owner(
        engine, initiated_by=pc, target_device_id=laptop.device_id
    )
    accepted = claim_next_operator_command(engine, laptop)

    assert result.success is True
    assert accepted.command_id == pending.command.command_id
    assert accepted.executor_device_id == laptop.device_id


def test_market_open_full_publish_is_blocked_for_operator_owner(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    payloads = {
        "watchlist": {"items": []},
        "buylist": {"items": []},
        "trade_plans": {"plans": []},
        "execution_queue": {"items": []},
    }

    result = publish_planning_snapshot(
        engine,
        laptop,
        payloads,
        expected_revisions={key: 0 for key in payloads},
        market_is_open=True,
    )

    assert result.success is False
    assert "Market is open" in result.error
    assert get_main_device(engine).main_device.device_id == pc.device_id


def test_premarket_operator_publish_updates_all_revisions_without_owner_change(
    engine, roles
):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    payloads = {
        "watchlist": {"items": [{"symbol": "AAPL"}]},
        "buylist": {"items": [{"symbol": "AAPL"}]},
        "trade_plans": {"plans": []},
        "execution_queue": {"items": [{"symbol": "AAPL"}]},
    }

    result = publish_planning_snapshot(
        engine,
        laptop,
        payloads,
        expected_revisions={key: 0 for key in payloads},
        market_is_open=False,
    )

    assert result.success is True
    assert set(result.revisions.values()) == {1}
    assert get_main_device(engine).main_device.device_id == pc.device_id


def test_partial_sell_rejects_quantity_above_broker_confirmed_holding(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    card = card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.OPEN_POSITION,
            position_runtime_status=PositionRuntimeStatus.OPEN,
            broker_quantity=5,
            orderable_quantity=5,
        ),
    )
    command = RequestPartialSell(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        expected_card_version=card.version,
        quantity=6,
    )
    queued = enqueue_board_operator_command(engine, laptop, command)

    outcome = process_next_board_operator_command(engine, pc)

    assert outcome.command_id == queued.command.command_id
    assert outcome.status == OperatorCommandStatus.REJECTED
    assert "broker-confirmed" in outcome.error_message


def test_add_buy_today_rejects_duplicate_active_symbol(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    card = card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.BUY_TODAY,
        ),
    )
    queued = enqueue_board_operator_command(
        engine,
        laptop,
        ActivateForToday(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
        ),
    )

    outcome = process_next_board_operator_command(engine, pc)

    assert outcome.command_id == queued.command.command_id
    assert outcome.status == OperatorCommandStatus.REJECTED
    assert "already has an active" in outcome.error_message


def test_add_buy_today_is_applied_by_the_execution_owner(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    card = card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.BUYLIST,
        ),
    )
    queued = enqueue_board_operator_command(
        engine,
        laptop,
        ActivateForToday(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
        ),
    )

    outcome = process_next_board_operator_command(engine, pc)
    stored = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")

    assert outcome.command_id == queued.command.command_id
    assert outcome.status == OperatorCommandStatus.COMPLETED
    assert stored.board_status == BoardStatus.BUY_TODAY


def test_partial_fill_lifecycle_is_durable(engine, roles):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)
    _pending(engine, laptop)
    accepted = claim_next_operator_command(engine, pc)
    start_operator_command(engine, pc, accepted.command_id)

    partial = finish_operator_command(
        engine,
        pc,
        accepted.command_id,
        OperatorCommandStatus.PARTIALLY_FILLED,
        broker_order_id="broker-1",
    )

    assert partial.status == OperatorCommandStatus.PARTIALLY_FILLED
    assert partial.broker_order_id == "broker-1"
    assert get_operator_command(engine, accepted.command_id).status == partial.status


def test_real_window_never_falls_back_to_private_execution_lease_store():
    window = MainWindow.__new__(MainWindow)
    window._qt_base_initialized = True
    window.operational_db_engine = object()
    window.pc_db_engine = None
    window._pc_database_ready = False

    assert window._execution_state_engine() is None
    assert window._execution_state_ready() is False


def test_emergency_disable_remains_available_from_either_device_and_is_audited(
    engine, roles
):
    pc, laptop = roles
    claim_main_device(engine, pc)
    set_operator_control(engine, pc, laptop)

    assert set_live_trading_control(engine, laptop, True).success
    assert set_live_trading_control(engine, pc, False).success
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT revision, previous_enabled, enabled, updated_by_device "
                "FROM live_trading_control_audit ORDER BY revision"
            )
        ).fetchall()

    assert [(row.previous_enabled, row.enabled) for row in rows] == [(0, 1), (1, 0)]
    assert rows[-1].updated_by_device == pc.device_id
