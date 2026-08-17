from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from src.utils.market_calendar import (
    is_regular_session_open,
    nyse_regular_session_close_time,
    seconds_until_regular_session_close,
)
from src.services.kis_realtime_market_data import (
    build_kis_realtime_market_data_from_environment,
)
from src.core import execution_config
from src.core.runtime_safety_audit import (
    BROKER_MUTATION_AUDIT_SOURCE,
    ENTRY_READINESS_AUDIT_SOURCE,
    begin_runtime_safety_audit,
)
from gate2.reporting import (
    Gate2Evidence,
    LiveGate2Runner,
    _ProgressWatchdog,
    _SecretRedactingFormatter,
    build_report,
    main as gate2_main,
)
from src.api.kis_websocket import KisWsProtocolOperation
from gate2.capabilities import (
    EXECUTION_NOTICE,
    NOTICE_INTERPRETATION,
    QUOTE_SEQUENCE,
    QUOTE_TIMESTAMP,
    TIMESTAMP_INTERPRETATION,
    TRADE_SEQUENCE,
    TRADE_TIMESTAMP,
    load_verified_capability_manifest,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_runtime_capability_manifest(
    directory: Path, *, commit: str, include_execution_notice: bool = False
) -> Path:
    definitions = [
        (TRADE_TIMESTAMP, "HDFSCNT0", TIMESTAMP_INTERPRETATION),
        (QUOTE_TIMESTAMP, "HDFSASP0", TIMESTAMP_INTERPRETATION),
        (TRADE_SEQUENCE, "HDFSCNT0", "MONOTONIC"),
        (QUOTE_SEQUENCE, "HDFSASP0", "NO_USABLE_SEQUENCE"),
    ]
    if include_execution_notice:
        definitions.append(
            (EXECUTION_NOTICE, "H0GSCNI0", NOTICE_INTERPRETATION)
        )
    entries = []
    for index, (capability_id, tr_id, interpretation) in enumerate(definitions):
        sequence = (
            {
                "sequence_field": "EVOL",
                "reset_semantics": "RESET_ON_RECONNECT",
            }
            if capability_id == TRADE_SEQUENCE
            else {}
        )
        evidence_path = directory / f"runtime-evidence-{index}.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "capability_id": capability_id,
                    "environment": "PROD",
                    "tr_id": tr_id,
                    "interpretation": interpretation,
                    "observed_at": "2026-08-17T00:00:00Z",
                    "observations": [{"frame_count": 10}],
                    **sequence,
                }
            ),
            encoding="utf-8",
        )
        entries.append(
            {
                "capability_id": capability_id,
                "status": "VERIFIED",
                "environment": "PROD",
                "tr_id": tr_id,
                "interpretation": interpretation,
                "evidence_file": evidence_path.name,
                "evidence_sha256": sha256_file(evidence_path),
                **sequence,
            }
        )
    manifest_path = directory / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit_sha": commit,
                "environment": "PROD",
                "review": {
                    "status": "APPROVED",
                    "author": "bundle-author",
                    "reviewer": "independent-reviewer",
                    "reviewed_at": "2026-08-17T00:00:00Z",
                    "method": "PROCEDURAL_DUAL_CONTROL",
                    "reference": "dual-control-log:runtime-review-1",
                },
                "capabilities": entries,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_pr4_market_data_configuration_is_present_and_fail_closed():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for name in (
        "KIS_PROD_WS_URL",
        "KIS_SIM_WS_URL",
        "KIS_WS_ENABLED=false",
        "KIS_WS_PROTOCOL_VERIFIED=false",
        "KIS_CAPABILITY_MANIFEST_PATH=",
        "KIS_CAPABILITY_MANIFEST_SHA256=",
        "KIS_RUNTIME_COMMIT_SHA=",
        "KIS_WS_HTS_ID",
        "KIS_MARKET_DATA_MODE=REST_DISPLAY_ONLY",
        "BROKER_EVENT_STALE_SECONDS",
        "LOCAL_RECEIVE_STALE_SECONDS",
        "MAX_MARKET_DATA_QUEUE_DELAY_SECONDS",
        "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY=0",
        "KIS_WS_RAW_CAPTURE_ENABLED=false",
        "KIS_LIVE_EXECUTION_MODE=DISABLED",
        "KIS_MUTATION_MIN_SPACING_SECONDS=0.2",
        "KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS=1",
        "BUYBOARD_ENGINE_ENABLED=false",
    ):
        assert name in env_example
    assert "websockets==15.0.1" in requirements


