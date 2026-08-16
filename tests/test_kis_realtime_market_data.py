from __future__ import annotations

import datetime as dt
import threading

import pytest

from src.api.kis_websocket import KisWsDataFrame, KisWsSubscription, KisWsSystemFrame
from src.services.kis_realtime_market_data import (
    ClockHealth,
    FeedChannel,
    KisRealtimeMarketDataService,
    PendingMarketStateAccumulator,
    StopRule,
    SubscriptionPriority,
    TRADE_COLUMNS,
)
from src.services.realtime_market_data import QuoteSnapshot


NOW = dt.datetime(2026, 8, 16, 14, 30, tzinfo=dt.timezone.utc)


class _Transport:
    def __init__(self):
        self.data_callbacks = []
        self.ack_callbacks = []
        self.connection_callbacks = []
        self.subscribed = []
        self.unsubscribed = []
        self.connected = True
        self.reconnect_count = 0
        self.malformed_frame_count = 0

    def on_data(self, callback):
        self.data_callbacks.append(callback)

    def on_ack(self, callback):
        self.ack_callbacks.append(callback)

    def on_connection(self, callback):
        self.connection_callbacks.append(callback)

    def subscribe(self, subscriptions):
        self.subscribed.extend(subscriptions)

    def unsubscribe(self, subscriptions):
        self.unsubscribed.extend(subscriptions)

    def is_connected(self):
        return self.connected


def _service(*, sequences=(), trade_capacity=10, quote_capacity=10, alert=lambda message: None):
    transport = _Transport()
    service = KisRealtimeMarketDataService(
        transport=transport,
        symbol_key_resolver=lambda symbol, channel: f"D{symbol}",
        trade_capacity=trade_capacity,
        quote_capacity=quote_capacity,
        confirmed_sequence_channels=sequences,
        alert=alert,
        clock=lambda: NOW,
    )
    service._on_connection(True, "", 1)
    return service, transport


def _event(
    symbol="AAPL",
    price=100.0,
    *,
    channel="HDFSCNT0",
    seconds=0,
    sequence=None,
    trade_id="",
    fingerprint="payload",
    processed_at=None,
):
    observed = NOW + dt.timedelta(seconds=seconds)
    return QuoteSnapshot(
        symbol=symbol,
        last_price=price,
        bid=price - 0.1,
        ask=price + 0.1,
        broker_event_at=observed,
        received_at=observed,
        processed_at=processed_at or observed,
        source="KIS_WS",
        channel=channel,
        sequence=sequence,
        trade_id=trade_id,
        payload_fingerprint=fingerprint,
    )


def _ack(service, symbol, tr_id):
    service._on_ack(
        KisWsSystemFrame(
            tr_id=tr_id,
            tr_key=f"D{symbol}",
            accepted=True,
            message="SUBSCRIBE SUCCESS",
        )
    )


def test_repeated_price_at_a_distinct_trade_is_not_rejected_as_a_duplicate():
    service, _ = _service()
    assert service.ingest_trade(_event(price=100, trade_id="1", fingerprint="one"))
    assert service.ingest_trade(
        _event(price=100, seconds=1, trade_id="2", fingerprint="two")
    )
    assert service.dropped_event_count == 0


def test_exact_duplicate_event_identity_is_coalesced():
    service, _ = _service()
    event = _event(trade_id="1", fingerprint="same")
    assert service.ingest_trade(event)
    assert not service.ingest_trade(event)
    assert service.dropped_event_count == 1


def test_sequence_check_is_only_enforced_for_confirmed_channels():
    unconfirmed, _ = _service()
    assert unconfirmed.ingest_trade(_event(sequence=2, fingerprint="a"))
    assert unconfirmed.ingest_trade(
        _event(sequence=1, seconds=1, fingerprint="b")
    )

    confirmed, _ = _service(sequences={"HDFSCNT0"})
    assert confirmed.ingest_trade(_event(sequence=2, fingerprint="a"))
    assert not confirmed.ingest_trade(
        _event(sequence=1, seconds=1, fingerprint="b")
    )
    assert confirmed.symbol_state("AAPL").clock_health == ClockHealth.SEQUENCE_REGRESSION


