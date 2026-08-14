"""Tests for src.core.capital_reservation and src.services.capital_allocator."""
from __future__ import annotations

import threading

import pytest

from src.core.capital_reservation import (
    CapitalReservation,
    CapitalReservationStatus,
    available_for_new_entries,
)
from src.services import capital_allocator as allocator


# --- CapitalReservation lifecycle -------------------------------------------


def test_create_starts_fully_reserved():
    reservation = CapitalReservation.create(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="grp-1",
        requested_notional=1000.0,
    )
    assert reservation.status == CapitalReservationStatus.RESERVED
    assert reservation.remaining_reserved_notional == 1000.0
    assert reservation.is_open()


def test_partial_consume_transitions_to_partially_consumed():
    reservation = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g", requested_notional=1000.0
    )
    reservation.consume(300.0)
    assert reservation.status == CapitalReservationStatus.PARTIALLY_CONSUMED
    assert reservation.remaining_reserved_notional == pytest.approx(700.0)
    assert reservation.is_open()


def test_full_consume_transitions_to_consumed_and_closes():
    reservation = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g", requested_notional=1000.0
    )
    reservation.consume(1000.0)
    assert reservation.status == CapitalReservationStatus.CONSUMED
    assert reservation.remaining_reserved_notional == 0.0
    assert not reservation.is_open()


def test_release_zeroes_remaining_and_closes():
    reservation = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g", requested_notional=1000.0
    )
    reservation.release()
    assert reservation.status == CapitalReservationStatus.RELEASED
    assert reservation.remaining_reserved_notional == 0.0
    assert reservation.released_at is not None
    assert not reservation.is_open()


def test_to_dict_from_dict_round_trip():
    reservation = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g", requested_notional=500.0
    )
    reservation.consume(200.0)
    restored = CapitalReservation.from_dict(reservation.to_dict())
    assert restored.to_dict() == reservation.to_dict()


# --- available_for_new_entries formula (spec section 865-870) --------------


def test_available_for_new_entries_subtracts_only_open_reservations():
    open_res = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g1", requested_notional=1000.0
    )
    released_res = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="MSFT", attempt_group_id="g2", requested_notional=2000.0
    )
    released_res.release()

    available = available_for_new_entries(10_000.0, [open_res, released_res])
    assert available == pytest.approx(9_000.0)


# --- capital_allocator.reserve_capital_for_entry ----------------------------


def test_reserve_capital_succeeds_when_available(tmp_path):
    path = tmp_path / "reservations.json"
    reservation = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=2000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    assert reservation is not None
    assert reservation.remaining_reserved_notional == 2000.0
    stored = allocator.load_reservations(path)
    assert len(stored) == 1


def test_reserve_capital_returns_none_when_insufficient(tmp_path):
    """Spec section 391: never submit and hope KIS rejects for insufficient funds."""
    path = tmp_path / "reservations.json"
    allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=9000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    second = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        attempt_group_id="g2",
        requested_notional=5000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    assert second is None
    # The rejected candidate must not have consumed any capital.
    assert len(allocator.load_reservations(path)) == 1


def test_reserve_capital_scoped_per_account(tmp_path):
    path = tmp_path / "reservations.json"
    allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=9000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    other_account = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="2",
        symbol="AAPL",
        attempt_group_id="g2",
        requested_notional=9000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    assert other_account is not None


def test_consume_then_release_lifecycle(tmp_path):
    path = tmp_path / "reservations.json"
    reservation = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=1000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    allocator.consume_reservation(reservation.reservation_id, 300.0, path=path)
    updated = allocator.load_reservations(path)[0]
    assert updated.status == CapitalReservationStatus.PARTIALLY_CONSUMED
    assert updated.remaining_reserved_notional == pytest.approx(700.0)

    allocator.release_reservation(reservation.reservation_id, path=path)
    released = allocator.load_reservations(path)[0]
    assert released.status == CapitalReservationStatus.RELEASED
    assert released.remaining_reserved_notional == 0.0


def test_two_near_simultaneous_triggers_do_not_both_reserve_the_same_capital(tmp_path):
    """Spec section 878: "This prevents two near-simultaneous triggers from
    both believing they have access to the same capital." """
    path = tmp_path / "reservations.json"
    results = []
    barrier = threading.Barrier(2)

    def attempt(symbol: str, group: str) -> None:
        barrier.wait(timeout=2)
        results.append(
            allocator.reserve_capital_for_entry(
                environment="PROD",
                account_no="1",
                symbol=symbol,
                attempt_group_id=group,
                requested_notional=7000.0,
                buying_power_provider=lambda: 10_000.0,
                path=path,
            )
        )

    threads = [
        threading.Thread(target=attempt, args=("AAPL", "g1")),
        threading.Thread(target=attempt, args=("NVDA", "g2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    successes = [r for r in results if r is not None]
    assert len(successes) == 1
    assert len(allocator.load_reservations(path)) == 1
