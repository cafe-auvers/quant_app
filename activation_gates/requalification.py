"""Reviewed change-impact rules for evidence-preserving requalification.

The first Gate-4 qualification always requires three supervised sessions.
After that baseline exists, changes confined to evidence tooling can use one
new supervised delta session.  Every production-affecting or unknown path
fails closed to the full three-session requirement.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from activation_gates.evidence import (
    canonical_report_sha256,
    validate_independent_review,
    valid_git_commit_sha,
    valid_sha256,
    violation,
)


GATE4_EVIDENCE_ONLY_PREFIXES = (
    ".github/",
    "docs/",
    "tests/",
    "gate2/",
    "gate3/",
    "gate5/",
    "activation_gates/",
)
GATE4_EVIDENCE_ONLY_FILES = frozenset(
    {
        "gate4/collector.py",
        "gate4/reporting.py",
        "gate4/runner.py",
        "scripts/check_gate2_readiness.py",
        "scripts/manage_gate2_session.py",
        "scripts/manage_gate4_session.py",
        "scripts/run_gate1.py",
        "scripts/run_gate2_soak.py",
        "scripts/run_gate3_shadow.py",
        "scripts/validate_activation_gate.py",
        "scripts/validate_activation_promotion.py",
    }
)


def normalized_changed_paths(values: Sequence[object]) -> tuple[str, ...]:
    paths = {
        str(value or "").strip().replace("\\", "/").removeprefix("./")
        for value in values
    }
    return tuple(sorted(path for path in paths if path and ".." not in path.split("/")))


def gate4_change_impact(changed_paths: Sequence[object]) -> str:
    """Return the highest Gate-4 impact for an exact Git path set."""

    paths = normalized_changed_paths(changed_paths)
    if paths and all(
        path in GATE4_EVIDENCE_ONLY_FILES
        or path.startswith(GATE4_EVIDENCE_ONLY_PREFIXES)
        for path in paths
    ):
        return "EVIDENCE_ONLY"
    return "PRODUCTION_OR_UNKNOWN"


def required_gate4_supervised_sessions(impact: str) -> int:
    return 1 if str(impact or "").upper() == "EVIDENCE_ONLY" else 3


def validate_gate4_requalification_manifest(
    manifest: Mapping[str, Any],
    *,
    baseline_report: Mapping[str, Any],
    current_commit: str,
) -> tuple[list[dict[str, str]], int, str]:
    """Validate a reviewed manifest against its immutable Gate-4 baseline."""

    violations: list[dict[str, str]] = []
    baseline_commit = str(baseline_report.get("commit_sha") or "").strip().lower()
    target_commit = str(manifest.get("target_commit_sha") or "").strip().lower()
    baseline_digest = canonical_report_sha256(baseline_report)
    expected_digest = str(
        manifest.get("baseline_gate4_report_sha256") or ""
    ).strip().lower()
    if (
        baseline_report.get("gate") != "GATE_4_CONTROLLED_LIVE"
        or str(baseline_report.get("result") or "").upper() != "PASSED"
        or not valid_git_commit_sha(baseline_commit)
    ):
        violations.append(
            violation(
                "gate4_requalification_baseline",
                "baseline must be a PASSED Gate-4 report with a complete commit SHA",
            )
        )
    if not valid_sha256(expected_digest) or expected_digest != baseline_digest:
        violations.append(
            violation(
                "gate4_requalification_baseline",
                "baseline Gate-4 report digest is missing or mismatched",
            )
        )
    if str(manifest.get("baseline_commit_sha") or "").strip().lower() != baseline_commit:
        violations.append(
            violation(
                "gate4_requalification_identity",
                "manifest baseline commit does not match the baseline report",
            )
        )
    if target_commit != str(current_commit or "").strip().lower():
        violations.append(
            violation(
                "gate4_requalification_identity",
                "manifest target commit does not match the current report",
            )
        )
    changed_value = manifest.get("changed_paths")
    changed_paths = (
        normalized_changed_paths(changed_value)
        if isinstance(changed_value, (list, tuple))
        else ()
    )
    if not changed_paths:
        violations.append(
            violation(
                "gate4_change_impact",
                "a non-empty exact changed-path list is required",
            )
        )
    computed_impact = gate4_change_impact(changed_paths)
    declared_impact = str(manifest.get("impact") or "").strip().upper()
    if declared_impact != computed_impact:
        violations.append(
            violation(
                "gate4_change_impact",
                "declared impact does not match the fail-closed path classification",
            )
        )
    required_sessions = required_gate4_supervised_sessions(computed_impact)
    try:
        declared_sessions = int(manifest.get("required_supervised_sessions"))
    except (TypeError, ValueError):
        declared_sessions = -1
    if declared_sessions != required_sessions:
        violations.append(
            violation(
                "gate4_change_impact",
                "manifest session requirement does not match the computed impact",
            )
        )
    violations.extend(
        validate_independent_review(
            manifest.get("review") if isinstance(manifest.get("review"), Mapping) else {}
        )
    )
    return violations, required_sessions, computed_impact


__all__ = [
    "GATE4_EVIDENCE_ONLY_FILES",
    "GATE4_EVIDENCE_ONLY_PREFIXES",
    "gate4_change_impact",
    "normalized_changed_paths",
    "required_gate4_supervised_sessions",
    "validate_gate4_requalification_manifest",
]