def test_future_broker_timestamp_is_rejected(monkeypatch):
    service, _ = _service()
    monkeypatch.setattr(
        "src.services.kis_realtime_market_data.execution_config.MAX_FUTURE_BROKER_EVENT_SECONDS",
        1.0,
    )
    event = QuoteSnapshot(
        symbol="AAPL",
        last_price=100,
        broker_event_at=NOW + dt.timedelta(seconds=5),
        received_at=NOW,
        channel="HDFSCNT0",
        payload_fingerprint="future",
    )
    assert not service.ingest_trade(event)
    assert service.symbol_state("AAPL").clock_health == ClockHealth.FUTURE_TIMESTAMP


def test_recent_event_with_backed_up_queue_is_not_execution_fresh():
    event = _event(processed_at=NOW + dt.timedelta(seconds=5))
    assert not event.is_execution_fresh(
        now=NOW,
        broker_max_age_seconds=10,
        receive_max_age_seconds=10,
        queue_max_delay_seconds=1,
    )


def test_one_healthy_symbol_does_not_mark_a_failing_symbol_ready():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1, "MSFT": 1},
        quote_priorities={"AAPL": 1, "MSFT": 1},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    _ack(service, "AAPL", "HDFSASP0")
    assert service.ingest_trade(_event("AAPL", channel="HDFSCNT0"))
    assert service.ingest_quote(
        _event("AAPL", channel="HDFSASP0", fingerprint="quote")
    )
    service.poll_once()

    assert service.is_symbol_execution_ready("AAPL", now=NOW)
    assert not service.is_symbol_execution_ready("MSFT", now=NOW)


def test_trade_channel_for_open_position_outranks_quote_channel_for_buy_today():
    service, transport = _service(trade_capacity=1, quote_capacity=1)
    service.configure_desired_channels(
        trade_priorities={
            "OPEN": SubscriptionPriority.OPEN_POSITION,
            "BUY": SubscriptionPriority.BUY_TODAY,
        },
        quote_priorities={
            "OPEN": SubscriptionPriority.DISPLAY_ONLY,
            "BUY": SubscriptionPriority.BUY_TODAY,
        },
    )
    selected = {(sub.tr_id, sub.symbol) for sub in transport.subscribed}
    assert ("HDFSCNT0", "OPEN") in selected
    assert ("HDFSASP0", "BUY") in selected


def test_breach_between_two_higher_prices_in_one_drain_window_is_never_lost():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    accumulator.replace_stop_rules("AAPL", [StopRule("PROD:1:AAPL", 100, "1")])
    for offset, price in enumerate((101, 99, 101)):
        accumulator.publish_trade(_event(price=price, seconds=offset, fingerprint=str(offset)))

    state = accumulator.drain("AAPL").pending
    assert state.minimum_trade_price_since_drain == 99
    assert state.maximum_trade_price_since_drain == 101
    assert state.stop_breach_latched


def test_stop_price_change_forces_drain_against_old_version_first():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    old = StopRule("PROD:1:AAPL", 100, "old")
    new = StopRule("PROD:1:AAPL", 98, "new")
    accumulator.replace_stop_rules("AAPL", [old])
    accumulator.publish_trade(_event(price=99, fingerprint="old-event"))

    detached = accumulator.replace_stop_rules("AAPL", [new])
    accumulator.publish_trade(_event(price=99, seconds=1, fingerprint="new-event"))
    current = accumulator.drain("AAPL")

    assert detached.stop_rules == (old,)
    assert detached.pending.breached_stop_versions == {old.card_key: "old"}
    assert current.stop_rules == (new,)
    assert current.pending.breached_stop_versions == {}


