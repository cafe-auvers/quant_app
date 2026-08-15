"""Entry-attempt engine: per-symbol locking, capital reservation, the
15-second attempt lifetime, and the retry/cooldown state machine.
``buydashboard_to_kanban.md`` sections 9-11.

This module does not recompute or reinterpret the ORB/breakout/risk rules --
"the existing ORB strategy calculation itself should remain unchanged"
(section 15). It consumes an already-decided :class:`EntryTrigger` (built by
the caller from the existing, unmodified execution-queue candidate
selection in :mod:`src.core.execution_queue`) and owns everything the spec
adds on top: symbol-level locking (section 9.2), the account-level capital
transaction (section 9.3, via :mod:`src.services.capital_allocator`),
simultaneous-trigger priority (section 9.4), and the attempt/retry state
machine (section 10).

Deliberately synchronous and side-effect-explicit (no internal thread or
timer loop) so :mod:`src.services.trading_engine`'s 1-second heartbeat
(Phase 5) can drive it, and so tests can call it directly without racing
real wall-clock time.

Order resolution is a two-phase state machine (fixed after code review):
a cancel request is never treated as a completed cancellation. Capital is
only settled and a retry cooldown only starts once the broker has actually
confirmed a terminal status (FILLED/CANCELLED/REJECTED) --
:func:`resolve_entry_order` is called every heartbeat tick for any working
order (not only at the 15s deadline), so a fill is protected the moment it
is observed and a cancel-in-flight order is never resubmitted against or
assumed gone.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from src.core import execution_config
from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.services import capital_allocator
from src.services.order_execution_service import (
    DuplicateOpenOrderError,
    submit_guarded_overseas_order,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EntryTrigger:
    """One symbol's EXECUTE_READY candidate for this evaluation pass."""

    environment: str
    account_no: str
    symbol: str
    trigger_at: datetime
    kanban_priority: int
    quantity: int
    limit_price: float
    notional: float
    exchange: str = "NASD"


class AttemptOutcome(str, Enum):
    SUBMITTED = "SUBMITTED"
    WAITING_FOR_CAPITAL = "WAITING_FOR_CAPITAL"
    SYMBOL_LOCKED = "SYMBOL_LOCKED"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    COOLDOWN = "COOLDOWN"
    RATE_LIMITED = "RATE_LIMITED"
    REJECTED = "REJECTED"


@dataclass
class AttemptResult:
    trigger: EntryTrigger
    outcome: AttemptOutcome
    order: Optional[BrokerOrder] = None
    reservation_id: str = ""
    attempt_group_id: str = ""
    attempt_count: int = 0
    # When set, the caller should persist this onto the card
    # (TradeCardState.next_retry_at) so retry timing survives a restart --
    # this engine's own in-memory _SymbolAttemptState is only a same-process
    # cache, not the durable record.
    retry_at: Optional[datetime] = None
    detail: str = ""


class AttemptDeadlineAction(str, Enum):
    """The outcome of one :func:`resolve_entry_order` call. Renamed from a
    plain "deadline" concept -- code review flagged that treating a cancel
    *request* as a completed cancellation could orphan/duplicate orders, so
    this now distinguishes "cancel just requested" from "cancel broker-
    confirmed" (section 401-444, corrected per review finding P0-7).
    """

    STILL_WORKING = "STILL_WORKING"  # order open, no fill yet, not at deadline
    MOVE_TO_OPEN_POSITION = "MOVE_TO_OPEN_POSITION"  # broker-confirmed FILLED
    AWAIT_CANCEL_CONFIRMATION = "AWAIT_CANCEL_CONFIRMATION"  # cancel requested, not yet confirmed
    CONFIRMED_CANCELLED_ZERO_FILL = "CONFIRMED_CANCELLED_ZERO_FILL"  # broker-confirmed CANCELLED, nothing filled
    CONFIRMED_CANCELLED_WITH_FILL = "CONFIRMED_CANCELLED_WITH_FILL"  # broker-confirmed CANCELLED, partial fill stands
    RELEASE_AND_RETRY_AFTER_COOLDOWN = "RELEASE_AND_RETRY_AFTER_COOLDOWN"  # broker-confirmed REJECTED
    BLOCK_SYMBOL_PENDING_RECONCILIATION = "BLOCK_SYMBOL_PENDING_RECONCILIATION"  # unknown

    # Retained for backward compatibility with anything that still branches
    # on the pre-review action names.
    CANCEL_REMAINDER_AND_RETRY = "CANCEL_REMAINDER_AND_RETRY"
    CANCEL_AND_RETRY_AFTER_COOLDOWN = "CANCEL_AND_RETRY_AFTER_COOLDOWN"


