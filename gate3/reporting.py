"""Machine-readable Gate-3 shadow-execution report validator."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from activation_gates.evidence import (
    evidence_integer,
    evidence_mapping,
    evidence_sequence,
    validate_independent_review,
    validate_upstream_report,
    valid_sha256,
    violation,
)
from gate3.shadow_boundary import SHADOW_EVENT_TYPES


REQUIRED_DECISION_BRANCHES = frozenset(
    {
        "ENTRY_ALLOWED",
        "ENTRY_STALE_DATA_BLOCKED",
        "LEASE_LOSS_BLOCKED",
        "OWNERSHIP_MISMATCH_BLOCKED",
        "AMBIGUOUS_ORDER_RECONCILIATION",
        "KILL_SWITCH_BLOCKED",
        "CANCEL_EXACT_OWNERSHIP",
        "SELL_PROTECTIVE_EXIT",
    }
)
REQUIRED_FENCES = frozenset(
    {"stale_data", "lease_loss", "ownership", "reconciliation", "kill_switch"}
)


def build_report(
    evidence: Mapping[str, Any], *, upstream_gate2_report: Mapping[str, Any]
) -> dict[str, Any]:
    commit_sha = str(evidence.get("commit_sha") or "").strip().lower()
    violations = validate_upstream_report(
        upstream_report=upstream_gate2_report,
        expected_gate="GATE_2_LIVE_KIS_READ_ONLY_SOAK",
        expected_digest=str(evidence.get("gate2_report_sha256") or ""),
        commit_sha=commit_sha,
    )

    boolean_requirements = {
        "final_production_decision_runtime_used": "final production decision runtime was not used",
        "real_quotes_used": "real quotes did not drive the decision pass",
        "final_boundary_interception_enabled": "final mutation boundary was not intercepted",
        "mutation_audit_initialized": "final-boundary mutation audit was not initialized",
        "shadow_store_isolated": "shadow state was not physically isolated",
        "shadow_store_visibly_labelled": "shadow state was not visibly labelled",
        "shadow_store_append_only_verified": "append-only shadow persistence was not verified",
        "production_ledgers_unchanged": "production ledgers were modified",
    }
    for key, detail in boolean_requirements.items():
        if evidence.get(key) is not True:
            violations.append(violation(key, detail))

    for digest_key in (
        "strategy_rules_sha256",
        "decision_oracle_sha256",
        "shadow_store_sha256",
    ):
        if not valid_sha256(evidence.get(digest_key)):
            violations.append(violation(digest_key, f"{digest_key} is missing or invalid"))

    complete_sessions = evidence_integer(evidence.get("complete_regular_session_count"))
    if complete_sessions is None or complete_sessions < 1:
        violations.append(violation("complete_regular_session", "one full session is required"))

    event_counts = evidence_mapping(evidence.get("would_event_counts"))
    candidate_count = evidence_integer(evidence.get("mutation_candidate_count"))
    parsed_event_counts = {
        kind: evidence_integer(event_counts.get(kind)) for kind in SHADOW_EVENT_TYPES
    }
    total_events = sum(
        max(0, count) for count in parsed_event_counts.values() if count is not None
    )
    if candidate_count is None or candidate_count <= 0 or total_events != candidate_count:
        violations.append(
            violation(
                "mutation_candidate_audit_coverage",
                "WOULD_* event count must equal the positive mutation-candidate count",
            )
        )
    missing_event_types = sorted(
        kind
        for kind, count in parsed_event_counts.items()
        if count is None or count < 1
    )
    if missing_event_types:
        violations.append(
            violation(
                "required_would_event_coverage",
                f"missing event coverage: {', '.join(missing_event_types)}",
            )
        )

    zero_metrics = (
        "broker_mutation_attempt_count",
        "fake_broker_ack_count",
        "fake_fill_count",
        "production_ledger_write_count",
        "unresolved_oracle_difference_count",
        "shadow_event_parse_error_count",
        "shadow_event_label_mismatch_count",
        "shadow_duplicate_event_id_count",
    )
    for key in zero_metrics:
        count = evidence_integer(evidence.get(key))
        if count is None or count != 0:
            violations.append(violation(key, f"{key} must equal zero"))

    observed = {
        str(item) for item in evidence_sequence(evidence.get("observed_decision_branches"))
    }
    missing_branches = sorted(REQUIRED_DECISION_BRANCHES - observed)
    if missing_branches:
        violations.append(
            violation(
                "decision_branch_coverage",
                f"missing live/captured-live branches: {', '.join(missing_branches)}",
            )
        )
    fences = evidence_mapping(evidence.get("fence_results"))
    failed_fences = sorted(key for key in REQUIRED_FENCES if fences.get(key) != "PASSED")
    if failed_fences:
        violations.append(
            violation("safety_fences", f"unpassed fences: {', '.join(failed_fences)}")
        )
    violations.extend(validate_independent_review(evidence_mapping(evidence.get("review"))))

    return {
        "schema_version": 1,
        "gate": "GATE_3_SHADOW_EXECUTION",
        "result": "PASSED" if not violations else "FAILED",
        "commit_sha": commit_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate2_report_sha256": evidence.get("gate2_report_sha256"),
        "evidence": dict(evidence),
        "invariant_violations": violations,
        "activation_state_changed": False,
    }
