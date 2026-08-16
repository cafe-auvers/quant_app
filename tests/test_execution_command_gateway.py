"""Tests for src.services.execution_command_gateway.

docs/kanban_production_readiness.md, Workstream 3 (PR2), second review
pass. ``submit_order``/``cancel_order`` (the Broker-protocol methods) are
LEGACY_COMPATIBILITY-only; ``submit_guarded``/``cancel_guarded``/
``replace_guarded`` (taking explicit request models from
src.core.execution_request) are GUARDED_ENGINE-only. See the gateway
module's own docstring for why these are not interchangeable.
"""
from __future__ import annotations

import sqlalchemy as sa
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool

from src.core.execution_mode import ExecutionLease, ExecutionMode, ExecutionSource
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.execution_order_record import AdoptedOrderPermission, ExecutionOrderStatus
from src.core.discovered_external_order import (
    ExternalOrderDisposition,
    adopt_external_order,
    new_discovered_external_order,
)
from src.core.execution_request import CancelExecutionRequest, ReplaceExecutionRequest, SubmitExecutionRequest
from src.core.order_recovery_state import OrderRecoveryState
from src.core.order_state import OrderIntent, OrderSide, OrderStatus
from src.services import execution_command_gateway as gw_module
from src.services.execution_command_gateway import (
    AmbiguousPostBrokerPersistenceError,
    CancelNotPermittedError,
    ConcurrentExecutionOwnershipError,
    ExecutionCommandGateway,
    ExecutionOwnershipMismatchError,
    GuardedCancellationAmbiguousError,
    GuardedCancellationPreBrokerAbortedError,
    GuardedCancellationRejectedError,
    GuardedEngineRequiresDatabaseError,
    GuardedEngineRequiresMutationBudgetError,
    GuardedSubmissionAmbiguousError,
    GuardedSubmissionPreBrokerAbortedError,
    GuardedSubmissionRejectedError,
    LeaseNotVerifiedError,
    ReplaceNotSafeError,
    WrongGatewayModeError,
    build_guarded_execution_gateway,
    get_default_execution_gateway,
)
from src.services.capital_reservation_repository import (
    ensure_capital_reservations_table,
    fetch_reservation,
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
    DefaultExecutionLeaseProtocol,
    FakeExecutionLeaseProtocol,
)
from src.services import state_sync
from src.services.execution_order_repository import (
    _get_execution_orders_table,
    fetch_execution_order,
    record_execution_order,
    save_execution_order,
)
from src.services.execution_ownership_repository import assign_ownership
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from src.services.discovered_external_order_repository import (
    ActiveExecutionOrderAdoptionConflictError,
    ActiveExternalOrderFenceError,
    adopt_external_order_in_db,
    list_discovered_external_orders_for_account,
    record_discovered_external_order,
    save_discovered_external_order,
)
from fakes.fake_execution_broker import FakeExecutionBroker


def _make_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'gateway.db'}", future=True, poolclass=NullPool)


def _lease(epoch=1, *, verified=True):
    protocol = FakeExecutionLeaseProtocol(
        current=ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=epoch),
        epoch_verified=verified,
    )
    handle = ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=epoch)
    return protocol, handle


def _guarded_gateway(tmp_path, *, lease_protocol=None, mutation_budget=None):
    engine = _make_engine(tmp_path)
    broker = FakeExecutionBroker()
    protocol, _ = _lease()
    gateway = ExecutionCommandGateway(
        real_broker=broker, engine=engine, mode_override=True,
        lease_protocol=lease_protocol or protocol,
        mutation_budget=mutation_budget or AllowAllMutationBudget(),
        buying_power_provider=lambda environment, account_no: 100_000.0,
    )
    return gateway, broker, engine


def _all_order_rows(engine):
    table = _get_execution_orders_table(sa.MetaData())
    with engine.begin() as conn:
        return conn.execute(select(table)).fetchall()


def _submit_request(**overrides):
    _, lease = _lease()
    fields = dict(
        client_order_id="CID-1", environment="PROD", account_no="12345678-01", symbol="AAPL",
        side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0, exchange="NASD",
        lease=lease, source=ExecutionSource.SYSTEM,
    )
    fields.update(overrides)
    return SubmitExecutionRequest(**fields)


def _record_active_external_order(engine, *, broker_order_id="B-EXTERNAL"):
    return record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id=broker_order_id,
            broker_status=ExecutionOrderStatus.WORKING,
        ),
    )


def _resolve_external_fence(engine, external_order):
    expected_version = external_order.version
    external_order.broker_status = ExecutionOrderStatus.CANCELLED
    external_order.disposition = ExternalOrderDisposition.DISMISSED_TERMINAL
    save_discovered_external_order(
        engine, external_order, expected_version=expected_version
    )


# --- mode selection / API split (findings 2) ---------------------------


def test_default_gateway_resolves_legacy_compatibility_mode():
    gateway = get_default_execution_gateway()
    assert gateway.mode == ExecutionMode.LEGACY_COMPATIBILITY


