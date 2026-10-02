from __future__ import annotations

import subprocess

from activation_gates.carryforward import (
    build_carried_gate3_report,
    build_change_manifest,
    manifest_review_subject_sha256,
    validate_carried_gate3_report,
)
from activation_gates.evidence import canonical_report_sha256
from activation_gates.requalification import (
    PRESENTATION_BEHAVIOR_INVARIANTS,
    gate4_change_impact,
    required_gate4_supervised_sessions,
)


def _git(root, *args):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(root, message):
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Gate Test",
        "-c",
        "user.email=gate@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _source_reports(commit):
    gate2 = {
        "gate": "GATE_2_LIVE_KIS_READ_ONLY_SOAK",
        "result": "PASSED",
        "commit_sha": commit,
    }
    gate3 = {
        "gate": "GATE_3_SHADOW_EXECUTION",
        "result": "PASSED",
        "commit_sha": commit,
        "gate2_report_sha256": canonical_report_sha256(gate2),
    }
    return gate2, gate3


def _target_gate1(commit):
    return {
        "gate": "GATE_1_DETERMINISTIC_SIMULATION",
        "result": "PASSED",
        "commit_sha": commit,
        "pytest_exit_code": 0,
        "source_identity": {
            "commit_sha": commit,
            "worktree_clean": True,
        },
        "ci_matrix": [
            {"python_version": "3.11", "result": "PASSED"},
            {"python_version": "3.12", "result": "PASSED"},
        ],
    }


def _approve(manifest):
    approved = dict(manifest)
    approved["behavior_invariants"] = {
        name: True for name in PRESENTATION_BEHAVIOR_INVARIANTS
    }
    approved["review_subject_sha256"] = manifest_review_subject_sha256(approved)
    approved["review"] = {
        "status": "APPROVED",
        "author": "change-author",
        "reviewer": "independent-reviewer",
        "reviewed_at": "2026-10-03T12:00:00+00:00",
        "reference": f"sha256:{approved['review_subject_sha256']}",
    }
    return approved


def _repo_with_change(tmp_path, relative_path, source_text, target_text):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source_text, encoding="utf-8")
    source = _commit(root, "source")
    path.write_text(target_text, encoding="utf-8")
    target = _commit(root, "target")
    return root, source, target


def test_static_css_change_can_carry_gate2_and_gate3_after_exact_review(tmp_path):
    root, source, target = _repo_with_change(
        tmp_path,
        "src/web/static/app.css",
        ".button { color: white; }\n",
        ".button { color: mintcream; }\n",
    )
    gate2, gate3 = _source_reports(source)
    gate1 = _target_gate1(target)
    manifest = _approve(
        build_change_manifest(
            source_gate2_report=gate2,
            source_gate3_report=gate3,
            target_gate1_report=gate1,
            root=root,
        )
    )

    report = build_carried_gate3_report(
        source_gate2_report=gate2,
        source_gate3_report=gate3,
        target_gate1_report=gate1,
        manifest=manifest,
        root=root,
    )

    assert manifest["impact"] == "PRESENTATION_ONLY"
    assert manifest["required_gate4_supervised_sessions_after_baseline"] == 0
    assert report["result"] == "PASSED"
    assert report["commit_sha"] == target
    assert validate_carried_gate3_report(
        report, root=root, expected_commit=target
    ) == []


def test_executable_ui_path_needs_explicit_review_scope():
    path = "src/web/static/app.js"

    assert gate4_change_impact([path]) == "PRODUCTION_OR_UNKNOWN"
    assert (
        gate4_change_impact([path], reviewed_presentation_paths=[path])
        == "PRESENTATION_ONLY"
    )
    assert required_gate4_supervised_sessions("PRESENTATION_ONLY") == 0


def test_core_change_cannot_be_reclassified_as_presentation_only(tmp_path):
    root, source, target = _repo_with_change(
        tmp_path,
        "src/services/broker.py",
        "LIMIT = 1\n",
        "LIMIT = 2\n",
    )
    gate2, gate3 = _source_reports(source)
    gate1 = _target_gate1(target)
    manifest = _approve(
        build_change_manifest(
            source_gate2_report=gate2,
            source_gate3_report=gate3,
            target_gate1_report=gate1,
            root=root,
        )
    )

    report = build_carried_gate3_report(
        source_gate2_report=gate2,
        source_gate3_report=gate3,
        target_gate1_report=gate1,
        manifest=manifest,
        root=root,
    )

    assert manifest["impact"] == "PRODUCTION_OR_UNKNOWN"
    assert report["result"] == "FAILED"
    assert any(
        item["property"] == "carry_forward_impact"
        for item in report["invariant_violations"]
    )


def test_exact_patch_digest_tampering_fails_closed(tmp_path):
    root, source, target = _repo_with_change(
        tmp_path,
        "src/web/static/app.css",
        ".button { color: white; }\n",
        ".button { color: mintcream; }\n",
    )
    gate2, gate3 = _source_reports(source)
    gate1 = _target_gate1(target)
    manifest = build_change_manifest(
        source_gate2_report=gate2,
        source_gate3_report=gate3,
        target_gate1_report=gate1,
        root=root,
    )
    manifest["file_reviews"][0]["patch_sha256"] = "0" * 64
    manifest = _approve(manifest)

    report = build_carried_gate3_report(
        source_gate2_report=gate2,
        source_gate3_report=gate3,
        target_gate1_report=gate1,
        manifest=manifest,
        root=root,
    )

    assert report["result"] == "FAILED"
    assert any(
        item["property"] == "carry_forward_file_review"
        for item in report["invariant_violations"]
    )
