"""Tests for src.services.buyboard_runtime (the P0-8 composition root)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from src.core.order_state import (
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.execution_result import UnifiedExecutionStatus
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services import buyboard_runtime as runtime_module
from src.services import execution_workflow_service as workflow_module
from src.services.broker import BrokerSubmissionResult
from src.services.intraday_provider import IntradayProviderName, IntradayResult
from src.services.realtime_market_data import QuoteSnapshot
from src.services.trading_engine import TradingEngine
from src.risk.pre_trade import PreTradeRiskRejectedError


@pytest.fixture(autouse=True)
def _legacy_mode_by_default(monkeypatch):
    monkeypatch.setattr(
        runtime_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: False,
    )


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
        self.cancel_order_error = None
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
        if getattr(self, "cancel_order_error", None) is not None:
            raise self.cancel_order_error
        return BrokerOrderStatusSnapshot(
            environment=kwargs.get("environment", "PROD"),
            account_no=kwargs.get("account_no", "1"),
            symbol=kwargs.get("symbol", ""),
            broker_order_id=kwargs.get("broker_order_id", ""),
            status=OrderStatus.CANCELLED,
        )

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


def test_build_buyboard_runtime_rejects_enabled_plain_broker(monkeypatch):
    """PR2's third review pass, finding 2: DefaultExecutionLeaseProtocol
    can never satisfy the gateway's own epoch-verification gate, and
    AllowAllMutationBudget is a testing placeholder -- constructing a
    GUARDED_ENGINE composition with either would "succeed" at startup and
    then fail on the very first submission. Refuse outright instead, with
    no broker= override to bypass it (a caller-supplied broker is the one
    escape hatch, used only by other tests / explicit fake-broker wiring,
    never by real BUYBOARD_ENGINE_ENABLED=true activation)."""
    from src.core import execution_config

    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    cards = {}
    with pytest.raises(RuntimeError, match="plain broker overrides"):
        runtime_module.build_buyboard_runtime(
            buying_power_provider=lambda env, acct: 100_000.0,
            card_lookup=lambda env, acct, sym: cards.get((env, acct, sym)),
        )


def test_submit_callback_reaches_the_guarded_gateway_not_wrongmode(
    tmp_path, trading_enabled, monkeypatch, authorize_full_live
):
    """The enabled runtime reaches submit_guarded with durable identity."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from src.core.execution_mode import ExecutionLease
    from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
    from src.services.execution_command_gateway import ExecutionCommandGateway
    from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
    from src.services.execution_ownership_repository import assign_ownership
    from src.services.mutation_budget_protocol import AllowAllMutationBudget
    from fakes.fake_execution_broker import FakeExecutionBroker

    engine = create_engine(f"sqlite:///{tmp_path / 'guarded.db'}", future=True, poolclass=NullPool)
    fake_broker = FakeExecutionBroker()
    lease = ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=1)
    guarded_gateway = ExecutionCommandGateway(
        real_broker=fake_broker, engine=engine, mode_override=True,
        lease_protocol=FakeExecutionLeaseProtocol(current=lease),
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda environment, account_no: 100_000.0,
    )
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD", account_no="1", symbol="AAPL", owner=ExecutionOwner.KANBAN,
            strategy_instance_id="orb",
        ),
    )

    card = _card()
    persisted = []
    monkeypatch.setattr(
        runtime_module.execution_config,
        "is_buyboard_engine_enabled",
        lambda: True,
    )
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        account_equity_provider=lambda env, acct: 10_000.0,
        card_lookup=lambda env, acct, sym: card,
        broker=guarded_gateway,
        strategy_instance_id="orb",
        execution_lease=lease,
        persist_card_before_execution=lambda current: persisted.append(current.to_dict()),
    )
    authorize_full_live()

    fake_broker.queue_acceptance(broker_order_id="B-GUARDED-1")
    result = runtime.entry_attempt_manager._submit_order(
        environment="PROD", account_no="1", symbol="AAPL", side=OrderSide.BUY,
        intent=OrderIntent.ENTRY, quantity=10, limit_price=100.0, exchange="NASD",
        attempt_group_id="g1", attempt_number=1, attempt_deadline_at=None,
        capital_reservation_id="",
    )
    assert result.status == UnifiedExecutionStatus.ACKNOWLEDGED
    assert persisted and persisted[0]["entry_client_order_id"]
    assert len(fake_broker.submit_calls) == 1