@pytest.mark.usefixtures("trading_enabled")
def test_active_unowned_external_order_fences_guarded_submit(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _record_active_external_order(engine)
    broker.queue_acceptance(broker_order_id="SHOULD-NOT-BE-USED")

    with pytest.raises(ActiveExternalOrderFenceError):
        gateway.submit_guarded(_submit_request())

    assert broker.submit_calls == []
    assert get_command_by_idempotency_key(engine, "SUBMIT:CID-1") is None


@pytest.mark.usefixtures("trading_enabled")
def test_active_unowned_external_order_fences_guarded_cancel_and_replace(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-OWNED")
    owned = gateway.submit_guarded(_submit_request())
    _record_active_external_order(engine)
    _, lease = _lease()

    broker.queue_cancel_confirmed()
    with pytest.raises(ActiveExternalOrderFenceError):
        gateway.cancel_guarded(
            CancelExecutionRequest(
                client_order_id=owned.client_order_id,
                cancel_command_id="CANCEL-FENCED",
                environment="PROD",
                account_no="12345678-01",
                lease=lease,
                source=ExecutionSource.SYSTEM,
            )
        )
    with pytest.raises(ActiveExternalOrderFenceError):
        gateway.replace_guarded(
            ReplaceExecutionRequest(
                client_order_id=owned.client_order_id,
                replace_command_id="REPLACE-FENCED",
                new_client_order_id="CID-REPLACEMENT",
                new_quantity=5,
                new_limit_price=99.0,
                environment="PROD",
                account_no="12345678-01",
                lease=lease,
                source=ExecutionSource.SYSTEM,
            )
        )

    assert broker.cancel_calls == []
    assert get_command_by_idempotency_key(engine, "CANCEL:CANCEL-FENCED") is None
    assert get_command_by_idempotency_key(engine, "REPLACE:REPLACE-FENCED") is None


@pytest.mark.usefixtures("trading_enabled")
def test_active_adoption_retains_fence_until_terminal_reconciliation(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    external = _record_active_external_order(engine)
    adopted = adopt_external_order_in_db(
        engine,
        external.external_order_id,
        adopted_by="operator",
    )
    broker.queue_acceptance(broker_order_id="B-NEW")

    with pytest.raises(ActiveExternalOrderFenceError):
        gateway.submit_guarded(_submit_request())

    assert broker.submit_calls == []
    adopted.status = ExecutionOrderStatus.CANCELLED
    save_execution_order(engine, adopted, expected_version=adopted.version)
    result = gateway.submit_guarded(_submit_request(client_order_id="CID-2"))

    assert result.broker_order_id == "B-NEW"
    assert len(broker.submit_calls) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_active_adopted_buy_fences_automatic_protective_sell(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    external = _record_active_external_order(engine)
    adopt_external_order_in_db(
        engine, external.external_order_id, adopted_by="operator"
    )
    broker.queue_acceptance(broker_order_id="SELL-MUST-NOT-REACH-BROKER")

    with pytest.raises(ActiveExternalOrderFenceError):
        gateway.submit_guarded(
            _submit_request(
                client_order_id="STOP-SELL-1",
                side=OrderSide.SELL,
                intent=OrderIntent.STOP_LOSS,
                quantity=100,
                limit_price=94.0,
                emergency=True,
            )
        )

    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_terminal_external_audit_row_does_not_fence_submission(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id="B-TERMINAL-EXTERNAL",
            broker_status=ExecutionOrderStatus.CANCELLED,
        ),
    )
    broker.queue_acceptance(broker_order_id="B-NEW")

    result = gateway.submit_guarded(_submit_request())

    assert result.broker_order_id == "B-NEW"
    assert len(broker.submit_calls) == 1


def test_legacy_compatibility_submit_is_a_transparent_passthrough():
    broker = FakeExecutionBroker()
    broker.queue_acceptance(broker_order_id="B-1")
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)

    result = gateway.submit_order(
        environment="PROD", account_no="12345678-01", symbol="AAPL", side=OrderSide.BUY,
        quantity=10, limit_price=100.0,
    )
    assert result.broker_order_id == "B-1"
    assert len(broker.submit_calls) == 1


def test_legacy_compatibility_cancel_is_a_transparent_passthrough():
    broker = FakeExecutionBroker()
    broker.queue_cancel_confirmed()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)

    snapshot = gateway.cancel_order(
        environment="PROD", account_no="12345678-01", symbol="AAPL",
        broker_order_id="B-1", quantity=10, side="BUY", exchange="NASD",
    )
    assert snapshot.status == OrderStatus.CANCELLED


def test_submit_order_raises_in_guarded_engine_mode(tmp_path):
    """submit_order()/cancel_order() are LEGACY_COMPATIBILITY-only --
    calling them while GUARDED_ENGINE is active is a caller bug, not a
    silently-degraded guarded submission (finding 2)."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    with pytest.raises(WrongGatewayModeError):
        gateway.submit_order(
            environment="PROD", account_no="12345678-01", symbol="AAPL", side=OrderSide.BUY,
            quantity=10, limit_price=100.0,
        )
    assert broker.submit_calls == []


def test_cancel_order_raises_in_guarded_engine_mode(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    with pytest.raises(WrongGatewayModeError):
        gateway.cancel_order(environment="PROD", account_no="12345678-01", symbol="AAPL")
    assert broker.cancel_calls == []


def test_submit_guarded_raises_in_legacy_compatibility_mode():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    with pytest.raises(WrongGatewayModeError):
        gateway.submit_guarded(_submit_request())


def test_guarded_engine_submit_without_an_engine_raises():
    broker = FakeExecutionBroker()
    broker.queue_acceptance()
    protocol, _ = _lease()
    gateway = ExecutionCommandGateway(
        real_broker=broker, mode_override=True, lease_protocol=protocol, mutation_budget=AllowAllMutationBudget()
    )
    with pytest.raises(GuardedEngineRequiresDatabaseError):
        gateway.submit_guarded(_submit_request())


def test_guarded_engine_submit_without_a_mutation_budget_raises(tmp_path):
    engine = _make_engine(tmp_path)
    broker = FakeExecutionBroker()
    broker.queue_acceptance()
    protocol, _ = _lease()
    gateway = ExecutionCommandGateway(
        real_broker=broker, engine=engine, mode_override=True, lease_protocol=protocol, mutation_budget=None
    )
    with pytest.raises(GuardedEngineRequiresMutationBudgetError):
        gateway.submit_guarded(_submit_request())


def test_build_guarded_execution_gateway_requires_every_dependency(tmp_path):
    engine = _make_engine(tmp_path)
    protocol, _ = _lease()
    with pytest.raises(TypeError):
        build_guarded_execution_gateway()  # missing everything
    with pytest.raises(gw_module.GuardedEngineRequiresDatabaseError):
        build_guarded_execution_gateway(
            engine=None,
            lease_protocol=protocol,
            mutation_budget=AllowAllMutationBudget(),
            buying_power_provider=lambda environment, account_no: 100_000.0,
        )


# --- A1/A2: atomic pre-submission transaction ----------------------------


@pytest.mark.usefixtures("trading_enabled")
def test_submit_commits_command_reservation_and_prepared_record_atomically(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_timeout()
    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_guarded(_submit_request())

    rows = _all_order_rows(engine)
    assert len(rows) == 1
    client_order_id = rows[0].client_order_id
    assert client_order_id == "CID-1"

    fetched_order = fetch_execution_order(engine, client_order_id)
    assert fetched_order.status == ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE

    command = get_command_by_idempotency_key(engine, f"SUBMIT:{client_order_id}")
    assert command is not None
    assert command.status == "AMBIGUOUS"

    reservations = list_active_reservations(engine, environment="PROD", account_no="12345678-01")
    assert len(reservations) == 1  # ambiguous outcome: not released


@pytest.mark.usefixtures("trading_enabled")
def test_a_transaction_failure_leaves_all_three_writes_absent(tmp_path, monkeypatch):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(gw_module, "insert_execution_order", _boom)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        gateway.submit_guarded(_submit_request())

    assert _all_order_rows(engine) == []
    assert list_active_reservations(engine, environment="PROD", account_no="12345678-01") == []
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_failed_submitting_commit_produces_zero_broker_calls(tmp_path, monkeypatch):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SUBMITTING commit failure")

    monkeypatch.setattr(gw_module, "update_execution_order", _boom)
    with pytest.raises(RuntimeError, match="simulated SUBMITTING commit failure"):
        gateway.submit_guarded(_submit_request())
    assert broker.submit_calls == []


# --- finding 1: caller-stable idempotency across a fresh gateway instance ---


@pytest.mark.usefixtures("trading_enabled")
def test_replaying_the_same_stable_client_order_id_on_a_fresh_gateway_makes_zero_additional_broker_calls(tmp_path):
    engine = _make_engine(tmp_path)
    first_broker = FakeExecutionBroker()
    first_broker.queue_timeout()
    protocol, lease = _lease()
    first_gateway = ExecutionCommandGateway(
        real_broker=first_broker, engine=engine, mode_override=True,
        lease_protocol=protocol, mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda environment, account_no: 100_000.0,
    )
    request = _submit_request(client_order_id="STABLE-ID-1", lease=lease)
    with pytest.raises(GuardedSubmissionAmbiguousError):
        first_gateway.submit_guarded(request)
    assert len(first_broker.submit_calls) == 1

    # A brand-new gateway instance (as if the process restarted) replays
    # the exact same logical submission using the same stable identity.
    second_broker = FakeExecutionBroker()
    second_broker.queue_acceptance(broker_order_id="SHOULD-NEVER-BE-USED")
    second_gateway = ExecutionCommandGateway(
        real_broker=second_broker, engine=engine, mode_override=True,
        lease_protocol=protocol, mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda environment, account_no: 100_000.0,
    )
    with pytest.raises(DuplicateCommandError):
        second_gateway.submit_guarded(request)
    assert second_broker.submit_calls == []  # zero additional broker calls


# --- explicit rejection / ambiguity ---------------------------------------


@pytest.mark.usefixtures("trading_enabled")
def test_explicit_rejection_reaches_rejected_and_releases_the_reservation(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_rejection(message="insufficient buying power")

    with pytest.raises(GuardedSubmissionRejectedError):
        gateway.submit_guarded(_submit_request())

    reservation_table = ensure_capital_reservations_table(engine)
    with engine.begin() as conn:
        reservation_rows = conn.execute(select(reservation_table)).fetchall()
    assert reservation_rows[0].status == "RELEASED"
    assert _all_order_rows(engine)[0].status == ExecutionOrderStatus.REJECTED.value


@pytest.mark.usefixtures("trading_enabled")
def test_timeout_reaches_unknown_submission_state_and_is_not_retried(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_timeout()
    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_guarded(_submit_request())
    assert len(broker.submit_calls) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_broker_acknowledgement_requires_and_persists_exact_identity(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-999", raw_response={"ODNO": "B-999"})

    record = gateway.submit_guarded(_submit_request())
    assert record.broker_order_id == "B-999"

    rows = _all_order_rows(engine)
    assert rows[0].status == ExecutionOrderStatus.ACKNOWLEDGED.value
    assert rows[0].broker_identity_status == "EXACT"


# --- finding 11: SELL exits don't reserve buying-power notional ------------


@pytest.mark.usefixtures("trading_enabled")
def test_a_buy_entry_reserves_notional_capital(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()
    gateway.submit_guarded(_submit_request(side=OrderSide.BUY, quantity=10, limit_price=100.0))

    reservations = list_active_reservations(engine, environment="PROD", account_no="12345678-01")
    assert reservations[0].requested_notional == pytest.approx(1000.0)


@pytest.mark.usefixtures("trading_enabled")
def test_a_sell_exit_reserves_zero_notional_capital(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()
    gateway.submit_guarded(
        _submit_request(side=OrderSide.SELL, intent=OrderIntent.MANUAL_EXIT, quantity=10, limit_price=100.0)
    )

    reservations = list_active_reservations(engine, environment="PROD", account_no="12345678-01")
    assert reservations[0].requested_notional == pytest.approx(0.0)


# --- finding 3: post-broker persistence failure is never a rejection -------


@pytest.mark.usefixtures("trading_enabled")
def test_persistence_failure_after_acceptance_raises_ambiguous_not_a_bare_db_error(tmp_path, monkeypatch):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")

    real_update = gw_module.update_execution_order
    calls = {"n": 0}

    def _fail_on_second_call(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # first call is PREPARED->SUBMITTING; must succeed
            raise RuntimeError("simulated post-acceptance persistence failure")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(gw_module, "update_execution_order", _fail_on_second_call)
    with pytest.raises(AmbiguousPostBrokerPersistenceError) as exc_info:
        gateway.submit_guarded(_submit_request())

    assert exc_info.value.broker_order_id == "B-1"
    assert len(broker.submit_calls) == 1  # the real broker call happened exactly once, never retried

    # The durable record was NOT confirmed updated -- still SUBMITTING.
    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.SUBMITTING

    # A caller must never resubmit -- the stable identity still blocks a replay.
    with pytest.raises(DuplicateCommandError):
        gateway.submit_guarded(_submit_request())


@pytest.mark.usefixtures("trading_enabled")
def test_persistence_failure_after_a_broker_rejection_also_raises_ambiguous(tmp_path, monkeypatch):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_rejection()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure-persistence failure")

    monkeypatch.setattr(gw_module, "update_command_response", _boom)
    with pytest.raises(AmbiguousPostBrokerPersistenceError):
        gateway.submit_guarded(_submit_request())


# --- finding 4: guarded lease gate never fails open -------------------------


@pytest.mark.usefixtures("trading_enabled")
def test_a_missing_lease_is_rejected_in_guarded_mode(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()
    with pytest.raises(LeaseNotVerifiedError):
        gateway.submit_guarded(_submit_request(lease=None))
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_an_unverified_epoch_is_rejected_even_with_a_matching_lease(tmp_path):
    protocol, lease = _lease(verified=False)  # epoch_verified=False
    gateway, broker, engine = _guarded_gateway(tmp_path, lease_protocol=protocol)
    broker.queue_acceptance()
    with pytest.raises(LeaseNotVerifiedError):
        gateway.submit_guarded(_submit_request(lease=lease))
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_default_lease_protocol_rejects_when_no_authoritative_lease_exists(tmp_path):
    engine = _make_engine(tmp_path)
    broker = FakeExecutionBroker()
    broker.queue_acceptance()
    gateway = ExecutionCommandGateway(
        real_broker=broker, engine=engine, mode_override=True,
        lease_protocol=DefaultExecutionLeaseProtocol(engine=engine), mutation_budget=AllowAllMutationBudget(),
    )
    _, lease = _lease()
    with pytest.raises(LeaseNotVerifiedError):
        gateway.submit_guarded(_submit_request(lease=lease))


@pytest.mark.usefixtures("trading_enabled")
def test_losing_device_cannot_submit_after_lease_loss(tmp_path):
    engine = _make_engine(tmp_path)
    first_role = state_sync.LocalDeviceRole("dev-1", "LAPTOP", True)
    claimed = state_sync.claim_main_device(engine, first_role)
    assert claimed.success
    lease = ExecutionLease(
        device_id=claimed.main_device.device_id,
        lease_token=claimed.main_device.lease_token,
        lease_epoch=claimed.main_device.lease_epoch,
    )
    broker = FakeExecutionBroker()
    broker.queue_acceptance(broker_order_id="B-FIRST")
    gateway = ExecutionCommandGateway(
        real_broker=broker,
        engine=engine,
        mode_override=True,
        lease_protocol=DefaultExecutionLeaseProtocol(engine=engine),
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda *_: 100_000.0,
    )
    first = gateway.submit_guarded(_submit_request(lease=lease))
    assert first.broker_order_id == "B-FIRST"

    second_role = state_sync.LocalDeviceRole("dev-2", "PC", True)
    replacement = state_sync.claim_main_device(engine, second_role)
    assert replacement.success
    assert replacement.main_device.lease_epoch > lease.lease_epoch
    broker.queue_acceptance(broker_order_id="B-MUST-NOT-HAPPEN")
    with pytest.raises(LeaseNotVerifiedError):
        gateway.submit_guarded(
            _submit_request(client_order_id="CID-2", lease=lease)
        )
    assert len(broker.submit_calls) == 1


def test_legacy_monitor_cannot_act_on_a_kanban_owned_symbol(tmp_path):
    engine = _make_engine(tmp_path)
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(
        real_broker=broker,
        engine=engine,
        mode_override=False,
    )
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="buyboard-orb-v1",
            assigned_by="test",
        ),
    )
    broker.queue_acceptance(broker_order_id="MUST-NOT-HAPPEN")

    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.submit_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            side=OrderSide.SELL,
            quantity=1,
            limit_price=99.0,
            source=ExecutionSource.LEGACY_BUY_DASHBOARD,
        )

    broker.queue_cancel_confirmed()
    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.cancel_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            broker_order_id="B-1",
            quantity=1,
            side="SELL",
            source=ExecutionSource.LEGACY_BUY_DASHBOARD,
        )

    assert broker.submit_calls == []
    assert broker.cancel_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_a_stale_lease_epoch_blocks_submission(tmp_path):
    protocol = FakeExecutionLeaseProtocol(
        current=ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=2)
    )
    gateway, broker, engine = _guarded_gateway(tmp_path, lease_protocol=protocol)
    broker.queue_acceptance()
    stale_lease = ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=1)
    with pytest.raises(LeaseNotVerifiedError):
        gateway.submit_guarded(_submit_request(lease=stale_lease))
    assert broker.submit_calls == []


# --- finding 9: mutation budget gate ----------------------------------------


@pytest.mark.usefixtures("trading_enabled")
def test_mutation_budget_exhaustion_blocks_submission(tmp_path):
    from src.services.mutation_budget_protocol import MutationBudgetExceededError

    class _NoBudget:
        def require_available(self, command_type):
            raise MutationBudgetExceededError("no budget remaining")

    gateway, broker, engine = _guarded_gateway(tmp_path, mutation_budget=_NoBudget())
    broker.queue_acceptance()
    with pytest.raises(MutationBudgetExceededError):
        gateway.submit_guarded(_submit_request())
    assert broker.submit_calls == []


# --- finding 5: H1 persisted execution ownership ----------------------------


@pytest.mark.usefixtures("trading_enabled")
def test_manual_owned_symbol_rejects_every_source(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    assign_ownership(
        engine,
        ExecutionOwnership(environment="PROD", account_no="12345678-01", symbol="AAPL", owner=ExecutionOwner.MANUAL),
    )
    broker.queue_acceptance()
    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.submit_guarded(_submit_request(source=ExecutionSource.LEGACY_BUY_DASHBOARD))
    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.submit_guarded(_submit_request(client_order_id="CID-2", source=ExecutionSource.KANBAN_BOARD))
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_kanban_owned_symbol_rejects_legacy_source(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="12345678-01", symbol="AAPL", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="orb-1",
        ),
    )
    broker.queue_acceptance()
    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.submit_guarded(_submit_request(source=ExecutionSource.LEGACY_BUY_DASHBOARD))
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_kanban_owned_symbol_accepts_kanban_source(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="12345678-01", symbol="AAPL", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="orb-1",
        ),
    )
    broker.queue_acceptance()
    record = gateway.submit_guarded(
        _submit_request(source=ExecutionSource.KANBAN_BOARD, strategy_instance_id="orb-1")
    )
    assert record.status == ExecutionOrderStatus.ACKNOWLEDGED


@pytest.mark.usefixtures("trading_enabled")
def test_kanban_owned_symbol_rejects_a_blank_strategy_instance_id(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="12345678-01", symbol="AAPL", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="orb-1",
        ),
    )
    broker.queue_acceptance()
    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.submit_guarded(_submit_request(source=ExecutionSource.KANBAN_BOARD))  # no strategy_instance_id
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_kanban_owned_symbol_rejects_a_different_strategy_instance_id(tmp_path):
    """Finding 3 (third pass): H1 is 'KANBAN plus strategy_instance_id' --
    one Kanban strategy instance must never be able to act on a symbol
    assigned to a different one."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="12345678-01", symbol="AAPL", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="orb-1",
        ),
    )
    broker.queue_acceptance()
    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.submit_guarded(
            _submit_request(source=ExecutionSource.KANBAN_BOARD, strategy_instance_id="some-other-strategy")
        )
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_unassigned_symbol_defaults_legacy_and_rejects_kanban(tmp_path):
    """H2: "Unassigned defaults closed to Kanban.\""""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()
    with pytest.raises(ExecutionOwnershipMismatchError):
        gateway.submit_guarded(_submit_request(source=ExecutionSource.KANBAN_BOARD))
    assert broker.submit_calls == []

    # LEGACY_BUY_DASHBOARD and unattributed SYSTEM are both fine against
    # the unassigned/LEGACY default.
    record = gateway.submit_guarded(_submit_request(source=ExecutionSource.LEGACY_BUY_DASHBOARD))
    assert record.status == ExecutionOrderStatus.ACKNOWLEDGED


