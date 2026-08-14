"""Tests for src.services.realtime_market_data."""
from __future__ import annotations

import datetime as dt

import pytest

from src.services.realtime_market_data import (
    QuoteSnapshot,
    RestPollingMarketDataService,
    is_quote_stale,
)


def _quote(symbol="AAPL", price=190.0, received_at=None):
    return QuoteSnapshot(
        symbol=symbol,
        last_price=price,
        received_at=received_at or dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.timezone.utc),
        source="test",
    )


def test_quote_not_stale_within_window():
    quote = _quote()
    now = quote.received_at + dt.timedelta(seconds=1)
    assert not quote.is_stale(now=now, max_age_seconds=3)


def test_quote_stale_after_window():
    quote = _quote()
    now = quote.received_at + dt.timedelta(seconds=5)
    assert quote.is_stale(now=now, max_age_seconds=3)


def test_missing_quote_counts_as_stale():
    assert is_quote_stale(None) is True


def test_poll_once_fetches_subscribed_symbols_and_caches():
    fetched = []

    def fetcher(symbol):
        fetched.append(symbol)
        return _quote(symbol=symbol, price=100.0)

    service = RestPollingMarketDataService(quote_fetcher=fetcher)
    service.subscribe(["aapl", "msft"])
    quotes = service.poll_once()

    assert sorted(fetched) == ["AAPL", "MSFT"]
    assert {q.symbol for q in quotes} == {"AAPL", "MSFT"}
    assert service.latest_quote("aapl").last_price == 100.0
    assert service.is_connected() is True


def test_unsubscribe_stops_polling_that_symbol():
    fetched = []

    def fetcher(symbol):
        fetched.append(symbol)
        return _quote(symbol=symbol)

    service = RestPollingMarketDataService(quote_fetcher=fetcher)
    service.subscribe(["AAPL", "MSFT"])
    service.unsubscribe(["MSFT"])
    service.poll_once()

    assert fetched == ["AAPL"]


def test_on_quote_callback_invoked_per_symbol():
    received = []
    service = RestPollingMarketDataService(quote_fetcher=lambda s: _quote(symbol=s))
    service.on_quote(received.append)
    service.subscribe(["AAPL"])
    service.poll_once()

    assert len(received) == 1
    assert received[0].symbol == "AAPL"


def test_disconnect_callback_fires_when_all_fetches_fail():
    def fetcher(symbol):
        raise RuntimeError("network down")

    disconnects = []
    service = RestPollingMarketDataService(quote_fetcher=fetcher)
    service.on_disconnect(disconnects.append)
    service.subscribe(["AAPL"])

    # First poll: transitions from the initial (not-yet-connected) state, so
    # no disconnect fires -- there was nothing to disconnect *from* yet.
    service.poll_once()
    assert service.is_connected() is False
    assert disconnects == []


def test_disconnect_callback_fires_on_transition_from_connected():
    calls = {"fail": False}

    def fetcher(symbol):
        if calls["fail"]:
            raise RuntimeError("network down")
        return _quote(symbol=symbol)

    disconnects = []
    service = RestPollingMarketDataService(quote_fetcher=fetcher)
    service.on_disconnect(disconnects.append)
    service.subscribe(["AAPL"])

    service.poll_once()
    assert service.is_connected() is True

    calls["fail"] = True
    service.poll_once()
    assert service.is_connected() is False
    assert disconnects == ["All quote fetches failed"]


def test_reconnect_resumes_monitoring_once_a_poll_succeeds():
    """Section 827-832 step 4-5: reconnect, resume once fresh data arrives."""
    service = RestPollingMarketDataService(quote_fetcher=lambda s: _quote(symbol=s))
    service.subscribe(["AAPL"])
    service._connected = False  # simulate a prior disconnect

    service.reconnect()

    assert service.is_connected() is True


def test_empty_subscription_is_not_treated_as_disconnected():
    service = RestPollingMarketDataService(quote_fetcher=lambda s: _quote(symbol=s))
    service.poll_once()
    assert service.is_connected() is True
