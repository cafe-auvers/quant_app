from __future__ import annotations

import datetime as dt
import threading

import pytest

from src.api.kis_websocket import KisWsDataFrame, KisWsSubscription, KisWsSystemFrame
from src.core.runtime_safety_audit import (
    ENTRY_READINESS_AUDIT_SOURCE,
    begin_runtime_safety_audit,
)
from src.services.kis_realtime_market_data import (
    ClockHealth,
    FeedChannel,
    KisRealtimeMarketDataService,
    PendingMarketStateAccumulator,
    QUOTE_COLUMNS,
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

    def forget_subscriptions(self, subscriptions):
        forgotten = set(subscriptions)
        self.subscribed = [sub for sub in self.subscribed if sub not in forgotten]

    def is_connected(self):
        return self.connected


def _service(
    *,
    sequences=(),
    trade_capacity=10,
    quote_capacity=10,
    total_capacity=None,
    execution_notice_subscription=None,
    alert=lambda message: None,
    qualification_mode=False,
    symbol_key_resolver=None,
    symbol_key_store=None,
):
    transport = _Transport()
    service = KisRealtimeMarketDataService(
        transport=transport,
        symbol_key_resolver=(
            symbol_key_resolver or (lambda symbol, channel: f"D{symbol}")
        ),
        symbol_key_store=symbol_key_store,
        trade_capacity=trade_capacity,
        quote_capacity=quote_capacity,
        total_capacity=total_capacity,
        confirmed_sequence_channels=sequences,
        execution_notice_subscription=execution_notice_subscription,
        alert=alert,
        clock=lambda: NOW,
        qualification_mode=qualification_mode,
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


def test_missing_display_symbol_key_does_not_block_mapped_critical_feed():
    def resolve(symbol, _channel):
        if symbol == "CDNA":
            raise RuntimeError("No verified key for CDNA")
        return f"D{symbol}"

    service, transport = _service(symbol_key_resolver=resolve)

    service.configure_desired_channels(
        trade_priorities={
            "STIM": SubscriptionPriority.OPEN_POSITION,
            "CDNA": SubscriptionPriority.DISPLAY_ONLY,
        },
        quote_priorities={
            "STIM": SubscriptionPriority.OPEN_POSITION,
            "CDNA": SubscriptionPriority.DISPLAY_ONLY,
        },
    )
    _ack(service, "STIM", "HDFSCNT0")
    _ack(service, "STIM", "HDFSASP0")

    assert {item.symbol for item in transport.subscribed} == {"STIM"}
    state = service.symbol_state("CDNA")
    assert state.trade_configuration_error == "No verified key for CDNA"
    assert state.quote_configuration_error == "No verified key for CDNA"
    metrics = service.health_metrics()
    assert metrics.critical_trade_channels_missing == ()
    assert metrics.critical_quote_channels_missing == ()


def test_missing_critical_symbol_key_blocks_readiness_without_starving_other_feed():
    def resolve(symbol, _channel):
        if symbol == "CDNA":
            raise RuntimeError("No verified key for CDNA")
        return f"D{symbol}"

    service, transport = _service(symbol_key_resolver=resolve)

    service.configure_desired_channels(
        trade_priorities={
            "STIM": SubscriptionPriority.OPEN_POSITION,
            "CDNA": SubscriptionPriority.OPEN_POSITION,
        },
        quote_priorities={
            "STIM": SubscriptionPriority.OPEN_POSITION,
            "CDNA": SubscriptionPriority.OPEN_POSITION,
        },
    )
    _ack(service, "STIM", "HDFSCNT0")
    _ack(service, "STIM", "HDFSASP0")

    assert {item.symbol for item in transport.subscribed} == {"STIM"}
    metrics = service.health_metrics()
    assert metrics.critical_trade_channels_missing == ("CDNA",)
    assert metrics.critical_quote_channels_missing == ("CDNA",)


def test_intraday_symbol_key_addition_subscribes_without_restarting_or_disrupting_existing(
    tmp_path,
):
    from src.services.kis_ws_symbol_keys import (
        KisWsSymbolKeyStore,
        update_symbol_keys_file,
        write_symbol_keys_file,
    )

    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"AAPL": "DAAPL"}, path)
    store = KisWsSymbolKeyStore(path)
    service, transport = _service(
        symbol_key_resolver=lambda symbol, _channel: store.resolve(symbol)
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1, "MSFT": 2},
        quote_priorities={"AAPL": 1, "MSFT": 2},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    _ack(service, "AAPL", "HDFSASP0")
    assert service.symbol_state("MSFT").trade_configuration_error

    update_symbol_keys_file(set_values={"MSFT": "DMSFT"}, path=path)
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1, "MSFT": 2},
        quote_priorities={"AAPL": 1, "MSFT": 2},
    )

    assert {item.symbol for item in transport.subscribed} == {"AAPL", "MSFT"}
    assert transport.unsubscribed == []
    assert service.symbol_state("MSFT").trade_configuration_error == ""
    assert service.symbol_state("MSFT").quote_configuration_error == ""


