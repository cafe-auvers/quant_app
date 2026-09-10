from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_gate2_readiness as preflight


@pytest.fixture
def local_ready(tmp_path, monkeypatch):
    from gate2 import capabilities, reporting
    from src.core import execution_config
    from src.services import kis_ws_symbol_keys
    from src.utils import config

    root = tmp_path / "repo"
    root.mkdir()
    bundle = tmp_path / "evidence"
    bundle.mkdir()
    commit = "a" * 40
    gate1 = bundle / "gate1.json"
    gate1.write_text(json.dumps({
        "result": "PASSED", "commit_sha": commit, "schema_version": 2,
        "gate": "GATE_1_DETERMINISTIC_SIMULATION", "pytest_exit_code": 0,
        "invariant_violations": [], "scenario_count": 1,
        "scenarios": [{"scenario_id": "test_example", "result": "PASSED", "group": "F1_CRASH_FAULT_INJECTION"}],
    }), encoding="utf-8")
    entries = []
    channels = {**capabilities.REQUIRED_CAPABILITIES, capabilities.EXECUTION_NOTICE: "H0GSCNI0"}
    for index, (capability_id, channel) in enumerate(channels.items()):
        interpretation = capabilities.TIMESTAMP_INTERPRETATION
        if capability_id in {capabilities.TRADE_SEQUENCE, capabilities.QUOTE_SEQUENCE}:
            interpretation = "NO_USABLE_SEQUENCE"
        elif capability_id == capabilities.EXECUTION_NOTICE:
            interpretation = capabilities.NOTICE_INTERPRETATION
        evidence = {
            "capability_id": capability_id, "environment": "PROD", "tr_id": channel,
            "interpretation": interpretation, "observed_at": "2026-09-08T14:00:00+00:00",
            "observations": [{"sample": "redacted"}],
        }
        path = bundle / f"evidence-{index}.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        entries.append({
            "capability_id": capability_id, "environment": "PROD", "tr_id": channel,
            "interpretation": interpretation, "status": "VERIFIED",
            "evidence_file": path.name, "evidence_sha256": capabilities.sha256_file(path),
        })
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1, "commit_sha": commit, "environment": "PROD",
        "review": {
            "status": "APPROVED", "author": "author", "reviewer": "reviewer",
            "reviewed_at": "2026-09-08T20:00:00+00:00", "method": "PROCEDURAL_DUAL_CONTROL",
            "reference": "test-attestation",
        },
        "capabilities": entries,
    }), encoding="utf-8")
    key_path = root / "symbol-keys.json"
    key_path.write_text('{"AAPL": "DNASAAPL"}', encoding="utf-8")
    monkeypatch.setattr(config, "install_repository_configuration", lambda: None)
    monkeypatch.setattr(preflight, "_git", lambda root, *args: commit if args[0] == "rev-parse" else "")
    monkeypatch.setattr(reporting, "runtime_activation_snapshot", lambda: dict(reporting.SAFE_RUNTIME_EXPECTATIONS))
    monkeypatch.setattr(
        reporting,
        "clock_synchronization_status",
        lambda: {
            "checked": True,
            "synchronized": True,
            "reference_id": "0x01020304",
            "leap_indicator": 0,
            "reason": "synchronized",
        },
    )
    monkeypatch.setattr(execution_config, "configuration_issues", lambda: ())
    monkeypatch.setattr(execution_config, "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY", 41)
    monkeypatch.setattr(execution_config, "BROKER_EVENT_STALE_SECONDS", 2.0)
    monkeypatch.setattr(execution_config, "LOCAL_RECEIVE_STALE_SECONDS", 2.0)
    monkeypatch.setattr(kis_ws_symbol_keys, "DEFAULT_KIS_WS_SYMBOL_KEYS_FILE", key_path)
    for key in ("KIS_PROD_APP_KEY", "KIS_PROD_APP_SECRET", "KIS_WS_HTS_ID"):
        monkeypatch.setenv(key, f"secret-value-for-{key}")
    monkeypatch.setenv("KIS_PROD_BASE_URL", "https://broker.invalid")
    monkeypatch.setenv("KIS_PROD_WS_URL", "ws://broker.invalid")
    args = preflight.build_parser().parse_args([
        "--gate1-report", str(gate1), "--capability-manifest", str(manifest_path),
        "--redacted-evidence", str(bundle / "evidence-0.json"), "--symbols", "AAPL",
        "--session-date", "2026-09-09",
    ])
    now = datetime(2026, 9, 9, 13, 20, tzinfo=timezone.utc)
    return args, root, now


