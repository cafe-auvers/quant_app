"""Tests for src.services.execution_command_gateway.

docs/kanban_production_readiness.md, Workstream 3 (PR2).
"""
from __future__ import annotations

import sqlalchemy as sa
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool

from src.core.execution_mode import ExecutionMode, ExecutionSource
from src.core.discovered_external_order import adopt_external_order, new_discovered_external_order
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderIntent, OrderSide, OrderStatus
from src.services import execution_command_gateway as gw_module
from src.services.execution_command_gateway import (
    CancelNotPermittedError,
    ConcurrentExecutionOwnershipError,
    ExecutionCommandGateway,
    GuardedCancellationAmbiguousError,
    GuardedCancellationRejectedError,
    GuardedEngineRequiresDatabaseError,
    GuardedSubmissionAmbiguousError,
    GuardedSubmissionRejectedError,
    ReplaceNotSafeError,
    get_default_execution_gateway,
)
from src.services.capital_reservation_repository import (
    ensure_capital_reservations_table,
    list_active_reservations,
)
from src.services.execution_command_repository import (
    DuplicateCommandError,
    ExecutionCommand,
    ensure_execution_commands_table,
    get_command_by_idempotency_key,
    insert_command,
)
from src.services.execution_lease_protocol import (
    ExecutionLease,
    FakeExecutionLeaseProtocol,
    LeaseNotCurrentError,
)
from src.services.execution_order_repository import _get_execution_orders_table, fetch_execution_order, record_execution_order
from fakes.fake_execution_broker import FakeExecutionBroker


def _make_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'gateway.db'}", future=True, poolclass=NullPool)


def _guarded_gateway(tmp_path, *, lease_protocol=None):
    engine = _make_engine(tmp_path)
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(
        real_broker=broker, engine=engine, mode_override=True,
        lease_protocol=lease_protocol or FakeExecutionLeaseProtocol(),
    )
    return gateway, broker, engine


def _all_order_rows(engine):
    table = _get_execution_orders_table(sa.MetaData())
    with engine.begin() as conn:
        return conn.execute(select(table)).fetchall()


SUBMIT_KWARGS = dict(
    environment="PROD", account_no="12345678-01", symbol="AAPL", side=OrderSide.BUY,
    quantity=10, limit_price=100.0, exchange="NASD", intent=OrderIntent.ENTRY,
)


# --- mode selection -----------------------------------------------------


def test_default_gateway_resolves_legacy_compatibility_mode():
    gateway = get_default_execution_gateway()
    assert gateway._mode() == ExecutionMode.LEGACY_COMPATIBILITY


def test_legacy_compatibility_submit_is_a_transparent_passthrough():
    broker = FakeExecutionBroker()
    broker.queue_acceptance(broker_order_id="B-1")
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)

    result = gateway.submit_order(**SUBMIT_KWARGS)

    assert result.broker_order_id == "B-1"
    assert len(broker.submit_calls) == 1
    # No engine was even configured -- if this had run the guarded
    # sequence instead, it would have raised GuardedEngineRequiresDatabaseError.


def test_legacy_compatibility_cancel_is_a_transparent_passthrough():
    broker = FakeExecutionBroker()
    broker.queue_cancel_confirmed()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)

    snapshot = gateway.cancel_order(
        environment="PROD", account_no="12345678-01", symbol="AAPL",
        broker_order_id="B-1", quantity=10, side="BUY", exchange="NASD",
    )
    assert snapshot.status == OrderStatus.CANCELLED
    assert len(broker.cancel_calls) == 1


def test_guarded_engine_submit_without_an_engine_raises():
    broker = FakeExecutionBroker()
    broker.queue_acceptance()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=True)
    with pytest.raises(GuardedEngineRequiresDatabaseError):
        gateway.submit_order(**SUBMIT_KWARGS)


# --- A1/A2: atomic pre-submission transaction ----------------------------


