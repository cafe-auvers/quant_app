"""Runtime-journal-derived Gate-4 controlled-live evidence."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from activation_gates.journal import AppendOnlyEvidenceJournal


GATE4_NAME = "GATE_4_CONTROLLED_LIVE"
GATE4_EVIDENCE_EVENTS = frozenset(
    {
        "SESSION_STARTED",
        "RUNTIME_ACTIVE",
        "MANUAL_ARM",
        "DISARMED",
        "ENTRY_CANDIDATE",
        "MUTATION_DISPATCHED",
        "MUTATION_TERMINAL",
        "LIFECYCLE_COMPARISON",
        "POSITION_PROTECTED",
        "DISARM_PROBE",
        "EXTERNAL_ALERT_DELIVERED",
        "FINAL_RECONCILIATION",
        "CAPABILITY_EVIDENCE_VERIFIED",
        "SESSION_ENDED",
    }
)


class Gate4EvidenceCollector:
    """Append observations and reduce them into the Gate-4 validator schema."""

    def __init__(self, *, journal_path: Path, commit_sha: str) -> None:
        self.journal = AppendOnlyEvidenceJournal(
            journal_path,
            gate=GATE4_NAME,
            commit_sha=commit_sha,
            allowed_event_types=GATE4_EVIDENCE_EVENTS,
        )
        self.commit_sha = self.journal.commit_sha

    def record(self, event_type: str, **payload: Any) -> None:
        self.journal.append(event_type, payload)

    def build_evidence(
        self,
        *,
        gate3_report_sha256: str,
        controlled_live_config_sha256: str,
        risk_limits_sha256: str,
        review: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        audit = self.journal.audit()
        events = self.journal.read_all() if audit.passed else []
        by_date: dict[str, list[tuple[int, Any]]] = defaultdict(list)
        for index, event in enumerate(events):
            session_date = str(event.payload.get("session_date") or "")
            if session_date:
                by_date[session_date].append((index, event))

        supervised_dates: list[str] = []
        started_disarmed = True
        manual_after_active = True
        final_reconciliation = True
        lifecycle_all_sessions = True
        entries_after_manual_arm = True
        active_observations = []
        qualifying_events = []
        for session_date in sorted(by_date):
            dated = by_date[session_date]
            starts = [item for item in dated if item[1].event_type == "SESSION_STARTED"]
            ends = [item for item in dated if item[1].event_type == "SESSION_ENDED"]
            if len(starts) != 1 or len(ends) != 1 or ends[0][0] <= starts[0][0]:
                continue
            if starts[0][1].payload.get("supervised") is not True:
                continue
            window = [
                item
                for item in dated
                if starts[0][0] <= item[0] <= ends[0][0]
            ]
            supervised_dates.append(session_date)
            qualifying_events.extend(item[1] for item in window)
            started_disarmed = bool(
                started_disarmed and starts[0][1].payload.get("armed") is False
            )
            active = [item for item in window if item[1].event_type == "RUNTIME_ACTIVE"]
            arms = [item for item in window if item[1].event_type == "MANUAL_ARM"]
            manual_after_active = bool(
                manual_after_active
                and active
                and arms
                and arms[0][0] > active[0][0]
                and arms[0][1].payload.get("source") == "MANUAL_UI"
            )
            active_observations.extend(item[1].payload for item in active)
            session_comparisons = [
                item
                for item in window
                if item[1].event_type == "LIFECYCLE_COMPARISON"
            ]
            lifecycle_all_sessions = bool(
                lifecycle_all_sessions
                and session_comparisons
                and all(
                    item[1].payload.get("agrees") is True
                    for item in session_comparisons
                )
            )
            session_entries = [
                item for item in window if item[1].event_type == "ENTRY_CANDIDATE"
            ]
            if session_entries:
                entries_after_manual_arm = bool(
                    entries_after_manual_arm
                    and arms
                    and all(item[0] > arms[0][0] for item in session_entries)
                )
            reconciliations = [
                item
                for item in window
                if item[1].event_type == "FINAL_RECONCILIATION"
            ]
            final_reconciliation = bool(
                final_reconciliation
                and reconciliations
                and reconciliations[-1][1].payload.get("matches_broker") is True
                and reconciliations[-1][0] < ends[0][0]
                and ends[0][1].payload.get("clean_shutdown") is True
            )

        entries = [
            event.payload
            for event in qualifying_events
            if event.event_type == "ENTRY_CANDIDATE"
        ]
        mutations = [
            event.payload
            for event in qualifying_events
            if event.event_type == "MUTATION_DISPATCHED"
        ]
        terminals = [
            event.payload
            for event in qualifying_events
            if event.event_type == "MUTATION_TERMINAL"
        ]
        comparisons = [
            event.payload
            for event in qualifying_events
            if event.event_type == "LIFECYCLE_COMPARISON"
        ]
        capability = next(
            (
                event.payload
                for event in reversed(events)
                if event.event_type == "CAPABILITY_EVIDENCE_VERIFIED"
            ),
            {},
        )
        approved_symbols = {
            str(symbol or "").strip().upper()
            for item in active_observations
            for symbol in item.get("approved_symbols", [])
            if str(symbol or "").strip()
        }
        observed_symbols = {
            str(item.get("symbol") or "").strip().upper()
            for item in entries
            if str(item.get("symbol") or "").strip()
        }
        caps = [float(item.get("reviewed_entry_notional_cap") or 0.0) for item in active_observations]
        notionals = [float(item.get("notional") or 0.0) for item in entries]
        dispatched_entry_ids = {
            str(item.get("client_order_id") or "")
            for item in mutations
            if item.get("command_type") == "SUBMIT"
            and item.get("strategy_entry") is True
            and str(item.get("client_order_id") or "")
        }
        dispatched_cancel_ids = {
            str(item.get("client_order_id") or "")
            for item in mutations
            if item.get("command_type") == "CANCEL"
            and str(item.get("client_order_id") or "")
        }
        dispatch_indexes = {
            (
                str(event.payload.get("command_type") or ""),
                str(event.payload.get("client_order_id") or ""),
            ): index
            for index, event in enumerate(qualifying_events)
            if event.event_type == "MUTATION_DISPATCHED"
            and str(event.payload.get("client_order_id") or "")
        }
        entry_terminal_events = [
            (index, event.payload)
            for index, event in enumerate(qualifying_events)
            if event.event_type == "MUTATION_TERMINAL"
            and event.payload.get("strategy_entry") is True
            and event.payload.get("broker_confirmed_terminal") is True
            and str(event.payload.get("client_order_id") or "")
            in dispatched_entry_ids
            and index
            > dispatch_indexes.get(
                (
                    "SUBMIT",
                    str(event.payload.get("client_order_id") or ""),
                ),
                index,
            )
        ]
        entry_terminals = [
            item for _index, item in entry_terminal_events
        ]
        cancellation_events = [
            (index, event.payload)
            for index, event in enumerate(qualifying_events)
            if event.event_type == "MUTATION_TERMINAL"
            and event.payload.get("command_type") == "CANCEL"
            and event.payload.get("broker_confirmed_terminal") is True
            and str(event.payload.get("client_order_id") or "")
            in dispatched_cancel_ids
            and index
            > dispatch_indexes.get(
                (
                    "CANCEL",
                    str(event.payload.get("client_order_id") or ""),
                ),
                index,
            )
        ]
        cancellations = [item for _index, item in cancellation_events]
        terminal_entry_symbols = {
            str(item.get("symbol") or "").strip().upper()
            for item in entry_terminals
            if str(item.get("symbol") or "").strip()
        }
        protected_positions = [
            event.payload
            for index, event in enumerate(qualifying_events)
            if event.event_type == "POSITION_PROTECTED"
            and event.payload.get("safe_exit_or_protected") is True
            and str(event.payload.get("symbol") or "").strip().upper()
            in terminal_entry_symbols
            and any(
                terminal_index < index
                and str(terminal.get("symbol") or "").strip().upper()
                == str(event.payload.get("symbol") or "").strip().upper()
                for terminal_index, terminal in entry_terminal_events
            )
        ]
        disarm_probes = [
            event.payload
            for index, event in enumerate(qualifying_events)
            if event.event_type == "DISARM_PROBE"
            and event.payload.get("next_mutation_blocked") is True
            and event.payload.get("broker_called") is False
            and any(
                previous.event_type == "DISARMED"
                and previous.payload.get("session_date")
                == event.payload.get("session_date")
                for previous in qualifying_events[:index]
            )
        ]
        alerts = [
            event.payload
            for event in qualifying_events
            if event.event_type == "EXTERNAL_ALERT_DELIVERED"
            and event.payload.get("delivered") is True
        ]
        lifecycle_mismatches = sum(
            item.get("agrees") is not True for item in comparisons
        )
        seen_mutations: set[tuple[str, str]] = set()
        repeated_mutations = 0
        for item in mutations:
            identity = (
                str(item.get("command_type") or ""),
                str(item.get("client_order_id") or ""),
            )
            if not identity[1] or identity in seen_mutations:
                repeated_mutations += 1
            seen_mutations.add(identity)
        automatic_retries = repeated_mutations + sum(
            item.get("automatic_retry") is True for item in mutations
        )
        duplicate_or_unowned = repeated_mutations + sum(
            item.get("duplicate") is True or item.get("owned") is not True
            for item in mutations
        )
        ambiguous = sum(
            item.get("identity_ambiguous") is True
            and item.get("resolved") is not True
            for item in terminals
        )
        all_active_single_owner = bool(
            supervised_dates
            and len(active_observations) >= len(supervised_dates)
            and all(
                int(item.get("owner_count") or 0) == 1
                and int(item.get("lease_count") or 0) == 1
                for item in active_observations
            )
        )
        controlled_live_observed = bool(
            active_observations
            and all(
                str(item.get("live_execution_mode") or "") == "CONTROLLED_LIVE"
                for item in active_observations
            )
        )

        return {
            "evidence_schema_version": 2,
            "collector_derived": True,
            "commit_sha": self.commit_sha,
            "gate3_report_sha256": str(gate3_report_sha256 or "").lower(),
            "evidence_journal_sha256": audit.sha256,
            "capability_evidence_sha256": capability.get("evidence_sha256", ""),
            "execution_capabilities_verified": capability.get("verified") is True,
            "live_execution_mode": (
                "CONTROLLED_LIVE" if controlled_live_observed else ""
            ),
            "session_started_disarmed": bool(supervised_dates and started_disarmed),
            "manual_arm_after_active_readiness": bool(
                supervised_dates and manual_after_active
            ),
            "current_execution_owner_count": 1 if all_active_single_owner else 0,
            "current_execution_lease_count": 1 if all_active_single_owner else 0,
            "every_entry_has_active_trade_card": bool(
                entries
                and entries_after_manual_arm
                and all(item.get("active_trade_card") is True for item in entries)
            ),
            "every_buy_below_notional_cap": bool(
                entries
                and caps
                and min(caps) > 0
                and all(0 < value <= min(caps) for value in notionals)
            ),
            "portfolio_risk_rechecked_atomically": bool(
                entries and all(item.get("risk_rechecked_atomically") is True for item in entries)
            ),
            "lifecycle_comparisons_agree": bool(
                comparisons
                and lifecycle_all_sessions
                and lifecycle_mismatches == 0
            ),
            "disarm_probe_blocked_next_mutation": bool(disarm_probes),
            "external_critical_alert_delivered": bool(alerts),
            "final_reconciliation_matches_broker": bool(
                supervised_dates and final_reconciliation
            ),
            "automatic_mutation_retry_attempt_count": automatic_retries,
            "duplicate_or_unowned_mutation_count": duplicate_or_unowned,
            "unresolved_ambiguous_identity_count": ambiguous,
            "lifecycle_mismatch_count": lifecycle_mismatches,
            "supervised_regular_session_dates": supervised_dates,
            "controlled_live_config_sha256": controlled_live_config_sha256,
            "risk_limits_sha256": risk_limits_sha256,
            "reviewed_entry_notional_cap": min(caps) if caps else 0.0,
            "max_observed_entry_notional": max(notionals) if notionals else 0.0,
            "approved_symbols": sorted(approved_symbols),
            "observed_entry_symbols": sorted(observed_symbols),
            "strategy_entry_terminal_outcome_count": len(entry_terminals),
            "safe_exit_or_protected_position_count": len(protected_positions),
            "controlled_cancel_lifecycle_count": len(cancellations),
            "pilot_exception_evidence": False,
            "evidence_journal_parse_error_count": audit.parse_error_count,
            "evidence_journal_duplicate_event_id_count": audit.duplicate_event_id_count,
            "evidence_journal_hash_chain_error_count": audit.hash_chain_error_count,
            "evidence_journal_identity_mismatch_count": audit.identity_mismatch_count,
            "evidence_journal_unknown_event_type_count": audit.unknown_event_type_count,
            "review": dict(review or {}),
        }


__all__ = ["GATE4_EVIDENCE_EVENTS", "GATE4_NAME", "Gate4EvidenceCollector"]
