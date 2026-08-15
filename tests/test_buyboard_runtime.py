"""Tests for src.services.buyboard_runtime (the P0-8 composition root)."""
from __future__ import annotations

import pandas as pd
import pytest

from src.core.order_state import (
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import buyboard_runtime as runtime_module
from src.services.broker import BrokerSubmissionResult
from src.services.intraday_provider import IntradayProviderName, IntradayResult
from src.services.realtime_market_data import QuoteSnapshot
from src.services.trading_engine import TradingEngine


def _card(**overrides):
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.BUY_TODAY,
        entry_trigger=100.0,
        entry_orb_low=95.0,
        stop_adr=30.0,
        risk_percent=1.0,
        selected_orb_window="5m",
        planned_quantity=20,
    )
    fields.update(overrides)
    return TradeCardState(**fields)


class _FakeBroker:
    """Minimal stand-in satisfying the Broker protocol surface this module
    actually calls, without touching real KIS."""

    def __init__(self):
        self.cancel_calls = []
        self.discover_result = BrokerOrderDiscoveryResult(
            open_orders_complete=True, history_complete=True, reserved_orders_complete=True
        )
        self.positions = {"overseas": {"holdings": []}}

    def submit_order(self, **kwargs):
        return BrokerSubmissionResult(broker_order_id="B-1", raw_response={})

    def is_ambiguous_submission_error(self, error):
        return False

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        raise AssertionError("not exercised in these tests")

    def get_order(self, **kwargs):
        return []

    def discover_orders(self, *, environment, account_no):
        return self.discover_result

    def get_positions(self, *, environment, account_no=None):
        return self.positions


# --- Pre-trade risk revalidation (real gate, not a rubber stamp) -----------


def test_revalidate_and_approve_approves_a_sound_plan():
    card = _card()
    decision = runtime_module._revalidate_and_approve(
        card, quantity=20, limit_price=100.0, exchange="NASD", account_size=10_000.0
    )
    assert decision is not None
    assert decision.approved is True


def test_revalidate_and_approve_rejects_stop_above_entry():
    card = _card(entry_orb_low=105.0)  # stop above the entry price
    decision = runtime_module._revalidate_and_approve(
        card, quantity=20, limit_price=100.0, exchange="NASD", account_size=10_000.0
    )
    assert decision.approved is False
    assert decision.reasons


def test_revalidate_and_approve_rejects_capital_percent_out_of_band():
    card = _card()
    # quantity*price way above 30% of a small account.
    decision = runtime_module._revalidate_and_approve(
        card, quantity=20, limit_price=100.0, exchange="NASD", account_size=1_000.0
    )
    assert decision.approved is False


def test_revalidate_and_approve_rejects_sl_adr_out_of_band():
    card = _card(stop_adr=200.0)  # far outside the 15-66 band
    decision = runtime_module._revalidate_and_approve(
        card, quantity=20, limit_price=100.0, exchange="NASD", account_size=10_000.0
    )
    assert decision.approved is False


def test_revalidate_and_approve_decision_matches_the_exact_order_fingerprint():
    """require_pre_trade_risk_approval validates every field -- confirm the
    decision this module builds actually carries the submitted order's real
    quantity/price/symbol, not placeholders."""
    card = _card(symbol="NVDA", account_no="99")
    decision = runtime_module._revalidate_and_approve(
        card, quantity=7, limit_price=123.45, exchange="NASD", account_size=100_000.0
    )
    assert decision.symbol == "NVDA"
    assert decision.account_no == "99"
    assert decision.quantity == 7
    assert decision.reference_price == pytest.approx(123.45)
    assert decision.side == OrderSide.BUY
    assert decision.intent == OrderIntent.ENTRY


# --- Composition root wiring -------------------------------------------


def test_build_buyboard_runtime_assembles_a_working_engine():
    broker = _FakeBroker()
    cards = {("PROD", "1", "AAPL"): _card()}

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: cards.get((env, acct, sym)),
        broker=broker,
    )

    assert isinstance(runtime.trading_engine, TradingEngine)
    assert runtime.market_data is not None  # falls back to the KIS-bar-close poller
    assert runtime.broker is broker


