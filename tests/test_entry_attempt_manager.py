"""Tests for src.services.entry_attempt_manager."""
from __future__ import annotations

import datetime as dt

import pytest

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import capital_allocator
from src.services.entry_attempt_manager import (
    AttemptDeadlineAction,
    AttemptOutcome,
    EntryAttemptManager,
    EntryTrigger,
    decide_attempt_deadline_action,
)
from src.services.order_execution_service import DuplicateOpenOrderError

RISK_STRATEGY_ID = "TEST"
RISK_PLAN_ID = "TEST:AAPL"


def _risk_approval(symbol="AAPL", quantity=10, reference_price=100.0):
    return PreTradeRiskDecision.approve(
        environment="PROD",
        account_no="1",
        symbol=symbol,
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=quantity,
        reference_price=reference_price,
        exchange="NASD",
        execution_policy="REGULAR_LIMIT",
        strategy_id=RISK_STRATEGY_ID,
        plan_id=RISK_PLAN_ID,
    )


def _trigger(symbol="AAPL", trigger_at=None, kanban_priority=0, quantity=10, limit_price=100.0):
    return EntryTrigger(
        environment="PROD",
        account_no="1",
        symbol=symbol,
        trigger_at=trigger_at or dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc),
        kanban_priority=kanban_priority,
        quantity=quantity,
        limit_price=limit_price,
        notional=quantity * limit_price,
    )


def _order(symbol="AAPL", *, status=OrderStatus.ACCEPTED, filled=0, avg_fill_price=0.0, reservation_id="res-1"):
    order = BrokerOrder.create(
        environment="PROD",
        account_no="1",
        symbol=symbol,
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=10,
        limit_price=100.0,
        status=status,
        capital_reservation_id=reservation_id,
    )
    order.filled_quantity = filled
    order.avg_fill_price = avg_fill_price
    return order


def _manager(tmp_path, submit_order, buying_power=100_000.0, clock=None, capital_reservation_engine=None):
    reservations_path = tmp_path / "reservations.json"

    def provider(_environment, _account_no):
        return buying_power

    kwargs = dict(
        buying_power_provider=provider,
        submit_order=submit_order,
        reservations_path=reservations_path,
    )
    if clock is not None:
        kwargs["clock"] = clock
    if capital_reservation_engine is not None:
        kwargs["capital_reservation_engine"] = capital_reservation_engine
    manager = EntryAttemptManager(**kwargs)
    return manager, reservations_path


# --- Successful submission + capital wiring ---------------------------------


def test_successful_attempt_submits_and_reserves_capital(tmp_path):
    def fake_submit(**kwargs):
        assert kwargs["attempt_group_id"]
        assert kwargs["attempt_number"] == 1
        assert kwargs["capital_reservation_id"]
        return _order(status=OrderStatus.ACCEPTED)

    manager, _ = _manager(tmp_path, fake_submit)
    result = manager.attempt_entry(_trigger())
    assert result.outcome == AttemptOutcome.SUBMITTED
    assert result.order.status == OrderStatus.ACCEPTED
    assert result.reservation_id


def test_insufficient_capital_returns_waiting_for_capital(tmp_path):
    def fake_submit(**kwargs):
        pytest.fail("must not submit an order when capital is unavailable (spec section 391)")

    manager, _ = _manager(tmp_path, fake_submit, buying_power=100.0)
    result = manager.attempt_entry(_trigger(quantity=10, limit_price=100.0))  # notional 1000 > 100
    assert result.outcome == AttemptOutcome.WAITING_FOR_CAPITAL


def test_symbol_lock_blocks_only_that_symbol(tmp_path):
    def fake_submit(**kwargs):
        return _order(status=OrderStatus.ACCEPTED)

    manager, _ = _manager(tmp_path, fake_submit)
    key = manager._symbol_key("PROD", "1", "AAPL")
    lock = manager._lock_for(key)
    lock.acquire()
    try:
        blocked = manager.attempt_entry(_trigger(symbol="AAPL"))
        assert blocked.outcome == AttemptOutcome.SYMBOL_LOCKED
        # A different symbol must be entirely unaffected (section 344-354).
        other = manager.attempt_entry(_trigger(symbol="NVDA"))
        assert other.outcome == AttemptOutcome.SUBMITTED
    finally:
        lock.release()


def test_duplicate_open_order_releases_capital(tmp_path):
    def fake_submit(**kwargs):
        raise DuplicateOpenOrderError(_order())

    manager, path = _manager(tmp_path, fake_submit)
    result = manager.attempt_entry(_trigger())
    assert result.outcome == AttemptOutcome.DUPLICATE_ORDER
    reservations = capital_allocator.load_reservations(path)
    assert all(not r.is_open() for r in reservations)


