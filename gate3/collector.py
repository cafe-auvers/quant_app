"""Gate-3 evidence derived from runtime and shadow journals.

This is deliberately the only supported path for producing Gate-3 evidence.
It turns durable observations into the validator input; it never accepts a
caller-supplied collection of pass/fail booleans.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from activation_gates.journal import AppendOnlyEvidenceJournal
from gate3.decision_oracle import oracle_source_sha256
from gate3.reporting import REQUIRED_DECISION_BRANCHES, REQUIRED_FENCES
from gate3.shadow_boundary import SHADOW_EVENT_TYPES, ShadowEventStore


GATE3_NAME = "GATE_3_SHADOW_EXECUTION"
GATE3_EVIDENCE_EVENTS = frozenset(
    {
        "SESSION_STARTED",
        "SESSION_ENDED",
        "PRODUCTION_RUNTIME_STARTED",
        "REAL_QUOTE_EVALUATED",
        "DECISION_BRANCH_OBSERVED",
        "ORACLE_COMPARISON",
        "SAFETY_FENCE_PROBE",
        "PRODUCTION_LEDGER_SNAPSHOT",
        "CAPTURED_LIVE_REPLAY_STARTED",
        "CAPTURED_LIVE_REPLAY_ENDED",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _last_payload(events, event_type: str) -> Mapping[str, Any]:
    for event in reversed(events):
        if event.event_type == event_type:
            return event.payload
    return {}


class Gate3EvidenceCollector:
    """Record and reduce one exact-release Gate-3 qualification campaign."""

    def __init__(
        self,
        *,
        journal_path: Path,
        shadow_store_path: Path,
        commit_sha: str,
        production_paths: tuple[Path, ...] = (),
    ) -> None:
        self.journal = AppendOnlyEvidenceJournal(
            journal_path,
            gate=GATE3_NAME,
            commit_sha=commit_sha,
            allowed_event_types=GATE3_EVIDENCE_EVENTS,
        )
        self.shadow_store = ShadowEventStore(
            shadow_store_path,
            production_paths=production_paths,
        )
        self.commit_sha = self.journal.commit_sha

    def record(self, event_type: str, **payload: Any) -> None:
        self.journal.append(event_type, payload)

    def build_evidence(
        self,
        *,
        gate2_report_sha256: str,
        strategy_rules_path: Path,
        review: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        journal_audit = self.journal.audit()
        shadow_audit = self.shadow_store.audit()
        all_events = self.journal.read_all() if journal_audit.passed else []
        starts = [
            (index, event)
            for index, event in enumerate(all_events)
            if event.event_type == "SESSION_STARTED"
        ]
        ends = [
            (index, event)
            for index, event in enumerate(all_events)
            if event.event_type == "SESSION_ENDED"
        ]
        events = []
        if len(starts) == 1 and len(ends) == 1 and ends[0][0] > starts[0][0]:
            events = all_events[starts[0][0] : ends[0][0] + 1]
        started = _last_payload(events, "SESSION_STARTED")
        ended = _last_payload(events, "SESSION_ENDED")
        runtime = _last_payload(events, "PRODUCTION_RUNTIME_STARTED")
        replay_start = _last_payload(events, "CAPTURED_LIVE_REPLAY_STARTED")
        replay_end = _last_payload(events, "CAPTURED_LIVE_REPLAY_ENDED")

        session_open = _parse_time(started.get("session_open"))
        session_close = _parse_time(started.get("session_close"))
        started_at = _parse_time(started.get("started_at"))
        ended_at = _parse_time(ended.get("ended_at"))
        complete_session = bool(
            session_open
            and session_close
            and started_at
            and ended_at
            and started_at <= session_open
            and ended_at >= session_close
            and str(started.get("session_date") or "")
            == str(ended.get("session_date") or "")
        )

        real_quotes = [
            event
            for event in events
            if event.event_type == "REAL_QUOTE_EVALUATED"
            and event.payload.get("real_quote") is True
            and event.payload.get("regular_session") is True
            and str(event.payload.get("source") or "").upper().startswith("KIS")
        ]
        observed_branches = sorted(
            {
                str(event.payload.get("branch") or "")
                for event in events
                if event.event_type == "DECISION_BRANCH_OBSERVED"
                and str(event.payload.get("branch") or "") in REQUIRED_DECISION_BRANCHES
            }
        )
        comparisons = [
            event.payload
            for event in events
            if event.event_type == "ORACLE_COMPARISON"
        ]
        unresolved = sum(item.get("matches") is not True for item in comparisons)
        fence_results = {
            fence: (
                "PASSED"
                if any(
                    event.event_type == "SAFETY_FENCE_PROBE"
                    and event.payload.get("fence") == fence
                    and event.payload.get("mutation_blocked") is True
                    for event in events
                )
                else "FAILED"
            )
            for fence in sorted(REQUIRED_FENCES)
        }
        ledger_before = next(
            (
                event.payload.get("digests")
                for event in events
                if event.event_type == "PRODUCTION_LEDGER_SNAPSHOT"
                and event.payload.get("phase") == "BEFORE"
            ),
            None,
        )
        ledger_after = next(
            (
                event.payload.get("digests")
                for event in reversed(events)
                if event.event_type == "PRODUCTION_LEDGER_SNAPSHOT"
                and event.payload.get("phase") == "AFTER"
            ),
            None,
        )
        ledgers_unchanged = bool(
            isinstance(ledger_before, Mapping)
            and ledger_before
            and dict(ledger_before) == dict(ledger_after or {})
        )
        replay_complete = bool(
            replay_start
            and replay_end
            and replay_end.get("completed") is True
            and int(replay_end.get("captured_quote_count") or 0) > 0
        )
        runtime_valid = bool(
            runtime.get("runtime_class")
            == "src.services.trading_engine.TradingEngine"
            and runtime.get("composition_function")
            == "src.services.buyboard_runtime.build_buyboard_runtime"
            and runtime.get("shadow_gateway_at_final_boundary") is True
            and runtime.get("production_runtime_source_sha256")
            and int(runtime.get("production_card_count") or 0) > 0
        )
        ended_counters = ended.get("counters") if isinstance(ended.get("counters"), Mapping) else {}

        return {
            "evidence_schema_version": 2,
            "collector_derived": True,
            "commit_sha": self.commit_sha,
            "gate2_report_sha256": str(gate2_report_sha256 or "").lower(),
            "strategy_rules_sha256": _sha256(strategy_rules_path),
            "decision_oracle_sha256": oracle_source_sha256(),
            "shadow_store_sha256": shadow_audit.sha256,
            "evidence_journal_sha256": journal_audit.sha256,
            "production_runtime_source_sha256": runtime.get(
                "production_runtime_source_sha256", ""
            ),
            "final_production_decision_runtime_used": runtime_valid,
            "real_quotes_used": bool(real_quotes),
            "final_boundary_interception_enabled": runtime.get(
                "shadow_gateway_at_final_boundary"
            )
            is True,
            "mutation_audit_initialized": bool(
                journal_audit.passed and shadow_audit.passed
            ),
            "shadow_store_isolated": runtime.get("shadow_store_isolated") is True,
            "shadow_store_visibly_labelled": shadow_audit.label_mismatch_count == 0,
            "shadow_store_append_only_verified": shadow_audit.passed,
            "production_ledgers_unchanged": ledgers_unchanged,
            "complete_regular_session_count": int(complete_session),
            "captured_live_replay_complete": replay_complete,
            "real_quote_evaluation_count": len(real_quotes),
            "mutation_candidate_count": shadow_audit.event_count,
            "would_event_counts": dict(shadow_audit.event_counts),
            "broker_mutation_attempt_count": int(
                ended_counters.get("broker_mutation_attempt_count") or 0
            ),
            "fake_broker_ack_count": int(ended_counters.get("fake_broker_ack_count") or 0),
            "fake_fill_count": int(ended_counters.get("fake_fill_count") or 0),
            "production_ledger_write_count": int(
                ended_counters.get("production_ledger_write_count") or 0
            ),
            "runtime_error_count": int(ended_counters.get("runtime_error_count") or 0),
            "unresolved_oracle_difference_count": unresolved,
            "shadow_event_parse_error_count": shadow_audit.parse_error_count,
            "shadow_event_label_mismatch_count": shadow_audit.label_mismatch_count,
            "shadow_duplicate_event_id_count": shadow_audit.duplicate_event_id_count,
            "evidence_journal_parse_error_count": journal_audit.parse_error_count,
            "evidence_journal_duplicate_event_id_count": journal_audit.duplicate_event_id_count,
            "evidence_journal_hash_chain_error_count": journal_audit.hash_chain_error_count,
            "evidence_journal_identity_mismatch_count": journal_audit.identity_mismatch_count,
            "evidence_journal_unknown_event_type_count": journal_audit.unknown_event_type_count,
            "observed_decision_branches": observed_branches,
            "fence_results": fence_results,
            "session_date": started.get("session_date"),
            "review": dict(review or {}),
        }


__all__ = ["GATE3_EVIDENCE_EVENTS", "GATE3_NAME", "Gate3EvidenceCollector"]