_TERMINAL_ACTIONS = {
    AttemptDeadlineAction.MOVE_TO_OPEN_POSITION,
    AttemptDeadlineAction.CONFIRMED_CANCELLED_ZERO_FILL,
    AttemptDeadlineAction.CONFIRMED_CANCELLED_WITH_FILL,
    AttemptDeadlineAction.RELEASE_AND_RETRY_AFTER_COOLDOWN,
}


def decide_attempt_deadline_action(order: BrokerOrder) -> AttemptDeadlineAction:
    """Pure decision from the order's *already-reconciled* status/fill --
    this function does not query KIS or mutate anything and does not by
    itself decide whether to *request* a cancel (that needs to know
    whether the 15s deadline has actually passed, which
    :func:`resolve_entry_order` supplies).
    """
    if order.status == OrderStatus.FILLED:
        return AttemptDeadlineAction.MOVE_TO_OPEN_POSITION
    if order.status == OrderStatus.CANCELLED:
        return (
            AttemptDeadlineAction.CONFIRMED_CANCELLED_WITH_FILL
            if order.filled_quantity > 0
            else AttemptDeadlineAction.CONFIRMED_CANCELLED_ZERO_FILL
        )
    if order.status == OrderStatus.CANCEL_REQUESTED:
        return AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION
    if order.status in (OrderStatus.UNKNOWN_SUBMISSION_STATE, OrderStatus.UNKNOWN):
        return AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION
    if order.status == OrderStatus.REJECTED:
        return AttemptDeadlineAction.RELEASE_AND_RETRY_AFTER_COOLDOWN
    # WORKING/ACCEPTED/CREATED, open (possibly partially filled) --
    # whether to escalate to a cancel request is resolve_entry_order's call.
    return AttemptDeadlineAction.STILL_WORKING


@dataclass
class _SymbolAttemptState:
    """Per-symbol bookkeeping the engine needs across ticks."""

    attempt_group_id: str = ""
    attempt_count: int = 0
    cooldown_until: Optional[datetime] = None
    attempt_timestamps: List[datetime] = field(default_factory=list)


