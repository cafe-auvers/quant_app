from types import SimpleNamespace

import pandas as pd
import pytest

from src.core.execution_queue import (
    ExecutionQueueManager,
    ExecutionQueueStatus,
    OrbCandidate,
    OrbCandidateStatus,
    build_orb_candidate,
    queue_key,
    resolve_queue_status,
    select_best_orb_candidate,
)


def _candidate(window, score, valid=True, status=OrbCandidateStatus.EXECUTE_READY):
    return OrbCandidate(
        symbol="AAPL",
        window=window,
        score=score,
        valid=valid,
        status=status,
        entry_trigger=100.0,
        current_price=101.0,
        shares=100,
        capital_percent=15.0,
        stop_loss_percent=2.0,
        stop_adr=50.0,
        risk_percent=0.005,
    )


def _intraday(minutes=31, high=100.0, low=98.0, close=101.0):
    index = pd.date_range("2026-07-01 09:30", periods=minutes, freq="min")
    rows = []
    for i, _ts in enumerate(index):
        rows.append(
            {
                "Open": 99.0,
                "High": high + (0.01 if i == 0 else 0.0),
                "Low": low - (0.01 if i == 0 else 0.0),
                "Close": close,
                "Volume": 1000,
            }
        )
    return pd.DataFrame(rows, index=index)


def test_one_symbol_creates_only_one_execution_queue_item():
    manager = ExecutionQueueManager()

    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    manager.upsert_item(
        symbol="aapl", name="Apple", candidates={"5m": _candidate("5m", 60)}
    )

    assert len(manager.items) == 1
    assert manager.items[queue_key("AAPL", "PROD")].name == "Apple"


def test_non_production_environment_is_rejected():
    manager = ExecutionQueueManager()

    with pytest.raises(ValueError, match="PROD"):
        manager.upsert_item(
            symbol="AAPL",
            environment="SIM",
            candidates={"1m": _candidate("1m", 50)},
        )
    with pytest.raises(ValueError, match="PROD"):
        queue_key("AAPL", "SIM")


def test_1m_candidate_becomes_available_first_and_is_selected():
    manager = ExecutionQueueManager()
    item = manager.upsert_item(
        symbol="AAPL",
        candidates={
            "1m": _candidate("1m", 55),
            "5m": _candidate("5m", 0, valid=False, status=OrbCandidateStatus.FORMING),
            "30m": _candidate("30m", 0, valid=False, status=OrbCandidateStatus.FORMING),
        },
    )

    assert item.selected_window == "1m"
    assert item.status == ExecutionQueueStatus.EXECUTE_READY


def test_5m_candidate_replaces_1m_when_score_improves_by_margin():
    manager = ExecutionQueueManager(upgrade_margin=5.0)
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})

    item = manager.upsert_item(
        symbol="AAPL",
        candidates={
            "1m": _candidate("1m", 50),
            "5m": _candidate("5m", 55),
        },
    )

    assert item.selected_window == "5m"


def test_5m_candidate_does_not_replace_1m_below_upgrade_margin():
    manager = ExecutionQueueManager(upgrade_margin=5.0)
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})

    item = manager.upsert_item(
        symbol="AAPL",
        candidates={
            "1m": _candidate("1m", 50),
            "5m": _candidate("5m", 54.9),
        },
    )

    assert item.selected_window == "1m"


def test_30m_candidate_replaces_5m_only_when_valid_and_sufficiently_better():
    manager = ExecutionQueueManager(upgrade_margin=5.0)
    manager.upsert_item(
        symbol="AAPL",
        candidates={
            "1m": _candidate("1m", 50),
            "5m": _candidate("5m", 60),
        },
    )

    item = manager.upsert_item(
        symbol="AAPL",
        candidates={
            "5m": _candidate("5m", 60),
            "30m": _candidate("30m", 70),
        },
    )

    assert item.selected_window == "30m"


def test_invalid_candidates_are_ignored_by_selection():
    selected = select_best_orb_candidate(
        {
            "1m": _candidate(
                "1m", 90, valid=False, status=OrbCandidateStatus.RISK_INVALID
            ),
            "5m": _candidate("5m", 50, valid=True),
        },
        current_selected_window=None,
        locked=False,
    )

    assert selected.window == "5m"


def test_missing_manual_breakout_price_prevents_execute_ready():
    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3),
        breakout_price=None,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.REJECTED
    assert candidate.valid is False
    assert "Manual breakout price" in candidate.reason


