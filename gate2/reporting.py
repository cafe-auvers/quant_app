"""Standalone, mutation-free Gate-2 KIS WebSocket soak reporter."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time as wall_time
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from src.core import execution_config
from src.core.runtime_safety_audit import (
    BROKER_MUTATION_AUDIT_SOURCE,
    ENTRY_READINESS_AUDIT_SOURCE,
    GATE2_REQUIRED_AUDIT_SOURCES,
    RuntimeSafetyAuditSession,
    begin_runtime_safety_audit,
)
# Importing the sole real broker adapter registers its instrumented boundary.
# Gate 2 never constructs it or any execution workflow.
from src.services import broker as _broker_audit_boundary  # noqa: F401
from src.services.kis_realtime_market_data import (
    FeedChannel,
    KisRealtimeMarketDataService,
    StopRule,
    SubscriptionPriority,
    build_kis_realtime_market_data_from_environment,
)
from src.services.kis_ws_symbol_keys import KisWsSymbolKeyStore
from src.api.kis_websocket import KisWsProtocolOperation
from src.services.realtime_market_data import QuoteSnapshot
from src.services.trading_state import is_trading_enabled
from src.utils.market_calendar import (
    US_MARKET_OPEN_TIME,
    US_MARKET_ZONE,
    nyse_holidays,
    nyse_regular_session_close_time,
)
from gate2.capabilities import (
    EXECUTION_NOTICE,
    QUOTE_SEQUENCE,
    QUOTE_TIMESTAMP,
    SHA256_PATTERN,
    TRADE_SEQUENCE,
    TRADE_TIMESTAMP,
    capability_snapshot_complete,
    load_verified_capability_manifest,
)


KST_ZONE = ZoneInfo("Asia/Seoul")
SAFE_RUNTIME_EXPECTATIONS = {
    "TRADING_ENABLED": False,
    "BUYBOARD_ENGINE_ENABLED": True,
    "KIS_WS_ENABLED": True,
    "KIS_WS_PROTOCOL_VERIFIED": True,
    "KIS_MUTATION_BUDGET_VERIFIED": False,
    "KIS_SUBMIT_MUTATION_CAPACITY": 0,
    "KIS_CANCEL_MUTATION_CAPACITY": 0,
    "KIS_REPLACE_MUTATION_CAPACITY": 0,
    "KIS_LIVE_EXECUTION_MODE": "DISABLED",
    "KIS_CONTROLLED_LIVE_SYMBOLS": [],
    "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL": 0.0,
}


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def runtime_activation_snapshot() -> dict[str, bool | int | float | str | list[str]]:
    """Read safety state without mutating any flag or composing an engine."""
    return {
        "TRADING_ENABLED": bool(is_trading_enabled()),
        "BUYBOARD_ENGINE_ENABLED": execution_config.is_buyboard_engine_enabled(),
        "KIS_WS_ENABLED": bool(execution_config.KIS_WS_ENABLED),
        "KIS_WS_PROTOCOL_VERIFIED": bool(
            execution_config.KIS_WS_PROTOCOL_VERIFIED
        ),
        "KIS_MUTATION_BUDGET_VERIFIED": bool(
            execution_config.KIS_MUTATION_BUDGET_VERIFIED
        ),
        "KIS_SUBMIT_MUTATION_CAPACITY": int(
            execution_config.KIS_SUBMIT_MUTATION_CAPACITY
        ),
        "KIS_CANCEL_MUTATION_CAPACITY": int(
            execution_config.KIS_CANCEL_MUTATION_CAPACITY
        ),
        "KIS_REPLACE_MUTATION_CAPACITY": int(
            execution_config.KIS_REPLACE_MUTATION_CAPACITY
        ),
        "KIS_LIVE_EXECUTION_MODE": str(
            execution_config.KIS_LIVE_EXECUTION_MODE
        ),
        "KIS_CONTROLLED_LIVE_SYMBOLS": list(
            execution_config.KIS_CONTROLLED_LIVE_SYMBOLS
        ),
        "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL": float(
            execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL
        ),
    }


@dataclass
class Gate2Evidence:
    commit_sha: str
    gate1_report_sha256: str
    capability_matrix_sha256: str
    capability_manifest_sha256: str
    capability_review: dict[str, str]
    verified_capabilities: dict[str, dict[str, str]]
    runtime_confirmed_sequence_channels: list[str]
    runtime_sequence_fields: dict[str, str]
    runtime_sequence_reset_semantics: dict[str, str]
    environment: str
    symbols: list[str]
    tr_ids: list[str]
    verified_subscription_keys: dict[str, str]
    activation_snapshot: dict[str, bool | int]
    session_open: datetime
    session_close: datetime
    started_at: datetime
    ended_at: datetime | None = None
    requested_subscriptions: list[str] = field(default_factory=list)
    acked_subscriptions: list[str] = field(default_factory=list)
    max_aggregate_registration_usage: int = 0
    aggregate_registration_capacity: int = 0
    connection_events: list[dict] = field(default_factory=list)
    disconnects: list[dict] = field(default_factory=list)
    injected_disconnect_request_count: int = 0
    protocol_operations: list[dict] = field(default_factory=list)
    duplicate_request_probe: dict[str, int] = field(default_factory=dict)
    reconnect_recovery_seconds: list[float] = field(default_factory=list)
    stale_detection_seconds: list[float] = field(default_factory=list)
    frame_counts_by_tr_id: dict[str, int] = field(default_factory=dict)
    record_counts_by_tr_id: dict[str, int] = field(default_factory=dict)
    schema_fingerprints_by_tr_id: dict[str, str] = field(default_factory=dict)
    parser_failure_count: int = 0
    malformed_frame_count: int = 0
    duplicate_event_count: int = 0
    duplicate_subscription_anomaly_count: int = 0
    receive_lag_ms: dict[str, float] = field(default_factory=dict)
    queue_lag_ms: dict[str, float] = field(default_factory=dict)
    synthetic_stop_tests: dict[str, int] = field(default_factory=dict)
    silent_stale_probe: dict = field(default_factory=dict)
    watchdog_cycles: int = 0
    deadlock_count: int = 0
    watchdog_max_gap_seconds: float = 0.0
    watchdog_timeout_seconds: float = 0.0
    continuity_sample_count: int = 0
    continuity_unexpected_unready_count: int = 0
    continuity_started_at: str = ""
    continuity_start_delay_seconds: float = 0.0
    poll_interval_seconds: float = 0.0
    safety_audit_initialized: bool = False
    safety_audit_sources: list[str] = field(default_factory=list)
    broker_mutation_count: int | None = None
    entry_readiness_check_count: int | None = None
    stale_entry_readiness_check_count: int | None = None
    stale_entry_readiness_rejection_count: int | None = None
    stale_entry_readiness_allow_count: int | None = None
    unhandled_disconnect_count: int = 0
    secret_leak_count: int = 0
    log_scan_completed: bool = False
    log_capture_sha256: str = ""
    log_bytes_scanned: int = 0
    sensitive_value_count: int = 0
    approval_key_count: int = 0
    log_redaction_count: int = 0
    redacted_evidence_sha256: dict[str, str] = field(default_factory=dict)
    later_execution_blockers: list[str] = field(default_factory=list)
    operator_abort_reasons: list[str] = field(default_factory=list)


def _metric(
    *, value: float | int, threshold: str, passed: bool, numerator=None, denominator=None
) -> dict:
    item = {"value": value, "threshold": threshold, "result": "PASSED" if passed else "FAILED"}
    if numerator is not None:
        item["numerator"] = numerator
    if denominator is not None:
        item["denominator"] = denominator
    return item


def _review_snapshot_complete(review: Mapping[str, str]) -> bool:
    author = str(review.get("author") or "").strip()
    reviewer = str(review.get("reviewer") or "").strip()
    if (
        review.get("status") != "APPROVED"
        or not author
        or not reviewer
        or author.casefold() == reviewer.casefold()
        or str(review.get("method") or "")
        not in {"GITHUB_PR_REVIEW", "SIGNED_ATTESTATION", "PROCEDURAL_DUAL_CONTROL"}
        or not str(review.get("reference") or "").strip()
    ):
        return False
    try:
        reviewed_at = datetime.fromisoformat(
            str(review.get("reviewed_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return False
    return reviewed_at.tzinfo is not None


def build_report(evidence: Gate2Evidence) -> dict:
    """Build the deterministic certification decision from observed evidence."""
    ended_at = evidence.ended_at or evidence.started_at
    requested = set(evidence.requested_subscriptions)
    acked = set(evidence.acked_subscriptions)
    recovered = evidence.reconnect_recovery_seconds
    receive = evidence.receive_lag_ms
    queue = evidence.queue_lag_ms
    stop = evidence.synthetic_stop_tests
    injected_disconnects = [
        item for item in evidence.disconnects if item.get("classification") == "INJECTED"
    ]
    unexpected_disconnects = [
        item for item in evidence.disconnects if item.get("classification") == "UNEXPECTED"
    ]
    unresolved_disconnects = [
        item for item in evidence.disconnects if not item.get("reacked_at")
    ]
    unhandled_disconnects = [
        item
        for item in evidence.disconnects
        if item.get("classification") == "UNEXPECTED" or not item.get("reacked_at")
    ]
    activation_ok = all(
        evidence.activation_snapshot.get(key) == expected
        for key, expected in SAFE_RUNTIME_EXPECTATIONS.items()
    )
    capability_ok = capability_snapshot_complete(
        evidence.verified_capabilities, environment=evidence.environment
    )
    audit_sources_ok = GATE2_REQUIRED_AUDIT_SOURCES <= set(
        evidence.safety_audit_sources
    )
    monotonic_capabilities = [
        evidence.verified_capabilities.get(item, {})
        for item in (TRADE_SEQUENCE, QUOTE_SEQUENCE)
        if evidence.verified_capabilities.get(item, {}).get("interpretation")
        == "MONOTONIC"
    ]
    expected_sequence_channels = sorted(
        str(item.get("tr_id") or "") for item in monotonic_capabilities
    )
    expected_sequence_fields = {
        str(item.get("tr_id") or ""): str(item.get("sequence_field") or "")
        for item in monotonic_capabilities
    }
    expected_sequence_resets = {
        str(item.get("tr_id") or ""): str(item.get("reset_semantics") or "")
        for item in monotonic_capabilities
    }
    sequence_ok = capability_ok and sorted(
        evidence.runtime_confirmed_sequence_channels
    ) == expected_sequence_channels and (
        evidence.runtime_sequence_fields == expected_sequence_fields
        and evidence.runtime_sequence_reset_semantics == expected_sequence_resets
    )
    redacted_evidence_ok = bool(evidence.redacted_evidence_sha256) and all(
        str(name or "").strip()
        and SHA256_PATTERN.fullmatch(str(digest or ""))
        for name, digest in evidence.redacted_evidence_sha256.items()
    )
    continuity_start = (
        datetime.fromisoformat(evidence.continuity_started_at)
        if evidence.continuity_started_at
        else evidence.session_close
    )
    scheduled_session_seconds = max(
        0.0, (evidence.session_close - continuity_start).total_seconds()
    )
    expected_continuity_samples = (
        int(scheduled_session_seconds / evidence.poll_interval_seconds * 0.95)
        if evidence.poll_interval_seconds > 0
        else 0
    )
    metrics = {
        "full_regular_session": _metric(
            value=max(0.0, (ended_at - evidence.started_at).total_seconds()),
            threshold="started_at<=session_open and ended_at>=session_close",
            passed=(
                evidence.started_at <= evidence.session_open
                and ended_at >= evidence.session_close
            ),
        ),
        "critical_subscription_ack": _metric(
            value=len(requested & acked),
            numerator=len(requested & acked),
            denominator=len(requested),
            threshold="100%",
            passed=bool(requested) and requested <= acked,
        ),
        "aggregate_registration_budget": _metric(
            value=evidence.max_aggregate_registration_usage,
            threshold=f"<= {evidence.aggregate_registration_capacity}",
            passed=(
                evidence.aggregate_registration_capacity > 0
                and evidence.max_aggregate_registration_usage
                <= evidence.aggregate_registration_capacity
            ),
        ),
        "market_data_frame_coverage": _metric(
            value=sum(
                evidence.frame_counts_by_tr_id.get(tr_id, 0)
                for tr_id in ("HDFSCNT0", "HDFSASP0")
            ),
            threshold="at least one HDFSCNT0 and HDFSASP0 frame",
            passed=all(
                evidence.frame_counts_by_tr_id.get(tr_id, 0) > 0
                for tr_id in ("HDFSCNT0", "HDFSASP0")
            ),
        ),
        "parser_failures": _metric(
            value=evidence.parser_failure_count + evidence.malformed_frame_count,
            threshold="0",
            passed=evidence.parser_failure_count + evidence.malformed_frame_count == 0,
        ),
        "unhandled_disconnects": _metric(
            value=len(unhandled_disconnects),
            threshold="every disconnect classified; no unexpected or unresolved disconnect",
            passed=(
                bool(evidence.disconnects)
                and not unexpected_disconnects
                and not unresolved_disconnects
                and evidence.unhandled_disconnect_count == 0
            ),
        ),
        "critical_ack_recovery_seconds": _metric(
            value=max(recovered, default=0.0),
            numerator=len(recovered),
            denominator=evidence.injected_disconnect_request_count,
            threshold="at least 1 injected reconnect; every recovery < 10s",
            passed=(
                bool(recovered)
                and len(recovered)
                == evidence.injected_disconnect_request_count
                == len(injected_disconnects)
                and max(recovered) < 10.0
            ),
        ),
        "stale_detection_seconds": _metric(
            value=float(evidence.silent_stale_probe.get("detection_seconds", 0.0)),
            threshold="connected silent-channel stall detected and recovered in <=3s",
            passed=(
                evidence.silent_stale_probe.get("connected_during_probe") is True
                and evidence.silent_stale_probe.get(
                    "entry_readiness_ready_before_probe"
                )
                is True
                and evidence.silent_stale_probe.get("detected") is True
                and evidence.silent_stale_probe.get("recovered") is True
                and float(evidence.silent_stale_probe.get("detection_seconds", 99.0))
                <= 3.0
            ),
        ),
        "stale_entry_readiness_fence": _metric(
            value=(
                evidence.stale_entry_readiness_allow_count
                if evidence.stale_entry_readiness_allow_count is not None
                else -1
            ),
            numerator=evidence.stale_entry_readiness_rejection_count,
            denominator=evidence.stale_entry_readiness_check_count,
            threshold=(
                "initialized real entry-readiness audit; controlled stale "
                "window rejected at least once and never allowed"
            ),
            passed=(
                evidence.safety_audit_initialized
                and audit_sources_ok
                and ENTRY_READINESS_AUDIT_SOURCE in evidence.safety_audit_sources
                and evidence.silent_stale_probe.get("detected") is True
                and evidence.silent_stale_probe.get(
                    "entry_readiness_ready_before_probe"
                )
                is True
                and evidence.silent_stale_probe.get(
                    "entry_readiness_ready_while_stale"
                )
                is False
                and evidence.entry_readiness_check_count is not None
                and evidence.stale_entry_readiness_check_count is not None
                and evidence.stale_entry_readiness_rejection_count is not None
                and evidence.stale_entry_readiness_allow_count is not None
                and evidence.stale_entry_readiness_check_count > 0
                and evidence.entry_readiness_check_count
                >= evidence.stale_entry_readiness_check_count
                and evidence.stale_entry_readiness_rejection_count > 0
                and evidence.stale_entry_readiness_allow_count == 0
                and evidence.stale_entry_readiness_check_count
                == evidence.stale_entry_readiness_rejection_count
                + evidence.stale_entry_readiness_allow_count
            ),
        ),
        "duplicate_subscription_corruption": _metric(
            value=evidence.duplicate_subscription_anomaly_count,
            threshold="actual protocol operations observed; 0 duplicate/invalid transitions",
            passed=(
                bool(evidence.protocol_operations)
                and evidence.duplicate_request_probe.get("requests_attempted", 0) >= 4
                and evidence.duplicate_request_probe.get(
                    "unexpected_protocol_operations", 1
                )
                == 0
                and evidence.duplicate_subscription_anomaly_count == 0
            ),
        ),
        "synthetic_stop_breaches": _metric(
            value=stop.get("consumed", 0),
            numerator=stop.get("consumed", 0),
            denominator=stop.get("injected", 0),
            threshold="injected=latched=consumed and injected>0",
            passed=(
                stop.get("injected", 0) > 0
                and stop.get("accepted_by_live_service") == stop.get("injected")
                and stop.get("injected") == stop.get("latched")
                == stop.get("consumed")
            ),
        ),
        "watchdog_deadlocks": _metric(
            value=evidence.deadlock_count,
            threshold="independent watchdog measured progress with 0 stalls",
            passed=(
                evidence.deadlock_count == 0
                and evidence.watchdog_cycles > 0
                and evidence.watchdog_timeout_seconds > 0
                and evidence.watchdog_timeout_seconds
                > evidence.poll_interval_seconds * 2
                and evidence.watchdog_max_gap_seconds
                <= evidence.watchdog_timeout_seconds
            ),
        ),
        "full_session_continuity": _metric(
            value=evidence.continuity_unexpected_unready_count,
            denominator=evidence.continuity_sample_count,
            threshold="session-wide samples>0 and no unexplained critical unready sample",
            passed=(
                expected_continuity_samples > 0
                and 0 < evidence.poll_interval_seconds <= 0.25
                and bool(evidence.continuity_started_at)
                and evidence.continuity_start_delay_seconds <= 3.0
                and evidence.continuity_sample_count >= expected_continuity_samples
                and evidence.continuity_unexpected_unready_count == 0
            ),
        ),
        "receive_lag_p95_ms": _metric(
            value=receive.get("p95", 0.0),
            threshold="< 1000ms",
            passed=receive.get("count", 0) > 0 and receive.get("p95", 0.0) < 1000.0,
        ),
        "receive_lag_p99_ms": _metric(
            value=receive.get("p99", 0.0),
            threshold="< 2000ms",
            passed=receive.get("count", 0) > 0 and receive.get("p99", 0.0) < 2000.0,
        ),
        "queue_lag_p99_ms": _metric(
            value=queue.get("p99", 0.0),
            threshold=(
                f"<= {execution_config.MAX_MARKET_DATA_QUEUE_DELAY_SECONDS * 1000.0:g}ms"
            ),
            passed=(
                queue.get("count", 0) > 0
                and queue.get("p99", 0.0)
                <= execution_config.MAX_MARKET_DATA_QUEUE_DELAY_SECONDS * 1000.0
            ),
        ),
        "secret_leaks": _metric(
            value=evidence.secret_leak_count,
            threshold="captured logs and issued approval key scanned; 0 leaks",
            passed=(
                evidence.log_scan_completed
                and evidence.sensitive_value_count > 0
                and evidence.approval_key_count > 0
                and evidence.secret_leak_count == 0
                and bool(SHA256_PATTERN.fullmatch(evidence.log_capture_sha256))
            ),
        ),
        "broker_mutations": _metric(
            value=(
                evidence.broker_mutation_count
                if evidence.broker_mutation_count is not None
                else -1
            ),
            threshold="initialized real KIS broker-boundary audit; 0 attempts",
            passed=(
                evidence.safety_audit_initialized
                and audit_sources_ok
                and BROKER_MUTATION_AUDIT_SOURCE in evidence.safety_audit_sources
                and evidence.broker_mutation_count == 0
            ),
        ),
        "runtime_activation_fence": _metric(
            value=int(activation_ok),
            threshold="read-only Gate-2 activation snapshot",
            passed=activation_ok,
        ),
        "capability_manifest": _metric(
            value=int(capability_ok),
            threshold="reviewed, exact-commit, digest-verified WS0 capability bundle",
            passed=(
                capability_ok
                and bool(SHA256_PATTERN.fullmatch(evidence.capability_manifest_sha256))
                and _review_snapshot_complete(evidence.capability_review)
            ),
        ),
        "timestamp_evidence": _metric(
            value=int(
                all(
                    item in evidence.verified_capabilities
                    for item in (TRADE_TIMESTAMP, QUOTE_TIMESTAMP)
                )
            ),
            threshold="both strict timestamp capabilities verified",
            passed=capability_ok,
        ),
        "sequence_semantics": _metric(
            value=int(sequence_ok),
            threshold="verified findings exactly configure runtime enforcement",
            passed=sequence_ok,
        ),
        "execution_notice_evidence": _metric(
            value=int(EXECUTION_NOTICE in evidence.verified_capabilities),
            threshold="strict execution-notice capability verified",
            passed=capability_ok,
        ),
        "redacted_evidence_bundle": _metric(
            value=len(evidence.redacted_evidence_sha256),
            threshold="at least one named redacted raw-evidence file with SHA-256",
            passed=redacted_evidence_ok,
        ),
    }
    blockers = sorted(
        [name for name, metric in metrics.items() if metric["result"] != "PASSED"]
        + list(evidence.operator_abort_reasons)
    )
    return {
        "schema_version": 1,
        "gate": "GATE_2_LIVE_KIS_READ_ONLY_SOAK",
        "result": "PASSED" if not blockers else "FAILED",
        "generated_at": _iso(ended_at),
        "commit_sha": evidence.commit_sha,
        "gate1_report_sha256": evidence.gate1_report_sha256,
        "capability_matrix_sha256": evidence.capability_matrix_sha256,
        "capability_manifest_sha256": evidence.capability_manifest_sha256,
        "capability_review": evidence.capability_review,
        "verified_capabilities": evidence.verified_capabilities,
        "environment": evidence.environment,
        "session": {
            "start_utc": _iso(evidence.started_at),
            "end_utc": _iso(ended_at),
            "scheduled_open_utc": _iso(evidence.session_open),
            "scheduled_close_utc": _iso(evidence.session_close),
            "start_kst": evidence.started_at.astimezone(KST_ZONE).isoformat(),
            "end_kst": ended_at.astimezone(KST_ZONE).isoformat(),
            "start_us_eastern": evidence.started_at.astimezone(US_MARKET_ZONE).isoformat(),
            "end_us_eastern": ended_at.astimezone(US_MARKET_ZONE).isoformat(),
        },
        "symbols": sorted(evidence.symbols),
        "tr_ids": sorted(evidence.tr_ids),
        "verified_subscription_keys": dict(sorted(evidence.verified_subscription_keys.items())),
        "requested_subscriptions": sorted(requested),
        "acked_subscriptions": sorted(acked),
        "max_aggregate_registration_usage": evidence.max_aggregate_registration_usage,
        "aggregate_registration_capacity": evidence.aggregate_registration_capacity,
        "connection_events": evidence.connection_events,
        "disconnects": evidence.disconnects,
        "injected_disconnect_request_count": evidence.injected_disconnect_request_count,
        "protocol_operations": evidence.protocol_operations,
        "duplicate_request_probe": evidence.duplicate_request_probe,
        "reconnect_recovery_seconds": recovered,
        "stale_detection_seconds": evidence.stale_detection_seconds,
        "frame_counts_by_tr_id": dict(sorted(evidence.frame_counts_by_tr_id.items())),
        "record_counts_by_tr_id": dict(sorted(evidence.record_counts_by_tr_id.items())),
        "schema_fingerprints_by_tr_id": dict(sorted(evidence.schema_fingerprints_by_tr_id.items())),
        "parser_failure_count": evidence.parser_failure_count,
        "malformed_frame_count": evidence.malformed_frame_count,
        "duplicate_event_count": evidence.duplicate_event_count,
        "duplicate_subscription_anomaly_count": evidence.duplicate_subscription_anomaly_count,
        "receive_lag_ms": evidence.receive_lag_ms,
        "queue_lag_ms": evidence.queue_lag_ms,
        "synthetic_stop_tests": evidence.synthetic_stop_tests,
        "silent_stale_probe": evidence.silent_stale_probe,
        "watchdog_cycles": evidence.watchdog_cycles,
        "watchdog_max_gap_seconds": evidence.watchdog_max_gap_seconds,
        "watchdog_timeout_seconds": evidence.watchdog_timeout_seconds,
        "continuity_sample_count": evidence.continuity_sample_count,
        "continuity_started_at": evidence.continuity_started_at,
        "continuity_start_delay_seconds": evidence.continuity_start_delay_seconds,
        "expected_continuity_sample_count": expected_continuity_samples,
        "poll_interval_seconds": evidence.poll_interval_seconds,
        "continuity_unexpected_unready_count": (
            evidence.continuity_unexpected_unready_count
        ),
        "log_scan": {
            "completed": evidence.log_scan_completed,
            "capture_sha256": evidence.log_capture_sha256,
            "bytes_scanned": evidence.log_bytes_scanned,
            "sensitive_value_count": evidence.sensitive_value_count,
            "approval_key_count": evidence.approval_key_count,
            "redaction_count": evidence.log_redaction_count,
            "leak_count": evidence.secret_leak_count,
        },
        "runtime_safety_audit": {
            "initialized": evidence.safety_audit_initialized,
            "sources": sorted(evidence.safety_audit_sources),
            "broker_mutation_attempt_count": evidence.broker_mutation_count,
            "entry_readiness_check_count": evidence.entry_readiness_check_count,
            "stale_entry_readiness_check_count": (
                evidence.stale_entry_readiness_check_count
            ),
            "stale_entry_readiness_rejection_count": (
                evidence.stale_entry_readiness_rejection_count
            ),
            "stale_entry_readiness_allow_count": (
                evidence.stale_entry_readiness_allow_count
            ),
        },
        "runtime_confirmed_sequence_channels": sorted(
            evidence.runtime_confirmed_sequence_channels
        ),
        "runtime_sequence_fields": dict(sorted(evidence.runtime_sequence_fields.items())),
        "runtime_sequence_reset_semantics": dict(
            sorted(evidence.runtime_sequence_reset_semantics.items())
        ),
        "redacted_evidence_sha256": dict(sorted(evidence.redacted_evidence_sha256.items())),
        "activation_snapshot": evidence.activation_snapshot,
        "production_activation_authorized": False,
        "broker_mutations": evidence.broker_mutation_count,
        "later_execution_blockers": evidence.later_execution_blockers,
        "metrics": metrics,
        "blockers": blockers,
    }


def _session_bounds(session_day: date) -> tuple[datetime, datetime]:
    if session_day.weekday() >= 5 or session_day in nyse_holidays(session_day.year):
        raise ValueError(f"{session_day.isoformat()} is not an NYSE trading day")
    opened = datetime.combine(session_day, US_MARKET_OPEN_TIME, US_MARKET_ZONE)
    closed = datetime.combine(
        session_day, nyse_regular_session_close_time(session_day), US_MARKET_ZONE
    )
    return opened.astimezone(timezone.utc), closed.astimezone(timezone.utc)


def _synthetic_stop_test(service: KisRealtimeMarketDataService) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    service.replace_stop_rules("GATE2", [StopRule("gate2", 100.0, "v1")])
    accepted = service.ingest_trade(
        QuoteSnapshot(
            symbol="GATE2",
            last_price=99.0,
            received_at=now,
            broker_event_at=now,
            processed_at=now,
            source="GATE2_SYNTHETIC",
            channel="HDFSCNT0",
            payload_fingerprint="gate2-stop-test",
        )
    )
    detached = service.poll_once()
    latched = int(
        any(("gate2", "v1") in event.breached_stop_versions for event in detached)
    )
    consumed = int(service.acknowledge_stop_breach("GATE2", "gate2", "v1"))
    return {
        "injected": 1,
        "accepted_by_live_service": int(accepted),
        "latched": latched,
        "consumed": consumed,
    }


class LiveGate2Runner:
    """Poll only the KIS market-data service and assemble qualification evidence."""

    def __init__(
        self,
        service: KisRealtimeMarketDataService,
        evidence: Gate2Evidence,
        safety_audit: RuntimeSafetyAuditSession,
    ):
        self.service = service
        self.evidence = evidence
        self.safety_audit = safety_audit
        self._expected_disconnects = 0
        self._operation_states: dict[tuple[int, str, str], str] = {}
        self._silent_probe_phase = ""
        self.current_feed_ready = False
        service.on_session(self._on_session)
        service.on_protocol_operation(self._on_protocol_operation)

    @staticmethod
    def _reason_evidence(reason: str) -> dict[str, str | bool]:
        value = str(reason or "")
        return {
            "reason_present": bool(value),
            "reason_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()
            if value
            else "",
        }

    def _operation_label(self, operation: KisWsProtocolOperation) -> str:
        if operation.tr_id in {"H0GSCNI0", "H0GSCNI9"}:
            return "EXECUTION_NOTICE"
        for symbol, key in self.evidence.verified_subscription_keys.items():
            if key == operation.tr_key:
                return symbol
        return "UNMAPPED_" + hashlib.sha256(
            operation.tr_key.encode("utf-8")
        ).hexdigest()[:12]

    def _on_protocol_operation(self, operation: KisWsProtocolOperation) -> None:
        label = self._operation_label(operation)
        identity = (operation.generation, operation.tr_id, label)
        previous = self._operation_states.get(identity, "")
        anomaly = False
        if operation.action == "SUBSCRIBE":
            anomaly = previous in {"SUBSCRIBE_SENT", "SUBSCRIBED"}
            self._operation_states[identity] = "SUBSCRIBE_SENT"
        elif operation.action == "UNSUBSCRIBE":
            anomaly = previous not in {"SUBSCRIBE_SENT", "SUBSCRIBED"}
            self._operation_states[identity] = "UNSUBSCRIBE_SENT"
        else:
            anomaly = True
        if anomaly:
            self.evidence.duplicate_subscription_anomaly_count += 1
        self.evidence.protocol_operations.append(
            {
                "generation": operation.generation,
                "action": operation.action,
                "tr_id": operation.tr_id,
                "registration": label,
                "sent_at": _iso(operation.sent_at),
                "transition_valid": not anomaly,
            }
        )

    def _on_session(
        self, connected: bool, reason: str, generation: int, observed_at: datetime
    ) -> None:
        event = {
            "kind": "CONNECTED" if connected else "DISCONNECTED",
            "at": _iso(observed_at),
            "generation": generation,
            **self._reason_evidence(reason),
        }
        self.evidence.connection_events.append(event)
        if connected:
            for disconnect in reversed(self.evidence.disconnects):
                if disconnect.get("reconnected_at") is None:
                    disconnect["reconnected_at"] = _iso(observed_at)
                    disconnect["reconnect_generation"] = generation
                    break
            return
        classification = "INJECTED" if self._expected_disconnects else "UNEXPECTED"
        if self._expected_disconnects:
            self._expected_disconnects -= 1
        self.evidence.disconnects.append(
            {
                "classification": classification,
                "disconnect_at": _iso(observed_at),
                "generation": generation,
                "reconnected_at": None,
                "reacked_at": None,
                "recovery_seconds": None,
                **self._reason_evidence(reason),
            }
        )

    def inject_disconnect(self) -> None:
        now = datetime.now(timezone.utc)
        self._expected_disconnects += 1
        self.evidence.injected_disconnect_request_count += 1
        self.evidence.connection_events.append(
            {"kind": "DISCONNECT_REQUESTED", "classification": "INJECTED", "at": _iso(now)}
        )
        self.service.reconnect()

    def start_silent_stale_probe(
        self, *, symbol: str, channel: FeedChannel, now: datetime
    ) -> None:
        if self.evidence.silent_stale_probe:
            return
        state = self.service.symbol_state(symbol)
        last_received = (
            state.last_trade_received_at
            if channel == FeedChannel.TRADE
            else state.last_quote_received_at
        )
        last_event = (
            state.last_trade_event_at
            if channel == FeedChannel.TRADE
            else state.last_quote_event_at
        )
        connected = self.service.is_connected()
        if not connected or last_received is None or last_event is None:
            return
        if not self.service.entry_quote_ready(symbol, now=now):
            return
        self.service.set_qualification_channel_suppressed(symbol, channel, True)
        self._silent_probe_phase = "SUPPRESSED"
        self.evidence.silent_stale_probe = {
            "symbol": symbol,
            "channel": channel.value,
            "started_at": _iso(now),
            "last_received_at": _iso(last_received),
            "last_broker_event_at": _iso(last_event),
            "connected_during_probe": connected,
            "entry_readiness_ready_before_probe": True,
            "detected": False,
            "detection_seconds": None,
            "detected_at": None,
            "recovered": False,
            "recovered_at": None,
        }

    def _advance_silent_probe(self, now: datetime, stale_symbols: set[str]) -> None:
        probe = self.evidence.silent_stale_probe
        if not probe or self._silent_probe_phase not in {"SUPPRESSED", "RECOVERING"}:
            return
        symbol = str(probe["symbol"])
        channel = FeedChannel(str(probe["channel"]))
        probe["connected_during_probe"] = bool(
            probe["connected_during_probe"] and self.service.is_connected()
        )
        if self._silent_probe_phase == "SUPPRESSED" and symbol in stale_symbols:
            last_received_at = datetime.fromisoformat(str(probe["last_received_at"]))
            last_broker_event_at = datetime.fromisoformat(
                str(probe["last_broker_event_at"])
            )
            detected_after = max(
                0.0,
                (now - min(last_received_at, last_broker_event_at)).total_seconds(),
            )
            probe["detected"] = True
            probe["detection_seconds"] = detected_after
            probe["detected_at"] = _iso(now)
            self.evidence.stale_detection_seconds.append(detected_after)
            self.safety_audit.begin_stale_entry_probe(symbol)
            try:
                probe["entry_readiness_ready_while_stale"] = bool(
                    self.service.entry_quote_ready(symbol, now=now)
                )
            finally:
                self.safety_audit.end_stale_entry_probe(symbol)
            self.service.set_qualification_channel_suppressed(symbol, channel, False)
            self._silent_probe_phase = "RECOVERING"
        elif self._silent_probe_phase == "RECOVERING" and symbol not in stale_symbols:
            probe["recovered"] = True
            probe["recovered_at"] = _iso(now)
            self._silent_probe_phase = "COMPLETE"

    def sample(self, now: datetime) -> None:
        health = self.service.health_metrics(now=now)
        capacity = self.service.subscription_capacity_snapshot()
        protocol = self.service.protocol_metrics_snapshot()
        self.evidence.max_aggregate_registration_usage = max(
            self.evidence.max_aggregate_registration_usage,
            capacity.max_occupied_count,
        )
        if capacity.occupied_count > capacity.total_capacity:
            self.evidence.duplicate_subscription_anomaly_count += 1
        acked = []
        for symbol in self.evidence.symbols:
            state = self.service.symbol_state(symbol)
            if state.trade_acked:
                acked.append(f"HDFSCNT0:{symbol}")
            if state.quote_acked:
                acked.append(f"HDFSASP0:{symbol}")
        notice_tr_id = "H0GSCNI0" if self.evidence.environment == "PROD" else "H0GSCNI9"
        if capacity.execution_notice_acked:
            acked.append(f"{notice_tr_id}:EXECUTION_NOTICE")
        self.evidence.acked_subscriptions = sorted(set(acked))
        requested = set(self.evidence.requested_subscriptions)
        acked_set = set(self.evidence.acked_subscriptions)
        if not acked_set <= requested:
            self.evidence.duplicate_subscription_anomaly_count += 1
        if (
            capacity.pending_subscribe_count
            + capacity.active_count
            + capacity.pending_unsubscribe_count
            != capacity.occupied_count
            or capacity.reconnect_replay_count > capacity.total_capacity
            or capacity.active_count != len(acked_set)
        ):
            self.evidence.duplicate_subscription_anomaly_count += 1
        for symbol in self.evidence.symbols:
            state = self.service.symbol_state(symbol)
            generation = state.reconnect_generation
            if state.trade_acked:
                self._operation_states[(generation, "HDFSCNT0", symbol)] = "SUBSCRIBED"
            if state.quote_acked:
                self._operation_states[(generation, "HDFSASP0", symbol)] = "SUBSCRIBED"
        notice_tr_id = "H0GSCNI0" if self.evidence.environment == "PROD" else "H0GSCNI9"
        if capacity.execution_notice_acked:
            generation = max(
                (state.reconnect_generation for state in map(self.service.symbol_state, self.evidence.symbols)),
                default=0,
            )
            self._operation_states[(generation, notice_tr_id, "EXECUTION_NOTICE")] = "SUBSCRIBED"

        critical_ready = requested <= acked_set
        for disconnect in self.evidence.disconnects:
            if disconnect.get("reconnected_at") and not disconnect.get("reacked_at") and critical_ready:
                disconnected_at = datetime.fromisoformat(str(disconnect["disconnect_at"]))
                recovery = max(0.0, (now - disconnected_at).total_seconds())
                disconnect["reacked_at"] = _iso(now)
                disconnect["recovery_seconds"] = recovery
                self.evidence.reconnect_recovery_seconds.append(recovery)

        stale_symbols = set(health.stale_symbols)
        self._advance_silent_probe(now, stale_symbols)
        self.current_feed_ready = critical_ready and not stale_symbols
        if self.evidence.session_open <= now <= self.evidence.session_close:
            if not self.evidence.continuity_started_at and self.current_feed_ready:
                self.evidence.continuity_started_at = _iso(now) or ""
                self.evidence.continuity_start_delay_seconds = max(
                    0.0, (now - self.evidence.session_open).total_seconds()
                )
            if self.evidence.continuity_started_at:
                self.evidence.continuity_sample_count += 1
                expected_gap = bool(
                    any(not item.get("reacked_at") for item in self.evidence.disconnects)
                    or self._silent_probe_phase in {"SUPPRESSED", "RECOVERING"}
                )
                if not self.current_feed_ready and not expected_gap:
                    self.evidence.continuity_unexpected_unready_count += 1
        self.evidence.frame_counts_by_tr_id = dict(protocol.frame_counts_by_tr_id)
        self.evidence.record_counts_by_tr_id = dict(protocol.record_counts_by_tr_id)
        self.evidence.schema_fingerprints_by_tr_id = dict(
            protocol.schema_fingerprints_by_tr_id
        )
        self.evidence.parser_failure_count = protocol.parser_failure_count
        self.evidence.malformed_frame_count = health.malformed_frame_count
        self.evidence.duplicate_event_count = protocol.duplicate_event_count
        self.evidence.receive_lag_ms = {
            "count": protocol.receive_lag_sample_count,
            "p50": protocol.receive_lag_p50_ms,
            "p95": protocol.receive_lag_p95_ms,
            "p99": protocol.receive_lag_p99_ms,
            "max": protocol.receive_lag_max_ms,
        }
        self.evidence.queue_lag_ms = {
            "count": protocol.queue_lag_sample_count,
            "p50": protocol.queue_lag_p50_ms,
            "p95": protocol.queue_lag_p95_ms,
            "p99": protocol.queue_lag_p99_ms,
            "max": protocol.queue_lag_max_ms,
        }

    def finalize(self) -> None:
        self.evidence.unhandled_disconnect_count = sum(
            item.get("classification") == "UNEXPECTED" or not item.get("reacked_at")
            for item in self.evidence.disconnects
        )
        if self._expected_disconnects:
            self.evidence.unhandled_disconnect_count += self._expected_disconnects


class _ProgressWatchdog:
    """Independent observer for a stalled reporter/service sampling loop."""

    def __init__(self, evidence: Gate2Evidence, timeout_seconds: float) -> None:
        self._evidence = evidence
        self._timeout = max(0.5, float(timeout_seconds))
        self._last_progress = wall_time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._alarm = False
        self._thread = threading.Thread(
            target=self._run, name="Gate2ProgressWatchdog", daemon=True
        )
        evidence.watchdog_timeout_seconds = self._timeout

    def start(self) -> None:
        self._thread.start()

    def progress(self) -> None:
        with self._lock:
            self._last_progress = wall_time.monotonic()
            self._alarm = False

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(min(0.25, self._timeout / 4.0)):
            with self._lock:
                gap = wall_time.monotonic() - self._last_progress
                self._evidence.watchdog_cycles += 1
                self._evidence.watchdog_max_gap_seconds = max(
                    self._evidence.watchdog_max_gap_seconds, gap
                )
                if gap > self._timeout and not self._alarm:
                    self._evidence.deadlock_count += 1
                    self._alarm = True


class _SecretRedactingFormatter(logging.Formatter):
    """Redact every known static/dynamic credential before log persistence."""

    def __init__(self, *args, sensitive_values: set[str], lock: threading.Lock, **kwargs):
        super().__init__(*args, **kwargs)
        self._sensitive_values = sensitive_values
        self._lock = lock
        self.redaction_count = 0

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        with self._lock:
            values = tuple(self._sensitive_values)
        for value in values:
            occurrences = rendered.count(value)
            if occurrences:
                rendered = rendered.replace(value, "<redacted-secret>")
                self.redaction_count += occurrences
        return rendered


def _write_report(path: Path, report: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_live_soak(args: argparse.Namespace, root: Path) -> int:
    if not args.confirm_read_only:
        raise RuntimeError("--confirm-read-only is required")
    commit = _git(root, "rev-parse", "HEAD")
    if _git(root, "status", "--porcelain"):
        raise RuntimeError("Gate 2 requires a clean exact-commit worktree")
    gate1 = json.loads(args.gate1_report.read_text(encoding="utf-8"))
    if gate1.get("result") != "PASSED" or gate1.get("commit_sha") != commit:
        raise RuntimeError("Gate-1 report must be PASSED on this exact commit")
    activation = runtime_activation_snapshot()
    if any(activation.get(key) != value for key, value in SAFE_RUNTIME_EXPECTATIONS.items()):
        raise RuntimeError("runtime activation snapshot is not read-only Gate-2 safe")
    if execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY <= 0:
        raise RuntimeError("verified aggregate WebSocket capacity is still zero")
    if not 0 < float(args.poll_seconds) <= 0.25:
        raise RuntimeError("Gate-2 poll interval must be in (0, 0.25] seconds")
    if float(args.watchdog_timeout_seconds) <= float(args.poll_seconds) * 2:
        raise RuntimeError("Gate-2 watchdog timeout must exceed two poll intervals")
    if max(
        execution_config.BROKER_EVENT_STALE_SECONDS,
        execution_config.LOCAL_RECEIVE_STALE_SECONDS,
    ) + float(args.poll_seconds) > 3.0:
        raise RuntimeError("configured stale budget cannot meet the 3-second Gate-2 limit")
    hts_id = os.getenv("KIS_WS_HTS_ID", "").strip()
    if not hts_id:
        raise RuntimeError(
            "KIS_WS_HTS_ID is required because execution-notice verification "
            "is part of the current Gate-2 contract"
        )

    symbols = sorted({item.strip().upper() for item in args.symbols.split(",") if item.strip()})
    if not symbols:
        raise ValueError("--symbols must contain at least one symbol")
    requested_slots = len(symbols) * 2 + 1
    if requested_slots > execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY:
        raise RuntimeError(
            f"Gate-2 requests {requested_slots} registrations but aggregate "
            f"capacity is {execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY}"
        )
    symbol_key_store = KisWsSymbolKeyStore()
    symbol_key_snapshot = symbol_key_store.snapshot()
    if symbol_key_snapshot.last_error:
        raise RuntimeError(symbol_key_snapshot.last_error)
    keys = dict(symbol_key_snapshot.keys)
    missing = sorted(set(symbols) - set(keys))
    if missing:
        raise RuntimeError(f"missing verified subscription keys: {', '.join(missing)}")
    session_day = date.fromisoformat(args.session_date)
    session_open, session_close = _session_bounds(session_day)
    now = datetime.now(timezone.utc)
    capability_manifest = load_verified_capability_manifest(
        args.capability_manifest,
        expected_commit=commit,
        expected_environment=args.environment,
    )
    evidence = Gate2Evidence(
        commit_sha=commit,
        gate1_report_sha256=_sha256(args.gate1_report),
        capability_matrix_sha256=_sha256(root / "docs" / "kis_capability_matrix.md"),
        capability_manifest_sha256=capability_manifest.sha256,
        capability_review={
            "status": "APPROVED",
            "author": capability_manifest.review_author,
            "reviewer": capability_manifest.reviewer,
            "reviewed_at": capability_manifest.reviewed_at,
            "method": capability_manifest.review_method,
            "reference": capability_manifest.review_reference,
        },
        verified_capabilities=capability_manifest.capabilities,
        runtime_confirmed_sequence_channels=list(
            capability_manifest.confirmed_sequence_channels
        ),
        runtime_sequence_fields=capability_manifest.sequence_field_by_channel,
        runtime_sequence_reset_semantics=(
            capability_manifest.sequence_reset_by_channel
        ),
        environment=args.environment,
        symbols=symbols,
        tr_ids=["HDFSCNT0", "HDFSASP0", "H0GSCNI0" if args.environment == "PROD" else "H0GSCNI9"],
        verified_subscription_keys={symbol: str(keys[symbol]) for symbol in symbols},
        activation_snapshot=activation,
        session_open=session_open,
        session_close=session_close,
        started_at=now,
        requested_subscriptions=sorted(
            [f"HDFSCNT0:{symbol}" for symbol in symbols]
            + [f"HDFSASP0:{symbol}" for symbol in symbols]
            + [
                f"{'H0GSCNI0' if args.environment == 'PROD' else 'H0GSCNI9'}:"
                "EXECUTION_NOTICE"
            ]
        ),
        aggregate_registration_capacity=execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY,
        watchdog_timeout_seconds=float(args.watchdog_timeout_seconds),
        poll_interval_seconds=float(args.poll_seconds),
        later_execution_blockers=[
            "caller correlation and accepted mutation semantics",
            "broker-order-ID uniqueness scope",
            "history boundary/latency and mutation rate-limit evidence",
        ],
    )
    evidence_names: set[str] = set()
    for path in args.redacted_evidence:
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            pass
        else:
            raise RuntimeError("redacted raw evidence must remain outside the repository")
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            raise RuntimeError(f"redacted raw evidence is missing or empty: {resolved}")
        if resolved.name in evidence_names:
            raise RuntimeError(
                f"redacted raw evidence basenames must be unique: {resolved.name}"
            )
        evidence_names.add(resolved.name)
        evidence.redacted_evidence_sha256[resolved.name] = _sha256(resolved)

    log_path = args.log_output.resolve()
    try:
        log_path.relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError("Gate-2 log capture must remain outside the repository")
    if log_path.exists():
        raise RuntimeError("Gate-2 log capture path already exists")
    sensitive_lock = threading.Lock()
    sensitive_values = {
        os.getenv(name, "")
        for name in (
            "KIS_PROD_APP_KEY",
            "KIS_PROD_APP_SECRET",
            "KIS_SIM_APP_KEY",
            "KIS_SIM_APP_SECRET",
            "KIS_WS_HTS_ID",
        )
        if len(os.getenv(name, "")) >= 6
    }
    approval_values: set[str] = set()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handler = logging.FileHandler(log_path, mode="x", encoding="utf-8")
    redacting_formatter = _SecretRedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        sensitive_values=sensitive_values,
        lock=sensitive_lock,
    )
    log_handler.setFormatter(redacting_formatter)
    log_handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    previous_root_level = root_logger.level
    if previous_root_level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    root_logger.addHandler(log_handler)

    def audit_approval_key(value: str) -> None:
        if len(value) < 6:
            return
        with sensitive_lock:
            approval_values.add(value)
            sensitive_values.add(value)

    def record_critical_alert(message: str) -> None:
        logging.getLogger("gate2.alert").critical("%s", message)
        evidence.operator_abort_reasons.append(
            "critical_alert:"
            + hashlib.sha256(str(message).encode("utf-8")).hexdigest()
        )

    service = build_kis_realtime_market_data_from_environment(
        environment=args.environment,
        critical_alert=record_critical_alert,
        confirmed_sequence_channels=capability_manifest.confirmed_sequence_channels,
        sequence_field_by_channel=capability_manifest.sequence_field_by_channel,
        sequence_reset_by_channel=capability_manifest.sequence_reset_by_channel,
        execution_notice_verified=(
            EXECUTION_NOTICE in capability_manifest.capabilities
        ),
        qualification_mode=True,
        sensitive_value_audit=audit_approval_key,
        symbol_key_store=symbol_key_store,
    )
    priority = {symbol: int(SubscriptionPriority.CRITICAL_EXIT) for symbol in symbols}
    service.configure_desired_channels(trade_priorities=priority, quote_priorities=priority)
    safety_audit = begin_runtime_safety_audit()
    runner = LiveGate2Runner(service, evidence, safety_audit)
    reconnect_offsets = sorted(float(value) for value in args.reconnect_after_seconds)
    next_reconnect = 0
    stop_probe_complete = False
    duplicate_probe_complete = False
    stale_probe_started = False
    watchdog = _ProgressWatchdog(evidence, args.watchdog_timeout_seconds)
    try:
        watchdog.start()
        service.start()
        while datetime.now(timezone.utc) < session_close:
            sampled_at = datetime.now(timezone.utc)
            elapsed = (sampled_at - evidence.started_at).total_seconds()
            runner.sample(sampled_at)
            watchdog.progress()
            frame_ready = all(
                evidence.frame_counts_by_tr_id.get(tr_id, 0) > 0
                for tr_id in ("HDFSCNT0", "HDFSASP0")
            )
            critical_ready = set(evidence.requested_subscriptions) <= set(
                evidence.acked_subscriptions
            )
            if runner.current_feed_ready and frame_ready and not stop_probe_complete:
                evidence.synthetic_stop_tests = _synthetic_stop_test(service)
                stop_probe_complete = True
            if runner.current_feed_ready and frame_ready and not duplicate_probe_complete:
                before = len(evidence.protocol_operations)
                service.subscribe(symbols)
                service.subscribe(symbols)
                service.unsubscribe(["GATE2-NOT-SUBSCRIBED"])
                service.unsubscribe(["GATE2-NOT-SUBSCRIBED"])
                evidence.duplicate_request_probe = {
                    "requests_attempted": 4,
                    "unexpected_protocol_operations": (
                        len(evidence.protocol_operations) - before
                    ),
                }
                duplicate_probe_complete = True
            if (
                not stale_probe_started
                and elapsed >= float(args.silent_stale_probe_after_seconds)
                and runner.current_feed_ready
                and frame_ready
            ):
                runner.start_silent_stale_probe(
                    symbol=symbols[0], channel=FeedChannel.TRADE, now=sampled_at
                )
                stale_probe_started = bool(evidence.silent_stale_probe)
            if (
                next_reconnect < len(reconnect_offsets)
                and elapsed >= reconnect_offsets[next_reconnect]
                and service.is_connected()
                and runner.current_feed_ready
                and runner._silent_probe_phase in {"", "COMPLETE"}
            ):
                runner.inject_disconnect()
                next_reconnect += 1
            wall_time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        evidence.operator_abort_reasons.append("operator interrupted soak")
    finally:
        evidence.ended_at = datetime.now(timezone.utc)
        runner.sample(evidence.ended_at)
        watchdog.progress()
        runner.finalize()
        watchdog.stop()
        service.stop()
        safety_snapshot = safety_audit.close()
        evidence.safety_audit_initialized = safety_snapshot.initialized
        evidence.safety_audit_sources = list(safety_snapshot.registered_sources)
        evidence.broker_mutation_count = (
            safety_snapshot.broker_mutation_attempt_count
        )
        evidence.entry_readiness_check_count = (
            safety_snapshot.entry_readiness_check_count
        )
        evidence.stale_entry_readiness_check_count = (
            safety_snapshot.stale_entry_readiness_check_count
        )
        evidence.stale_entry_readiness_rejection_count = (
            safety_snapshot.stale_entry_readiness_rejection_count
        )
        evidence.stale_entry_readiness_allow_count = (
            safety_snapshot.stale_entry_readiness_allow_count
        )
        root_logger.removeHandler(log_handler)
        root_logger.setLevel(previous_root_level)
        log_handler.flush()
        log_handler.close()

    # The capture contains every application/transport log emitted during the
    # soak. Sensitive values are held only in memory and never serialized.
    draft = json.dumps(asdict(evidence), sort_keys=True, default=str)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    with sensitive_lock:
        values_to_scan = set(sensitive_values)
        evidence.approval_key_count = len(approval_values)
    evidence.secret_leak_count = sum(
        value in draft or value in log_text for value in values_to_scan
    )
    evidence.sensitive_value_count = len(values_to_scan)
    evidence.log_bytes_scanned = log_path.stat().st_size
    evidence.log_capture_sha256 = _sha256(log_path)
    evidence.log_scan_completed = True
    evidence.log_redaction_count = redacting_formatter.redaction_count
    report = build_report(evidence)
    _write_report(args.output, report)
    print(f"Gate 2 {report['result']}: report={args.output}")
    return 0 if report["result"] == "PASSED" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the live, read-only KIS Gate-2 soak")
    parser.add_argument("--confirm-read-only", action="store_true")
    parser.add_argument("--environment", choices=("PROD", "SIM"), default="PROD")
    parser.add_argument("--symbols", required=True, help="comma-separated critical symbols")
    parser.add_argument("--session-date", required=True, help="NYSE session date YYYY-MM-DD")
    parser.add_argument("--gate1-report", type=Path, required=True)
    parser.add_argument("--capability-manifest", type=Path, required=True)
    parser.add_argument(
        "--redacted-evidence", type=Path, action="append", required=True
    )
    parser.add_argument("--reconnect-after-seconds", type=float, action="append", required=True)
    parser.add_argument("--silent-stale-probe-after-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument("--watchdog-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--log-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/gate2_report.json"))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    for name in ("gate1_report", "capability_manifest", "log_output", "output"):
        value = getattr(args, name)
        if not value.is_absolute():
            setattr(args, name, root / value)
    args.redacted_evidence = [path if path.is_absolute() else root / path for path in args.redacted_evidence]
    return run_live_soak(args, root)


if __name__ == "__main__":
    raise SystemExit(main())