def test_early_close_is_used_for_market_session_decisions():
    # 2026-11-27 is the Friday after US Thanksgiving.
    eastern = ZoneInfo("America/New_York")
    before = dt.datetime(2026, 11, 27, 12, 59, tzinfo=eastern)
    after = dt.datetime(2026, 11, 27, 13, 1, tzinfo=eastern)

    assert nyse_regular_session_close_time(before.date()) == dt.time(13, 0)
    assert is_regular_session_open(before)
    assert not is_regular_session_open(after)
    assert seconds_until_regular_session_close(before) == 60


def test_live_factory_requires_both_enable_and_protocol_verification(monkeypatch):
    monkeypatch.setattr(execution_config, "KIS_WS_ENABLED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_PROTOCOL_VERIFIED", False)

    try:
        build_kis_realtime_market_data_from_environment()
    except RuntimeError as exc:
        assert "Workstream 0" in str(exc)
    else:
        raise AssertionError("unverified KIS protocol must fail closed")


def test_live_factory_uses_only_aggregate_pool_and_wires_verified_sequences(
    monkeypatch,
):
    monkeypatch.setattr(execution_config, "KIS_WS_ENABLED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_PROTOCOL_VERIFIED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY", 3)
    monkeypatch.setattr(execution_config, "KIS_WS_TRADE_CHANNEL_CAPACITY", 0)
    monkeypatch.setattr(execution_config, "KIS_WS_QUOTE_CHANNEL_CAPACITY", 0)
    monkeypatch.setenv(
        "KIS_WS_SYMBOL_KEYS_JSON",
        json.dumps({"AAPL": "DNASAAPL", "MSFT": "DNASMSFT", "NVDA": "DNASNVDA"}),
    )

    service = build_kis_realtime_market_data_from_environment(
        confirmed_sequence_channels=("HDFSCNT0",),
        sequence_field_by_channel={"HDFSCNT0": "EVOL"},
        sequence_reset_by_channel={"HDFSCNT0": "RESET_ON_RECONNECT"},
        qualification_mode=True,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 0, "MSFT": 0, "NVDA": 0},
        quote_priorities={},
    )

    snapshot = service.subscription_capacity_snapshot()
    assert snapshot.reconnect_replay_count == 3
    assert snapshot.total_capacity == 3
    assert service._confirmed_sequence_channels == {"HDFSCNT0"}


def test_normal_live_factory_loads_exact_pinned_reviewed_capabilities(
    tmp_path, monkeypatch
):
    commit = "a" * 40
    manifest_path = _write_runtime_capability_manifest(tmp_path, commit=commit)
    manifest_digest = sha256_file(manifest_path)
    monkeypatch.setattr(execution_config, "KIS_WS_ENABLED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_PROTOCOL_VERIFIED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY", 3)
    monkeypatch.setenv("KIS_WS_HTS_ID", "reviewed-user")
    monkeypatch.setenv("KIS_WS_SYMBOL_KEYS_JSON", '{"AAPL":"DNASAAPL"}')

    service = build_kis_realtime_market_data_from_environment(
        capability_manifest_path=manifest_path,
        capability_manifest_sha256=manifest_digest,
        runtime_commit_sha=commit,
    )

    assert service._confirmed_sequence_channels == {"HDFSCNT0"}
    assert service._sequence_field_by_channel == {"HDFSCNT0": "EVOL"}
    assert service.capability_manifest_sha256 == manifest_digest
    assert service.capability_manifest_commit_sha == commit
    assert service.subscription_capacity_snapshot().execution_notice_desired is False