# --- finding 7: last-instant re-fencing immediately before the broker call --


@pytest.mark.usefixtures("trading_enabled")
def test_an_ownership_transfer_between_the_initial_check_and_the_broker_call_blocks_submission(tmp_path, monkeypatch):
    """Simulates another device reassigning ownership away from this
    source in the window between the gateway's initial B2 check and the
    actual broker call -- the last-instant re-check (finding 7, third
    pass) must catch it, not just the earlier one."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance()

    real_get_ownership = gw_module.get_ownership
    calls = {"n": 0}

    def _ownership_changes_after_first_check(engine_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get_ownership(engine_arg, **kwargs)
        # Second call (the last-instant re-check): ownership has been
        # transferred to MANUAL in the interim.
        return ExecutionOwnership(**{**kwargs, "owner": ExecutionOwner.MANUAL})

    monkeypatch.setattr(gw_module, "get_ownership", _ownership_changes_after_first_check)

    with pytest.raises(GuardedSubmissionPreBrokerAbortedError):
        gateway.submit_guarded(_submit_request())
    assert broker.submit_calls == []  # the broker was never actually reached
    assert calls["n"] == 2  # both the initial check and the re-check ran


@pytest.mark.usefixtures("trading_enabled")
def test_a_lease_epoch_advance_between_the_initial_check_and_the_broker_call_blocks_submission(tmp_path):
    """Simulates another device's handoff advancing the lease epoch in the
    window between the initial check and the actual broker call."""

    class _EpochAdvancesAfterFirstCall:
        epoch_verified = True

        def __init__(self):
            self.calls = 0

        def require_current(self, lease):
            self.calls += 1
            if self.calls >= 2:
                raise gw_module.LeaseNotCurrentError("epoch advanced by another device")

    lease_protocol = _EpochAdvancesAfterFirstCall()
    gateway, broker, engine = _guarded_gateway(tmp_path, lease_protocol=lease_protocol)
    broker.queue_acceptance()

    with pytest.raises(GuardedSubmissionPreBrokerAbortedError):
        gateway.submit_guarded(_submit_request())
    assert broker.submit_calls == []
    assert lease_protocol.calls == 2


@pytest.mark.usefixtures("trading_enabled")
def test_external_fence_inserted_after_submitting_commit_blocks_broker_submit(
    tmp_path, monkeypatch
):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="SHOULD-NOT-BE-USED")
    real_require_lease = gateway._require_verified_lease
    calls = {"n": 0}

    def insert_fence_on_final_check(lease):
        calls["n"] += 1
        real_require_lease(lease)
        if calls["n"] == 2:
            _record_active_external_order(engine, broker_order_id="B-RACED-SUBMIT")

    monkeypatch.setattr(
        gateway, "_require_verified_lease", insert_fence_on_final_check
    )

    with pytest.raises(GuardedSubmissionPreBrokerAbortedError):
        gateway.submit_guarded(_submit_request())

    assert broker.submit_calls == []
    aborted = fetch_execution_order(engine, "CID-1")
    assert aborted.status == ExecutionOrderStatus.CANCELLED_LOCALLY
    assert aborted.broker_order_id == ""
    assert get_command_by_idempotency_key(engine, "SUBMIT:CID-1").status == "PRE_BROKER_ABORTED"
    reservation = fetch_reservation(engine, aborted.capital_reservation_id)
    assert reservation is not None
    assert not reservation.is_open()

    external = next(
        order
        for order in list_discovered_external_orders_for_account(
            engine, environment="PROD", account_no="12345678-01"
        )
        if order.broker_order_id == "B-RACED-SUBMIT"
    )
    _resolve_external_fence(engine, external)
    fresh = gateway.submit_guarded(_submit_request(client_order_id="CID-2"))
    assert fresh.status == ExecutionOrderStatus.ACKNOWLEDGED
    assert fresh.broker_order_id == "SHOULD-NOT-BE-USED"
    assert len(broker.submit_calls) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_adoption_racing_sell_all_cannot_pass_the_final_broker_fence(
    tmp_path, monkeypatch
):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="SELL-MUST-NOT-BE-USED")
    real_require_lease = gateway._require_verified_lease
    calls = {"n": 0}
    adoption_conflicts = []

    def adopt_on_final_check(lease):
        calls["n"] += 1
        real_require_lease(lease)
        if calls["n"] == 2:
            external = _record_active_external_order(
                engine, broker_order_id="B-RACED-ADOPTION"
            )
            try:
                adopt_external_order_in_db(
                    engine, external.external_order_id, adopted_by="operator"
                )
            except ActiveExecutionOrderAdoptionConflictError as exc:
                adoption_conflicts.append(str(exc))

    monkeypatch.setattr(gateway, "_require_verified_lease", adopt_on_final_check)

    with pytest.raises(GuardedSubmissionPreBrokerAbortedError):
        gateway.submit_guarded(
            _submit_request(
                client_order_id="SELL-ALL-RACE",
                side=OrderSide.SELL,
                intent=OrderIntent.MANUAL_EXIT,
                quantity=100,
                limit_price=99.0,
                emergency=True,
            )
        )

    assert broker.submit_calls == []
    assert adoption_conflicts
    assert fetch_execution_order(engine, "SELL-ALL-RACE").status == (
        ExecutionOrderStatus.CANCELLED_LOCALLY
    )


# --- cancellation -----------------------------------------------------------


def _submit_and_acknowledge(gateway, broker, *, client_order_id="CID-1", **overrides):
    broker.queue_acceptance(broker_order_id="B-1")
    gateway.submit_guarded(_submit_request(client_order_id=client_order_id, **overrides))
    return client_order_id


def _cancel_request(**overrides):
    _, lease = _lease()
    fields = dict(
        client_order_id="CID-1", cancel_command_id="CANCEL-1", environment="PROD",
        account_no="12345678-01", lease=lease, source=ExecutionSource.SYSTEM,
    )
    fields.update(overrides)
    return CancelExecutionRequest(**fields)


@pytest.mark.usefixtures("trading_enabled")
def test_cancel_requires_exact_identity(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_timeout()
    with pytest.raises(GuardedSubmissionAmbiguousError):
        gateway.submit_guarded(_submit_request())

    with pytest.raises(CancelNotPermittedError):
        gateway.cancel_guarded(_cancel_request())
    assert broker.cancel_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_cancel_rejects_an_account_environment_mismatch(tmp_path):
    """Finding 9: a caller-supplied environment/account_no that doesn't
    match the order's own persisted record must be rejected, not silently
    ignored in favor of the record's real values."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()

    with pytest.raises(CancelNotPermittedError):
        gateway.cancel_guarded(_cancel_request(account_no="99999999-01"))
    assert broker.cancel_calls == []

    with pytest.raises(CancelNotPermittedError):
        gateway.cancel_guarded(_cancel_request(environment="SIM"))
    assert broker.cancel_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_cancel_confirmed_reaches_cancelled(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()

    gateway.cancel_guarded(_cancel_request())
    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.CANCELLED


@pytest.mark.usefixtures("trading_enabled")
def test_external_fence_inserted_after_cancel_pending_commit_blocks_broker_cancel(
    tmp_path, monkeypatch
):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()
    real_require_lease = gateway._require_verified_lease
    calls = {"n": 0}

    def insert_fence_on_final_check(lease):
        calls["n"] += 1
        real_require_lease(lease)
        if calls["n"] == 2:
            _record_active_external_order(engine, broker_order_id="B-RACED-CANCEL")

    monkeypatch.setattr(
        gateway, "_require_verified_lease", insert_fence_on_final_check
    )

    with pytest.raises(GuardedCancellationPreBrokerAbortedError):
        gateway.cancel_guarded(_cancel_request())

    assert broker.cancel_calls == []
    assert fetch_execution_order(engine, "CID-1").status == ExecutionOrderStatus.ACKNOWLEDGED
    assert get_command_by_idempotency_key(engine, "CANCEL:CANCEL-1").status == "PRE_BROKER_ABORTED"

    external = next(
        order
        for order in list_discovered_external_orders_for_account(
            engine, environment="PROD", account_no="12345678-01"
        )
        if order.broker_order_id == "B-RACED-CANCEL"
    )
    _resolve_external_fence(engine, external)
    broker.queue_cancel_confirmed()
    gateway.cancel_guarded(_cancel_request(cancel_command_id="CANCEL-2"))
    assert fetch_execution_order(engine, "CID-1").status == ExecutionOrderStatus.CANCELLED
    assert len(broker.cancel_calls) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_cancel_timeout_leaves_cancel_pending_with_discovering_recovery_state_and_is_not_retried(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_timeout()

    with pytest.raises(GuardedCancellationAmbiguousError):
        gateway.cancel_guarded(_cancel_request())

    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.CANCEL_PENDING
    assert record.recovery_state == OrderRecoveryState.DISCOVERING
    assert len(broker.cancel_calls) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_cancel_explicit_rejection_returns_the_order_to_working(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_rejected()

    with pytest.raises(GuardedCancellationRejectedError):
        gateway.cancel_guarded(_cancel_request())
    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.WORKING


@pytest.mark.usefixtures("trading_enabled")
def test_a_fill_racing_the_cancel_is_reflected_not_ignored(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_fill_race(filled_quantity=10, quantity_requested=10)

    gateway.cancel_guarded(_cancel_request())
    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.FILLED
    assert record.filled_quantity == 10


@pytest.mark.usefixtures("trading_enabled")
def test_a_duplicate_cancel_replay_with_the_same_command_id_makes_zero_additional_broker_calls(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()
    gateway.cancel_guarded(_cancel_request(cancel_command_id="CANCEL-1"))
    assert len(broker.cancel_calls) == 1

    # A replay of the exact same cancel decision (same cancel_command_id).
    # By the time a duplicate cancel_command_id could be detected, the
    # order has already moved off the status a fresh cancel requires
    # (is_cancellable's own status check rejects first here, exactly as
    # it would for insert_command's own uniqueness check on any other
    # replay shape) -- either way, the real property under test is zero
    # additional broker calls, which holds regardless of which gate fires.
    with pytest.raises((DuplicateCommandError, CancelNotPermittedError)):
        gateway.cancel_guarded(_cancel_request(cancel_command_id="CANCEL-1"))
    assert len(broker.cancel_calls) == 1  # zero additional broker calls


@pytest.mark.usefixtures("trading_enabled")
def test_finding_8_a_new_cancel_decision_after_an_explicit_rejection_is_permitted_with_a_new_command_id(tmp_path):
    """The bug: reusing attempt_number as the cancel key permanently
    blocked a later, genuinely new cancel decision after an earlier one
    was explicitly rejected. Fixed: a new cancel_command_id is a new,
    permitted decision."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)

    broker.queue_cancel_rejected()
    with pytest.raises(GuardedCancellationRejectedError):
        gateway.cancel_guarded(_cancel_request(cancel_command_id="CANCEL-1"))
    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.WORKING

    # A later, genuinely new cancel decision -- different cancel_command_id.
    broker.queue_cancel_confirmed()
    gateway.cancel_guarded(_cancel_request(cancel_command_id="CANCEL-2"))
    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.CANCELLED
    assert len(broker.cancel_calls) == 2


@pytest.mark.usefixtures("trading_enabled")
def test_adopted_cancel_requires_explicit_cancel_permission(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    ext = new_discovered_external_order(
        environment="PROD", account_no="12345678-01", symbol="MSFT", side=OrderSide.BUY,
        broker_order_id="B-EXT-1", quantity_requested=5,
    )
    record = adopt_external_order(ext, adopted_by="tony", permissions=frozenset())
    record_execution_order(engine, record)

    with pytest.raises(CancelNotPermittedError):
        gateway.cancel_guarded(_cancel_request(client_order_id=record.client_order_id))
    assert broker.cancel_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_adopted_cancel_succeeds_with_explicit_cancel_permission(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    ext = new_discovered_external_order(
        environment="PROD", account_no="12345678-01", symbol="MSFT", side=OrderSide.BUY,
        broker_order_id="B-EXT-1", quantity_requested=5,
    )
    record = adopt_external_order(ext, adopted_by="tony", permissions=frozenset({AdoptedOrderPermission.CANCEL}))
    record_execution_order(engine, record)
    broker.queue_cancel_confirmed()

    gateway.cancel_guarded(_cancel_request(client_order_id=record.client_order_id))
    refreshed = fetch_execution_order(engine, record.client_order_id)
    assert refreshed.status == ExecutionOrderStatus.CANCELLED


# --- replace: finding 7 (REPLACE permission, not CANCEL) --------------------


def _replace_request(**overrides):
    _, lease = _lease()
    fields = dict(
        client_order_id="CID-1", replace_command_id="REPLACE-1", new_client_order_id="CID-1-REPLACEMENT",
        new_quantity=5, new_limit_price=101.0, environment="PROD", account_no="12345678-01",
        lease=lease, source=ExecutionSource.SYSTEM,
    )
    fields.update(overrides)
    return ReplaceExecutionRequest(**fields)


@pytest.mark.usefixtures("trading_enabled")
def test_replace_preserves_the_original_order_and_creates_a_linked_new_record(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()
    broker.queue_acceptance(broker_order_id="B-2")

    result = gateway.replace_guarded(_replace_request())
    assert result.broker_order_id == "B-2"

    original = fetch_execution_order(engine, "CID-1")
    assert original.status == ExecutionOrderStatus.CANCELLED
    assert original.submitted_quantity == 10

    new_record = fetch_execution_order(engine, "CID-1-REPLACEMENT")
    assert new_record.status == ExecutionOrderStatus.ACKNOWLEDGED
    assert new_record.replaces_execution_order_id == "CID-1"
    assert new_record.submitted_quantity == 5


@pytest.mark.usefixtures("trading_enabled")
def test_replace_persists_a_durable_parent_command_that_finalizes_on_success(tmp_path):
    """Finding 6 (third pass): the parent replace command's own row is
    what lets restart recovery distinguish a replace-in-progress from an
    independent cancel/submit pair."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_confirmed()
    broker.queue_acceptance(broker_order_id="B-2")

    gateway.replace_guarded(_replace_request())

    parent = get_command_by_idempotency_key(engine, "REPLACE:REPLACE-1")
    assert parent is not None
    assert parent.status == "COMPLETED"
    assert parent.command_type == "replace"

    # The linked sub-commands are independently visible too.
    assert get_command_by_idempotency_key(engine, "CANCEL:REPLACE-1:CANCEL") is not None
    assert get_command_by_idempotency_key(engine, "SUBMIT:CID-1-REPLACEMENT") is not None


@pytest.mark.usefixtures("trading_enabled")
def test_an_invalid_replacement_quantity_makes_zero_cancel_calls(tmp_path):
    """Finding 6: validate the entire replacement request before ever
    cancelling the perfectly good original order."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)

    with pytest.raises(ValueError):
        gateway.replace_guarded(_replace_request(new_quantity=0))
    assert broker.cancel_calls == []
    assert len(broker.submit_calls) == 1  # only the original submission, no replacement attempted
    # The original is untouched.
    original = fetch_execution_order(engine, "CID-1")
    assert original.status == ExecutionOrderStatus.ACKNOWLEDGED


@pytest.mark.usefixtures("trading_enabled")
def test_an_invalid_replacement_price_makes_zero_cancel_calls(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)

    with pytest.raises(ValueError):
        gateway.replace_guarded(_replace_request(new_limit_price=-1.0))
    assert broker.cancel_calls == []
    original = fetch_execution_order(engine, "CID-1")
    assert original.status == ExecutionOrderStatus.ACKNOWLEDGED


@pytest.mark.usefixtures("trading_enabled")
def test_a_duplicate_new_client_order_id_makes_zero_cancel_calls(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    # A second, unrelated order already occupies the identity the replace
    # request wants to reuse.
    broker.queue_acceptance(broker_order_id="B-EXISTING")
    gateway.submit_guarded(_submit_request(client_order_id="CID-1-REPLACEMENT"))

    with pytest.raises(ValueError):
        gateway.replace_guarded(_replace_request())  # new_client_order_id="CID-1-REPLACEMENT"
    assert broker.cancel_calls == []

    original = fetch_execution_order(engine, "CID-1")
    assert original.status == ExecutionOrderStatus.ACKNOWLEDGED  # untouched, never cancelled


@pytest.mark.usefixtures("trading_enabled")
def test_replace_rejects_an_account_environment_mismatch_before_any_cancel_call(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)

    with pytest.raises(CancelNotPermittedError):
        gateway.replace_guarded(_replace_request(account_no="99999999-01"))
    assert broker.cancel_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_replace_propagates_an_ambiguous_cancel_outcome_without_submitting_a_replacement(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_timeout()

    with pytest.raises(GuardedCancellationAmbiguousError):
        gateway.replace_guarded(_replace_request())
    assert len(broker.submit_calls) == 1  # only the original -- no replacement submitted

    record = fetch_execution_order(engine, "CID-1")
    assert record.status == ExecutionOrderStatus.CANCEL_PENDING


@pytest.mark.usefixtures("trading_enabled")
def test_replace_refuses_when_a_fill_races_the_cancel(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    _submit_and_acknowledge(gateway, broker)
    broker.queue_cancel_fill_race(filled_quantity=10, quantity_requested=10)

    with pytest.raises(ReplaceNotSafeError):
        gateway.replace_guarded(_replace_request())
    assert len(broker.submit_calls) == 1


def test_replace_is_not_available_in_legacy_compatibility_mode():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    with pytest.raises(WrongGatewayModeError):
        gateway.replace_guarded(_replace_request())


@pytest.mark.usefixtures("trading_enabled")
def test_replace_rejected_when_the_adopted_order_has_only_cancel_permission_not_replace(tmp_path):
    """Finding 7: CANCEL permission alone must not authorize a replace --
    only REPLACE does."""
    gateway, broker, engine = _guarded_gateway(tmp_path)
    ext = new_discovered_external_order(
        environment="PROD", account_no="12345678-01", symbol="MSFT", side=OrderSide.BUY,
        broker_order_id="B-EXT-1", quantity_requested=5,
    )
    record = adopt_external_order(ext, adopted_by="tony", permissions=frozenset({AdoptedOrderPermission.CANCEL}))
    record_execution_order(engine, record)

    with pytest.raises(CancelNotPermittedError):
        # The permission check (_cancellable_for_replace, requiring
        # REPLACE) fails inside _do_cancel and propagates as-is, distinct
        # from ReplaceNotSafeError (which is specifically about an unsafe
        # *broker* outcome, not an authorization failure).
        gateway.replace_guarded(
            _replace_request(client_order_id=record.client_order_id, new_client_order_id="NEW-1")
        )
    assert broker.cancel_calls == []
    assert broker.submit_calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_replace_succeeds_when_the_adopted_order_has_replace_permission(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    ext = new_discovered_external_order(
        environment="PROD", account_no="12345678-01", symbol="MSFT", side=OrderSide.BUY,
        broker_order_id="B-EXT-1", quantity_requested=5,
    )
    record = adopt_external_order(ext, adopted_by="tony", permissions=frozenset({AdoptedOrderPermission.REPLACE}))
    record_execution_order(engine, record)
    broker.queue_cancel_confirmed()
    broker.queue_acceptance(broker_order_id="B-NEW-1")

    result = gateway.replace_guarded(
        _replace_request(client_order_id=record.client_order_id, new_client_order_id="NEW-1")
    )
    assert result.broker_order_id == "B-NEW-1"


# --- Workstream 9: in-process mutual exclusion (finding 6) ------------------


def test_two_different_sources_cannot_concurrently_hold_the_same_account_symbol():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    key = ("PROD", "12345678-01", "AAPL")
    with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
        with pytest.raises(ConcurrentExecutionOwnershipError):
            with gateway._ownership.claim(key, ExecutionSource.KANBAN_BOARD):
                pass


def test_the_same_source_cannot_reclaim_a_key_it_already_holds():
    """Strict exclusion, not source-based reentrancy: a second claim on an
    already-held key always raises, even from the same source --
    reentrancy was never actually used by any real code path and its
    prior allowance had a genuine thread-safety bug (finding 6)."""
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    key = ("PROD", "12345678-01", "AAPL")
    with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
        with pytest.raises(ConcurrentExecutionOwnershipError):
            with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
                pass


def test_the_ownership_claim_is_released_after_the_call_completes():
    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    key = ("PROD", "12345678-01", "AAPL")
    with gateway._ownership.claim(key, ExecutionSource.LEGACY_BUY_DASHBOARD):
        pass
    with gateway._ownership.claim(key, ExecutionSource.KANBAN_BOARD):
        pass  # must not raise -- the first claim already released


def test_two_threads_racing_the_same_key_never_both_hold_it_concurrently():
    """A real multithreaded contention test (finding 6) -- proves the lock
    actually serializes concurrent claims rather than merely looking
    correct in single-threaded tests."""
    import threading
    import time

    broker = FakeExecutionBroker()
    gateway = ExecutionCommandGateway(real_broker=broker, mode_override=False)
    key = ("PROD", "12345678-01", "AAPL")

    concurrent_holders = []
    currently_inside = {"count": 0}
    lock = threading.Lock()

    def worker():
        try:
            with gateway._ownership.claim(key, ExecutionSource.KANBAN_BOARD):
                with lock:
                    currently_inside["count"] += 1
                    concurrent_holders.append(currently_inside["count"])
                time.sleep(0.01)
                with lock:
                    currently_inside["count"] -= 1
        except ConcurrentExecutionOwnershipError:
            pass  # expected for whichever thread loses the race

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max(concurrent_holders) == 1


@pytest.mark.usefixtures("trading_enabled")
def test_submitted_commands_record_the_issuing_source(tmp_path):
    gateway, broker, engine = _guarded_gateway(tmp_path)
    broker.queue_acceptance(broker_order_id="B-1")
    gateway.submit_guarded(_submit_request(source=ExecutionSource.LEGACY_BUY_DASHBOARD))

    command = get_command_by_idempotency_key(engine, "SUBMIT:CID-1")
    assert command.source == ExecutionSource.LEGACY_BUY_DASHBOARD.value
