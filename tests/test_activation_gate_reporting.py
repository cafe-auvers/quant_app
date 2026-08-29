from __future__ import annotations

from activation_gates.evidence import canonical_report_sha256
from activation_gates.promotion import build_promotion_decision
from gate3.reporting import REQUIRED_DECISION_BRANCHES, build_report as build_gate3
from gate4.reporting import build_report as build_gate4
from gate5.reporting import build_report as build_gate5


COMMIT = "a" * 40


def _review():
    return {
        "status": "APPROVED",
        "author": "operator-a",
        "reviewer": "reviewer-b",
        "reviewed_at": "2026-08-28T20:00:00+00:00",
        "reference": f"sha256:{'9' * 64}",
    }


def _gate2_report():
    return {
        "gate": "GATE_2_LIVE_KIS_READ_ONLY_SOAK",
        "result": "PASSED",
        "commit_sha": COMMIT,
    }


def _passing_gate3():
    upstream = _gate2_report()
    evidence = {
        "commit_sha": COMMIT,
        "gate2_report_sha256": canonical_report_sha256(upstream),
        "final_production_decision_runtime_used": True,
        "real_quotes_used": True,
        "final_boundary_interception_enabled": True,
        "mutation_audit_initialized": True,
        "shadow_store_isolated": True,
        "shadow_store_visibly_labelled": True,
        "shadow_store_append_only_verified": True,
        "production_ledgers_unchanged": True,
        "strategy_rules_sha256": "1" * 64,
        "decision_oracle_sha256": "3" * 64,
        "shadow_store_sha256": "2" * 64,
        "complete_regular_session_count": 1,
        "would_event_counts": {
            "WOULD_SUBMIT": 1,
            "WOULD_CANCEL": 1,
            "WOULD_REPLACE": 1,
            "WOULD_SELL": 1,
        },
        "mutation_candidate_count": 4,
        "broker_mutation_attempt_count": 0,
        "fake_broker_ack_count": 0,
        "fake_fill_count": 0,
        "production_ledger_write_count": 0,
        "unresolved_oracle_difference_count": 0,
        "shadow_event_parse_error_count": 0,
        "shadow_event_label_mismatch_count": 0,
        "shadow_duplicate_event_id_count": 0,
        "observed_decision_branches": sorted(REQUIRED_DECISION_BRANCHES),
        "fence_results": {
            "stale_data": "PASSED",
            "lease_loss": "PASSED",
            "ownership": "PASSED",
            "reconciliation": "PASSED",
            "kill_switch": "PASSED",
        },
        "review": _review(),
    }
    return build_gate3(evidence, upstream_gate2_report=upstream)


def _passing_gate4():
    upstream = _passing_gate3()
    evidence = {
        "commit_sha": COMMIT,
        "gate3_report_sha256": canonical_report_sha256(upstream),
        "execution_capabilities_verified": True,
        "live_execution_mode": "CONTROLLED_LIVE",
        "session_started_disarmed": True,
        "manual_arm_after_active_readiness": True,
        "current_execution_owner_count": 1,
        "current_execution_lease_count": 1,
        "every_entry_has_active_trade_card": True,
        "every_buy_below_notional_cap": True,
        "portfolio_risk_rechecked_atomically": True,
        "automatic_mutation_retry_attempt_count": 0,
        "lifecycle_comparisons_agree": True,
        "lifecycle_mismatch_count": 0,
        "duplicate_or_unowned_mutation_count": 0,
        "unresolved_ambiguous_identity_count": 0,
        "disarm_probe_blocked_next_mutation": True,
        "external_critical_alert_delivered": True,
        "final_reconciliation_matches_broker": True,
        "controlled_live_config_sha256": "4" * 64,
        "risk_limits_sha256": "5" * 64,
        "reviewed_entry_notional_cap": 1_000.0,
        "max_observed_entry_notional": 500.0,
        "approved_symbols": ["AAPL", "MSFT"],
        "observed_entry_symbols": ["AAPL"],
        "supervised_regular_session_dates": ["2026-08-24", "2026-08-25", "2026-08-26"],
        "strategy_entry_terminal_outcome_count": 1,
        "safe_exit_or_protected_position_count": 1,
        "controlled_cancel_lifecycle_count": 1,
        "pilot_exception_evidence": False,
        "review": _review(),
    }
    return build_gate4(evidence, upstream_gate3_report=upstream)


