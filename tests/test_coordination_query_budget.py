"""Regression limits for routine shared-coordination reads.

TiDB bills transaction work.  Read-only polling helpers must therefore use a
connection scope, not a committing transaction scope.  The safety-critical
write paths keep their explicit transactions in their own repository tests.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event, inspect

from src.core import execution_config
from src.core.runtime_readiness import RuntimeDeviceState
from src.services import discovered_external_order_repository as external_repo
from src.services import execution_command_repository as command_repo
from src.services import execution_order_repository as order_repo
from src.services.coordination_schema import ensure_coordination_schema
from src.services.operator_commands import claim_next_operator_command
from src.services.state_sync import LocalDeviceRole
from src.services.runtime_device_state_repository import (
    refresh_runtime_device_state,
    save_runtime_device_state,
)


def test_cloud_coordination_poll_floors_preserve_monthly_budget():
    assert execution_config.COORDINATION_ACTIVE_CARD_POLL_SECONDS >= 180.0
    assert execution_config.COORDINATION_STANDBY_CARD_POLL_SECONDS >= 300.0
    assert execution_config.COORDINATION_DATABASE_PROBE_SECONDS >= 180.0
    assert execution_config.COORDINATION_LEASE_POLL_SECONDS >= 20.0
    assert execution_config.COORDINATION_DEVICE_HEARTBEAT_SECONDS >= 45.0
    assert execution_config.COORDINATION_OWNERSHIP_PROOF_SECONDS >= 30.0
    assert execution_config.COORDINATION_ALERT_POLL_SECONDS >= 90.0
    assert execution_config.COORDINATION_OPERATOR_COMMAND_POLL_SECONDS >= 20.0
    assert execution_config.COORDINATION_OFF_HOURS_POLL_SECONDS >= 300.0
    assert execution_config.COORDINATION_STATE_SYNC_SECONDS >= 180.0
    assert execution_config.COORDINATION_BOARD_PROJECTION_SECONDS >= 180.0
    assert execution_config.COORDINATION_RECONCILIATION_CACHE_SECONDS >= 900.0
    assert execution_config.EXTERNAL_WATCHDOG_HEARTBEAT_SECONDS <= 5.0
    assert execution_config.EXTERNAL_WATCHDOG_TIDB_AUDIT_SECONDS >= 3600.0
    assert execution_config.PENDING_ORDER_RECONCILIATION_SECONDS >= 2
    assert execution_config.UNKNOWN_ORDER_RECONCILIATION_SECONDS >= 1
    assert execution_config.DURABLE_ORDER_OBSERVATION_SECONDS >= 3600


def test_external_pulse_profile_has_two_x_headroom_below_nine_ru_per_second():
    """Lock the documented two-device scheduled-request envelope."""

    month_seconds = 30 * 24 * 60 * 60
    regular_session_seconds = 22 * 6.5 * 60 * 60
    off_hours_seconds = month_seconds - regular_session_seconds

    scheduled_requests = sum(
        (
            # Two fallback write probes plus their explicit COMMITs.
            4 * month_seconds
            / execution_config.COORDINATION_DATABASE_PROBE_SECONDS,
            # Two devices: revision read plus readiness UPDATE.
            4 * month_seconds
            / execution_config.COORDINATION_DEVICE_HEARTBEAT_SECONDS,
            # app_runtime_status is lifecycle-only while the guarded runtime
            # publishes the canonical runtime_device_state heartbeat.
            month_seconds / execution_config.COORDINATION_LEASE_POLL_SECONDS,
            month_seconds
            / execution_config.COORDINATION_ACTIVE_CARD_POLL_SECONDS,
            month_seconds
            / execution_config.COORDINATION_STANDBY_CARD_POLL_SECONDS,
            regular_session_seconds
            / execution_config.COORDINATION_OPERATOR_COMMAND_POLL_SECONDS,
            off_hours_seconds
            / execution_config.COORDINATION_OFF_HOURS_POLL_SECONDS,
            month_seconds
            / execution_config.COORDINATION_OWNERSHIP_PROOF_SECONDS,
            # Two alert consumers plus hourly TiDB evidence for the fast
            # external-webhook pulses.
            2 * month_seconds
            / execution_config.COORDINATION_ALERT_POLL_SECONDS,
            2
            * month_seconds
            / execution_config.EXTERNAL_WATCHDOG_TIDB_AUDIT_SECONDS,
            2 * month_seconds
            / execution_config.COORDINATION_BOARD_PROJECTION_SECONDS,
            # Current display synchronization has five compact request slots.
            5 * month_seconds
            / execution_config.COORDINATION_STATE_SYNC_SECONDS,
            # Broker truth remains minutely; unchanged relational comparison
            # state is served from the process cache between TiDB refreshes.
            6
            * month_seconds
            / execution_config.COORDINATION_RECONCILIATION_CACHE_SECONDS,
        )
    )
    conservative_ru_per_second = (
        scheduled_requests * 1.25 * 8.0 / month_seconds
    )

    assert scheduled_requests <= 750_000
    assert conservative_ru_per_second <= 2.9


def test_idle_operator_command_poll_uses_covering_index_without_commit(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operator_command_poll.db'}", future=True
    )
    ensure_coordination_schema(engine)

    indexes = {
        item["name"]: item
        for item in inspect(engine).get_indexes("operator_commands")
    }
    assert indexes["ix_operator_commands_status_created"]["column_names"] == [
        "status",
        "created_at",
        "command_id",
    ]

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

    assert (
        claim_next_operator_command(
            engine,
            LocalDeviceRole(device_id="executor", hostname="PC", is_main=True),
        )
        is None
    )
    assert sum("FROM OPERATOR_COMMANDS" in statement for statement in statements) == 1
    assert commits == []


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


def test_unchanged_device_heartbeat_rewrites_only_freshness_timestamp(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'coordination_lean_heartbeat.db'}", future=True
    )
    save_runtime_device_state(
        engine,
        device_id="laptop-id",
        hostname="LAPTOP",
        state=RuntimeDeviceState.STANDBY_READY,
        details={"device_kind": "Laptop"},
    )
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(
            " ".join(statement.upper().split())
        ),
    )

    assert refresh_runtime_device_state(
        engine,
        device_id="laptop-id",
        hostname="LAPTOP",
        state=RuntimeDeviceState.STANDBY_READY,
        details={"device_kind": "Laptop"},
        heartbeat_only=True,
    )

    heartbeat_update = next(
        statement
        for statement in statements
        if statement.startswith("UPDATE RUNTIME_DEVICE_STATE")
    )
    assert "SET UPDATED_AT" in heartbeat_update
    updated_columns = heartbeat_update.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    assert "HOSTNAME" not in updated_columns
    assert "DETAILS_JSON" not in updated_columns
    assert "SCHEMA_VERSION" not in updated_columns