def test_intraday_plan_addition_provisions_key_from_kis_master_and_subscribes(
    tmp_path,
):
    from src.services.kis_ws_symbol_keys import (
        KisWsSymbolKeyStore,
        read_symbol_keys_file,
        write_symbol_keys_file,
    )

    key_path = tmp_path / "kis_ws_symbol_keys.json"
    master_path = tmp_path / "us_kis_tickers.csv"
    write_symbol_keys_file({"AAPL": "DAAPL"}, key_path)
    master_path.write_text(
        "Symbol,KisSymbol,Exchange,Name,KoreanName,Currency\n"
        "RNG,RNG,NYS,RINGCENTRAL INC,,USD\n",
        encoding="utf-8",
    )
    store = KisWsSymbolKeyStore(
        key_path,
        universe_path=master_path,
        auto_provision=True,
    )
    service, transport = _service(
        symbol_key_resolver=lambda symbol, _channel: store.resolve(symbol)
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={"AAPL": 1},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    _ack(service, "AAPL", "HDFSASP0")

    service.configure_desired_channels(
        trade_priorities={"AAPL": 1, "RNG": 2},
        quote_priorities={"AAPL": 1, "RNG": 2},
    )

    assert read_symbol_keys_file(key_path) == {
        "AAPL": "DAAPL",
        "RNG": "DNYSRNG",
    }
    assert {item.symbol for item in transport.subscribed} == {"AAPL", "RNG"}
    assert transport.unsubscribed == []
    assert service.symbol_state("RNG").trade_configuration_error == ""
    assert service.symbol_state("RNG").quote_configuration_error == ""


def test_canonical_buy_today_key_handoff_materializes_missing_executor_file(tmp_path):
    from src.services.kis_ws_symbol_keys import (
        KisWsSymbolKeyStore,
        read_symbol_keys_file,
    )

    path = tmp_path / "kis_ws_symbol_keys.json"
    store = KisWsSymbolKeyStore(path)
    service, transport = _service(
        symbol_key_resolver=lambda symbol, _channel: store.resolve(symbol),
        symbol_key_store=store,
    )

    service.adopt_canonical_symbol_keys({"LUNG": "DLUNG"})
    service.configure_desired_channels(
        trade_priorities={"LUNG": 1},
        quote_priorities={"LUNG": 1},
    )

    assert read_symbol_keys_file(path) == {"LUNG": "DLUNG"}
    assert {item.symbol for item in transport.subscribed} == {"LUNG"}
    assert service.symbol_state("LUNG").trade_configuration_error == ""
    assert service.symbol_state("LUNG").quote_configuration_error == ""


def test_canonical_buy_today_key_handoff_never_overwrites_local_conflict(tmp_path):
    from src.services.kis_ws_symbol_keys import (
        KisWsSymbolKeyStore,
        read_symbol_keys_file,
        write_symbol_keys_file,
    )

    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"LUNG": "LOCAL_REVIEWED"}, path)
    store = KisWsSymbolKeyStore(path)
    service, _ = _service(
        symbol_key_resolver=lambda symbol, _channel: store.resolve(symbol),
        symbol_key_store=store,
    )

    service.adopt_canonical_symbol_keys(
        {"LUNG": "REMOTE_DIFFERENT", "MCS": "DMCS"}
    )

    assert read_symbol_keys_file(path) == {
        "LUNG": "LOCAL_REVIEWED",
        "MCS": "DMCS",
    }
    assert "LUNG" in service._canonical_symbol_key_conflicts