def test_current_price_below_entry_trigger_is_armed_not_execute_ready():
    manager = ExecutionQueueManager()
    waiting = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=100.5,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )
    item = manager.upsert_item(symbol="AAPL", candidates={"1m": waiting})

    assert waiting.status == OrbCandidateStatus.WAITING_BREAKOUT
    assert item.status == ExecutionQueueStatus.ARMED


def test_current_price_above_entry_trigger_with_valid_risk_is_execute_ready():
    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.EXECUTE_READY
    assert candidate.valid is True
    assert candidate.shares >= 1
    assert candidate.source_session_date == "2026-07-01"


def test_candidate_source_session_date_survives_queue_round_trip():
    manager = ExecutionQueueManager()
    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )
    manager.upsert_item(symbol="AAPL", candidates={"1m": candidate})

    restored = ExecutionQueueManager.from_dict(manager.to_dict())

    assert (
        restored.get_item("AAPL", "PROD")
        .candidates["1m"]
        .source_session_date
        == "2026-07-01"
    )


def test_terminal_rejection_proof_survives_queue_round_trip():
    manager = ExecutionQueueManager()
    candidate = OrbCandidate(
        symbol="AAPL",
        window="1m",
        status=OrbCandidateStatus.REJECTED,
        terminal_rejection=True,
        reason="structural breakout rejection",
    )
    manager.upsert_item(symbol="AAPL", candidates={"1m": candidate})

    restored = ExecutionQueueManager.from_dict(manager.to_dict())

    assert restored.get_item("AAPL", "PROD").candidates["1m"].terminal_rejection


def test_orb_high_must_clear_buffered_breakout_trigger():
    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=100.0, low=98.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.REJECTED
    assert candidate.valid is False
    assert "has not cleared breakout trigger" in candidate.reason


def test_after_order_submission_selected_window_is_locked():
    manager = ExecutionQueueManager(upgrade_margin=5.0)
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    manager.mark_order_submitted("AAPL", order_id="ORDER-1")

    item = manager.upsert_item(
        symbol="AAPL",
        candidates={
            "1m": _candidate("1m", 50),
            "5m": _candidate("5m", 90),
        },
    )

    assert item.locked is True
    assert item.selected_window == "1m"
    assert item.status == ExecutionQueueStatus.ORDER_SUBMITTED


def test_safe_account_reassignment_discards_detached_queue_state():
    manager = ExecutionQueueManager()
    old_candidate = _candidate("1m", 50)
    item = manager.upsert_item(
        symbol="AAPL",
        account_no="account-1",
        candidates={"1m": old_candidate},
    )
    item.locked = True
    item.manual_window_lock = True
    item.locked_reason = "Manual ORB selection"
    item.order_status = "REJECTED"
    item.order_id = "terminal-old-order"

    new_candidate = _candidate("5m", 80)
    reassigned = manager.upsert_item(
        symbol="AAPL",
        account_no="account-2",
        candidates={"5m": new_candidate},
    )

    assert reassigned.account_no == "account-2"
    assert reassigned.candidates == {"5m": new_candidate}
    assert reassigned.selected_window == "5m"
    assert reassigned.selected_candidate is new_candidate
    assert reassigned.locked is False
    assert reassigned.manual_window_lock is False
    assert reassigned.locked_reason is None
    assert reassigned.order_status is None
    assert reassigned.order_id is None


@pytest.mark.parametrize(
    "order_status",
    ["SUBMITTED", "WORKING", "UNKNOWN_SUBMISSION_STATE", "FILLED"],
)
def test_account_reassignment_refuses_unresolved_or_live_order(order_status):
    manager = ExecutionQueueManager()
    manager.upsert_item(
        symbol="AAPL",
        account_no="account-1",
        candidates={"1m": _candidate("1m", 50)},
    )
    manager.mark_order_submitted(
        "AAPL",
        order_id="ORDER-1",
        order_status=order_status,
    )
    before = manager.get_item("AAPL", "PROD").to_dict()

    with pytest.raises(ValueError, match="requires broker reconciliation"):
        manager.upsert_item(
            symbol="AAPL",
            account_no="account-2",
            candidates={"5m": _candidate("5m", 80)},
        )

    assert manager.get_item("AAPL", "PROD").to_dict() == before