def test_normal_live_factory_rejects_drift_from_reviewed_manifest(
    tmp_path, monkeypatch
):
    commit = "b" * 40
    manifest_path = _write_runtime_capability_manifest(tmp_path, commit=commit)
    monkeypatch.setattr(execution_config, "KIS_WS_ENABLED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_PROTOCOL_VERIFIED", True)

    with pytest.raises(RuntimeError, match="digest mismatch"):
        build_kis_realtime_market_data_from_environment(
            capability_manifest_path=manifest_path,
            capability_manifest_sha256="f" * 64,
            runtime_commit_sha=commit,
        )

    with pytest.raises(ValueError, match="ad-hoc capability"):
        build_kis_realtime_market_data_from_environment(
            confirmed_sequence_channels=("HDFSCNT0",),
            sequence_field_by_channel={"HDFSCNT0": "EVOL"},
            sequence_reset_by_channel={"HDFSCNT0": "RESET_ON_RECONNECT"},
        )


def test_ws0_contract_explicitly_allows_only_inactive_provisional_adapters():
    contract = (ROOT / "docs" / "kanban_production_readiness.md").read_text(
        encoding="utf-8"
    )
    matrix = (ROOT / "docs" / "kis_capability_matrix.md").read_text(
        encoding="utf-8"
    )

    assert "revision 3.5 pilot amendment recorded" in contract
    assert "May be written provisionally before evidence" in contract
    assert "KIS_WS_PROTOCOL_VERIFIED=true or a live connection/subscription" in contract
    assert "non-zero production/simulation channel capacity" in contract
    assert "provisional D1/D3/D11 adapter may be implemented inactive" in matrix


def test_ws0_credentialed_capacity_evidence_matches_fail_closed_runtime_contract():
    fixture_dir = ROOT / "tests" / "fixtures" / "kis_protocol"
    capacity = json.loads(
        (fixture_dir / "ws0_20260817_subscription_capacity.json").read_text(
            encoding="utf-8"
        )
    )
    acknowledgements = json.loads(
        (fixture_dir / "ws0_20260817_subscription_acks.json").read_text(
            encoding="utf-8"
        )
    )
    simulated_rejection = json.loads(
        (fixture_dir / "ws0_20260817_sim_mutation_rejection.json").read_text(
            encoding="utf-8"
        )
    )

    assert capacity["broker_mutations"] == 0
    assert capacity["accepted_registrations"] == 41
    assert capacity["first_rejection"] == {
        "ordinal": 42,
        "tr_id": "HDFSASP0",
        "tr_key": "DNASBKNG",
        "rt_cd": "1",
        "msg_cd": "OPSP0008",
        "msg1": "MAX SUBSCRIBE OVER",
    }
    assert acknowledgements["broker_mutations"] == 0
    assert simulated_rejection["submit"]["msg_cd"] == "40100000"
    assert not simulated_rejection["submit"]["accepted"]
    assert simulated_rejection["open_order_check"]["matching_probe_order_count"] == 0
    assert simulated_rejection["safety"]["production_endpoints_called"] == 0
    assert execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY == 0


def test_ws0_committed_evidence_contains_no_credential_or_account_material():
    fixture_dir = ROOT / "tests" / "fixtures" / "kis_protocol"
    forbidden = (
        "approval_key",
        "access_token",
        "appsecret",
        "authorization",
        "account_no",
        "acnt_no",
        "cano",
    )
    for path in sorted(fixture_dir.glob("ws0_*.json")):
        text = path.read_text(encoding="utf-8").lower()
        assert not any(token in text for token in forbidden), path.name


