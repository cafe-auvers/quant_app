from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from activation_gates.evidence import canonical_report_sha256
from activation_gates.journal import AppendOnlyEvidenceJournal
from gate3.collector import GATE3_EVIDENCE_EVENTS, GATE3_NAME, Gate3EvidenceCollector
from gate3.reporting import build_report as build_gate3_report
from gate3.runner import ProductionShadowDecisionRuntime, run_captured_live_replay
from gate4.capabilities import (
    REQUIRED_EXECUTION_CAPABILITIES,
    load_verified_execution_capabilities,
)
from gate4.collector import Gate4EvidenceCollector
from gate4.reporting import build_report as build_gate4_report
from gate4.runtime_observer import (
    configured_collector,
    observe_gate4_event,
    reset_runtime_observer_for_tests,
)
from src.services.realtime_market_data import QuoteSnapshot, RealtimeMarketDataService


COMMIT = "a" * 40


def _review():
    return {
        "status": "APPROVED",
        "author": "operator-a",
        "reviewer": "reviewer-b",
        "reviewed_at": "2026-08-28T20:00:00+00:00",
        "reference": f"sha256:{'9' * 64}",
    }


def test_append_only_evidence_journal_detects_tampering(tmp_path):
    path = tmp_path / "gate3.evidence.jsonl"
    journal = AppendOnlyEvidenceJournal(
        path,
        gate=GATE3_NAME,
        commit_sha=COMMIT,
        allowed_event_types=GATE3_EVIDENCE_EVENTS,
    )
    journal.append("SESSION_STARTED", {"session_date": "2026-08-24"})
    journal.append("SESSION_ENDED", {"session_date": "2026-08-24"})

    assert journal.audit().passed is True
    rows = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    payload["payload"]["session_date"] = "2026-08-25"
    rows[0] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    audit = journal.audit()
    assert audit.passed is False
    assert audit.hash_chain_error_count >= 1
    with pytest.raises(RuntimeError, match="corrupt"):
        journal.append("SESSION_ENDED", {"session_date": "2026-08-25"})


def test_gate3_collector_builds_passing_evidence_from_journals(tmp_path):
    collector = Gate3EvidenceCollector(
        journal_path=tmp_path / "gate3.evidence.jsonl",
        shadow_store_path=tmp_path / "gate3.shadow.jsonl",
        commit_sha=COMMIT,
    )
    collector.record(
        "SESSION_STARTED",
        session_date="2026-08-24",
        session_open="2026-08-24T13:30:00+00:00",
        session_close="2026-08-24T20:00:00+00:00",
        started_at="2026-08-24T13:20:00+00:00",
    )
    collector.record(
        "PRODUCTION_RUNTIME_STARTED",
        runtime_class="src.services.trading_engine.TradingEngine",
        composition_function="src.services.buyboard_runtime.build_buyboard_runtime",
        production_runtime_source_sha256="1" * 64,
        shadow_gateway_at_final_boundary=True,
        shadow_store_isolated=True,
        production_card_count=1,
    )
    collector.record(
        "PRODUCTION_LEDGER_SNAPSHOT", phase="BEFORE", digests={"mysql:orders": "2" * 64}
    )
    collector.record(
        "REAL_QUOTE_EVALUATED",
        symbol="AAPL",
        last_price=100.0,
        bid=99.9,
        ask=100.1,
        source="KIS_WEBSOCKET",
        channel="HDFSASP0",
        real_quote=True,
        regular_session=True,
    )
    run_captured_live_replay(collector)
    collector.record(
        "PRODUCTION_LEDGER_SNAPSHOT", phase="AFTER", digests={"mysql:orders": "2" * 64}
    )
    collector.record(
        "SESSION_ENDED",
        session_date="2026-08-24",
        ended_at="2026-08-24T20:01:00+00:00",
        counters={
            "broker_mutation_attempt_count": 0,
            "fake_broker_ack_count": 0,
            "fake_fill_count": 0,
            "production_ledger_write_count": 0,
            "runtime_error_count": 0,
        },
    )
    rules = tmp_path / "rules.md"
    rules.write_text("frozen rules", encoding="utf-8")
    gate2 = {
        "gate": "GATE_2_LIVE_KIS_READ_ONLY_SOAK",
        "result": "PASSED",
        "commit_sha": COMMIT,
    }

    evidence = collector.build_evidence(
        gate2_report_sha256=canonical_report_sha256(gate2),
        strategy_rules_path=rules,
        review=_review(),
    )
    report = build_gate3_report(evidence, upstream_gate2_report=gate2)

    assert report["result"] == "PASSED"
    assert evidence["collector_derived"] is True
    assert evidence["captured_live_replay_complete"] is True
    assert set(evidence["would_event_counts"]) == {
        "WOULD_SUBMIT",
        "WOULD_CANCEL",
        "WOULD_REPLACE",
        "WOULD_SELL",
    }


