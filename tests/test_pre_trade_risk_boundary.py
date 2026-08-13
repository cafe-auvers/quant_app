from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.core.order_state import (REGULAR_LIMIT_EXECUTION, OrderIntent,
                                  OrderSide, OrderStatus)
from src.risk.pre_trade import (PreTradeRiskDecision,
                                PreTradeRiskRejectedError,
                                assess_orb_entry_candidate,
                                orb_candidate_plan_id)
from src.services.broker import BrokerSubmissionResult
from src.services.order_execution_service import submit_guarded_overseas_order
from src.services.order_ledger import load_orders

STRATEGY_ID = "TEST"
PLAN_ID = "TEST:AAPL"


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


def _decision(
    quantity=3,
    *,
    approved=True,
    environment="SIM",
    account_no="12345678",
    symbol="AAPL",
    side=OrderSide.BUY,
    intent=OrderIntent.ENTRY,
    reference_price=100.0,
    exchange="NASD",
    execution_policy=REGULAR_LIMIT_EXECUTION,
    strategy_id=STRATEGY_ID,
    plan_id=PLAN_ID,
    reasons=(),
    evaluated_at=None,
    expires_at=None,
):
    return PreTradeRiskDecision.create(
        approved=approved,
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        side=side,
        intent=intent,
        quantity=quantity,
        reference_price=reference_price,
        exchange=exchange,
        execution_policy=execution_policy,
        strategy_id=strategy_id,
        plan_id=plan_id,
        reasons=reasons,
        evaluated_at=evaluated_at,
        expires_at=expires_at,
    )


def _submit_entry(path, broker, decision, **overrides):
    command = {
        "environment": "SIM",
        "account_no": "12345678",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "intent": OrderIntent.ENTRY,
        "quantity": 3,
        "limit_price": 100.0,
        "exchange": "NASD",
        "execution_policy": REGULAR_LIMIT_EXECUTION,
        "strategy_id": STRATEGY_ID,
        "plan_id": PLAN_ID,
        "path": path,
        "broker": broker,
        "pre_trade_risk_decision": decision,
    }
    command.update(overrides)
    return submit_guarded_overseas_order(**command)


@pytest.mark.parametrize(
    "decision",
    [
        None,
        _decision(
            approved=False,
            reasons=("capital allocation exceeds limit",),
        ),
        _decision(quantity=2),
    ],
)
def test_entry_requires_matching_approved_risk_decision(
    tmp_path, trading_enabled, decision
):
    path = tmp_path / "orders.json"
    broker = FakeBroker()

    with pytest.raises(PreTradeRiskRejectedError):
        _submit_entry(path, broker, decision)

    assert broker.submissions == []
    assert load_orders(path=path) == []


@pytest.mark.parametrize(
    ("decision", "overrides"),
    [
        (_decision(environment="PROD"), {}),
        (_decision(account_no="99999999"), {}),
        (_decision(symbol="MSFT"), {}),
        (_decision(reference_price=101.0), {}),
        (_decision(strategy_id="OTHER"), {}),
        (_decision(plan_id="OTHER:AAPL"), {}),
        (_decision(exchange="NYSE"), {}),
        (_decision(execution_policy="RESERVED_MOO"), {}),
    ],
)
def test_entry_rejects_any_order_fingerprint_mismatch(
    tmp_path, trading_enabled, decision, overrides
):
    broker = FakeBroker()

    with pytest.raises(PreTradeRiskRejectedError, match="does not match"):
        _submit_entry(tmp_path / "orders.json", broker, decision, **overrides)

    assert broker.submissions == []


def test_entry_rejects_expired_approval_before_reservation(tmp_path, trading_enabled):
    evaluated = datetime(2026, 8, 13, tzinfo=timezone.utc)
    decision = _decision(
        evaluated_at=evaluated,
        expires_at=evaluated + timedelta(seconds=1),
    )
    broker = FakeBroker()

    with pytest.raises(PreTradeRiskRejectedError, match="expired"):
        _submit_entry(tmp_path / "orders.json", broker, decision)

    assert broker.submissions == []