def test_intraday_removal_does_not_tear_down_an_acked_active_symbol(tmp_path):
    from src.services.kis_ws_symbol_keys import (
        KisWsSymbolKeyStore,
        write_symbol_keys_file,
    )

    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"AAPL": "DAAPL"}, path)
    store = KisWsSymbolKeyStore(path)
    service, transport = _service(
        symbol_key_resolver=lambda symbol, _channel: store.resolve(symbol)
    )
    desired = {"AAPL": SubscriptionPriority.OPEN_POSITION}
    service.configure_desired_channels(
        trade_priorities=desired,
        quote_priorities=desired,
    )
    _ack(service, "AAPL", "HDFSCNT0")
    _ack(service, "AAPL", "HDFSASP0")

    write_symbol_keys_file({}, path)
    service.configure_desired_channels(
        trade_priorities=desired,
        quote_priorities=desired,
    )

    assert transport.unsubscribed == []
    assert service.symbol_state("AAPL").trade_acked
    assert service.symbol_state("AAPL").quote_acked
    assert len(service._deferred_subscription_key_updates) == 2


def test_intraday_active_key_change_waits_until_symbol_leaves_board(tmp_path):
    from src.services.kis_ws_symbol_keys import (
        KisWsSymbolKeyStore,
        write_symbol_keys_file,
    )

    path = tmp_path / "kis_ws_symbol_keys.json"
    write_symbol_keys_file({"AAPL": "DAAPL"}, path)
    store = KisWsSymbolKeyStore(path)
    service, transport = _service(
        trade_capacity=2,
        quote_capacity=0,
        total_capacity=2,
        symbol_key_resolver=lambda symbol, _channel: store.resolve(symbol),
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={},
    )
    _ack(service, "AAPL", "HDFSCNT0")

    write_symbol_keys_file({"AAPL": "DNEW"}, path)
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={},
    )
    assert transport.unsubscribed == []
    assert transport.subscribed[-1].tr_key == "DAAPL"

    service.configure_desired_channels(trade_priorities={}, quote_priorities={})
    assert transport.unsubscribed[-1].tr_key == "DAAPL"
    service._on_ack(
        KisWsSystemFrame(
            tr_id="HDFSCNT0",
            tr_key="DAAPL",
            accepted=True,
            message="UNSUBSCRIBE SUCCESS",
        )
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={},
    )
    assert transport.subscribed[-1].tr_key == "DNEW"


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
    assert service.protocol_metrics_snapshot().duplicate_event_count == 1


def test_protocol_metrics_count_frames_records_schema_and_parser_failures():
    service, _ = _service()
    values = [""] * len(TRADE_COLUMNS)
    values[0] = "BAD"
    service._on_data_frame(
        KisWsDataFrame(
            tr_id="HDFSCNT0",
            record_count=1,
            payload="^".join(values),
            encrypted=False,
            received_at=NOW,
            payload_fingerprint="bad-frame",
        )
    )

    metrics = service.protocol_metrics_snapshot()

    assert dict(metrics.frame_counts_by_tr_id) == {"HDFSCNT0": 1}
    assert dict(metrics.record_counts_by_tr_id) == {"HDFSCNT0": 1}
    assert len(dict(metrics.schema_fingerprints_by_tr_id)["HDFSCNT0"]) == 64
    assert metrics.parser_failure_count == 1


