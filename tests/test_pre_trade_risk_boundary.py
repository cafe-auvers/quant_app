from types import SimpleNamespace

import pytest

from src.core.order_state import OrderIntent, OrderSide, OrderStatus
from src.risk.pre_trade import (
    PreTradeRiskDecision,
    PreTradeRiskRejectedError,
    assess_orb_entry_candidate,
)
from src.services.broker import BrokerSubmissionResult
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.order_ledger import load_orders


class FakeBroker:
    def __init__(self):
        self.submissions = []

    def submit_order(self, **kwargs):
        self.submissions.append(kwargs)
        return BrokerSubmissionResult(
            broker_order_id="SIM-1",
            raw_response={"accepted": True},
        )

    def is_ambiguous_submission_error(self, _error):
        return False


@pytest.mark.parametrize(
    "decision",
    [
        None,
        PreTradeRiskDecision.reject(3, "capital allocation exceeds limit"),
        PreTradeRiskDecision.approve(2),
    ],
)
def test_entry_requires_matching_approved_risk_decision(
    tmp_path, trading_enabled, decision
):
    path = tmp_path / "orders.json"
    broker = FakeBroker()

    with pytest.raises(PreTradeRiskRejectedError):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=3,
            limit_price=100.0,
            path=path,
            broker=broker,
            pre_trade_risk_decision=decision,
        )

    assert broker.submissions == []
    assert load_orders(path=path) == []


def test_approved_entry_reaches_broker_and_persists_normalized_result(
    tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    broker = FakeBroker()

    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=3,
        limit_price=100.0,
        path=path,
        broker=broker,
        pre_trade_risk_decision=PreTradeRiskDecision.approve(3),
    )

    assert order.status == OrderStatus.ACCEPTED
    assert order.broker_order_id == "SIM-1"
    assert order.raw_submit_response == {"accepted": True}
    assert len(broker.submissions) == 1


def test_exit_order_does_not_require_entry_risk_approval(
    tmp_path, trading_enabled
):
    broker = FakeBroker()

    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.SELL,
        intent=OrderIntent.STOP_LOSS,
        quantity=3,
        limit_price=99.0,
        path=tmp_path / "orders.json",
        broker=broker,
    )

    assert order.status == OrderStatus.ACCEPTED
    assert len(broker.submissions) == 1


def test_orb_candidate_is_revalidated_into_quantity_bound_decision():
    ready = SimpleNamespace(
        status="EXECUTE_READY",
        valid=True,
        shares=10,
        entry_trigger=100.0,
        stop_loss=98.0,
        capital_percent=20.0,
        stop_loss_percent=2.0,
        stop_adr=50.0,
        warnings=[],
    )

    assert assess_orb_entry_candidate(ready, 10) == PreTradeRiskDecision.approve(10)

    changed_quantity = assess_orb_entry_candidate(ready, 11)
    assert changed_quantity.approved is False
    assert any("quantity" in reason.lower() for reason in changed_quantity.reasons)


def test_order_execution_service_has_no_kis_response_dependency():
    import src.services.order_execution_service as service

    assert not hasattr(service, "kis_order")


@pytest.mark.parametrize(
    ("ambiguous", "expected_status"),
    [
        (True, OrderStatus.UNKNOWN_SUBMISSION_STATE),
        (False, OrderStatus.REJECTED),
    ],
)
def test_submission_error_classification_stays_behind_broker_boundary(
    tmp_path, trading_enabled, ambiguous, expected_status
):
    class ErrorBroker(FakeBroker):
        def submit_order(self, **kwargs):
            self.submissions.append(kwargs)
            raise RuntimeError("broker-specific failure")

        def is_ambiguous_submission_error(self, error):
            assert str(error) == "broker-specific failure"
            return ambiguous

    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=3,
        limit_price=100.0,
        path=tmp_path / "orders.json",
        broker=ErrorBroker(),
        pre_trade_risk_decision=PreTradeRiskDecision.approve(3),
    )

    assert order.status == expected_status