def test_trade_during_stop_change_is_assigned_once():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    accumulator.replace_stop_rules("AAPL", [StopRule("card", 100, "old")])
    barrier = threading.Barrier(2)

    def publish():
        barrier.wait()
        accumulator.publish_trade(_event(price=99, fingerprint="race"))

    thread = threading.Thread(target=publish)
    thread.start()
    barrier.wait()
    accumulator.replace_stop_rules("AAPL", [StopRule("card", 98, "new")])
    thread.join()

    drained = accumulator.drain_all()
    assert sum(item.pending.event_count for item in drained) == 1


def test_latch_clears_only_on_explicit_engine_acknowledgement():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    accumulator.replace_stop_rules("AAPL", [StopRule("card", 100, "v1")])
    accumulator.publish_trade(_event(price=99, fingerprint="breach"))
    accumulator.drain("AAPL")
    assert accumulator.drain("AAPL").pending.stop_breach_latched
    assert accumulator.acknowledge_breach("AAPL", "card", "v1")
    assert not accumulator.drain("AAPL").pending.stop_breach_latched


def test_execution_notice_never_substitutes_for_broker_reconciled_fill():
    service, _ = _service()
    notices = []
    service.on_execution_notice(lambda tr_id, fields: notices.append((tr_id, fields)))
    service._on_data_frame(
        KisWsDataFrame(
            tr_id="H0GSCNI0",
            record_count=1,
            payload="ACCT_REDACTED^ORDER_REDACTED",
            encrypted=True,
            received_at=NOW,
            payload_fingerprint="notice",
        )
    )
    assert notices == [("H0GSCNI0", ("ACCT_REDACTED", "ORDER_REDACTED"))]
    assert service.latest_quote("AAPL") is None


def test_one_symbol_parse_failure_does_not_block_next_record(monkeypatch):
    service, _ = _service()
    seen = []

    def ingest(record, frame):
        seen.append(record["SYMB"])
        if record["SYMB"] == "BAD":
            raise ValueError("malformed symbol record")

    monkeypatch.setattr(service, "_ingest_trade_record", ingest)
    bad = [""] * len(TRADE_COLUMNS)
    good = [""] * len(TRADE_COLUMNS)
    bad[0] = "BAD"
    good[0] = "GOOD"
    service._on_data_frame(
        KisWsDataFrame(
            tr_id="HDFSCNT0",
            record_count=2,
            payload="^".join(bad + good),
            encrypted=False,
            received_at=NOW,
            payload_fingerprint="frame",
        )
    )

    assert seen == ["BAD", "GOOD"]


def test_market_data_health_metrics_are_exposed_and_update():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.OPEN_POSITION},
        quote_priorities={"AAPL": SubscriptionPriority.OPEN_POSITION},
    )
    metrics = service.health_metrics(now=NOW)
    assert metrics.ws_connected
    assert metrics.trade_channels_desired == 1
    assert metrics.quote_channels_desired == 1
    assert metrics.critical_trade_channels_missing == ("AAPL",)
    assert metrics.critical_quote_channels_missing == ("AAPL",)


def test_subscription_nack_blocks_symbol_and_alerts():
    alerts = []
    service, _ = _service(alert=alerts.append)
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1}, quote_priorities={"AAPL": 1}
    )
    service._on_ack(
        KisWsSystemFrame(
            tr_id="HDFSCNT0",
            tr_key="DAAPL",
            accepted=False,
            message="capacity exceeded",
        )
    )
    assert not service.is_symbol_execution_ready("AAPL", now=NOW)
    assert alerts and "AAPL" in alerts[0]


def test_subscription_ack_timeout_blocks_only_that_symbol_and_alerts(monkeypatch):
    alerts = []
    service, _ = _service(alert=alerts.append)
    monkeypatch.setattr(
        "src.services.kis_realtime_market_data.execution_config.KIS_WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS",
        1.0,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1}, quote_priorities={"AAPL": 1}
    )

    assert not service.is_symbol_execution_ready(
        "AAPL", now=NOW + dt.timedelta(seconds=2)
    )
    assert "ACK timeout" in service.symbol_state("AAPL").last_error
    assert alerts