def _passing_gate2_evidence() -> Gate2Evidence:
    opened = dt.datetime(2026, 8, 17, 13, 30, tzinfo=dt.timezone.utc)
    closed = dt.datetime(2026, 8, 17, 20, 0, tzinfo=dt.timezone.utc)
    subscriptions = [
        "HDFSCNT0:AAPL",
        "HDFSASP0:AAPL",
        "H0GSCNI0:EXECUTION_NOTICE",
    ]
    capabilities = {
        TRADE_TIMESTAMP: {
            "capability_id": TRADE_TIMESTAMP,
            "status": "VERIFIED",
            "environment": "PROD",
            "tr_id": "HDFSCNT0",
            "interpretation": TIMESTAMP_INTERPRETATION,
            "evidence_sha256": "1" * 64,
        },
        QUOTE_TIMESTAMP: {
            "capability_id": QUOTE_TIMESTAMP,
            "status": "VERIFIED",
            "environment": "PROD",
            "tr_id": "HDFSASP0",
            "interpretation": TIMESTAMP_INTERPRETATION,
            "evidence_sha256": "2" * 64,
        },
        TRADE_SEQUENCE: {
            "capability_id": TRADE_SEQUENCE,
            "status": "VERIFIED",
            "environment": "PROD",
            "tr_id": "HDFSCNT0",
            "interpretation": "NO_USABLE_SEQUENCE",
            "evidence_sha256": "3" * 64,
        },
        QUOTE_SEQUENCE: {
            "capability_id": QUOTE_SEQUENCE,
            "status": "VERIFIED",
            "environment": "PROD",
            "tr_id": "HDFSASP0",
            "interpretation": "NO_USABLE_SEQUENCE",
            "evidence_sha256": "4" * 64,
        },
        EXECUTION_NOTICE: {
            "capability_id": EXECUTION_NOTICE,
            "status": "VERIFIED",
            "environment": "PROD",
            "tr_id": "H0GSCNI0",
            "interpretation": NOTICE_INTERPRETATION,
            "evidence_sha256": "5" * 64,
        },
    }
    return Gate2Evidence(
        commit_sha="a" * 40,
        gate1_report_sha256="b" * 64,
        capability_matrix_sha256="c" * 64,
        capability_manifest_sha256="d" * 64,
        capability_review={
            "status": "APPROVED",
            "author": "bundle-author",
            "reviewer": "reviewer",
            "reviewed_at": "2026-08-17T00:00:00+00:00",
            "method": "PROCEDURAL_DUAL_CONTROL",
            "reference": "dual-control-log:gate2-review-1",
        },
        verified_capabilities=capabilities,
        runtime_confirmed_sequence_channels=[],
        runtime_sequence_fields={},
        runtime_sequence_reset_semantics={},
        environment="PROD",
        symbols=["AAPL"],
        tr_ids=["HDFSCNT0", "HDFSASP0", "H0GSCNI0"],
        verified_subscription_keys={"AAPL": "DNASAAPL"},
        activation_snapshot={
            "TRADING_ENABLED": False,
            "BUYBOARD_ENGINE_ENABLED": False,
            "KIS_WS_ENABLED": True,
            "KIS_WS_PROTOCOL_VERIFIED": True,
            "KIS_MUTATION_BUDGET_VERIFIED": False,
            "KIS_SUBMIT_MUTATION_CAPACITY": 0,
            "KIS_CANCEL_MUTATION_CAPACITY": 0,
            "KIS_REPLACE_MUTATION_CAPACITY": 0,
            "KIS_LIVE_EXECUTION_MODE": "DISABLED",
            "KIS_CONTROLLED_LIVE_SYMBOLS": [],
            "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL": 0.0,
        },
        session_open=opened,
        session_close=closed,
        started_at=opened - dt.timedelta(minutes=1),
        ended_at=closed + dt.timedelta(seconds=1),
        requested_subscriptions=subscriptions,
        acked_subscriptions=subscriptions,
        max_aggregate_registration_usage=3,
        aggregate_registration_capacity=41,
        connection_events=[{"kind": "DISCONNECT_REQUESTED", "at": opened.isoformat()}],
        disconnects=[
            {
                "classification": "INJECTED",
                "disconnect_at": opened.isoformat(),
                "reconnected_at": (opened + dt.timedelta(seconds=1)).isoformat(),
                "reacked_at": (opened + dt.timedelta(seconds=2)).isoformat(),
                "recovery_seconds": 2.0,
            }
        ],
        injected_disconnect_request_count=1,
        protocol_operations=[
            {
                "generation": 1,
                "action": "SUBSCRIBE",
                "tr_id": "HDFSCNT0",
                "registration": "AAPL",
                "transition_valid": True,
            }
        ],
        duplicate_request_probe={
            "requests_attempted": 4,
            "unexpected_protocol_operations": 0,
        },
        reconnect_recovery_seconds=[2.0],
        stale_detection_seconds=[0.2],
        frame_counts_by_tr_id={"HDFSCNT0": 100, "HDFSASP0": 100},
        record_counts_by_tr_id={"HDFSCNT0": 100, "HDFSASP0": 100},
        schema_fingerprints_by_tr_id={"HDFSCNT0": "d" * 64, "HDFSASP0": "e" * 64},
        receive_lag_ms={"count": 200, "p50": 50.0, "p95": 100.0, "p99": 200.0, "max": 300.0},
        queue_lag_ms={"count": 200, "p50": 1.0, "p95": 2.0, "p99": 3.0, "max": 4.0},
        synthetic_stop_tests={
            "injected": 1,
            "accepted_by_live_service": 1,
            "latched": 1,
            "consumed": 1,
        },
        silent_stale_probe={
            "connected_during_probe": True,
            "entry_readiness_ready_before_probe": True,
            "entry_readiness_ready_while_stale": False,
            "detected": True,
            "recovered": True,
            "detection_seconds": 2.1,
        },
        watchdog_cycles=10,
        watchdog_max_gap_seconds=0.2,
        watchdog_timeout_seconds=2.0,
        continuity_sample_count=250_000,
        continuity_started_at=opened.isoformat(),
        continuity_start_delay_seconds=0.0,
        poll_interval_seconds=0.1,
        safety_audit_initialized=True,
        safety_audit_sources=[
            BROKER_MUTATION_AUDIT_SOURCE,
            ENTRY_READINESS_AUDIT_SOURCE,
        ],
        broker_mutation_count=0,
        entry_readiness_check_count=1,
        stale_entry_readiness_check_count=1,
        stale_entry_readiness_rejection_count=1,
        stale_entry_readiness_allow_count=0,
        log_scan_completed=True,
        log_capture_sha256="f" * 64,
        sensitive_value_count=4,
        approval_key_count=1,
        redacted_evidence_sha256={"session-frames.json": "9" * 64},
    )


