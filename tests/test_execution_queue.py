import pandas as pd

from src.core.execution_queue import (
    ExecutionQueueManager,
    ExecutionQueueStatus,
    OrbCandidate,
    OrbCandidateStatus,
    build_orb_candidate,
    queue_key,
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
    manager.upsert_item(symbol="aapl", name="Apple", candidates={"5m": _candidate("5m", 60)})

    assert len(manager.items) == 1
    assert manager.items[queue_key("AAPL", "SIM")].name == "Apple"


def test_same_symbol_can_be_queued_independently_by_environment():
    manager = ExecutionQueueManager()

    sim_item = manager.upsert_item(
        symbol="AAPL",
        environment="SIM",
        name="Apple SIM",
        candidates={"1m": _candidate("1m", 50)},
    )
    prod_item = manager.upsert_item(
        symbol="AAPL",
        environment="PROD",
        name="Apple PROD",
        candidates={"5m": _candidate("5m", 60)},
    )

    assert len(manager.items) == 2
    assert manager.items[queue_key("AAPL", "SIM")] is sim_item
    assert manager.items[queue_key("AAPL", "PROD")] is prod_item
    assert sim_item.name == "Apple SIM"
    assert prod_item.name == "Apple PROD"


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
            "1m": _candidate("1m", 90, valid=False, status=OrbCandidateStatus.RISK_INVALID),
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
        intraday=_intraday(minutes=3, high=100.0, low=98.0),
        breakout_price=100.0,
        current_price=99.5,
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
        intraday=_intraday(minutes=3, high=100.0, low=98.0),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert candidate.status == OrbCandidateStatus.EXECUTE_READY
    assert candidate.valid is True
    assert candidate.shares >= 1


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


def test_order_failure_unlocks_selected_candidate_for_retry():
    manager = ExecutionQueueManager()
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    manager.mark_order_submitted("AAPL", order_id="ORDER-1")

    manager.mark_order_failed("AAPL")

    item = manager.items[queue_key("AAPL", "SIM")]
    assert item.locked is False
    assert item.order_status == "REJECTED"
    assert item.selected_window == "1m"
    assert item.status == ExecutionQueueStatus.EXECUTE_READY


def test_execution_queue_serializes_enum_values_round_trip():
    manager = ExecutionQueueManager()
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    manager.mark_order_submitted("AAPL", order_id="ORDER-1")

    restored = ExecutionQueueManager.from_dict(manager.to_dict())

    assert restored.items[queue_key("AAPL", "SIM")].status == ExecutionQueueStatus.ORDER_SUBMITTED
    assert restored.items[queue_key("AAPL", "SIM")].selected_candidate.status == OrbCandidateStatus.EXECUTE_READY
    assert restored.items[queue_key("AAPL", "SIM")].environment == "SIM"


def test_old_symbol_only_execution_queue_state_loads_as_sim_key():
    manager = ExecutionQueueManager()
    manager.upsert_item(symbol="AAPL", candidates={"1m": _candidate("1m", 50)})
    old_item = manager.items[queue_key("AAPL", "SIM")].to_dict()
    old_item.pop("environment")

    restored = ExecutionQueueManager.from_dict({
        "upgrade_margin": 5.0,
        "items": {"AAPL": old_item},
    })

    assert list(restored.items) == [queue_key("AAPL", "SIM")]
    assert restored.items[queue_key("AAPL", "SIM")].symbol == "AAPL"
    assert restored.items[queue_key("AAPL", "SIM")].environment == "SIM"


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
    assert manager.has_pending_or_submitted_order("AAPL", environment="PROD") is False
    assert duplicate_candidate.status == OrbCandidateStatus.REJECTED
    assert "Duplicate" in duplicate_candidate.reason


def test_queue_status_rejected_when_all_candidates_fail_hard_validation():
    manager = ExecutionQueueManager()
    item = manager.upsert_item(
        symbol="AAPL",
        candidates={
            "1m": _candidate("1m", 0, valid=False, status=OrbCandidateStatus.REJECTED),
            "5m": _candidate("5m", 0, valid=False, status=OrbCandidateStatus.RISK_INVALID),
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


def test_stop_adr_validation_follows_existing_thresholds():
    too_tight = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=100.0, low=99.9),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.0001,
        adr_percent=5.0,
    )
    valid = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=100.0, low=98.0),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )
    too_wide = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=100.0, low=90.0),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )

    assert too_tight.status == OrbCandidateStatus.RISK_INVALID
    assert valid.status == OrbCandidateStatus.EXECUTE_READY
    assert too_wide.status == OrbCandidateStatus.RISK_INVALID


def test_capital_allocation_validation_follows_existing_thresholds():
    too_low = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=100.0, low=98.0),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.001,
        adr_percent=5.0,
    )
    valid = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=100.0, low=98.0),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.005,
        adr_percent=5.0,
    )
    too_high = build_orb_candidate(
        symbol="AAPL",
        window="1m",
        intraday=_intraday(minutes=3, high=100.0, low=98.0),
        breakout_price=100.0,
        current_price=101.0,
        account_size=100000.0,
        risk_percent=0.01,
        adr_percent=5.0,
    )

    assert too_low.status == OrbCandidateStatus.RISK_INVALID
    assert valid.status == OrbCandidateStatus.EXECUTE_READY
    assert too_high.status == OrbCandidateStatus.RISK_INVALID
