"""Bounded timing diagnostics, separate from market-data acceptance rules."""
from __future__ import annotations

import threading
from zoneinfo import ZoneInfo


class MarketDataLatencyWindows:
    """Keep cumulative and broker-session timings separate for both channels."""

    def __init__(self, histogram_factory):
        self._histogram_factory = histogram_factory
        self._lock = threading.Lock()
        self.reset()

    def _series(self):
        return {name: self._histogram_factory()
                for name in ("receive", "parsing", "engine_queue")}

    def reset(self):
        with self._lock:
            self._runtime = self._series()
            self._runtime_channels = {}
            self._session_date = ""
            self._regular = self._series()
            self._regular_channels = {}

    def _channel_series(self, channels, channel):
        if channel not in channels:
            channels[channel] = self._series()
        return channels[channel]

    @staticmethod
    def _channel(quote):
        channel = str(quote.channel).upper()
        return channel if channel in {"HDFSCNT0", "HDFSASP0"} else "OTHER"

    def record_ingress(self, quote, *, regular_session):
        channel = self._channel(quote)
        values = {
            "receive": max(0.0, (quote.received_at - quote.broker_event_at).total_seconds() * 1000),
            "parsing": quote.queue_delay_seconds() * 1000,
        }
        with self._lock:
            channels = self._channel_series(self._runtime_channels, channel)
            for key, value in values.items():
                self._runtime[key].add(value)
                channels[key].add(value)
            if not regular_session:
                return
            session_date = self._session_date_for_quote(quote)
            if session_date > self._session_date:
                self._session_date = session_date
                self._regular = self._series()
                self._regular_channels = {}
            if session_date != self._session_date:
                return
            channels = self._channel_series(self._regular_channels, channel)
            for key, value in values.items():
                self._regular[key].add(value)
                channels[key].add(value)

    @staticmethod
    def _session_date_for_quote(quote):
        return quote.broker_event_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()

    def record_drain(self, quote, *, regular_session):
        channel = self._channel(quote)
        value = quote.queue_delay_seconds() * 1000
        with self._lock:
            channels = self._channel_series(self._runtime_channels, channel)
            self._runtime["engine_queue"].add(value)
            channels["engine_queue"].add(value)
            if regular_session and self._session_date_for_quote(quote) == self._session_date:
                channels = self._channel_series(self._regular_channels, channel)
                self._regular["engine_queue"].add(value)
                channels["engine_queue"].add(value)

    @staticmethod
    def _snapshot_series(series):
        result = {}
        for name, histogram in series.items():
            count, p50, p95, p99, maximum = histogram.snapshot()
            result[name] = {"sample_count": count, "p50_ms": p50, "p95_ms": p95,
                            "p99_ms": p99, "max_ms": maximum}
        return result

    def snapshot(self):
        with self._lock:
            return {
                "runtime": {
                    **self._snapshot_series(self._runtime),
                    "channels": {key: self._snapshot_series(value)
                                 for key, value in sorted(self._runtime_channels.items())},
                },
                "regular_session": {
                    "session_date": self._session_date,
                    "scope": "accepted events with broker timestamps in the regular session",
                    **self._snapshot_series(self._regular),
                    "channels": {key: self._snapshot_series(value)
                                 for key, value in sorted(self._regular_channels.items())},
                },
                "queue_scope": "unique coalesced observations when the engine drains them; historical stop replay is excluded",
            }
