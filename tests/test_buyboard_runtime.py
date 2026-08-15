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