def test_synthetic_complete_gate3_gate4_and_gate5_evidence_passes():
    gate3 = _passing_gate3()
    gate4 = _passing_gate4()
    gate5_evidence = {
        "commit_sha": COMMIT,
        "gate4_report_sha256": canonical_report_sha256(gate4),
        "full_live_scope_and_limits_approved": True,
        "live_execution_mode": "FULL_LIVE",
        "full_live_config_sha256": "6" * 64,
        "risk_limits_sha256": "5" * 64,
        "external_watchdog_evidence_sha256": "7" * 64,
        "drills_within_qualification_window": True,
        "consecutive_full_session_dates": [
            "2026-08-24",
            "2026-08-25",
            "2026-08-26",
            "2026-08-27",
            "2026-08-28",
        ],
        "duplicate_command_count": 0,
        "unresolved_broker_local_discrepancy_count": 0,
        "stale_projected_quantity_count": 0,
        "command_after_lease_loss_count": 0,
        "all_reconnects_restored_desired_subscriptions": True,
        "restart_and_handoff_converged_without_manual_repair": True,
        "every_stop_used_fresh_event_data": True,
        "every_automatic_cancel_had_exact_ownership": True,
        "protective_exits_operational": True,
        "external_critical_alert_delivery_confirmed": True,
        "external_heartbeat_watchdog_running": True,
        "external_heartbeat_watchdog_tested": True,
        "startup_reconciliation_matches_broker": True,
        "final_reconciliation_matches_broker": True,
        "unresolved_critical_incident_count": 0,
        "manual_database_repair_count": 0,
        "drill_counts": {
            "planned_process_restart": 1,
            "execution_lease_handoff": 1,
            "forced_ws_reconnect": 1,
        },
        "review": _review(),
    }
    gate5 = build_gate5(gate5_evidence, upstream_gate4_report=gate4)

    assert gate3["result"] == "PASSED"
    assert gate4["result"] == "PASSED"
    assert gate5["result"] == "PASSED"
    assert gate5["activation_state_changed"] is False


def test_digest_mismatch_and_out_of_sequence_upstream_fail_closed():
    upstream = _gate2_report()
    upstream["result"] = "FAILED"
    evidence = dict(_passing_gate3()["evidence"])
    evidence["gate2_report_sha256"] = "0" * 64

    report = build_gate3(evidence, upstream_gate2_report=upstream)

    assert report["result"] == "FAILED"
    assert any(
        item["property"] == "compatible_upstream_gate"
        for item in report["invariant_violations"]
    )


def test_pilot_exception_evidence_cannot_satisfy_gate4():
    passing = _passing_gate4()
    upstream = _passing_gate3()
    evidence = dict(passing["evidence"])
    evidence["pilot_exception_evidence"] = True
    evidence["gate3_report_sha256"] = canonical_report_sha256(upstream)

    report = build_gate4(evidence, upstream_gate3_report=upstream)

    assert report["result"] == "FAILED"
    assert any(
        item["property"] == "pilot_evidence_excluded"
        for item in report["invariant_violations"]
    )


def test_gate4_accepts_more_than_the_three_session_minimum():
    passing = _passing_gate4()
    upstream = _passing_gate3()
    evidence = dict(passing["evidence"])
    evidence["gate3_report_sha256"] = canonical_report_sha256(upstream)
    evidence["supervised_regular_session_dates"] = [
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
    ]

    report = build_gate4(evidence, upstream_gate3_report=upstream)

    assert report["result"] == "PASSED"


def test_malformed_nested_evidence_fails_closed_without_validator_exception():
    gate2 = _gate2_report()
    gate3_evidence = dict(_passing_gate3()["evidence"])
    gate3_evidence.update(
        {
            "gate2_report_sha256": canonical_report_sha256(gate2),
            "complete_regular_session_count": 1.5,
            "would_event_counts": "not-a-map",
            "observed_decision_branches": "not-a-list",
            "fence_results": 17,
            "review": ["not", "a", "map"],
        }
    )
    gate3 = build_gate3(gate3_evidence, upstream_gate2_report=gate2)

    upstream_gate3 = _passing_gate3()
    gate4_evidence = dict(_passing_gate4()["evidence"])
    gate4_evidence.update(
        {
            "gate3_report_sha256": canonical_report_sha256(upstream_gate3),
            "current_execution_owner_count": True,
            "approved_symbols": 17,
            "review": "not-a-map",
        }
    )
    gate4 = build_gate4(gate4_evidence, upstream_gate3_report=upstream_gate3)

    upstream_gate4 = _passing_gate4()
    gate5 = build_gate5(
        {
            "commit_sha": COMMIT,
            "gate4_report_sha256": canonical_report_sha256(upstream_gate4),
            "duplicate_command_count": "many",
            "drill_counts": "not-a-map",
            "manual_database_repair_count": [],
            "review": 17,
        },
        upstream_gate4_report=upstream_gate4,
    )
    promotion = build_promotion_decision(
        {
            "target_gate": "GATE_4_CONTROLLED_LIVE",
            "operator_approval": "not-a-map",
        },
        gate_report={
            "gate": "GATE_4_CONTROLLED_LIVE",
            "result": "PASSED",
            "commit_sha": COMMIT,
            "evidence": "not-a-map",
        },
    )

    assert gate3["result"] == "FAILED"
    assert gate4["result"] == "FAILED"
    assert gate5["result"] == "FAILED"
    assert promotion["decision"] == "REJECTED"