def test_rejected_order_enters_cooldown_and_releases_capital(tmp_path):
    def fake_submit(**kwargs):
        return _order(status=OrderStatus.REJECTED)

    manager, path = _manager(tmp_path, fake_submit)
    first = manager.attempt_entry(_trigger())
    assert first.outcome == AttemptOutcome.REJECTED

    second = manager.attempt_entry(_trigger())
    assert second.outcome == AttemptOutcome.COOLDOWN

    reservations = capital_allocator.load_reservations(path)
    assert all(not r.is_open() for r in reservations)


def test_rate_limit_after_max_attempts_per_minute(tmp_path):
    clock_time = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)

    def clock():
        return clock_time

    def fake_submit(**kwargs):
        return _order(status=OrderStatus.REJECTED)

    manager, _ = _manager(tmp_path, fake_submit, clock=clock)

    outcomes = []
    for _ in range(6):
        result = manager.attempt_entry(_trigger())
        outcomes.append(result.outcome)
        # Skip past each cooldown so the next call actually re-attempts
        # instead of just reporting COOLDOWN.
        clock_time += dt.timedelta(seconds=10)

    assert AttemptOutcome.RATE_LIMITED in outcomes


def test_attempt_group_id_stable_across_retries(tmp_path):
    clock_time = dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc)

    def clock():
        return clock_time

    def fake_submit(**kwargs):
        return _order(status=OrderStatus.REJECTED)

    manager, _ = _manager(tmp_path, fake_submit, clock=clock)
    first = manager.attempt_entry(_trigger())
    clock_time += dt.timedelta(seconds=10)
    second = manager.attempt_entry(_trigger())

    assert first.attempt_group_id == second.attempt_group_id


# --- Simultaneous-trigger priority (spec section 9.4) -----------------------


def test_process_triggers_orders_by_timestamp_then_priority_then_symbol(tmp_path):
    submitted_order = []

    def fake_submit(**kwargs):
        submitted_order.append(kwargs["symbol"])
        return _order(symbol=kwargs["symbol"], status=OrderStatus.ACCEPTED)

    manager, _ = _manager(tmp_path, fake_submit)
    later = _trigger(symbol="NVDA", trigger_at=dt.datetime(2026, 1, 5, 14, 30, 5, tzinfo=dt.timezone.utc))
    earlier = _trigger(symbol="MSFT", trigger_at=dt.datetime(2026, 1, 5, 14, 30, 0, tzinfo=dt.timezone.utc))
    tie_a = _trigger(
        symbol="ZZZZ",
        trigger_at=dt.datetime(2026, 1, 5, 14, 30, 2, tzinfo=dt.timezone.utc),
        kanban_priority=1,
    )
    tie_b = _trigger(
        symbol="AAAA",
        trigger_at=dt.datetime(2026, 1, 5, 14, 30, 2, tzinfo=dt.timezone.utc),
        kanban_priority=5,
    )

    manager.process_triggers([later, earlier, tie_a, tie_b])
    assert submitted_order == ["MSFT", "AAAA", "ZZZZ", "NVDA"]


def test_first_trigger_reserves_capital_second_waits(tmp_path):
    """Section 384-389: the first candidate reserves capital; others that
    can't get capital stay WAITING_FOR_CAPITAL."""

    def fake_submit(**kwargs):
        return _order(symbol=kwargs["symbol"], status=OrderStatus.ACCEPTED)

    manager, _ = _manager(tmp_path, fake_submit, buying_power=1500.0)
    first = _trigger(symbol="AAPL", trigger_at=dt.datetime(2026, 1, 5, 14, 30, 0, tzinfo=dt.timezone.utc), quantity=10, limit_price=100.0)
    second = _trigger(symbol="NVDA", trigger_at=dt.datetime(2026, 1, 5, 14, 30, 1, tzinfo=dt.timezone.utc), quantity=10, limit_price=100.0)

    results = manager.process_triggers([first, second])
    assert results[0].outcome == AttemptOutcome.SUBMITTED
    assert results[1].outcome == AttemptOutcome.WAITING_FOR_CAPITAL


# --- Deadline decision table (spec section 401-444) -------------------------