def test_account_reassignment_does_not_reapply_symbol_scoped_saved_lock():
    manager = ExecutionQueueManager()
    manager.upsert_item(
        symbol="AAPL",
        account_no="account-1",
        candidates={"1m": _candidate("1m", 50)},
    )
    watch_item = SimpleNamespace(
        symbol="AAPL",
        name="Apple",
        breakout_price=100.0,
        stop_loss=98.0,
        selected_orb_plan={
            "window": "5m",
            "risk_percent": 0.02,
            "buffer_pct": 0.02,
        },
    )

    reassigned = manager.build_or_update_from_watchlist_item(
        watch_item,
        {
            "1m": _intraday(high=101.0),
            "5m": _intraday(high=101.0),
            "30m": _intraday(high=101.0),
        },
        current_price=102.0,
        account_size=100_000.0,
        risk_percent=0.005,
        account_no="account-2",
        adr_percent=5.0,
        buffer_pct=0.001,
        force_buffer_pct=True,
    )

    assert reassigned.account_no == "account-2"
    assert reassigned.manual_window_lock is False
    assert reassigned.locked is False
    assert reassigned.locked_reason is None


def test_order_failure_unlocks_selected_candidate_for_retry():
    manager = ExecutionQueueManager()
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    manager.mark_order_submitted("AAPL", order_id="ORDER-1")

    manager.mark_order_failed("AAPL")

    item = manager.items[queue_key("AAPL", "PROD")]
    assert item.locked is False
    assert item.order_status == "REJECTED"
    assert item.selected_window == "1m"
    assert item.status == ExecutionQueueStatus.EXECUTE_READY


def test_execution_queue_serializes_enum_values_round_trip():
    manager = ExecutionQueueManager()
    manager.upsert_item(
        symbol="AAPL",
        account_no="12345678-01",
        candidates={"1m": _candidate("1m", 50)},
    )
    manager.mark_order_submitted("AAPL", order_id="ORDER-1")

    restored = ExecutionQueueManager.from_dict(manager.to_dict())

    assert (
        restored.items[queue_key("AAPL", "PROD")].status
        == ExecutionQueueStatus.ORDER_SUBMITTED
    )
    assert (
        restored.items[queue_key("AAPL", "PROD")].selected_candidate.status
        == OrbCandidateStatus.EXECUTE_READY
    )
    assert restored.items[queue_key("AAPL", "PROD")].environment == "PROD"
    assert (
        restored.items[queue_key("AAPL", "PROD")].account_no
        == "12345678-01"
    )


def test_execution_queue_legacy_naive_timestamp_is_normalized_to_utc():
    manager = ExecutionQueueManager.from_dict(
        {
            "items": {
                "PROD:AAPL": {
                    "symbol": "AAPL",
                    "environment": "PROD",
                    "last_updated": "2026-07-01T09:30:00",
                }
            }
        }
    )

    assert manager.get_item("AAPL", "PROD").last_updated.utcoffset().total_seconds() == 0


def test_execution_queue_invalid_container_is_reported_without_partial_load():
    rejected = []

    manager = ExecutionQueueManager.from_dict(
        {"upgrade_margin": "invalid", "items": ["not", "a", "mapping"]},
        on_rejected=lambda index, record, error: rejected.append(
            (index, record, error)
        ),
    )

    assert manager.items == {}
    assert manager.upgrade_margin == 0.0
    assert [record["field"] for _, record, _ in rejected] == [
        "upgrade_margin",
        "items",
    ]


def test_legacy_symbol_only_execution_queue_state_is_ignored():
    manager = ExecutionQueueManager()
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    old_item = manager.items[queue_key("AAPL", "PROD")].to_dict()
    old_item.pop("environment")

    restored = ExecutionQueueManager.from_dict(
        {
            "upgrade_margin": 5.0,
            "items": {"AAPL": old_item},
        }
    )

    assert restored.items == {}


def test_legacy_sim_execution_queue_state_is_ignored():
    restored = ExecutionQueueManager.from_dict(
        {
            "items": {
                "SIM:AAPL": {
                    "symbol": "AAPL",
                    "environment": "SIM",
                },
            },
        }
    )

    assert restored.items == {}


def test_duplicate_pending_or_submitted_orders_are_prevented():
    manager = ExecutionQueueManager()
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    manager.mark_order_submitted("AAPL", order_id="ORDER-1")

    duplicate_candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
        duplicate_pending_order=manager.has_pending_or_submitted_order("AAPL"),
    )

    assert manager.has_pending_or_submitted_order("AAPL") is True
    assert manager.has_pending_or_submitted_order("AAPL", environment="PROD") is True
    assert duplicate_candidate.status == OrbCandidateStatus.REJECTED
    assert duplicate_candidate.terminal_rejection is False
    assert "Duplicate" in duplicate_candidate.reason


