"""Audit captured session evidence without broker/database access or arming."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def audit_session(source: Path, *, session_date: str, commit_sha: str) -> dict:
    from activation_gates.journal import AppendOnlyEvidenceJournal
    from gate3.collector import GATE3_EVIDENCE_EVENTS, GATE3_NAME

    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "session_date": session_date,
              "expected_commit": commit_sha, "broker_calls": 0, "canonical_writes": 0,
              "activation_state_changes": 0, "formal_gate_result": "NOT_CERTIFIED"}
    try:
        report = json.loads((source / "checks_report.json").read_text(encoding="utf-8"))
        if report["session_date"] != session_date or report["commit_sha"] != commit_sha:
            raise ValueError("Evidence belongs to a different session or release")
        audits = {}
        for name, gate, allowed in (
            ("live_checks.evidence.jsonl", "PASSIVE_LIVE_SESSION_DIAGNOSTICS",
             ("COLLECTOR_STARTED", "FEED_SAMPLE", "LIVE_EVENT", "COLLECTOR_ERROR", "COLLECTOR_ENDED")),
            ("shadow.evidence.jsonl", GATE3_NAME, GATE3_EVIDENCE_EVENTS),
        ):
            path = source / name
            if not path.is_file() or not path.stat().st_size:
                raise ValueError("Required evidence journal is missing or empty: " + name)
            journal = AppendOnlyEvidenceJournal(path, gate=gate, commit_sha=commit_sha, allowed_event_types=allowed)
            audit = journal.audit()
            audits[name] = {**asdict(audit), "integrity_passed": audit.passed}
        complete = bool(all(audit["integrity_passed"] for audit in audits.values())
            and report["collector_state"] == "ENDED" and report["full_regular_session_observed"]
            and not report["collector_errors"] and not report["dropped_batches"] and not report["queue_depth"])
        result.update(collection_status="COMPLETE_PASSIVE_COLLECTION" if complete else "INCOMPLETE_PASSIVE_COLLECTION",
            collector_report=report, journal_audits=audits,
            formal_gate_missing_requirements={key: report[key]["missing_qualification"] for key in ("gate2", "gate3", "gate4")})
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(collection_status="COLLECTION_MISSING_OR_INVALID", error_type=type(error).__name__, error=str(error))
    return result


def main(argv=None) -> int:
    from src.utils.config import get_env_value
    from src.utils.market_calendar import US_MARKET_ZONE, is_nyse_trading_day, nyse_regular_session_close_time, previous_nyse_trading_day

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-date")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    now = datetime.now(timezone.utc).astimezone(US_MARKET_ZONE)
    day = date.fromisoformat(args.session_date) if args.session_date else now.date()
    if not args.session_date and (not is_nyse_trading_day(day) or now.time() < nyse_regular_session_close_time(day)):
        day = previous_nyse_trading_day(day - timedelta(days=1))
    source = args.evidence_dir or Path(get_env_value("LIVE_SESSION_EVIDENCE_DIR", ""))
    if args.evidence_dir is None and str(get_env_value("LIVE_SESSION_DATE", "auto")).strip().lower() in {"", "auto"}:
        source = source / (day.isoformat() + "_" + commit[:12])
    result = audit_session(source, session_date=day.isoformat(), commit_sha=commit)
    output = args.output or source / "post_session_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("session_date", "expected_commit", "collection_status", "formal_gate_result")}))
    return 0 if result["collection_status"] == "COMPLETE_PASSIVE_COLLECTION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
