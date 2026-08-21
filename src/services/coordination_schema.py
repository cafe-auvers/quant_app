"""Provision only the compact tables required for cross-device execution."""
from __future__ import annotations

from sqlalchemy.engine import Engine


def ensure_coordination_schema(engine: Engine) -> None:
    """Idempotently prepare an empty shared coordination database.

    No historical price, scanner, chart, or indicator tables are created.
    """

    from src.services.capital_reservation_repository import (
        ensure_capital_reservations_table,
    )
    from src.services.discovered_external_order_repository import (
        ensure_discovered_external_orders_table,
    )
    from src.services.emergency_journal import ensure_emergency_reconciliation_table
    from src.services.execution_command_repository import ensure_execution_commands_table
    from src.services.execution_order_repository import ensure_execution_orders_table
    from src.services.execution_ownership_repository import ensure_execution_ownership_table
    from src.services.external_alerting import ensure_external_alert_tables
    from src.services.operator_commands import ensure_operator_commands_table
    from src.services.runtime_device_state_repository import (
        ensure_runtime_device_state_table,
    )
    from src.services.runtime_status import ensure_runtime_status_table
    from src.services.schema_migration import ensure_schema_migration_table
    from src.services.state_sync import ensure_state_sync_tables
    from src.services.trade_card_repository import ensure_trade_cards_table

    ensure_state_sync_tables(engine)
    ensure_trade_cards_table(engine)
    ensure_execution_ownership_table(engine)
    ensure_operator_commands_table(engine)
    ensure_runtime_device_state_table(engine)
    ensure_runtime_status_table(engine)
    ensure_execution_commands_table(engine)
    ensure_execution_orders_table(engine)
    ensure_capital_reservations_table(engine)
    ensure_discovered_external_orders_table(engine)
    ensure_emergency_reconciliation_table(engine)
    ensure_external_alert_tables(engine)
    ensure_schema_migration_table(engine)