def test_guarded_runtime_submits_exact_orb_high_once_on_fresh_trade_drain(
    tmp_path, trading_enabled, monkeypatch, authorize_full_live
):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from fakes.fake_execution_broker import FakeExecutionBroker
    from src.api.kis_websocket import KisWsSystemFrame
    from src.core.execution_mode import ExecutionLease
    from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
    from src.services.execution_command_gateway import ExecutionCommandGateway
    from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
    from src.services.execution_ownership_repository import assign_ownership
    from src.services.kis_realtime_market_data import (
        KisRealtimeMarketDataService,
        QUOTE_TR_ID,
        SubscriptionPriority,
        TRADE_TR_ID,
    )
    from src.services.mutation_budget_protocol import AllowAllMutationBudget

    class Transport:
        def on_data(self, callback):
            self.data_callback = callback

        def on_ack(self, callback):
            self.ack_callback = callback

        def on_connection(self, callback):
            self.connection_callback = callback

        def subscribe(self, subscriptions):
            return None

        def unsubscribe(self, subscriptions):
            return None

        def is_connected(self):
            return True

        reconnect_count = 0
        malformed_frame_count = 0

    observed_at = dt.datetime.now(dt.timezone.utc)
    transport = Transport()
    market_data = KisRealtimeMarketDataService(
        transport=transport,
        symbol_key_resolver=lambda symbol, channel: f"D{symbol}",
        trade_capacity=1,
        quote_capacity=1,
        clock=lambda: observed_at,
    )
    market_data._on_connection(True, "", 1)
    market_data.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.BUY_TODAY},
        quote_priorities={"AAPL": SubscriptionPriority.BUY_TODAY},
    )
    for tr_id in (TRADE_TR_ID, QUOTE_TR_ID):
        market_data._on_ack(
            KisWsSystemFrame(
                tr_id=tr_id,
                tr_key="DAAPL",
                accepted=True,
                message="SUBSCRIBE SUCCESS",
            )
        )

    database = create_engine(
        f"sqlite:///{tmp_path / 'upward-extreme.db'}",
        future=True,
        poolclass=NullPool,
    )
    broker = FakeExecutionBroker()
    broker.queue_acceptance(broker_order_id="B-UPWARD-EXTREME")
    lease = ExecutionLease(device_id="dev-1", lease_token="tok-1", lease_epoch=1)
    gateway = ExecutionCommandGateway(
        real_broker=broker,
        engine=database,
        mode_override=True,
        lease_protocol=FakeExecutionLeaseProtocol(current=lease),
        mutation_budget=AllowAllMutationBudget(),
        buying_power_provider=lambda *_: 100_000.0,
    )
    assign_ownership(
        database,
        ExecutionOwnership(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="orb",
        ),
    )
    card = _card(
        entry_trigger=104.0,
        entry_execution_price=104.0,
        entry_orb_high=104.0,
        breakout_price=100.0,
        entry_breakout_trigger=104.0,
        entry_range_closed_at=observed_at - dt.timedelta(minutes=1),
        entry_candidate_created_at=observed_at - dt.timedelta(minutes=1),
        entry_orb_score=50.0,
        entry_score_version="ORB_POSITION_SCORE_V1",
        planned_quantity=10,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
    )
    monkeypatch.setattr(runtime_module.execution_config, "is_buyboard_engine_enabled", lambda: True)
    monkeypatch.setattr(
        "src.services.trading_engine.is_buyboard_engine_enabled", lambda: True
    )
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda *_: 100_000.0,
        account_equity_provider=lambda *_: 10_000.0,
        card_lookup=lambda *_: card,
        capital_reservation_engine=database,
        broker=gateway,
        market_data=market_data,
        strategy_instance_id="orb",
        execution_lease=lease,
        persist_card_before_execution=lambda current: None,
    )
    authorize_full_live()
    runtime.trading_engine._clock = lambda: observed_at
    runtime.trading_engine._market_is_open_fn = lambda: True

    assert market_data.ingest_quote(
        QuoteSnapshot(
            symbol="AAPL",
                last_price=105.0,
                bid=104.9,
                ask=105.1,
            broker_event_at=observed_at,
            received_at=observed_at,
            channel=QUOTE_TR_ID,
            payload_fingerprint="quote",
        )
    )
    for index, price in enumerate((100.0, 105.0, 101.0)):
        # Distinct trade IDs/fingerprints establish event identity; keep all
        # three observations on the engine's frozen clock so this remains a
        # freshness test rather than introducing future-dated ticks.
        event_at = observed_at
        assert market_data.ingest_trade(
            QuoteSnapshot(
                symbol="AAPL",
                last_price=price,
                broker_event_at=event_at,
                received_at=event_at,
                channel=TRADE_TR_ID,
                trade_id=str(index),
                payload_fingerprint=f"trade-{index}",
            )
        )

    events = market_data.poll_once()
    assert 105.0 in [event.last_price for event in events]
    assert market_data.entry_quote_ready("AAPL", now=observed_at)
    for event in events:
        runtime.trading_engine.evaluate_entry_quote([card], event)

    assert card.entry_runtime_status == EntryRuntimeStatus.ORDER_PENDING
    assert [call["limit_price"] for call in broker.submit_calls] == [104.0]
    assert card.board_status == BoardStatus.ENTRY_PENDING


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

    monkeypatch.setattr(workflow_module, "submit_guarded_overseas_order", fake_submit_guarded)

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

    assert order.status == UnifiedExecutionStatus.ACKNOWLEDGED
    assert captured["pre_trade_risk_decision"] is not None
    assert captured["pre_trade_risk_decision"].approved is True
    assert captured["strategy_id"] == runtime_module.RISK_STRATEGY_ID
    # Workstream 9 (PR2 third pass): the broker kwarg is now the shared
    # workflow service's source-attribution adapter, not the raw broker
    # directly -- it still delegates every call to the exact same
    # configured broker underneath.
    assert captured["broker"]._gateway is broker