def test_live_overseas_schemas_preserve_rsym_prefix_and_parse_prices():
    assert len(TRADE_COLUMNS) == 26
    assert TRADE_COLUMNS[:3] == ("RSYM", "SYMB", "ZDIV")
    assert len(QUOTE_COLUMNS) == 71
    assert QUOTE_COLUMNS[:3] == ("RSYM", "SYMB", "ZDIV")
    assert QUOTE_COLUMNS[-6:] == (
        "PBID10", "PASK10", "VBID10", "VASK10", "DBID10", "DASK10"
    )

    service, _ = _service()
    service.subscribe(["AAPL"])
    service._event_time_parser = lambda *_args: NOW

    trade = dict.fromkeys(TRADE_COLUMNS, "")
    trade.update(
        RSYM="DNASAAPL",
        SYMB="AAPL",
        ZDIV="4",
        TYMD="20260817",
        XYMD="20260817",
        XHMS="103000",
        KYMD="20260817",
        KHMS="233000",
        LAST="189.12",
        PBID="189.10",
        PASK="189.14",
    )
    service._on_data_frame(
        KisWsDataFrame(
            tr_id="HDFSCNT0",
            record_count=1,
            payload="^".join(trade[column] for column in TRADE_COLUMNS),
            encrypted=False,
            received_at=NOW,
            payload_fingerprint="live-trade-schema",
        )
    )
    service.poll_once()
    parsed_trade = service.latest_quote("AAPL")
    assert parsed_trade is not None
    assert parsed_trade.last_price == pytest.approx(189.12)

    quote = dict.fromkeys(QUOTE_COLUMNS, "")
    quote.update(
        RSYM="DNASAAPL",
        SYMB="AAPL",
        ZDIV="4",
        XYMD="20260817",
        XHMS="103001",
        KYMD="20260817",
        KHMS="233001",
        PBID1="189.11",
        PASK1="189.15",
    )
    service._on_data_frame(
        KisWsDataFrame(
            tr_id="HDFSASP0",
            record_count=1,
            payload="^".join(quote[column] for column in QUOTE_COLUMNS),
            encrypted=False,
            received_at=NOW,
            payload_fingerprint="live-quote-schema",
        )
    )
    service.poll_once()
    parsed_quote = service.latest_quote("AAPL")
    assert parsed_quote is not None
    assert parsed_quote.last_price == pytest.approx(189.12)
    assert parsed_quote.bid == pytest.approx(189.11)
    assert parsed_quote.ask == pytest.approx(189.15)


def test_protocol_latency_statistics_cover_more_than_the_old_rolling_window():
    service, _ = _service()
    for index in range(3_000):
        observed = NOW + dt.timedelta(microseconds=index)
        received = observed + dt.timedelta(milliseconds=index % 500)
        assert service.ingest_trade(
            QuoteSnapshot(
                symbol="AAPL",
                last_price=100,
                broker_event_at=observed,
                received_at=received,
                processed_at=received + dt.timedelta(milliseconds=index % 700),
                channel="HDFSCNT0",
                payload_fingerprint=f"event-{index}",
            )
        )

    metrics = service.protocol_metrics_snapshot()

    assert metrics.receive_lag_sample_count == 3_000
    assert metrics.queue_lag_sample_count == 3_000
    assert metrics.receive_lag_max_ms >= 499
    assert metrics.queue_lag_max_ms >= 699


def test_qualification_silent_channel_probe_uses_live_service_freshness(monkeypatch):
    monkeypatch.setattr(
        "src.services.kis_realtime_market_data.execution_config.BROKER_EVENT_STALE_SECONDS",
        2.0,
    )
    monkeypatch.setattr(
        "src.services.kis_realtime_market_data.execution_config.LOCAL_RECEIVE_STALE_SECONDS",
        2.0,
    )
    service, _ = _service(qualification_mode=True)
    service.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.OPEN_POSITION},
        quote_priorities={},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    assert service.ingest_trade(_event(fingerprint="before-suppression"))

    service.set_qualification_channel_suppressed("AAPL", FeedChannel.TRADE, True)
    assert not service.ingest_trade(
        _event(seconds=1, fingerprint="suppressed-live-event")
    )
    assert service.is_connected()
    assert service.health_metrics(now=NOW + dt.timedelta(seconds=2.1)).stale_symbols == (
        "AAPL",
    )
    with begin_runtime_safety_audit(
        required_sources={ENTRY_READINESS_AUDIT_SOURCE}
    ) as audit:
        audit.begin_stale_entry_probe("AAPL")
        try:
            assert not service.entry_quote_ready(
                "AAPL", now=NOW + dt.timedelta(seconds=2.1)
            )
        finally:
            audit.end_stale_entry_probe("AAPL")
        snapshot = audit.snapshot()

    assert snapshot.initialized
    assert ENTRY_READINESS_AUDIT_SOURCE in snapshot.registered_sources
    assert snapshot.stale_entry_readiness_check_count == 1
    assert snapshot.stale_entry_readiness_rejection_count == 1
    assert snapshot.stale_entry_readiness_allow_count == 0

    service.set_qualification_channel_suppressed("AAPL", FeedChannel.TRADE, False)
    assert service.ingest_trade(
        _event(seconds=2.1, fingerprint="after-suppression")
    )
    assert service.health_metrics(now=NOW + dt.timedelta(seconds=2.1)).stale_symbols == ()


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


