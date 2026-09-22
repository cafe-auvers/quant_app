"""Prepare, inspect, and finalize Gate-4 controlled-live evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from activation_gates.evidence import canonical_report_sha256
from activation_gates.requalification import normalized_changed_paths
from gate4.capabilities import load_verified_execution_capabilities
from gate4.collector import Gate4EvidenceCollector
from gate4.reporting import build_report
from src.infrastructure.database.engine import init_mysql_engine
from src.services.state_sync import get_live_trading_control
from src.utils.market_calendar import US_MARKET_ZONE, is_nyse_trading_day


ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_external_path(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        return resolved
    raise RuntimeError("Gate-4 runtime evidence must remain outside the repository")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _exact_release(gate3_report_path: Path) -> tuple[str, Mapping[str, Any]]:
    commit = _git("rev-parse", "HEAD").lower()
    if _git("status", "--porcelain"):
        raise RuntimeError("Gate 4 requires a clean exact-commit worktree")
    report = _load(gate3_report_path)
    if report.get("result") != "PASSED" or report.get("commit_sha") != commit:
        raise RuntimeError("Gate-3 report must be PASSED on this exact commit")
    return commit, report


def _collector(path: Path, commit: str) -> Gate4EvidenceCollector:
    return Gate4EvidenceCollector(
        journal_path=_require_external_path(path), commit_sha=commit
    )


def prepare(args: argparse.Namespace) -> int:
    commit, _gate3 = _exact_release(args.gate3_report)
    _require_external_path(args.capability_manifest)
    capability = load_verified_execution_capabilities(
        args.capability_manifest,
        expected_commit=commit,
    )
    collector = _collector(args.journal, commit)
    collector.record(
        "CAPABILITY_EVIDENCE_VERIFIED",
        verified=True,
        evidence_sha256=capability.sha256,
        capability_ids=list(capability.capability_ids),
        environment=capability.environment,
        account_ref=capability.account_ref,
    )
    print(
        "Gate 4 collector prepared. Configure the exact journal path and set "
        "GATE4_QUALIFICATION_ENABLED=true before the first supervised session."
    )
    return 0


def status(args: argparse.Namespace) -> int:
    commit = _git("rev-parse", "HEAD").lower()
    collector = _collector(args.journal, commit)
    audit = collector.journal.audit()
    print(
        json.dumps(
            {
                "commit_sha": commit,
                "journal": str(collector.journal.path),
                "audit_passed": audit.passed,
                "row_count": audit.row_count,
                "event_counts": audit.event_counts,
                "journal_sha256": audit.sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if audit.passed else 1


def start_session(args: argparse.Namespace) -> int:
    commit, _gate3 = _exact_release(args.gate3_report)
    session_day = date.fromisoformat(args.session_date)
    if not is_nyse_trading_day(session_day):
        raise RuntimeError(f"{session_day.isoformat()} is not an NYSE trading day")
    if datetime.now(US_MARKET_ZONE).date() != session_day:
        raise RuntimeError("Gate-4 session can only be started on its NYSE calendar date")
    supervisor = str(args.supervisor or "").strip()
    if len(supervisor) < 3:
        raise RuntimeError("Gate-4 session requires a named supervisor")
    collector = _collector(args.journal, commit)
    events = collector.journal.read_all()
    if not any(
        event.event_type == "CAPABILITY_EVIDENCE_VERIFIED"
        and event.payload.get("verified") is True
        for event in events
    ):
        raise RuntimeError("Run Gate-4 prepare before starting a session")
    if any(event.payload.get("session_date") == session_day.isoformat() for event in events):
        raise RuntimeError("Gate-4 session already has evidence for this date")

    engine = init_mysql_engine(ensure_schema=False)
    if engine is None:
        raise RuntimeError("Gate-4 session start requires canonical MySQL")
    try:
        control_result = get_live_trading_control(engine)
    finally:
        engine.dispose()
    if not control_result.success or control_result.control is None:
        raise RuntimeError(
            control_result.error or "Shared live-trading control is unavailable"
        )
    if control_result.control.enabled:
        raise RuntimeError("Gate-4 session must start with shared trading disabled")
    collector.record(
        "SESSION_STARTED",
        session_date=session_day.isoformat(),
        supervised=True,
        supervisor=supervisor,
        armed=False,
    )
    print(
        f"Gate 4 supervised session {session_day.isoformat()} started DISARMED; "
        "launch the exact-release runtime and arm only from the UI after ACTIVE."
    )
    return 0


def finalize(args: argparse.Namespace) -> int:
    commit, gate3 = _exact_release(args.gate3_report)
    for evidence_path in (
        args.capability_manifest,
        args.controlled_live_config,
        args.risk_limits,
    ):
        _require_external_path(evidence_path)
    if args.review is None:
        raise RuntimeError("Gate-4 finalization requires an independent review file")
    _require_external_path(args.review)
    capability = load_verified_execution_capabilities(
        args.capability_manifest,
        expected_commit=commit,
    )
    collector = _collector(args.journal, commit)
    review = _load(args.review)
    baseline_gate4 = None
    change_impact = None
    if args.baseline_gate4_report is not None or args.change_impact_manifest is not None:
        if args.baseline_gate4_report is None or args.change_impact_manifest is None:
            raise RuntimeError(
                "--baseline-gate4-report and --change-impact-manifest are required together"
            )
        _require_external_path(args.baseline_gate4_report)
        _require_external_path(args.change_impact_manifest)
        baseline_gate4 = _load(args.baseline_gate4_report)
        change_impact = _load(args.change_impact_manifest)
        baseline_commit = str(baseline_gate4.get("commit_sha") or "").strip().lower()
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", baseline_commit, commit],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if ancestor.returncode != 0:
            raise RuntimeError(
                "Gate-4 requalification baseline must be an ancestor of the target commit"
            )
        actual_changed_paths = normalized_changed_paths(
            _git("diff", "--name-only", baseline_commit, commit).splitlines()
        )
        recorded_changed_paths = normalized_changed_paths(
            change_impact.get("changed_paths", [])
            if isinstance(change_impact.get("changed_paths"), (list, tuple))
            else []
        )
        if not actual_changed_paths or actual_changed_paths != recorded_changed_paths:
            raise RuntimeError(
                "change-impact manifest paths do not exactly match the baseline-to-target Git diff"
            )
    evidence = collector.build_evidence(
        gate3_report_sha256=canonical_report_sha256(gate3),
        controlled_live_config_sha256=_sha256(args.controlled_live_config),
        risk_limits_sha256=_sha256(args.risk_limits),
        review=review,
    )
    if baseline_gate4 is not None and change_impact is not None:
        evidence["baseline_gate4_report_sha256"] = canonical_report_sha256(
            baseline_gate4
        )
        evidence["change_impact_manifest"] = dict(change_impact)
    if evidence.get("capability_evidence_sha256") != capability.sha256:
        raise RuntimeError("Gate-4 journal capability digest does not match the manifest")
    report = build_report(
        evidence,
        upstream_gate3_report=gate3,
        baseline_gate4_report=baseline_gate4,
        change_impact_manifest=change_impact,
    )
    _require_external_path(args.output)
    _write_json(args.output, report)
    evidence_path = args.output.with_name(f"{args.output.stem}_evidence.json")
    _write_json(evidence_path, evidence)
    print(
        f"Gate 4 {report['result']}: {len(report['invariant_violations'])} "
        f"violation(s); sessions={len(evidence['supervised_regular_session_dates'])}"
    )
    return 0 if report["result"] == "PASSED" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Gate-4 controlled-live evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--gate3-report", type=Path, required=True)
    prepare_parser.add_argument("--capability-manifest", type=Path, required=True)
    prepare_parser.add_argument("--journal", type=Path, required=True)
    prepare_parser.set_defaults(handler=prepare)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--journal", type=Path, required=True)
    status_parser.set_defaults(handler=status)

    start_parser = subparsers.add_parser("start-session")
    start_parser.add_argument("--gate3-report", type=Path, required=True)
    start_parser.add_argument("--journal", type=Path, required=True)
    start_parser.add_argument("--session-date", required=True)
    start_parser.add_argument("--supervisor", required=True)
    start_parser.set_defaults(handler=start_session)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--gate3-report", type=Path, required=True)
    finalize_parser.add_argument("--capability-manifest", type=Path, required=True)
    finalize_parser.add_argument("--journal", type=Path, required=True)
    finalize_parser.add_argument("--controlled-live-config", type=Path, required=True)
    finalize_parser.add_argument("--risk-limits", type=Path, required=True)
    finalize_parser.add_argument("--review", type=Path)
    finalize_parser.add_argument("--baseline-gate4-report", type=Path)
    finalize_parser.add_argument("--change-impact-manifest", type=Path)
    finalize_parser.add_argument("--output", type=Path, required=True)
    finalize_parser.set_defaults(handler=finalize)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_parser",
    "finalize",
    "main",
    "prepare",
    "start_session",
    "status",
]
