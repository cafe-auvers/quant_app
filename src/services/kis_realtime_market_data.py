"""Production KIS real-time market-data service (Workstream 5).

The wire transport is read-only.  Execution notices are surfaced to
observers but never mutate cards, orders, fills, or capital; Workstream 4's
broker reconciliation remains authoritative.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.api.kis_websocket import (
    KisWebSocketClient,
    KisWsDataFrame,
    KisWsProtocolOperation,
    KisWsSubscription,
    KisWsSystemFrame,
)
from src.api.kis_ws_auth import KisWsApprovalKeyProvider
from src.core import execution_config
from src.core.runtime_safety_audit import (
    ENTRY_READINESS_AUDIT_SOURCE,
    record_entry_readiness,
    register_runtime_safety_audit_source,
)
from src.services.realtime_market_data import (
    DisconnectCallback,
    InMemoryQuoteCache,
    QuoteCallback,
    QuoteSnapshot,
    RealtimeMarketDataService,
)
from src.services.kis_ws_symbol_keys import KisWsSymbolKeyStore
from src.utils.market_calendar import is_regular_session_open

logger = logging.getLogger(__name__)

register_runtime_safety_audit_source(ENTRY_READINESS_AUDIT_SOURCE)

TRADE_TR_ID = "HDFSCNT0"
QUOTE_TR_ID = "HDFSASP0"
EXECUTION_NOTICE_TR_IDS = frozenset({"H0GSCNI0", "H0GSCNI9"})
KIS_WS_VERIFIED_TOTAL_SUBSCRIPTION_LIMIT = 41

TRADE_COLUMNS = (
    "RSYM", "SYMB", "ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS",
    "OPEN", "HIGH", "LOW", "LAST", "SIGN", "DIFF", "RATE", "PBID",
    "PASK", "VBID", "VASK", "EVOL", "TVOL", "TAMT", "BIVL", "ASVL",
    "STRN", "MTYP",
)
_QUOTE_HEADER_COLUMNS = (
    "RSYM", "SYMB", "ZDIV", "XYMD", "XHMS", "KYMD", "KHMS", "BVOL",
    "AVOL", "BDVL", "ADVL",
)
QUOTE_COLUMNS = _QUOTE_HEADER_COLUMNS + tuple(
    f"{field}{level}"
    for level in range(1, 11)
    for field in ("PBID", "PASK", "VBID", "VASK", "DBID", "DASK")
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
    SEQUENCE_MISSING = "SEQUENCE_MISSING"


class SubscriptionPriority(IntEnum):
    CRITICAL_EXIT = 0
    OPEN_POSITION = 1
    ENTRY_PENDING = 2
    # A price-qualified entry and an armed breakout must win capacity before
    # an ORB that is still forming. Existing positions/orders remain ahead of
    # every new entry.
    ENTRY_READY = 3
    ENTRY_ARMED = 4
    BUY_TODAY = 5
    DISPLAY_ONLY = 6


class SubscriptionSessionStatus(str, Enum):
    PENDING_SUBSCRIBE = "PENDING_SUBSCRIBE"
    ACTIVE = "ACTIVE"
    PENDING_UNSUBSCRIBE = "PENDING_UNSUBSCRIBE"


@dataclass(frozen=True)
class SubscriptionCapacitySnapshot:
    total_capacity: int
    desired_count: int
    reconnect_replay_count: int
    pending_subscribe_count: int
    active_count: int
    pending_unsubscribe_count: int
    occupied_count: int
    available_count: int
    max_occupied_count: int
    execution_notice_desired: bool
    execution_notice_acked: bool


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
    trade_configuration_error: str = ""
    quote_configuration_error: str = ""
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

    def latch_breach(
        self,
        symbol: str,
        card_key: str,
        version: str,
        quote: QuoteSnapshot,
        stop_price: float,
    ) -> bool:
        """Latch a breach discovered during a pending-stop handoff.

        The event may have been observed under a looser old rule, so normal
        ingestion could not classify it as a breach.  Once the runtime checks
        it against the newly durable tighter rule, it receives the same exact
        replay/ack lifecycle as an ingestion-time breach.
        """

        bucket = self._bucket(symbol)
        identity = (str(card_key), str(version))
        rule = StopRule(
            card_key=str(card_key), price=float(stop_price), version=str(version)
        )
        with bucket.lock:
            created = identity not in bucket.pending_breaches
            bucket.pending_breaches.setdefault(identity, (quote, rule))
            bucket.pending.breached_stop_versions.add(identity)
            bucket.pending.stop_breach_latched = True
            bucket.pending.breached_stop_version = identity[1]
        return created

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


@dataclass(frozen=True)
class MarketDataProtocolMetrics:
    frame_counts_by_tr_id: tuple[tuple[str, int], ...]
    record_counts_by_tr_id: tuple[tuple[str, int], ...]
    schema_fingerprints_by_tr_id: tuple[tuple[str, str], ...]
    parser_failure_count: int
    duplicate_event_count: int
    receive_lag_sample_count: int
    receive_lag_p50_ms: float
    receive_lag_p95_ms: float
    receive_lag_p99_ms: float
    receive_lag_max_ms: float
    queue_lag_p50_ms: float
    queue_lag_p95_ms: float
    queue_lag_p99_ms: float
    queue_lag_max_ms: float
    queue_lag_sample_count: int


class _OnlineLatencyHistogram:
    """Session-wide 1 ms histogram with bounded memory and exact maxima."""

    def __init__(self, *, largest_bucket_ms: int = 10_000) -> None:
        self._largest_bucket_ms = int(largest_bucket_ms)
        self._buckets = [0] * (self._largest_bucket_ms + 2)
        self._count = 0
        self._maximum = 0.0
        self._lock = threading.Lock()

    def add(self, value_ms: float) -> None:
        value = max(0.0, float(value_ms))
        bucket = min(int(value), self._largest_bucket_ms + 1)
        with self._lock:
            self._buckets[bucket] += 1
            self._count += 1
            self._maximum = max(self._maximum, value)

    def snapshot(self) -> tuple[int, float, float, float, float]:
        with self._lock:
            buckets = tuple(self._buckets)
            count = self._count
            maximum = self._maximum

        def quantile(fraction: float) -> float:
            if not count:
                return 0.0
            target = max(1, math.ceil(count * fraction))
            seen = 0
            for index, occurrences in enumerate(buckets):
                seen += occurrences
                if seen >= target:
                    return maximum if index > self._largest_bucket_ms else float(index)
            return maximum

        return count, quantile(0.50), quantile(0.95), quantile(0.99), maximum


class KisRealtimeMarketDataService(RealtimeMarketDataService):
    """HDFSCNT0/HDFSASP0 service with strict per-symbol readiness."""

    def __init__(
        self,
        *,
        transport: KisWebSocketClient,
        symbol_key_resolver: Callable[[str, FeedChannel], str],
        symbol_key_store: Optional[KisWsSymbolKeyStore] = None,
        trade_capacity: int,
        quote_capacity: int,
        total_capacity: Optional[int] = None,
        confirmed_sequence_channels: Iterable[str] = (),
        sequence_field_by_channel: Optional[Mapping[str, str]] = None,
        sequence_reset_by_channel: Optional[Mapping[str, str]] = None,
        execution_notice_subscription: Optional[KisWsSubscription] = None,
        event_time_parser: Optional[
            Callable[[str, str, str, datetime], datetime]
        ] = None,
        regular_session_filter: Callable[[datetime], bool] = is_regular_session_open,
        approval_key_age: Callable[[], Optional[float]] = lambda: None,
        alert: Callable[[str], None] = lambda message: None,
        clock: Callable[[], datetime] = _utc_now,
        qualification_mode: bool = False,
    ) -> None:
        self._transport = transport
        self._symbol_key_resolver = symbol_key_resolver
        self.symbol_key_store = symbol_key_store
        self._trade_capacity = max(0, int(trade_capacity))
        self._quote_capacity = max(0, int(quote_capacity))
        # KIS enforces one aggregate session budget across every realtime TR,
        # not an independent budget for each channel. ``None`` preserves the
        # explicitly injected unit-test capacity; the live factory always
        # supplies the fail-closed WS0 value.
        self._total_capacity = (
            self._trade_capacity
            + self._quote_capacity
            + (1 if execution_notice_subscription is not None else 0)
            if total_capacity is None
            else max(0, int(total_capacity))
        )
        if self._total_capacity > KIS_WS_VERIFIED_TOTAL_SUBSCRIPTION_LIMIT:
            raise ValueError(
                "KIS aggregate realtime capacity exceeds the credential-verified "
                f"limit of {KIS_WS_VERIFIED_TOTAL_SUBSCRIPTION_LIMIT}"
            )
        self._confirmed_sequence_channels = {
            str(channel).upper() for channel in confirmed_sequence_channels
        }
        self._sequence_field_by_channel = {
            str(channel).upper(): str(field).upper()
            for channel, field in (sequence_field_by_channel or {}).items()
        }
        self._sequence_reset_by_channel = {
            str(channel).upper(): str(semantics).upper()
            for channel, semantics in (sequence_reset_by_channel or {}).items()
        }
        self._execution_notice_subscription = execution_notice_subscription
        self._event_time_parser = event_time_parser or self._parse_us_event_time
        self._regular_session_filter = regular_session_filter
        self._approval_key_age = approval_key_age
        self._alert = alert
        self._clock = clock
        self._qualification_mode = bool(qualification_mode)
        self._qualification_suppressed_channels: set[tuple[str, str]] = set()
        self._cache = InMemoryQuoteCache()
        self._states: Dict[str, SymbolFeedState] = {}
        self._state_lock = threading.RLock()
        self._trade_priorities: Dict[str, int] = {}
        self._quote_priorities: Dict[str, int] = {}
        self._subscription_resolution_errors: Dict[tuple[str, str], str] = {}
        self._deferred_subscription_key_updates: Dict[tuple[str, str], str] = {}
        self._target_subscriptions: Dict[tuple[str, str], KisWsSubscription] = {}
        # Subscriptions retained by the transport as deterministic reconnect
        # intent. This is not the same thing as an ACKed KIS session slot.
        self._active_subscriptions: Dict[tuple[str, str], KisWsSubscription] = {}
        self._session_subscriptions: Dict[tuple[str, str], KisWsSubscription] = {}
        self._session_status: Dict[tuple[str, str], SubscriptionSessionStatus] = {}
        self._session_nacked: set[tuple[str, str]] = set()
        self._max_session_slots_used = 0
        self._symbol_by_key: Dict[tuple[str, str], str] = {}
        self._quote_callbacks: list[QuoteCallback] = []
        self._disconnect_callbacks: list[DisconnectCallback] = []
        self._notice_callbacks: list[Callable[[str, tuple[str, ...]], None]] = []
        self._session_callbacks: list[
            Callable[[bool, str, int, datetime], None]
        ] = []
        self._operation_callbacks: list[
            Callable[[KisWsProtocolOperation], None]
        ] = []
        self._accumulator = PendingMarketStateAccumulator(clock=clock)
        self._last_timestamp: Dict[tuple[str, str], datetime] = {}
        self._last_sequence: Dict[tuple[str, str], int] = {}
        self._dedup_seen: set[tuple] = set()
        self._dedup_order: deque[tuple] = deque(maxlen=4096)
        self._receive_lags_ms = _OnlineLatencyHistogram()
        self._queue_lags_ms = _OnlineLatencyHistogram()
        self._frame_counts: Dict[str, int] = {}
        self._record_counts: Dict[str, int] = {}
        self._schema_fingerprints: Dict[str, str] = {}
        self._parser_failure_count = 0
        self._duplicate_event_count = 0
        self._connected = False
        self._reconnect_generation = 0
        self.nack_count = 0
        self.dropped_event_count = 0
        self._transport.on_data(self._on_data_frame)
        self._transport.on_ack(self._on_ack)
        self._transport.on_connection(self._on_connection)
        on_operation = getattr(self._transport, "on_operation", None)
        if callable(on_operation):
            on_operation(self._on_protocol_operation)
        self._rebalance_subscriptions()

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

    def on_session(
        self, callback: Callable[[bool, str, int, datetime], None]
    ) -> None:
        """Observe connection generations for read-only qualification evidence."""
        self._session_callbacks.append(callback)

    def on_protocol_operation(
        self, callback: Callable[[KisWsProtocolOperation], None]
    ) -> None:
        self._operation_callbacks.append(callback)

    def _on_protocol_operation(self, operation: KisWsProtocolOperation) -> None:
        for callback in list(self._operation_callbacks):
            try:
                callback(operation)
            except Exception:
                logger.exception("KIS WebSocket protocol audit callback failed")

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
        channel_candidates = []
        resolution_errors: Dict[tuple[str, str], str] = {}
        deferred_key_updates: Dict[tuple[str, str], str] = {}
        # Intraday configuration edits must never break a healthy existing
        # feed.  Pin only subscriptions that the current KIS connection has
        # ACKed (or every reconnect intent while disconnected).  A missing or
        # corrected key for an unsubscribed/NACKed channel can activate on the
        # next cycle without restarting the process.
        with self._state_lock:
            prior_by_symbol_channel = {
                (sub.symbol, sub.channel): sub
                for key, sub in self._active_subscriptions.items()
                if sub.symbol
                and (
                    not self._connected
                    or self._session_status.get(key)
                    == SubscriptionSessionStatus.ACTIVE
                )
            }
        channel_inputs = (
            (self._trade_priorities, self._trade_capacity, 0, FeedChannel.TRADE),
            (self._quote_priorities, self._quote_capacity, 1, FeedChannel.QUOTE),
        )
        for priorities, capacity, channel_order, channel in channel_inputs:
            resolved = []
            for symbol, priority in sorted(
                priorities.items(), key=lambda item: (item[1], item[0])
            ):
                prior = prior_by_symbol_channel.get((symbol, channel.value))
                try:
                    key = str(self._symbol_key_resolver(symbol, channel) or "").strip()
                    if not key:
                        raise RuntimeError(
                            f"No KIS WebSocket subscription key configured for {symbol}"
                        )
                    tr_id = TRADE_TR_ID if channel == FeedChannel.TRADE else QUOTE_TR_ID
                    sub = KisWsSubscription(tr_id, key, symbol, channel.value)
                except Exception as exc:  # one symbol must not starve every feed
                    message = str(exc) or type(exc).__name__
                    if prior is None:
                        resolution_errors[(symbol, channel.value)] = message
                        continue
                    sub = prior
                    deferred_key_updates[(symbol, channel.value)] = (
                        f"{message}; retaining the currently ACKed key until "
                        "this channel is no longer desired"
                    )
                else:
                    if prior is not None and prior.tr_key != sub.tr_key:
                        deferred_key_updates[(symbol, channel.value)] = (
                            "verified key change deferred while the existing "
                            "channel remains ACKed; remove the symbol from the "
                            "active board before retiring its current feed"
                        )
                        sub = prior
                resolved.append((priority, symbol, channel_order, channel, sub))
            channel_candidates.extend(resolved[: max(0, int(capacity))])

        target_subscriptions: Dict[tuple[str, str], KisWsSubscription] = {}
        if (
            self._execution_notice_subscription is not None
            and self._total_capacity > 0
        ):
            notice = self._execution_notice_subscription
            target_subscriptions[(notice.tr_id, notice.tr_key)] = notice
        for _, _, _, _, sub in sorted(channel_candidates):
            target_subscriptions[(sub.tr_id, sub.tr_key)] = sub

        all_symbols = set(self._trade_priorities) | set(self._quote_priorities)
        with self._state_lock:
            for symbol in set(self._states) | all_symbols:
                state = self._states.setdefault(symbol, SymbolFeedState(symbol=symbol))
                state.trade_desired = symbol in self._trade_priorities
                state.quote_desired = symbol in self._quote_priorities
                previous_trade_error = state.trade_configuration_error
                previous_quote_error = state.quote_configuration_error
                state.trade_configuration_error = resolution_errors.get(
                    (symbol, FeedChannel.TRADE.value), ""
                )
                state.quote_configuration_error = resolution_errors.get(
                    (symbol, FeedChannel.QUOTE.value), ""
                )
                if state.trade_configuration_error:
                    state.trade_acked = False
                    state.trade_requested_at = None
                if state.quote_configuration_error:
                    state.quote_acked = False
                    state.quote_requested_at = None
                configuration_error = (
                    state.quote_configuration_error
                    or state.trade_configuration_error
                )
                if configuration_error:
                    state.last_error = configuration_error
                elif state.last_error in {
                    previous_trade_error,
                    previous_quote_error,
                }:
                    state.last_error = ""
            self._target_subscriptions = target_subscriptions
            self._symbol_by_key = {
                key: sub.symbol
                for key, sub in target_subscriptions.items()
                if sub.symbol
            }
            self._reconcile_subscription_target_locked()
            self._refresh_capacity_rejections_locked()

        previous_errors = self._subscription_resolution_errors
        self._subscription_resolution_errors = resolution_errors
        for key, message in sorted(resolution_errors.items()):
            if previous_errors.get(key) != message:
                logger.warning(
                    "KIS WebSocket %s subscription unavailable for %s: %s",
                    key[1],
                    key[0],
                    message,
                )
        for key in sorted(set(previous_errors) - set(resolution_errors)):
            logger.info(
                "KIS WebSocket %s subscription configuration recovered for %s",
                key[1],
                key[0],
            )
        previous_deferred = self._deferred_subscription_key_updates
        self._deferred_subscription_key_updates = deferred_key_updates
        for key, message in sorted(deferred_key_updates.items()):
            if previous_deferred.get(key) != message:
                logger.warning(
                    "KIS WebSocket %s key update deferred for %s: %s",
                    key[1],
                    key[0],
                    message,
                )
        for key in sorted(set(previous_deferred) - set(deferred_key_updates)):
            logger.info(
                "KIS WebSocket %s deferred key update cleared for %s",
                key[1],
                key[0],
            )

    def _reconcile_subscription_target_locked(self) -> None:
        target_keys = set(self._target_subscriptions)
        self._session_nacked.intersection_update(target_keys)

        removals = [
            (key, sub)
            for key, sub in self._active_subscriptions.items()
            if key not in target_keys
        ]
        for key, sub in removals:
            self._transport.unsubscribe([sub])
            del self._active_subscriptions[key]
            self._clear_channel_ack_locked(sub)
            if self._connected and key in self._session_status:
                self._session_subscriptions[key] = sub
                self._session_status[key] = (
                    SubscriptionSessionStatus.PENDING_UNSUBSCRIBE
                )
            else:
                self._session_subscriptions.pop(key, None)
                self._session_status.pop(key, None)

        occupied = (
            len(self._session_status)
            if self._connected
            else len(self._active_subscriptions)
        )
        for key, sub in self._target_subscriptions.items():
            if key in self._active_subscriptions or key in self._session_status:
                continue
            if key in self._session_nacked or occupied >= self._total_capacity:
                continue
            self._transport.subscribe([sub])
            self._active_subscriptions[key] = sub
            occupied += 1
            if self._connected:
                self._session_subscriptions[key] = sub
                self._session_status[key] = (
                    SubscriptionSessionStatus.PENDING_SUBSCRIBE
                )
                self._mark_channel_requested_locked(sub)

        self._max_session_slots_used = max(
            self._max_session_slots_used,
            len(self._session_status),
        )

    def _mark_channel_requested_locked(self, sub: KisWsSubscription) -> None:
        if not sub.symbol:
            return
        state = self._states.setdefault(
            sub.symbol, SymbolFeedState(symbol=sub.symbol)
        )
        requested_at = self._clock()
        if sub.tr_id == TRADE_TR_ID:
            state.trade_acked = False
            state.trade_requested_at = requested_at
        elif sub.tr_id == QUOTE_TR_ID:
            state.quote_acked = False
            state.quote_requested_at = requested_at

    def _clear_channel_ack_locked(self, sub: KisWsSubscription) -> None:
        if not sub.symbol:
            return
        state = self._states.setdefault(
            sub.symbol, SymbolFeedState(symbol=sub.symbol)
        )
        if sub.tr_id == TRADE_TR_ID:
            state.trade_acked = False
            state.trade_requested_at = None
        elif sub.tr_id == QUOTE_TR_ID:
            state.quote_acked = False
            state.quote_requested_at = None

    def _refresh_capacity_rejections_locked(self) -> None:
        selected_trade = {
            sub.symbol
            for sub in self._active_subscriptions.values()
            if sub.tr_id == TRADE_TR_ID
        }
        selected_quote = {
            sub.symbol
            for sub in self._active_subscriptions.values()
            if sub.tr_id == QUOTE_TR_ID
        }
        for state in self._states.values():
            state.trade_rejected_due_to_capacity = (
                state.trade_desired
                and not state.trade_configuration_error
                and state.symbol not in selected_trade
            )
            state.quote_rejected_due_to_capacity = (
                state.quote_desired
                and not state.quote_configuration_error
                and state.symbol not in selected_quote
            )
            if state.trade_rejected_due_to_capacity:
                state.trade_acked = False
            if state.quote_rejected_due_to_capacity:
                state.quote_acked = False

    def subscription_capacity_snapshot(self) -> SubscriptionCapacitySnapshot:
        with self._state_lock:
            pending_subscribe = sum(
                status == SubscriptionSessionStatus.PENDING_SUBSCRIBE
                for status in self._session_status.values()
            )
            active = sum(
                status == SubscriptionSessionStatus.ACTIVE
                for status in self._session_status.values()
            )
            pending_unsubscribe = sum(
                status == SubscriptionSessionStatus.PENDING_UNSUBSCRIBE
                for status in self._session_status.values()
            )
            occupied = len(self._session_status)
            notice_key = (
                (
                    self._execution_notice_subscription.tr_id,
                    self._execution_notice_subscription.tr_key,
                )
                if self._execution_notice_subscription is not None
                else None
            )
            return SubscriptionCapacitySnapshot(
                total_capacity=self._total_capacity,
                desired_count=len(self._target_subscriptions),
                reconnect_replay_count=len(self._active_subscriptions),
                pending_subscribe_count=pending_subscribe,
                active_count=active,
                pending_unsubscribe_count=pending_unsubscribe,
                occupied_count=occupied,
                available_count=max(0, self._total_capacity - occupied),
                max_occupied_count=self._max_session_slots_used,
                execution_notice_desired=(
                    notice_key is not None and notice_key in self._target_subscriptions
                ),
                execution_notice_acked=(
                    notice_key is not None
                    and self._session_status.get(notice_key)
                    == SubscriptionSessionStatus.ACTIVE
                ),
            )

    def protocol_metrics_snapshot(self) -> MarketDataProtocolMetrics:
        """Return immutable counters used by the standalone Gate-2 reporter."""
        receive_count, receive_p50, receive_p95, receive_p99, receive_max = (
            self._receive_lags_ms.snapshot()
        )
        queue_count, queue_p50, queue_p95, queue_p99, queue_max = (
            self._queue_lags_ms.snapshot()
        )
        with self._state_lock:
            return MarketDataProtocolMetrics(
                frame_counts_by_tr_id=tuple(sorted(self._frame_counts.items())),
                record_counts_by_tr_id=tuple(sorted(self._record_counts.items())),
                schema_fingerprints_by_tr_id=tuple(
                    sorted(self._schema_fingerprints.items())
                ),
                parser_failure_count=self._parser_failure_count,
                duplicate_event_count=self._duplicate_event_count,
                receive_lag_sample_count=receive_count,
                receive_lag_p50_ms=receive_p50,
                receive_lag_p95_ms=receive_p95,
                receive_lag_p99_ms=receive_p99,
                receive_lag_max_ms=receive_max,
                queue_lag_sample_count=queue_count,
                queue_lag_p50_ms=queue_p50,
                queue_lag_p95_ms=queue_p95,
                queue_lag_p99_ms=queue_p99,
                queue_lag_max_ms=queue_max,
            )

    def replace_stop_rules(self, symbol: str, rules: Iterable[StopRule]):
        return self._accumulator.replace_stop_rules(symbol, rules)

    def acknowledge_stop_breach(self, symbol: str, card_key: str, version: str) -> bool:
        return self._accumulator.acknowledge_breach(symbol, card_key, version)

    def latch_stop_breach(
        self,
        symbol: str,
        card_key: str,
        version: str,
        quote: QuoteSnapshot,
        stop_price: float,
    ) -> bool:
        return self._accumulator.latch_breach(
            symbol, card_key, version, quote, stop_price
        )

    def set_symbol_trading_halted(self, symbol: str, halted: bool) -> None:
        """Apply a verified halt signal without guessing from quote absence."""
        symbol = str(symbol or "").upper()
        with self._state_lock:
            state = self._states.setdefault(symbol, SymbolFeedState(symbol=symbol))
            state.trading_halted = bool(halted)

    def set_qualification_channel_suppressed(
        self, symbol: str, channel: FeedChannel, suppressed: bool
    ) -> None:
        """Suppress one live channel only for the explicit read-only soak probe."""
        if not self._qualification_mode:
            raise RuntimeError("channel suppression is restricted to qualification mode")
        identity = (str(symbol or "").upper(), channel.value)
        with self._state_lock:
            if suppressed:
                self._qualification_suppressed_channels.add(identity)
            else:
                self._qualification_suppressed_channels.discard(identity)

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

    def is_symbol_feed_available(
        self,
        symbol: str,
        *,
        require_trade: bool = True,
        require_quote: bool = True,
    ) -> bool:
        """Return structural KIS channel health without using event age.

        KIS sends symbol data when market activity changes; an acknowledged
        channel can therefore be healthy even when an illiquid symbol has not
        produced a trade for several seconds.  Exact broker mutations still
        call :meth:`is_symbol_execution_ready` and retain every timestamp and
        queue-delay check.
        """

        reference = self._clock()
        self._expire_ack_timeouts(reference)
        state = self.symbol_state(symbol)
        if not self.is_connected() or state.clock_health != ClockHealth.HEALTHY:
            return False
        if require_trade and (
            not state.trade_acked
            or state.trade_rejected_due_to_capacity
            or bool(state.trade_error)
            or bool(state.trade_configuration_error)
        ):
            return False
        if require_quote and (
            not state.quote_acked
            or state.quote_rejected_due_to_capacity
            or bool(state.quote_error)
            or bool(state.quote_configuration_error)
        ):
            return False
        return True

    def entry_quote_ready(
        self, symbol: str, *, now: Optional[datetime] = None
    ) -> bool:
        quote = self.latest_quote(symbol)
        ready = bool(
            self.is_symbol_execution_ready(
                symbol, require_trade=True, require_quote=True, now=now
            )
            and quote is not None
            and quote.ask is not None
            and quote.ask > 0
            and quote.last_price > 0
        )
        record_entry_readiness(symbol=symbol, ready=ready)
        return ready

    def _on_connection(self, connected: bool, reason: str, generation: int) -> None:
        was_connected = self._connected
        self._connected = connected
        self._reconnect_generation = generation
        with self._state_lock:
            self._session_subscriptions.clear()
            self._session_status.clear()
            self._session_nacked.clear()
            for state in self._states.values():
                state.reconnect_generation = generation
                state.trade_acked = False
                state.quote_acked = False
                if not connected:
                    state.trade_requested_at = None
                    state.quote_requested_at = None
                elif generation:
                    state.last_error = ""
                    state.trade_error = ""
                    state.quote_error = ""
                    state.clock_health = ClockHealth.HEALTHY
            if connected:
                reset_channels = {
                    (
                        FeedChannel.TRADE.value
                        if channel == TRADE_TR_ID
                        else FeedChannel.QUOTE.value
                    )
                    for channel, semantics in self._sequence_reset_by_channel.items()
                    if semantics == "RESET_ON_RECONNECT"
                }
                if reset_channels:
                    self._last_sequence = {
                        key: value
                        for key, value in self._last_sequence.items()
                        if key[1] not in reset_channels
                    }
                # KisWebSocketClient notifies only after replaying its durable
                # desired set into this new socket session.
                for key, sub in self._active_subscriptions.items():
                    self._session_subscriptions[key] = sub
                    self._session_status[key] = (
                        SubscriptionSessionStatus.PENDING_SUBSCRIBE
                    )
                    self._mark_channel_requested_locked(sub)
                self._max_session_slots_used = max(
                    self._max_session_slots_used,
                    len(self._session_status),
                )
                self._reconcile_subscription_target_locked()
            self._refresh_capacity_rejections_locked()
        if was_connected and not connected:
            for callback in list(self._disconnect_callbacks):
                callback(reason or "KIS WebSocket disconnected")
        observed_at = self._clock()
        for callback in list(self._session_callbacks):
            try:
                callback(connected, reason, generation, observed_at)
            except Exception:
                logger.exception("KIS WebSocket session audit callback failed")

    def _on_ack(self, frame: KisWsSystemFrame) -> None:
        key = (frame.tr_id, frame.tr_key)
        with self._state_lock:
            sub = self._session_subscriptions.get(key)
            status = self._session_status.get(key)
            if sub is None or status is None:
                return

            if frame.is_unsubscribe:
                if frame.accepted:
                    self._session_subscriptions.pop(key, None)
                    self._session_status.pop(key, None)
                    self._clear_channel_ack_locked(sub)
                    self._reconcile_subscription_target_locked()
                    self._refresh_capacity_rejections_locked()
                    return
                # A rejected unsubscribe did not release the KIS slot. Keep it
                # occupied until a later exact ACK or a session reconnect.
                self._session_status[key] = SubscriptionSessionStatus.ACTIVE
                self.nack_count += 1
                message = frame.message or "unsubscription NACK"
                self._record_subscription_error_locked(sub, message)
                self._alert(
                    f"KIS unsubscription rejected for {sub.tr_id}:{sub.tr_key}: {message}"
                )
                self._refresh_capacity_rejections_locked()
                return

            if frame.accepted:
                self._session_status[key] = SubscriptionSessionStatus.ACTIVE
                self._record_subscription_ack_locked(sub)
                return

            # Explicit subscribe rejection releases tentative capacity and is
            # removed from reconnect replay. A lower-priority desired item may
            # consume the newly available slot, but this key is not retried in
            # the same session.
            self._session_subscriptions.pop(key, None)
            self._session_status.pop(key, None)
            self._active_subscriptions.pop(key, None)
            self._session_nacked.add(key)
            forget = getattr(self._transport, "forget_subscriptions", None)
            if callable(forget):
                forget([sub])
            self._clear_channel_ack_locked(sub)
            self.nack_count += 1
            message = frame.message or "subscription NACK"
            self._record_subscription_error_locked(sub, message)
            self._alert(
                f"KIS subscription rejected for {sub.tr_id}:{sub.tr_key}: {message}"
            )
            self._reconcile_subscription_target_locked()
            self._refresh_capacity_rejections_locked()

    def _record_subscription_ack_locked(self, sub: KisWsSubscription) -> None:
        if not sub.symbol:
            return
        state = self._states[sub.symbol]
        if sub.tr_id == TRADE_TR_ID:
            state.trade_acked = True
            state.trade_requested_at = None
            state.trade_error = ""
        elif sub.tr_id == QUOTE_TR_ID:
            state.quote_acked = True
            state.quote_requested_at = None
            state.quote_error = ""
        if not state.trade_error and not state.quote_error:
            state.last_error = ""

    def _record_subscription_error_locked(
        self, sub: KisWsSubscription, message: str
    ) -> None:
        if not sub.symbol:
            return
        state = self._states[sub.symbol]
        if sub.tr_id == TRADE_TR_ID:
            state.trade_error = message
            state.trade_requested_at = None
        elif sub.tr_id == QUOTE_TR_ID:
            state.quote_error = message
            state.quote_requested_at = None
        state.last_error = message

    def _on_data_frame(self, frame: KisWsDataFrame) -> None:
        if frame.tr_id == TRADE_TR_ID:
            schema = TRADE_COLUMNS
        elif frame.tr_id == QUOTE_TR_ID:
            schema = QUOTE_COLUMNS
        else:
            schema = (f"FIELD_COUNT:{len(frame.payload.split('^'))}",)
        schema_fingerprint = hashlib.sha256(
            "^".join(schema).encode("utf-8")
        ).hexdigest()
        with self._state_lock:
            self._frame_counts[frame.tr_id] = self._frame_counts.get(frame.tr_id, 0) + 1
            self._record_counts[frame.tr_id] = (
                self._record_counts.get(frame.tr_id, 0) + frame.record_count
            )
            self._schema_fingerprints[frame.tr_id] = schema_fingerprint
        try:
            if frame.tr_id == TRADE_TR_ID:
                for record in self._split_records(frame, TRADE_COLUMNS):
                    try:
                        self._ingest_trade_record(record, frame)
                    except Exception:
                        with self._state_lock:
                            self._parser_failure_count += 1
                        logger.exception(
                            "KIS trade record dropped without blocking later records"
                        )
            elif frame.tr_id == QUOTE_TR_ID:
                for record in self._split_records(frame, QUOTE_COLUMNS):
                    try:
                        self._ingest_quote_record(record, frame)
                    except Exception:
                        with self._state_lock:
                            self._parser_failure_count += 1
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
            with self._state_lock:
                self._parser_failure_count += 1
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
            processed_at=self._clock(),
            source="KIS_WS",
            channel=frame.tr_id,
            sequence=self._sequence_from_record(frame.tr_id, record),
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
            processed_at=self._clock(),
            source="KIS_WS",
            channel=frame.tr_id,
            sequence=self._sequence_from_record(frame.tr_id, record),
            payload_fingerprint=hashlib.sha256(
                "^".join(record.values()).encode("utf-8")
            ).hexdigest(),
        )
        self.ingest_quote(quote)

    def _sequence_from_record(
        self, tr_id: str, record: Mapping[str, str]
    ) -> Optional[int]:
        field = self._sequence_field_by_channel.get(str(tr_id).upper(), "")
        if not field:
            return None
        value = str(record.get(field) or "").strip()
        try:
            return int(value) if value else None
        except ValueError:
            return None

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
        with self._state_lock:
            if key in self._qualification_suppressed_channels:
                self.dropped_event_count += 1
                return False
        broker_at = quote.broker_event_at
        if (
            quote.channel.upper() in self._confirmed_sequence_channels
            and quote.sequence is None
        ):
            return self._reject_event(
                quote.symbol, channel, ClockHealth.SEQUENCE_MISSING
            )
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
            self._duplicate_event_count += 1
            return False
        if len(self._dedup_order) == self._dedup_order.maxlen:
            self._dedup_seen.discard(self._dedup_order[0])
        self._dedup_order.append(identity)
        self._dedup_seen.add(identity)
        self._last_timestamp[key] = broker_at
        if quote.sequence is not None and quote.channel.upper() in self._confirmed_sequence_channels:
            self._last_sequence[key] = quote.sequence
        self._receive_lags_ms.add(
            max(0.0, (now - broker_at).total_seconds() * 1000.0)
        )
        self._queue_lags_ms.add(quote.queue_delay_seconds() * 1000.0)
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
                configuration_error = state.trade_configuration_error
                broker_event_at = state.last_trade_event_at
                received_at = state.last_trade_received_at
            else:
                acked = state.quote_acked
                rejected = state.quote_rejected_due_to_capacity
                error = state.quote_error
                configuration_error = state.quote_configuration_error
                broker_event_at = state.last_quote_event_at
                received_at = state.last_quote_received_at
            if (
                not acked
                or rejected
                or bool(error)
                or bool(configuration_error)
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
        _, receive_p50, receive_p95, receive_p99, _ = (
            self._receive_lags_ms.snapshot()
        )
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
            receive_lag_p50_ms=receive_p50,
            receive_lag_p95_ms=receive_p95,
            receive_lag_p99_ms=receive_p99,
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
    confirmed_sequence_channels: Iterable[str] = (),
    sequence_field_by_channel: Optional[Mapping[str, str]] = None,
    sequence_reset_by_channel: Optional[Mapping[str, str]] = None,
    execution_notice_verified: bool = False,
    qualification_mode: bool = False,
    sensitive_value_audit: Callable[[str], None] = lambda value: None,
    capability_manifest_path: Optional[Path] = None,
    capability_manifest_sha256: str = "",
    runtime_commit_sha: str = "",
    symbol_key_store: Optional[KisWsSymbolKeyStore] = None,
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
    manifest_sha256 = ""
    manifest_commit_sha = ""
    if qualification_mode:
        confirmed_sequences = {
            str(channel).upper() for channel in confirmed_sequence_channels
        }
        sequence_fields = {
            str(channel).upper(): str(field).upper()
            for channel, field in (sequence_field_by_channel or {}).items()
        }
        sequence_resets = {
            str(channel).upper(): str(semantics).upper()
            for channel, semantics in (sequence_reset_by_channel or {}).items()
        }
    else:
        if (
            tuple(confirmed_sequence_channels)
            or sequence_field_by_channel
            or sequence_reset_by_channel
        ):
            raise ValueError(
                "normal KIS WebSocket composition cannot accept ad-hoc capability "
                "interpretation; configure the reviewed capability manifest"
            )
        # Keep the certification loader at the composition boundary so the
        # production service consumes the exact same validated interpretation
        # as the read-only qualifier. A path alone is insufficient: both the
        # runtime commit and the independently recorded manifest digest are
        # required and compared before a socket can be opened.
        from gate2.capabilities import (
            EXECUTION_NOTICE,
            SHA256_PATTERN,
            load_verified_capability_manifest,
        )

        configured_path = capability_manifest_path or Path(
            os.getenv("KIS_CAPABILITY_MANIFEST_PATH", "").strip()
        )
        expected_commit = str(
            runtime_commit_sha
            or os.getenv("KIS_RUNTIME_COMMIT_SHA", "")
        ).strip().lower()
        expected_digest = str(
            capability_manifest_sha256
            or os.getenv("KIS_CAPABILITY_MANIFEST_SHA256", "")
        ).strip().lower()
        if not str(configured_path) or str(configured_path) == ".":
            raise RuntimeError("KIS_CAPABILITY_MANIFEST_PATH is required")
        if len(expected_commit) != 40 or any(
            char not in "0123456789abcdef" for char in expected_commit
        ):
            raise RuntimeError("KIS_RUNTIME_COMMIT_SHA must be an exact 40-character SHA")
        if not SHA256_PATTERN.fullmatch(expected_digest):
            raise RuntimeError(
                "KIS_CAPABILITY_MANIFEST_SHA256 must pin the reviewed manifest"
            )
        manifest = load_verified_capability_manifest(
            configured_path,
            expected_commit=expected_commit,
            expected_environment=environment,
            # Execution notices are supplementary to authoritative REST
            # reconciliation and are not required for supervised pilot
            # composition. Full Gate 2 keeps the loader's strict default.
            require_execution_notice=False,
        )
        if manifest.sha256 != expected_digest:
            raise RuntimeError("reviewed KIS capability manifest digest mismatch")
        confirmed_sequences = set(manifest.confirmed_sequence_channels)
        sequence_fields = manifest.sequence_field_by_channel
        sequence_resets = manifest.sequence_reset_by_channel
        execution_notice_verified = EXECUTION_NOTICE in manifest.capabilities
        manifest_sha256 = manifest.sha256
        manifest_commit_sha = manifest.commit_sha
    if confirmed_sequences != set(sequence_fields):
        raise ValueError(
            "every verified monotonic channel requires one exact sequence field"
        )
    if confirmed_sequences != set(sequence_resets) or any(
        semantics not in {"RESET_ON_RECONNECT", "CONTINUES_ACROSS_RECONNECT"}
        for semantics in sequence_resets.values()
    ):
        raise ValueError(
            "every verified monotonic channel requires exact reset semantics"
        )
    supported_fields = {
        TRADE_TR_ID: set(TRADE_COLUMNS),
        QUOTE_TR_ID: set(QUOTE_COLUMNS),
    }
    for channel, field in sequence_fields.items():
        if field not in supported_fields.get(channel, set()):
            raise ValueError(f"unsupported sequence field {channel}:{field}")
    prefix = f"KIS_{environment}"
    base_url = os.getenv(f"{prefix}_BASE_URL", "").strip()
    ws_url = os.getenv(f"{prefix}_WS_URL", "").strip()
    app_key = os.getenv(f"{prefix}_APP_KEY", "").strip()
    app_secret = os.getenv(f"{prefix}_APP_SECRET", "").strip()
    key_store = symbol_key_store or KisWsSymbolKeyStore()

    def resolve_key(symbol: str, channel: FeedChannel) -> str:
        del channel  # KIS uses the same verified key for trade and quote TRs.
        return key_store.resolve(symbol)

    approval_keys = KisWsApprovalKeyProvider(
        base_url=base_url,
        app_key=app_key,
        app_secret=app_secret,
        ttl_seconds=execution_config.KIS_WS_APPROVAL_KEY_TTL_SECONDS,
        max_retries=execution_config.KIS_WS_AUTH_MAX_RETRIES,
        protocol_verified=execution_config.KIS_WS_PROTOCOL_VERIFIED,
        critical_alert=critical_alert,
        sensitive_value_audit=sensitive_value_audit,
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
        if hts_id and execution_notice_verified
        else None
    )
    service = KisRealtimeMarketDataService(
        transport=transport,
        symbol_key_resolver=resolve_key,
        symbol_key_store=key_store,
        total_capacity=execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY,
        # Credentialed WS0 evidence proved one aggregate pool. The legacy
        # per-channel values remain available only to directly constructed
        # deterministic tests; live composition cannot preserve an old
        # unverified channel ceiling or accidentally stay at zero.
        trade_capacity=execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY,
        quote_capacity=execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY,
        confirmed_sequence_channels=confirmed_sequences,
        sequence_field_by_channel=sequence_fields,
        sequence_reset_by_channel=sequence_resets,
        execution_notice_subscription=notice_subscription,
        approval_key_age=approval_keys.approval_key_age_seconds,
        alert=critical_alert,
        qualification_mode=qualification_mode,
    )
    # Read-only diagnostics: these prove which reviewed interpretation was
    # used by ordinary production composition. They confer no activation.
    service.capability_manifest_sha256 = manifest_sha256
    service.capability_manifest_commit_sha = manifest_commit_sha
    return service