@pytest.mark.parametrize(
    "status,filled,expected",
    [
        (OrderStatus.FILLED, 10, AttemptDeadlineAction.MOVE_TO_OPEN_POSITION),
        # PARTIALLY_FILLED/WORKING/ACCEPTED are all "still working" from a
        # pure status read -- whether to escalate to a cancel request
        # depends on the deadline/cancel_requested flags, which only
        # resolve_entry_order (not this pure function) has (code review
        # finding P0-7: a cancel is never assumed, only requested and later
        # confirmed).
        (OrderStatus.PARTIALLY_FILLED, 4, AttemptDeadlineAction.STILL_WORKING),
        (OrderStatus.WORKING, 0, AttemptDeadlineAction.STILL_WORKING),
        (OrderStatus.ACCEPTED, 0, AttemptDeadlineAction.STILL_WORKING),
        (OrderStatus.CANCEL_REQUESTED, 0, AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION),
        (OrderStatus.CANCELLED, 0, AttemptDeadlineAction.CONFIRMED_CANCELLED_ZERO_FILL),
        (OrderStatus.CANCELLED, 4, AttemptDeadlineAction.CONFIRMED_CANCELLED_WITH_FILL),
        (OrderStatus.UNKNOWN_SUBMISSION_STATE, 0, AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION),
        (OrderStatus.UNKNOWN, 0, AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION),
        (OrderStatus.REJECTED, 0, AttemptDeadlineAction.RELEASE_AND_RETRY_AFTER_COOLDOWN),
    ],
)
def test_decide_attempt_deadline_action(status, filled, expected):
    order = _order(status=status, filled=filled)
    assert decide_attempt_deadline_action(order) == expected


def test_resolve_entry_order_still_working_before_deadline_does_nothing(tmp_path):
    order = _order(status=OrderStatus.ACCEPTED, filled=0)
    manager, _ = _manager(tmp_path, lambda **kw: None)
    cancels = []
    action = manager.resolve_entry_order(
        order, at_deadline=False, cancel_requested=False, cancel_order=cancels.append
    )
    assert action == AttemptDeadlineAction.STILL_WORKING
    assert cancels == []


def test_resolve_entry_order_requests_cancel_at_deadline_but_does_not_finalize(tmp_path):
    """P0-7: the first call past the deadline only requests the cancel --
    capital settlement/cooldown must not happen until a later call sees a
    broker-confirmed terminal status."""
    order = _order(status=OrderStatus.ACCEPTED, filled=0, reservation_id="res-1")
    manager, _ = _manager(tmp_path, lambda **kw: None)
    cancels = []
    action = manager.resolve_entry_order(
        order, at_deadline=True, cancel_requested=False, cancel_order=cancels.append
    )
    assert action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION
    assert cancels == [order]


def test_resolve_entry_order_repeated_await_does_not_resend_cancel(tmp_path):
    order = _order(status=OrderStatus.CANCEL_REQUESTED, filled=0)
    manager, _ = _manager(tmp_path, lambda **kw: None)
    cancels = []
    action = manager.resolve_entry_order(
        order, at_deadline=True, cancel_requested=False, cancel_order=cancels.append
    )
    assert action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION
    assert cancels == []  # already requested -- must not resend


def test_handle_attempt_deadline_full_fill_consumes_reservation(tmp_path):
    manager, path = _manager(tmp_path, lambda **kw: None)
    reservation = capital_allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=1000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    order = _order(status=OrderStatus.FILLED, filled=10, avg_fill_price=100.0, reservation_id=reservation.reservation_id)

    cancels = []
    action = manager.handle_attempt_deadline(order, cancel_order=cancels.append)

    assert action == AttemptDeadlineAction.MOVE_TO_OPEN_POSITION
    assert cancels == []
    stored = capital_allocator.load_reservations(path)[0]
    assert not stored.is_open()


def test_handle_attempt_deadline_zero_fill_only_requests_cancel_first_call(tmp_path):
    """P0-7: capital must stay reserved and no cooldown starts until a
    *later* call observes the broker-confirmed CANCELLED status -- the
    first call at the deadline only sends the cancel request."""
    manager, path = _manager(tmp_path, lambda **kw: None)
    reservation = capital_allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=1000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    order = _order(status=OrderStatus.ACCEPTED, filled=0, reservation_id=reservation.reservation_id)

    cancels = []
    action = manager.handle_attempt_deadline(order, cancel_order=cancels.append)

    assert action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION
    assert cancels == [order]
    stored = capital_allocator.load_reservations(path)[0]
    assert stored.is_open()  # not yet released


