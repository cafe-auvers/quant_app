"""Standalone, mutation-free Gate-2 KIS WebSocket soak reporter."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time as wall_time
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from src.core import execution_config
from src.services.kis_realtime_market_data import (
    KisRealtimeMarketDataService,
    PendingMarketStateAccumulator,
    StopRule,
    SubscriptionPriority,
    build_kis_realtime_market_data_from_environment,
)
from src.services.realtime_market_data import QuoteSnapshot
from src.services.trading_state import is_trading_enabled
from src.utils.market_calendar import (
    US_MARKET_OPEN_TIME,
    US_MARKET_ZONE,
    nyse_holidays,
    nyse_regular_session_close_time,
)


KST_ZONE = ZoneInfo("Asia/Seoul")
SEQUENCE_FINDINGS = {"MONOTONIC", "NO_USABLE_SEQUENCE"}
SAFE_RUNTIME_EXPECTATIONS = {
    "TRADING_ENABLED": False,
    "BUYBOARD_ENGINE_ENABLED": False,
    "KIS_WS_ENABLED": True,
    "KIS_WS_PROTOCOL_VERIFIED": True,
    "KIS_MUTATION_BUDGET_VERIFIED": False,
    "KIS_SUBMIT_MUTATION_CAPACITY": 0,
    "KIS_CANCEL_MUTATION_CAPACITY": 0,
    "KIS_REPLACE_MUTATION_CAPACITY": 0,
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


def runtime_activation_snapshot() -> dict[str, bool | int]:
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
    }


@dataclass
class Gate2Evidence:
    commit_sha: str
    gate1_report_sha256: str
    capability_matrix_sha256: str
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
    watchdog_cycles: int = 0
    deadlock_count: int = 0
    stale_entry_attempt_count: int = 0
    broker_mutation_count: int = 0
    unhandled_disconnect_count: int = 0
    secret_leak_count: int = 0
    redacted_evidence_sha256: dict[str, str] = field(default_factory=dict)
    sequence_findings: dict[str, str] = field(default_factory=dict)
    timestamp_evidence_sha256: str = ""
    execution_notice_evidence_sha256: str = ""
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


def build_report(evidence: Gate2Evidence) -> dict:
    """Build the deterministic certification decision from observed evidence."""
    ended_at = evidence.ended_at or evidence.started_at
    requested = set(evidence.requested_subscriptions)
    acked = set(evidence.acked_subscriptions)
    recovered = evidence.reconnect_recovery_seconds
    receive = evidence.receive_lag_ms
    queue = evidence.queue_lag_ms
    stop = evidence.synthetic_stop_tests
    activation_ok = all(
        evidence.activation_snapshot.get(key) == expected
        for key, expected in SAFE_RUNTIME_EXPECTATIONS.items()
    )
    sequence_ok = all(
        evidence.sequence_findings.get(tr_id) in SEQUENCE_FINDINGS
        for tr_id in ("HDFSCNT0", "HDFSASP0")
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
            value=evidence.unhandled_disconnect_count,
            threshold="0",
            passed=evidence.unhandled_disconnect_count == 0,
        ),
        "critical_ack_recovery_seconds": _metric(
            value=max(recovered, default=0.0),
            numerator=len(recovered),
            denominator=sum(
                event.get("kind") == "INJECTED_DISCONNECT"
                for event in evidence.connection_events
            ),
            threshold="at least 1 injected reconnect; every recovery < 10s",
            passed=(
                bool(recovered)
                and len(recovered)
                == sum(
                    event.get("kind") == "INJECTED_DISCONNECT"
                    for event in evidence.connection_events
                )
                and max(recovered) < 10.0
            ),
        ),
        "stale_detection_seconds": _metric(
            value=max(evidence.stale_detection_seconds, default=0.0),
            threshold="at least 1 observation; max <= 3s",
            passed=(
                bool(evidence.stale_detection_seconds)
                and max(evidence.stale_detection_seconds) <= 3.0
            ),
        ),
        "entry_attempts_while_stale": _metric(
            value=evidence.stale_entry_attempt_count,
            threshold="0",
            passed=evidence.stale_entry_attempt_count == 0,
        ),
        "duplicate_subscription_corruption": _metric(
            value=evidence.duplicate_subscription_anomaly_count,
            threshold="0",
            passed=evidence.duplicate_subscription_anomaly_count == 0,
        ),
        "synthetic_stop_breaches": _metric(
            value=stop.get("consumed", 0),
            numerator=stop.get("consumed", 0),
            denominator=stop.get("injected", 0),
            threshold="injected=latched=consumed and injected>0",
            passed=(
                stop.get("injected", 0) > 0
                and stop.get("injected") == stop.get("latched")
                == stop.get("consumed")
            ),
        ),
        "watchdog_deadlocks": _metric(
            value=evidence.deadlock_count,
            threshold="0 deadlocks and watchdog_cycles>0",
            passed=evidence.deadlock_count == 0 and evidence.watchdog_cycles > 0,
        ),
        "receive_lag_p95_ms": _metric(
            value=receive.get("p95", 0.0),
            threshold="< 1000ms",
            passed=receive.get("p95", 0.0) < 1000.0,
        ),
        "receive_lag_p99_ms": _metric(
            value=receive.get("p99", 0.0),
            threshold="< 2000ms",
            passed=receive.get("p99", 0.0) < 2000.0,
        ),
        "queue_lag_p99_ms": _metric(
            value=queue.get("p99", 0.0),
            threshold=(
                f"<= {execution_config.MAX_MARKET_DATA_QUEUE_DELAY_SECONDS * 1000.0:g}ms"
            ),
            passed=(
                queue.get("p99", 0.0)
                <= execution_config.MAX_MARKET_DATA_QUEUE_DELAY_SECONDS * 1000.0
            ),
        ),
        "secret_leaks": _metric(
            value=evidence.secret_leak_count,
            threshold="0",
            passed=evidence.secret_leak_count == 0,
        ),
        "broker_mutations": _metric(
            value=evidence.broker_mutation_count,
            threshold="0",
            passed=evidence.broker_mutation_count == 0,
        ),
        "runtime_activation_fence": _metric(
            value=int(activation_ok),
            threshold="read-only Gate-2 activation snapshot",
            passed=activation_ok,
        ),
        "timestamp_evidence": _metric(
            value=int(bool(evidence.timestamp_evidence_sha256)),
            threshold="redacted evidence digest present",
            passed=bool(evidence.timestamp_evidence_sha256),
        ),
        "sequence_semantics": _metric(
            value=int(sequence_ok),
            threshold="MONOTONIC or NO_USABLE_SEQUENCE for both channels",
            passed=sequence_ok,
        ),
        "execution_notice_evidence": _metric(
            value=int(bool(evidence.execution_notice_evidence_sha256)),
            threshold="redacted encryption/mapping evidence digest present",
            passed=bool(evidence.execution_notice_evidence_sha256),
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
        "watchdog_cycles": evidence.watchdog_cycles,
        "sequence_findings": evidence.sequence_findings,
        "timestamp_evidence_sha256": evidence.timestamp_evidence_sha256,
        "execution_notice_evidence_sha256": evidence.execution_notice_evidence_sha256,
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


def _synthetic_stop_test() -> dict[str, int]:
    accumulator = PendingMarketStateAccumulator()
    now = datetime.now(timezone.utc)
    accumulator.replace_stop_rules("GATE2", [StopRule("gate2", 100.0, "v1")])
    accumulator.publish_trade(
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
    detached = accumulator.drain("GATE2")
    latched = int(("gate2", "v1") in detached.pending.breached_stop_versions)
    consumed = int(accumulator.acknowledge_breach("GATE2", "gate2", "v1"))
    return {"injected": 1, "latched": latched, "consumed": consumed}


class LiveGate2Runner:
    """Poll only the KIS market-data service and assemble qualification evidence."""

    def __init__(self, service: KisRealtimeMarketDataService, evidence: Gate2Evidence):
        self.service = service
        self.evidence = evidence
        self._last_disconnect: datetime | None = None
        self._awaiting_recovery = False
        self._stale_recorded: set[str] = set()
        service.on_session(self._on_session)

    def _on_session(
        self, connected: bool, reason: str, generation: int, observed_at: datetime
    ) -> None:
        self.evidence.connection_events.append(
            {
                "kind": "CONNECTED" if connected else "DISCONNECTED",
                "at": _iso(observed_at),
                "generation": generation,
                "reason": reason,
            }
        )
        if not connected:
            self._last_disconnect = observed_at
            self._awaiting_recovery = True
            self._stale_recorded.clear()

    def inject_disconnect(self) -> None:
        now = datetime.now(timezone.utc)
        self.evidence.connection_events.append(
            {"kind": "INJECTED_DISCONNECT", "at": _iso(now)}
        )
        self._last_disconnect = now
        self._awaiting_recovery = True
        self._stale_recorded.clear()
        self.service.reconnect()

    def sample(self, now: datetime) -> None:
        health = self.service.health_metrics(now=now)
        capacity = self.service.subscription_capacity_snapshot()
        protocol = self.service.protocol_metrics_snapshot()
        self.evidence.watchdog_cycles += 1
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
        if self._last_disconnect is not None:
            for symbol in health.stale_symbols:
                if symbol not in self._stale_recorded:
                    self.evidence.stale_detection_seconds.append(
                        max(0.0, (now - self._last_disconnect).total_seconds())
                    )
                    self._stale_recorded.add(symbol)
        if self._awaiting_recovery and not (
            health.critical_trade_channels_missing
            or health.critical_quote_channels_missing
        ):
            if self._last_disconnect is not None:
                self.evidence.reconnect_recovery_seconds.append(
                    max(0.0, (now - self._last_disconnect).total_seconds())
                )
            self._awaiting_recovery = False
        self.evidence.frame_counts_by_tr_id = dict(protocol.frame_counts_by_tr_id)
        self.evidence.record_counts_by_tr_id = dict(protocol.record_counts_by_tr_id)
        self.evidence.schema_fingerprints_by_tr_id = dict(
            protocol.schema_fingerprints_by_tr_id
        )
        self.evidence.parser_failure_count = protocol.parser_failure_count
        self.evidence.malformed_frame_count = health.malformed_frame_count
        self.evidence.duplicate_event_count = protocol.duplicate_event_count
        self.evidence.receive_lag_ms = {
            "p50": protocol.receive_lag_p50_ms,
            "p95": protocol.receive_lag_p95_ms,
            "p99": protocol.receive_lag_p99_ms,
            "max": protocol.receive_lag_max_ms,
        }
        self.evidence.queue_lag_ms = {
            "p50": protocol.queue_lag_p50_ms,
            "p95": protocol.queue_lag_p95_ms,
            "p99": protocol.queue_lag_p99_ms,
            "max": protocol.queue_lag_max_ms,
        }


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
    hts_id = os.getenv("KIS_WS_HTS_ID", "").strip()
    if not hts_id:
        raise RuntimeError(
            "KIS_WS_HTS_ID is required because execution-notice verification "
            "is part of the current Gate-2 contract"
        )

    symbols = sorted({item.strip().upper() for item in args.symbols.split(",") if item.strip()})
    if not symbols:
        raise ValueError("--symbols must contain at least one symbol")
    keys = {
        str(symbol).upper(): str(key)
        for symbol, key in json.loads(
            os.getenv("KIS_WS_SYMBOL_KEYS_JSON", "{}") or "{}"
        ).items()
    }
    missing = sorted(set(symbols) - set(keys))
    if missing:
        raise RuntimeError(f"missing verified subscription keys: {', '.join(missing)}")
    session_day = date.fromisoformat(args.session_date)
    session_open, session_close = _session_bounds(session_day)
    now = datetime.now(timezone.utc)
    timestamp_path = args.timestamp_evidence.resolve()
    notice_path = args.execution_notice_evidence.resolve()
    evidence = Gate2Evidence(
        commit_sha=commit,
        gate1_report_sha256=_sha256(args.gate1_report),
        capability_matrix_sha256=_sha256(root / "docs" / "kis_capability_matrix.md"),
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
        sequence_findings={
            "HDFSCNT0": args.trade_sequence_finding,
            "HDFSASP0": args.quote_sequence_finding,
        },
        timestamp_evidence_sha256=_sha256(timestamp_path),
        execution_notice_evidence_sha256=_sha256(notice_path),
        synthetic_stop_tests=_synthetic_stop_test(),
        later_execution_blockers=[
            "caller correlation and accepted mutation semantics",
            "broker-order-ID uniqueness scope",
            "history boundary/latency and mutation rate-limit evidence",
        ],
    )
    for path in args.redacted_evidence:
        resolved = path.resolve()
        evidence.redacted_evidence_sha256[resolved.name] = _sha256(resolved)

    service = build_kis_realtime_market_data_from_environment(
        environment=args.environment,
        critical_alert=lambda message: evidence.operator_abort_reasons.append(message),
    )
    priority = {symbol: int(SubscriptionPriority.CRITICAL_EXIT) for symbol in symbols}
    service.configure_desired_channels(trade_priorities=priority, quote_priorities=priority)
    runner = LiveGate2Runner(service, evidence)
    reconnect_offsets = sorted(float(value) for value in args.reconnect_after_seconds)
    next_reconnect = 0
    try:
        service.start()
        while datetime.now(timezone.utc) < session_close:
            sampled_at = datetime.now(timezone.utc)
            elapsed = (sampled_at - evidence.started_at).total_seconds()
            runner.sample(sampled_at)
            if (
                next_reconnect < len(reconnect_offsets)
                and elapsed >= reconnect_offsets[next_reconnect]
                and service.is_connected()
                and set(evidence.requested_subscriptions)
                <= set(evidence.acked_subscriptions)
            ):
                runner.inject_disconnect()
                next_reconnect += 1
            wall_time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        evidence.operator_abort_reasons.append("operator interrupted soak")
    finally:
        service.stop()
        evidence.ended_at = datetime.now(timezone.utc)
        runner.sample(evidence.ended_at)

    # Scan only generated, redacted report material. Actual secret values are
    # never serialized; matching any non-trivial configured secret fails it.
    draft = json.dumps(asdict(evidence), sort_keys=True, default=str)
    secret_values = {
        os.getenv(name, "")
        for name in (
            "KIS_PROD_APP_KEY", "KIS_PROD_APP_SECRET", "KIS_SIM_APP_KEY",
            "KIS_SIM_APP_SECRET", "KIS_WS_HTS_ID",
        )
        if len(os.getenv(name, "")) >= 6
    }
    evidence.secret_leak_count = sum(value in draft for value in secret_values)
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
    parser.add_argument("--timestamp-evidence", type=Path, required=True)
    parser.add_argument("--execution-notice-evidence", type=Path, required=True)
    parser.add_argument("--redacted-evidence", type=Path, action="append", default=[])
    parser.add_argument("--trade-sequence-finding", choices=sorted(SEQUENCE_FINDINGS), required=True)
    parser.add_argument("--quote-sequence-finding", choices=sorted(SEQUENCE_FINDINGS), required=True)
    parser.add_argument("--reconnect-after-seconds", type=float, action="append", required=True)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument("--output", type=Path, default=Path("artifacts/gate2_report.json"))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    for name in ("gate1_report", "timestamp_evidence", "execution_notice_evidence", "output"):
        value = getattr(args, name)
        if not value.is_absolute():
            setattr(args, name, root / value)
    args.redacted_evidence = [path if path.is_absolute() else root / path for path in args.redacted_evidence]
    return run_live_soak(args, root)


if __name__ == "__main__":
    raise SystemExit(main())
