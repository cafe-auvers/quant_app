"""Exact-diff qualification carry-forward for non-runtime changes.

This module never infers safety from a commit message or a broad UI directory.
It binds a passed Gate-2/Gate-3 source chain and a passed Gate-1 target report
to the exact Git tree, binary diff, per-file patches, behavior assertions, and
an independent review.  Unknown paths fail closed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from activation_gates.evidence import (
    canonical_report_sha256,
    evidence_mapping,
    evidence_sequence,
    validate_independent_review,
    valid_git_commit_sha,
    valid_sha256,
    violation,
)
from activation_gates.requalification import (
    PRESENTATION_BEHAVIOR_INVARIANTS,
    gate4_change_impact,
    normalized_changed_paths,
    presentation_path_kind,
    required_gate4_supervised_sessions,
)


QUALIFICATION_CARRY_FORWARD_GATE = "EXECUTION_QUALIFICATION_CARRY_FORWARD"
ALLOWED_CARRY_FORWARD_IMPACTS = frozenset({"EVIDENCE_ONLY", "PRESENTATION_ONLY"})


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def manifest_review_subject_sha256(manifest: Mapping[str, Any]) -> str:
    """Digest the complete review subject without creating a self-reference."""

    subject = {
        str(key): value
        for key, value in manifest.items()
        if key not in {"review", "review_subject_sha256"}
    }
    return _canonical_sha256(subject)


def _git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=Path(root),
        check=check,
        capture_output=True,
        timeout=30,
    )


def _git_text(root: Path, *args: str, check: bool = True) -> str:
    completed = _git(root, *args, check=check)
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _commit_file(root: Path, commit: str, path: str) -> bytes | None:
    probe = _git(root, "cat-file", "-e", f"{commit}:{path}", check=False)
    if probe.returncode != 0:
        return None
    return _git(root, "show", f"{commit}:{path}").stdout


def collect_git_change_evidence(
    *,
    root: Path,
    source_commit: str,
    target_commit: str,
) -> dict[str, Any]:
    """Collect a deterministic, binary-safe fingerprint for an exact Git diff."""

    root = Path(root).resolve()
    source = str(source_commit or "").strip().lower()
    target = str(target_commit or "").strip().lower()
    if not valid_git_commit_sha(source) or not valid_git_commit_sha(target):
        raise RuntimeError("source and target must be complete Git commit SHAs")
    if _git_text(root, "rev-parse", f"{source}^{{commit}}").lower() != source:
        raise RuntimeError("source commit does not resolve exactly")
    if _git_text(root, "rev-parse", f"{target}^{{commit}}").lower() != target:
        raise RuntimeError("target commit does not resolve exactly")
    if _git(root, "merge-base", "--is-ancestor", source, target, check=False).returncode:
        raise RuntimeError("source commit must be an ancestor of target commit")

    raw_names = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        source,
        target,
        "--",
    ).stdout
    decoded_names = tuple(
        item.decode("utf-8", errors="strict")
        for item in raw_names.split(b"\0")
        if item
    )
    changed_paths = normalized_changed_paths(decoded_names)
    if not changed_paths or len(changed_paths) != len(decoded_names):
        raise RuntimeError("Git diff must contain a non-empty, normalized path set")

    full_patch = _git(
        root,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-renames",
        source,
        target,
        "--",
    ).stdout
    file_changes: list[dict[str, Any]] = []
    for path in changed_paths:
        source_bytes = _commit_file(root, source, path)
        target_bytes = _commit_file(root, target, path)
        patch = _git(
            root,
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-renames",
            source,
            target,
            "--",
            path,
        ).stdout
        file_changes.append(
            {
                "path": path,
                "path_kind": presentation_path_kind(path),
                "source_sha256": (
                    _sha256_bytes(source_bytes) if source_bytes is not None else None
                ),
                "target_sha256": (
                    _sha256_bytes(target_bytes) if target_bytes is not None else None
                ),
                "patch_sha256": _sha256_bytes(patch),
            }
        )
    return {
        "source_commit_sha": source,
        "target_commit_sha": target,
        "target_tree_sha": _git_text(root, "rev-parse", f"{target}^{{tree}}").lower(),
        "exact_diff_sha256": _sha256_bytes(full_patch),
        "changed_paths": list(changed_paths),
        "file_changes": file_changes,
    }


def validate_manifest_git_binding(
    manifest: Mapping[str, Any],
    *,
    root: Path,
    source_commit: str,
    target_commit: str,
) -> list[dict[str, str]]:
    """Prove that a reviewed manifest still names the exact Git change set."""

    violations: list[dict[str, str]] = []
    try:
        actual = collect_git_change_evidence(
            root=root,
            source_commit=source_commit,
            target_commit=target_commit,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError) as exc:
        return [violation("carry_forward_git_diff", str(exc))]
    for key in ("target_tree_sha", "exact_diff_sha256"):
        if str(manifest.get(key) or "").strip().lower() != actual[key]:
            violations.append(
                violation("carry_forward_git_diff", f"{key} does not match Git")
            )
    changed_paths = normalized_changed_paths(
        manifest.get("changed_paths")
        if isinstance(manifest.get("changed_paths"), (list, tuple))
        else ()
    )
    if list(changed_paths) != actual["changed_paths"]:
        violations.append(
            violation(
                "carry_forward_git_diff",
                "manifest changed paths do not exactly match the Git diff",
            )
        )
    expected = {item["path"]: item for item in actual["file_changes"]}
    raw_reviews = manifest.get("file_reviews")
    reviews = raw_reviews if isinstance(raw_reviews, list) else []
    recorded: dict[str, Mapping[str, Any]] = {}
    for item in reviews:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if path in recorded:
            violations.append(
                violation("carry_forward_file_review", f"duplicate file review: {path}")
            )
        recorded[path] = item
    if set(recorded) != set(expected):
        violations.append(
            violation(
                "carry_forward_file_review",
                "every changed path must have exactly one file review",
            )
        )
    for path, expected_item in expected.items():
        recorded_item = recorded.get(path, {})
        for key in ("path_kind", "source_sha256", "target_sha256", "patch_sha256"):
            if recorded_item.get(key) != expected_item.get(key):
                violations.append(
                    violation(
                        "carry_forward_file_review",
                        f"{path} {key} does not match the exact Git patch",
                    )
                )
    return violations


def _validate_source_chain(
    source_gate2_report: Mapping[str, Any],
    source_gate3_report: Mapping[str, Any],
) -> tuple[list[dict[str, str]], str]:
    violations: list[dict[str, str]] = []
    source_commit = str(source_gate3_report.get("commit_sha") or "").strip().lower()
    if (
        source_gate2_report.get("gate") != "GATE_2_LIVE_KIS_READ_ONLY_SOAK"
        or str(source_gate2_report.get("result") or "").upper() != "PASSED"
    ):
        violations.append(
            violation("carry_forward_source", "source Gate-2 report must be PASSED")
        )
    if (
        source_gate3_report.get("gate") != "GATE_3_SHADOW_EXECUTION"
        or str(source_gate3_report.get("result") or "").upper() != "PASSED"
    ):
        violations.append(
            violation("carry_forward_source", "source Gate-3 report must be PASSED")
        )
    gate2_commit = str(source_gate2_report.get("commit_sha") or "").strip().lower()
    if (
        not valid_git_commit_sha(source_commit)
        or gate2_commit != source_commit
    ):
        violations.append(
            violation(
                "carry_forward_source",
                "source Gate-2 and Gate-3 reports must share one complete commit SHA",
            )
        )
    expected_gate2_digest = str(
        source_gate3_report.get("gate2_report_sha256") or ""
    ).strip().lower()
    if expected_gate2_digest != canonical_report_sha256(source_gate2_report):
        violations.append(
            violation(
                "carry_forward_source",
                "source Gate-3 report does not bind the supplied Gate-2 report",
            )
        )
    return violations, source_commit


def _validate_target_gate1(
    target_gate1_report: Mapping[str, Any],
) -> tuple[list[dict[str, str]], str]:
    violations: list[dict[str, str]] = []
    target_commit = str(target_gate1_report.get("commit_sha") or "").strip().lower()
    if (
        target_gate1_report.get("gate") != "GATE_1_DETERMINISTIC_SIMULATION"
        or str(target_gate1_report.get("result") or "").upper() != "PASSED"
        or target_gate1_report.get("pytest_exit_code") != 0
        or not valid_git_commit_sha(target_commit)
    ):
        violations.append(
            violation(
                "carry_forward_target_gate1",
                "target must have a PASSED exact-commit Gate-1 report",
            )
        )
    source_identity = evidence_mapping(target_gate1_report.get("source_identity"))
    if (
        source_identity.get("commit_sha") != target_commit
        or source_identity.get("worktree_clean") is not True
    ):
        violations.append(
            violation(
                "carry_forward_target_gate1",
                "Gate-1 source identity must bind a clean target commit",
            )
        )
    matrix_versions = {
        str(evidence_mapping(item).get("python_version") or "").strip()
        for item in evidence_sequence(target_gate1_report.get("ci_matrix"))
        if str(evidence_mapping(item).get("result") or "").upper() == "PASSED"
    }
    if not {"3.11", "3.12"}.issubset(matrix_versions):
        violations.append(
            violation(
                "carry_forward_target_gate1",
                "target Gate-1 matrix must pass on Python 3.11 and 3.12",
            )
        )
    return violations, target_commit


def build_change_manifest(
    *,
    source_gate2_report: Mapping[str, Any],
    source_gate3_report: Mapping[str, Any],
    target_gate1_report: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Build an independently reviewable exact-diff manifest in PENDING state."""

    source_violations, source_commit = _validate_source_chain(
        source_gate2_report, source_gate3_report
    )
    target_violations, target_commit = _validate_target_gate1(target_gate1_report)
    if source_violations or target_violations:
        details = source_violations + target_violations
        raise RuntimeError("; ".join(item["detail"] for item in details))
    git_evidence = collect_git_change_evidence(
        root=root,
        source_commit=source_commit,
        target_commit=target_commit,
    )
    reviewed_paths = [
        item["path"]
        for item in git_evidence["file_changes"]
        if item["path_kind"] == "REVIEWED_EXECUTABLE_UI"
    ]
    impact = gate4_change_impact(
        git_evidence["changed_paths"],
        reviewed_presentation_paths=reviewed_paths,
    )
    file_reviews = []
    for item in git_evidence["file_changes"]:
        file_reviews.append(
            {
                **item,
                "review_rationale": (
                    "Exact patch requires independent UI behavior review."
                    if item["path_kind"] == "REVIEWED_EXECUTABLE_UI"
                    else "Path is mechanically scoped by the fail-closed classifier."
                ),
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "gate": QUALIFICATION_CARRY_FORWARD_GATE,
        "source_commit_sha": source_commit,
        "target_commit_sha": target_commit,
        "target_tree_sha": git_evidence["target_tree_sha"],
        "source_gate2_report_sha256": canonical_report_sha256(source_gate2_report),
        "source_gate3_report_sha256": canonical_report_sha256(source_gate3_report),
        "target_gate1_report_sha256": canonical_report_sha256(target_gate1_report),
        "exact_diff_sha256": git_evidence["exact_diff_sha256"],
        "changed_paths": git_evidence["changed_paths"],
        "file_reviews": file_reviews,
        "reviewed_presentation_paths": reviewed_paths,
        "impact": impact,
        "required_gate4_supervised_sessions_after_baseline": (
            required_gate4_supervised_sessions(impact)
        ),
        "behavior_invariants": {
            name: False for name in PRESENTATION_BEHAVIOR_INVARIANTS
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review": {
            "status": "PENDING",
            "author": "qualification carry-forward manifest builder",
            "reviewer": "",
            "reviewed_at": "",
            "reference": "",
        },
    }
    manifest["review_subject_sha256"] = manifest_review_subject_sha256(manifest)
    return manifest


def validate_change_manifest(
    manifest: Mapping[str, Any],
    *,
    source_gate2_report: Mapping[str, Any],
    source_gate3_report: Mapping[str, Any],
    target_gate1_report: Mapping[str, Any],
    root: Path,
) -> tuple[list[dict[str, str]], str]:
    """Validate an approved manifest against reports and the real Git objects."""

    violations, source_commit = _validate_source_chain(
        source_gate2_report, source_gate3_report
    )
    target_violations, target_commit = _validate_target_gate1(target_gate1_report)
    violations.extend(target_violations)
    if manifest.get("gate") != QUALIFICATION_CARRY_FORWARD_GATE:
        violations.append(
            violation("carry_forward_manifest", "unexpected carry-forward manifest type")
        )
    expected_scalars = {
        "source_commit_sha": source_commit,
        "target_commit_sha": target_commit,
        "source_gate2_report_sha256": canonical_report_sha256(source_gate2_report),
        "source_gate3_report_sha256": canonical_report_sha256(source_gate3_report),
        "target_gate1_report_sha256": canonical_report_sha256(target_gate1_report),
    }
    for key, expected in expected_scalars.items():
        if str(manifest.get(key) or "").strip().lower() != expected:
            violations.append(
                violation("carry_forward_identity", f"{key} is missing or mismatched")
            )
    try:
        git_evidence = collect_git_change_evidence(
            root=root,
            source_commit=source_commit,
            target_commit=target_commit,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError) as exc:
        violations.append(violation("carry_forward_git_diff", str(exc)))
        git_evidence = {
            "target_tree_sha": "",
            "exact_diff_sha256": "",
            "changed_paths": [],
            "file_changes": [],
        }
    for key in ("target_tree_sha", "exact_diff_sha256"):
        if str(manifest.get(key) or "").strip().lower() != git_evidence[key]:
            violations.append(
                violation("carry_forward_git_diff", f"{key} does not match Git")
            )
    changed_paths = normalized_changed_paths(
        manifest.get("changed_paths")
        if isinstance(manifest.get("changed_paths"), (list, tuple))
        else ()
    )
    if list(changed_paths) != git_evidence["changed_paths"]:
        violations.append(
            violation(
                "carry_forward_git_diff",
                "manifest changed paths do not exactly match the Git diff",
            )
        )
    reviewed_paths = normalized_changed_paths(
        manifest.get("reviewed_presentation_paths")
        if isinstance(manifest.get("reviewed_presentation_paths"), (list, tuple))
        else ()
    )
    expected_reviews = {
        item["path"]: item for item in git_evidence["file_changes"]
    }
    recorded_reviews = manifest.get("file_reviews")
    if not isinstance(recorded_reviews, list):
        recorded_reviews = []
    recorded_by_path: dict[str, Mapping[str, Any]] = {}
    for raw in recorded_reviews:
        if not isinstance(raw, Mapping):
            continue
        path = str(raw.get("path") or "").strip().replace("\\", "/")
        if path in recorded_by_path:
            violations.append(
                violation("carry_forward_file_review", f"duplicate file review: {path}")
            )
        recorded_by_path[path] = raw
    if set(recorded_by_path) != set(expected_reviews):
        violations.append(
            violation(
                "carry_forward_file_review",
                "every changed path must have exactly one file review",
            )
        )
    for path, expected in expected_reviews.items():
        recorded = recorded_by_path.get(path, {})
        for key in ("path_kind", "source_sha256", "target_sha256", "patch_sha256"):
            if recorded.get(key) != expected.get(key):
                violations.append(
                    violation(
                        "carry_forward_file_review",
                        f"{path} {key} does not match the exact Git patch",
                    )
                )
        if (
            expected["path_kind"] == "REVIEWED_EXECUTABLE_UI"
            and not str(recorded.get("review_rationale") or "").strip()
        ):
            violations.append(
                violation(
                    "carry_forward_file_review",
                    f"{path} requires an explicit review rationale",
                )
            )
    expected_reviewed_paths = tuple(
        sorted(
            path
            for path, item in expected_reviews.items()
            if item["path_kind"] == "REVIEWED_EXECUTABLE_UI"
        )
    )
    if reviewed_paths != expected_reviewed_paths:
        violations.append(
            violation(
                "carry_forward_file_review",
                "reviewed presentation paths must exactly cover executable UI changes",
            )
        )
    computed_impact = gate4_change_impact(
        changed_paths,
        reviewed_presentation_paths=reviewed_paths,
    )
    if str(manifest.get("impact") or "").strip().upper() != computed_impact:
        violations.append(
            violation(
                "carry_forward_impact",
                "declared impact does not match the fail-closed classifier",
            )
        )
    if computed_impact not in ALLOWED_CARRY_FORWARD_IMPACTS:
        violations.append(
            violation(
                "carry_forward_impact",
                "production-affecting or unknown changes cannot carry Gate-2/Gate-3",
            )
        )
    expected_sessions = required_gate4_supervised_sessions(computed_impact)
    if manifest.get("required_gate4_supervised_sessions_after_baseline") != expected_sessions:
        violations.append(
            violation(
                "carry_forward_impact",
                "Gate-4 session impact does not match the computed category",
            )
        )
    if computed_impact == "PRESENTATION_ONLY":
        assertions = evidence_mapping(manifest.get("behavior_invariants"))
        if any(assertions.get(name) is not True for name in PRESENTATION_BEHAVIOR_INVARIANTS):
            violations.append(
                violation(
                    "presentation_only_behavior",
                    "all presentation-only behavior invariants must be explicitly true",
                )
            )
    subject_digest = manifest_review_subject_sha256(manifest)
    if str(manifest.get("review_subject_sha256") or "").strip().lower() != subject_digest:
        violations.append(
            violation(
                "carry_forward_review",
                "review-subject digest does not match the exact manifest",
            )
        )
    review = evidence_mapping(manifest.get("review"))
    violations.extend(validate_independent_review(review))
    if str(review.get("reference") or "").removeprefix("sha256:").lower() != subject_digest:
        violations.append(
            violation(
                "carry_forward_review",
                "independent review reference must bind the exact review subject",
            )
        )
    return violations, computed_impact


def build_carried_gate3_report(
    *,
    source_gate2_report: Mapping[str, Any],
    source_gate3_report: Mapping[str, Any],
    target_gate1_report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    """Build a transparent target-commit Gate-3 carry-forward report."""

    violations, impact = validate_change_manifest(
        manifest,
        source_gate2_report=source_gate2_report,
        source_gate3_report=source_gate3_report,
        target_gate1_report=target_gate1_report,
        root=root,
    )
    target_commit = str(target_gate1_report.get("commit_sha") or "").strip().lower()
    return {
        "schema_version": 1,
        "gate": "GATE_3_SHADOW_EXECUTION",
        "result": "PASSED" if not violations else "FAILED",
        "commit_sha": target_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "qualification_mode": f"{impact}_CARRY_FORWARD",
        "gate2_report_sha256": canonical_report_sha256(source_gate2_report),
        "source_gate3_report_sha256": canonical_report_sha256(source_gate3_report),
        "target_gate1_report_sha256": canonical_report_sha256(target_gate1_report),
        "change_manifest_sha256": _canonical_sha256(manifest),
        "change_impact": impact,
        "carry_forward": {
            "source_gate2_report": dict(source_gate2_report),
            "source_gate3_report": dict(source_gate3_report),
            "target_gate1_report": dict(target_gate1_report),
            "change_manifest": dict(manifest),
        },
        "invariant_violations": violations,
        "activation_state_changed": False,
    }


def validate_carried_gate3_report(
    report: Mapping[str, Any],
    *,
    root: Path,
    expected_commit: str,
) -> list[dict[str, str]]:
    """Revalidate a carry-forward report before Gate 4 consumes it."""

    violations: list[dict[str, str]] = []
    carry = evidence_mapping(report.get("carry_forward"))
    gate2 = evidence_mapping(carry.get("source_gate2_report"))
    gate3 = evidence_mapping(carry.get("source_gate3_report"))
    gate1 = evidence_mapping(carry.get("target_gate1_report"))
    manifest = evidence_mapping(carry.get("change_manifest"))
    manifest_violations, impact = validate_change_manifest(
        manifest,
        source_gate2_report=gate2,
        source_gate3_report=gate3,
        target_gate1_report=gate1,
        root=root,
    )
    violations.extend(manifest_violations)
    target_commit = str(expected_commit or "").strip().lower()
    if (
        report.get("gate") != "GATE_3_SHADOW_EXECUTION"
        or report.get("result") != "PASSED"
        or str(report.get("commit_sha") or "").strip().lower() != target_commit
        or str(gate1.get("commit_sha") or "").strip().lower() != target_commit
    ):
        violations.append(
            violation(
                "carry_forward_report",
                "carried Gate-3 report is not PASSED on the exact target commit",
            )
        )
    expected_mode = f"{impact}_CARRY_FORWARD"
    if report.get("qualification_mode") != expected_mode:
        violations.append(
            violation("carry_forward_report", "qualification mode is mismatched")
        )
    expected_digests = {
        "gate2_report_sha256": canonical_report_sha256(gate2),
        "source_gate3_report_sha256": canonical_report_sha256(gate3),
        "target_gate1_report_sha256": canonical_report_sha256(gate1),
        "change_manifest_sha256": _canonical_sha256(manifest),
    }
    for key, expected in expected_digests.items():
        if str(report.get(key) or "").strip().lower() != expected:
            violations.append(
                violation("carry_forward_report", f"{key} is missing or mismatched")
            )
    if report.get("change_impact") != impact:
        violations.append(
            violation("carry_forward_report", "change impact is mismatched")
        )
    if report.get("activation_state_changed") is not False:
        violations.append(
            violation("carry_forward_report", "carry-forward cannot change activation")
        )
    if report.get("invariant_violations") not in ([], ()):
        violations.append(
            violation(
                "carry_forward_report",
                "a passed carry-forward report cannot contain recorded violations",
            )
        )
    return violations


__all__ = [
    "ALLOWED_CARRY_FORWARD_IMPACTS",
    "QUALIFICATION_CARRY_FORWARD_GATE",
    "build_carried_gate3_report",
    "build_change_manifest",
    "collect_git_change_evidence",
    "manifest_review_subject_sha256",
    "validate_carried_gate3_report",
    "validate_change_manifest",
    "validate_manifest_git_binding",
]