class EntryAttemptManager:
    """Owns per-symbol locks and the retry/cooldown state machine.

    One instance is shared for the app's lifetime; every method is cheap
    and safe to call repeatedly from the 1-second heartbeat.
    """

    def __init__(
        self,
        *,
        buying_power_provider: Callable[[str, str], float],
        submit_order: Callable[..., BrokerOrder] = submit_guarded_overseas_order,
        clock: Callable[[], datetime] = _utc_now,
        reservations_path: Optional[Path] = None,
    ) -> None:
        self._buying_power_provider = buying_power_provider
        self._submit_order = submit_order
        self._clock = clock
        # Resolved once here (not baked into a function default parameter)
        # so a test/caller can point this at an isolated path and every
        # capital_allocator call below actually honors it -- module-level
        # default parameters are bound at import time and will not pick up
        # a later monkeypatch of capital_allocator.RESERVATIONS_FILE.
        self._reservations_path = reservations_path or capital_allocator.RESERVATIONS_FILE
        self._symbol_locks: Dict[Tuple[str, str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._state: Dict[Tuple[str, str, str], _SymbolAttemptState] = {}

    @staticmethod
    def _symbol_key(environment: str, account_no: str, symbol: str) -> Tuple[str, str, str]:
        return (str(environment or "").upper(), str(account_no or ""), str(symbol or "").upper())

    def _lock_for(self, key: Tuple[str, str, str]) -> threading.Lock:
        with self._locks_guard:
            lock = self._symbol_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._symbol_locks[key] = lock
            return lock

    def restore_symbol_state(
        self,
        environment: str,
        account_no: str,
        symbol: str,
        *,
        cooldown_until: Optional[datetime] = None,
        attempt_group_id: str = "",
        attempt_count: int = 0,
    ) -> None:
        """Seeds this process's in-memory retry bookkeeping from a card's
        persisted ``next_retry_at``/``entry_attempt_group_id``/
        ``entry_attempt_count`` fields (review finding P0-4: retry
        bookkeeping must survive a restart, not live only in this
        in-process cache). Call once per card during startup/reconciliation
        before the first heartbeat runs.
        """
        if not (cooldown_until or attempt_group_id or attempt_count):
            return
        key = self._symbol_key(environment, account_no, symbol)
        state = self._state.setdefault(key, _SymbolAttemptState())
        if cooldown_until is not None:
            state.cooldown_until = cooldown_until
        if attempt_group_id:
            state.attempt_group_id = attempt_group_id
        if attempt_count:
            state.attempt_count = attempt_count

    def process_triggers(self, triggers: List[EntryTrigger], **submit_kwargs) -> List[AttemptResult]:
        """Section 9.4 priority: earlier trigger timestamp wins; ties broken
        by higher kanban_priority, then deterministic symbol order. The
        first candidate to run reserves capital; anything after it that
        can't get capital comes back as WAITING_FOR_CAPITAL (section 384-389)
        rather than being submitted and left to a broker-side rejection
        (section 391).
        """
        ordered = sorted(triggers, key=lambda t: (t.trigger_at, -t.kanban_priority, t.symbol))
        return [self.attempt_entry(trigger, **submit_kwargs) for trigger in ordered]

    def attempt_entry(self, trigger: EntryTrigger, **submit_kwargs) -> AttemptResult:
        key = self._symbol_key(trigger.environment, trigger.account_no, trigger.symbol)
        lock = self._lock_for(key)
        if not lock.acquire(blocking=False):
            # Section 344-354: "An unresolved AAPL order blocks only AAPL."
            # This *is* that isolation -- another attempt for this exact
            # symbol is already mid-flight in this process; every other
            # symbol's lock is untouched.
            return AttemptResult(
                trigger,
                AttemptOutcome.SYMBOL_LOCKED,
                detail="Another attempt for this symbol is already in progress",
            )
        try:
            return self._attempt_entry_locked(trigger, key, **submit_kwargs)
        finally:
            lock.release()

    def _attempt_entry_locked(
        self, trigger: EntryTrigger, key: Tuple[str, str, str], **submit_kwargs
    ) -> AttemptResult:
        state = self._state.setdefault(key, _SymbolAttemptState())
        now = self._clock()

        if state.cooldown_until is not None and now < state.cooldown_until:
            return AttemptResult(
                trigger,
                AttemptOutcome.COOLDOWN,
                attempt_group_id=state.attempt_group_id,
                attempt_count=state.attempt_count,
                retry_at=state.cooldown_until,
                detail=f"Cooling down until {state.cooldown_until.isoformat()}",
            )

        one_minute_ago = now - timedelta(minutes=1)
        state.attempt_timestamps = [ts for ts in state.attempt_timestamps if ts >= one_minute_ago]
        if len(state.attempt_timestamps) >= execution_config.MAX_ENTRY_ATTEMPTS_PER_SYMBOL_PER_MINUTE:
            retry_at = min(state.attempt_timestamps) + timedelta(minutes=1)
            return AttemptResult(
                trigger,
                AttemptOutcome.RATE_LIMITED,
                attempt_group_id=state.attempt_group_id,
                attempt_count=state.attempt_count,
                retry_at=retry_at,
                detail="Max entry attempts per symbol per minute reached",
            )

        if not state.attempt_group_id:
            state.attempt_group_id = uuid4().hex
        attempt_group_id = state.attempt_group_id
        attempt_number = state.attempt_count + 1

        reservation = capital_allocator.reserve_capital_for_entry(
            environment=trigger.environment,
            account_no=trigger.account_no,
            symbol=trigger.symbol,
            attempt_group_id=attempt_group_id,
            requested_notional=trigger.notional,
            buying_power_provider=lambda: self._buying_power_provider(
                trigger.environment, trigger.account_no
            ),
            path=self._reservations_path,
        )
        if reservation is None:
            return AttemptResult(
                trigger,
                AttemptOutcome.WAITING_FOR_CAPITAL,
                attempt_group_id=attempt_group_id,
                attempt_count=state.attempt_count,
                detail="Capital not currently available",
            )

        deadline = now + timedelta(seconds=execution_config.ENTRY_ATTEMPT_TTL_SECONDS)
        try:
            order = self._submit_order(
                environment=trigger.environment,
                account_no=trigger.account_no,
                symbol=trigger.symbol,
                side=OrderSide.BUY,
                intent=OrderIntent.ENTRY,
                quantity=trigger.quantity,
                limit_price=trigger.limit_price,
                exchange=trigger.exchange,
                attempt_group_id=attempt_group_id,
                attempt_number=attempt_number,
                attempt_deadline_at=deadline.isoformat(),
                capital_reservation_id=reservation.reservation_id,
                **submit_kwargs,
            )
        except DuplicateOpenOrderError as exc:
            capital_allocator.release_reservation(
                reservation.reservation_id, path=self._reservations_path
            )
            return AttemptResult(
                trigger,
                AttemptOutcome.DUPLICATE_ORDER,
                reservation_id=reservation.reservation_id,
                attempt_group_id=attempt_group_id,
                attempt_count=state.attempt_count,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced via AttemptResult
            capital_allocator.release_reservation(
                reservation.reservation_id, path=self._reservations_path
            )
            state.cooldown_until = now + timedelta(seconds=execution_config.ENTRY_RETRY_COOLDOWN_SECONDS)
            logger.exception("Entry submission raised for %s", trigger.symbol)
            return AttemptResult(
                trigger,
                AttemptOutcome.REJECTED,
                reservation_id=reservation.reservation_id,
                attempt_group_id=attempt_group_id,
                attempt_count=state.attempt_count,
                retry_at=state.cooldown_until,
                detail=str(exc),
            )

        state.attempt_count = attempt_number
        state.attempt_timestamps.append(now)

        if order.status == OrderStatus.REJECTED:
            capital_allocator.release_reservation(
                reservation.reservation_id, path=self._reservations_path
            )
            state.cooldown_until = now + timedelta(seconds=execution_config.ENTRY_RETRY_COOLDOWN_SECONDS)
            return AttemptResult(
                trigger,
                AttemptOutcome.REJECTED,
                order=order,
                reservation_id=reservation.reservation_id,
                attempt_group_id=attempt_group_id,
                attempt_count=state.attempt_count,
                retry_at=state.cooldown_until,
                detail=order.error_message,
            )

        return AttemptResult(
            trigger,
            AttemptOutcome.SUBMITTED,
            order=order,
            reservation_id=reservation.reservation_id,
            attempt_group_id=attempt_group_id,
            attempt_count=state.attempt_count,
        )

    def resolve_entry_order(
        self,
        order: BrokerOrder,
        *,
        at_deadline: bool,
        cancel_requested: bool,
        cancel_order: Callable[[BrokerOrder], None],
    ) -> AttemptDeadlineAction:
        """Called every heartbeat tick for *any* order this engine is
        tracking (review finding P0-6: not only at the 15s deadline) so a
        fill is observed as soon as it happens. Only escalates to
        requesting a cancel when ``at_deadline`` or ``cancel_requested`` is
        True, and -- review finding P0-7 -- never treats a just-requested
        cancel as complete: capital is settled and a retry cooldown starts
        only once the broker has actually confirmed a terminal status on a
        *later* call, once ``order.status`` has moved past
        ``CANCEL_REQUESTED``.

        The caller applies any fill to the card itself (this module has no
        knowledge of ``TradeCardState``) -- this method only owns capital
        settlement and retry-cooldown bookkeeping.
        """
        action = decide_attempt_deadline_action(order)
        key = self._symbol_key(order.environment, order.account_no, order.symbol)
        state = self._state.setdefault(key, _SymbolAttemptState())
        now = self._clock()

        if action == AttemptDeadlineAction.STILL_WORKING:
            if at_deadline or cancel_requested:
                cancel_order(order)
                return AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION
            return action

        if action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION:
            # Already requested (either by us on a previous tick, or the
            # order already carried CANCEL_REQUESTED) -- do not resend, do
            # not settle anything yet. Wait for the broker to actually
            # resolve it.
            return action

        if action == AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION:
            # Capital stays reserved until reconciliation resolves the order
            # to a known state -- section 436-442: "Query KIS open orders
            # and executions. Resolve accepted / filled / not submitted."
            return action

        # Every remaining action is terminal -- settle capital now.
        self._settle_reservation_for_order(order)
        if action in (
            AttemptDeadlineAction.CONFIRMED_CANCELLED_ZERO_FILL,
            AttemptDeadlineAction.RELEASE_AND_RETRY_AFTER_COOLDOWN,
        ):
            state.cooldown_until = now + timedelta(seconds=execution_config.ENTRY_RETRY_COOLDOWN_SECONDS)
        return action

    # Backward-compatible name for the same call, kept because tests and
    # any external caller written against the original single-phase API
    # still reach it under this name; behavior is identical.
    def handle_attempt_deadline(
        self,
        order: BrokerOrder,
        *,
        cancel_order: Callable[[BrokerOrder], None],
    ) -> AttemptDeadlineAction:
        return self.resolve_entry_order(
            order, at_deadline=True, cancel_requested=False, cancel_order=cancel_order
        )

    def _settle_reservation_for_order(self, order: BrokerOrder) -> None:
        if not order.capital_reservation_id:
            return
        filled_notional = float(order.filled_quantity or 0) * float(order.avg_fill_price or 0.0)
        if filled_notional > 0:
            # Section 872-874: consume capital corresponding to the filled
            # quantity -- that money is genuinely spent, not "available."
            capital_allocator.consume_reservation(
                order.capital_reservation_id, filled_notional, path=self._reservations_path
            )
        # Section 876: release whatever of the reservation remains
        # unconsumed once we know no more fills are coming for this attempt.
        capital_allocator.release_reservation(
            order.capital_reservation_id, path=self._reservations_path
        )

    def reset_symbol(self, environment: str, account_no: str, symbol: str) -> None:
        """Clear retry/cooldown bookkeeping -- EOD reset (section 513-515)."""
        self._state.pop(self._symbol_key(environment, account_no, symbol), None)