def test_local_checks_never_claim_gate_pass_or_construct_live_services(local_ready, monkeypatch):
    from src.services import broker, kis_realtime_market_data
    from src.api import kis_websocket
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Preflight must not construct broker/WebSocket services or open a network connection")

    monkeypatch.setattr(broker, "KisBroker", forbidden)
    monkeypatch.setattr(kis_realtime_market_data, "build_kis_realtime_market_data_from_environment", forbidden)
    monkeypatch.setattr(kis_websocket, "KisWebSocketClient", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    args, root, now = local_ready
    before = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file()}

    report = preflight.check_readiness(args, root=root, now=now)

    assert report["blockers"] == []
    assert report["status"] == "LOCAL_CHECKS_COMPLETE"
    assert report["gate2_pass"] is False
    assert report["external_checks_not_performed"]
    after = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file()}
    assert after == before


def test_preflight_reports_independent_blockers_together(local_ready, monkeypatch):
    from gate2 import reporting
    from src.core import execution_config

    args, root, now = local_ready
    monkeypatch.setattr(preflight, "_git", lambda root, *args: "a" * 40 if args[0] == "rev-parse" else " M changed.py")
    args.gate1_report.write_text('{"result": "PASSED", "commit_sha": "older"}', encoding="utf-8")
    activation = {**reporting.SAFE_RUNTIME_EXPECTATIONS, "KIS_WS_ENABLED": False, "KIS_WS_PROTOCOL_VERIFIED": False}
    monkeypatch.setattr(reporting, "runtime_activation_snapshot", lambda: activation)
    monkeypatch.setattr(execution_config, "LOCAL_RECEIVE_STALE_SECONDS", 5.0)
    monkeypatch.setattr(execution_config, "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY", 2)
    monkeypatch.delenv("KIS_WS_HTS_ID")
    args.symbols = "AAPL,MSFT"
    manifest = json.loads(args.capability_manifest.read_text())
    manifest["review"]["status"] = "PENDING"
    manifest["capabilities"] = manifest["capabilities"][:-1]
    args.capability_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    report = preflight.check_readiness(args, root=root, now=now)

    assert {
        "source_clean", "gate1_exact_commit", "runtime.KIS_WS_ENABLED",
        "runtime.KIS_WS_PROTOCOL_VERIFIED", "stale_budget", "aggregate_capacity",
        "credential.KIS_WS_HTS_ID", "symbol_keys", "capability.EXECUTION_NOTICE_ENCRYPTION",
        "capability_manifest",
    }.issubset(report["blockers"])
    assert report["status"] == "LOCAL_BLOCKERS"


def test_preflight_blocks_an_unsynchronized_windows_clock(local_ready, monkeypatch):
    from gate2 import reporting

    args, root, now = local_ready
    monkeypatch.setattr(
        reporting,
        "clock_synchronization_status",
        lambda: {
            "checked": True,
            "synchronized": False,
            "reference_id": "0x00000000",
            "leap_indicator": 3,
            "reason": "windows_time_unsynchronized",
        },
    )

    report = preflight.check_readiness(args, root=root, now=now)

    assert "clock_synchronization" in report["blockers"]


def test_windows_clock_status_requires_a_nonzero_reference_and_no_leap_warning(
    monkeypatch,
):
    from gate2 import reporting

    monkeypatch.setattr(reporting.os, "name", "nt")
    monkeypatch.setattr(
        reporting.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Leap Indicator: 0(no warning)\nReferenceId: 0x34E772B7\n",
        ),
    )
    assert reporting.clock_synchronization_status()["synchronized"] is True

    monkeypatch.setattr(
        reporting.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Leap Indicator: 3(not synchronized)\nReferenceId: 0x00000000\n",
        ),
    )
    status = reporting.clock_synchronization_status()
    assert status["synchronized"] is False
    assert status["reason"] == "windows_time_unsynchronized"


@pytest.mark.parametrize("poll", [float("nan"), float("inf"), 0, -1, 0.3])
def test_invalid_poll_timing_fails_closed_and_remains_valid_json(local_ready, poll):
    args, root, now = local_ready
    args.poll_seconds = poll
    report = preflight.check_readiness(args, root=root, now=now)
    assert "poll_interval" in report["blockers"]
    assert "stale_budget" in report["blockers"]
    json.dumps(report, allow_nan=False)


def test_drills_use_launch_offset_not_open_and_late_start_fails(local_ready):
    args, root, now = local_ready
    args.start_at = "2026-09-09T13:25:00+00:00"
    args.reconnect_after_seconds = [60.0, 23999.0]
    args.silent_stale_probe_after_seconds = 300.0
    report = preflight.check_readiness(args, root=root, now=now)
    assert {"drill.reconnect_1", "drill.reconnect_2", "drill.silent_stale"}.issubset(report["blockers"])
    assert report["session"]["offset_basis"] == "runner start, not market open"

    late = now.replace(hour=14)
    report = preflight.check_readiness(args, root=root, now=late)
    assert "full_session_not_started" in report["blockers"]
    assert "planned_start" in report["blockers"]