def test_confirmed_sequence_channel_rejects_event_without_sequence():
    confirmed, _ = _service(sequences={"HDFSCNT0"})

    assert not confirmed.ingest_trade(_event(sequence=None, fingerprint="missing"))
    assert confirmed.symbol_state("AAPL").clock_health == ClockHealth.SEQUENCE_MISSING


def test_verified_sequence_reset_semantics_clear_only_resetting_channel():
    service, _ = _service(sequences={"HDFSCNT0", "HDFSASP0"})
    service._sequence_reset_by_channel = {
        "HDFSCNT0": "RESET_ON_RECONNECT",
        "HDFSASP0": "CONTINUES_ACROSS_RECONNECT",
    }
    assert service.ingest_trade(_event(sequence=10, fingerprint="trade"))
    assert service.ingest_quote(
        _event(channel="HDFSASP0", sequence=20, fingerprint="quote")
    )

    service._on_connection(False, "forced", 1)
    service._on_connection(True, "", 2)

    assert ("AAPL", FeedChannel.TRADE.value) not in service._last_sequence
    assert service._last_sequence[("AAPL", FeedChannel.QUOTE.value)] == 20


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


def test_stale_maximum_is_not_execution_fresh_when_latest_cache_is_fresh():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.BUY_TODAY},
        quote_priorities={"AAPL": SubscriptionPriority.BUY_TODAY},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    _ack(service, "AAPL", "HDFSASP0")
    assert service.ingest_quote(
        _event(channel="HDFSASP0", fingerprint="fresh-quote")
    )
    assert service.ingest_trade(
        _event(price=105, seconds=-2, fingerprint="stale-maximum")
    )
    assert service.ingest_trade(
        _event(price=101, fingerprint="fresh-latest")
    )

    events = service.poll_once()
    maximum = next(event for event in events if event.last_price == 105)

    assert not maximum.is_execution_fresh(now=NOW)
    assert service.entry_quote_ready("AAPL", now=NOW)
    assert service.latest_quote("AAPL").last_price == 101


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


def test_aggregate_session_capacity_pairs_highest_priority_channels():
    service, transport = _service(
        trade_capacity=10,
        quote_capacity=10,
        total_capacity=3,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1, "MSFT": 2},
        quote_priorities={"AAPL": 1, "MSFT": 2},
    )

    selected = {(sub.tr_id, sub.symbol) for sub in transport.subscribed}
    assert selected == {
        ("HDFSCNT0", "AAPL"),
        ("HDFSASP0", "AAPL"),
        ("HDFSCNT0", "MSFT"),
    }
    assert service.symbol_state("MSFT").quote_rejected_due_to_capacity


def test_execution_notice_consumes_one_aggregate_kis_session_slot():
    notice = KisWsSubscription("H0GSCNI0", "HTS_REDACTED")
    service, transport = _service(
        trade_capacity=10,
        quote_capacity=10,
        total_capacity=3,
        execution_notice_subscription=notice,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1, "MSFT": 2},
        quote_priorities={"AAPL": 1, "MSFT": 2},
    )

    selected = {(sub.tr_id, sub.symbol) for sub in transport.subscribed}
    assert ("H0GSCNI0", "") in selected
    assert ("HDFSCNT0", "AAPL") in selected
    assert ("HDFSASP0", "AAPL") in selected
    assert len(selected) == 3
    assert service.symbol_state("MSFT").trade_rejected_due_to_capacity
    assert service.symbol_state("MSFT").quote_rejected_due_to_capacity