def test_submit_order_wrapper_supplies_a_fresh_risk_decision(monkeypatch):
    broker = _FakeBroker()
    card = _card()
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(runtime_module, "submit_guarded_overseas_order", fake_submit_guarded)

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 10_000.0,  # 2000/10000 = 20%, inside the 10-30% band
        card_lookup=lambda env, acct, sym: card,
        broker=broker,
    )

    order = runtime.entry_attempt_manager._submit_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=20, limit_price=100.0, exchange="NASD",
        attempt_group_id="g1", attempt_number=1, attempt_deadline_at=None,
        capital_reservation_id="",
    )

    assert order.status == OrderStatus.ACCEPTED
    assert captured["pre_trade_risk_decision"] is not None
    assert captured["pre_trade_risk_decision"].approved is True
    assert captured["strategy_id"] == runtime_module.RISK_STRATEGY_ID
    assert captured["broker"] is broker


def test_submit_order_wrapper_denies_when_card_is_unknown(monkeypatch):
    """No card found for the symbol -- must not fabricate an approval."""
    broker = _FakeBroker()
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.REJECTED,
        )

    monkeypatch.setattr(runtime_module, "submit_guarded_overseas_order", fake_submit_guarded)

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
    )
    runtime.entry_attempt_manager._submit_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=20, limit_price=100.0, exchange="NASD",
        attempt_group_id="g1", attempt_number=1, attempt_deadline_at=None,
        capital_reservation_id="",
    )
    assert captured["pre_trade_risk_decision"] is None


# --- KIS-only quote fallback -------------------------------------------


def test_kis_only_quote_fetcher_uses_latest_bar_close(monkeypatch):
    bars = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [101.0, 102.5], "Volume": [10, 20]},
        index=pd.to_datetime(["2026-01-05 14:29", "2026-01-05 14:30"]),
    )
    result = IntradayResult(symbol="AAPL", interval="1m", source=IntradayProviderName.KIS, bars=bars)
    monkeypatch.setattr(runtime_module, "fetch_execution_grade_intraday", lambda request: result)

    quote = runtime_module._kis_only_quote_fetcher("AAPL")

    assert isinstance(quote, QuoteSnapshot)
    assert quote.last_price == pytest.approx(102.5)


def test_kis_only_quote_fetcher_raises_when_no_bars(monkeypatch):
    empty = IntradayResult(symbol="AAPL", interval="1m", source=IntradayProviderName.KIS, bars=pd.DataFrame())
    monkeypatch.setattr(runtime_module, "fetch_execution_grade_intraday", lambda request: empty)

    with pytest.raises(runtime_module.ExecutionGradeDataUnavailableError):
        runtime_module._kis_only_quote_fetcher("AAPL")


# --- refresh_orderable_quantity ------------------------------------------


def test_refresh_orderable_quantity_reads_matching_holding():
    broker = _FakeBroker()
    broker.positions = {
        "overseas": {"holdings": [{"symbol": "AAPL", "orderable_quantity": 42, "quantity": 50}]}
    }
    quantity = runtime_module._refresh_orderable_quantity("PROD", "1", "AAPL", broker=broker)
    assert quantity == 42


def test_refresh_orderable_quantity_returns_zero_when_symbol_absent():
    broker = _FakeBroker()
    quantity = runtime_module._refresh_orderable_quantity("PROD", "1", "AAPL", broker=broker)
    assert quantity == 0


# --- P0-4: entry plan_id is consistent between approval and submission -----