@pytest.mark.parametrize("day", ["2026-09-07", "2026-09-12", "invalid"])
def test_calendar_rejects_holiday_weekend_and_invalid_day(local_ready, day):
    args, root, now = local_ready
    args.session_date = day
    report = preflight.check_readiness(args, root=root, now=now)
    assert "session_calendar" in report["blockers"]


def test_tampered_evidence_and_internal_or_empty_files_fail(local_ready):
    args, root, now = local_ready
    args.redacted_evidence[0].write_text("{}", encoding="utf-8")
    inside = root / "private-evidence.json"
    inside.write_text("{}", encoding="utf-8")
    empty = root.parent / "empty.json"
    empty.touch()
    args.redacted_evidence.extend([inside, empty, inside])
    report = preflight.check_readiness(args, root=root, now=now)
    assert {"capability_digest.0", "capability_manifest", "redacted_evidence.1", "redacted_evidence.2", "redacted_evidence_name.3"}.issubset(report["blockers"])


def test_credential_values_are_not_exposed_even_in_validator_exceptions(local_ready, monkeypatch):
    from gate2 import capabilities

    args, root, now = local_ready
    secret = "short"
    monkeypatch.setenv("KIS_PROD_APP_SECRET", secret)

    def bad_manifest(*args, **kwargs):
        raise ValueError(f"malformed capability contains {secret}")

    monkeypatch.setattr(capabilities, "load_verified_capability_manifest", bad_manifest)
    report = preflight.check_readiness(args, root=root, now=now)
    rendered = json.dumps(report)
    assert secret not in rendered
    assert "secret-value-for-" not in rendered
    assert "[REDACTED]" in rendered
    credential = next(item for item in report["checks"] if item["id"] == "credential.KIS_PROD_APP_SECRET")
    assert credential["present"] is True


def test_unreadable_configuration_is_a_blocker_and_does_not_print_exception(local_ready, monkeypatch):
    from src.utils import config

    args, root, now = local_ready

    def denied():
        raise PermissionError("private file contents must not escape")

    monkeypatch.setattr(config, "install_repository_configuration", denied)
    report = preflight.check_readiness(args, root=root, now=now)
    assert "repository_configuration" in report["blockers"]
    assert "private file contents" not in json.dumps(report)


def test_in_repo_gate1_and_manifest_allowed_but_minimal_pass_claim_rejected(local_ready):
    args, root, now = local_ready
    gate1 = root / "gate1.json"
    gate1.write_bytes(args.gate1_report.read_bytes())
    args.gate1_report = gate1
    manifest = root / "manifest.json"
    manifest.write_bytes(args.capability_manifest.read_bytes())
    for path in args.capability_manifest.parent.glob("evidence-*.json"):
        (root / path.name).write_bytes(path.read_bytes())
    args.capability_manifest = manifest
    report = preflight.check_readiness(args, root=root, now=now)
    assert report["blockers"] == []

    gate1.write_text(json.dumps({"result": "PASSED", "commit_sha": "a" * 40}), encoding="utf-8")
    report = preflight.check_readiness(args, root=root, now=now)
    assert "gate1_report_completeness" in report["blockers"]
    assert "gate1_exact_commit" not in report["blockers"]


@pytest.mark.parametrize("relevant_key", [
    "KIS_WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS",
    "MAX_MARKET_DATA_QUEUE_DELAY_SECONDS",
    "MAX_FUTURE_BROKER_EVENT_SECONDS",
    "KIS_SUBMIT_MUTATION_CAPACITY",
])
def test_only_gate2_configuration_issues_block_preflight(local_ready, monkeypatch, relevant_key):
    from src.core import execution_config

    args, root, now = local_ready
    coordination_issue = "COORDINATION_ACTIVE_CARD_POLL_SECONDS: must be at least 1; using safe default 5"
    monkeypatch.setattr(execution_config, "configuration_issues", lambda: (coordination_issue,))

    report = preflight.check_readiness(args, root=root, now=now)

    assert report["blockers"] == []
    configuration = next(item for item in report["checks"] if item["id"] == "configuration_values")
    assert configuration["issues"] == []
    assert configuration["unrelated_issues"] == [coordination_issue]

    relevant_issue = f"{relevant_key}: must be numeric; using safe default"
    monkeypatch.setattr(execution_config, "configuration_issues", lambda: (coordination_issue, relevant_issue))

    report = preflight.check_readiness(args, root=root, now=now)

    assert "configuration_values" in report["blockers"]
    configuration = next(item for item in report["checks"] if item["id"] == "configuration_values")
    assert configuration["issues"] == [relevant_issue]
    assert configuration["unrelated_issues"] == [coordination_issue]