def test_submit_order_uses_verified_nyse_key_instead_of_nasdaq_default(monkeypatch):
    broker = _FakeBroker()
    card = _card(symbol="ESTC", kis_ws_symbol_key="DNYSESTC")
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"],
            account_no=kwargs["account_no"],
            symbol=kwargs["symbol"],
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        workflow_module, "submit_guarded_overseas_order", fake_submit_guarded
    )
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda _env, _acct: 10_000.0,
        card_lookup=lambda _env, _acct, _symbol: card,
        broker=broker,
    )

    runtime.entry_attempt_manager._submit_order(
        environment="PROD",
        account_no="1",
        symbol="ESTC",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=20,
        limit_price=100.0,
        exchange="NASD",
        attempt_group_id="g1",
        attempt_number=1,
        attempt_deadline_at=None,
        capital_reservation_id="",
    )

    assert captured["exchange"] == "NYSE"
    assert captured["pre_trade_risk_decision"].exchange == "NYSE"


def test_execution_exchange_revalidates_estc_against_kis_master(tmp_path):
    master = tmp_path / "us_kis_tickers.csv"
    master.write_text(
        "Symbol,KisSymbol,Exchange\nESTC,ESTC,NYS\n",
        encoding="utf-8",
    )
    store = SimpleNamespace(
        universe_path=master,
        resolve=lambda _symbol: "DNYSESTC",
    )
    market_data = SimpleNamespace(symbol_key_store=store)

    assert runtime_module._execution_exchange_for_card(
        _card(symbol="ESTC", kis_ws_symbol_key="DNYSESTC"),
        market_data,
    ) == "NYSE"


