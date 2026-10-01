#!/usr/bin/env python3
"""Fail-closed post-session review/finalization for combined Gate 2/Gate 3."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PENDING_REVIEW_PROPERTY = "independent_review_approved"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _latest_matching_session(
    evidence_root: Path,
    *,
    session_date: str,
    commit_sha: str,
) -> Path:
    pointer = _read_json(evidence_root / "latest_session.json")
    candidates: list[Path] = []
    pointer_path = str(pointer.get("session_dir") or "").strip()
    if pointer_path:
        candidates.append(Path(pointer_path))
    candidates.extend(
        sorted(
            evidence_root.glob("gate2_*"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    )
    seen: set[Path] = set()
    root = evidence_root.resolve()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen or resolved.parent != root:
            continue
        seen.add(resolved)
        config = _read_json(resolved / "session_config.json")
        options = config.get("options") if isinstance(config.get("options"), dict) else {}
        if (
            config.get("mode") == "COMBINED_GATE2_GATE3"
            and str(config.get("commit_sha") or "").lower() == commit_sha.lower()
            and str(options.get("session_date") or "") == session_date
        ):
            return resolved
    raise RuntimeError("No matching combined Gate 2/Gate 3 session was found.")


def _validate_closure_authorization(
    path: Path,
    *,
    commit_sha: str,
) -> dict[str, Any]:
    approval = _read_json(path)
    scopes = {str(value) for value in approval.get("scope", [])}
    if str(approval.get("commit_sha") or "").lower() != commit_sha.lower():
        raise RuntimeError("Closure authorization does not match the qualification commit.")
    if not {"GATE_2_CONTINUITY", "GATE_3_SHADOW"}.issubset(scopes):
        raise RuntimeError("Closure authorization does not cover both Gate 2 and Gate 3.")
    if not str(approval.get("approver") or "").strip():
        raise RuntimeError("Closure authorization has no approver.")
    return approval


def _technical_review_basis(
    session_dir: Path,
    *,
    commit_sha: str,
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    gate2_path = session_dir / "gate2_report.json"
    gate3_path = session_dir / "gate3" / "gate3_report.json"
    evidence_path = session_dir / "gate3" / "gate3_evidence.json"
    session_path = session_dir / "session.json"
    gate2 = _read_json(gate2_path)
    gate3 = _read_json(gate3_path)
    evidence = _read_json(evidence_path)
    session = _read_json(session_path)

    if str(session.get("state") or "") not in {"PASSED", "FAILED"}:
        raise RuntimeError("The combined session has not reached a terminal report state.")
    if gate2.get("result") != "PASSED":
        raise RuntimeError(
            "Gate 2 did not pass; Gate 3 independent approval is forbidden."
        )
    if str(gate2.get("commit_sha") or "").lower() != commit_sha.lower():
        raise RuntimeError("Gate 2 report commit does not match the approved commit.")
    if str(gate3.get("commit_sha") or "").lower() != commit_sha.lower():
        raise RuntimeError("Gate 3 report commit does not match the approved commit.")
    violations = {
        str(item.get("property") or "")
        for item in gate3.get("invariant_violations", [])
        if isinstance(item, dict)
    }
    if violations != {PENDING_REVIEW_PROPERTY}:
        raise RuntimeError(
            "Gate 3 has unresolved technical violations beyond independent review: "
            + ", ".join(sorted(violations or {"missing violation detail"}))
        )
    if gate3.get("activation_state_changed") is not False:
        raise RuntimeError("Gate 3 unexpectedly changed activation state.")

    return {
        "schema_version": 1,
        "gate": "GATE_3_SHADOW_EXECUTION",
        "decision": "APPROVED",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": str(approval.get("approver") or "").strip(),
        "approval_source": str(approval.get("authorization") or "").strip(),
        "closure_authorization_sha256": _sha256(Path(approval["_path"])),
        "commit_sha": commit_sha,
        "session_dir": str(session_dir),
        "technical_findings": {
            "gate2_result": gate2.get("result"),
            "gate2_continuity_availability_percent": gate2.get(
                "continuity_availability_percent"
            ),
            "gate2_receive_lag_p95_ms": (
                gate2.get("receive_lag_ms", {}).get("p95")
                if isinstance(gate2.get("receive_lag_ms"), dict)
                else None
            ),
            "complete_regular_session_count": evidence.get(
                "complete_regular_session_count"
            ),
            "real_quote_evaluation_count": evidence.get(
                "real_quote_evaluation_count"
            ),
            "mutation_candidate_count": evidence.get("mutation_candidate_count"),
            "runtime_error_count": evidence.get("runtime_error_count"),
            "broker_mutation_attempt_count": evidence.get(
                "broker_mutation_attempt_count"
            ),
            "production_ledger_write_count": evidence.get(
                "production_ledger_write_count"
            ),
        },
        "artifact_sha256": {
            "gate2_report.json": _sha256(gate2_path),
            "gate3_report.pending_review.json": _sha256(gate3_path),
            "gate3_evidence.pending_review.json": _sha256(evidence_path),
            "gate3.evidence.jsonl": _sha256(
                session_dir / "gate3" / "gate3.evidence.jsonl"
            ),
            "gate3.shadow.jsonl": _sha256(
                session_dir / "gate3" / "gate3.shadow.jsonl"
            ),
        },
    }


def finalize(args: argparse.Namespace) -> Path:
    repository = args.repository.expanduser().resolve()
    evidence_root = args.evidence_root.expanduser().resolve()
    session_dir = _latest_matching_session(
        evidence_root,
        session_date=args.session_date,
        commit_sha=args.commit_sha,
    )
    approval = _validate_closure_authorization(
        args.closure_authorization,
        commit_sha=args.commit_sha,
    )
    approval["_path"] = str(args.closure_authorization.resolve())
    basis = _technical_review_basis(
        session_dir,
        commit_sha=args.commit_sha,
        approval=approval,
    )
    gate3_dir = session_dir / "gate3"
    basis_path = gate3_dir / "independent_review_basis.json"
    review_path = gate3_dir / "independent_review.json"
    _write_json(basis_path, basis)
    review = {
        "status": "APPROVED",
        "author": f"Gate 3 automated evidence collector ({args.commit_sha[:7]})",
        "reviewer": str(approval.get("approver") or "").strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reference": f"sha256:{_sha256(basis_path)}",
    }
    _write_json(review_path, review)

    config = _read_json(session_dir / "session_config.json")
    paths = config.get("paths") if isinstance(config.get("paths"), dict) else {}
    strategy_rules = Path(
        str(paths.get("gate3_strategy_rules") or repository / "rulebooks" / "technical_rules.md")
    )
    command = [
        sys.executable,
        str(repository / "scripts" / "run_gate3_shadow.py"),
        "--finalize-only",
        "--gate2-report",
        str(session_dir / "gate2_report.json"),
        "--capability-manifest",
        str(args.capability_manifest),
        "--session-date",
        args.session_date,
        "--symbols",
        ",".join(config.get("options", {}).get("symbols", ["RNG"])),
        "--output-dir",
        str(gate3_dir),
        "--strategy-rules",
        str(strategy_rules),
        "--review",
        str(review_path),
    ]
    completed = subprocess.run(
        command,
        cwd=str(repository),
        capture_output=True,
        text=True,
        check=False,
    )
    final_report = _read_json(gate3_dir / "gate3_report.json")
    if completed.returncode != 0 or final_report.get("result") != "PASSED":
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "Gate 3 finalization did not produce a passing report."
        )

    session_path = session_dir / "session.json"
    session = _read_json(session_path)
    session["gate3_result"] = "PASSED"
    session["gate3_qualification_state"] = "PASSED"
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(session_path, session)
    _write_json(
        gate3_dir / "finalization.json",
        {
            "schema_version": 1,
            "result": "PASSED",
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "commit_sha": args.commit_sha,
            "gate3_report_sha256": _sha256(gate3_dir / "gate3_report.json"),
            "independent_review_sha256": _sha256(review_path),
            "independent_review_basis_sha256": _sha256(basis_path),
        },
    )
    return session_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--capability-manifest", type=Path, required=True)
    parser.add_argument("--closure-authorization", type=Path, required=True)
    parser.add_argument("--status-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session_dir = finalize(args)
    except Exception as exc:
        if args.status_file is not None:
            _write_json(
                args.status_file,
                {
                    "schema_version": 1,
                    "result": "REFUSED",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "commit_sha": args.commit_sha,
                    "reason": str(exc),
                },
            )
        print(f"Gate 2/3 finalization refused: {exc}", file=sys.stderr)
        return 1
    if args.status_file is not None:
        _write_json(
            args.status_file,
            {
                "schema_version": 1,
                "result": "PASSED",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "commit_sha": args.commit_sha,
                "session_dir": str(session_dir),
            },
        )
    print(f"Gate 2/3 finalization passed: {session_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
