"""Characterization tests: LEGACY_COMPATIBILITY mode is a byte-for-byte
transparent pass-through (Workstream 3/9, PR2).

docs/kanban_production_readiness.md: "Existing live behavior remains
unchanged" and "Change existing order timing, sizing, pricing, or retry
behavior in compatibility mode" is explicitly forbidden. These tests pin
that guarantee directly: the exact same scripted fake-broker outcome,
submitted once through
``submit_guarded_overseas_order(..., broker=<fake broker directly>)`` and
once through ``submit_guarded_overseas_order(..., broker=<ExecutionCommandGateway
wrapping the same fake broker, LEGACY_COMPATIBILITY mode>)``, must produce
an identical persisted ``BrokerOrder`` -- same status, quantity, price,
order type, ``broker_order_id``, and error propagation. If the gateway
ever stops being transparent in this mode, one of these tests catches it
immediately rather than only showing up as an unrelated legacy-path
regression somewhere else in the suite.
"""
from __future__ import annotations

import pytest

from src.core.order_state import OrderIntent, OrderSide, OrderStatus, RESERVED_MOO_EXECUTION
from src.risk.pre_trade import PreTradeRiskDecision
from src.services.execution_command_gateway import ExecutionCommandGateway
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.order_reconciliation import cancel_and_reconcile_order
from fakes.fake_execution_broker import BrokerRejectionError, BrokerTimeoutError, FakeExecutionBroker

STRATEGY_ID = "TEST"
PLAN_ID = "TEST:AAPL"


def _risk_approval(quantity=1, *, side=OrderSide.BUY, intent=OrderIntent.ENTRY, reference_price=100.0):
    return PreTradeRiskDecision.approve(
        environment="SIM", account_no="12345678", symbol="AAPL", side=side, intent=intent,
        quantity=quantity, reference_price=reference_price, exchange="NASD",
        execution_policy="REGULAR_LIMIT", strategy_id=STRATEGY_ID, plan_id=PLAN_ID,
    )


def _submit(*, broker, path, quantity=1, limit_price=100.0):
    return submit_guarded_overseas_order(
        environment="SIM", account_no="12345678", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=quantity, limit_price=limit_price, path=path,
        broker=broker, pre_trade_risk_decision=_risk_approval(quantity, reference_price=limit_price),
        strategy_id=STRATEGY_ID, plan_id=PLAN_ID,
    )


def _gateway(fake_broker: FakeExecutionBroker) -> ExecutionCommandGateway:
    return ExecutionCommandGateway(real_broker=fake_broker, mode_override=False)


@pytest.mark.usefixtures("trading_enabled")
def test_a_clean_acceptance_is_identical_through_the_gateway_and_direct(tmp_path):
    direct_broker = FakeExecutionBroker()
    direct_broker.queue_acceptance(broker_order_id="B-1", raw_response={"ODNO": "B-1"})
    direct_order = _submit(broker=direct_broker, path=tmp_path / "direct.json")

    gateway_broker = FakeExecutionBroker()
    gateway_broker.queue_acceptance(broker_order_id="B-1", raw_response={"ODNO": "B-1"})
    gateway_order = _submit(broker=_gateway(gateway_broker), path=tmp_path / "gateway.json")

    assert direct_order.status == gateway_order.status == OrderStatus.ACCEPTED
    assert direct_order.broker_order_id == gateway_order.broker_order_id == "B-1"
    assert direct_order.quantity_requested == gateway_order.quantity_requested
    assert direct_order.limit_price == gateway_order.limit_price
    assert direct_order.remaining_quantity == gateway_order.remaining_quantity
    # The real broker call it made was itself identical, not just the result.
    assert direct_broker.submit_calls == gateway_broker.submit_calls


@pytest.mark.usefixtures("trading_enabled")
def test_an_explicit_rejection_is_identical_through_the_gateway_and_direct(tmp_path):
    direct_broker = FakeExecutionBroker()
    direct_broker.queue_rejection(message="insufficient funds")
    direct_order = _submit(broker=direct_broker, path=tmp_path / "direct.json")

    gateway_broker = FakeExecutionBroker()
    gateway_broker.queue_rejection(message="insufficient funds")
    gateway_order = _submit(broker=_gateway(gateway_broker), path=tmp_path / "gateway.json")

    assert direct_order.status == gateway_order.status == OrderStatus.REJECTED
    assert direct_order.error_message == gateway_order.error_message


