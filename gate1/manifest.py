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
            "tests/test_gate1_contract.py",
            "tests/test_gate1_reporting.py",
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


# Contract-critical identities cannot disappear, rename, or skip while the
# broad file selectors continue to pass.  Totals remain informational; these
# frozen nodes are the stable Gate-1 certification inventory.
REQUIRED_SCENARIO_IDS: frozenset[str] = frozenset(
    {
        # PR8 cross-workstream capstones.
        "tests.test_gate1_capstone::test_gate1_open_position_handoff_reconciles_before_transfer_and_rejects_old_device",
        "tests.test_gate1_capstone::test_gate1_ambiguous_submission_restart_reconciles_without_resubmitting",
        "tests.test_gate1_capstone::test_gate1_database_outage_open_exposure_replays_emergency_journal_on_restart",
        "tests.test_gate1_capstone::test_gate1_stop_breach_survives_device_handoff_and_submits_once",
        "tests.test_gate1_capstone::test_gate1_external_order_fence_requires_exact_adopted_cancel_before_emergency_exit",
        "tests.test_gate1_capstone::test_gate1_migration_restart_runs_broker_reconciliation_before_entries_ready",
        "tests.test_gate1_capstone::test_gate1_rate_limit_pressure_prioritizes_real_emergency_liquidation",
        # The complete signed L3 parity matrix.
        "tests.test_ws13_legacy_kanban_parity::test_l3_add_to_buy_today_produces_the_same_entry_monitoring_command",
        "tests.test_ws13_legacy_kanban_parity::test_l3_pending_entry_cancel_produces_the_same_cancel_intent",
        "tests.test_ws13_legacy_kanban_parity::test_l3_partial_sell_produces_equal_submission_result_and_command",
        "tests.test_ws13_legacy_kanban_parity::test_l3_sell_all_produces_equal_submission_result_and_command",
        "tests.test_ws13_legacy_kanban_parity::test_l3_stop_change_produces_the_same_protective_domain_state",
        "tests.test_ws13_legacy_kanban_parity::test_l3_premarket_sell_all_produces_the_same_next_open_intent",
        "tests.test_ws13_legacy_kanban_parity::test_l3_eod_unfilled_entry_produces_the_same_authoritative_transition",
        "tests.test_ws13_legacy_kanban_parity::test_l3_partial_fill_produces_the_same_reconciled_position_and_order_tracking",
        # F4 real-SUT exploration plus the frozen transition tables.
        "tests.test_gate1_model_state_exploration::test_f4_exhaustively_explores_every_execution_order_transition_path",
        "tests.test_gate1_model_state_exploration::test_f4_exhaustively_rejects_every_undocumented_order_transition",
        "tests.test_gate1_model_state_exploration::test_f4_exhaustively_checks_the_complete_kanban_transition_table",
        "tests.test_gate1_model_state_exploration::test_f4_seeded_adversarial_actions_drive_real_sut_and_converge",
        # Named F1/F2/F3 failure-boundary requirements.
        "tests.test_execution_command_gateway::test_replaying_the_same_stable_client_order_id_on_a_fresh_gateway_makes_zero_additional_broker_calls",
        "tests.test_execution_command_gateway::test_a_lease_epoch_advance_between_the_initial_check_and_the_broker_call_blocks_submission",
        "tests.test_execution_gateway_db_outage::test_local_journal_failure_prevents_destructive_call",
        "tests.test_account_reconciliation::test_a_broker_order_used_as_an_a4a_candidate_is_not_also_created_as_an_external_order",
        "tests.test_kis_websocket::test_parse_realtime_frame_preserves_count_and_payload_identity",
        "tests.test_kis_websocket::test_reconnect_resubscribes_every_desired_subscription",
        "tests.test_kis_ws_auth::test_unverified_protocol_blocks_before_network_call",
        "tests.test_kis_realtime_market_data::test_recent_event_with_backed_up_queue_is_not_execution_fresh",
        "tests.test_runtime_readiness::test_runtime_device_state_requires_explicit_fresh_handoff_confirmation",
        "tests.test_runtime_readiness::test_readiness_loss_demotes_and_recovery_mints_a_new_generation",
        "tests.test_main_window_handoff::test_post_claim_clean_broker_result_stays_blocked_if_state_publish_fails",
        "tests.test_handoff_reconciliation::test_reconciliation_blocks_entire_account_for_unmatched_order",
    }
)


REQUIRED_GROUP_MINIMUMS: dict[str, int] = {
    "ARCHITECTURE": 1,
    "STATE_MACHINES": 1,
    "F1_CRASH_FAULT_INJECTION": 4,
    "F2_WEBSOCKET_PROTOCOL": 4,
    "F3_MULTI_DEVICE_HANDOFF": 4,
    "F4_MODEL_EXPLORATION": 4,
    "L3_KANBAN_LEGACY_PARITY": 8,
    "PR8_CROSS_WORKSTREAM": 7,
}


def unique_selectors() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            selector
            for group in SCENARIO_GROUPS
            for selector in group.selectors
        )
    )
