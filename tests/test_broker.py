"""Tests for the thin Broker abstraction (src/services/broker.py).

These verify KisBroker is a thin adapter around the existing src.api.kis_order
/ kis_account_snapshot_dual calls, including response/error normalization but
no retry/lifecycle logic. OrderExecutionService's own
state-machine behavior (CREATED -> UNKNOWN_SUBMISSION_STATE -> ACCEPTED/
REJECTED) is covered separately in test_order_lifecycle.py.
"""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("authorized_full_live")

from src.api import kis_account_snapshot_dual, kis_order
from src.core.order_state import (REGULAR_LIMIT_EXECUTION,
                                  RESERVED_MOO_EXECUTION,
                                  BrokerOrderStatusSnapshot, OrderSide,
                                  OrderStatus)
from src.core.runtime_safety_audit import (
    BROKER_MUTATION_AUDIT_SOURCE,
    begin_runtime_safety_audit,
)
from src.risk.pre_trade import PreTradeRiskDecision
from src.services import trading_state
from src.services.broker import BrokerSubmissionResult, KisBroker, ReadOnlyBroker
from src.services.trading_state import TradingDisabledError


def test_real_broker_submission_is_disarmed_by_default(monkeypatch):
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled broker must not reach the KIS adapter")
        ),
    )

    with begin_runtime_safety_audit(
        required_sources={BROKER_MUTATION_AUDIT_SOURCE}
    ) as audit:
        with pytest.raises(TradingDisabledError):
            KisBroker().submit_order(
                environment="SIM",
                account_no="12345678",
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=1,
                limit_price=100.0,
            )
        snapshot = audit.snapshot()

    assert snapshot.initialized
    assert BROKER_MUTATION_AUDIT_SOURCE in snapshot.registered_sources
    assert snapshot.broker_mutation_attempt_count == 1


def test_standby_read_only_broker_never_delegates_mutations():
    calls = []
    delegate = SimpleNamespace(
        submit_order=lambda **kwargs: calls.append("submit"),
        cancel_order=lambda **kwargs: calls.append("cancel"),
        get_order=lambda **kwargs: ["order"],
        discover_orders=lambda **kwargs: "orders",
        get_positions=lambda **kwargs: {"positions": []},
    )
    broker = ReadOnlyBroker(delegate)

    with pytest.raises(RuntimeError, match="read-only"):
        broker.submit_order()
    with pytest.raises(RuntimeError, match="read-only"):
        broker.cancel_order()
    assert broker.get_order(environment="PROD", account_no="1") == ["order"]
    assert broker.get_positions(environment="PROD", account_no="1") == {
        "positions": []
    }
    assert calls == []


def test_low_level_kis_submission_is_disarmed_before_authentication(monkeypatch):
    monkeypatch.setattr(
        kis_order,
        "load_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled submission must not authenticate")
        ),
    )

    with pytest.raises(TradingDisabledError):
        kis_order.place_overseas_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            quantity=1,
            price=100.0,
            side="buy",
        )


def test_submit_order_regular_limit_calls_place_overseas_order(
    monkeypatch, trading_enabled
):
    captured = {}

    def fake_place_overseas_order(**kwargs):
        captured.update(kwargs)
        return {"rt_cd": "0", "output": {"ODNO": "OK"}}

    monkeypatch.setattr(kis_order, "place_overseas_order", fake_place_overseas_order)
    monkeypatch.setattr(
        kis_order,
        "place_overseas_reserved_market_on_open_sell",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("wrong endpoint called")),
    )

    result = KisBroker().submit_order(
        environment="PROD",
        account_no="12345678-01",
        symbol="aapl",
        side=OrderSide.BUY,
        quantity=3,
        limit_price=191.23,
        exchange="NASD",
        execution_policy=REGULAR_LIMIT_EXECUTION,
    )

    assert result == BrokerSubmissionResult(
        broker_order_id="OK",
        raw_response={"rt_cd": "0", "output": {"ODNO": "OK"}},
    )
    assert captured == {
        "environment": "PROD",
        "account_no": "12345678-01",
        "symbol": "aapl",
        "quantity": 3,
        "price": 191.23,
        "side": "buy",
        "exchange": "NASD",
        "order_type": "limit",
    }


