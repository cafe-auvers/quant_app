"""Pure qualification-to-operation promotion decision.

A gate report proves evidence.  It never changes runtime configuration.  This
module validates the separate operator decision and deployed identity while
remaining deliberately incapable of arming or mutating the application.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from activation_gates.evidence import (
    canonical_report_sha256,
    evidence_mapping,
    valid_aware_iso_datetime,
    valid_git_commit_sha,
    valid_sha256,
    violation,
)


_CONFIG_KEYS = {
    "GATE_3_SHADOW_EXECUTION": "strategy_rules_sha256",
    "GATE_4_CONTROLLED_LIVE": "controlled_live_config_sha256",
    "GATE_5_UNATTENDED_QUALIFICATION": "full_live_config_sha256",
}


def build_promotion_decision(
    request: Mapping[str, Any], *, gate_report: Mapping[str, Any]
) -> dict[str, Any]:
    """Return APPROVED only for an exact, reviewed, deployed gate identity."""

    violations: list[dict[str, str]] = []
    target_gate = str(request.get("target_gate") or "").strip()
    if target_gate not in _CONFIG_KEYS:
        violations.append(
            violation("known_target_gate", "promotion target must be Gate 3, 4, or 5")
        )
    if str(gate_report.get("gate") or "") != target_gate:
        violations.append(
            violation("qualified_target_gate", "gate report does not match promotion target")
        )
    if str(gate_report.get("result") or "").upper() != "PASSED":
        violations.append(
            violation("qualified_target_gate", "gate report result is not PASSED")
        )
    expected_report_digest = str(request.get("gate_report_sha256") or "").lower()
    if (
        not valid_sha256(expected_report_digest)
        or expected_report_digest != canonical_report_sha256(gate_report)
    ):
        violations.append(
            violation("qualified_report_identity", "gate report digest is missing or mismatched")
        )

    report_commit = str(gate_report.get("commit_sha") or "").strip().lower()
    deployed_commit = str(request.get("deployed_commit_sha") or "").strip().lower()
    if (
        not valid_git_commit_sha(report_commit)
        or not valid_git_commit_sha(deployed_commit)
        or deployed_commit != report_commit
    ):
        violations.append(
            violation("deployed_commit_identity", "deployed commit does not match qualification")
        )

    config_key = _CONFIG_KEYS.get(target_gate)
    report_evidence = evidence_mapping(gate_report.get("evidence"))
    qualified_config = str(report_evidence.get(config_key or "") or "").lower()
    deployed_config = str(request.get("deployed_config_sha256") or "").lower()
    if (
        not valid_sha256(qualified_config)
        or not valid_sha256(deployed_config)
        or deployed_config != qualified_config
    ):
        violations.append(
            violation(
                "deployed_configuration_identity",
                "deployed configuration does not match qualified configuration",
            )
        )

    approval = evidence_mapping(request.get("operator_approval"))
    approval_reference = str(approval.get("reference") or "").strip()
    if not (
        str(approval.get("status") or "").upper() == "APPROVED"
        and str(approval.get("operator") or "").strip()
        and valid_aware_iso_datetime(approval.get("approved_at"))
        and valid_sha256(approval_reference.removeprefix("sha256:"))
    ):
        violations.append(
            violation(
                "explicit_operator_approval",
                "promotion requires APPROVED operator, aware timestamp, and SHA-256 reference",
            )
        )

    return {
        "schema_version": 1,
        "decision": "APPROVED" if not violations else "REJECTED",
        "target_gate": target_gate,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate_report_sha256": expected_report_digest,
        "deployed_commit_sha": deployed_commit,
        "deployed_config_sha256": deployed_config,
        "violations": violations,
        "activation_state_changed": False,
    }


__all__ = ["build_promotion_decision"]
