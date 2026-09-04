"""Launch and inspect an unattended Gate-2 read-only soak session.

The detached worker keeps the Windows system awake while allowing the display,
terminal, dashboard, and Codex session to close.  All mutable runtime artifacts
live outside the repository in a unique evidence directory.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_ROOT = Path.home() / "quant_evidence" / "gate2_sessions"
TERMINAL_STATES = frozenset({"PASSED", "FAILED", "COMPLETED", "ERROR"})
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

BLOCKER_GUIDANCE = {
    "preflight_failed": "Correct the recorded fail-closed preflight error before creating a new session.",
    "full_regular_session": "Start before the NYSE regular-session open and run through the full close.",
    "critical_subscription_ack": "Resolve every missing trade, quote, or execution-notice subscription ACK.",
    "aggregate_registration_budget": "Reduce requested registrations or correct the reviewed aggregate capacity.",
    "market_data_frame_coverage": "Verify the symbol keys and live HDFSCNT0/HDFSASP0 data mapping.",
    "parser_failures": "Inspect the runtime log and fix or review every malformed or rejected frame.",
    "unhandled_disconnects": "Classify unexpected disconnects and ensure every reconnect is fully re-ACKed.",
    "critical_ack_recovery_seconds": "Fix reconnect replay so all critical subscriptions recover in under 10 seconds.",
    "stale_detection_seconds": "Fix the connected silent-channel probe so staleness is detected and recovers within 3 seconds.",
    "stale_entry_readiness_fence": "Ensure stale quotes are rejected by the instrumented entry-readiness boundary.",
    "duplicate_subscription_corruption": "Fix invalid or duplicate subscribe/unsubscribe protocol transitions.",
    "synthetic_stop_breaches": "Fix the live accumulator so every injected stop breach is latched and consumed.",
    "watchdog_deadlocks": "Investigate sampling-loop stalls recorded by the independent watchdog.",
    "full_session_continuity": "Eliminate unexplained critical-feed unready samples across the regular session.",
    "receive_lag_p95_ms": "Bring broker-event receive-lag p95 below 1 second.",
    "receive_lag_p99_ms": "Bring broker-event receive-lag p99 below 2 seconds.",
    "queue_lag_p99_ms": "Bring queue-lag p99 within the configured decision deadline.",
    "secret_leaks": "Review the redacted log scan and eliminate every credential or approval-key leak.",
    "broker_mutations": "Restore the initialized broker-boundary audit and keep mutation attempts at zero.",
    "runtime_activation_fence": "Restore the exact read-only Gate-2 activation snapshot before retrying.",
    "capability_manifest": "Obtain an independent approval for the exact-commit, digest-pinned capability manifest.",
    "timestamp_evidence": "Complete reviewed timestamp evidence for both live trade and quote channels.",
    "sequence_semantics": "Make runtime sequence enforcement match the reviewed channel findings exactly.",
    "execution_notice_evidence": "Capture and independently review one genuine decrypted H0GSCNI0 account notice.",
    "redacted_evidence_bundle": "Provide at least one nonempty redacted evidence file outside the repository.",
    "execution_notice_not_observed": "Arrange one genuine external account receipt, accepted, cancelled, or fill event while the read-only H0GSCNI0 collector is connected.",
    "execution_notice_ack_incomplete": "Fix the H0GSCNI0 subscription ACK or missing encryption key/IV before retrying.",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
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
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _git_commit() -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError(
            "Gate-2 unattended sessions require a clean exact-commit worktree"
        )
    return commit


def _require_external_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return resolved
    raise RuntimeError("Gate-2 session evidence must remain outside the repository")


def _require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise RuntimeError(f"{label} is missing or empty: {resolved}")
    return resolved


def _require_external_file(path: Path, label: str) -> Path:
    resolved = _require_file(path, label)
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return resolved
    raise RuntimeError(f"{label} must remain outside the repository: {resolved}")


def _resolve_python_executable(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    discovered = shutil.which(value)
    if discovered:
        return str(Path(discovered).resolve())
    raise RuntimeError(f"Python executable was not found: {value}")


def _runner_command(config: Mapping[str, Any]) -> list[str]:
    paths = config["paths"]
    options = config["options"]
    if config.get("mode") == "NOTICE":
        return [
            str(config["python_executable"]),
            str(REPO_ROOT / "scripts" / "capture_kis_ws_notice_evidence.py"),
            "--confirm-read-only",
            "--timeout-seconds",
            str(options["timeout_seconds"]),
            "--status-seconds",
            str(options["status_seconds"]),
            "--status-output",
            str(paths["live_status"]),
            "--output",
            str(paths["notice_evidence"]),
        ]
    command = [
        str(config["python_executable"]),
        str(REPO_ROOT / "scripts" / "run_gate2_soak.py"),
        "--confirm-read-only",
        "--environment",
        str(options["environment"]),
        "--symbols",
        ",".join(options["symbols"]),
        "--session-date",
        str(options["session_date"]),
        "--gate1-report",
        str(paths["gate1_report"]),
        "--capability-manifest",
        str(paths["capability_manifest"]),
        "--silent-stale-probe-after-seconds",
        str(options["silent_stale_probe_after_seconds"]),
        "--poll-seconds",
        str(options["poll_seconds"]),
        "--watchdog-timeout-seconds",
        str(options["watchdog_timeout_seconds"]),
        "--status-seconds",
        str(options["status_seconds"]),
        "--log-output",
        str(paths["runtime_log"]),
        "--status-output",
        str(paths["live_status"]),
        "--output",
        str(paths["report"]),
    ]
    for value in options["reconnect_after_seconds"]:
        command.extend(["--reconnect-after-seconds", str(value)])
    for value in paths["redacted_evidence"]:
        command.extend(["--redacted-evidence", str(value)])
    return command


def _set_keep_awake(enabled: bool) -> bool:
    if os.name != "nt":
        return False
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except (AttributeError, OSError):
        return False


def _process_alive(pid: object) -> bool:
    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric_pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, numeric_pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(numeric_pid, 0)
    except OSError:
        return False
    return True


def _session_payload(
    *,
    session_dir: Path,
    state: str,
    worker_pid: int | None,
    started_at: str,
    keep_awake: bool = False,
    mode: str = "SOAK",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "state": state,
        "session_dir": str(session_dir),
        "worker_pid": worker_pid,
        "started_at": started_at,
        "updated_at": _utc_now(),
        "ended_at": None,
        "keep_awake_active": keep_awake,
        "runner_exit_code": None,
        "result": None,
        "blockers": [],
        "failed_metrics": [],
        "artifacts": {
            "live_status": str(session_dir / "live_status.json"),
            "report": str(session_dir / "gate2_report.json"),
            "runtime_log": str(session_dir / "gate2_runtime.log"),
            "supervisor_log": str(session_dir / "supervisor.log"),
            "configuration": str(session_dir / "session_config.json"),
            "notice_evidence": str(session_dir / "notice_evidence.json"),
        },
    }


def _launch_configured_session(
    *,
    session_dir: Path,
    evidence_root: Path,
    config: Mapping[str, Any],
    started_at: str,
) -> Path:
    config_path = session_dir / "session_config.json"
    session_path = session_dir / "session.json"
    mode = str(config.get("mode") or "SOAK")
    _atomic_write_json(config_path, config)
    _atomic_write_json(
        session_path,
        _session_payload(
            session_dir=session_dir,
            state="STARTING",
            worker_pid=None,
            started_at=started_at,
            mode=mode,
        ),
    )
    worker_command = [
        str(config["python_executable"]),
        str(Path(__file__).resolve()),
        "_worker",
        "--config",
        str(config_path),
    ]
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    try:
        with (session_dir / "supervisor.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                worker_command,
                cwd=REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=creationflags,
            )
    except Exception as exc:
        session = _read_json(session_path)
        session.update(
            {
                "state": "ERROR",
                "updated_at": _utc_now(),
                "ended_at": _utc_now(),
                "blockers": ["detached_worker_launch_failed"],
                "error": {
                    "type": type(exc).__name__,
                    "fingerprint": hashlib.sha256(str(exc).encode()).hexdigest(),
                },
            }
        )
        _atomic_write_json(session_path, session)
        raise
    current = _read_json(session_path)
    if current.get("state") == "STARTING":
        current["worker_pid"] = process.pid
        current["updated_at"] = _utc_now()
        _atomic_write_json(session_path, current)
    _atomic_write_json(
        evidence_root / "latest_session.json",
        {"session_dir": str(session_dir), "updated_at": _utc_now()},
    )
    return session_dir


def _create_session(args: argparse.Namespace) -> Path:
    if not args.confirm_read_only:
        raise RuntimeError("--confirm-read-only is required")
    commit = _git_commit()
    evidence_root = _require_external_root(args.evidence_root)
    gate1 = _require_file(args.gate1_report, "Gate-1 report")
    manifest = _require_file(args.capability_manifest, "capability manifest")
    redacted = [
        _require_external_file(path, "redacted evidence")
        for path in args.redacted_evidence
    ]
    symbols = sorted(
        {value.strip().upper() for value in args.symbols.split(",") if value.strip()}
    )
    if not symbols:
        raise RuntimeError("--symbols must contain at least one symbol")
    evidence_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = evidence_root / (
        f"gate2_{args.session_date}_{commit[:7]}_{stamp}_{uuid.uuid4().hex[:8]}"
    )
    session_dir.mkdir()
    started_at = _utc_now()
    python_executable = _resolve_python_executable(args.python_executable)
    paths = {
        "gate1_report": str(gate1),
        "capability_manifest": str(manifest),
        "redacted_evidence": [str(path) for path in redacted],
        "runtime_log": str(session_dir / "gate2_runtime.log"),
        "live_status": str(session_dir / "live_status.json"),
        "report": str(session_dir / "gate2_report.json"),
    }
    config = {
        "schema_version": 1,
        "mode": "SOAK",
        "created_at": started_at,
        "commit_sha": commit,
        "repo_root": str(REPO_ROOT),
        "python_executable": python_executable,
        "paths": paths,
        "options": {
            "environment": args.environment,
            "symbols": symbols,
            "session_date": args.session_date,
            "reconnect_after_seconds": args.reconnect_after_seconds,
            "silent_stale_probe_after_seconds": (args.silent_stale_probe_after_seconds),
            "poll_seconds": args.poll_seconds,
            "watchdog_timeout_seconds": args.watchdog_timeout_seconds,
            "status_seconds": args.status_seconds,
        },
    }
    return _launch_configured_session(
        session_dir=session_dir,
        evidence_root=evidence_root,
        config=config,
        started_at=started_at,
    )


def _create_notice_session(args: argparse.Namespace) -> Path:
    if not args.confirm_read_only:
        raise RuntimeError("--confirm-read-only is required")
    commit = _git_commit()
    evidence_root = _require_external_root(args.evidence_root)
    evidence_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = evidence_root / (
        f"gate2_notice_{commit[:7]}_{stamp}_{uuid.uuid4().hex[:8]}"
    )
    session_dir.mkdir()
    started_at = _utc_now()
    python_executable = _resolve_python_executable(args.python_executable)
    config = {
        "schema_version": 1,
        "mode": "NOTICE",
        "created_at": started_at,
        "commit_sha": commit,
        "repo_root": str(REPO_ROOT),
        "python_executable": python_executable,
        "paths": {
            "notice_evidence": str(session_dir / "notice_evidence.json"),
            "live_status": str(session_dir / "live_status.json"),
        },
        "options": {
            "timeout_seconds": args.timeout_seconds,
            "status_seconds": args.status_seconds,
        },
    }
    return _launch_configured_session(
        session_dir=session_dir,
        evidence_root=evidence_root,
        config=config,
        started_at=started_at,
    )


def _run_worker(config_path: Path) -> int:
    config = _read_json(config_path)
    if not config:
        raise RuntimeError(f"unreadable Gate-2 session configuration: {config_path}")
    session_dir = config_path.resolve().parent
    session_path = session_dir / "session.json"
    session = _read_json(session_path) or _session_payload(
        session_dir=session_dir,
        state="STARTING",
        worker_pid=None,
        started_at=_utc_now(),
        mode=str(config.get("mode") or "SOAK"),
    )
    keep_awake = _set_keep_awake(True)
    session.update(
        {
            "state": "RUNNING",
            "worker_pid": os.getpid(),
            "updated_at": _utc_now(),
            "keep_awake_active": keep_awake,
        }
    )
    _atomic_write_json(session_path, session)
    exit_code = 1
    try:
        exit_code = subprocess.run(_runner_command(config), cwd=REPO_ROOT).returncode
        live_status = _read_json(Path(config["paths"]["live_status"]))
        if config.get("mode") == "NOTICE":
            evidence = _read_json(Path(config["paths"]["notice_evidence"]))
            ack = evidence.get("subscription_acknowledgement", {})
            notice_observed = bool(evidence.get("notice_observation"))
            blockers = []
            if not (
                ack.get("accepted")
                and ack.get("encryption_key_present")
                and ack.get("encryption_iv_present")
            ):
                blockers.append("execution_notice_ack_incomplete")
            if not notice_observed:
                blockers.append("execution_notice_not_observed")
            if evidence:
                state = "COMPLETED"
                result = "NOTICE_CAPTURED" if not blockers else "INCOMPLETE"
            else:
                state = "ERROR"
                result = ""
                blockers = ["runner_exited_without_report"]
            failed_metrics = []
        else:
            report = _read_json(Path(config["paths"]["report"]))
            result = str(report.get("result") or live_status.get("result") or "")
            if result == "PASSED" and exit_code == 0:
                state = "PASSED"
            elif report:
                state = "FAILED"
            else:
                state = "ERROR"
            if report:
                blockers = list(report.get("blockers", []))
            elif live_status.get("state") == "PREFLIGHT_FAILED":
                blockers = ["preflight_failed"]
            else:
                blockers = ["runner_exited_without_report"]
            failed_metrics = list(live_status.get("failed_metrics", []))
        session.update(
            {
                "state": state,
                "runner_exit_code": exit_code,
                "result": result or None,
                "blockers": blockers,
                "failed_metrics": failed_metrics,
            }
        )
    except BaseException as exc:
        exit_code = 1
        session.update(
            {
                "state": "ERROR",
                "runner_exit_code": None,
                "result": None,
                "blockers": ["session_worker_failed"],
                "error": {
                    "type": type(exc).__name__,
                    "fingerprint": hashlib.sha256(
                        f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace")
                    ).hexdigest(),
                },
            }
        )
        raise
    finally:
        _set_keep_awake(False)
        session.update(
            {
                "updated_at": _utc_now(),
                "ended_at": _utc_now(),
                "keep_awake_active": False,
            }
        )
        _atomic_write_json(session_path, session)
    return exit_code


def _latest_session(evidence_root: Path) -> Path:
    evidence_root = evidence_root.expanduser().resolve()
    pointer = _read_json(evidence_root / "latest_session.json")
    candidate = Path(str(pointer.get("session_dir") or ""))
    if candidate.is_dir():
        return candidate
    directories = sorted(
        (path for path in evidence_root.glob("gate2_*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not directories:
        raise RuntimeError(f"no Gate-2 sessions found under {evidence_root}")
    return directories[0]


def summarize_session(session_dir: Path) -> dict[str, Any]:
    session_dir = session_dir.expanduser().resolve()
    session = _read_json(session_dir / "session.json")
    if not session:
        raise RuntimeError(f"session.json is missing or unreadable: {session_dir}")
    live = _read_json(session_dir / "live_status.json")
    report = _read_json(session_dir / "gate2_report.json")
    notice_evidence = _read_json(session_dir / "notice_evidence.json")
    alive = _process_alive(session.get("worker_pid"))
    recorded_state = str(session.get("state") or "UNKNOWN")
    effective_state = recorded_state
    if recorded_state in {"STARTING", "RUNNING"} and not alive:
        effective_state = "INTERRUPTED"
    blockers = [str(value) for value in (report or session).get("blockers", [])]
    failed_metrics = list(live.get("failed_metrics", []))
    if report and not failed_metrics:
        for name, metric in report.get("metrics", {}).items():
            if isinstance(metric, dict) and metric.get("result") != "PASSED":
                failed_metrics.append(
                    {
                        "name": name,
                        "value": metric.get("value"),
                        "threshold": metric.get("threshold"),
                    }
                )
    actions = []
    for blocker in blockers:
        action = BLOCKER_GUIDANCE.get(blocker)
        if action and action not in actions:
            actions.append(action)
    if effective_state == "INTERRUPTED":
        actions.insert(
            0,
            "Inspect supervisor.log and the last live_status.json checkpoint; the detached worker ended before a terminal summary.",
        )
    elif effective_state == "ERROR" and not report:
        actions.insert(
            0,
            "Inspect supervisor.log for the fail-closed preflight error, correct it, and create a new session directory for the retry.",
        )
    generated_at = str(live.get("generated_at") or "")
    checkpoint_age = None
    if generated_at:
        try:
            checkpoint_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            checkpoint_age = max(
                0.0, (datetime.now(timezone.utc) - checkpoint_at).total_seconds()
            )
        except ValueError:
            pass
    return {
        "schema_version": 1,
        "session_dir": str(session_dir),
        "state": effective_state,
        "recorded_state": recorded_state,
        "process_alive": alive,
        "worker_pid": session.get("worker_pid"),
        "started_at": session.get("started_at"),
        "ended_at": session.get("ended_at"),
        "checkpoint_at": generated_at or None,
        "checkpoint_age_seconds": checkpoint_age,
        "result": report.get("result") or session.get("result"),
        "mode": session.get("mode", "SOAK"),
        "notice": {
            "observed": bool(notice_evidence.get("notice_observation"))
            or bool(live.get("notice_observed")),
            "acknowledgement": notice_evidence.get(
                "subscription_acknowledgement",
                live.get("subscription_acknowledgement", {}),
            ),
            "errors": notice_evidence.get("errors", live.get("errors", [])),
        },
        "subscriptions": live.get("subscriptions", {}),
        "frame_counts_by_tr_id": live.get("frame_counts_by_tr_id", {}),
        "continuity_sample_count": live.get("continuity_sample_count", 0),
        "parser_failure_count": live.get("parser_failure_count", 0),
        "malformed_frame_count": live.get("malformed_frame_count", 0),
        "preflight_error": (
            live.get("error") if live.get("state") == "PREFLIGHT_FAILED" else None
        ),
        "watchdog": live.get("watchdog", {}),
        "blockers": blockers,
        "failed_metrics": failed_metrics,
        "recommended_actions": actions,
        "artifacts": session.get("artifacts", {}),
    }


def _print_summary(summary: Mapping[str, Any]) -> None:
    print(f"Gate 2 session: {summary['state']}")
    print(f"Directory: {summary['session_dir']}")
    print(
        f"Worker: pid={summary.get('worker_pid')} alive={summary.get('process_alive')}"
    )
    if summary.get("mode") == "NOTICE":
        notice = summary.get("notice") or {}
        ack = notice.get("acknowledgement") or {}
        print(
            "Execution notice: "
            f"observed={notice.get('observed')} "
            f"ack={bool(ack.get('accepted'))} "
            f"key={bool(ack.get('encryption_key_present'))} "
            f"iv={bool(ack.get('encryption_iv_present'))}"
        )
    if summary.get("checkpoint_at"):
        print(
            "Last checkpoint: "
            f"{summary['checkpoint_at']} (age={summary.get('checkpoint_age_seconds', 0):.1f}s)"
        )
    if summary.get("preflight_error"):
        error = summary["preflight_error"]
        print("Preflight error: " f"{error.get('type')}: {error.get('message')}")
    subscriptions = summary.get("subscriptions") or {}
    if subscriptions:
        print(
            "Subscriptions: "
            f"{subscriptions.get('acked_count', 0)}/"
            f"{subscriptions.get('requested_count', 0)} ACKed"
        )
    frames = summary.get("frame_counts_by_tr_id") or {}
    if frames:
        print("Frames: " + ", ".join(f"{key}={value}" for key, value in frames.items()))
    if summary.get("failed_metrics"):
        print("Failed metrics:")
        for metric in summary["failed_metrics"]:
            print(
                f"  - {metric.get('name')}: value={metric.get('value')}; "
                f"required={metric.get('threshold')}"
            )
    if summary.get("recommended_actions"):
        print("What to fix next:")
        for action in summary["recommended_actions"]:
            print(f"  - {action}")
    artifacts = summary.get("artifacts") or {}
    if artifacts:
        print("Artifacts:")
        for name, path in artifacts.items():
            print(f"  - {name}: {path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch or inspect a detached, read-only Gate-2 session"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="launch a detached session")
    start.add_argument("--confirm-read-only", action="store_true")
    start.add_argument("--environment", choices=("PROD", "SIM"), default="PROD")
    start.add_argument("--symbols", required=True)
    start.add_argument("--session-date", required=True)
    start.add_argument("--gate1-report", type=Path, required=True)
    start.add_argument("--capability-manifest", type=Path, required=True)
    start.add_argument("--redacted-evidence", type=Path, action="append", required=True)
    start.add_argument("--reconnect-after-seconds", type=float, action="append")
    start.add_argument("--silent-stale-probe-after-seconds", type=float, default=5400.0)
    start.add_argument("--poll-seconds", type=float, default=0.1)
    start.add_argument("--watchdog-timeout-seconds", type=float, default=2.0)
    start.add_argument("--status-seconds", type=float, default=30.0)
    start.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    start.add_argument("--python-executable", default=sys.executable)

    notice = subparsers.add_parser(
        "start-notice",
        help="launch a detached read-only H0GSCNI0 observation",
    )
    notice.add_argument("--confirm-read-only", action="store_true")
    notice.add_argument("--timeout-seconds", type=float, default=24000.0)
    notice.add_argument("--status-seconds", type=float, default=30.0)
    notice.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    notice.add_argument("--python-executable", default=sys.executable)

    status = subparsers.add_parser("status", help="inspect a session")
    status.add_argument("--session-dir", type=Path)
    status.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    status.add_argument("--json", action="store_true")

    worker = subparsers.add_parser("_worker")
    worker.add_argument("--config", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "start":
        if args.reconnect_after_seconds is None:
            args.reconnect_after_seconds = [3600.0]
        session_dir = _create_session(args)
        print(f"Detached Gate-2 session started: {session_dir}")
        print(
            "Check it later with: python scripts/manage_gate2_session.py status "
            f'--session-dir "{session_dir}"'
        )
        return 0
    if args.command == "start-notice":
        session_dir = _create_notice_session(args)
        print(f"Detached Gate-2 notice observation started: {session_dir}")
        print(
            "Check it later with: python scripts/manage_gate2_session.py status "
            f'--session-dir "{session_dir}"'
        )
        return 0
    if args.command == "_worker":
        return _run_worker(args.config)
    session_dir = args.session_dir or _latest_session(args.evidence_root)
    summary = summarize_session(session_dir)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return (
        0
        if (
            summary["state"] in {"RUNNING", "PASSED"}
            or summary.get("result") == "NOTICE_CAPTURED"
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