def test_submit_order_reserved_moo_calls_reserved_endpoint(
    monkeypatch, trading_enabled
):
    captured = {}

    def fake_reserved(**kwargs):
        captured.update(kwargs)
        return {"rt_cd": "0", "output": {"OVRS_RSVN_ODNO": "RSV-1"}}

    monkeypatch.setattr(kis_order, "place_overseas_reserved_market_on_open_sell", fake_reserved)
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("wrong endpoint called")),
    )

    result = KisBroker().submit_order(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        side=OrderSide.SELL,
        quantity=4,
        limit_price=0.0,
        exchange="NASD",
        execution_policy=RESERVED_MOO_EXECUTION,
    )

    assert result == BrokerSubmissionResult(
        broker_order_id="RSV-1",
        raw_response={
            "rt_cd": "0",
            "output": {"OVRS_RSVN_ODNO": "RSV-1"},
        },
    )
    assert captured == {
        "environment": "PROD",
        "account_no": "12345678-01",
        "symbol": "AAPL",
        "quantity": 4,
        "exchange": "NASD",
    }


def test_cancel_order_regular_vs_reserved_routes_to_different_endpoints(monkeypatch):
    regular_calls = []
    reserved_calls = []
    monkeypatch.setattr(
        kis_order,
        "cancel_overseas_order",
        lambda **kwargs: regular_calls.append(kwargs) or "regular-result",
    )
    monkeypatch.setattr(
        kis_order,
        "cancel_overseas_reserved_order",
        lambda **kwargs: reserved_calls.append(kwargs) or "reserved-result",
    )

    broker = KisBroker()
    with begin_runtime_safety_audit(
        required_sources={BROKER_MUTATION_AUDIT_SOURCE}
    ) as audit:
        regular_result = broker.cancel_order(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            broker_order_id="KIS-1",
            quantity=1,
            side="buy",
        )
        reserved_result = broker.cancel_order(
            environment="PROD",
            account_no="12345678-01",
            is_reserved=True,
            broker_order_id="RSV-1",
            reservation_date="20260101",
        )
        audit_snapshot = audit.snapshot()

    assert regular_result == "regular-result"
    assert reserved_result == "reserved-result"
    assert audit_snapshot.broker_mutation_attempt_count == 2
    assert regular_calls == [
        {
            "environment": "PROD",
            "account_no": "12345678-01",
            "symbol": "AAPL",
            "broker_order_id": "KIS-1",
            "quantity": 1,
            "side": "buy",
        }
    ]
    assert reserved_calls == [
        {
            "environment": "PROD",
            "account_no": "12345678-01",
            "broker_order_id": "RSV-1",
            "reservation_date": "20260101",
        }
    ]


def test_get_order_regular_vs_reserved_routes_to_different_endpoints(monkeypatch):
    monkeypatch.setattr(kis_order, "query_overseas_order", lambda **kwargs: ["regular"])
    monkeypatch.setattr(kis_order, "query_overseas_reserved_order", lambda **kwargs: ["reserved"])

    broker = KisBroker()
    assert broker.get_order(
        environment="PROD", account_no="12345678-01", symbol="AAPL"
    ) == ["regular"]
    assert broker.get_order(
        environment="PROD", account_no="12345678-01", is_reserved=True
    ) == ["reserved"]


def test_discover_orders_requires_regular_and_reserved_sources(monkeypatch):
    regular = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        status=OrderStatus.WORKING,
    )
    reserved = BrokerOrderStatusSnapshot(
        environment="PROD",
        account_no="12345678-01",
        symbol="MSFT",
        side=OrderSide.SELL,
        status=OrderStatus.ACCEPTED,
    )
    monkeypatch.setattr(
        kis_order, "query_overseas_order", lambda **kwargs: [regular]
    )
    monkeypatch.setattr(
        kis_order, "query_overseas_reserved_order", lambda **kwargs: [reserved]
    )

    result = KisBroker().discover_orders(
        environment="PROD", account_no="12345678-01"
    )

    assert result.complete is True
    assert result.snapshots == [regular, reserved]


