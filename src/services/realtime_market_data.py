"""Shared real-time market-data interface and REST diagnostic backend.

The production WebSocket implementation lives in
:mod:`src.services.kis_realtime_market_data`. ``RestPollingMarketDataService``
remains useful for display, diagnostics, and deterministic tests, but the
production composition marks its minute-bar fallback non-execution-grade:
it cannot authorize an entry or impersonate tick-level stop protection.
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
    """Execution-grade quote with broker and local timing kept separate.

    ``broker_event_at`` is the timestamp carried by KIS. ``received_at`` is
    when this process received the frame, and ``processed_at`` is when the
    engine drained it from the accumulator.  Older polling callers omit the
    two new fields; they intentionally fall back to ``received_at`` so this
    additive change does not reinterpret existing PR1-PR3 test fixtures.
    """

    symbol: str
    last_price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    received_at: datetime = field(default_factory=_utc_now)
    broker_event_at: Optional[datetime] = None
    processed_at: Optional[datetime] = None
    source: str = ""
    channel: str = ""
    sequence: Optional[int] = None
    trade_id: str = ""
    payload_fingerprint: str = ""
    # When a stop version changes, the accumulator is detached under the
    # feed's symbol lock and later evaluated against the exact old per-card
    # stop set.  This tuple carries those immutable overrides to the engine.
    stop_price_overrides: tuple[tuple[str, float], ...] = ()
    breached_stop_versions: tuple[tuple[str, str], ...] = ()
    regular_session: bool = True
    # Some events are replayed solely to guarantee delivery of a previously
    # latched protective-stop breach.  They remain valid for exit evaluation,
    # but must never authorize a new entry even when their timestamps still
    # fall inside the execution freshness budgets.
    entry_trigger_eligible: bool = True

    def __post_init__(self) -> None:
        broker_at = self.broker_event_at or self.received_at
        processed_at = self.processed_at or self.received_at
        for name, value in (
            ("received_at", self.received_at),
            ("broker_event_at", broker_at),
            ("processed_at", processed_at),
        ):
            normalized = value
            if normalized.tzinfo is None:
                normalized = normalized.replace(tzinfo=timezone.utc)
            object.__setattr__(self, name, normalized.astimezone(timezone.utc))

    def age_seconds(self, *, now: Optional[datetime] = None) -> float:
        reference = now or _utc_now()
        return (reference - self.received_at).total_seconds()

    def broker_age_seconds(self, *, now: Optional[datetime] = None) -> float:
        reference = now or _utc_now()
        return (reference - self.broker_event_at).total_seconds()

    def queue_delay_seconds(self) -> float:
        return max(0.0, (self.processed_at - self.received_at).total_seconds())

    def is_execution_fresh(
        self,
        *,
        now: Optional[datetime] = None,
        broker_max_age_seconds: Optional[float] = None,
        receive_max_age_seconds: Optional[float] = None,
        queue_max_delay_seconds: Optional[float] = None,
    ) -> bool:
        reference = now or _utc_now()
        broker_budget = (
            execution_config.BROKER_EVENT_STALE_SECONDS
            if broker_max_age_seconds is None
            else broker_max_age_seconds
        )
        receive_budget = (
            execution_config.LOCAL_RECEIVE_STALE_SECONDS
            if receive_max_age_seconds is None
            else receive_max_age_seconds
        )
        queue_budget = (
            execution_config.MAX_MARKET_DATA_QUEUE_DELAY_SECONDS
            if queue_max_delay_seconds is None
            else queue_max_delay_seconds
        )
        return (
            0.0 <= self.broker_age_seconds(now=reference) <= broker_budget
            and 0.0 <= self.age_seconds(now=reference) <= receive_budget
            and self.queue_delay_seconds() <= queue_budget
        )

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

    def is_symbol_execution_ready(
        self,
        symbol: str,
        *,
        require_trade: bool = True,
        require_quote: bool = True,
        now: Optional[datetime] = None,
    ) -> bool:
        """Per-symbol readiness seam used by the execution engine.

        Legacy/test backends inherit the conservative connection+freshness
        implementation.  The KIS backend overrides it with channel ACK,
        timestamp, queue-delay, and channel-error checks.
        """
        quote = self.latest_quote(symbol)
        return self.is_connected() and quote is not None and quote.is_execution_fresh(now=now)

    def poll_once(self) -> List[QuoteSnapshot]:
        """Return events ready for engine evaluation in this heartbeat."""
        return []

    def entry_quote_ready(
        self, symbol: str, *, now: Optional[datetime] = None
    ) -> bool:
        """Whether the backend has everything needed to price a new BUY."""
        return self.is_symbol_execution_ready(symbol, now=now)

    def is_symbol_trading_halted(self, symbol: str) -> bool:
        """Whether a separately verified feed signal says execution is halted.

        Backends that cannot prove halt state return False.  A broker-side
        rejection still remains authoritative; this seam only lets a known
        halt prevent futile emergency submissions while retaining intent.
        """
        return False


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
        execution_grade: bool = True,
    ) -> None:
        self._quote_fetcher = quote_fetcher
        self._poll_interval_seconds = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else execution_config.FALLBACK_POSITION_PRICE_POLL_SECONDS
        )
        self._clock = clock
        self._execution_grade = bool(execution_grade)
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

    def is_symbol_execution_ready(
        self,
        symbol: str,
        *,
        require_trade: bool = True,
        require_quote: bool = True,
        now: Optional[datetime] = None,
    ) -> bool:
        quote = self.latest_quote(symbol)
        return bool(
            self._execution_grade
            and self._connected
            and quote is not None
            and quote.is_execution_fresh(now=now or self._clock())
        )

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