def test_gate2_report_passes_only_complete_read_only_session_evidence():
    report = build_report(_passing_gate2_evidence())

    assert report["result"] == "PASSED"
    assert report["commit_sha"] == "a" * 40
    assert report["max_aggregate_registration_usage"] == 3
    assert report["broker_mutations"] == 0
    assert not report["production_activation_authorized"]
    assert report["metrics"]["critical_subscription_ack"]["result"] == "PASSED"


def test_gate2_report_fails_closed_for_activation_or_missing_reconnect_evidence():
    evidence = _passing_gate2_evidence()
    evidence.activation_snapshot["TRADING_ENABLED"] = True
    evidence.reconnect_recovery_seconds.clear()

    report = build_report(evidence)

    assert report["result"] == "FAILED"
    assert "runtime_activation_fence" in report["blockers"]
    assert "critical_ack_recovery_seconds" in report["blockers"]


def test_gate2_cli_requires_at_least_one_redacted_evidence_file():
    with pytest.raises(SystemExit) as exc:
        gate2_main(
            [
                "--confirm-read-only",
                "--symbols",
                "AAPL",
                "--session-date",
                "2026-08-17",
                "--gate1-report",
                "gate1.json",
                "--capability-manifest",
                "capabilities.json",
                "--reconnect-after-seconds",
                "60",
                "--silent-stale-probe-after-seconds",
                "120",
                "--log-output",
                "gate2.log",
            ]
        )

    assert exc.value.code == 2


