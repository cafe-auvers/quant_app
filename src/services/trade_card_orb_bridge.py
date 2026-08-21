"""Bridges the legacy, *unchanged* ORB/execution-queue candidate selection
into the ``TradeCardState`` fields the new Kanban entry engine actually
reads. ``buydashboard_to_kanban.md`` section 15 ("the existing ORB strategy
calculation itself should remain unchanged"); code review finding P0-2.

Moving a card to BUY_TODAY (:mod:`src.ui.buyboard.controller`) only changes
``board_status``/membership metadata -- nothing else in this redesign
recomputes ORB candidates (deliberately; section 15 forbids duplicating
that logic). Without this adapter, a Kanban card can never reach
EXECUTE_READY: nothing else populates
``entry_trigger``/``entry_orb_low``/``entry_orb_high``/``planned_quantity``/
``stop_adr``, or translates :class:`~src.core.execution_queue.OrbCandidateStatus`
into :class:`~src.core.trade_card_state.EntryRuntimeStatus`.

:class:`TradeCardOrbEvaluator` is a pure function of its inputs -- no I/O,
no broker calls, no recomputation of the breakout/risk numbers themselves
-- so a caller can invoke it for every card with a live
:class:`~src.core.execution_queue.ExecutionQueueItem` (the same object the
legacy execution queue already refreshes on its own existing cadence) and
persist the result via :mod:`src.services.trade_card_repository`. In
production that caller is :class:`src.ui.buyboard.runtime_worker.BuyboardRuntimeWorker`,
which reads items from the already-running ``ExecutionQueueManager``
(``main_window.execution_queue_manager``) rather than triggering a second,
competing ORB refresh cycle.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from src.core.execution_queue import (
    SUPPORTED_ORB_WINDOWS,
    ExecutionQueueItem,
    OrbCandidateStatus,
)
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState

# ORB begins only when the trader activates a planning card for Buy Today.
# Watchlist and Buylist retain their configured breakout target without
# current-session ORB state.  Once a position opens, the entry ORB record is
# frozen and this bridge must not overwrite it.
_ORB_ACTIVE_STATUSES = {BoardStatus.BUY_TODAY}

_CANDIDATE_STATUS_TO_ENTRY_RUNTIME_STATUS = {
    OrbCandidateStatus.NOT_AVAILABLE: EntryRuntimeStatus.ORB_FORMING,
    OrbCandidateStatus.FORMING: EntryRuntimeStatus.ORB_FORMING,
    OrbCandidateStatus.WAITING_BREAKOUT: EntryRuntimeStatus.WAITING_BREAKOUT,
    OrbCandidateStatus.RISK_INVALID: EntryRuntimeStatus.RISK_INVALID,
    OrbCandidateStatus.VALID: EntryRuntimeStatus.ARMED,
    OrbCandidateStatus.EXECUTE_READY: EntryRuntimeStatus.EXECUTE_READY,
    OrbCandidateStatus.REJECTED: EntryRuntimeStatus.RISK_INVALID,
}

# Statuses for which the execution queue's own human-readable reason should
# surface as the card's entry_block_reason -- anything else (FORMING,
# WAITING_BREAKOUT, VALID, EXECUTE_READY) is a normal in-progress state, not
# a block worth explaining.
_BLOCKED_CANDIDATE_STATUSES = {OrbCandidateStatus.RISK_INVALID, OrbCandidateStatus.REJECTED}

_LIVE_SELECTION_CARD_STATUSES = {
    EntryRuntimeStatus.ORB_FORMING,
    EntryRuntimeStatus.WAITING_BREAKOUT,
    EntryRuntimeStatus.ARMED,
    EntryRuntimeStatus.EXECUTE_READY,
    EntryRuntimeStatus.DATA_UNAVAILABLE,
}

_US_MARKET_ZONE = ZoneInfo("America/New_York")
_US_REGULAR_OPEN = time(9, 30)
_US_REGULAR_CLOSE = time(16, 0)
_ORB_WINDOW_MINUTES = {"1m": 1, "5m": 5, "30m": 30}


def _positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _complete_candidate_for_card(
    card: TradeCardState,
    execution_queue_item: ExecutionQueueItem,
    candidate,
) -> bool:
    """Validate one persisted candidate before a live event can select it."""

    window = str(getattr(candidate, "window", "") or "").strip()
    try:
        trigger = float(getattr(candidate, "entry_trigger", None))
        orb_high = float(getattr(candidate, "orb_high", None))
        orb_low = float(getattr(candidate, "orb_low", None))
        candidate_breakout = float(getattr(candidate, "breakout_price", None))
        queue_breakout = float(getattr(execution_queue_item, "breakout_price", None))
        card_breakout = float(card.breakout_price)
        buffer_pct = float(card.buffer_pct)
        shares = int(getattr(candidate, "shares", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(
        math.isfinite(value)
        for value in (
            trigger,
            orb_high,
            orb_low,
            candidate_breakout,
            queue_breakout,
            card_breakout,
            buffer_pct,
        )
    ):
        return False
    return bool(
        window in SUPPORTED_ORB_WINDOWS
        and getattr(candidate, "status", None)
        in {
            OrbCandidateStatus.WAITING_BREAKOUT,
            OrbCandidateStatus.VALID,
            OrbCandidateStatus.EXECUTE_READY,
        }
        and trigger > 0
        and orb_high > 0
        and orb_low > 0
        and candidate_breakout > 0
        and queue_breakout > 0
        and card_breakout > 0
        and 0.0 <= buffer_pct <= 1.0
        and math.isclose(trigger, orb_high, rel_tol=1e-9, abs_tol=1e-6)
        and math.isclose(
            candidate_breakout,
            queue_breakout,
            rel_tol=1e-9,
            abs_tol=1e-6,
        )
        and math.isclose(
            card_breakout,
            queue_breakout,
            rel_tol=1e-9,
            abs_tol=1e-6,
        )
        and orb_high > card_breakout * (1.0 + buffer_pct)
        and orb_low < trigger
        and shares > 0
    )


def queue_has_execution_order_lock(
    execution_queue_item: ExecutionQueueItem,
) -> bool:
    """Distinguish a premarket manual window lock from an order lock."""

    return bool(
        execution_queue_item.locked
        and (
            not execution_queue_item.manual_window_lock
            or execution_queue_item.order_id
            or execution_queue_item.order_status
        )
    )


def _clear_entry_plan(
    card: TradeCardState, *, clear_quantity: bool = True
) -> None:
    """Remove values that must never leak from an older ORB/session."""

    card.entry_orb_high = None
    card.entry_orb_low = None
    card.entry_trigger = None
    card.stop_adr = None
    if clear_quantity:
        card.planned_quantity = 0
        card.target_position_quantity = 0


def orb_snapshot_stale_for_current_session(
    execution_queue_item: ExecutionQueueItem,
    *,
    now: datetime | None = None,
    candidate=None,
) -> bool:
    """Return whether queue ORB data cannot belong to the current session.

    New candidates persist the source bars' New York session date, which is
    authoritative.  A candidate without that provenance is always stale; the
    queue timestamp is consulted only for an entirely empty snapshot where no
    candidate could be inspected. Passing ``candidate`` checks that specific
    ORB window; without it this returns true only when every available window
    is stale.
    """

    if candidate is not None:
        return orb_candidate_stale_for_current_session(
            execution_queue_item,
            candidate,
            now=now,
        )

    candidates = list(
        dict(getattr(execution_queue_item, "candidates", {}) or {}).values()
    )
    if candidates:
        return all(
            orb_candidate_stale_for_current_session(
                execution_queue_item,
                item,
                now=now,
            )
            for item in candidates
        )

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    market_now = reference.astimezone(_US_MARKET_ZONE)
    session_open = datetime.combine(
        market_now.date(), _US_REGULAR_OPEN, tzinfo=_US_MARKET_ZONE
    )
    windows = [
        _ORB_WINDOW_MINUTES.get(str(window or "").strip(), 0)
        for window in (execution_queue_item.candidates or {})
    ]
    longest_window = max(windows or _ORB_WINDOW_MINUTES.values())
    if market_now < session_open + timedelta(minutes=longest_window):
        return False

    updated_at = execution_queue_item.last_updated
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at.astimezone(_US_MARKET_ZONE) < session_open


def orb_candidate_stale_for_current_session(
    execution_queue_item: ExecutionQueueItem,
    candidate,
    *,
    now: datetime | None = None,
) -> bool:
    """Check one candidate against the actual source-bar session identity."""

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    market_now = reference.astimezone(_US_MARKET_ZONE)
    expected_session = market_now.date().isoformat()

    raw_source_session = str(
        getattr(candidate, "source_session_date", "") or ""
    ).strip()
    if raw_source_session:
        try:
            parsed_source = datetime.strptime(raw_source_session, "%Y-%m-%d")
        except ValueError:
            return True
        return parsed_source.date().isoformat() != expected_session

    # A queue refresh timestamp proves only when a calculation ran, not which
    # bars it consumed.  Legacy candidates without explicit source-session
    # provenance must be rebuilt before they can influence execution.
    return True


def _display_candidate(
    execution_queue_item: ExecutionQueueItem,
    *,
    excluded_windows: Iterable[str] = (),
):
    """Return the plan the card should explain while no trigger is ready.

    The queue deliberately reserves ``selected_candidate`` for an ORB that
    can execute immediately. Before the trigger, however, the board still
    needs to show the operator which risk-valid ORB plan is waiting. A manual
    selection always wins; automatic display otherwise prefers a completed,
    risk-valid plan, then a still-forming plan, and only shows an invalid plan
    after no viable/forming alternative remains.
    """

    excluded = {str(window or "").strip() for window in excluded_windows}
    selected = execution_queue_item.selected_candidate
    if selected is not None and str(getattr(selected, "window", "")) not in excluded:
        return selected

    candidates = {
        window: candidate
        for window, candidate in dict(
            getattr(execution_queue_item, "candidates", {}) or {}
        ).items()
        if str(window or "").strip() not in excluded
    }
    selected_window = str(
        getattr(execution_queue_item, "selected_window", "") or ""
    )
    if selected_window and (
        getattr(execution_queue_item, "manual_window_lock", False)
        or getattr(execution_queue_item, "locked", False)
    ):
        manually_selected = candidates.get(selected_window)
        if manually_selected is not None:
            return manually_selected

    waiting = [
        candidate
        for candidate in candidates.values()
        if candidate.status
        in {OrbCandidateStatus.WAITING_BREAKOUT, OrbCandidateStatus.VALID}
    ]
    if waiting:
        return max(waiting, key=lambda candidate: float(candidate.score or 0.0))

    forming = [
        candidate
        for candidate in candidates.values()
        if candidate.status == OrbCandidateStatus.FORMING
    ]
    if forming:
        order = {"1m": 0, "5m": 1, "30m": 2}
        return min(forming, key=lambda candidate: order.get(candidate.window, 99))

    invalid = [
        candidate
        for candidate in candidates.values()
        if candidate.status
        in {OrbCandidateStatus.RISK_INVALID, OrbCandidateStatus.REJECTED}
    ]
    if invalid:
        return max(invalid, key=lambda candidate: float(candidate.score or 0.0))
    return None


def _regular_session_complete(*, now: datetime) -> bool:
    reference = now
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(_US_MARKET_ZONE).time() >= _US_REGULAR_CLOSE


def all_supported_orb_plans_rejected(
    execution_queue_item: ExecutionQueueItem,
    *,
    excluded_windows: Iterable[str] = (),
) -> bool:
    """Return true only after every supported ORB window is terminal-invalid.

    Missing, unavailable, or still-forming data is deliberately not a
    rejection.  That keeps a transient 1m/5m/30m data gap from removing a
    potentially valid Buy Today card.
    """

    candidates = dict(getattr(execution_queue_item, "candidates", {}) or {})
    terminal_invalid = {
        OrbCandidateStatus.RISK_INVALID,
        OrbCandidateStatus.REJECTED,
    }
    excluded = {str(window or "").strip() for window in excluded_windows}
    return all(
        window not in excluded
        and window in candidates
        and candidates[window].status in terminal_invalid
        and getattr(candidates[window], "terminal_rejection", False) is True
        for window in SUPPORTED_ORB_WINDOWS
    )


def _orb_rejection_note(execution_queue_item: ExecutionQueueItem) -> str:
    candidates = dict(getattr(execution_queue_item, "candidates", {}) or {})
    details = []
    for window in SUPPORTED_ORB_WINDOWS:
        candidate = candidates[window]
        reason = str(getattr(candidate, "reason", "") or "invalid ORB plan").strip()
        details.append(f"{window}: {reason}")
    return "Buy Today rejected - all ORB plans invalid. " + "; ".join(details)


def _can_return_rejected_card_to_buylist(card: TradeCardState) -> bool:
    """Never hide a BUY whose durable lifecycle may already have started."""

    return not bool(
        card.entry_client_order_id
        or card.entry_pending_attempt_number
        or card.entry_submission_unresolved
        or card.entry_cancel_in_flight
        or card.entry_remaining_target_quantity
        or card.broker_quantity
    )


def _return_rejected_card_to_buylist(
    card: TradeCardState,
    execution_queue_item: ExecutionQueueItem,
    *,
    now: datetime,
) -> TradeCardState:
    reference = now
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    card.previous_board_status = card.board_status
    card.board_status = BoardStatus.BUYLIST
    card.board_status_updated_at = reference.astimezone(timezone.utc)
    card.session_date = None
    card.buylist_member = True
    card.buy_today_note = _orb_rejection_note(execution_queue_item)
    _clear_entry_plan(card)
    card.selected_orb_window = None
    card.entry_runtime_status = None
    card.entry_block_reason = ""
    card.next_retry_at = None
    card.entry_attempt_group_id = ""
    card.entry_attempt_count = 0
    card.entry_client_order_id = ""
    card.entry_pending_attempt_number = 0
    card.entry_submission_unresolved = False
    card.entry_cancel_in_flight = False
    card.entry_cancel_reason = ""
    card.entry_cancel_command_id = ""
    return card


class TradeCardOrbEvaluator:
    """Copies the execution queue's already-computed candidate selection
    onto a card. Never recomputes ORB/breakout/risk numbers itself --
    :mod:`src.core.orb`/:mod:`src.core.execution_queue`'s existing
    calculation remains the single source of truth for those.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _apply_candidate(
        card: TradeCardState,
        execution_queue_item: ExecutionQueueItem,
        candidate,
    ) -> None:
        queue_breakout = _positive_float(execution_queue_item.breakout_price)
        if queue_breakout is not None:
            card.breakout_price = queue_breakout
        card.selected_orb_window = (
            candidate.window or execution_queue_item.selected_window
        )
        card.entry_orb_high = candidate.orb_high
        card.entry_orb_low = candidate.orb_low
        card.entry_trigger = candidate.entry_trigger
        card.stop_adr = candidate.stop_adr
        if candidate.risk_percent:
            card.risk_percent = candidate.risk_percent
        card.planned_quantity = max(0, int(candidate.shares or 0))
        card.target_position_quantity = card.planned_quantity
        card.entry_runtime_status = _CANDIDATE_STATUS_TO_ENTRY_RUNTIME_STATUS.get(
            candidate.status, EntryRuntimeStatus.ORB_FORMING
        )
        card.entry_block_reason = (
            candidate.reason
            if candidate.status in _BLOCKED_CANDIDATE_STATUSES
            else ""
        )

    def select_crossed_candidate(
        self,
        card: TradeCardState,
        execution_queue_item: ExecutionQueueItem,
        *,
        last_price: float,
    ) -> bool:
        """Attach the best plan actually crossed by this fresh trade event.

        Automatic queue display is score-ranked, but execution cannot ignore
        a different risk-valid ORB whose lower ORH crossed between minute-bar
        refreshes. A manual window choice remains exact and excludes every
        other candidate.
        """

        if card.board_status != BoardStatus.BUY_TODAY:
            return False
        if card.entry_runtime_status not in _LIVE_SELECTION_CARD_STATUSES:
            return False
        if (
            card.entry_client_order_id
            or card.entry_pending_attempt_number
            or card.entry_submission_unresolved
            or card.entry_cancel_in_flight
        ):
            return False
        if queue_has_execution_order_lock(execution_queue_item):
            return False
        queue_account = str(execution_queue_item.account_no or "").strip()
        card_account = str(card.account_no or "").strip()
        if not queue_account or not card_account or queue_account != card_account:
            return False
        try:
            event_price = float(last_price)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(event_price) or event_price <= 0:
            return False

        candidates = dict(execution_queue_item.candidates or {})
        if execution_queue_item.manual_window_lock:
            selected_window = str(execution_queue_item.selected_window or "").strip()
            selected = candidates.get(selected_window)
            candidate_items = [(selected_window, selected)] if selected is not None else []
        else:
            candidate_items = list(candidates.items())

        now = self._clock()
        crossed = []
        for window, candidate in candidate_items:
            if (
                candidate is None
                or str(window or "").strip()
                != str(getattr(candidate, "window", "") or "").strip()
                or orb_candidate_stale_for_current_session(
                    execution_queue_item,
                    candidate,
                    now=now,
                )
                or not _complete_candidate_for_card(
                    card,
                    execution_queue_item,
                    candidate,
                )
            ):
                continue
            if event_price >= float(candidate.entry_trigger):
                crossed.append(candidate)
        if not crossed:
            return False

        chosen = max(
            crossed,
            key=lambda candidate: (
                float(getattr(candidate, "score", 0.0) or 0.0),
                -SUPPORTED_ORB_WINDOWS.index(candidate.window),
            ),
        )
        before = (
            card.breakout_price,
            card.selected_orb_window,
            card.entry_orb_high,
            card.entry_orb_low,
            card.entry_trigger,
            card.stop_adr,
            card.risk_percent,
            card.planned_quantity,
            card.target_position_quantity,
            card.entry_runtime_status,
            card.entry_block_reason,
        )
        self._apply_candidate(card, execution_queue_item, chosen)
        after = (
            card.breakout_price,
            card.selected_orb_window,
            card.entry_orb_high,
            card.entry_orb_low,
            card.entry_trigger,
            card.stop_adr,
            card.risk_percent,
            card.planned_quantity,
            card.target_position_quantity,
            card.entry_runtime_status,
            card.entry_block_reason,
        )
        return before != after

    def update_card(
        self,
        card: TradeCardState,
        execution_queue_item: ExecutionQueueItem,
    ) -> TradeCardState:
        """Mutate a Buy Today card; all other lifecycle stages are no-ops."""
        if card.board_status not in _ORB_ACTIVE_STATUSES:
            return card

        queue_account = str(
            getattr(execution_queue_item, "account_no", "") or ""
        ).strip()
        card_account = str(card.account_no or "").strip()
        if not queue_account or not card_account or queue_account != card_account:
            _clear_entry_plan(card)
            card.entry_runtime_status = EntryRuntimeStatus.RISK_INVALID
            if not queue_account:
                card.entry_block_reason = (
                    "ORB execution blocked: the queue candidate has no account "
                    "sizing provenance"
                )
            elif not card_account:
                card.entry_block_reason = (
                    "ORB execution blocked: the Buy Today card has no account identity"
                )
            else:
                card.entry_block_reason = (
                    "ORB execution blocked: the queue candidate was sized for "
                    f"account {queue_account}, not this card's account"
                )
            return card

        if execution_queue_item.name and not card.name:
            card.name = execution_queue_item.name
        queue_breakout = _positive_float(execution_queue_item.breakout_price)
        if queue_breakout is not None:
            card.breakout_price = queue_breakout
        observed_price = _positive_float(execution_queue_item.current_price)
        if observed_price is not None:
            card.market_data_last_trusted_price = observed_price
            card.market_data_last_trusted_at = execution_queue_item.last_updated

        now = self._clock()
        candidates = dict(getattr(execution_queue_item, "candidates", {}) or {})
        selected = getattr(execution_queue_item, "selected_candidate", None)
        candidate_records = dict(candidates)
        stale_windows = {
            str(window)
            for window, queue_candidate in candidate_records.items()
            if orb_candidate_stale_for_current_session(
                execution_queue_item,
                queue_candidate,
                now=now,
            )
        }
        selected_window = str(getattr(selected, "window", "") or "").strip()
        if selected is not None and selected_window and (
            orb_candidate_stale_for_current_session(
                execution_queue_item,
                selected,
                now=now,
            )
        ):
            stale_windows.add(selected_window)
        candidate_windows = {
            str(window or "").strip() for window in candidate_records
        }
        if selected_window:
            candidate_windows.add(selected_window)
        empty_snapshot_stale = (
            not candidate_windows
            and orb_snapshot_stale_for_current_session(
                execution_queue_item,
                now=now,
            )
        )
        if (
            candidate_windows
            and candidate_windows.issubset(stale_windows)
        ) or empty_snapshot_stale:
            _clear_entry_plan(card)
            card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            card.entry_block_reason = "Current-session ORB minute bars are unavailable"
            card.selected_orb_window = execution_queue_item.selected_window
            return card

        if all_supported_orb_plans_rejected(
            execution_queue_item,
            excluded_windows=stale_windows,
        ) and _can_return_rejected_card_to_buylist(card):
            return _return_rejected_card_to_buylist(
                card,
                execution_queue_item,
                now=now,
            )

        candidate = _display_candidate(
            execution_queue_item,
            excluded_windows=stale_windows,
        )
        if (
            card.board_status == BoardStatus.BUY_TODAY
            and _regular_session_complete(now=now)
            and (
                candidate is None
                or candidate.status == OrbCandidateStatus.FORMING
            )
        ):
            _clear_entry_plan(card)
            card.entry_runtime_status = EntryRuntimeStatus.SESSION_COMPLETE
            card.entry_block_reason = "Regular session is complete"
            card.selected_orb_window = (
                candidate.window
                if candidate is not None
                else execution_queue_item.selected_window
            )
            return card
        if candidate is None:
            # No ORB window has produced even a displayable plan yet.
            _clear_entry_plan(card)
            card.entry_runtime_status = EntryRuntimeStatus.ORB_FORMING
            card.entry_block_reason = ""
            card.selected_orb_window = None
            return card

        # ORB observations may refresh while an entry attempt is cooling
        # down.  Copy the newest verified geometry, but leave retry timing to
        # TradingEngine._recover_retryable_cards; otherwise this one-second
        # bridge would turn RETRY_COOLDOWN back into EXECUTE_READY early.
        prior_runtime_status = card.entry_runtime_status
        prior_block_reason = card.entry_block_reason
        self._apply_candidate(card, execution_queue_item, candidate)

        if candidate.status in {
            OrbCandidateStatus.WAITING_BREAKOUT,
            OrbCandidateStatus.VALID,
            OrbCandidateStatus.EXECUTE_READY,
        } and (
            _positive_float(candidate.entry_trigger) is None
            or _positive_float(candidate.orb_high) is None
            or _positive_float(candidate.orb_low) is None
            or card.planned_quantity <= 0
        ):
            _clear_entry_plan(card)
            card.entry_runtime_status = EntryRuntimeStatus.RISK_INVALID
            card.entry_block_reason = "ORB candidate is missing executable plan fields"
            return card

        if prior_runtime_status == EntryRuntimeStatus.RETRY_COOLDOWN:
            card.entry_runtime_status = prior_runtime_status
            card.entry_block_reason = prior_block_reason

        # Deliberately does not touch card.warnings -- STOP_REQUIRED/
        # DATA_STALE and any other warning set elsewhere (position_manager,
        # trading_engine) must not be clobbered by an ORB-only refresh.
        return card
