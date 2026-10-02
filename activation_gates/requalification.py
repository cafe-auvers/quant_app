"""Reviewed change-impact rules for evidence-preserving requalification.

The first Gate-4 qualification always requires three supervised sessions.
After that baseline exists, changes confined to evidence tooling can use one
new supervised delta session.  A narrowly reviewed presentation-only change
can preserve the baseline without another live session.  Every production-
affecting or unknown path fails closed to the full three-session requirement.
"""

from __future__ import annotations

import hashlib
import json
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
        "scripts/build_gate4_requalification_manifest.py",
        "scripts/check_gate2_readiness.py",
        "scripts/manage_gate2_session.py",
        "scripts/manage_gate4_session.py",
        "scripts/build_qualification_carryforward.py",
        "scripts/run_gate1.py",
        "scripts/run_gate2_soak.py",
        "scripts/run_gate3_shadow.py",
        "scripts/validate_activation_gate.py",
        "scripts/validate_activation_promotion.py",
    }
)

# These file types contain no executable controller code.  They are still
# bound to an exact diff and independent review by the manifest validator.
PRESENTATION_ONLY_STATIC_SUFFIXES = frozenset(
    {".css", ".ico", ".jpg", ".jpeg", ".png", ".webp"}
)
PRESENTATION_ONLY_STATIC_PREFIXES = ("src/web/static/", "src/ui/assets/")

# These paths can contain live action callbacks.  They are never accepted by
# path alone: every changed path must be named in ``reviewed_presentation_paths``
# and every behavior invariant below must be explicitly approved.
PRESENTATION_REVIEW_CANDIDATE_PREFIXES = (
    "src/ui/",
    "src/web/static/",
)
PRESENTATION_REVIEW_CANDIDATE_SUFFIXES = frozenset({".html", ".js", ".py", ".svg"})

PRESENTATION_BEHAVIOR_INVARIANTS = (
    "no_execution_command_target_change",
    "no_command_payload_change",
    "no_enablement_or_permission_change",
    "no_confirmation_or_safety_interlock_change",
    "no_quantity_price_risk_or_default_change",
    "no_persistence_or_runtime_state_change",
    "no_operator_control_or_lease_change",
    "no_execution_state_interpretation_change",
    "no_dependency_or_configuration_change",
)


def normalized_changed_paths(values: Sequence[object]) -> tuple[str, ...]:
    paths = {
        str(value or "").strip().replace("\\", "/").removeprefix("./")
        for value in values
    }
    return tuple(sorted(path for path in paths if path and ".." not in path.split("/")))


def _review_subject_sha256(manifest: Mapping[str, Any]) -> str:
    subject = {
        str(key): value
        for key, value in manifest.items()
        if key not in {"review", "review_subject_sha256"}
    }
    payload = json.dumps(
        subject,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_evidence_only_path(path: str) -> bool:
    return path in GATE4_EVIDENCE_ONLY_FILES or path.startswith(
        GATE4_EVIDENCE_ONLY_PREFIXES
    )


def _is_static_presentation_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.startswith(PRESENTATION_ONLY_STATIC_PREFIXES) and any(
        lowered.endswith(suffix) for suffix in PRESENTATION_ONLY_STATIC_SUFFIXES
    )


def _is_reviewable_presentation_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.startswith(PRESENTATION_REVIEW_CANDIDATE_PREFIXES) and any(
        lowered.endswith(suffix)
        for suffix in PRESENTATION_REVIEW_CANDIDATE_SUFFIXES
    )


def presentation_path_kind(path: object) -> str:
    """Return the narrow manifest-review class for one normalized Git path."""

    normalized = normalized_changed_paths((path,))
    if len(normalized) != 1:
        return "PRODUCTION_OR_UNKNOWN"
    value = normalized[0]
    if _is_evidence_only_path(value):
        return "EVIDENCE_ONLY"
    if _is_static_presentation_path(value):
        return "STATIC_PRESENTATION"
    if _is_reviewable_presentation_path(value):
        return "REVIEWED_EXECUTABLE_UI"
    return "PRODUCTION_OR_UNKNOWN"


def gate4_change_impact(
    changed_paths: Sequence[object],
    *,
    reviewed_presentation_paths: Sequence[object] = (),
) -> str:
    """Return the highest Gate-4 impact for an exact Git path set."""

    paths = normalized_changed_paths(changed_paths)
    reviewed = set(normalized_changed_paths(reviewed_presentation_paths))
    if paths and all(_is_evidence_only_path(path) for path in paths):
        return "EVIDENCE_ONLY"
    if paths and all(
        _is_evidence_only_path(path)
        or _is_static_presentation_path(path)
        or (path in reviewed and _is_reviewable_presentation_path(path))
        for path in paths
    ):
        return "PRESENTATION_ONLY"
    return "PRODUCTION_OR_UNKNOWN"


def required_gate4_supervised_sessions(impact: str) -> int:
    normalized = str(impact or "").upper()
    if normalized == "PRESENTATION_ONLY":
        return 0
    if normalized == "EVIDENCE_ONLY":
        return 1
    return 3


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
    reviewed_value = manifest.get("reviewed_presentation_paths")
    reviewed_paths = (
        normalized_changed_paths(reviewed_value)
        if isinstance(reviewed_value, (list, tuple))
        else ()
    )
    if any(path not in changed_paths for path in reviewed_paths):
        violations.append(
            violation(
                "gate4_change_impact",
                "reviewed presentation paths must be present in the exact changed-path list",
            )
        )
    if any(not _is_reviewable_presentation_path(path) for path in reviewed_paths):
        violations.append(
            violation(
                "gate4_change_impact",
                "a reviewed presentation path is outside the narrow UI candidate scope",
            )
        )
    computed_impact = gate4_change_impact(
        changed_paths,
        reviewed_presentation_paths=reviewed_paths,
    )
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
    if computed_impact == "PRESENTATION_ONLY":
        assertions = manifest.get("behavior_invariants")
        if not isinstance(assertions, Mapping) or any(
            assertions.get(name) is not True
            for name in PRESENTATION_BEHAVIOR_INVARIANTS
        ):
            violations.append(
                violation(
                    "presentation_only_behavior",
                    "every presentation-only behavior invariant must be explicitly true",
                )
            )
        review_subject = _review_subject_sha256(manifest)
        if (
            str(manifest.get("review_subject_sha256") or "").strip().lower()
            != review_subject
        ):
            violations.append(
                violation(
                    "presentation_only_review",
                    "review-subject digest does not match the exact manifest",
                )
            )
        review = manifest.get("review")
        reference = (
            str(review.get("reference") or "")
            if isinstance(review, Mapping)
            else ""
        )
        if reference.removeprefix("sha256:").lower() != review_subject:
            violations.append(
                violation(
                    "presentation_only_review",
                    "independent review reference must bind the exact manifest",
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
    "PRESENTATION_BEHAVIOR_INVARIANTS",
    "PRESENTATION_ONLY_STATIC_PREFIXES",
    "PRESENTATION_ONLY_STATIC_SUFFIXES",
    "PRESENTATION_REVIEW_CANDIDATE_PREFIXES",
    "PRESENTATION_REVIEW_CANDIDATE_SUFFIXES",
    "gate4_change_impact",
    "normalized_changed_paths",
    "presentation_path_kind",
    "required_gate4_supervised_sessions",
    "validate_gate4_requalification_manifest",
]
