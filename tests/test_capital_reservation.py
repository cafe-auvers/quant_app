"""Tests for src.core.capital_reservation and src.services.capital_allocator."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from src.core.capital_reservation import (
    CapitalReservation,
    CapitalReservationStatus,
    available_for_new_entries,
)
from src.services import capital_allocator as allocator
from src.services import capital_reservation_repository
from src.risk.portfolio import PortfolioRiskReservationSpec


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


def test_partial_consume_reduces_projected_open_risk_proportionally():
    reservation = CapitalReservation.create(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g",
        requested_notional=1_000.0,
        projected_open_risk=100.0,
    )

    reservation.consume(250.0)

    assert reservation.remaining_reserved_notional == pytest.approx(750.0)
    assert reservation.remaining_projected_open_risk == pytest.approx(75.0)


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


def test_account_transaction_serializes_concurrent_projected_position_slots(tmp_path):
    engine = _sqlite_engine(tmp_path)
    capital_reservation_repository.ensure_capital_reservations_table(engine)
    barrier = threading.Barrier(2)
    successes = []
    failures = []

    def attempt(symbol: str) -> None:
        reservation = CapitalReservation.create(
            environment="PROD",
            account_no="1",
            symbol=symbol,
            attempt_group_id=f"group-{symbol}",
            requested_notional=1_000.0,
            projected_open_risk=100.0,
        )
        spec = PortfolioRiskReservationSpec(
            environment="PROD",
            account_no="1",
            symbol=symbol,
            proposed_notional_usd=1_000.0,
            proposed_open_risk_usd=100.0,
            account_equity_usd=10_000.0,
            baseline_position_symbols=(),
            baseline_open_risk_usd=0.0,
            baseline_gross_notional_usd=0.0,
            max_simultaneous_positions=1,
            max_total_open_risk_fraction=1.0,
            max_gross_notional_fraction=1.0,
            evaluated_at=datetime.now(timezone.utc),
        )
        barrier.wait(timeout=2)
        try:
            with engine.begin() as conn:
                capital_reservation_repository.insert_reservation_if_available(
                    conn,
                    reservation,
                    buying_power=10_000.0,
                    portfolio_risk_spec=spec,
                )
            successes.append(symbol)
        except Exception as exc:  # captured for exact post-thread assertion
            failures.append(exc)

    threads = [
        threading.Thread(target=attempt, args=("AAPL",)),
        threading.Thread(target=attempt, args=("MSFT",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(
        failures[0],
        capital_reservation_repository.ProjectedPortfolioRiskLimitError,
    )
    assert len(
        capital_reservation_repository.list_active_reservations(
            engine, environment="PROD", account_no="1"
        )
    ) == 1


# --- P1-12: database-backed capital reservations (cross-device) ------------


def _sqlite_engine(tmp_path):
    from sqlalchemy.pool import NullPool

    return create_engine(f"sqlite:///{tmp_path / 'capital.db'}", future=True, poolclass=NullPool)


def test_reserve_capital_with_engine_mirrors_to_database(tmp_path):
    engine = _sqlite_engine(tmp_path)
    path = tmp_path / "reservations.json"

    reservation = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=1000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
        engine=engine,
    )

    assert reservation is not None
    db_active = capital_reservation_repository.list_active_reservations(
        engine, environment="PROD", account_no="1"
    )
    assert len(db_active) == 1
    assert db_active[0].reservation_id == reservation.reservation_id


def test_reservation_from_another_device_blocks_local_availability_check(tmp_path):
    """Cross-device visibility is the whole point of P1-12: a reservation
    this process's local JSON ledger has never seen (because another
    device made it) must still count against available capital."""
    engine = _sqlite_engine(tmp_path)
    path = tmp_path / "reservations.json"

    other_device_reservation = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="NVDA", attempt_group_id="g-other",
        requested_notional=9000.0,
    )
    capital_reservation_repository.save_reservation(engine, other_device_reservation)

    # This process's local ledger knows nothing about the NVDA reservation.
    result = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=5000.0,
        buying_power_provider=lambda: 10_000.0,
        path=path,
        engine=engine,
    )

    assert result is None  # only 1000 left after the other device's 9000


def test_consume_and_release_mirror_to_database(tmp_path):
    engine = _sqlite_engine(tmp_path)
    path = tmp_path / "reservations.json"

    reservation = allocator.reserve_capital_for_entry(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g1",
        requested_notional=1000.0, buying_power_provider=lambda: 10_000.0,
        path=path, engine=engine,
    )
    allocator.consume_reservation(reservation.reservation_id, 400.0, path=path, engine=engine)
    allocator.release_reservation(reservation.reservation_id, path=path, engine=engine)

    db_active = capital_reservation_repository.list_active_reservations(
        engine, environment="PROD", account_no="1"
    )
    assert db_active == []  # released -- no longer active


def test_allocator_conflict_repairs_local_mirror_from_authoritative_database(tmp_path):
    engine = _sqlite_engine(tmp_path)
    path = tmp_path / "reservations.json"
    reservation = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        attempt_group_id="g1",
        requested_notional=1000.0,
        buying_power_provider=lambda: 1000.0,
        path=path,
        engine=engine,
    )
    assert reservation is not None

    # Another device consumes only 100 while this process still has v1.
    newer = capital_reservation_repository.fetch_reservation(
        engine, reservation.reservation_id
    )
    assert newer is not None
    newer.consume(100.0)
    capital_reservation_repository.save_reservation_strict(engine, newer)
    assert newer.version == 2

    # The stale local mutation must lose its CAS and must not be mirrored.
    with pytest.raises(
        capital_reservation_repository.CapitalReservationVersionConflictError
    ):
        allocator.consume_reservation(
            reservation.reservation_id,
            600.0,
            path=path,
            engine=engine,
        )

    mirrored = allocator.load_reservations(path)[0]
    assert mirrored.version == 2
    assert mirrored.status == CapitalReservationStatus.PARTIALLY_CONSUMED
    assert mirrored.remaining_reserved_notional == pytest.approx(900.0)

    # The repaired/authoritative 900 reservation leaves only 100; a stale
    # local value of 400 would have incorrectly authorized this entry.
    denied = allocator.reserve_capital_for_entry(
        environment="PROD",
        account_no="1",
        symbol="NVDA",
        attempt_group_id="g2",
        requested_notional=200.0,
        buying_power_provider=lambda: 1000.0,
        path=path,
        engine=engine,
    )
    assert denied is None


def test_engine_none_never_touches_database(tmp_path):
    """Default behavior (engine=None) must be identical to before this
    module existed -- purely local JSON, no database calls at all."""
    path = tmp_path / "reservations.json"
    reservation = allocator.reserve_capital_for_entry(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g1",
        requested_notional=1000.0, buying_power_provider=lambda: 10_000.0,
        path=path,
    )
    assert reservation is not None
    # No engine was ever touched -- nothing to assert against a database,
    # this just needs to not raise/behave differently from before.


def test_reconcile_stale_reservations_releases_orphaned_entries(tmp_path):
    engine = _sqlite_engine(tmp_path)
    orphan = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="AAPL", attempt_group_id="g1",
        requested_notional=1000.0,
    )
    still_working = CapitalReservation.create(
        environment="PROD", account_no="1", symbol="NVDA", attempt_group_id="g2",
        requested_notional=2000.0,
    )
    capital_reservation_repository.save_reservation(engine, orphan)
    capital_reservation_repository.save_reservation(engine, still_working)

    released = capital_reservation_repository.reconcile_stale_reservations(
        engine, environment="PROD", account_no="1", open_broker_order_symbols=["NVDA"]
    )

    assert [r.symbol for r in released] == ["AAPL"]
    remaining_active = capital_reservation_repository.list_active_reservations(
        engine, environment="PROD", account_no="1"
    )
    assert [r.symbol for r in remaining_active] == ["NVDA"]


def test_list_active_reservations_returns_empty_for_no_engine():
    assert capital_reservation_repository.list_active_reservations(
        None, environment="PROD", account_no="1"
    ) == []


def test_existing_pr2_reservation_table_is_migrated_with_absence_evidence_columns(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'old-reservations.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE capital_reservations (
                    reservation_id VARCHAR(64) PRIMARY KEY,
                    environment VARCHAR(10) NOT NULL,
                    account_no VARCHAR(32) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    attempt_group_id VARCHAR(64) NOT NULL,
                    requested_notional FLOAT NOT NULL,
                    remaining_reserved_notional FLOAT NOT NULL,
                    status VARCHAR(24) NOT NULL,
                    created_at DATETIME NOT NULL,
                    released_at DATETIME NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO capital_reservations (
                    reservation_id, environment, account_no, symbol,
                    attempt_group_id, requested_notional,
                    remaining_reserved_notional, status, created_at, updated_at
                ) VALUES (
                    'R-OLD', 'PROD', '1', 'AAPL', 'G-OLD', 1000, 1000,
                    'RESERVED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    stored = capital_reservation_repository.list_active_reservations(
        engine, environment="PROD", account_no="1"
    )[0]
    assert stored.reservation_id == "R-OLD"
    assert stored.version == 1
    assert stored.absence_count == 0
    assert stored.last_absence_snapshot_id == ""