def test_discover_orders_reports_partial_regular_source_failure(monkeypatch):
    monkeypatch.setattr(
        kis_order,
        "query_overseas_order",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("nccs unavailable")),
    )
    monkeypatch.setattr(
        kis_order, "query_overseas_reserved_order", lambda **kwargs: []
    )

    result = KisBroker().discover_orders(
        environment="PROD", account_no="12345678-01"
    )

    assert result.complete is False
    assert result.open_orders_complete is False
    assert result.reserved_orders_complete is True
    assert any("nccs unavailable" in error for error in result.errors)


def test_discover_orders_checks_every_configured_exchange(monkeypatch):
    regular_calls = []
    reserved_calls = []
    monkeypatch.setenv("KIS_PROD_OVERSEAS_EXCHANGES", "NASD,NYSE,AMEX")
    monkeypatch.setattr(
        kis_order,
        "query_overseas_order",
        lambda **kwargs: regular_calls.append(kwargs) or [],
    )
    monkeypatch.setattr(
        kis_order,
        "query_overseas_reserved_order",
        lambda **kwargs: reserved_calls.append(kwargs) or [],
    )

    result = KisBroker().discover_orders(
        environment="PROD", account_no="12345678-01"
    )

    assert result.complete is True
    assert {call["exchange"] for call in regular_calls} == {"NASD", "NYSE", "AMEX"}
    assert {call["exchange"] for call in reserved_calls} == {"NASD", "NYSE", "AMEX"}


def test_get_positions_delegates_to_full_reconciliation_snapshot(monkeypatch):
    captured = {}

    def fake_fetch_account_snapshot(environment, **kwargs):
        captured["environment"] = environment
        captured.update(kwargs)
        return {"overseas": {"holdings": []}}

    monkeypatch.setattr(
        kis_account_snapshot_dual, "fetch_account_snapshot", fake_fetch_account_snapshot
    )

    result = KisBroker().get_positions(environment="SIM", account_no="12345678")

    assert result == {"overseas": {"holdings": []}}
    assert captured == {
        "environment": "SIM",
        "include_domestic": True,
        "include_overseas": True,
        "include_realized_pnl": False,
        "account_no": "12345678",
    }


def test_submit_guarded_overseas_order_accepts_injected_broker(monkeypatch, tmp_path):
    """OrderExecutionService's guarded submission goes through whatever Broker
    is passed in -- KisBroker is only the default, not a hardcoded dependency."""
    from src.core.order_state import OrderIntent
    from src.services import trading_state
    from src.services.order_execution_service import \
        submit_guarded_overseas_order

    trading_state.set_trading_enabled(True)
    path = tmp_path / "orders.json"

    class FakeBroker:
        def __init__(self):
            self.calls = []

        def submit_order(self, **kwargs):
            self.calls.append(kwargs)
            return BrokerSubmissionResult(
                broker_order_id="FAKE-1",
                raw_response={"accepted": True},
            )

        def is_ambiguous_submission_error(self, _error):
            return False

    fake_broker = FakeBroker()
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("real KIS API must not be called")),
    )

    order = submit_guarded_overseas_order(
        environment="SIM",
        account_no="12345678",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        limit_price=100.0,
        path=path,
        broker=fake_broker,
        pre_trade_risk_decision=PreTradeRiskDecision.approve(
            environment="SIM",
            account_no="12345678",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=1,
            reference_price=100.0,
            exchange="NASD",
            execution_policy=REGULAR_LIMIT_EXECUTION,
            strategy_id="TEST",
            plan_id="TEST:AAPL",
        ),
        strategy_id="TEST",
        plan_id="TEST:AAPL",
    )

    assert order.broker_order_id == "FAKE-1"
    assert len(fake_broker.calls) == 1
    assert fake_broker.calls[0]["symbol"] == "AAPL"
