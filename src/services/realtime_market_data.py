"""Real-time market-data service. ``buydashboard_to_kanban.md`` section 19.

No WebSocket/streaming client exists anywhere in this codebase today
(confirmed by a repo-wide search before writing this module) -- the
existing KIS integration is entirely REST/request-based
(:mod:`src.services.kis_intraday_provider`), matching the 60-second
``QTimer`` polling loop the legacy Buy Dashboard uses. ``RealtimeMarketDataService``
is the interface every consumer (stop-loss checks, entry-trigger
evaluation, the Kanban UI) is written against; ``RestPollingMarketDataService``
is the transport that ships today. A future KIS WebSocket backend (once a
streaming approval key is provisioned for this account -- KIS's overseas
real-time quote feed requires one) can implement the same interface without
touching any consumer.

Wiring a *real* KIS quote fetcher (bid/ask/last) into
``RestPollingMarketDataService`` is intentionally left to the caller
(:mod:`src.services.trading_engine`, Phase 5): no live tick-level quote
endpoint exists in :mod:`src.api` today, only historical/intraday bar
fetches and order-status queries. Callers must use
:func:`src.services.intraday_data_service.fetch_execution_grade_intraday`
(never the display-fallback path) to build that fetcher, per section 21's
KIS-only-for-execution rule.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

from src.core import execution_config

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class QuoteSnapshot:
    """Section 756-764."""

    symbol: str
    last_price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    received_at: datetime = field(default_factory=_utc_now)
    source: str = ""
    sequence: Optional[int] = None

    def age_seconds(self, *, now: Optional[datetime] = None) -> float:
        reference = now or _utc_now()
        return (reference - self.received_at).total_seconds()

    def is_stale(
        self, *, now: Optional[datetime] = None, max_age_seconds: Optional[float] = None
    ) -> bool:
        max_age = (
            max_age_seconds
            if max_age_seconds is not None
            else execution_config.QUOTE_STALE_AFTER_SECONDS
        )
        return self.age_seconds(now=now) > max_age


def is_quote_stale(
    quote: Optional[QuoteSnapshot], *, now: Optional[datetime] = None
) -> bool:
    """A missing quote counts as stale (section 783's
    ``QUOTE_STALE_AFTER_SECONDS`` has nothing to measure against)."""
    if quote is None:
        return True
    return quote.is_stale(now=now)


QuoteCallback = Callable[[QuoteSnapshot], None]
DisconnectCallback = Callable[[str], None]


class RealtimeMarketDataService:
    """Interface, section 747-752. Every method raises
    ``NotImplementedError`` here -- concrete backends implement all of it.
    """

    def subscribe(self, symbols: Iterable[str]) -> None:
        raise NotImplementedError

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        raise NotImplementedError

    def on_quote(self, callback: QuoteCallback) -> None:
        raise NotImplementedError

    def on_disconnect(self, callback: DisconnectCallback) -> None:
        raise NotImplementedError

    def reconnect(self) -> None:
        raise NotImplementedError

    def latest_quote(self, symbol: str) -> Optional[QuoteSnapshot]:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError


class InMemoryQuoteCache:
    """Thread-safe last-quote-per-symbol cache any backend can share."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: Dict[str, QuoteSnapshot] = {}

    def update(self, quote: QuoteSnapshot) -> None:
        with self._lock:
            self._quotes[quote.symbol.upper()] = quote

    def get(self, symbol: str) -> Optional[QuoteSnapshot]:
        with self._lock:
            return self._quotes.get(str(symbol or "").upper())

    def all(self) -> Dict[str, QuoteSnapshot]:
        with self._lock:
            return dict(self._quotes)


class RestPollingMarketDataService(RealtimeMarketDataService):
    """The transport that ships today: polls ``quote_fetcher`` for every
    subscribed symbol.

    ``poll_once()`` is exposed separately from ``start()``'s background
    thread loop so :mod:`src.services.trading_engine`'s 1-second heartbeat
    (or a test) can drive it deterministically instead of racing a real
    timer thread -- consistent with how :mod:`src.services.entry_attempt_manager`
    is heartbeat-driven rather than owning its own timer.
    """

    def __init__(
        self,
        *,
        quote_fetcher: Callable[[str], QuoteSnapshot],
        poll_interval_seconds: Optional[float] = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._quote_fetcher = quote_fetcher
        self._poll_interval_seconds = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else execution_config.FALLBACK_POSITION_PRICE_POLL_SECONDS
        )
        self._clock = clock
        self._cache = InMemoryQuoteCache()
        self._symbols: set[str] = set()
        self._symbols_lock = threading.Lock()
        self._quote_callbacks: List[QuoteCallback] = []
        self._disconnect_callbacks: List[DisconnectCallback] = []
        self._connected = False
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    def subscribe(self, symbols: Iterable[str]) -> None:
        with self._symbols_lock:
            self._symbols.update(str(symbol).upper() for symbol in symbols)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        with self._symbols_lock:
            for symbol in symbols:
                self._symbols.discard(str(symbol).upper())

    def subscribed_symbols(self) -> List[str]:
        with self._symbols_lock:
            return sorted(self._symbols)

    def on_quote(self, callback: QuoteCallback) -> None:
        self._quote_callbacks.append(callback)

    def on_disconnect(self, callback: DisconnectCallback) -> None:
        self._disconnect_callbacks.append(callback)

    def latest_quote(self, symbol: str) -> Optional[QuoteSnapshot]:
        return self._cache.get(symbol)

    def is_connected(self) -> bool:
        return self._connected

    def poll_once(self) -> List[QuoteSnapshot]:
        """Fetch every subscribed symbol once, synchronously."""
        symbols = self.subscribed_symbols()
        quotes: List[QuoteSnapshot] = []
        any_success = False
        any_failure = False
        for symbol in symbols:
            try:
                quote = self._quote_fetcher(symbol)
            except Exception as exc:
                any_failure = True
                logger.warning("Quote fetch failed for %s: %s", symbol, exc)
                continue
            any_success = True
            self._cache.update(quote)
            quotes.append(quote)
            for callback in list(self._quote_callbacks):
                try:
                    callback(quote)
                except Exception:
                    logger.exception("on_quote callback failed for %s", symbol)

        was_connected = self._connected
        # Connected means "the last poll pass produced at least one usable
        # quote" -- an empty subscription set is not a disconnect (section
        # 827-832's disconnect handling only applies once we're actually
        # trying to watch something).
        self._connected = any_success or (not symbols)
        if was_connected and not self._connected and any_failure:
            for callback in list(self._disconnect_callbacks):
                try:
                    callback("All quote fetches failed")
                except Exception:
                    logger.exception("on_disconnect callback failed")
        return quotes

    def reconnect(self) -> None:
        # Section 827-832 step 4/5: reconnect, then only resume treating the
        # feed as live once a fresh poll actually succeeds.
        self._connected = False
        self.poll_once()

    def start(self) -> None:
        """Optional background loop for standalone (non-heartbeat-driven) use."""
        if self._poll_thread is not None:
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                self.poll_once()
                self._stop_event.wait(self._poll_interval_seconds)

        self._poll_thread = threading.Thread(
            target=_loop, name="RestPollingMarketDataService", daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
