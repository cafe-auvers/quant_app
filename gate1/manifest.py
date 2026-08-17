"""The explicit deterministic Gate-1 pytest manifest."""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_MODEL_SEED = 20260817


@dataclass(frozen=True)
class ScenarioGroup:
    group_id: str
    selectors: tuple[str, ...]


SCENARIO_GROUPS: tuple[ScenarioGroup, ...] = (
    ScenarioGroup(
        "ARCHITECTURE",
        (
            "tests/test_architecture_broker_boundary.py",
            "tests/test_p2_architecture.py",
            "tests/test_refactor_boundaries.py",
        ),
    ),
    ScenarioGroup(
        "STATE_MACHINES",
        (
            "tests/test_execution_order_record.py",
            "tests/test_order_lifecycle.py",
            "tests/test_kanban_transitions.py",
            "tests/test_trade_card_state.py",
        ),
    ),
    ScenarioGroup(
        "F1_CRASH_FAULT_INJECTION",
        (
            "tests/test_execution_command_gateway.py",
            "tests/test_execution_gateway_db_outage.py",
            "tests/test_account_reconciliation.py",
            "tests/test_emergency_journal.py",
            "tests/test_schema_migration.py",
        ),
    ),
    ScenarioGroup(
        "F2_WEBSOCKET_PROTOCOL",
        (
            "tests/test_kis_websocket.py",
            "tests/test_kis_ws_auth.py",
            "tests/test_kis_realtime_market_data.py",
            "tests/test_realtime_market_data.py",
        ),
    ),
    ScenarioGroup(
        "F3_MULTI_DEVICE_HANDOFF",
        (
            "tests/test_runtime_readiness.py",
            "tests/test_main_window_handoff.py",
            "tests/test_handoff_reconciliation.py",
        ),
    ),
    ScenarioGroup(
        "F4_MODEL_EXPLORATION",
        ("tests/test_gate1_model_state_exploration.py",),
    ),
    ScenarioGroup(
        "L3_KANBAN_LEGACY_PARITY",
        ("tests/test_ws13_legacy_kanban_parity.py",),
    ),
    ScenarioGroup(
        "PR8_CROSS_WORKSTREAM",
        ("tests/test_gate1_capstone.py",),
    ),
)


def unique_selectors() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            selector
            for group in SCENARIO_GROUPS
            for selector in group.selectors
        )
    )