def test_gate4_collector_derives_three_session_lifecycle(tmp_path):
    collector = Gate4EvidenceCollector(
        journal_path=tmp_path / "gate4.evidence.jsonl", commit_sha=COMMIT
    )
    collector.record(
        "CAPABILITY_EVIDENCE_VERIFIED",
        verified=True,
        evidence_sha256="1" * 64,
    )
    for index, session_date in enumerate(
        ("2026-08-24", "2026-08-25", "2026-08-26")
    ):
        collector.record(
            "SESSION_STARTED", session_date=session_date, supervised=True, armed=False
        )
        collector.record(
            "RUNTIME_ACTIVE",
            session_date=session_date,
            owner_count=1,
            lease_count=1,
            live_execution_mode="CONTROLLED_LIVE",
            reviewed_entry_notional_cap=1_000.0,
            approved_symbols=["AAPL"],
        )
        collector.record("MANUAL_ARM", session_date=session_date, source="MANUAL_UI")
        collector.record("LIFECYCLE_COMPARISON", session_date=session_date, agrees=True)
        if index == 0:
            collector.record(
                "ENTRY_CANDIDATE",
                session_date=session_date,
                symbol="AAPL",
                notional=500.0,
                active_trade_card=True,
                risk_rechecked_atomically=True,
            )
            collector.record(
                "MUTATION_DISPATCHED",
                session_date=session_date,
                automatic_retry=False,
                duplicate=False,
                owned=True,
                command_type="SUBMIT",
                strategy_entry=True,
                client_order_id="entry-1",
            )
            collector.record(
                "MUTATION_TERMINAL",
                session_date=session_date,
                command_type="SUBMIT",
                strategy_entry=True,
                broker_confirmed_terminal=True,
                identity_ambiguous=False,
                resolved=True,
                client_order_id="entry-1",
                symbol="AAPL",
            )
            collector.record(
                "MUTATION_DISPATCHED",
                session_date=session_date,
                automatic_retry=False,
                duplicate=False,
                owned=True,
                command_type="CANCEL",
                strategy_entry=False,
                client_order_id="entry-1",
            )
            collector.record(
                "MUTATION_TERMINAL",
                session_date=session_date,
                command_type="CANCEL",
                strategy_entry=False,
                broker_confirmed_terminal=True,
                identity_ambiguous=False,
                resolved=True,
                client_order_id="entry-1",
            )
            collector.record(
                "POSITION_PROTECTED",
                session_date=session_date,
                symbol="AAPL",
                safe_exit_or_protected=True,
            )
            collector.record(
                "DISARMED", session_date=session_date, source="MANUAL_UI"
            )
            collector.record(
                "DISARM_PROBE",
                session_date=session_date,
                next_mutation_blocked=True,
                broker_called=False,
            )
            collector.record(
                "EXTERNAL_ALERT_DELIVERED",
                session_date=session_date,
                delivered=True,
            )
        collector.record(
            "FINAL_RECONCILIATION", session_date=session_date, matches_broker=True
        )
        collector.record(
            "SESSION_ENDED",
            session_date=session_date,
            supervised=True,
            clean_shutdown=True,
        )
    gate3 = {
        "gate": "GATE_3_SHADOW_EXECUTION",
        "result": "PASSED",
        "commit_sha": COMMIT,
    }
    collector.record(
        "ENTRY_CANDIDATE",
        session_date="2026-08-24",
        symbol="AAPL",
        notional=999.0,
        active_trade_card=False,
        risk_rechecked_atomically=False,
    )

    evidence = collector.build_evidence(
        gate3_report_sha256=canonical_report_sha256(gate3),
        controlled_live_config_sha256="2" * 64,
        risk_limits_sha256="3" * 64,
        review=_review(),
    )
    report = build_gate4_report(evidence, upstream_gate3_report=gate3)

    assert report["result"] == "PASSED"
    assert evidence["supervised_regular_session_dates"] == [
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
    ]
    assert evidence["max_observed_entry_notional"] == 500.0