@pytest.mark.usefixtures("trading_enabled")
def test_a_timeout_is_identical_through_the_gateway_and_direct(tmp_path):
    direct_broker = FakeExecutionBroker()
    direct_broker.queue_timeout()
    direct_order = _submit(broker=direct_broker, path=tmp_path / "direct.json")

    gateway_broker = FakeExecutionBroker()
    gateway_broker.queue_timeout()
    gateway_order = _submit(broker=_gateway(gateway_broker), path=tmp_path / "gateway.json")

    assert direct_order.status == gateway_order.status == OrderStatus.UNKNOWN_SUBMISSION_STATE
    assert direct_order.error_message == gateway_order.error_message


@pytest.mark.usefixtures("trading_enabled")
def test_a_reserved_moo_submission_is_identical_through_the_gateway_and_direct(tmp_path):
    direct_broker = FakeExecutionBroker()
    direct_broker.queue_acceptance(broker_order_id="RSV-1")
    direct_order = submit_guarded_overseas_order(
        environment="PROD", account_no="12345678", symbol="AAPL", side=OrderSide.SELL,
        intent=OrderIntent.STOP_LOSS, quantity=5, limit_price=0.0, execution_policy=RESERVED_MOO_EXECUTION,
        path=tmp_path / "direct.json", broker=direct_broker,
    )

    gateway_broker = FakeExecutionBroker()
    gateway_broker.queue_acceptance(broker_order_id="RSV-1")
    gateway_order = submit_guarded_overseas_order(
        environment="PROD", account_no="12345678", symbol="AAPL", side=OrderSide.SELL,
        intent=OrderIntent.STOP_LOSS, quantity=5, limit_price=0.0, execution_policy=RESERVED_MOO_EXECUTION,
        path=tmp_path / "gateway.json", broker=_gateway(gateway_broker),
    )

    assert direct_order.status == gateway_order.status == OrderStatus.ACCEPTED
    assert direct_order.execution_policy == gateway_order.execution_policy == RESERVED_MOO_EXECUTION
    assert direct_broker.submit_calls == gateway_broker.submit_calls


def test_cancellation_is_identical_through_the_gateway_and_direct(tmp_path, trading_enabled):
    # Get an ACCEPTED order on each ledger first, via the same path each
    # test uses elsewhere in this file.
    direct_broker = FakeExecutionBroker()
    direct_broker.queue_acceptance(broker_order_id="B-1")
    direct_path = tmp_path / "direct.json"
    direct_order = _submit(broker=direct_broker, path=direct_path)

    gateway_broker = FakeExecutionBroker()
    gateway_broker.queue_acceptance(broker_order_id="B-1")
    gateway_path = tmp_path / "gateway.json"
    gateway_order = _submit(broker=gateway_broker, path=gateway_path)  # accepted directly first

    direct_broker.queue_cancel_confirmed()
    direct_cancelled = cancel_and_reconcile_order(
        direct_order.client_order_id, path=direct_path, broker=direct_broker
    )

    gateway_broker.queue_cancel_confirmed()
    gateway_cancelled = cancel_and_reconcile_order(
        gateway_order.client_order_id, path=gateway_path, broker=_gateway(gateway_broker)
    )

    assert direct_cancelled.status == gateway_cancelled.status == OrderStatus.CANCELLED
    assert direct_broker.cancel_calls == gateway_broker.cancel_calls


def test_a_cancel_rejection_propagates_identically_through_the_gateway_and_direct(tmp_path, trading_enabled):
    direct_broker = FakeExecutionBroker()
    direct_broker.queue_acceptance(broker_order_id="B-1")
    direct_path = tmp_path / "direct.json"
    direct_order = _submit(broker=direct_broker, path=direct_path)

    gateway_broker = FakeExecutionBroker()
    gateway_broker.queue_acceptance(broker_order_id="B-1")
    gateway_path = tmp_path / "gateway.json"
    gateway_order = _submit(broker=gateway_broker, path=gateway_path)

    direct_broker.queue_cancel_rejected()
    gateway_broker.queue_cancel_rejected()

    with pytest.raises(BrokerRejectionError) as direct_exc:
        cancel_and_reconcile_order(direct_order.client_order_id, path=direct_path, broker=direct_broker)
    with pytest.raises(BrokerRejectionError) as gateway_exc:
        cancel_and_reconcile_order(
            gateway_order.client_order_id, path=gateway_path, broker=_gateway(gateway_broker)
        )
    assert str(direct_exc.value) == str(gateway_exc.value)
