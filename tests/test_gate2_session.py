from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from scripts import manage_gate2_session


def test_runner_command_routes_every_artifact_to_the_session_directory(tmp_path):
    config = {
        "python_executable": "python.exe",
        "paths": {
            "gate1_report": str(tmp_path / "gate1.json"),
            "capability_manifest": str(tmp_path / "manifest.json"),
            "redacted_evidence": [str(tmp_path / "frames.json")],
            "runtime_log": str(tmp_path / "runtime.log"),
            "live_status": str(tmp_path / "status.json"),
            "report": str(tmp_path / "report.json"),
        },
        "options": {
            "environment": "PROD",
            "symbols": ["AAPL", "MSFT"],
            "session_date": "2026-09-03",
            "reconnect_after_seconds": [3600.0, 7200.0],
            "silent_stale_probe_after_seconds": 5400.0,
            "poll_seconds": 0.1,
            "watchdog_timeout_seconds": 2.0,
            "status_seconds": 30.0,
        },
    }

    command = manage_gate2_session._runner_command(config)

    assert command[0] == "python.exe"
    assert command[command.index("--symbols") + 1] == "AAPL,MSFT"
    assert command[command.index("--status-output") + 1] == str(
        tmp_path / "status.json"
    )
    assert command.count("--reconnect-after-seconds") == 2
    assert command.count("--redacted-evidence") == 1


def test_notice_runner_command_uses_only_read_only_capture_outputs(tmp_path):
    config = {
        "mode": "NOTICE",
        "python_executable": "python.exe",
        "paths": {
            "notice_evidence": str(tmp_path / "notice.json"),
            "live_status": str(tmp_path / "status.json"),
        },
        "options": {"timeout_seconds": 24000.0, "status_seconds": 30.0},
    }

    command = manage_gate2_session._runner_command(config)

    assert "capture_kis_ws_notice_evidence.py" in command[1]
    assert "--confirm-read-only" in command
    assert "--output" in command
    assert "run_gate2_soak.py" not in command[1]


