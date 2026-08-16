"""Production KIS real-time market-data service (Workstream 5).

The wire transport is read-only.  Execution notices are surfaced to
observers but never mutate cards, orders, fills, or capital; Workstream 4's
broker reconciliation remains authoritative.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.api.kis_websocket import (
    KisWebSocketClient,
    KisWsDataFrame,
    KisWsSubscription,
    KisWsSystemFrame,
)
from src.api.kis_ws_auth import KisWsApprovalKeyProvider
from src.core import execution_config
from src.services.realtime_market_data import (
    DisconnectCallback,
    InMemoryQuoteCache,
    QuoteCallback,
    QuoteSnapshot,
    RealtimeMarketDataService,
)
from src.utils.market_calendar import is_regular_session_open

logger = logging.getLogger(__name__)

TRADE_TR_ID = "HDFSCNT0"
QUOTE_TR_ID = "HDFSASP0"
EXECUTION_NOTICE_TR_IDS = frozenset({"H0GSCNI0", "H0GSCNI9"})

TRADE_COLUMNS = (
    "SYMB", "ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN",
    "HIGH", "LOW", "LAST", "SIGN", "DIFF", "RATE", "PBID", "PASK",
    "VBID", "VASK", "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP",
)
QUOTE_COLUMNS = (
    "SYMB", "ZDIV", "XYMD", "XHMS", "KYMD", "KHMS", "BVOL", "AVOL",
    "BDVL", "ADVL", "PBID1", "PASK1", "VBID1", "VASK1", "DBID1", "DASK1",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FeedChannel(str, Enum):
    TRADE = "TRADE"
    QUOTE = "QUOTE"


class ClockHealth(str, Enum):
    HEALTHY = "HEALTHY"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    EXCESSIVE_SKEW = "EXCESSIVE_SKEW"
    NON_MONOTONIC = "NON_MONOTONIC"
    SEQUENCE_REGRESSION = "SEQUENCE_REGRESSION"


class SubscriptionPriority(IntEnum):
    CRITICAL_EXIT = 0
    OPEN_POSITION = 1
    ENTRY_PENDING = 2
    BUY_TODAY = 3
    DISPLAY_ONLY = 4


@dataclass
class SymbolFeedState:
    symbol: str
    trade_desired: bool = False
    quote_desired: bool = False
    trade_acked: bool = False
    quote_acked: bool = False
    trade_rejected_due_to_capacity: bool = False
    quote_rejected_due_to_capacity: bool = False
    trade_requested_at: Optional[datetime] = None
    quote_requested_at: Optional[datetime] = None
    last_trade_event_at: Optional[datetime] = None
    last_quote_event_at: Optional[datetime] = None
    last_trade_received_at: Optional[datetime] = None
    last_quote_received_at: Optional[datetime] = None
    last_error: str = ""
    trade_error: str = ""
    quote_error: str = ""
    clock_health: ClockHealth = ClockHealth.HEALTHY
    reconnect_generation: int = 0
    trading_halted: bool = False


@dataclass(frozen=True)
class StopRule:
    card_key: str
    price: float
    version: str


@dataclass
class PendingMarketState:
    latest_trade: Optional[QuoteSnapshot] = None
    latest_quote: Optional[QuoteSnapshot] = None
    minimum_trade: Optional[QuoteSnapshot] = None
    maximum_trade: Optional[QuoteSnapshot] = None
    minimum_trade_price_since_drain: Optional[float] = None
    maximum_trade_price_since_drain: Optional[float] = None
    first_event_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    event_count: int = 0
    stop_breach_latched: bool = False
    breached_stop_version: Optional[str] = None
    breached_stop_versions: set[tuple[str, str]] = field(default_factory=set)
    channel_error: str = ""

    def add_trade(self, quote: QuoteSnapshot, stop_rules: Mapping[str, StopRule]) -> None:
        price = float(quote.last_price)
        self.latest_trade = quote
        if not quote.regular_session:
            self._record_event(quote.received_at)
            return
        if (
            self.minimum_trade_price_since_drain is None
            or price < self.minimum_trade_price_since_drain
        ):
            self.minimum_trade_price_since_drain = price
            self.minimum_trade = quote
        if (
            self.maximum_trade_price_since_drain is None
            or price > self.maximum_trade_price_since_drain
        ):
            self.maximum_trade_price_since_drain = price
            self.maximum_trade = quote
        for card_key, rule in stop_rules.items():
            if price <= rule.price:
                self.breached_stop_versions.add((card_key, rule.version))
        self.stop_breach_latched = bool(self.breached_stop_versions)
        if self.breached_stop_versions:
            self.breached_stop_version = next(iter(self.breached_stop_versions))[1]
        self._record_event(quote.received_at)

    def add_quote(self, quote: QuoteSnapshot) -> None:
        self.latest_quote = quote
        self._record_event(quote.received_at)

    def _record_event(self, observed_at: datetime) -> None:
        self.first_event_at = self.first_event_at or observed_at
        self.last_event_at = observed_at
        self.event_count += 1


@dataclass(frozen=True)
class DetachedMarketState:
    symbol: str
    pending: PendingMarketState
    stop_rules: tuple[StopRule, ...]
    detached_at: datetime
    latch_replay: bool = False


class PendingMarketStateAccumulator:
    """Per-symbol accumulator with a shared feed/stop-version lock."""

    @dataclass
    class _Bucket:
        lock: threading.RLock = field(default_factory=threading.RLock)
        pending: PendingMarketState = field(default_factory=PendingMarketState)
        stop_rules: Dict[str, StopRule] = field(default_factory=dict)
        # A breach belongs to the symbol bucket, not to one accumulator
        # generation.  Stop rotation may detach the generation that first
        # observed it, but only an exact engine acknowledgement removes it.
        pending_breaches: Dict[
            tuple[str, str], tuple[QuoteSnapshot, StopRule]
        ] = field(default_factory=dict)

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._buckets: Dict[str, PendingMarketStateAccumulator._Bucket] = {}
        self._buckets_lock = threading.Lock()
        self._detached: deque[DetachedMarketState] = deque()
        self._detached_lock = threading.Lock()

    def _bucket(self, symbol: str) -> _Bucket:
        key = str(symbol or "").upper()
        with self._buckets_lock:
            return self._buckets.setdefault(key, self._Bucket())

    def publish_trade(self, quote: QuoteSnapshot) -> None:
        bucket = self._bucket(quote.symbol)
        with bucket.lock:
            before = set(bucket.pending.breached_stop_versions)
            bucket.pending.add_trade(quote, bucket.stop_rules)
            for identity in bucket.pending.breached_stop_versions - before:
                card_key, version = identity
                rule = bucket.stop_rules.get(card_key)
                if rule is not None and rule.version == version:
                    bucket.pending_breaches.setdefault(identity, (quote, rule))

    def publish_quote(self, quote: QuoteSnapshot) -> None:
        bucket = self._bucket(quote.symbol)
        with bucket.lock:
            bucket.pending.add_quote(quote)

    def replace_stop_rules(
        self, symbol: str, rules: Iterable[StopRule]
    ) -> Optional[DetachedMarketState]:
        """Atomically detach old-version events, then install new rules."""
        symbol = str(symbol or "").upper()
        bucket = self._bucket(symbol)
        new_rules = {rule.card_key: rule for rule in rules}
        with bucket.lock:
            if bucket.stop_rules == new_rules:
                return None
            detached = DetachedMarketState(
                symbol=symbol,
                pending=bucket.pending,
                stop_rules=tuple(bucket.stop_rules.values()),
                detached_at=self._clock(),
            )
            bucket.stop_rules = new_rules
            bucket.pending = PendingMarketState()
        if detached.pending.event_count:
            with self._detached_lock:
                self._detached.append(detached)
            return detached
        return None

    def drain(self, symbol: str) -> DetachedMarketState:
        symbol = str(symbol or "").upper()
        bucket = self._bucket(symbol)
        with bucket.lock:
            bucket.pending.breached_stop_versions.update(bucket.pending_breaches)
            bucket.pending.stop_breach_latched = bool(
                bucket.pending.breached_stop_versions
            )
            bucket.pending.breached_stop_version = (
                next(iter(bucket.pending.breached_stop_versions))[1]
                if bucket.pending.breached_stop_versions
                else None
            )
            detached = DetachedMarketState(
                symbol=symbol,
                pending=bucket.pending,
                stop_rules=tuple(bucket.stop_rules.values()),
                detached_at=self._clock(),
            )
            bucket.pending = PendingMarketState()
            return detached

    def drain_all(self) -> list[DetachedMarketState]:
        with self._detached_lock:
            detached = list(self._detached)
            self._detached.clear()
        with self._buckets_lock:
            symbols = list(self._buckets)
        detached.extend(self.drain(symbol) for symbol in symbols)

        represented: Dict[str, set[tuple[str, str]]] = {}
        for item in detached:
            bucket = self._bucket(item.symbol)
            with bucket.lock:
                outstanding = dict(bucket.pending_breaches)
            item.pending.breached_stop_versions.intersection_update(outstanding)
            item.pending.stop_breach_latched = bool(
                item.pending.breached_stop_versions
            )
            item.pending.breached_stop_version = (
                next(iter(item.pending.breached_stop_versions))[1]
                if item.pending.breached_stop_versions
                else None
            )
            representatives = {
                quote
                for quote in (
                    item.pending.minimum_trade,
                    item.pending.maximum_trade,
                    item.pending.latest_trade,
                )
                if quote is not None
            }
            for identity in item.pending.breached_stop_versions:
                breach_quote, _ = outstanding[identity]
                if breach_quote in representatives:
                    represented.setdefault(item.symbol, set()).add(identity)

        # If evaluation failed or acknowledgement was skipped after an older
        # generation was drained, replay the exact breaching event and stop
        # version on the next poll.  This is a latch replay, not a broker or
        # market-data duplicate: it stops only after acknowledge_breach().
        for symbol in symbols:
            bucket = self._bucket(symbol)
            with bucket.lock:
                outstanding = dict(bucket.pending_breaches)
            for identity, (quote, rule) in outstanding.items():
                if identity in represented.get(symbol, set()):
                    continue
                detached.append(
                    DetachedMarketState(
                        symbol=symbol,
                        pending=PendingMarketState(
                            latest_trade=quote,
                            minimum_trade=quote,
                            maximum_trade=quote,
                            minimum_trade_price_since_drain=float(quote.last_price),
                            maximum_trade_price_since_drain=float(quote.last_price),
                            first_event_at=quote.received_at,
                            last_event_at=quote.received_at,
                            event_count=1,
                            stop_breach_latched=True,
                            breached_stop_version=identity[1],
                            breached_stop_versions={identity},
                        ),
                        stop_rules=(rule,),
                        detached_at=self._clock(),
                        latch_replay=True,
                    )
                )
        return [item for item in detached if item.pending.event_count or item.pending.stop_breach_latched]

    def acknowledge_breach(self, symbol: str, card_key: str, version: str) -> bool:
        bucket = self._bucket(symbol)
        identity = (card_key, version)
        with bucket.lock:
            if identity not in bucket.pending_breaches:
                return False
            del bucket.pending_breaches[identity]
            bucket.pending.breached_stop_versions.discard(identity)
            bucket.pending.stop_breach_latched = bool(bucket.pending.breached_stop_versions)
            bucket.pending.breached_stop_version = (
                next(iter(bucket.pending.breached_stop_versions))[1]
                if bucket.pending.breached_stop_versions
                else None
            )
        with self._detached_lock:
            for item in self._detached:
                if item.symbol != str(symbol or "").upper():
                    continue
                item.pending.breached_stop_versions.discard(identity)
                item.pending.stop_breach_latched = bool(
                    item.pending.breached_stop_versions
                )
                item.pending.breached_stop_version = (
                    next(iter(item.pending.breached_stop_versions))[1]
                    if item.pending.breached_stop_versions
                    else None
                )
        return True

    def queue_depth(self) -> int:
        with self._detached_lock:
            total = sum(item.pending.event_count for item in self._detached)
        with self._buckets_lock:
            buckets = list(self._buckets.values())
        for bucket in buckets:
            with bucket.lock:
                total += bucket.pending.event_count
        return total


@dataclass(frozen=True)
class MarketDataHealthMetrics:
    ws_connected: bool
    approval_key_age_seconds: Optional[float]
    trade_channels_desired: int
    trade_channels_acked: int
    quote_channels_desired: int
    quote_channels_acked: int
    critical_trade_channels_missing: tuple[str, ...]
    critical_quote_channels_missing: tuple[str, ...]
    stale_symbols: tuple[str, ...]
    last_trade_event: Optional[datetime]
    last_quote_event: Optional[datetime]
    receive_lag_p50_ms: float
    receive_lag_p95_ms: float
    receive_lag_p99_ms: float
    reconnect_count: int
    nack_count: int
    malformed_frame_count: int
    queue_depth: int
    dropped_event_count: int


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


class KisRealtimeMarketDataService(RealtimeMarketDataService):
    """HDFSCNT0/HDFSASP0 service with strict per-symbol readiness."""

    def __init__(
        self,
        *,
        transport: KisWebSocketClient,
        symbol_key_resolver: Callable[[str, FeedChannel], str],
        trade_capacity: int,
        quote_capacity: int,
        confirmed_sequence_channels: Iterable[str] = (),
        execution_notice_subscription: Optional[KisWsSubscription] = None,
        event_time_parser: Optional[
            Callable[[str, str, str, datetime], datetime]
        ] = None,
        regular_session_filter: Callable[[datetime], bool] = is_regular_session_open,
        approval_key_age: Callable[[], Optional[float]] = lambda: None,
        alert: Callable[[str], None] = lambda message: None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._transport = transport
        self._symbol_key_resolver = symbol_key_resolver
        self._trade_capacity = max(0, int(trade_capacity))
        self._quote_capacity = max(0, int(quote_capacity))
        self._confirmed_sequence_channels = {
            str(channel).upper() for channel in confirmed_sequence_channels
        }
        self._execution_notice_subscription = execution_notice_subscription
        self._event_time_parser = event_time_parser or self._parse_us_event_time
        self._regular_session_filter = regular_session_filter
        self._approval_key_age = approval_key_age
        self._alert = alert
        self._clock = clock
        self._cache = InMemoryQuoteCache()
        self._states: Dict[str, SymbolFeedState] = {}
        self._state_lock = threading.RLock()
        self._trade_priorities: Dict[str, int] = {}
        self._quote_priorities: Dict[str, int] = {}
        self._active_subscriptions: Dict[tuple[str, str], KisWsSubscription] = {}
        self._symbol_by_key: Dict[tuple[str, str], str] = {}
        self._quote_callbacks: list[QuoteCallback] = []
        self._disconnect_callbacks: list[DisconnectCallback] = []
        self._notice_callbacks: list[Callable[[str, tuple[str, ...]], None]] = []
        self._accumulator = PendingMarketStateAccumulator(clock=clock)
        self._last_timestamp: Dict[tuple[str, str], datetime] = {}
        self._last_sequence: Dict[tuple[str, str], int] = {}
        self._dedup_seen: set[tuple] = set()
        self._dedup_order: deque[tuple] = deque(maxlen=4096)
        self._receive_lags_ms: deque[float] = deque(maxlen=2048)
        self._connected = False
        self._reconnect_generation = 0
        self.nack_count = 0
        self.dropped_event_count = 0
        self._transport.on_data(self._on_data_frame)
        self._transport.on_ack(self._on_ack)
        self._transport.on_connection(self._on_connection)
        if self._execution_notice_subscription is not None:
            # Supplementary notification channel only. It is intentionally
            # outside trade/quote capacity and cannot update execution state.
            self._transport.subscribe([self._execution_notice_subscription])

    @staticmethod
    def _parse_us_event_time(
        date_text: str, time_text: str, channel: str, received_at: datetime
    ) -> datetime:
        """Official-sample field parser, still gated by live verification."""
        parsed = datetime.strptime(
            f"{str(date_text).strip()}{str(time_text).strip()[:6]}", "%Y%m%d%H%M%S"
        ).replace(tzinfo=ZoneInfo("America/New_York"))
        return parsed.astimezone(timezone.utc)

    def start(self) -> None:
        self._transport.start()

    def stop(self) -> None:
        self._transport.stop()

    def reconnect(self) -> None:
        loop = getattr(self._transport, "_loop", None)
        if loop is not None:
            import asyncio

            asyncio.run_coroutine_threadsafe(self._transport.reconnect(), loop)

    def is_connected(self) -> bool:
        return self._connected and self._transport.is_connected()

    def on_quote(self, callback: QuoteCallback) -> None:
        self._quote_callbacks.append(callback)

    def on_disconnect(self, callback: DisconnectCallback) -> None:
        self._disconnect_callbacks.append(callback)

    def on_execution_notice(self, callback: Callable[[str, tuple[str, ...]], None]) -> None:
        """Notification only; no callback is allowed to project a fill here."""
        self._notice_callbacks.append(callback)

    def latest_quote(self, symbol: str) -> Optional[QuoteSnapshot]:
        return self._cache.get(symbol)

    def subscribed_symbols(self) -> list[str]:
        with self._state_lock:
            return sorted(
                symbol
                for symbol, state in self._states.items()
                if state.trade_desired or state.quote_desired
            )

    def symbol_state(self, symbol: str) -> SymbolFeedState:
        symbol = str(symbol or "").upper()
        with self._state_lock:
            state = self._states.setdefault(symbol, SymbolFeedState(symbol=symbol))
            return replace(state)

    def subscribe(self, symbols: Iterable[str]) -> None:
        # Generic subscribers are display-only. Runtime card-aware callers
        # replace these priorities through configure_desired_channels().
        for symbol in symbols:
            key = str(symbol or "").upper()
            self._trade_priorities.setdefault(key, SubscriptionPriority.DISPLAY_ONLY)
            self._quote_priorities.setdefault(key, SubscriptionPriority.DISPLAY_ONLY)
        self._rebalance_subscriptions()

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        for symbol in symbols:
            key = str(symbol or "").upper()
            self._trade_priorities.pop(key, None)
            self._quote_priorities.pop(key, None)
        self._rebalance_subscriptions()

    def configure_desired_channels(
        self,
        *,
        trade_priorities: Mapping[str, int],
        quote_priorities: Mapping[str, int],
    ) -> None:
        self._trade_priorities = {
            str(symbol).upper(): int(priority)
            for symbol, priority in trade_priorities.items()
        }
        self._quote_priorities = {
            str(symbol).upper(): int(priority)
            for symbol, priority in quote_priorities.items()
        }
        self._rebalance_subscriptions()

    def _selected(self, priorities: Mapping[str, int], capacity: int) -> set[str]:
        return {
            symbol
            for symbol, _ in sorted(priorities.items(), key=lambda item: (item[1], item[0]))[
                :capacity
            ]
        }

    def _rebalance_subscriptions(self) -> None:
        selected_trade = self._selected(self._trade_priorities, self._trade_capacity)
        selected_quote = self._selected(self._quote_priorities, self._quote_capacity)
        desired_subscriptions: Dict[tuple[str, str], KisWsSubscription] = {}
        all_symbols = set(self._trade_priorities) | set(self._quote_priorities)
        with self._state_lock:
            for symbol in set(self._states) | all_symbols:
                state = self._states.setdefault(symbol, SymbolFeedState(symbol=symbol))
                state.trade_desired = symbol in self._trade_priorities
                state.quote_desired = symbol in self._quote_priorities
                state.trade_rejected_due_to_capacity = (
                    state.trade_desired and symbol not in selected_trade
                )
                state.quote_rejected_due_to_capacity = (
                    state.quote_desired and symbol not in selected_quote
                )
                if symbol in selected_trade:
                    key = self._symbol_key_resolver(symbol, FeedChannel.TRADE)
                    sub = KisWsSubscription(TRADE_TR_ID, key, symbol, FeedChannel.TRADE.value)
                    desired_subscriptions[(sub.tr_id, sub.tr_key)] = sub
                else:
                    state.trade_acked = False
                if symbol in selected_quote:
                    key = self._symbol_key_resolver(symbol, FeedChannel.QUOTE)
                    sub = KisWsSubscription(QUOTE_TR_ID, key, symbol, FeedChannel.QUOTE.value)
                    desired_subscriptions[(sub.tr_id, sub.tr_key)] = sub
                else:
                    state.quote_acked = False

        removals = [
            sub for key, sub in self._active_subscriptions.items() if key not in desired_subscriptions
        ]
        additions = [
            sub for key, sub in desired_subscriptions.items() if key not in self._active_subscriptions
        ]
        if removals:
            self._transport.unsubscribe(removals)
        if additions:
            self._transport.subscribe(additions)
            requested_at = self._clock()
            with self._state_lock:
                for sub in additions:
                    state = self._states[sub.symbol]
                    if sub.tr_id == TRADE_TR_ID:
                        state.trade_requested_at = requested_at
                    else:
                        state.quote_requested_at = requested_at
        self._active_subscriptions = desired_subscriptions
        self._symbol_by_key = {
            (sub.tr_id, sub.tr_key): sub.symbol for sub in desired_subscriptions.values()
        }

    def replace_stop_rules(self, symbol: str, rules: Iterable[StopRule]):
        return self._accumulator.replace_stop_rules(symbol, rules)

    def acknowledge_stop_breach(self, symbol: str, card_key: str, version: str) -> bool:
        return self._accumulator.acknowledge_breach(symbol, card_key, version)

    def set_symbol_trading_halted(self, symbol: str, halted: bool) -> None:
        """Apply a verified halt signal without guessing from quote absence."""
        symbol = str(symbol or "").upper()
        with self._state_lock:
            state = self._states.setdefault(symbol, SymbolFeedState(symbol=symbol))
            state.trading_halted = bool(halted)

    def is_symbol_trading_halted(self, symbol: str) -> bool:
        return self.symbol_state(symbol).trading_halted

    def poll_once(self) -> list[QuoteSnapshot]:
        """Drain coalesced states; no network polling occurs."""
        processed_at = self._clock()
        ready: list[QuoteSnapshot] = []
        for detached in self._accumulator.drain_all():
            pending = detached.pending
            overrides = tuple((rule.card_key, rule.price) for rule in detached.stop_rules)
            breach_identities = tuple(sorted(pending.breached_stop_versions))
            representative_trades: list[QuoteSnapshot] = []
            for representative in (pending.minimum_trade, pending.maximum_trade):
                if representative is not None and representative not in representative_trades:
                    representative_trades.append(representative)
            if not representative_trades and pending.latest_trade is not None:
                representative_trades.append(pending.latest_trade)
            representative_trades.sort(
                key=lambda item: (item.received_at, item.broker_event_at)
            )
            for representative in representative_trades:
                trade = replace(
                    representative,
                    processed_at=processed_at,
                    stop_price_overrides=overrides,
                    breached_stop_versions=breach_identities,
                    entry_trigger_eligible=(
                        representative.entry_trigger_eligible
                        and not detached.latch_replay
                    ),
                )
                ready.append(trade)
            # A replay is an engine-delivery guarantee for a previously
            # unacknowledged breach, not a new market observation.  Never let
            # its historical event regress the latest-quote cache or fire a
            # normal quote callback.
            latest = None if detached.latch_replay else (
                pending.latest_quote or pending.latest_trade
            )
            if latest is not None:
                current = self._cache.get(detached.symbol)
                combined = replace(
                    latest,
                    last_price=(
                        pending.latest_trade.last_price
                        if pending.latest_trade is not None
                        else (current.last_price if current is not None else 0.0)
                    ),
                    bid=(
                        pending.latest_quote.bid
                        if pending.latest_quote is not None
                        else (current.bid if current is not None else None)
                    ),
                    ask=(
                        pending.latest_quote.ask
                        if pending.latest_quote is not None
                        else (current.ask if current is not None else None)
                    ),
                    processed_at=processed_at,
                )
                self._cache.update(combined)
                if not ready or ready[-1] != combined:
                    ready.append(combined)
                for callback in list(self._quote_callbacks):
                    callback(combined)
        return ready

    def is_symbol_execution_ready(
        self,
        symbol: str,
        *,
        require_trade: bool = True,
        require_quote: bool = True,
        now: Optional[datetime] = None,
    ) -> bool:
        reference = now or self._clock()
        self._expire_ack_timeouts(reference)
        state = self.symbol_state(symbol)
        if not self.is_connected() or state.last_error or state.clock_health != ClockHealth.HEALTHY:
            return False
        if require_trade and (
            not state.trade_acked
            or state.trade_rejected_due_to_capacity
            or state.last_trade_event_at is None
        ):
            return False
        if require_quote and (
            not state.quote_acked
            or state.quote_rejected_due_to_capacity
            or state.last_quote_event_at is None
        ):
            return False
        quote = self.latest_quote(symbol)
        if quote is None or not quote.is_execution_fresh(now=reference):
            return False
        if require_trade and (
            reference - state.last_trade_event_at
        ).total_seconds() > execution_config.BROKER_EVENT_STALE_SECONDS:
            return False
        if require_quote and (
            reference - state.last_quote_event_at
        ).total_seconds() > execution_config.BROKER_EVENT_STALE_SECONDS:
            return False
        return True

    def entry_quote_ready(
        self, symbol: str, *, now: Optional[datetime] = None
    ) -> bool:
        quote = self.latest_quote(symbol)
        return bool(
            self.is_symbol_execution_ready(
                symbol, require_trade=True, require_quote=True, now=now
            )
            and quote is not None
            and quote.ask is not None
            and quote.ask > 0
            and quote.last_price > 0
        )

    def _on_connection(self, connected: bool, reason: str, generation: int) -> None:
        was_connected = self._connected
        self._connected = connected
        self._reconnect_generation = generation
        with self._state_lock:
            for state in self._states.values():
                state.reconnect_generation = generation
                if not connected:
                    state.trade_acked = False
                    state.quote_acked = False
                    state.trade_requested_at = None
                    state.quote_requested_at = None
                elif generation:
                    state.last_error = ""
                    state.trade_error = ""
                    state.quote_error = ""
                    state.clock_health = ClockHealth.HEALTHY
                    if state.trade_desired and not state.trade_rejected_due_to_capacity:
                        state.trade_requested_at = self._clock()
                    if state.quote_desired and not state.quote_rejected_due_to_capacity:
                        state.quote_requested_at = self._clock()
        if was_connected and not connected:
            for callback in list(self._disconnect_callbacks):
                callback(reason or "KIS WebSocket disconnected")

    def _on_ack(self, frame: KisWsSystemFrame) -> None:
        symbol = self._symbol_by_key.get((frame.tr_id, frame.tr_key))
        if not symbol:
            return
        channel = FeedChannel.TRADE if frame.tr_id == TRADE_TR_ID else FeedChannel.QUOTE
        with self._state_lock:
            state = self._states[symbol]
            accepted = frame.accepted and not frame.is_unsubscribe
            if channel == FeedChannel.TRADE:
                state.trade_acked = accepted
                state.trade_requested_at = None
                state.trade_error = "" if accepted else frame.message or "subscription NACK"
            else:
                state.quote_acked = accepted
                state.quote_requested_at = None
                state.quote_error = "" if accepted else frame.message or "subscription NACK"
            if not accepted:
                self.nack_count += 1
                state.last_error = state.trade_error or state.quote_error
                self._alert(
                    f"KIS {channel.value} subscription rejected for {symbol}: {state.last_error}"
                )
            elif not state.trade_error and not state.quote_error:
                state.last_error = ""

    def _on_data_frame(self, frame: KisWsDataFrame) -> None:
        try:
            if frame.tr_id == TRADE_TR_ID:
                for record in self._split_records(frame, TRADE_COLUMNS):
                    try:
                        self._ingest_trade_record(record, frame)
                    except Exception:
                        logger.exception(
                            "KIS trade record dropped without blocking later records"
                        )
            elif frame.tr_id == QUOTE_TR_ID:
                for record in self._split_records(frame, QUOTE_COLUMNS):
                    try:
                        self._ingest_quote_record(record, frame)
                    except Exception:
                        logger.exception(
                            "KIS quote record dropped without blocking later records"
                        )
            elif frame.tr_id in EXECUTION_NOTICE_TR_IDS:
                fields = tuple(frame.payload.split("^"))
                for callback in list(self._notice_callbacks):
                    callback(frame.tr_id, fields)
            else:
                logger.debug("Ignoring unsupported KIS realtime TR ID %s", frame.tr_id)
        except Exception:
            # One channel/symbol parse failure must not poison dispatch of a
            # later frame for another symbol.
            logger.exception("KIS realtime frame parse failed for %s", frame.tr_id)

    @staticmethod
    def _split_records(frame: KisWsDataFrame, columns: Sequence[str]) -> list[dict[str, str]]:
        values = frame.payload.split("^")
        width = len(columns)
        if len(values) != frame.record_count * width:
            raise ValueError(
                f"{frame.tr_id} expected {frame.record_count * width} fields, got {len(values)}"
            )
        return [
            dict(zip(columns, values[offset : offset + width]))
            for offset in range(0, len(values), width)
        ]

    def _resolve_symbol(self, tr_id: str, raw_symbol: str) -> str:
        raw = str(raw_symbol or "").upper()
        candidates = {
            symbol
            for (candidate_tr, _), symbol in self._symbol_by_key.items()
            if candidate_tr == tr_id and raw.endswith(symbol)
        }
        if len(candidates) == 1:
            return next(iter(candidates))
        if raw in self._states:
            return raw
        raise ValueError(f"could not correlate {tr_id} symbol {raw!r} to a desired subscription")

    def _ingest_trade_record(self, record: Mapping[str, str], frame: KisWsDataFrame) -> None:
        symbol = self._resolve_symbol(frame.tr_id, record["SYMB"])
        event_at = self._event_time_parser(
            record["XYMD"], record["XHMS"], frame.tr_id, frame.received_at
        )
        quote = QuoteSnapshot(
            symbol=symbol,
            last_price=float(record["LAST"]),
            bid=float(record["PBID"]) if record["PBID"] else None,
            ask=float(record["PASK"]) if record["PASK"] else None,
            broker_event_at=event_at,
            received_at=frame.received_at,
            processed_at=frame.received_at,
            source="KIS_WS",
            channel=frame.tr_id,
            payload_fingerprint=hashlib.sha256(
                "^".join(record.values()).encode("utf-8")
            ).hexdigest(),
            regular_session=self._regular_session_filter(event_at),
        )
        self.ingest_trade(quote)

    def _ingest_quote_record(self, record: Mapping[str, str], frame: KisWsDataFrame) -> None:
        symbol = self._resolve_symbol(frame.tr_id, record["SYMB"])
        event_at = self._event_time_parser(
            record["XYMD"], record["XHMS"], frame.tr_id, frame.received_at
        )
        current = self._cache.get(symbol)
        quote = QuoteSnapshot(
            symbol=symbol,
            last_price=current.last_price if current is not None else 0.0,
            bid=float(record["PBID1"]) if record["PBID1"] else None,
            ask=float(record["PASK1"]) if record["PASK1"] else None,
            broker_event_at=event_at,
            received_at=frame.received_at,
            processed_at=frame.received_at,
            source="KIS_WS",
            channel=frame.tr_id,
            payload_fingerprint=hashlib.sha256(
                "^".join(record.values()).encode("utf-8")
            ).hexdigest(),
        )
        self.ingest_quote(quote)

    def ingest_trade(self, quote: QuoteSnapshot) -> bool:
        if not self._accept_event(quote, FeedChannel.TRADE):
            return False
        self._accumulator.publish_trade(quote)
        with self._state_lock:
            state = self._states.setdefault(quote.symbol, SymbolFeedState(symbol=quote.symbol))
            state.last_trade_event_at = quote.broker_event_at
            state.last_trade_received_at = quote.received_at
        return True

    def ingest_quote(self, quote: QuoteSnapshot) -> bool:
        if not self._accept_event(quote, FeedChannel.QUOTE):
            return False
        self._accumulator.publish_quote(quote)
        with self._state_lock:
            state = self._states.setdefault(quote.symbol, SymbolFeedState(symbol=quote.symbol))
            state.last_quote_event_at = quote.broker_event_at
            state.last_quote_received_at = quote.received_at
        return True

    def _accept_event(self, quote: QuoteSnapshot, channel: FeedChannel) -> bool:
        now = quote.received_at
        key = (quote.symbol.upper(), channel.value)
        broker_at = quote.broker_event_at
        future = (broker_at - now).total_seconds()
        skew = abs((now - broker_at).total_seconds())
        if future > execution_config.MAX_FUTURE_BROKER_EVENT_SECONDS:
            return self._reject_event(quote.symbol, channel, ClockHealth.FUTURE_TIMESTAMP)
        if skew > execution_config.MAX_BROKER_CLOCK_SKEW_SECONDS:
            return self._reject_event(quote.symbol, channel, ClockHealth.EXCESSIVE_SKEW)
        previous_at = self._last_timestamp.get(key)
        if previous_at is not None and broker_at < previous_at:
            return self._reject_event(quote.symbol, channel, ClockHealth.NON_MONOTONIC)
        if quote.channel.upper() in self._confirmed_sequence_channels and quote.sequence is not None:
            previous_sequence = self._last_sequence.get(key)
            if previous_sequence is not None and quote.sequence <= previous_sequence:
                return self._reject_event(
                    quote.symbol, channel, ClockHealth.SEQUENCE_REGRESSION
                )
        identity = (
            quote.channel,
            quote.sequence,
            quote.trade_id,
            quote.broker_event_at,
            quote.payload_fingerprint,
        )
        if identity in self._dedup_seen:
            self.dropped_event_count += 1
            return False
        if len(self._dedup_order) == self._dedup_order.maxlen:
            self._dedup_seen.discard(self._dedup_order[0])
        self._dedup_order.append(identity)
        self._dedup_seen.add(identity)
        self._last_timestamp[key] = broker_at
        if quote.sequence is not None and quote.channel.upper() in self._confirmed_sequence_channels:
            self._last_sequence[key] = quote.sequence
        self._receive_lags_ms.append(max(0.0, (now - broker_at).total_seconds() * 1000.0))
        return True

    def _reject_event(
        self, symbol: str, channel: FeedChannel, health: ClockHealth
    ) -> bool:
        with self._state_lock:
            state = self._states.setdefault(symbol, SymbolFeedState(symbol=symbol))
            state.clock_health = health
            message = f"{channel.value}:{health.value}"
            state.last_error = message
            if channel == FeedChannel.TRADE:
                state.trade_error = message
            else:
                state.quote_error = message
        return False

    def clear_symbol_error(self, symbol: str) -> None:
        with self._state_lock:
            state = self._states.setdefault(symbol.upper(), SymbolFeedState(symbol=symbol.upper()))
            state.last_error = ""
            state.trade_error = ""
            state.quote_error = ""
            state.clock_health = ClockHealth.HEALTHY

    def _expire_ack_timeouts(self, now: datetime) -> None:
        timeout = execution_config.KIS_WS_SUBSCRIPTION_ACK_TIMEOUT_SECONDS
        alerts = []
        with self._state_lock:
            for state in self._states.values():
                for channel, requested_at, acked in (
                    (FeedChannel.TRADE, state.trade_requested_at, state.trade_acked),
                    (FeedChannel.QUOTE, state.quote_requested_at, state.quote_acked),
                ):
                    if requested_at is None or acked:
                        continue
                    if (now - requested_at).total_seconds() <= timeout:
                        continue
                    message = f"{channel.value} subscription ACK timeout"
                    existing = (
                        state.trade_error
                        if channel == FeedChannel.TRADE
                        else state.quote_error
                    )
                    if existing == message:
                        continue
                    if channel == FeedChannel.TRADE:
                        state.trade_error = message
                    else:
                        state.quote_error = message
                    state.last_error = message
                    alerts.append(
                        f"KIS {channel.value} subscription ACK timed out for {state.symbol}"
                    )
        for message in alerts:
            self._alert(message)

    def health_metrics(self, *, now: Optional[datetime] = None) -> MarketDataHealthMetrics:
        reference = now or self._clock()
        self._expire_ack_timeouts(reference)
        with self._state_lock:
            states = [replace(state) for state in self._states.values()]
        trade_critical = {
            symbol
            for symbol, priority in self._trade_priorities.items()
            if priority < SubscriptionPriority.DISPLAY_ONLY
        }
        quote_critical = {
            symbol
            for symbol, priority in self._quote_priorities.items()
            if priority < SubscriptionPriority.DISPLAY_ONLY
        }
        state_by_symbol = {state.symbol: state for state in states}

        def channel_unavailable(symbol: str, channel: FeedChannel) -> bool:
            state = state_by_symbol.get(symbol, SymbolFeedState(symbol))
            if channel == FeedChannel.TRADE:
                acked = state.trade_acked
                rejected = state.trade_rejected_due_to_capacity
                error = state.trade_error
                broker_event_at = state.last_trade_event_at
                received_at = state.last_trade_received_at
            else:
                acked = state.quote_acked
                rejected = state.quote_rejected_due_to_capacity
                error = state.quote_error
                broker_event_at = state.last_quote_event_at
                received_at = state.last_quote_received_at
            if (
                not acked
                or rejected
                or bool(error)
                or state.clock_health != ClockHealth.HEALTHY
                or broker_event_at is None
                or received_at is None
            ):
                return True
            broker_age = (reference - broker_event_at).total_seconds()
            receive_age = (reference - received_at).total_seconds()
            return not (
                0.0 <= broker_age <= execution_config.BROKER_EVENT_STALE_SECONDS
                and 0.0 <= receive_age <= execution_config.LOCAL_RECEIVE_STALE_SECONDS
            )

        stale = {
            symbol
            for symbol in trade_critical
            if channel_unavailable(symbol, FeedChannel.TRADE)
        } | {
            symbol
            for symbol in quote_critical
            if channel_unavailable(symbol, FeedChannel.QUOTE)
        }
        trade_states = [
            state_by_symbol[symbol]
            for symbol in self._trade_priorities
            if symbol in state_by_symbol
        ]
        quote_states = [
            state_by_symbol[symbol]
            for symbol in self._quote_priorities
            if symbol in state_by_symbol
        ]
        lags = list(self._receive_lags_ms)
        pending_depth = self._accumulator.queue_depth()
        return MarketDataHealthMetrics(
            ws_connected=self.is_connected(),
            approval_key_age_seconds=self._approval_key_age(),
            trade_channels_desired=len(self._trade_priorities),
            trade_channels_acked=sum(state.trade_acked for state in trade_states),
            quote_channels_desired=len(self._quote_priorities),
            quote_channels_acked=sum(state.quote_acked for state in quote_states),
            critical_trade_channels_missing=tuple(
                sorted(
                    symbol
                    for symbol in trade_critical
                    if not state_by_symbol.get(symbol, SymbolFeedState(symbol)).trade_acked
                )
            ),
            critical_quote_channels_missing=tuple(
                sorted(
                    symbol
                    for symbol in quote_critical
                    if not state_by_symbol.get(symbol, SymbolFeedState(symbol)).quote_acked
                )
            ),
            stale_symbols=tuple(sorted(stale)),
            last_trade_event=max(
                (
                    state.last_trade_event_at
                    for state in trade_states
                    if state.last_trade_event_at
                ),
                default=None,
            ),
            last_quote_event=max(
                (
                    state.last_quote_event_at
                    for state in quote_states
                    if state.last_quote_event_at
                ),
                default=None,
            ),
            receive_lag_p50_ms=_percentile(lags, 0.50),
            receive_lag_p95_ms=_percentile(lags, 0.95),
            receive_lag_p99_ms=_percentile(lags, 0.99),
            reconnect_count=self._transport.reconnect_count,
            nack_count=self.nack_count,
            malformed_frame_count=self._transport.malformed_frame_count,
            queue_depth=pending_depth,
            dropped_event_count=self.dropped_event_count,
        )


def build_kis_realtime_market_data_from_environment(
    *,
    environment: str = "PROD",
    critical_alert: Callable[[str], None] = lambda message: None,
) -> KisRealtimeMarketDataService:
    """Compose the live service without starting it.

    Exact symbol keys and channel capacities are live-verified inputs, not
    guessed defaults. Missing values therefore fail closed.
    """
    environment = str(environment or "PROD").upper()
    if environment not in {"PROD", "SIM"}:
        raise ValueError("KIS WebSocket environment must be PROD or SIM")
    if not execution_config.KIS_WS_ENABLED:
        raise RuntimeError("KIS_WS_ENABLED is false")
    if not execution_config.KIS_WS_PROTOCOL_VERIFIED:
        raise RuntimeError(
            "KIS_WS_PROTOCOL_VERIFIED is false; Workstream 0 capability evidence is required"
        )
    prefix = f"KIS_{environment}"
    base_url = os.getenv(f"{prefix}_BASE_URL", "").strip()
    ws_url = os.getenv(f"{prefix}_WS_URL", "").strip()
    app_key = os.getenv(f"{prefix}_APP_KEY", "").strip()
    app_secret = os.getenv(f"{prefix}_APP_SECRET", "").strip()
    try:
        symbol_keys = {
            str(symbol).upper(): str(key).strip()
            for symbol, key in json.loads(
                os.getenv("KIS_WS_SYMBOL_KEYS_JSON", "{}") or "{}"
            ).items()
        }
    except (ValueError, AttributeError) as exc:
        raise ValueError("KIS_WS_SYMBOL_KEYS_JSON must be a JSON object") from exc

    def resolve_key(symbol: str, channel: FeedChannel) -> str:
        key = symbol_keys.get(symbol.upper(), "")
        if not key:
            raise RuntimeError(
                f"No live-verified KIS WebSocket subscription key configured for {symbol}"
            )
        return key

    approval_keys = KisWsApprovalKeyProvider(
        base_url=base_url,
        app_key=app_key,
        app_secret=app_secret,
        ttl_seconds=execution_config.KIS_WS_APPROVAL_KEY_TTL_SECONDS,
        max_retries=execution_config.KIS_WS_AUTH_MAX_RETRIES,
        protocol_verified=execution_config.KIS_WS_PROTOCOL_VERIFIED,
        critical_alert=critical_alert,
    )
    transport = KisWebSocketClient(
        url=ws_url,
        approval_keys=approval_keys,
        reconnect_initial_seconds=execution_config.KIS_WS_RECONNECT_INITIAL_SECONDS,
        reconnect_max_seconds=execution_config.KIS_WS_RECONNECT_MAX_SECONDS,
        reconnect_jitter_seconds=execution_config.KIS_WS_RECONNECT_JITTER_SECONDS,
        critical_alert=critical_alert,
    )
    hts_id = os.getenv("KIS_WS_HTS_ID", "").strip()
    notice_subscription = (
        KisWsSubscription(
            "H0GSCNI0" if environment == "PROD" else "H0GSCNI9",
            hts_id,
            channel="EXECUTION_NOTICE",
        )
        if hts_id
        else None
    )
    return KisRealtimeMarketDataService(
        transport=transport,
        symbol_key_resolver=resolve_key,
        trade_capacity=execution_config.KIS_WS_TRADE_CHANNEL_CAPACITY,
        quote_capacity=execution_config.KIS_WS_QUOTE_CHANNEL_CAPACITY,
        execution_notice_subscription=notice_subscription,
        approval_key_age=approval_keys.approval_key_age_seconds,
        alert=critical_alert,
    )