def test_submit_commits_command_reservation_and_prepared_record_atomically(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_timeout()  # never reaches a clean broker outcome in this test
    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_order(**SUBMIT_KWARGS)

    rows = _all_order_rows(engine)
    assert len(rows) == 1
    client_order_id = rows[0].client_order_id

    fetched_order = fetch_execution_order(engine, client_order_id)
    assert fetched_order.status == ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE

    command = get_command_by_idempotency_key(engine, client_order_id)
    assert command is not None
    assert command.status == "AMBIGUOUS"

    # Ambiguous outcome: the reservation is NOT released (the order may
    # still exist at the broker).
    reservations = list_active_reservations(engine, environment="PROD", account_no="12345678-01")
    assert len(reservations) == 1


def test_a_transaction_failure_leaves_all_three_writes_absent(tmp_path, monkeypatch, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(gw_module, "insert_execution_order", _boom)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        gateway.submit_order(**SUBMIT_KWARGS)

    assert _all_order_rows(engine) == []
    reservations = list_active_reservations(engine, environment="PROD", account_no="12345678-01")
    assert reservations == []
    assert broker.submit_calls == []  # never reached the broker


def test_failed_submitting_commit_produces_zero_broker_calls(tmp_path, monkeypatch, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SUBMITTING commit failure")

    monkeypatch.setattr(gw_module, "update_execution_order", _boom)
    with pytest.raises(RuntimeError, match="simulated SUBMITTING commit failure"):
        gateway.submit_order(**SUBMIT_KWARGS)

    assert broker.submit_calls == []


def test_broker_is_never_called_if_the_lease_is_not_current(tmp_path, trading_enabled):
    lease_protocol = FakeExecutionLeaseProtocol()  # nothing granted
    gateway, broker, engine = _guarded_gateway(tmp_path, lease_protocol=lease_protocol)
    broker.queue_acceptance()

    with pytest.raises(LeaseNotCurrentError):
        gateway.submit_order(
            **SUBMIT_KWARGS, lease=ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=1)
        )
    assert broker.submit_calls == []


def test_a_stale_lease_epoch_blocks_submission(tmp_path, trading_enabled):
    lease_protocol = FakeExecutionLeaseProtocol(
        current=ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=2)
    )
    gateway, broker, engine = _guarded_gateway(tmp_path, lease_protocol=lease_protocol)
    broker.queue_acceptance()

    with pytest.raises(LeaseNotCurrentError):
        gateway.submit_order(
            **SUBMIT_KWARGS,
            lease=ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=1),  # stale epoch
        )
    assert broker.submit_calls == []


# --- explicit rejection / ambiguity ---------------------------------------