def test_gate4_runtime_observer_requires_explicit_open_session(tmp_path, monkeypatch):
    journal = tmp_path / "gate4.evidence.jsonl"
    monkeypatch.setenv("GATE4_QUALIFICATION_ENABLED", "true")
    monkeypatch.setenv("GATE4_EVIDENCE_JOURNAL_PATH", str(journal))
    monkeypatch.setenv("KIS_RUNTIME_COMMIT_SHA", COMMIT)
    reset_runtime_observer_for_tests()
    try:
        with pytest.raises(RuntimeError, match="start one supervised"):
            observe_gate4_event(
                "RUNTIME_ACTIVE",
                owner_count=1,
                lease_count=1,
                live_execution_mode="CONTROLLED_LIVE",
            )
        collector = configured_collector()
        assert collector is not None
        from src.core.exit_policy import market_session_date

        collector.record(
            "SESSION_STARTED",
            session_date=market_session_date().isoformat(),
            supervised=True,
            armed=False,
        )
        observe_gate4_event(
            "RUNTIME_ACTIVE",
            owner_count=1,
            lease_count=1,
            live_execution_mode="CONTROLLED_LIVE",
        )
    finally:
        reset_runtime_observer_for_tests()


def test_gate4_execution_capability_manifest_is_exact_and_complete(tmp_path):
    path = tmp_path / "gate4_capabilities.json"
    payload = {
        "commit_sha": COMMIT,
        "environment": "PROD",
        "account_ref": "1" * 64,
        "review": _review(),
        "capabilities": [
            {
                "capability_id": capability_id,
                "status": "VERIFIED",
                "evidence_sha256": hashlib.sha256(capability_id.encode()).hexdigest(),
                "finding": "credentialed controlled-simulation observation",
            }
            for capability_id in sorted(REQUIRED_EXECUTION_CAPABILITIES)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    verified = load_verified_execution_capabilities(path, expected_commit=COMMIT)

    assert verified.capability_ids == tuple(sorted(REQUIRED_EXECUTION_CAPABILITIES))
    payload["capabilities"] = payload["capabilities"][:-1]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing Gate-4 capabilities"):
        load_verified_execution_capabilities(path, expected_commit=COMMIT)


class _MarketData(RealtimeMarketDataService):
    def latest_quote(self, _symbol):
        return None

    def is_connected(self):
        return True

    def is_symbol_trading_halted(self, _symbol):
        return False


def test_production_shadow_runtime_composes_while_normal_engine_flag_is_off(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BUYBOARD_ENGINE_ENABLED", "false")
    collector = Gate3EvidenceCollector(
        journal_path=tmp_path / "gate3.evidence.jsonl",
        shadow_store_path=tmp_path / "gate3.shadow.jsonl",
        commit_sha=COMMIT,
    )

    runtime = ProductionShadowDecisionRuntime(
        collector=collector,
        market_data=_MarketData(),
        cards=[],
        isolated_database_path=tmp_path / "isolated.sqlite3",
        account_equity=10_000,
    )
    try:
        assert runtime.runtime.trading_engine.is_enabled() is True
        assert runtime.gateway.qualification_shadow_only is True
    finally:
        runtime.close()
