"""Regression limits for routine shared-coordination reads.

TiDB bills transaction work.  Read-only polling helpers must therefore use a
connection scope, not a committing transaction scope.  The safety-critical
write paths keep their explicit transactions in their own repository tests.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event

from src.core import execution_config
from src.core.runtime_readiness import RuntimeDeviceState
from src.services import discovered_external_order_repository as external_repo
from src.services import execution_command_repository as command_repo
from src.services import execution_order_repository as order_repo
from src.services.runtime_device_state_repository import (
    refresh_runtime_device_state,
    save_runtime_device_state,
)


def test_cloud_coordination_poll_floors_preserve_monthly_budget():
    assert execution_config.COORDINATION_DATABASE_PROBE_SECONDS >= 60.0
    assert execution_config.COORDINATION_DEVICE_HEARTBEAT_SECONDS >= 30.0
    assert execution_config.DURABLE_ORDER_OBSERVATION_SECONDS >= 3600


def test_routine_order_repository_reads_emit_no_commits(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'coordination_reads.db'}", future=True
    )
    order_repo.ensure_execution_orders_table(engine)
    external_repo.ensure_discovered_external_orders_table(engine)
    command_repo.ensure_execution_commands_table(engine)

    commits = []
    event.listen(engine, "commit", lambda _connection: commits.append(True))

    assert order_repo.fetch_execution_order(engine, "missing") is None
    assert order_repo.list_execution_orders_for_card(
        engine, environment="PROD", account_no="1", symbol="AAPL"
    ) == []
    assert order_repo.list_execution_orders_for_account(
        engine, environment="PROD", account_no="1"
    ) == []
    assert order_repo.list_execution_orders(engine, environment="PROD") == []

    assert external_repo.fetch_discovered_external_order(engine, "missing") is None
    assert external_repo.list_discovered_external_orders_for_account(
        engine, environment="PROD", account_no="1"
    ) == []
    assert external_repo.list_discovered_external_orders(
        engine, environment="PROD"
    ) == []

    assert command_repo.get_command_by_idempotency_key(engine, "missing") is None
    assert command_repo.list_execution_commands_for_account(
        engine, environment="PROD", account_no="1"
    ) == []

    assert commits == []


def test_unchanged_device_heartbeat_is_one_autocommitted_update(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'coordination_heartbeat.db'}", future=True
    )
    save_runtime_device_state(
        engine,
        device_id="laptop-id",
        hostname="LAPTOP",
        state=RuntimeDeviceState.STANDBY_READY,
        details={"device_kind": "Laptop"},
    )
    statements = []
    commits = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(
            " ".join(statement.upper().split())
        ),
    )
    event.listen(engine, "commit", lambda _connection: commits.append(True))

    refreshed = refresh_runtime_device_state(
        engine,
        device_id="laptop-id",
        hostname="LAPTOP",
        state=RuntimeDeviceState.STANDBY_READY,
        details={"device_kind": "Laptop"},
    )

    assert refreshed is True
    assert sum(
        statement.startswith("UPDATE RUNTIME_DEVICE_STATE")
        for statement in statements
    ) == 1
    assert not any(statement.startswith("SELECT") for statement in statements)
    assert commits == []