def test_gate2_report_rejects_unmeasured_default_zero_metrics_and_runtime_mismatch():
    evidence = _passing_gate2_evidence()
    evidence.protocol_operations.clear()
    evidence.log_scan_completed = False
    evidence.receive_lag_ms["count"] = 0
    evidence.safety_audit_initialized = False
    evidence.broker_mutation_count = None
    evidence.stale_entry_readiness_check_count = None
    evidence.stale_entry_readiness_rejection_count = None
    evidence.stale_entry_readiness_allow_count = None
    evidence.redacted_evidence_sha256.clear()
    evidence.verified_capabilities[TRADE_SEQUENCE]["interpretation"] = "MONOTONIC"
    evidence.verified_capabilities[TRADE_SEQUENCE]["sequence_field"] = "EVOL"

    report = build_report(evidence)

    assert report["result"] == "FAILED"
    assert "duplicate_subscription_corruption" in report["blockers"]
    assert "secret_leaks" in report["blockers"]
    assert "receive_lag_p95_ms" in report["blockers"]
    assert "receive_lag_p99_ms" in report["blockers"]
    assert "sequence_semantics" in report["blockers"]
    assert "broker_mutations" in report["blockers"]
    assert "stale_entry_readiness_fence" in report["blockers"]
    assert "redacted_evidence_bundle" in report["blockers"]


class _Gate2AuditService:
    def on_session(self, callback):
        self.session_callback = callback

    def on_protocol_operation(self, callback):
        self.operation_callback = callback

    def reconnect(self):
        return None


def test_gate2_protocol_audit_detects_duplicate_subscribe_and_unexpected_disconnect():
    evidence = _passing_gate2_evidence()
    evidence.protocol_operations.clear()
    evidence.disconnects.clear()
    evidence.injected_disconnect_request_count = 0
    service = _Gate2AuditService()
    safety_audit = begin_runtime_safety_audit()
    runner = LiveGate2Runner(service, evidence, safety_audit)
    observed = dt.datetime(2026, 8, 17, 14, 0, tzinfo=dt.timezone.utc)
    operation = KisWsProtocolOperation(
        generation=1,
        action="SUBSCRIBE",
        tr_id="HDFSCNT0",
        tr_key="DNASAAPL",
        sent_at=observed,
    )

    service.operation_callback(operation)
    service.operation_callback(operation)
    service.session_callback(False, "transport lost", 1, observed)
    runner.finalize()
    safety_audit.close()

    assert evidence.duplicate_subscription_anomaly_count == 1
    assert evidence.disconnects[0]["classification"] == "UNEXPECTED"
    assert evidence.unhandled_disconnect_count == 1


def test_gate2_log_formatter_redacts_static_and_dynamically_issued_secrets():
    values = {"STATIC-SECRET"}
    lock = threading.Lock()
    formatter = _SecretRedactingFormatter(
        "%(message)s", sensitive_values=values, lock=lock
    )
    first = logging.LogRecord(
        "gate2", logging.INFO, __file__, 1, "value=STATIC-SECRET", (), None
    )
    with lock:
        values.add("DYNAMIC-APPROVAL")
    second = logging.LogRecord(
        "gate2", logging.INFO, __file__, 1, "key=DYNAMIC-APPROVAL", (), None
    )

    assert formatter.format(first) == "value=<redacted-secret>"
    assert formatter.format(second) == "key=<redacted-secret>"
    assert formatter.redaction_count == 2


