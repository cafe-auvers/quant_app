from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.finalize_combined_gate23 import (
    _latest_matching_session,
    _technical_review_basis,
)


COMMIT = "7" * 40


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_session(tmp_path: Path, *, gate2_result: str = "PASSED") -> Path:
    session_dir = tmp_path / "gate2_20260930_222020"
    _write_json(
        session_dir / "session_config.json",
        {
            "mode": "COMBINED_GATE2_GATE3",
            "commit_sha": COMMIT,
            "options": {"session_date": "2026-09-30", "symbols": ["RNG"]},
        },
    )
    _write_json(session_dir / "session.json", {"state": gate2_result})
    _write_json(
        session_dir / "gate2_report.json",
        {
            "result": gate2_result,
            "commit_sha": COMMIT,
            "continuity_availability_percent": 100.0,
            "receive_lag_ms": {"p95": 200.0},
        },
    )
    _write_json(
        session_dir / "gate3" / "gate3_report.json",
        {
            "result": "FAILED",
            "commit_sha": COMMIT,
            "activation_state_changed": False,
            "invariant_violations": [
                {"property": "independent_review_approved"}
            ],
        },
    )
    _write_json(
        session_dir / "gate3" / "gate3_evidence.json",
        {
            "complete_regular_session_count": 1,
            "real_quote_evaluation_count": 5,
            "mutation_candidate_count": 2,
            "runtime_error_count": 0,
            "broker_mutation_attempt_count": 0,
            "production_ledger_write_count": 0,
        },
    )
    for name in ("gate3.evidence.jsonl", "gate3.shadow.jsonl"):
        (session_dir / "gate3" / name).write_text("{}\n", encoding="utf-8")
    return session_dir


def test_latest_matching_session_selects_combined_exact_commit(tmp_path):
    session_dir = _build_session(tmp_path)
    _write_json(tmp_path / "latest_session.json", {"session_dir": str(session_dir)})

    assert _latest_matching_session(
        tmp_path,
        session_date="2026-09-30",
        commit_sha=COMMIT,
    ) == session_dir.resolve()


def test_technical_review_basis_accepts_only_pending_review(tmp_path):
    session_dir = _build_session(tmp_path)
    authorization = tmp_path / "authorization.json"
    authorization.write_text("{}", encoding="utf-8")

    basis = _technical_review_basis(
        session_dir,
        commit_sha=COMMIT,
        approval={
            "_path": str(authorization),
            "approver": "owner",
            "authorization": "conditional approval",
        },
    )

    assert basis["decision"] == "APPROVED"
    assert basis["technical_findings"]["gate2_result"] == "PASSED"


def test_technical_review_basis_refuses_failed_gate2(tmp_path):
    session_dir = _build_session(tmp_path, gate2_result="FAILED")
    authorization = tmp_path / "authorization.json"
    authorization.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Gate 2 did not pass"):
        _technical_review_basis(
            session_dir,
            commit_sha=COMMIT,
            approval={
                "_path": str(authorization),
                "approver": "owner",
                "authorization": "conditional approval",
            },
        )