@pytest.mark.parametrize("account_size", [None, 0.0, float("nan")])
def test_missing_or_zero_sizing_equity_never_proves_terminal_rejection(account_size):
    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=100.9),
        breakout_price=100.0,
        current_price=102.0,
        account_size=account_size,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.RISK_INVALID
    assert candidate.terminal_rejection is False


def test_missing_current_price_never_proves_terminal_rejection():
    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=None,
        account_size=100_000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.RISK_INVALID
    assert candidate.terminal_rejection is False


def test_structural_rejection_with_known_equity_is_terminal():
    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=99.0, low=98.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100_000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.REJECTED
    assert candidate.terminal_rejection is True


def test_unknown_submission_state_resolves_and_blocks_duplicate():
    manager = ExecutionQueueManager()
    manager.upsert_item(
        symbol="AAPL", environment="PROD", candidates={"1m": _candidate("1m", 50)}
    )

    for order_status in (
        "UNKNOWN",
        "UNKNOWN_SUBMISSION_STATE",
        "AMBIGUOUS",
        "TIMEOUT",
        "NETWORK_ERROR",
    ):
        assert (
            resolve_queue_status(
                {"1m": _candidate("1m", 50)},
                _candidate("1m", 50),
                locked=True,
                order_status=order_status,
            )
            == ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE
        )

    manager.mark_order_submitted(
        "AAPL",
        order_id="LOCAL-1",
        order_status="UNKNOWN_SUBMISSION_STATE",
        environment="PROD",
    )

    prod_item = manager.items[queue_key("AAPL", "PROD")]
    assert prod_item.status == ExecutionQueueStatus.UNKNOWN_SUBMISSION_STATE
    assert prod_item.locked is True
    assert manager.has_pending_or_submitted_order("AAPL", environment="PROD") is True


def test_queue_status_rejected_when_all_candidates_fail_hard_validation():
    manager = ExecutionQueueManager()
    item = manager.upsert_item(
        symbol="AAPL",
        candidates={
            "1m": _candidate("1m", 0, valid=False, status=OrbCandidateStatus.REJECTED),
            "5m": _candidate(
                "5m", 0, valid=False, status=OrbCandidateStatus.RISK_INVALID
            ),
        },
    )

    assert item.status == ExecutionQueueStatus.REJECTED


def test_queue_status_orb_forming_when_windows_not_completed():
    manager = ExecutionQueueManager()
    forming = build_orb_candidate(
        symbol="AAPL",
        window="5m",
        intraday=_intraday(minutes=3),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )
    item = manager.upsert_item(symbol="AAPL", candidates={"5m": forming})

    assert forming.status == OrbCandidateStatus.FORMING
    assert item.status == ExecutionQueueStatus.ORB_FORMING


def test_completed_orb_window_with_missing_opening_bar_is_not_available():
    intraday = _intraday(minutes=4)
    intraday = intraday.iloc[1:]

    candidate = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=intraday,
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.NOT_AVAILABLE
    assert candidate.source_session_date == "2026-07-01"
    assert "09:30 opening bar is unavailable" in candidate.reason


def test_stop_adr_validation_follows_existing_thresholds():
    too_tight = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=100.9),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.0001,
        adr_percent=5.0,
    )
    valid = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )
    too_wide = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=90.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert too_tight.status == OrbCandidateStatus.RISK_INVALID
    assert valid.status == OrbCandidateStatus.EXECUTE_READY
    assert too_wide.status == OrbCandidateStatus.RISK_INVALID


def test_capital_allocation_auto_selects_valid_supported_risk_case():
    too_low = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.001,
        adr_percent=5.0,
    )
    valid = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )
    too_high = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=101.0, low=99.0),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100000.0,
        risk_percent=0.01,
        adr_percent=5.0,
    )

    assert too_low.status == OrbCandidateStatus.EXECUTE_READY
    assert valid.status == OrbCandidateStatus.EXECUTE_READY
    assert too_high.status == OrbCandidateStatus.EXECUTE_READY
    assert 0.0025 <= too_low.risk_percent <= 0.02
    assert 0.0025 <= valid.risk_percent <= 0.02
    assert 0.0025 <= too_high.risk_percent <= 0.02