def test_zero_aggregate_capacity_blocks_execution_notice_and_market_data():
    notice = KisWsSubscription("H0GSCNI0", "HTS_REDACTED")
    service, transport = _service(
        trade_capacity=10,
        quote_capacity=10,
        total_capacity=0,
        execution_notice_subscription=notice,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={"AAPL": 1},
    )

    assert transport.subscribed == []
    state = service.symbol_state("AAPL")
    assert state.trade_rejected_due_to_capacity
    assert state.quote_rejected_due_to_capacity


def test_capacity_above_credential_verified_kis_limit_is_rejected():
    with pytest.raises(ValueError, match="credential-verified limit of 41"):
        _service(
            trade_capacity=41,
            quote_capacity=41,
            total_capacity=42,
        )


def test_pending_subscribe_consumes_capacity_and_ack_makes_it_active():
    service, _ = _service(
        trade_capacity=2,
        quote_capacity=0,
        total_capacity=1,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={},
    )

    pending = service.subscription_capacity_snapshot()
    assert pending.pending_subscribe_count == 1
    assert pending.active_count == 0
    assert pending.occupied_count == 1
    assert pending.available_count == 0

    _ack(service, "AAPL", "HDFSCNT0")

    active = service.subscription_capacity_snapshot()
    assert active.pending_subscribe_count == 0
    assert active.active_count == 1
    assert active.occupied_count == 1


def test_subscribe_nack_releases_slot_and_promotes_next_desired_key():
    service, transport = _service(
        trade_capacity=2,
        quote_capacity=0,
        total_capacity=1,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1, "MSFT": 2},
        quote_priorities={},
    )
    assert transport.subscribed[-1].symbol == "AAPL"

    service._on_ack(
        KisWsSystemFrame(
            tr_id="HDFSCNT0",
            tr_key="DAAPL",
            accepted=False,
            message="MAX SUBSCRIBE OVER",
        )
    )

    assert transport.subscribed[-1].symbol == "MSFT"
    snapshot = service.subscription_capacity_snapshot()
    assert snapshot.pending_subscribe_count == 1
    assert snapshot.occupied_count == 1
    assert service.symbol_state("AAPL").trade_error == "MAX SUBSCRIBE OVER"
    assert not service.symbol_state("MSFT").trade_rejected_due_to_capacity


def test_unsubscribe_ack_releases_slot_before_replacement_subscribe():
    service, transport = _service(
        trade_capacity=2,
        quote_capacity=0,
        total_capacity=1,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={},
    )
    _ack(service, "AAPL", "HDFSCNT0")

    service.configure_desired_channels(
        trade_priorities={"MSFT": 1},
        quote_priorities={},
    )

    waiting = service.subscription_capacity_snapshot()
    assert waiting.pending_unsubscribe_count == 1
    assert waiting.available_count == 0
    assert transport.unsubscribed[-1].symbol == "AAPL"
    assert all(sub.symbol != "MSFT" for sub in transport.subscribed)

    service._on_ack(
        KisWsSystemFrame(
            tr_id="HDFSCNT0",
            tr_key="DAAPL",
            accepted=True,
            is_unsubscribe=True,
            message="UNSUBSCRIBE SUCCESS",
        )
    )

    assert transport.subscribed[-1].symbol == "MSFT"
    promoted = service.subscription_capacity_snapshot()
    assert promoted.pending_unsubscribe_count == 0
    assert promoted.pending_subscribe_count == 1
    assert promoted.occupied_count == 1