def test_execution_exchange_fails_closed_on_stale_estc_venue(tmp_path):
    master = tmp_path / "us_kis_tickers.csv"
    master.write_text(
        "Symbol,KisSymbol,Exchange\nESTC,ESTC,NYS\n",
        encoding="utf-8",
    )
    store = SimpleNamespace(
        universe_path=master,
        resolve=lambda _symbol: "DNASESTC",
    )
    market_data = SimpleNamespace(symbol_key_store=store)

    with pytest.raises(RuntimeError, match="venue is stale"):
        runtime_module._execution_exchange_for_card(
            _card(symbol="ESTC", kis_ws_symbol_key="DNASESTC"),
            market_data,
        )


def test_submit_order_revalidates_capital_percent_against_equity_not_cash(
    monkeypatch,
):
    broker = _FakeBroker()
    card = _card(
        planned_quantity=200,
        entry_orb_low=95.0,
        risk_percent=0.01,
    )
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"],
            account_no=kwargs["account_no"],
            symbol=kwargs["symbol"],
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        workflow_module, "submit_guarded_overseas_order", fake_submit_guarded
    )
    runtime = runtime_module.build_buyboard_runtime(
        # The $20,000 order is affordable, but it is 80% of cash.  Its ORB
        # capital allocation is correctly 20% of the $100,000 equity base.
        buying_power_provider=lambda _env, _acct: 25_000.0,
        account_equity_provider=lambda _env, _acct: 100_000.0,
        card_lookup=lambda _env, _acct, _symbol: card,
        broker=broker,
    )

    result = runtime.entry_attempt_manager._submit_order(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=200,
        limit_price=100.0,
        exchange="NASD",
        attempt_group_id="g1",
        attempt_number=1,
        attempt_deadline_at=None,
        capital_reservation_id="",
    )

    assert result.status == UnifiedExecutionStatus.ACKNOWLEDGED
    assert captured["quantity"] == 200
    assert captured["pre_trade_risk_decision"].approved is True


def test_submit_order_fails_closed_without_positive_fresh_equity(trading_enabled):
    broker = _FakeBroker()
    card = _card(
        planned_quantity=200,
        entry_orb_low=95.0,
        risk_percent=0.01,
    )
    broker_calls = []

    def record_broker_submit(**kwargs):
        broker_calls.append(kwargs)
        raise AssertionError("a missing-equity entry reached the broker")

    broker.submit_order = record_broker_submit
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda _env, _acct: 100_000.0,
        account_equity_provider=lambda _env, _acct: 0.0,
        card_lookup=lambda _env, _acct, _symbol: card,
        broker=broker,
    )

    with pytest.raises(
        PreTradeRiskRejectedError,
        match="Fresh positive account equity is required",
    ):
        runtime.entry_attempt_manager._submit_order(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            quantity=200,
            limit_price=100.0,
            exchange="NASD",
            attempt_group_id="g1",
            attempt_number=1,
            attempt_deadline_at=None,
            capital_reservation_id="",
        )

    assert broker_calls == []


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

    monkeypatch.setattr(workflow_module, "submit_guarded_overseas_order", fake_submit_guarded)

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

    monkeypatch.setattr(workflow_module, "submit_guarded_overseas_order", fake_submit_guarded)

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

    monkeypatch.setattr(workflow_module, "submit_guarded_overseas_order", fake_submit_guarded)

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
        workflow_module,
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
        workflow_module,
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

    monkeypatch.setattr(workflow_module, "submit_guarded_overseas_order", fake_submit_guarded)
    market_data = _market_data_with_quote("AAPL", bid=99.5)

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
        market_data=market_data,
    )
    runtime.trading_engine._market_is_open_fn = lambda: True

    order = runtime.trading_engine._position_callbacks.submit_sell_order(
        environment="PROD", account_no="1", symbol="AAPL", quantity=50, reason="partial_sell",
    )

    assert order.status == UnifiedExecutionStatus.ACKNOWLEDGED
    assert captured["side"] == OrderSide.SELL
    assert captured["intent"] == OrderIntent.PARTIAL_EXIT
    assert captured["limit_price"] == pytest.approx(
        99.5 * (1 - runtime_module.execution_config.SELL_MARKETABLE_DISCOUNT_PCT)
    )  # uses the live bid with the configured bounded collar
    assert captured["quantity"] == 50
    assert "reason" not in captured  # never forwarded to submit_guarded_overseas_order


