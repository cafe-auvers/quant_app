"""Inspect local Gate-2 prerequisites without connecting or changing files.

Exit zero means only that the inspected local prerequisites have no blockers.
It is never a Gate-2 pass, approval, or permission to trade.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXTERNAL_CHECKS = [
    "Authenticate the independent reviewer's identity and approval reference.",
    "Confirm credential/token file permissions, backup, free disk space and log retention.",
    "Confirm no other WebSocket client uses this app key; credentials are not authenticated here.",
    "Confirm host power, network, operator coverage and stop procedure.",
    "Live ACK/data coverage, notice mapping, reconnect, latency and full-session metrics require the actual soak.",
]

GATE2_MARKET_DATA_CONFIG_KEYS = frozenset({
    "BROKER_EVENT_STALE_SECONDS",
    "LOCAL_RECEIVE_STALE_SECONDS",
    "MAX_MARKET_DATA_QUEUE_DELAY_SECONDS",
    "MAX_FUTURE_BROKER_EVENT_SECONDS",
    "MAX_BROKER_CLOCK_SKEW_SECONDS",
    "QUOTE_STALE_AFTER_SECONDS",
})


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _redact(value: Any) -> Any:
    # Redact all configured credential values, including unusually short ones.
    secrets = sorted({
        val for key, val in os.environ.items()
        if val and re.search(r"SECRET|PASSWORD|TOKEN|APP_KEY|HTS_ID|CANO|ACCOUNT", key)
    }, key=len, reverse=True)

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED]")
            return item
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        return item

    return clean(value)


def check_readiness(
    args: argparse.Namespace, *, root: Path = ROOT, now: datetime | None = None
) -> dict:
    # Match the standalone runner's import order, without the startup migration.
    from src.utils.config import install_repository_configuration

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str, **data: Any) -> None:
        checks.append({"id": name, "status": "OK" if ok else "BLOCKED", "detail": detail, **data})

    try:
        install_repository_configuration()
    except (OSError, ValueError):
        check("repository_configuration", False, "Repository configuration could not be loaded; check read permissions and its JSON/schema. Dependent values may be unavailable.")

    from gate2.capabilities import (
        EXECUTION_NOTICE, REQUIRED_CAPABILITIES, load_verified_capability_manifest,
        sha256_file,
    )
    from gate2.reporting import (
        SAFE_RUNTIME_EXPECTATIONS, _session_bounds, clock_synchronization_status,
        runtime_activation_snapshot,
        validate_session_start,
    )
    from src.core import execution_config
    from src.services.kis_realtime_market_data import (
        KIS_WS_VERIFIED_TOTAL_SUBSCRIPTION_LIMIT, QUOTE_COLUMNS, TRADE_COLUMNS,
    )
    from src.services.kis_ws_symbol_keys import (
        DEFAULT_KIS_WS_SYMBOL_KEYS_FILE, KisWsSymbolKeysError, read_symbol_keys_file,
    )

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    commit = ""
    try:
        commit = _git(root, "rev-parse", "HEAD")
        dirty = _git(root, "status", "--porcelain")
        check("source_clean", not dirty, "Worktree must be clean on the reviewed qualification commit.", commit_sha=commit)
    except (OSError, subprocess.SubprocessError):
        check("source_clean", False, "Cannot establish Git commit and clean-worktree identity.")

    def evidence_file(name: str, path: Path, *, require_external: bool = False) -> bool:
        try:
            path = path.expanduser().resolve()
            external = not path.is_relative_to(root.resolve())
            nonempty = path.is_file() and path.stat().st_size > 0
            digest = sha256_file(path) if nonempty else None
            valid = nonempty and (external or not require_external)
            detail = "Evidence must be a nonempty readable file."
            if require_external:
                detail += " Redacted captures must remain outside the repository."
            check(name, valid, detail, external=external, nonempty=nonempty, sha256=digest)
            return valid
        except OSError:
            check(name, False, "Evidence file cannot be inspected.")
            return False

    evidence_file("gate1_file", args.gate1_report)
    try:
        gate1 = _json_object(args.gate1_report)
        exact_pass = bool(commit) and gate1.get("commit_sha") == commit and gate1.get("result") == "PASSED"
        result = gate1.get("result")
        result = result if isinstance(result, str) and result in {"PASSED", "FAILED"} else "OTHER_OR_MISSING"
        check("gate1_exact_commit", exact_pass, "Gate-1 must report PASSED on the current exact commit.", result=result, commit_matches=bool(commit) and gate1.get("commit_sha") == commit)
        scenarios = gate1.get("scenarios")
        complete = (
            gate1.get("schema_version") == 2
            and gate1.get("gate") == "GATE_1_DETERMINISTIC_SIMULATION"
            and gate1.get("pytest_exit_code") == 0
            and gate1.get("invariant_violations") == []
            and isinstance(scenarios, list) and bool(scenarios)
            and gate1.get("scenario_count") == len(scenarios)
            and all(isinstance(item, dict) and item.get("result") == "PASSED"
                    and item.get("scenario_id") and item.get("group")
                    and "UNCLASSIFIED" not in str(item["group"]).upper()
                    for item in scenarios)
        )
        check("gate1_report_completeness", bool(complete), "Schema-2 Gate-1 evidence needs successful pytest, zero invariants and a complete list of passing, classified scenarios.")
    except (OSError, ValueError):
        check("gate1_exact_commit", False, "Gate-1 report is missing, unreadable or not a JSON object.")

    try:
        activation = runtime_activation_snapshot()
        for key, expected in SAFE_RUNTIME_EXPECTATIONS.items():
            actual = activation.get(key)
            check(f"runtime.{key}", actual == expected, "Read-only Gate-2 runtime requirement.", actual=actual, expected=expected)
    except (OSError, ValueError, TypeError):
        check("runtime_snapshot", False, "Runtime activation snapshot could not be read.")
    relevant_keys = set(SAFE_RUNTIME_EXPECTATIONS) | GATE2_MARKET_DATA_CONFIG_KEYS
    issues: list[str] = []
    unrelated_issues: list[str] = []
    for issue in execution_config.configuration_issues():
        key = issue.partition(":")[0].strip()
        if key in relevant_keys or key.startswith("KIS_WS_"):
            issues.append(issue)
        else:
            unrelated_issues.append(issue)
    check(
        "configuration_values", not issues,
        "Invalid Gate-2 activation or market-data overrides block preflight; unrelated overrides are informational.",
        issues=issues, unrelated_issues=unrelated_issues,
    )
    clock_status = clock_synchronization_status()
    check(
        "clock_synchronization",
        bool(clock_status.get("synchronized")),
        "Windows clock must be synchronized to an NTP source before a Gate-2 soak.",
        **clock_status,
    )

    symbols = sorted({item.strip().upper() for item in args.symbols.split(",") if item.strip()})
    symbols_valid = bool(symbols) and all(re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", symbol) for symbol in symbols)
    check("symbols", symbols_valid, "Provide at least one valid critical symbol.")
    requested_slots = len(symbols) * 2 + 1
    capacity = execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY
    check("aggregate_capacity", requested_slots <= capacity <= KIS_WS_VERIFIED_TOTAL_SUBSCRIPTION_LIMIT, "Trade + quote + notice registrations must fit the verified aggregate limit.", requested=requested_slots, configured=capacity, verified_limit=KIS_WS_VERIFIED_TOTAL_SUBSCRIPTION_LIMIT)
    try:
        keys = read_symbol_keys_file(DEFAULT_KIS_WS_SYMBOL_KEYS_FILE)
        missing = sorted(set(symbols) - set(keys))
        check("symbol_keys", not missing, "Each requested symbol needs a validated local subscription key; broker correctness requires WS0 evidence.", missing=missing, configured_symbol_count=len(keys))
    except (OSError, KisWsSymbolKeysError):
        check("symbol_keys", False, "The dedicated symbol-key file is missing or invalid; run manage_kis_ws_symbol_keys.py validate.")

    for key in (f"KIS_{args.environment}_APP_KEY", f"KIS_{args.environment}_APP_SECRET", "KIS_WS_HTS_ID"):
        present = bool(os.environ.get(key, "").strip())
        check(f"credential.{key}", present, "Credential presence only; value and validity are not reported.", present=present)
    for suffix, schemes in (("BASE_URL", {"https", "http"}), ("WS_URL", {"wss", "ws"})):
        key = f"KIS_{args.environment}_{suffix}"
        try:
            endpoint = urlsplit(os.environ.get(key, "").strip())
            valid = endpoint.scheme in schemes and bool(endpoint.hostname) and not endpoint.username and not endpoint.password
        except ValueError:
            valid = False
        check(f"endpoint.{key}", valid, "An endpoint with the expected scheme and host must be configured; no connection is made.")

    poll = args.poll_seconds
    watchdog = args.watchdog_timeout_seconds
    poll_ok = math.isfinite(poll) and 0 < poll <= 0.25
    check("poll_interval", poll_ok, "Poll interval must be finite and in (0, 0.25] seconds.")
    check("watchdog_timeout", math.isfinite(watchdog) and poll_ok and watchdog > 2 * poll, "Watchdog timeout must be finite and exceed two poll intervals.")
    stale = max(execution_config.BROKER_EVENT_STALE_SECONDS, execution_config.LOCAL_RECEIVE_STALE_SECONDS)
    check("stale_budget", poll_ok and math.isfinite(stale) and stale > 0 and stale + poll <= 3, "Largest stale budget plus polling must fit the 3-second Gate-2 limit.", broker_event_seconds=execution_config.BROKER_EVENT_STALE_SECONDS, local_receive_seconds=execution_config.LOCAL_RECEIVE_STALE_SECONDS, poll_seconds=poll if math.isfinite(poll) else None)

    session: dict = {}
    try:
        day = date.fromisoformat(args.session_date)
        try:
            opened, closed = validate_session_start(day, current)
            session_not_started = True
        except RuntimeError:
            opened, closed = _session_bounds(day)
            session_not_started = False
        check("full_session_not_started", session_not_started, "A full regular session requires launching before its open.", open_utc=opened.isoformat(), close_utc=closed.isoformat())
        start = datetime.fromisoformat(args.start_at.replace("Z", "+00:00")) if args.start_at else current
        if start.tzinfo is None:
            raise ValueError("planned start must have a timezone")
        start = start.astimezone(timezone.utc)
        check("planned_start", current <= start < opened, "Planned start must be now or later and strictly before the session opens.")
        session = {"date": args.session_date, "open_utc": opened.isoformat(), "close_utc": closed.isoformat(), "planned_start_utc": start.isoformat(), "offset_basis": "runner start, not market open"}
        drills = [(f"reconnect_{index + 1}", offset, 10.0) for index, offset in enumerate(args.reconnect_after_seconds or [3600.0])]
        drills.append(("silent_stale", args.silent_stale_probe_after_seconds, 10.0))
        for name, offset, recovery_margin in drills:
            finite = math.isfinite(offset) and offset > 0
            at = start + timedelta(seconds=offset) if finite and offset < 1e9 else None
            fits = at is not None and opened < at and at + timedelta(seconds=recovery_margin) < closed
            check(f"drill.{name}", fits, "Schedule after open and leave at least 10 seconds before close; live readiness can delay the drill.", offset_seconds=offset if math.isfinite(offset) else None, scheduled_utc=at.isoformat() if at else None)
    except (ValueError, OverflowError):
        check("session_calendar", False, "Use a valid NYSE trading date and a timezone-aware --start-at, if supplied.")

    evidence_file("capability_manifest_file", args.capability_manifest)
    try:
        payload = _json_object(args.capability_manifest)
        entries = payload.get("capabilities")
        entries = entries if isinstance(entries, list) else []
        for capability_id in [*REQUIRED_CAPABILITIES, EXECUTION_NOTICE]:
            matches = [item for item in entries if isinstance(item, dict) and item.get("capability_id") == capability_id]
            check(f"capability.{capability_id}", len(matches) == 1 and str(matches[0].get("status", "")).upper() == "VERIFIED", "Exactly one VERIFIED entry is required; strict evidence validation is checked separately.")
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                continue
            relative = Path(str(item.get("evidence_file") or ""))
            safe = bool(item.get("evidence_file")) and not relative.is_absolute() and ".." not in relative.parts
            if not safe:
                check(f"capability_evidence.{index}", False, "Manifest evidence paths must be safe relative bundle paths.")
                continue
            evidence_path = (args.capability_manifest.parent / relative).resolve()
            if not evidence_path.is_relative_to(args.capability_manifest.parent.resolve()):
                check(f"capability_evidence.{index}", False, "Manifest evidence path escapes its bundle.")
                continue
            if evidence_file(f"capability_evidence.{index}", evidence_path):
                check(f"capability_digest.{index}", sha256_file(evidence_path) == str(item.get("evidence_sha256") or "").lower(), "Manifest evidence SHA-256 must match its file.")
        manifest = load_verified_capability_manifest(args.capability_manifest, expected_commit=commit, expected_environment=args.environment)
        supported_fields = {"HDFSCNT0": set(TRADE_COLUMNS), "HDFSASP0": set(QUOTE_COLUMNS)}
        sequence_supported = all(field in supported_fields.get(channel, set()) for channel, field in manifest.sequence_field_by_channel.items())
        check("capability_manifest", sequence_supported, "Strict manifest review, exact commit, environment, digests and parser sequence fields must validate.", sha256=manifest.sha256)
    except (OSError, ValueError) as exc:
        check("capability_manifest", False, str(exc))

    names: set[str] = set()
    for index, path in enumerate(args.redacted_evidence):
        evidence_file(f"redacted_evidence.{index}", path, require_external=True)
        check(f"redacted_evidence_name.{index}", path.name not in names, "Redacted evidence basenames must be unique.")
        names.add(path.name)
    check("redacted_evidence_supplied", bool(args.redacted_evidence), "At least one redacted evidence file is required.")
    blockers = [item["id"] for item in checks if item["status"] == "BLOCKED"]
    return _redact({
        "schema_version": 1,
        "kind": "GATE2_LOCAL_PREFLIGHT",
        "status": "LOCAL_BLOCKERS" if blockers else "LOCAL_CHECKS_COMPLETE",
        "gate2_pass": False,
        "scope": "Local inspection only. No live services were created; this is not operational readiness or approval.",
        "checked_at": current.isoformat(),
        "commit_sha": commit,
        "session": session,
        "checks": checks,
        "blockers": blockers,
        "external_checks_not_performed": EXTERNAL_CHECKS,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate1-report", type=Path, required=True)
    parser.add_argument("--capability-manifest", type=Path, required=True)
    parser.add_argument("--redacted-evidence", type=Path, action="append", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--environment", choices=("PROD", "SIM"), default="PROD")
    parser.add_argument("--start-at", help="Planned runner launch in timezone-aware ISO-8601; defaults to now. Does not schedule a launch.")
    parser.add_argument("--reconnect-after-seconds", type=float, action="append")
    parser.add_argument("--silent-stale-probe-after-seconds", type=float, default=5400.0)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument("--watchdog-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON to stdout.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = check_readiness(args)
    if args.json:
        print(json.dumps(report, indent=2, allow_nan=False))
    else:
        print(f"Gate 2 local preflight: {report['status']} ({len(report['blockers'])} blockers)")
        print(report["scope"])
        for item in report["checks"]:
            details = {key: value for key, value in item.items() if key not in {"id", "status", "detail"}}
            suffix = f" {json.dumps(details, allow_nan=False)}" if details else ""
            print(f"[{item['status']}] {item['id']}: {item['detail']}{suffix}")
        for detail in report["external_checks_not_performed"]:
            print(f"[UNCHECKED] {detail}")
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