def test_reconnect_clears_session_ack_state_and_preserves_replay_intent():
    service, _ = _service(
        trade_capacity=1,
        quote_capacity=0,
        total_capacity=1,
    )
    service.configure_desired_channels(
        trade_priorities={"AAPL": 1},
        quote_priorities={},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    assert service.subscription_capacity_snapshot().active_count == 1

    service._on_connection(False, "forced", 1)
    disconnected = service.subscription_capacity_snapshot()
    assert disconnected.occupied_count == 0
    assert disconnected.reconnect_replay_count == 1
    assert not service.symbol_state("AAPL").trade_acked

    service._on_connection(True, "", 2)
    replayed = service.subscription_capacity_snapshot()
    assert replayed.pending_subscribe_count == 1
    assert replayed.active_count == 0
    assert replayed.reconnect_replay_count == 1
    assert service.symbol_state("AAPL").reconnect_generation == 2


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
    assert detached.pending.breached_stop_versions == {(old.card_key, "old")}
    assert current.stop_rules == (new,)
    # The v1 identity is bucket-level and remains visible across the v2
    # accumulator generation until the engine acknowledges that exact pair.
    assert current.pending.breached_stop_versions == {(old.card_key, "old")}


def test_trade_during_stop_change_is_assigned_once():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    old = StopRule("card", 100, "old")
    new = StopRule("card", 98, "new")
    accumulator.replace_stop_rules("AAPL", [old])
    barrier = threading.Barrier(2)

    def publish():
        barrier.wait()
        accumulator.publish_trade(_event(price=99, fingerprint="race"))

    thread = threading.Thread(target=publish)
    thread.start()
    barrier.wait()
    accumulator.replace_stop_rules("AAPL", [new])
    thread.join()

    drained = accumulator.drain_all()
    event_generations = [
        item.stop_rules for item in drained if item.pending.event_count
    ]
    assert sum(item.pending.event_count for item in drained) == 1
    assert len(event_generations) == 1
    assert event_generations[0] in {(old,), (new,)}


def test_latch_clears_only_on_explicit_engine_acknowledgement():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    accumulator.replace_stop_rules("AAPL", [StopRule("card", 100, "v1")])
    accumulator.publish_trade(_event(price=99, fingerprint="breach"))
    accumulator.drain("AAPL")
    assert accumulator.drain("AAPL").pending.stop_breach_latched
    assert accumulator.acknowledge_breach("AAPL", "card", "v1")
    assert not accumulator.drain("AAPL").pending.stop_breach_latched


def test_stop_rotation_preserves_old_breach_until_exact_acknowledgement():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    accumulator.replace_stop_rules("AAPL", [StopRule("card", 100, "v1")])
    accumulator.publish_trade(_event(price=99, fingerprint="v1-breach"))

    accumulator.replace_stop_rules("AAPL", [StopRule("card", 98, "v2")])
    drained = accumulator.drain_all()

    assert any(
        ("card", "v1") in item.pending.breached_stop_versions
        for item in drained
    )
    assert accumulator.acknowledge_breach("AAPL", "card", "v1")
    assert not accumulator.drain("AAPL").pending.stop_breach_latched


def test_two_stop_versions_can_remain_latched_and_acknowledge_independently():
    accumulator = PendingMarketStateAccumulator(clock=lambda: NOW)
    accumulator.replace_stop_rules("AAPL", [StopRule("card", 100, "v1")])
    accumulator.publish_trade(_event(price=99, fingerprint="v1-breach"))
    accumulator.replace_stop_rules("AAPL", [StopRule("card", 98, "v2")])
    accumulator.publish_trade(
        _event(price=97, seconds=1, fingerprint="v2-breach")
    )

    identities = {
        identity
        for item in accumulator.drain_all()
        for identity in item.pending.breached_stop_versions
    }
    assert identities == {("card", "v1"), ("card", "v2")}

    assert accumulator.acknowledge_breach("AAPL", "card", "v1")
    assert accumulator.drain("AAPL").pending.breached_stop_versions == {
        ("card", "v2")
    }
    assert accumulator.acknowledge_breach("AAPL", "card", "v2")
    assert not accumulator.drain("AAPL").pending.stop_breach_latched


def test_unacknowledged_breach_replay_never_regresses_latest_quote_cache():
    service, _ = _service()
    service.replace_stop_rules("AAPL", [StopRule("card", 100, "v1")])
    service.ingest_trade(_event(price=99, fingerprint="breach"))
    service.poll_once()

    service.ingest_trade(_event(price=105, seconds=1, fingerprint="recovery"))
    replay_window = service.poll_once()

    replay = next(
        event
        for event in replay_window
        if event.last_price == 99
        and ("card", "v1") in event.breached_stop_versions
    )
    assert replay.entry_trigger_eligible is False
    assert service.latest_quote("AAPL").last_price == 105


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
    bad[TRADE_COLUMNS.index("SYMB")] = "BAD"
    good[TRADE_COLUMNS.index("SYMB")] = "GOOD"
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


def test_health_is_critical_when_quote_is_fresh_but_trade_never_arrived():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.BUY_TODAY},
        quote_priorities={"AAPL": SubscriptionPriority.BUY_TODAY},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    _ack(service, "AAPL", "HDFSASP0")
    assert service.ingest_quote(
        _event(channel="HDFSASP0", fingerprint="fresh-quote")
    )

    metrics = service.health_metrics(now=NOW)

    assert metrics.critical_trade_channels_missing == ()
    assert metrics.critical_quote_channels_missing == ()
    assert metrics.stale_symbols == ("AAPL",)