def test_gate2_progress_watchdog_produces_independent_measurement_cycles():
    evidence = _passing_gate2_evidence()
    evidence.watchdog_cycles = 0
    evidence.watchdog_max_gap_seconds = 0.0
    watchdog = _ProgressWatchdog(evidence, 0.5)

    watchdog.start()
    deadline = time.monotonic() + 1.0
    while evidence.watchdog_cycles == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    watchdog.stop()

    assert evidence.watchdog_cycles > 0
    assert 0 < evidence.watchdog_max_gap_seconds <= 0.5


def test_gate2_capability_manifest_requires_reviewed_matching_nonempty_evidence(tmp_path):
    commit = "a" * 40
    entries = []
    definitions = (
        (TRADE_TIMESTAMP, "HDFSCNT0", TIMESTAMP_INTERPRETATION),
        (QUOTE_TIMESTAMP, "HDFSASP0", TIMESTAMP_INTERPRETATION),
        (TRADE_SEQUENCE, "HDFSCNT0", "MONOTONIC"),
        (QUOTE_SEQUENCE, "HDFSASP0", "NO_USABLE_SEQUENCE"),
        (EXECUTION_NOTICE, "H0GSCNI0", NOTICE_INTERPRETATION),
    )
    for index, (capability_id, tr_id, interpretation) in enumerate(definitions):
        evidence_path = tmp_path / f"evidence-{index}.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "capability_id": capability_id,
                    "environment": "PROD",
                    "tr_id": tr_id,
                    "interpretation": interpretation,
                    "observed_at": "2026-08-17T00:00:00Z",
                    "observations": [{"frame_count": 10}],
                    **(
                        {
                            "sequence_field": "EVOL",
                            "reset_semantics": "RESET_ON_RECONNECT",
                        }
                        if capability_id == TRADE_SEQUENCE
                        else {}
                    ),
                }
            ),
            encoding="utf-8",
        )
        entries.append(
            {
                "capability_id": capability_id,
                "status": "VERIFIED",
                "environment": "PROD",
                "tr_id": tr_id,
                "interpretation": interpretation,
                "evidence_file": evidence_path.name,
                "evidence_sha256": sha256_file(evidence_path),
                **(
                    {
                        "sequence_field": "EVOL",
                        "reset_semantics": "RESET_ON_RECONNECT",
                    }
                    if capability_id == TRADE_SEQUENCE
                    else {}
                ),
            }
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit_sha": commit,
                "environment": "PROD",
                "review": {
                    "status": "APPROVED",
                    "author": "bundle-author",
                    "reviewer": "independent-reviewer",
                    "reviewed_at": "2026-08-17T00:00:00Z",
                    "method": "PROCEDURAL_DUAL_CONTROL",
                    "reference": "dual-control-log:gate2-review-1",
                },
                "capabilities": entries,
            }
        ),
        encoding="utf-8",
    )

    verified = load_verified_capability_manifest(
        manifest_path, expected_commit=commit, expected_environment="PROD"
    )

    assert verified.confirmed_sequence_channels == ("HDFSCNT0",)

    same_reviewer = json.loads(manifest_path.read_text(encoding="utf-8"))
    same_reviewer["review"]["reviewer"] = same_reviewer["review"]["author"]
    manifest_path.write_text(json.dumps(same_reviewer), encoding="utf-8")
    with pytest.raises(ValueError, match="reviewer must differ"):
        load_verified_capability_manifest(
            manifest_path, expected_commit=commit, expected_environment="PROD"
        )

    (tmp_path / "evidence-0.json").write_text("{}", encoding="utf-8")
    entries[0]["evidence_sha256"] = sha256_file(tmp_path / "evidence-0.json")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit_sha": commit,
                "environment": "PROD",
                "review": {
                    "status": "APPROVED",
                    "author": "bundle-author",
                    "reviewer": "independent-reviewer",
                    "reviewed_at": "2026-08-17T00:00:00Z",
                    "method": "PROCEDURAL_DUAL_CONTROL",
                    "reference": "dual-control-log:gate2-review-1",
                },
                "capabilities": entries,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="lacks the required"):
        load_verified_capability_manifest(
            manifest_path, expected_commit=commit, expected_environment="PROD"
        )
