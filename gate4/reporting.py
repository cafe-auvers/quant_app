"""Fail-closed Gate-4 controlled-live report validator."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping

from activation_gates.evidence import (
    canonical_report_sha256,
    evidence_integer,
    evidence_mapping,
    evidence_sequence,
    validate_independent_review,
    validate_nyse_session_dates,
    validate_upstream_report,
    valid_sha256,
    violation,
)
from activation_gates.requalification import (
    validate_gate4_requalification_manifest,
)


def build_report(
    evidence: Mapping[str, Any],
    *,
    upstream_gate3_report: Mapping[str, Any],
    baseline_gate4_report: Mapping[str, Any] | None = None,
    change_impact_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    commit_sha = str(evidence.get("commit_sha") or "").strip().lower()
    violations = validate_upstream_report(
        upstream_report=upstream_gate3_report,
        expected_gate="GATE_3_SHADOW_EXECUTION",
        expected_digest=str(evidence.get("gate3_report_sha256") or ""),
        commit_sha=commit_sha,
    )
    delta_requested = (
        baseline_gate4_report is not None or change_impact_manifest is not None
    )
    delta_mode = (
        baseline_gate4_report is not None and change_impact_manifest is not None
    )
    required_sessions = 3
    change_impact = "FULL_INITIAL_QUALIFICATION"
    carry_historical_coverage = False
    if delta_requested and not delta_mode:
        violations.append(
            violation(
                "gate4_requalification_inputs",
                "baseline report and change-impact manifest must be supplied together",
            )
        )
    if delta_mode:
        manifest_violations, required_sessions, change_impact = (
            validate_gate4_requalification_manifest(
                change_impact_manifest,
                baseline_report=baseline_gate4_report,
                current_commit=commit_sha,
            )
        )
        violations.extend(manifest_violations)
        baseline_evidence = evidence_mapping(baseline_gate4_report.get("evidence"))
        violations.extend(
            validate_nyse_session_dates(
                baseline_evidence.get("supervised_regular_session_dates"),
                expected_count=3,
                consecutive=False,
                exact_count=False,
            )
        )
        baseline_historical_counts = (
            "strategy_entry_terminal_outcome_count",
            "safe_exit_or_protected_position_count",
            "controlled_cancel_lifecycle_count",
        )
        if any(
            (evidence_integer(baseline_evidence.get(key)) or 0) < 1
            for key in baseline_historical_counts
        ):
            violations.append(
                violation(
                    "gate4_requalification_baseline",
                    "baseline lacks the required genuine entry, protection, or cancel coverage",
                )
            )
        violations.extend(
            validate_independent_review(
                evidence_mapping(baseline_evidence.get("review"))
            )
        )
        carry_historical_coverage = change_impact == "EVIDENCE_ONLY"
    required_true = {
        "collector_derived",
        "execution_capabilities_verified",
        "session_started_disarmed",
        "manual_arm_after_active_readiness",
        "every_entry_has_active_trade_card",
        "every_buy_below_notional_cap",
        "portfolio_risk_rechecked_atomically",
        "lifecycle_comparisons_agree",
        "disarm_probe_blocked_next_mutation",
        "external_critical_alert_delivered",
        "final_reconciliation_matches_broker",
    }
    entry_count = evidence_integer(evidence.get("entry_candidate_count"))
    if carry_historical_coverage and entry_count == 0:
        required_true -= {
            "every_entry_has_active_trade_card",
            "every_buy_below_notional_cap",
            "portfolio_risk_rechecked_atomically",
        }
    for key in sorted(required_true):
        if evidence.get(key) is not True:
            violations.append(violation(key, f"{key} must be true"))
    if str(evidence.get("live_execution_mode") or "") != "CONTROLLED_LIVE":
        violations.append(
            violation("controlled_live_mode", "KIS live execution mode is not CONTROLLED_LIVE")
        )
    if evidence_integer(evidence.get("current_execution_owner_count")) != 1:
        violations.append(violation("single_execution_owner", "owner count must equal one"))
    if evidence_integer(evidence.get("current_execution_lease_count")) != 1:
        violations.append(violation("single_execution_lease", "lease count must equal one"))
    for key in (
        "automatic_mutation_retry_attempt_count",
        "duplicate_or_unowned_mutation_count",
        "unresolved_ambiguous_identity_count",
        "lifecycle_mismatch_count",
    ):
        if evidence_integer(evidence.get(key)) != 0:
            violations.append(violation(key, f"{key} must equal zero"))

    violations.extend(
        validate_nyse_session_dates(
            evidence.get("supervised_regular_session_dates"),
            expected_count=required_sessions,
            consecutive=False,
            exact_count=False,
        )
    )
    for key in ("controlled_live_config_sha256", "risk_limits_sha256"):
        if not valid_sha256(evidence.get(key)):
            violations.append(violation(key, f"{key} is missing or invalid"))
    for key in ("evidence_journal_sha256", "capability_evidence_sha256"):
        if not valid_sha256(evidence.get(key)):
            violations.append(violation(key, f"{key} is missing or invalid"))
    for key in (
        "evidence_journal_parse_error_count",
        "evidence_journal_duplicate_event_id_count",
        "evidence_journal_hash_chain_error_count",
        "evidence_journal_identity_mismatch_count",
        "evidence_journal_unknown_event_type_count",
    ):
        if evidence_integer(evidence.get(key)) != 0:
            violations.append(violation(key, f"{key} must equal zero"))
    try:
        notional_cap = float(evidence.get("reviewed_entry_notional_cap"))
        max_notional = float(evidence.get("max_observed_entry_notional"))
    except (TypeError, ValueError, OverflowError):
        notional_cap = max_notional = float("nan")
    require_current_entry_coverage = not (
        carry_historical_coverage and entry_count == 0
    )
    if require_current_entry_coverage and not (
        math.isfinite(notional_cap)
        and math.isfinite(max_notional)
        and notional_cap > 0
        and 0 < max_notional <= notional_cap
    ):
        violations.append(
            violation(
                "reviewed_entry_notional_envelope",
                "positive observed entry notional must not exceed the positive reviewed cap",
            )
        )
    approved_symbols = {
        str(item or "").strip().upper()
        for item in evidence_sequence(evidence.get("approved_symbols"))
        if str(item or "").strip()
    }
    observed_symbols = {
        str(item or "").strip().upper()
        for item in evidence_sequence(evidence.get("observed_entry_symbols"))
        if str(item or "").strip()
    }
    if require_current_entry_coverage and (
        not approved_symbols
        or not observed_symbols
        or not observed_symbols <= approved_symbols
    ):
        violations.append(
            violation(
                "reviewed_symbol_envelope",
                "observed entry symbols must be a non-empty subset of approved symbols",
            )
        )
    strategy_outcomes = evidence_integer(
        evidence.get("strategy_entry_terminal_outcome_count")
    )
    if (
        not carry_historical_coverage
        and (strategy_outcomes is None or strategy_outcomes < 1)
    ):
        violations.append(violation("strategy_entry_outcome", "a genuine terminal entry is required"))
    safe_positions = evidence_integer(
        evidence.get("safe_exit_or_protected_position_count")
    )
    if (
        not carry_historical_coverage
        and (safe_positions is None or safe_positions < 1)
    ):
        violations.append(
            violation("position_protection", "the resulting position must be exited or protected")
        )
    cancel_lifecycles = evidence_integer(
        evidence.get("controlled_cancel_lifecycle_count")
    )
    if (
        not carry_historical_coverage
        and (cancel_lifecycles is None or cancel_lifecycles < 1)
    ):
        violations.append(
            violation("controlled_cancel_lifecycle", "one controlled cancellation is required")
        )
    if evidence.get("pilot_exception_evidence") is True:
        violations.append(
            violation("pilot_evidence_excluded", "pre-Gate-2 pilot evidence cannot satisfy Gate 4")
        )
    violations.extend(validate_independent_review(evidence_mapping(evidence.get("review"))))
    return {
        "schema_version": 1,
        "gate": "GATE_4_CONTROLLED_LIVE",
        "result": "PASSED" if not violations else "FAILED",
        "commit_sha": commit_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate3_report_sha256": evidence.get("gate3_report_sha256"),
        "qualification_mode": (
            "DELTA_REQUALIFICATION" if delta_mode else "FULL_QUALIFICATION"
        ),
        "required_supervised_session_count": required_sessions,
        "change_impact": change_impact,
        "baseline_gate4_report_sha256": (
            canonical_report_sha256(baseline_gate4_report)
            if baseline_gate4_report is not None
            else None
        ),
        "evidence": dict(evidence),
        "invariant_violations": violations,
        "activation_state_changed": False,
    }