def test_explicit_rejection_reaches_rejected_and_releases_the_reservation(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_rejection(message="insufficient buying power")

    with pytest.raises(GuardedSubmissionRejectedError):
        gateway.submit_order(**SUBMIT_KWARGS)

    reservation_table = ensure_capital_reservations_table(engine)
    with engine.begin() as conn:
        reservation_rows = conn.execute(select(reservation_table)).fetchall()
    assert len(reservation_rows) == 1
    assert reservation_rows[0].status == "RELEASED"

    order_rows = _all_order_rows(engine)
    assert order_rows[0].status == ExecutionOrderStatus.REJECTED.value


def test_timeout_reaches_unknown_submission_state_and_is_not_retried(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_timeout()

    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_order(**SUBMIT_KWARGS)

    assert len(broker.submit_calls) == 1  # never retried
    order_rows = _all_order_rows(engine)
    assert order_rows[0].status == ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE.value


def test_transport_exception_is_also_treated_as_ambiguous(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_transport_exception()
    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_order(**SUBMIT_KWARGS)


def test_broker_acknowledgement_requires_and_persists_exact_identity(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-999", raw_response={"ODNO": "B-999"})

    result = gateway.submit_order(**SUBMIT_KWARGS)
    assert result.broker_order_id == "B-999"

    rows = _all_order_rows(engine)
    assert len(rows) == 1
    assert rows[0].status == ExecutionOrderStatus.ACKNOWLEDGED.value
    assert rows[0].broker_identity_status == "EXACT"
    assert rows[0].broker_order_id == "B-999"


def test_response_persistence_failure_after_acceptance_never_triggers_a_second_broker_call(tmp_path, monkeypatch, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")

    real_update = gw_module.update_execution_order
    calls = {"n": 0}

    def _fail_on_second_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            # The first call is the PREPARED -> SUBMITTING commit, which
            # must succeed (the broker call happens only after it does);
            # only the post-acceptance persist (the second call) fails.
            raise RuntimeError("simulated post-acceptance persistence failure")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(gw_module, "update_execution_order", _fail_on_second_call)
    with pytest.raises(RuntimeError, match="simulated post-acceptance persistence failure"):
        gateway.submit_order(**SUBMIT_KWARGS)

    assert len(broker.submit_calls) == 1  # the real broker call happened exactly once


def test_duplicate_idempotency_key_is_rejected_before_a_broker_call(tmp_path):
    """Simulates a restart re-attempting the exact same command journal
    entry -- insert_command's own uniqueness guarantee (A5) blocks the
    duplicate before the gateway would ever reach the broker again."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    ensure_execution_commands_table(engine)
    command = ExecutionCommand(
        idempotency_key="DUPLICATE-KEY", command_type="submit", environment="PROD",
        account_no="12345678-01", symbol="AAPL", lease_epoch=0,
    )
    with engine.begin() as conn:
        insert_command(conn, command)
    with pytest.raises(DuplicateCommandError):
        with engine.begin() as conn:
            insert_command(conn, command)
    assert broker.submit_calls == []


def test_restart_after_durable_submitting_does_not_resubmit(tmp_path, trading_enabled):
    """A record already sitting at SUBMITTING (as if a prior process
    crashed after committing SUBMITTING but before the broker responded)
    must never be re-submitted by a fresh submit_order call for the exact
    same logical order -- the duplicate idempotency_key raises first."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_timeout()
    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_order(**SUBMIT_KWARGS)
    assert len(broker.submit_calls) == 1

    rows = _all_order_rows(engine)
    client_order_id = rows[0].client_order_id
    command = get_command_by_idempotency_key(engine, client_order_id)
    ensure_execution_commands_table(engine)
    with pytest.raises(DuplicateCommandError):
        with engine.begin() as conn:
            insert_command(conn, command)
    # Still exactly one broker call from the original attempt.
    assert len(broker.submit_calls) == 1


# --- cancellation -----------------------------------------------------------


def _submit_and_acknowledge(gateway, broker, **overrides):
    kwargs = dict(SUBMIT_KWARGS)
    kwargs.update(overrides)
    broker.queue_acceptance(broker_order_id="B-1")
    gateway.submit_order(**kwargs)
    rows = _all_order_rows(gateway._engine)
    return rows[-1].client_order_id


def test_cancel_requires_exact_identity(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_timeout()  # leaves the record UNKNOWN_SUBMISSION_STATE, never EXACT
    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_order(**SUBMIT_KWARGS)

    client_order_id = _all_order_rows(engine)[0].client_order_id
    with pytest.raises(CancelNotPermittedError):
        gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=client_order_id)
    assert broker.cancel_calls == []


def test_cancel_confirmed_reaches_cancelled(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()

    gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=client_order_id)

    record = fetch_execution_order(engine, client_order_id)
    assert record.status == ExecutionOrderStatus.CANCELLED


def test_cancel_timeout_leaves_cancel_pending_with_discovering_recovery_state_and_is_not_retried(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_timeout()

    with pytest.raises(GuardedCancellationAmbiguousError):
        gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=client_order_id)

    record = fetch_execution_order(engine, client_order_id)
    assert record.status == ExecutionOrderStatus.CANCEL_PENDING
    assert record.recovery_state == OrderRecoveryState.DISCOVERING
    assert len(broker.cancel_calls) == 1


def test_cancel_explicit_rejection_returns_the_order_to_working(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_rejected()

    with pytest.raises(GuardedCancellationRejectedError):
        gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=client_order_id)

    record = fetch_execution_order(engine, client_order_id)
    assert record.status == ExecutionOrderStatus.WORKING


def test_a_fill_racing_the_cancel_is_reflected_not_ignored(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_fill_race(filled_quantity=10, quantity_requested=10)

    gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=client_order_id)

    record = fetch_execution_order(engine, client_order_id)
    assert record.status == ExecutionOrderStatus.FILLED
    assert record.filled_quantity == 10


def test_adopted_cancel_requires_explicit_permission(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    ext = new_discovered_external_order(
        environment="PROD", account_no="12345678-01", symbol="MSFT", side=OrderSide.BUY,
        broker_order_id="B-EXT-1", quantity_requested=5,
    )
    record = adopt_external_order(ext, adopted_by="tony", permissions=frozenset())  # no CANCEL granted
    record_execution_order(engine, record)

    with pytest.raises(CancelNotPermittedError):
        gateway.cancel_order(
            environment="PROD", account_no="12345678-01", client_order_id=record.client_order_id
        )
    assert broker.cancel_calls == []


def test_adopted_cancel_succeeds_with_explicit_permission(tmp_path):
    from src.core.execution_order_record import AdoptedOrderPermission

    gateway, broker, engine = _guarded_gateway(tmp_path)
    ext = new_discovered_external_order(
        environment="PROD", account_no="12345678-01", symbol="MSFT", side=OrderSide.BUY,
        broker_order_id="B-EXT-1", quantity_requested=5,
    )
    record = adopt_external_order(
        ext, adopted_by="tony", permissions=frozenset({AdoptedOrderPermission.CANCEL})
    )
    record_execution_order(engine, record)
    broker.queue_cancel_confirmed()

    gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=record.client_order_id)

    refreshed = fetch_execution_order(engine, record.client_order_id)
    assert refreshed.status == ExecutionOrderStatus.CANCELLED


def test_a_duplicate_cancel_command_is_rejected(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()
    gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=client_order_id)

    # A second cancel attempt on the now-terminal order is rejected by
    # is_cancellable's own status check before any broker call.
    with pytest.raises(CancelNotPermittedError):
        gateway.cancel_order(environment="PROD", account_no="12345678-01", client_order_id=client_order_id)
    assert len(broker.cancel_calls) == 1


# --- replace ----------------------------------------------------------------


def test_replace_preserves_the_original_order_and_creates_a_linked_new_record(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()
    broker.queue_acceptance(broker_order_id="B-2")

    result = gateway.replace_order(client_order_id=client_order_id, new_quantity=5, new_limit_price=101.0)
    assert result.broker_order_id == "B-2"

    original = fetch_execution_order(engine, client_order_id)
    assert original.status == ExecutionOrderStatus.CANCELLED
    assert original.submitted_quantity == 10  # never mutated into the replacement

    rows = _all_order_rows(engine)
    assert len(rows) == 2
    new_row = [r for r in rows if r.client_order_id != client_order_id][0]
    assert new_row.status == ExecutionOrderStatus.ACKNOWLEDGED.value

    new_record = fetch_execution_order(engine, new_row.client_order_id)
    assert new_record.replaces_execution_order_id == client_order_id
    assert new_record.submitted_quantity == 5


def test_replace_propagates_an_ambiguous_cancel_outcome_without_submitting_a_replacement(tmp_path, trading_enabled):
    """An ambiguous cancel (timeout/transport loss) during replace is not
    reinterpreted as a generic "not safe" -- the specific
    GuardedCancellationAmbiguousError propagates unchanged, same as a
    standalone cancel_order call would raise, and no replacement is ever
    submitted."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_timeout()

    with pytest.raises(GuardedCancellationAmbiguousError):
        gateway.replace_order(client_order_id=client_order_id, new_quantity=5, new_limit_price=101.0)
    assert len(broker.submit_calls) == 1  # only the original submission -- no replacement submitted

    record = fetch_execution_order(engine, client_order_id)
    assert record.status == ExecutionOrderStatus.CANCEL_PENDING
    assert record.recovery_state == OrderRecoveryState.DISCOVERING


def test_replace_refuses_when_a_fill_races_the_cancel(tmp_path, trading_enabled):
    """The cancel call itself completes cleanly (no exception) but the
    broker's answer is "it already filled" -- a real, non-ambiguous
    outcome that is nonetheless unsafe to replace: ReplaceNotSafeError,
    and no replacement is submitted."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    client_order_id = _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_fill_race(filled_quantity=10, quantity_requested=10)

    with pytest.raises(ReplaceNotSafeError):
        gateway.replace_order(client_order_id=client_order_id, new_quantity=5, new_limit_price=101.0)
    assert len(broker.submit_calls) == 1  # only the original submission -- no replacement submitted

    record = fetch_execution_order(engine, client_order_id)
    assert record.status == ExecutionOrderStatus.FILLED


def test_replace_is_not_available_in_legacy_compatibility_mode():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    with pytest.raises(NotImplementedError):
        gateway.replace_order(client_order_id="X", new_quantity=1, new_limit_price=1.0)


# --- Workstream 9: mutual exclusion / source attribution --------------------


def test_two_different_sources_cannot_concurrently_hold_the_same_account_symbol():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    key = ("PROD", "12345678-01", "AAPL")
    with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
        with pytest.raises(ConcurrentExecutionOwnershipError):
            with gateway._ownership.claim(key, ExecutionSource.KANBAN_BOARD):
                pass


def test_the_same_source_can_reclaim_reentrantly():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    key = ("PROD", "12345678-01", "AAPL")
    with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
        with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
            pass  # must not raise


def test_the_ownership_claim_is_released_after_the_call_completes():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    key = ("PROD", "12345678-01", "AAPL")
    with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
        pass
    with gateway._ownership.claim(key, ExecutionSource.KANBAN_BOARD):
        pass  # must not raise -- the first claim already released


def test_submitted_commands_record_the_issuing_source(tmp_path, trading_enabled):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")
    gateway.submit_order(**SUBMIT_KWARGS, source=ExecutionSource.KANBAN_BOARD)

    client_order_id = _all_order_rows(engine)[0].client_order_id
    command = get_command_by_idempotency_key(engine, client_order_id)
    assert command.source == ExecutionSource.KANBAN_BOARD.value