def test_submit_sell_order_uses_verified_nyse_key(monkeypatch):
    broker = _FakeBroker()
    card = _card(symbol="RNG", kis_ws_symbol_key="DNYSRNG")
    captured = {}

    def fake_submit_guarded(**kwargs):
        captured.update(kwargs)
        return BrokerOrder.create(
            environment=kwargs["environment"],
            account_no=kwargs["account_no"],
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            intent=kwargs["intent"],
            quantity_requested=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=OrderStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        workflow_module, "submit_guarded_overseas_order", fake_submit_guarded
    )
    market_data = _market_data_with_quote("RNG", bid=69.5)
    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda _env, _acct: 100_000.0,
        card_lookup=lambda _env, _acct, _symbol: card,
        broker=broker,
        market_data=market_data,
    )
    runtime.trading_engine._market_is_open_fn = lambda: True

    runtime.trading_engine._position_callbacks.submit_sell_order(
        environment="PROD",
        account_no="1",
        symbol="RNG",
        quantity=10,
        reason="sell_all",
    )

    assert captured["exchange"] == "NYSE"


def test_emergency_sell_without_fresh_bid_uses_bounded_reprice_collars():
    reference = 100.0
    assert runtime_module._marketable_sell_limit_price(
        None,
        quote_is_execution_ready=False,
        last_trusted_price=reference,
        emergency_reprice_attempt=0,
    ) == pytest.approx(
        reference
        * (1 - runtime_module.execution_config.SELL_MARKETABLE_DISCOUNT_PCT)
    )
    assert runtime_module._marketable_sell_limit_price(
        None,
        quote_is_execution_ready=False,
        last_trusted_price=reference,
        emergency_reprice_attempt=2,
    ) == pytest.approx(
        reference
        * (1 - 3 * runtime_module.execution_config.SELL_MARKETABLE_DISCOUNT_PCT)
    )
    assert runtime_module._marketable_sell_limit_price(
        None,
        quote_is_execution_ready=False,
        last_trusted_price=reference,
        emergency_reprice_attempt=100,
    ) == pytest.approx(95.0)


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

    monkeypatch.setattr(workflow_module, "submit_guarded_overseas_order", fake_submit_guarded)
    market_data = _market_data_with_quote("AAPL", last_price=50.0)  # no bid cached

    runtime = runtime_module.build_buyboard_runtime(
        buying_power_provider=lambda env, acct: 100_000.0,
        card_lookup=lambda env, acct, sym: None,
        broker=broker,
        market_data=market_data,
    )
    runtime.trading_engine._market_is_open_fn = lambda: True

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
    runtime.trading_engine._market_is_open_fn = lambda: True
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


# --- Review finding P0-4: cancel an untracked discovered order's ------------
# --- remainder directly by broker_order_id ----------------------------------




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


@pytest.mark.parametrize(
    ("seconds_left", "expected"),
    ((61.0, False), (60.0, True), (0.0, True), (-60.0, True)),
)
def test_eod_processing_window_stays_open_through_market_close(
    monkeypatch, seconds_left, expected
):
    monkeypatch.setattr(runtime_module, "is_nyse_trading_day", lambda: True)
    monkeypatch.setattr(
        runtime_module,
        "seconds_until_regular_session_close",
        lambda: seconds_left,
    )
    assert runtime_module._eod_window_reached() is expected


def test_eod_processing_window_is_closed_on_non_trading_days(monkeypatch):
    monkeypatch.setattr(runtime_module, "is_nyse_trading_day", lambda: False)
    monkeypatch.setattr(
        runtime_module,
        "seconds_until_regular_session_close",
        lambda: -60.0,
    )

    assert runtime_module._eod_window_reached() is False


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