def test_missing_gate5_drill_fails_closed_without_conflating_promotion():
    upstream = _passing_gate4()
    evidence = {
        "commit_sha": COMMIT,
        "gate4_report_sha256": canonical_report_sha256(upstream),
        "consecutive_full_session_dates": [],
        "drill_counts": {},
        "review": _review(),
    }

    report = build_gate5(evidence, upstream_gate4_report=upstream)

    properties = {item["property"] for item in report["invariant_violations"]}
    assert report["result"] == "FAILED"
    assert "five_consecutive_sessions" in properties
    assert "required_drills" in properties


def test_promotion_is_a_separate_exact_identity_decision():
    gate4 = _passing_gate4()
    gate5 = build_gate5(
        {
            "commit_sha": COMMIT,
            "gate4_report_sha256": canonical_report_sha256(gate4),
            "full_live_scope_and_limits_approved": True,
            "live_execution_mode": "FULL_LIVE",
            "full_live_config_sha256": "6" * 64,
            "risk_limits_sha256": "5" * 64,
            "external_watchdog_evidence_sha256": "7" * 64,
            "drills_within_qualification_window": True,
            "consecutive_full_session_dates": [
                "2026-08-24",
                "2026-08-25",
                "2026-08-26",
                "2026-08-27",
                "2026-08-28",
            ],
            "duplicate_command_count": 0,
            "unresolved_broker_local_discrepancy_count": 0,
            "stale_projected_quantity_count": 0,
            "command_after_lease_loss_count": 0,
            "all_reconnects_restored_desired_subscriptions": True,
            "restart_and_handoff_converged_without_manual_repair": True,
            "every_stop_used_fresh_event_data": True,
            "every_automatic_cancel_had_exact_ownership": True,
            "protective_exits_operational": True,
            "external_critical_alert_delivery_confirmed": True,
            "external_heartbeat_watchdog_running": True,
            "external_heartbeat_watchdog_tested": True,
            "startup_reconciliation_matches_broker": True,
            "final_reconciliation_matches_broker": True,
            "unresolved_critical_incident_count": 0,
            "manual_database_repair_count": 0,
            "drill_counts": {
                "planned_process_restart": 1,
                "execution_lease_handoff": 1,
                "forced_ws_reconnect": 1,
            },
            "review": _review(),
        },
        upstream_gate4_report=gate4,
    )
    request = {
        "target_gate": "GATE_5_UNATTENDED_QUALIFICATION",
        "gate_report_sha256": canonical_report_sha256(gate5),
        "deployed_commit_sha": COMMIT,
        "deployed_config_sha256": "6" * 64,
        "operator_approval": {
            "status": "APPROVED",
            "operator": "owner",
            "approved_at": "2026-08-29T12:00:00+00:00",
            "reference": f"sha256:{'8' * 64}",
        },
    }

    approved = build_promotion_decision(request, gate_report=gate5)
    mismatched = build_promotion_decision(
        {**request, "deployed_commit_sha": "b" * 40}, gate_report=gate5
    )

    assert approved["decision"] == "APPROVED"
    assert approved["activation_state_changed"] is False
    assert mismatched["decision"] == "REJECTED"
    assert any(
        item["property"] == "deployed_commit_identity"
        for item in mismatched["violations"]
    )


def test_gate5_rejects_calendar_gap_even_if_five_dates_are_supplied():
    upstream = _passing_gate4()
    evidence = {
        "commit_sha": COMMIT,
        "gate4_report_sha256": canonical_report_sha256(upstream),
        "consecutive_full_session_dates": [
            "2026-08-24",
            "2026-08-25",
            "2026-08-27",
            "2026-08-28",
            "2026-08-31",
        ],
        "drill_counts": {},
        "review": _review(),
    }

    report = build_gate5(evidence, upstream_gate4_report=upstream)

    assert report["result"] == "FAILED"
    assert any(
        item["property"] == "five_consecutive_sessions"
        for item in report["invariant_violations"]
    )