def test_summarize_failed_session_explains_each_gate_metric(tmp_path, monkeypatch):
    session = {
        "state": "FAILED",
        "worker_pid": 123,
        "started_at": "2026-09-03T13:25:00+00:00",
        "ended_at": "2026-09-03T20:01:00+00:00",
        "result": "FAILED",
        "blockers": ["execution_notice_evidence", "full_regular_session"],
        "artifacts": {"report": str(tmp_path / "gate2_report.json")},
    }
    live = {
        "generated_at": "2026-09-03T20:01:00+00:00",
        "subscriptions": {"requested_count": 3, "acked_count": 2},
        "frame_counts_by_tr_id": {"HDFSCNT0": 10, "HDFSASP0": 10},
        "failed_metrics": [
            {
                "name": "execution_notice_evidence",
                "value": 0,
                "threshold": "strict execution-notice capability verified",
            }
        ],
    }
    (tmp_path / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (tmp_path / "live_status.json").write_text(json.dumps(live), encoding="utf-8")
    monkeypatch.setattr(manage_gate2_session, "_process_alive", lambda _pid: False)

    summary = manage_gate2_session.summarize_session(tmp_path)

    assert summary["state"] == "FAILED"
    assert summary["subscriptions"]["acked_count"] == 2
    assert any("H0GSCNI0" in action for action in summary["recommended_actions"])
    assert any("before the NYSE" in action for action in summary["recommended_actions"])


def test_summarize_marks_dead_nonterminal_worker_interrupted(tmp_path, monkeypatch):
    session = {
        "state": "RUNNING",
        "worker_pid": 456,
        "started_at": "2026-09-03T13:25:00+00:00",
        "ended_at": None,
        "blockers": [],
        "artifacts": {"supervisor_log": str(tmp_path / "supervisor.log")},
    }
    (tmp_path / "session.json").write_text(json.dumps(session), encoding="utf-8")
    monkeypatch.setattr(manage_gate2_session, "_process_alive", lambda _pid: False)

    summary = manage_gate2_session.summarize_session(tmp_path)

    assert summary["state"] == "INTERRUPTED"
    assert "last live_status.json" in summary["recommended_actions"][0]


def test_summarize_surfaces_safe_preflight_error(tmp_path, monkeypatch):
    session = {
        "state": "ERROR",
        "worker_pid": 789,
        "started_at": "2026-09-03T13:25:00+00:00",
        "ended_at": "2026-09-03T13:25:01+00:00",
        "blockers": ["preflight_failed"],
        "artifacts": {},
    }
    live = {
        "state": "PREFLIGHT_FAILED",
        "generated_at": "2026-09-03T13:25:01+00:00",
        "error": {
            "type": "RuntimeError",
            "message": "capability manifest is not independently APPROVED",
        },
    }
    (tmp_path / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (tmp_path / "live_status.json").write_text(json.dumps(live), encoding="utf-8")
    monkeypatch.setattr(manage_gate2_session, "_process_alive", lambda _pid: False)

    summary = manage_gate2_session.summarize_session(tmp_path)

    assert summary["preflight_error"]["type"] == "RuntimeError"
    assert "not independently APPROVED" in summary["preflight_error"]["message"]
    assert any("preflight" in action for action in summary["recommended_actions"])


def test_create_session_records_detached_worker_before_returning(tmp_path, monkeypatch):
    gate1 = tmp_path / "gate1.json"
    manifest = tmp_path / "manifest.json"
    frames = tmp_path / "frames.json"
    for path in (gate1, manifest, frames):
        path.write_text("{}", encoding="utf-8")
    evidence_root = tmp_path / "outside" / "sessions"
    monkeypatch.setattr(manage_gate2_session, "_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(
        manage_gate2_session,
        "_require_external_root",
        lambda path: Path(path).resolve(),
    )

    class FakeProcess:
        pid = 9876

    launched = {}

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(manage_gate2_session.subprocess, "Popen", fake_popen)
    args = argparse.Namespace(
        confirm_read_only=True,
        environment="PROD",
        symbols="AAPL,MSFT",
        session_date="2026-09-03",
        gate1_report=gate1,
        capability_manifest=manifest,
        redacted_evidence=[frames],
        reconnect_after_seconds=[3600.0],
        silent_stale_probe_after_seconds=5400.0,
        poll_seconds=0.1,
        watchdog_timeout_seconds=2.0,
        status_seconds=30.0,
        evidence_root=evidence_root,
        python_executable=str(Path(manage_gate2_session.sys.executable)),
    )

    session_dir = manage_gate2_session._create_session(args)
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    config_text = (session_dir / "session_config.json").read_text(encoding="utf-8")

    assert session["state"] == "STARTING"
    assert session["worker_pid"] == 9876
    assert "_worker" in launched["command"]
    assert "KIS_PROD_APP_SECRET" not in config_text
    assert (evidence_root / "latest_session.json").is_file()


def test_notice_worker_fails_closed_when_evidence_contains_validation_error(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "session_config.json"
    evidence_path = tmp_path / "notice_evidence.json"
    status_path = tmp_path / "live_status.json"
    config = {
        "mode": "NOTICE",
        "python_executable": "python.exe",
        "paths": {
            "notice_evidence": str(evidence_path),
            "live_status": str(status_path),
        },
        "options": {"timeout_seconds": 10.0, "status_seconds": 1.0},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "session.json").write_text(
        json.dumps(
            manage_gate2_session._session_payload(
                session_dir=tmp_path,
                state="STARTING",
                worker_pid=None,
                started_at="2026-09-04T13:00:00+00:00",
                mode="NOTICE",
            )
        ),
        encoding="utf-8",
    )
    evidence_path.write_text(
        json.dumps(
            {
                "subscription_acknowledgement": {
                    "accepted": True,
                    "encryption_key_present": True,
                    "encryption_iv_present": True,
                },
                "notice_observation": {"field_count": 24},
                "errors": ["schema mismatch"],
            }
        ),
        encoding="utf-8",
    )
    status_path.write_text(
        json.dumps({"state": "INCOMPLETE", "errors": ["schema mismatch"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(manage_gate2_session, "_set_keep_awake", lambda _value: True)
    monkeypatch.setattr(
        manage_gate2_session.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    exit_code = manage_gate2_session._run_worker(config_path)
    session = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))

    assert exit_code == 1
    assert session["state"] == "COMPLETED"
    assert session["result"] == "INCOMPLETE"
    assert "execution_notice_validation_failed" in session["blockers"]


def test_terminal_session_never_reports_stale_worker_as_alive(tmp_path, monkeypatch):
    session = {
        "state": "COMPLETED",
        "mode": "NOTICE",
        "worker_pid": 123,
        "started_at": "2026-09-04T13:00:00+00:00",
        "ended_at": "2026-09-04T13:01:00+00:00",
        "result": "NOTICE_CAPTURED",
        "blockers": [],
        "artifacts": {},
    }
    (tmp_path / "session.json").write_text(json.dumps(session), encoding="utf-8")
    monkeypatch.setattr(
        manage_gate2_session,
        "_process_alive",
        lambda _pid: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    summary = manage_gate2_session.summarize_session(tmp_path)

    assert summary["state"] == "COMPLETED"
    assert summary["process_alive"] is False