def test_handle_attempt_deadline_finalizes_once_broker_confirms_cancelled(tmp_path):
    manager, path = _manager(tmp_path, lambda **kw: None)
    reservation = capital_allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=1000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    order = _order(status=OrderStatus.ACCEPTED, filled=0, reservation_id=reservation.reservation_id)
    manager.handle_attempt_deadline(order, cancel_order=lambda o: None)  # first tick: request only

    order.status = OrderStatus.CANCELLED  # broker confirms on a later tick
    cancels = []
    action = manager.handle_attempt_deadline(order, cancel_order=cancels.append)

    assert action == AttemptDeadlineAction.CONFIRMED_CANCELLED_ZERO_FILL
    assert cancels == []  # no cancel resent -- it's already terminal
    stored = capital_allocator.load_reservations(path)[0]
    assert stored.status.value == "RELEASED"

    # Cooldown must now block an immediate re-attempt for this symbol.
    def fake_submit(**kwargs):
        pytest.fail("must not submit during cooldown")

    manager._submit_order = fake_submit
    result = manager.attempt_entry(_trigger())
    assert result.outcome == AttemptOutcome.COOLDOWN


def test_handle_attempt_deadline_unknown_leaves_capital_reserved(tmp_path):
    manager, path = _manager(tmp_path, lambda **kw: None)
    reservation = capital_allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=1000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    order = _order(status=OrderStatus.UNKNOWN_SUBMISSION_STATE, reservation_id=reservation.reservation_id)

    cancels = []
    action = manager.handle_attempt_deadline(order, cancel_order=cancels.append)

    assert action == AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION
    assert cancels == []
    stored = capital_allocator.load_reservations(path)[0]
    assert stored.is_open()


def test_reset_symbol_clears_cooldown(tmp_path):
    def fake_submit(**kwargs):
        return _order(status=OrderStatus.REJECTED)

    manager, _ = _manager(tmp_path, fake_submit)
    manager.attempt_entry(_trigger())
    manager.reset_symbol("PROD", "1", "AAPL")

    def fake_submit_after_reset(**kwargs):
        return _order(status=OrderStatus.ACCEPTED)

    manager._submit_order = fake_submit_after_reset
    result = manager.attempt_entry(_trigger())
    assert result.outcome == AttemptOutcome.SUBMITTED


# --- P1-1: capital_reservation_engine is actually used -----------------------


def test_capital_reservation_engine_is_passed_to_the_allocator(tmp_path, monkeypatch):
    """build_buyboard_runtime accepted this parameter but never passed it
    anywhere -- confirm EntryAttemptManager actually forwards it into every
    capital_allocator call, not just stores it unused."""
    captured_engines = []
    original = capital_allocator.reserve_capital_for_entry

    def spy(**kwargs):
        captured_engines.append(kwargs.get("engine"))
        return original(**kwargs)

    monkeypatch.setattr(capital_allocator, "reserve_capital_for_entry", spy)
    sentinel_engine = object()
    manager, _ = _manager(
        tmp_path, lambda **kw: _order(status=OrderStatus.ACCEPTED), capital_reservation_engine=sentinel_engine
    )

    manager.attempt_entry(_trigger())

    assert captured_engines == [sentinel_engine]


def test_capital_reservation_db_failure_blocks_only_that_symbols_attempt(tmp_path, monkeypatch):
    """Review finding P1-1: a central-reservation database failure must
    block the affected symbol's entry (fail closed) rather than silently
    falling back to "capital looks available" -- and must not prevent a
    *different* symbol's attempt in the same batch from succeeding."""

    def flaky_reserve(**kwargs):
        if kwargs["symbol"] == "AAPL":
            raise RuntimeError("simulated database outage")
        return capital_allocator_original(**kwargs)

    capital_allocator_original = capital_allocator.reserve_capital_for_entry
    monkeypatch.setattr(capital_allocator, "reserve_capital_for_entry", flaky_reserve)

    submitted = []

    def fake_submit(**kw):
        submitted.append(kw["symbol"])
        return _order(symbol=kw["symbol"], status=OrderStatus.ACCEPTED)

    manager, _ = _manager(tmp_path, fake_submit)

    results = manager.process_triggers([_trigger(symbol="AAPL"), _trigger(symbol="NVDA")])

    by_symbol = {r.trigger.symbol: r for r in results}
    assert by_symbol["AAPL"].outcome == AttemptOutcome.REJECTED
    assert by_symbol["AAPL"].retry_at is not None
    assert by_symbol["NVDA"].outcome == AttemptOutcome.SUBMITTED
    assert submitted == ["NVDA"]


# --- P1-1: the database read path fails loud instead of hiding reservations -


def test_list_active_reservations_propagates_database_errors(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError

    from src.services import capital_reservation_repository

    # A real engine pointed at a directory that does not exist -- connect()
    # genuinely fails, exercising the real SQLAlchemy error path rather
    # than a hand-rolled fake.
    broken_engine = create_engine(
        f"sqlite:///{tmp_path / 'does-not-exist' / 'db.sqlite'}", future=True
    )
    with pytest.raises(SQLAlchemyError):
        capital_reservation_repository.list_active_reservations(
            broken_engine, environment="PROD", account_no="1"
        )