def test_health_is_critical_when_trade_is_fresh_but_quote_is_stale():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.OPEN_POSITION},
        quote_priorities={"AAPL": SubscriptionPriority.OPEN_POSITION},
    )
    _ack(service, "AAPL", "HDFSCNT0")
    _ack(service, "AAPL", "HDFSASP0")
    assert service.ingest_quote(
        _event(
            channel="HDFSASP0",
            seconds=-4,
            fingerprint="stale-quote",
        )
    )
    assert service.ingest_trade(
        _event(channel="HDFSCNT0", fingerprint="fresh-trade")
    )

    assert service.health_metrics(now=NOW).stale_symbols == ("AAPL",)


def test_quiet_acked_symbol_is_available_but_not_execution_ready():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"STIM": SubscriptionPriority.OPEN_POSITION},
        quote_priorities={"STIM": SubscriptionPriority.OPEN_POSITION},
    )
    _ack(service, "STIM", "HDFSCNT0")
    _ack(service, "STIM", "HDFSASP0")

    assert service.is_symbol_feed_available("STIM")
    assert not service.is_symbol_execution_ready("STIM", now=NOW)

    service._on_connection(False, "socket closed", 1)

    assert not service.is_symbol_feed_available("STIM")


def test_removed_stale_symbol_no_longer_contaminates_critical_health():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.OPEN_POSITION},
        quote_priorities={"AAPL": SubscriptionPriority.OPEN_POSITION},
    )
    assert service.health_metrics(now=NOW).stale_symbols == ("AAPL",)

    service.configure_desired_channels(trade_priorities={}, quote_priorities={})

    metrics = service.health_metrics(now=NOW)
    assert metrics.stale_symbols == ()
    assert metrics.last_trade_event is None
    assert metrics.last_quote_event is None


def test_stale_display_only_symbol_does_not_contaminate_execution_health():
    service, _ = _service()
    service.configure_desired_channels(
        trade_priorities={"AAPL": SubscriptionPriority.DISPLAY_ONLY},
        quote_priorities={"AAPL": SubscriptionPriority.DISPLAY_ONLY},
    )

    assert service.health_metrics(now=NOW).stale_symbols == ()


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


def test_single_session_nack_is_exposed_only_as_recent_handoff_evidence():
    service, _ = _service()
    service._on_ack(
        KisWsSystemFrame(
            tr_id="(null)",
            accepted=False,
            message_code="OPSP8996",
            message="ALREADY IN USE appkey",
        )
    )

    assert service.single_session_handoff_conflict_active(now=NOW)
    assert not service.single_session_handoff_conflict_active(
        now=NOW + dt.timedelta(seconds=91)
    )

    service._on_ack(
        KisWsSystemFrame(
            tr_id="HDFSCNT0",
            tr_key="unused",
            accepted=True,
            message="SUBSCRIBE SUCCESS",
        )
    )
    assert not service.single_session_handoff_conflict_active(now=NOW)


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


def test_verified_halt_state_is_exposed_without_inferring_it_from_staleness():
    service, _ = _service()
    assert not service.is_symbol_trading_halted("AAPL")
    service.set_symbol_trading_halted("AAPL", True)
    assert service.is_symbol_trading_halted("AAPL")
    service.set_symbol_trading_halted("AAPL", False)
    assert not service.is_symbol_trading_halted("AAPL")