def test_entry_plan_id_matches_between_risk_decision_and_submission(monkeypatch):
    broker = _FakeBroker()
    card = _card()
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(runtime_module, "submit_guarded_overseas_order", fake_submit_guarded)

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 10_000.0,
        card_lookup=lambda env, acct, sym: card,
        broker=broker,
    )
    runtime.entry_attempt_manager._submit_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=20, limit_price=100.0, exchange="NASD",
        attempt_group_id="g1", attempt_number=1, attempt_deadline_at=None,
        capital_reservation_id="",
    )

    decision = captured["pre_trade_risk_decision"]
    assert decision.plan_id == captured["plan_id"]
    assert captured["plan_id"] == runtime_module._entry_plan_id(card)
    assert captured["plan_id"] == "PROD:AAPL:5m"


# --- P1-9: submitted quantity is resized to the actual live price ----------


def test_submit_order_resizes_quantity_down_at_a_higher_live_price(monkeypatch):
    broker = _FakeBroker()
    card = _card(entry_orb_low=95.0, risk_percent=0.01)  # $5/share risk, 1% risk budget
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(runtime_module, "submit_guarded_overseas_order", fake_submit_guarded)

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 10_000.0,
        card_lookup=lambda env, acct, sym: card,
        broker=broker,
    )
    # 10_000 * 1% = 100 total risk. At $5/share (entry 100, stop 95) that's
    # 20 shares (matches the plan). At an actual submit price of 145 (stop
    # unchanged at 95 -> $50/share risk), only 2 shares are safe.
    runtime.entry_attempt_manager._submit_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=20, limit_price=145.0, exchange="NASD",
        attempt_group_id="g1", attempt_number=1, attempt_deadline_at=None,
        capital_reservation_id="",
    )

    assert captured["quantity"] == 2
    # The risk decision must be built for the *same* (resized) quantity
    # actually submitted, not the original, or the fingerprint gate would
    # reject a mismatched order.
    assert captured["pre_trade_risk_decision"].quantity == 2


def test_submit_order_does_not_resize_up_at_a_lower_live_price(monkeypatch):
    broker = _FakeBroker()
    card = _card(entry_orb_low=95.0, risk_percent=0.5)  # generous risk budget
    captured = {}
    monkeypatch.setattr(
        runtime_module,
        "submit_guarded_overseas_order",
        lambda **kwargs: (captured.update(kwargs) or BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )),
    )
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: card,
        broker=broker,
    )
    runtime.entry_attempt_manager._submit_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=20, limit_price=96.0, exchange="NASD",
        attempt_group_id="g1", attempt_number=1, attempt_deadline_at=None,
        capital_reservation_id="",
    )
    assert captured["quantity"] == 20  # never resized above the originally requested amount


def test_submit_order_skips_resize_without_a_trustworthy_risk_percent(monkeypatch):
    broker = _FakeBroker()
    card = _card(entry_orb_low=95.0, risk_percent=0.0)
    captured = {}
    monkeypatch.setattr(
        runtime_module,
        "submit_guarded_overseas_order",
        lambda **kwargs: (captured.update(kwargs) or BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=OrderSide.BUY, intent=OrderIntent.ENTRY, quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )),
    )
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 10_000.0,
        card_lookup=lambda env, acct, sym: card,
        broker=broker,
    )
    runtime.entry_attempt_manager._submit_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=20, limit_price=500.0, exchange="NASD",
        attempt_group_id="g1", attempt_number=1, attempt_deadline_at=None,
        capital_reservation_id="",
    )
    assert captured["quantity"] == 20  # no trustworthy risk_percent -> no resize attempted


# --- P0-3: the production SELL adapter --------------------------------------


def _market_data_with_quote(symbol, *, bid=None, last_price=100.0):
    from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService

    market_data = RestPollingMarketDataService(
        quote_fetcher=lambda s: QuoteSnapshot(symbol=s, last_price=last_price, bid=bid)
    )
    market_data.subscribe([symbol])
    market_data.poll_once()
    return market_data