def test_entry_rejects_approval_window_longer_than_policy(tmp_path, trading_enabled):
    evaluated = datetime.now(timezone.utc)
    decision = _decision(
        evaluated_at=evaluated,
        expires_at=evaluated + timedelta(minutes=5),
    )

    with pytest.raises(PreTradeRiskRejectedError, match="expired"):
        _submit_entry(tmp_path / "orders.json", FakeBroker(), decision)


def test_entry_rejects_malformed_direct_decision_before_reservation(
    tmp_path, trading_enabled
):
    valid = _decision()
    malformed = PreTradeRiskDecision(
        **{
            **valid.__dict__,
            "approved": "yes",
        }
    )

    with pytest.raises(PreTradeRiskRejectedError):
        _submit_entry(tmp_path / "orders.json", FakeBroker(), malformed)


def test_entry_rechecks_approval_after_durable_reservation(
    monkeypatch, tmp_path, trading_enabled
):
    import src.services.order_execution_service as service

    path = tmp_path / "orders.json"
    broker = FakeBroker()
    calls = {"count": 0}

    def expire_after_reservation(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise PreTradeRiskRejectedError("approval expired while waiting")

    monkeypatch.setattr(
        service, "require_pre_trade_risk_approval", expire_after_reservation
    )

    with pytest.raises(PreTradeRiskRejectedError, match="expired while waiting"):
        _submit_entry(path, broker, _decision())

    assert broker.submissions == []
    orders = load_orders(path=path)
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.REJECTED


@pytest.mark.parametrize("quantity", [True, 3.5, float("nan"), float("inf"), 0, -1])
def test_risk_decision_rejects_non_whole_or_non_finite_quantity(quantity):
    with pytest.raises(ValueError, match="quantity"):
        _decision(quantity=quantity)


def test_approved_entry_reaches_broker_and_persists_normalized_result(
    tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    broker = FakeBroker()

    order = _submit_entry(path, broker, _decision())

    assert order.status == OrderStatus.ACCEPTED
    assert order.broker_order_id == "SIM-1"
    assert order.raw_submit_response == {"accepted": True}
    assert len(broker.submissions) == 1


def test_buy_with_unknown_intent_is_rejected_before_ledger_and_broker(
    tmp_path, trading_enabled
):
    path = tmp_path / "orders.json"
    broker = FakeBroker()

    with pytest.raises(ValueError, match="explicit ENTRY"):
        submit_guarded_overseas_order(
            environment="SIM",
            account_no="12345678",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.UNKNOWN,
            quantity=3,
            limit_price=100.0,
            path=path,
            broker=broker,
        )

    assert broker.submissions == []
    assert load_orders(path=path) == []


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


def test_orb_candidate_is_revalidated_into_full_order_decision():
    evaluated = datetime.now(timezone.utc)
    ready = SimpleNamespace(
        symbol="AAPL",
        window="1m",
        status="EXECUTE_READY",
        valid=True,
        shares=10,
        entry_trigger=100.0,
        stop_loss=98.0,
        capital_percent=20.0,
        stop_loss_percent=2.0,
        stop_adr=50.0,
        risk_percent=0.4,
        warnings=[],
    )
    plan_id = orb_candidate_plan_id(ready)

    approved = assess_orb_entry_candidate(
        ready,
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        quantity=10,
        reference_price=100.0,
        plan_id=plan_id,
        evaluated_at=evaluated,
    )
    assert approved.approved is True
    assert approved.quantity == 10
    assert approved.reference_price == 100.0
    assert approved.strategy_id == "ORB"
    assert approved.plan_id == plan_id

    changed_quantity = assess_orb_entry_candidate(
        ready,
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        quantity=11,
        reference_price=100.0,
        plan_id=plan_id,
    )
    assert changed_quantity.approved is False
    assert any("quantity" in reason.lower() for reason in changed_quantity.reasons)

    wrong_plan = assess_orb_entry_candidate(
        ready,
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        quantity=10,
        reference_price=100.0,
        plan_id="ORB:stale",
    )
    assert wrong_plan.approved is False
    assert any("plan identifier" in reason.lower() for reason in wrong_plan.reasons)


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

    order = _submit_entry(
        tmp_path / "orders.json",
        ErrorBroker(),
        _decision(),
    )

    assert order.status == expected_status
