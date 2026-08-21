from __future__ import annotations

from src.core.execution_queue import (
    ExecutionQueueItem,
    OrbCandidate,
    OrbCandidateStatus,
    SUPPORTED_ORB_WINDOWS,
)
from src.core.orb_combinations import (
    ORB_RISK_CASES,
    build_orb_position_combinations,
)


def _candidate(window: str, **overrides) -> OrbCandidate:
    values = dict(
        symbol="AAPL",
        window=window,
        orb_high=100.0,
        orb_low=98.0,
        breakout_price=99.0,
        breakout_trigger=99.099,
        entry_trigger=100.0,
        stop_loss=98.0,
        stop_loss_percent=2.0,
        stop_adr=50.0,
        status=OrbCandidateStatus.WAITING_BREAKOUT,
        reason="Waiting for price to clear entry trigger",
    )
    values.update(overrides)
    return OrbCandidate(**values)


def _queue(**candidate_overrides) -> ExecutionQueueItem:
    return ExecutionQueueItem(
        symbol="AAPL",
        candidates={
            window: _candidate(window, **candidate_overrides)
            for window in SUPPORTED_ORB_WINDOWS
        },
    )


def test_builds_every_risk_and_window_combination_without_mutating_queue():
    queue = _queue()
    original_candidates = dict(queue.candidates)

    combinations = build_orb_position_combinations(
        queue, account_equity=100_000.0
    )

    assert len(combinations) == len(ORB_RISK_CASES) * len(SUPPORTED_ORB_WINDOWS)
    assert {
        (item.risk_percent, item.window) for item in combinations
    } == {
        (risk, window)
        for risk in ORB_RISK_CASES
        for window in SUPPORTED_ORB_WINDOWS
    }
    assert queue.candidates == original_candidates
    assert queue.locked is False


def test_combination_validity_uses_canonical_capital_and_stop_adr_rules():
    combinations = build_orb_position_combinations(
        _queue(), account_equity=100_000.0
    )

    valid = {
        (item.risk_percent, item.window)
        for item in combinations
        if item.valid
    }
    assert valid == {
        (risk, window)
        for risk in (0.0025, 0.005)
        for window in SUPPORTED_ORB_WINDOWS
    }
    too_large = next(
        item
        for item in combinations
        if item.risk_percent == 0.0075 and item.window == "1m"
    )
    assert too_large.capital_percent >= 30.0
    assert "Capital allocation" in too_large.reason


def test_buffered_breakout_failure_keeps_all_affected_options_visible_invalid():
    queue = _queue(orb_high=99.05, entry_trigger=99.05)

    combinations = build_orb_position_combinations(
        queue, account_equity=100_000.0
    )

    assert len(combinations) == 24
    assert not any(item.valid for item in combinations)
    assert all(
        "buffered breakout" in item.reason
        for item in combinations
    )


def test_missing_windows_still_produce_the_complete_diagnostic_matrix():
    queue = ExecutionQueueItem(
        symbol="AAPL",
        candidates={"5m": _candidate("5m")},
    )

    combinations = build_orb_position_combinations(
        queue, account_equity=100_000.0
    )

    assert len(combinations) == 24
    unavailable = [
        item for item in combinations if item.window in {"1m", "30m"}
    ]
    assert len(unavailable) == 16
    assert all(item.status == OrbCandidateStatus.NOT_AVAILABLE for item in unavailable)
    assert all(not item.valid for item in unavailable)


def test_persisted_candidate_recovers_equity_when_live_snapshot_is_not_ready():
    combinations = build_orb_position_combinations(
        _queue(shares=250, capital_percent=25.0), account_equity=0.0
    )

    assert any(item.valid for item in combinations)
    assert all(item.account_equity > 0 for item in combinations)


def test_rejected_runtime_candidate_never_appears_as_a_valid_combination():
    combinations = build_orb_position_combinations(
        _queue(
            status=OrbCandidateStatus.REJECTED,
            reason="ORB strategy did not emit an entry signal",
        ),
        account_equity=100_000.0,
    )

    assert not any(item.valid for item in combinations)
    assert all("did not emit" in item.reason for item in combinations)


def test_explicit_published_buffer_recalculates_every_combination_consistently():
    combinations = build_orb_position_combinations(
        _queue(),
        account_equity=100_000.0,
        buffer_pct=0.02,
    )

    assert not any(item.valid for item in combinations)
    assert {item.buffer_pct for item in combinations} == {0.02}
    assert {item.breakout_trigger for item in combinations} == {100.98}


def test_stale_queue_snapshot_is_diagnostic_only_and_never_green():
    combinations = build_orb_position_combinations(
        _queue(),
        account_equity=100_000.0,
        buffer_pct=0.001,
        snapshot_stale=True,
    )

    assert not any(item.valid for item in combinations)
    assert all(
        item.status == OrbCandidateStatus.NOT_AVAILABLE
        for item in combinations
    )
    assert all("Current-session" in item.reason for item in combinations)


def test_only_stale_source_windows_are_never_shown_green():
    queue = _queue()

    combinations = build_orb_position_combinations(
        queue,
        account_equity=100_000.0,
        buffer_pct=0.001,
        stale_windows={"1m"},
    )

    one_minute = [item for item in combinations if item.window == "1m"]
    other_windows = [item for item in combinations if item.window != "1m"]
    assert one_minute and not any(item.valid for item in one_minute)
    assert all(
        item.status == OrbCandidateStatus.NOT_AVAILABLE
        for item in one_minute
    )
    assert any(item.valid for item in other_windows)