def test_submit_sell_order_maps_partial_sell_reason_and_prices_from_live_bid(monkeypatch):
    broker = _FakeBroker()
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=kwargs["side"], intent=kwargs["intent"], quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(runtime_module, "submit_guarded_overseas_order", fake_submit_guarded)
    market_data = _market_data_with_quote("AAPL", bid=99.5)

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
        market_data=market_data,
    )

    order = runtime.trading_engine._position_callbacks.submit_sell_order(
        environment="PROD", account_no="1", symbol="AAPL", quantity=50, reason="partial_sell",
    )

    assert order.status == OrderStatus.ACCEPTED
    assert captured["side"] == OrderSide.SELL
    assert captured["intent"] == OrderIntent.PARTIAL_EXIT
    assert captured["limit_price"] == pytest.approx(99.5)  # uses the live bid
    assert captured["quantity"] == 50
    assert "reason" not in captured  # never forwarded to submit_guarded_overseas_order


def test_submit_sell_order_maps_sell_all_reasons_to_manual_exit_and_discounts_last_price(monkeypatch):
    broker = _FakeBroker()
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"], account_no=kwargs["account_no"], symbol=kwargs["symbol"],
            side=kwargs["side"], intent=kwargs["intent"], quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"], status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(runtime_module, "submit_guarded_overseas_order", fake_submit_guarded)
    market_data = _market_data_with_quote("AAPL", last_price=50.0)  # no bid cached

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
        market_data=market_data,
    )

    for reason in ("sell_all", "sell_all_retry"):
        runtime.trading_engine._position_callbacks.submit_sell_order(
            environment="PROD", account_no="1", symbol="AAPL", quantity=100, reason=reason,
        )
        assert captured["intent"] == OrderIntent.MANUAL_EXIT
        assert captured["limit_price"] == pytest.approx(50.0 * (1 - 0.005))


def test_submit_sell_order_raises_without_a_live_quote():
    from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService

    broker = _FakeBroker()
    market_data = RestPollingMarketDataService(quote_fetcher=lambda s: QuoteSnapshot(symbol=s, last_price=100.0))
    # never subscribed/polled -> latest_quote() is None

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
        market_data=market_data,
    )
    with pytest.raises(runtime_module.ExecutionGradeDataUnavailableError):
        runtime.trading_engine._position_callbacks.submit_sell_order(
            environment="PROD", account_no="1", symbol="AAPL", quantity=10, reason="sell_all",
        )


# --- P0-5: broker-truth cumulative fill refresh -----------------------------


def test_refresh_broker_position_finds_matching_holding():
    from src.services.position_manager import BrokerHolding

    broker = _FakeBroker()
    broker.positions = {
        "overseas": {"holdings": [{"symbol": "AAPL", "quantity": 40, "average_price": 101.25}]}
    }
    card = _card(symbol="AAPL")
    holding = runtime_module._refresh_broker_position(card, broker=broker)
    assert holding == BrokerHolding(symbol="AAPL", quantity=40, average_price=101.25)


def test_refresh_broker_position_returns_none_when_symbol_absent():
    broker = _FakeBroker()
    card = _card(symbol="AAPL")
    assert runtime_module._refresh_broker_position(card, broker=broker) is None


# --- P0-8: real market-session hooks are wired ------------------------------


def test_build_buyboard_runtime_wires_real_market_session_hooks():
    broker = _FakeBroker()
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
    )
    # Not asserting a specific True/False (depends on real wall-clock time)
    # -- only that the engine is no longer using the always-open/never-EOD
    # test defaults.
    assert runtime.trading_engine._market_is_open_fn is runtime_module.is_regular_session_open
    assert runtime.trading_engine._eod_window_reached_fn is runtime_module._eod_window_reached


# --- P1-1: capital_reservation_engine is actually threaded through ----------


def test_build_buyboard_runtime_threads_capital_reservation_engine():
    broker = _FakeBroker()
    sentinel_engine = object()
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
        capital_reservation_engine=sentinel_engine,
    )
    assert runtime.entry_attempt_manager._capital_reservation_engine is sentinel_engine
    assert runtime.eod_service._capital_reservation_engine is sentinel_engine
