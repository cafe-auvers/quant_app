"""Fail-closed Gate-5 unattended-qualification report validator."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from activation_gates.evidence import (
    evidence_integer,
    evidence_mapping,
    validate_independent_review,
    validate_nyse_session_dates,
    validate_upstream_report,
    valid_sha256,
    violation,
)


def build_report(
    evidence: Mapping[str, Any], *, upstream_gate4_report: Mapping[str, Any]
) -> dict[str, Any]:
    commit_sha = str(evidence.get("commit_sha") or "").strip().lower()
    violations = validate_upstream_report(
        upstream_report=upstream_gate4_report,
        expected_gate="GATE_4_CONTROLLED_LIVE",
        expected_digest=str(evidence.get("gate4_report_sha256") or ""),
        commit_sha=commit_sha,
    )
    required_true = {
        "full_live_scope_and_limits_approved",
        "drills_within_qualification_window",
        "all_reconnects_restored_desired_subscriptions",
        "restart_and_handoff_converged_without_manual_repair",
        "every_stop_used_fresh_event_data",
        "every_automatic_cancel_had_exact_ownership",
        "protective_exits_operational",
        "external_critical_alert_delivery_confirmed",
        "external_heartbeat_watchdog_running",
        "external_heartbeat_watchdog_tested",
        "startup_reconciliation_matches_broker",
        "final_reconciliation_matches_broker",
    }
    for key in sorted(required_true):
        if evidence.get(key) is not True:
            violations.append(violation(key, f"{key} must be true"))
    if str(evidence.get("live_execution_mode") or "") != "FULL_LIVE":
        violations.append(
            violation("full_live_mode", "KIS live execution mode is not FULL_LIVE")
        )
    violations.extend(
        validate_nyse_session_dates(
            evidence.get("consecutive_full_session_dates"),
            expected_count=5,
            consecutive=True,
        )
    )
    for key in (
        "full_live_config_sha256",
        "risk_limits_sha256",
        "external_watchdog_evidence_sha256",
    ):
        if not valid_sha256(evidence.get(key)):
            violations.append(violation(key, f"{key} is missing or invalid"))
    for key in (
        "duplicate_command_count",
        "unresolved_broker_local_discrepancy_count",
        "stale_projected_quantity_count",
        "command_after_lease_loss_count",
        "unresolved_critical_incident_count",
    ):
        if evidence_integer(evidence.get(key)) != 0:
            violations.append(violation(key, f"{key} must equal zero"))
    drill_counts = evidence_mapping(evidence.get("drill_counts"))
    for drill in ("planned_process_restart", "execution_lease_handoff", "forced_ws_reconnect"):
        count = evidence_integer(drill_counts.get(drill))
        if count is None or count < 1:
            violations.append(violation("required_drills", f"missing drill: {drill}"))
    if evidence_integer(evidence.get("manual_database_repair_count")) != 0:
        violations.append(violation("manual_database_repair", "manual database repair is disqualifying"))
    violations.extend(validate_independent_review(evidence_mapping(evidence.get("review"))))
    return {
        "schema_version": 1,
        "gate": "GATE_5_UNATTENDED_QUALIFICATION",
        "result": "PASSED" if not violations else "FAILED",
        "commit_sha": commit_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate4_report_sha256": evidence.get("gate4_report_sha256"),
        "evidence": dict(evidence),
        "invariant_violations": violations,
        "activation_state_changed": False,
    }
