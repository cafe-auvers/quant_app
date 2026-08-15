"""End-of-day cleanup and startup reconciliation.
``buydashboard_to_kanban.md`` section 13 and Phase 6 (section 1070-1075).

Like :mod:`src.services.entry_attempt_manager`/:mod:`src.services.position_manager`,
this is synchronous and takes explicit input/output: the caller (in
production, :mod:`src.services.trading_engine`'s heartbeat, gated on
``EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE``) supplies the current cards and
gets back the ones that changed, to persist via
:mod:`src.services.trade_card_repository`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sqlalchemy.engine import Engine

from src.core.order_state import (
    BrokerOrder,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderSide,
    OrderStatus,
    is_open_status,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import capital_allocator
from src.services.entry_attempt_manager import (
    AttemptDeadlineAction,
    EntryAttemptManager,
    EntryCancelReason,
)
from src.services.position_manager import (
    BrokerHolding,
    PositionManager,
    extract_overseas_holdings,
)

logger = logging.getLogger(__name__)

# Statuses that mean "the broker has already finished with this order" --
# no cancel call is needed or safe to attempt.
_ALREADY_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}
_UNRESOLVED_STATUSES = {OrderStatus.UNKNOWN_SUBMISSION_STATE, OrderStatus.UNKNOWN}


@dataclass
class EodActionCallbacks:
    """Injected broker-boundary actions -- this module never imports
    :mod:`src.api.kis_order` directly, mirroring
    :mod:`src.services.entry_attempt_manager`/:mod:`src.services.position_manager`.
    """

    find_open_entry_order: Callable[[TradeCardState], Optional[BrokerOrder]]
    reconcile_order: Callable[[BrokerOrder], BrokerOrder]
    cancel_order: Callable[[str], None]  # by client_order_id
    # Full account-wide discovery (open orders + history + reserved orders)
    # for this card's symbol. Code review finding P1-15: "no local order
    # found" must never by itself be treated as "no order exists" -- only a
    # *complete* discovery query (BrokerOrderDiscoveryResult.complete,
    # i.e. every source succeeded with no errors) can confirm that.
    # Optional/defaulted to an always-complete-empty result so existing
    # callers/tests that never exercise the "no order" path are unaffected.
    discover_all_orders: Callable[[TradeCardState], BrokerOrderDiscoveryResult] = (
        lambda card: BrokerOrderDiscoveryResult(
            open_orders_complete=True, history_complete=True, reserved_orders_complete=True
        )
    )
    # Review finding P0: "broker-order matching is too broad and can match
    # old orders" -- KIS regular-order discovery defaults to a ~14-day
    # history window and BrokerOrderStatusSnapshot.checked_at is query
    # time, not submission time, so a historical FILLED BUY for the same
    # symbol that was later sold could otherwise be mistaken for this
    # card's current missing entry order and wrongly resurrect a position.
    # A historical fill is only trusted to reopen a position when the
    # account's *current* holdings independently confirm the shares still
    # exist -- see _find_matching_broker_entry_snapshot. Optional/defaulted
    # to "unknown" (None): unknown holdings means that safety gate refuses
    # to trust the historical fill rather than guessing either way, so
    # existing callers/tests that never wire this are unaffected for the
    # already-covered still-working-order and no-match paths.
    get_current_holding: Callable[[TradeCardState], Optional[BrokerHolding]] = (
        lambda card: None
    )
    # Review finding P0: "untracked partially filled orders are still not
    # controlled" -- a discovered broker order with nothing local tracking
    # it previously was only ever flagged (UNRECONCILED_BROKER_ORDER),
    # never actually stopped, so its unfilled remainder could keep filling
    # unnoticed. KIS's real cancel endpoint is keyed by broker_order_id
    # (not our own client_order_id), which a discovered
    # BrokerOrderStatusSnapshot already carries -- this callback lets a
    # best-effort direct cancel be attempted without first reconstructing
    # a full local BrokerOrder record. Not the full two-phase
    # cancel-then-confirm state machine tracked orders get (there is no
    # local order to poll for confirmation); may be retried on a later
    # reconciliation pass if the order is rediscovered still open.
    # Optional/defaulted to a no-op so existing callers/tests that never
    # wire this are unaffected.
    cancel_discovered_order: Callable[[TradeCardState, BrokerOrderStatusSnapshot], None] = (
        lambda card, snapshot: None
    )


class EodTradingService:
    def __init__(
        self,
        *,
        entry_attempt_manager: EntryAttemptManager,
        position_manager: PositionManager,
        callbacks: EodActionCallbacks,
        reservations_path: Optional[Path] = None,
        capital_reservation_engine: Optional[Engine] = None,
    ) -> None:
        self._entry_attempt_manager = entry_attempt_manager
        self._position_manager = position_manager
        self._callbacks = callbacks
        # Resolved once here rather than relying on capital_allocator's own
        # function-default parameter, which is bound at import time and
        # will not pick up a later monkeypatch/override -- same reasoning
        # as EntryAttemptManager.__init__.
        self._reservations_path = reservations_path or capital_allocator.RESERVATIONS_FILE
        # Review finding P1-1: mirror capital-reservation releases to the
        # shared database too, when one is available, for the same
        # cross-device-visibility reason EntryAttemptManager does.
        self._capital_reservation_engine = capital_reservation_engine

    def run_eod_cleanup(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Section 13's table, applied to every card. The caller owns the
        timing gate (``EOD_ENTRY_CLEANUP_SECONDS_BEFORE_CLOSE`` before
        close) -- this method always processes whatever it's given.
        """
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status == BoardStatus.BUY_TODAY:
                if self._reset_buy_today_with_no_order(card):
                    changed.append(card)
            elif card.board_status == BoardStatus.ENTRY_PENDING:
                if self._resolve_entry_pending_at_eod(card):
                    changed.append(card)
            elif card.board_status == BoardStatus.OPEN_POSITION:
                if self._stop_incomplete_target_completion(card):
                    changed.append(card)
        return changed

    # -- "Buy Today with no submitted order" (section 512-516) ----------

    def _reset_buy_today_with_no_order(self, card: TradeCardState) -> bool:
        discovered = _mark_buy_today_order_discovered(card, callbacks=self._callbacks)
        if discovered is None:
            # No local order and either a local order *does* exist (stale
            # board_status -- leave it for the ENTRY_PENDING branch) or
            # discovery was incomplete (retry later); either way this
            # method's own "genuinely no order" reset must not run.
            return False
        if discovered:
            # Review finding P0: "Buy Today can remain eligible despite a
            # discovered broker order" -- a matching broker order was
            # found and the card was already moved off BUY_TODAY (see
            # _mark_buy_today_order_discovered) so _evaluate_buy_today can
            # never treat it as a fresh candidate again.
            return True
        card.board_status = BoardStatus.BUYLIST
        card.entry_runtime_status = None
        card.entry_block_reason = ""
        card.entry_orb_high = None
        card.entry_orb_low = None
        card.entry_orb_window = None
        card.entry_trigger = None
        self._entry_attempt_manager.reset_symbol(card.environment, card.account_no, card.symbol)
        if card.capital_reservation_id:
            capital_allocator.release_reservation(
                card.capital_reservation_id,
                path=self._reservations_path,
                engine=self._capital_reservation_engine,
            )
            card.capital_reservation_id = ""
        return True

    # -- Entry Pending at EOD (section 517-529, review finding P0-7) -----

    def _resolve_entry_pending_at_eod(self, card: TradeCardState) -> bool:
        """Drives the *same* two-phase request-then-confirm cancellation
        state machine (:meth:`EntryAttemptManager.resolve_entry_order`) the
        normal intraday heartbeat uses, instead of calling ``cancel_order``
        and immediately assuming the cancellation succeeded. Review finding
        P0-7: the old version released the capital reservation and moved
        the card to Buylist/Open Positions in the same call that requested
        the cancel -- if the broker actually filled part or all of the
        order *after* that instant, the fill would be silently orphaned
        (no card, no reservation, no stop). Now:

        ENTRY_PENDING -> (cancel requested, EOD reason stamped) ->
        AWAIT_CANCEL_CONFIRMATION -> next call confirms ->
        zero fill -> Buylist / any fill -> Open Positions / still
        unresolved -> stays Entry Pending (Reconciling).
        """
        order = self._callbacks.find_open_entry_order(card)
        if order is None:
            # P1-15: the local lookup alone cannot confirm "no order
            # exists" -- require a complete account-wide discovery first.
            # An incomplete query must leave the card in
            # Entry Pending/Reconciling (section 526-529's "do not assume
            # cancellation" applies just as much to "assume no order").
            discovery = self._callbacks.discover_all_orders(card)
            if not discovery.complete:
                if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                    card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                    return True
                return False
            # Review finding P0 (same pattern as
            # reconcile_unresolved_orders_at_startup): a complete discovery
            # query confirms the local ledger has nothing, but not that the
            # broker has nothing -- moving straight to Buylist without ever
            # inspecting discovery.snapshots would silently orphan a real
            # broker order the local ledger lost track of, right as the
            # session is ending.
            match = _find_matching_broker_entry_snapshot(
                discovery,
                card,
                get_current_holding=lambda: self._callbacks.get_current_holding(card),
            )
            if match is None:
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                return True
            return _apply_discovered_entry_match(
                card, match, position_manager=self._position_manager, callbacks=self._callbacks
            )

        refreshed = self._callbacks.reconcile_order(order)

        if refreshed.status in _UNRESOLVED_STATUSES:
            # Section 526-529: "Remain Entry Pending — Reconciling. Do not
            # assume cancellation. Do not move to Buylist."
            card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            return True

        if not card.entry_cancel_in_flight:
            card.entry_cancel_reason = EntryCancelReason.EOD.value
        action = self._entry_attempt_manager.resolve_entry_order(
            refreshed,
            at_deadline=True,
            cancel_requested=True,
            cancel_order=lambda o: self._callbacks.cancel_order(o.client_order_id),
        )

        if action in (
            AttemptDeadlineAction.STILL_WORKING,
            AttemptDeadlineAction.BLOCK_SYMBOL_PENDING_RECONCILIATION,
        ):
            # Unresolved -- stays Entry Pending/Reconciling; capital stays
            # reserved until a later EOD pass (or startup reconciliation)
            # resolves it.
            if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                return True
            return False

        if action == AttemptDeadlineAction.AWAIT_CANCEL_CONFIRMATION:
            if card.entry_cancel_in_flight:
                return False
            card.entry_cancel_in_flight = True
            return True

        # Every remaining action is terminal -- the broker has confirmed a
        # final status for the cancel EOD requested; resolve_entry_order
        # has already settled/released the capital reservation.
        card.entry_cancel_in_flight = False
        card.entry_cancel_reason = ""
        card.capital_reservation_id = ""

        if refreshed.filled_quantity > 0:
            # Section 522-525: confirmed cancelled with a fill (or, rarely,
            # confirmed FILLED outright) -- reconcile the actual filled
            # position and move to Open Positions.
            card.board_status = BoardStatus.OPEN_POSITION
            card.broker_quantity = refreshed.filled_quantity
            card.orderable_quantity = refreshed.filled_quantity
            card.average_entry_price = refreshed.avg_fill_price
            card.entry_remaining_target_quantity = 0  # stop attempting completion at EOD
            card.position_runtime_status = PositionRuntimeStatus.OPEN
            self._position_manager.apply_first_fill_stop(
                card,
                entry_orb_low=card.entry_orb_low or 0.0,
                entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
            )
            card.entry_runtime_status = None
            return True

        # Section 517-521: confirmed cancelled with zero fill.
        card.board_status = BoardStatus.BUYLIST
        card.entry_runtime_status = None
        return True

    # -- Open position with an incomplete target (section 530-533) ------

    def _stop_incomplete_target_completion(self, card: TradeCardState) -> bool:
        if card.entry_remaining_target_quantity <= 0:
            return False
        order = self._callbacks.find_open_entry_order(card)
        # Section 530-533: stop attempting to complete the target at EOD
        # regardless of what the cancel eventually resolves to -- unlike
        # the ENTRY_PENDING case above, an already-open position stays open
        # either way, so there is no Buylist/Open-Positions branch to wait
        # for. Still request+track the cancel through two-phase
        # confirmation (rather than assuming it immediately succeeded) so a
        # late fill from *this* order is protected and its capital
        # reservation is actually settled instead of silently orphaned --
        # entry_cancel_in_flight keeps _reconcile_entry_orders tracking it
        # on the next heartbeat regardless of board_status/remaining target.
        if order is not None:
            self._callbacks.cancel_order(order.client_order_id)
            if not card.entry_cancel_in_flight:
                card.entry_cancel_in_flight = True
                card.entry_cancel_reason = EntryCancelReason.EOD.value
        card.entry_remaining_target_quantity = 0
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        return True


@dataclass
class BrokerEntryMatch:
    """Result of :func:`_find_matching_broker_entry_snapshot`: the matched
    snapshot plus whatever current-holding lookup was actually performed
    to validate it (``None`` when the match didn't need one, e.g. a
    still-open order) -- callers use ``current_holding``, when present, as
    the *authoritative* source for recovered quantity/price (review
    finding P0: "current holdings confirm existence, but not authoritative
    quantity" -- the historical order snapshot only explains why a
    position exists, it must never override live broker truth for how
    much is actually held).
    """

    snapshot: BrokerOrderStatusSnapshot
    current_holding: Optional[BrokerHolding] = None


def _find_matching_broker_entry_snapshot(
    discovery: BrokerOrderDiscoveryResult,
    card: TradeCardState,
    *,
    get_current_holding: Callable[[], Optional[BrokerHolding]] = lambda: None,
) -> Optional[BrokerEntryMatch]:
    """Finds this card's BUY entry among a *complete* broker-wide
    discovery query, so a local-ledger lookup miss is never treated as
    broker truth on its own (review finding P0). Prefers a still-open
    snapshot (the working order to leave alone, matched on symbol/side
    alone -- a genuinely still-open order surviving many days is rare
    enough that the stricter check below is not worth the risk of
    re-orphaning it); falls back to the most recently checked terminal
    one when nothing is open.

    Review finding P0: "broker-order matching is too broad and can match
    old orders" -- KIS's regular-order discovery defaults to an ~14-day
    history window, and ``checked_at`` is query time, not submission time,
    so an unrelated historical BUY for the same symbol that was already
    filled *and sold* days ago could otherwise be mistaken for this card's
    current missing entry order and wrongly resurrect a closed-out
    position. A terminal match with a fill is therefore only trusted when
    the holding returned by ``get_current_holding()`` independently
    confirms the account still actually holds shares of this symbol right
    now -- unknown/absent confirmation (``None`` or zero) means the match
    is rejected rather than guessed at either way, matching every other
    fail-closed gate in this module. ``get_current_holding`` is a
    callable, not an already-resolved value: review finding P1
    ("excessive KIS requests during reconciliation") -- most calls into
    this function have no candidates at all (nothing to match), so
    eagerly fetching current holdings on every call regardless of whether
    it would actually be needed was one extra KIS round trip per card,
    every reconciliation pass, for no reason.
    """
    candidates = [
        snapshot
        for snapshot in discovery.snapshots
        if snapshot.account_no == card.account_no
        and snapshot.symbol == card.symbol
        and snapshot.side == OrderSide.BUY
    ]
    if not candidates:
        return None
    open_candidates = [c for c in candidates if is_open_status(c.status)]
    if open_candidates:
        chosen = max(open_candidates, key=lambda c: c.checked_at)
        # Review finding P0: "the authoritative holding fix does not cover
        # open partial orders" -- a still-open order's own filled_quantity
        # (e.g. 30/100) can undercount what the account actually holds
        # right now (accumulated from more than this one order, or simply
        # stale relative to a fill that landed since this snapshot was
        # queried); fetch current holdings for this branch too, on the
        # same lazy "only when there is a fill to validate" terms as the
        # terminal-fill branch below, so _apply_discovered_entry_match can
        # prefer live broker truth here as well instead of only for
        # already-terminal matches.
        current_holding = get_current_holding() if chosen.filled_quantity > 0 else None
        return BrokerEntryMatch(chosen, current_holding=current_holding)
    terminal_with_fill = [c for c in candidates if c.filled_quantity > 0]
    current_holding = get_current_holding() if terminal_with_fill else None
    holding_confirmed = current_holding is not None and current_holding.quantity > 0
    if terminal_with_fill and not holding_confirmed:
        # A filled match exists but nothing confirms the shares are still
        # actually held -- fall back to the zero-fill terminal candidates
        # only (a plain CANCELLED/REJECTED confirmation is harmless either
        # way; only trusting a *fill* without confirmation is the danger).
        zero_fill = [c for c in candidates if c.filled_quantity <= 0]
        if not zero_fill:
            return None
        return BrokerEntryMatch(max(zero_fill, key=lambda c: c.checked_at))
    return BrokerEntryMatch(
        max(candidates, key=lambda c: c.checked_at), current_holding=current_holding
    )


def _snapshot_plausibly_belongs_to_card(
    card: TradeCardState, snapshot: BrokerOrderStatusSnapshot
) -> bool:
    """Review finding P0: "automatic cancellation is unsafe while order
    identity remains weak" -- :func:`_find_matching_broker_entry_snapshot`
    matches purely on account/symbol/side, which is loose enough to also
    match an unrelated order the user placed manually, directly at the
    broker, for the same symbol (the app never submitted anything for this
    card at all). That was only ever a cosmetic risk while a match merely
    set a warning; now that a match can trigger an actual best-effort
    broker-side cancel (:func:`_try_cancel_discovered_remainder`), firing
    it against an order this card's own plan did not produce would cancel
    a live order that has nothing to do with this application.

    This is a deliberately narrow, best-effort corroboration -- there is
    no broker order ID, client order ID, or submission timestamp recorded
    anywhere to check against for an order discovery found with *nothing*
    local tracking it (that is the entire premise of this code path) --
    not the fuller "submission session/attempt window" fingerprint a
    persisted order record could support. Requires the snapshot's own
    requested quantity to exactly match this card's planned order size and
    its limit price to fall within a generous band of this card's planned
    entry trigger; anything unset or outside tolerance fails closed. A
    failed check never blocks recovery of a confirmed fill (still handled
    by the caller) -- it only withholds the destructive cancel action,
    leaving the card flagged UNRECONCILED_BROKER_ORDER for manual review
    instead, matching this module's "ambiguous stays alert-and-block-only"
    rule everywhere else.
    """
    expected_quantity = card.planned_quantity or card.target_position_quantity
    if expected_quantity <= 0 or snapshot.quantity_requested <= 0:
        return False
    if snapshot.quantity_requested != expected_quantity:
        return False
    expected_price = card.entry_trigger
    if not expected_price or expected_price <= 0 or snapshot.limit_price <= 0:
        return False
    tolerance = max(expected_price * 0.05, 0.01)
    if abs(snapshot.limit_price - expected_price) > tolerance:
        return False
    return True


def _remove_warnings(card: TradeCardState, *names: str) -> None:
    if not card.warnings:
        return
    remove = set(names)
    if any(w in remove for w in card.warnings):
        card.warnings = [w for w in card.warnings if w not in remove]


def _try_cancel_discovered_remainder(
    card: TradeCardState, snapshot: BrokerOrderStatusSnapshot, *, callbacks: EodActionCallbacks
) -> None:
    """Best-effort direct cancel for a discovered order's unfilled
    remainder -- see :attr:`EodActionCallbacks.cancel_discovered_order`.
    Never raises: a cancel-attempt failure must not stop the rest of
    reconciliation, and the card is flagged with UNRECONCILED_BROKER_ORDER
    by the caller regardless of whether this succeeds. Gated on
    :func:`_snapshot_plausibly_belongs_to_card` (review finding P0) --
    a weak account/symbol/side-only match must never itself trigger the
    actual broker-side cancel.
    """
    if not _snapshot_plausibly_belongs_to_card(card, snapshot):
        logger.warning(
            "Refusing to auto-cancel a discovered order for %s: ownership could not be "
            "positively established against this card's plan (broker_order_id=%s) -- "
            "left flagged for manual review instead.",
            card.symbol, snapshot.broker_order_id,
        )
        return
    try:
        callbacks.cancel_discovered_order(card, snapshot)
    except Exception:
        logger.exception(
            "cancel_discovered_order failed for %s (broker_order_id=%s)",
            card.symbol, snapshot.broker_order_id,
        )


def _apply_discovered_entry_match(
    card: TradeCardState,
    match: BrokerEntryMatch,
    *,
    position_manager: PositionManager,
    callbacks: EodActionCallbacks,
) -> bool:
    """Applies a :class:`BrokerEntryMatch` to an ENTRY_PENDING card whose
    local order record was lost. Shared by
    :func:`reconcile_unresolved_orders_at_startup` and
    :meth:`EodTradingService._resolve_entry_pending_at_eod` -- both were
    independently implementing the identical recovery decision; this is
    now the single place it's made; review finding P0 ("current holdings
    confirm existence, but not authoritative quantity") is fixed once
    here for both.
    """
    snapshot = match.snapshot
    if snapshot.filled_quantity > 0:
        # Review finding P0: once available, the *current* broker holding
        # is authoritative for how many shares actually exist right now
        # -- the historical order snapshot only explains why a position
        # exists, and can be stale relative to the account's real current
        # size (e.g. a later partial exit the snapshot knows nothing
        # about). Only fall back to the order's own filled_quantity when
        # no current-holding confirmation was available at all (the
        # still-open-order path, which doesn't require one).
        if match.current_holding is not None and match.current_holding.quantity > 0:
            recovered_quantity = match.current_holding.quantity
            recovered_price = (
                match.current_holding.average_price
                or snapshot.avg_fill_price
                or card.average_entry_price
            )
        else:
            recovered_quantity = snapshot.filled_quantity
            recovered_price = snapshot.avg_fill_price or card.average_entry_price
        card.board_status = BoardStatus.OPEN_POSITION
        card.broker_quantity = recovered_quantity
        card.orderable_quantity = recovered_quantity
        card.average_entry_price = recovered_price
        # Review finding P0: "a partially filled working broker order is
        # treated as completed" -- entry_remaining_target_quantity is
        # still deliberately zeroed either way (no local order record for
        # any remainder to safely hand back to normal completion
        # tracking, so a nonzero target here would risk a *second*,
        # duplicate completion order); the still-open case below is
        # flagged for urgent attention instead of being treated as a
        # clean, fully-settled fill.
        card.entry_remaining_target_quantity = 0
        card.position_runtime_status = PositionRuntimeStatus.OPEN
        position_manager.apply_first_fill_stop(
            card,
            entry_orb_low=card.entry_orb_low or 0.0,
            entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
        )
        card.entry_runtime_status = None
        if "ORDER_RECOVERED_FROM_BROKER_DISCOVERY" not in card.warnings:
            card.warnings = [*card.warnings, "ORDER_RECOVERED_FROM_BROKER_DISCOVERY"]
        if is_open_status(snapshot.status):
            # The broker's own remainder is still genuinely live and
            # untracked, not merely "recovered." Review finding P0: an
            # untracked partial fill's remainder must not simply sit at
            # the broker unattended -- attempt a direct best-effort cancel
            # keyed by the snapshot's own broker_order_id (no local order
            # record is needed for this, unlike a full reconstruction; see
            # EodActionCallbacks.cancel_discovered_order).
            if "UNRECONCILED_BROKER_ORDER" not in card.warnings:
                card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
            _try_cancel_discovered_remainder(card, snapshot, callbacks=callbacks)
        else:
            # Review finding: a prior pass may have flagged
            # UNRECONCILED_BROKER_ORDER while this exact remainder was
            # still open; now that the same snapshot resolves to a
            # terminal status (cancelled, or simply finished filling), the
            # warning is stale and must not keep alerting as "still needs
            # manual review" forever.
            _remove_warnings(card, "UNRECONCILED_BROKER_ORDER")
        return True
    if snapshot.status in _ALREADY_TERMINAL_STATUSES:
        # Terminal with zero fill (CANCELLED/REJECTED/EXPIRED) --
        # confirmed gone, safe to return to Buylist. Review finding: clear
        # any recovery-related warnings a prior pass may have set for this
        # same discovered order -- nothing about it is still relevant once
        # the card is back to a clean Buylist state.
        card.board_status = BoardStatus.BUYLIST
        card.entry_runtime_status = None
        _remove_warnings(card, "UNRECONCILED_BROKER_ORDER", "ORDER_RECOVERED_FROM_BROKER_DISCOVERY")
        return True
    # A real broker order is still open/working with nothing local
    # tracking it -- never abandon it to Buylist. The local order record
    # itself still needs full reconstruction to hand it back to normal
    # completion tracking (not implemented here), but a direct best-effort
    # cancel of the untracked remainder (keyed by the snapshot's own
    # broker_order_id) is a bounded, safe mitigation that does not require
    # that reconstruction -- see EodActionCallbacks.cancel_discovered_order.
    # This still flags the card for attention regardless of whether the
    # cancel attempt itself succeeds.
    changed_here = card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE
    card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
    if "UNRECONCILED_BROKER_ORDER" not in card.warnings:
        card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
        changed_here = True
    _try_cancel_discovered_remainder(card, snapshot, callbacks=callbacks)
    return changed_here


def _mark_buy_today_order_discovered(
    card: TradeCardState, *, callbacks: EodActionCallbacks
) -> Optional[bool]:
    """Checks a BUY_TODAY card's local order lookup against a *complete*
    broker-wide discovery query, and -- if a real broker order is found --
    moves the card off BUY_TODAY immediately so it can never again be
    treated as a fresh entry candidate. Shared by
    :meth:`EodTradingService._reset_buy_today_with_no_order` (EOD) and
    :func:`reconcile_buy_today_orders` (periodic, review finding P0: "Buy
    Today broker-order recovery is effectively EOD-only" -- this check was
    previously reachable only once the EOD window was reached, leaving a
    BUY_TODAY card whose local order record was lost free to be treated
    as a fresh candidate -- and potentially resubmitted -- for the entire
    rest of the trading day).

    Returns:
    - ``None`` if discovery was incomplete (retry later).
    - ``True`` if a local order was found (the card's board_status was
      simply stale -- moved to ENTRY_PENDING/ORDER_PENDING here, mirroring
      the normal ``AttemptOutcome.SUBMITTED`` transition) or a matching
      broker order was found via discovery (card moved to
      ENTRY_PENDING/DATA_UNAVAILABLE, flagged UNRECONCILED_BROKER_ORDER).
    - ``False`` if discovery is complete and confirms nothing exists --
      the caller decides what "safe, no order" means in its own context.
    """
    order = callbacks.find_open_entry_order(card)
    if order is not None:
        # Review finding P0: "a local order with stale BUY_TODAY card
        # state remains uncorrected" -- a real local order already exists
        # (e.g. a crash landed between the broker confirming submission
        # and this card's board_status being updated to ENTRY_PENDING),
        # but leaving board_status at BUY_TODAY keeps it outside
        # TradingEngine._entry_tracking_scope entirely: no fill ever gets
        # a stop attached, no cancellation is ever tracked. The duplicate-
        # order guard (order_execution_service.DuplicateOpenOrderError)
        # already stops _evaluate_buy_today from submitting a second
        # broker order for this symbol, but that alone does not resume
        # normal reconciliation for the order that already exists -- only
        # actually moving the card does.
        card.board_status = BoardStatus.ENTRY_PENDING
        card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
        if getattr(order, "attempt_group_id", ""):
            card.entry_attempt_group_id = order.attempt_group_id
        if getattr(order, "attempt_number", 0):
            card.entry_attempt_count = order.attempt_number
        return True
    # P1-15: a single "not found" lookup is not proof nothing was
    # submitted -- confirm with a full account-wide discovery query first.
    discovery = callbacks.discover_all_orders(card)
    if not discovery.complete:
        return None
    match = _find_matching_broker_entry_snapshot(
        discovery, card, get_current_holding=lambda: callbacks.get_current_holding(card)
    )
    if match is None:
        return False
    card.board_status = BoardStatus.ENTRY_PENDING
    card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
    if "UNRECONCILED_BROKER_ORDER" not in card.warnings:
        card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
    return True


def reconcile_buy_today_orders(
    cards: List[TradeCardState], *, callbacks: EodActionCallbacks
) -> List[TradeCardState]:
    """Runs :func:`_mark_buy_today_order_discovered` over every BUY_TODAY
    card -- see that function's docstring for the review finding this
    closes. Intended to run on the periodic (e.g. 60s) reconciliation
    cadence, not only at EOD; deliberately does *not* perform
    :meth:`EodTradingService._reset_buy_today_with_no_order`'s other
    "genuinely no order" duties (releasing capital reservations, clearing
    ORB state) -- those remain EOD's job so a Buy Today candidate is not
    reset out from under the user mid-session just because no order has
    been submitted yet.
    """
    changed: List[TradeCardState] = []
    for card in cards:
        if card.board_status != BoardStatus.BUY_TODAY:
            continue
        try:
            if _mark_buy_today_order_discovered(card, callbacks=callbacks):
                changed.append(card)
        except Exception:
            logger.exception("reconcile_buy_today_orders failed for %s", card.symbol)
    return changed


def reconcile_untracked_position_remainders(
    cards: List[TradeCardState], *, callbacks: EodActionCallbacks
) -> List[TradeCardState]:
    """Review finding P0: "a partially filled discovered order is not
    retried on the next reconciliation pass" -- once
    :func:`_apply_discovered_entry_match` recovers a discovery-matched fill
    into OPEN_POSITION, the card falls out of every other reconciliation
    sweep for good: :func:`reconcile_unresolved_orders_at_startup` only
    ever revisits ENTRY_PENDING cards, and the recovered card's
    ``entry_remaining_target_quantity`` is deliberately left at zero (no
    local order record exists to safely hand the remainder back to normal
    completion tracking). A single failed
    :attr:`EodActionCallbacks.cancel_discovered_order` attempt was
    therefore the last word on a broker-side remainder that might still be
    filling, unattended, indefinitely.

    This is the periodic/startup sweep that keeps chasing that remainder:
    every OPEN_POSITION card still flagged ``UNRECONCILED_BROKER_ORDER``
    gets rediscovered and, while still open at the broker, gets another
    best-effort direct cancel attempt (also review finding P0: gated by
    the same :func:`_snapshot_plausibly_belongs_to_card` ownership check
    every other automatic cancel uses). Once discovery no longer finds an
    open match for it -- confirmed cancelled, confirmed fully filled, or
    simply gone -- current holdings (when available) are applied as the
    final authoritative quantity/price and the warning is cleared, so this
    stops being reprocessed by future passes.
    """
    changed: List[TradeCardState] = []
    for card in cards:
        if card.board_status != BoardStatus.OPEN_POSITION:
            continue
        if "UNRECONCILED_BROKER_ORDER" not in card.warnings:
            continue
        try:
            discovery = callbacks.discover_all_orders(card)
            if not discovery.complete:
                continue  # retry next pass -- do not guess either way
            match = _find_matching_broker_entry_snapshot(
                discovery, card, get_current_holding=lambda: callbacks.get_current_holding(card)
            )
            if match is not None and is_open_status(match.snapshot.status):
                _try_cancel_discovered_remainder(card, match.snapshot, callbacks=callbacks)
                continue
            # Nothing open matches this card any more -- either the
            # remainder resolved (cancelled, or finished filling) or it's
            # no longer discoverable. Current holdings, if available,
            # remain authoritative for the final quantity/price, the same
            # rule every other discovery-recovered fill in this module
            # uses.
            if (
                match is not None
                and match.current_holding is not None
                and match.current_holding.quantity > 0
            ):
                card.broker_quantity = match.current_holding.quantity
                card.orderable_quantity = match.current_holding.quantity
                card.average_entry_price = (
                    match.current_holding.average_price or card.average_entry_price
                )
            _remove_warnings(card, "UNRECONCILED_BROKER_ORDER")
            changed.append(card)
        except Exception:
            logger.exception("reconcile_untracked_position_remainders failed for %s", card.symbol)
    return changed


def reconcile_unresolved_orders_at_startup(
    cards: List[TradeCardState],
    *,
    position_manager: PositionManager,
    callbacks: EodActionCallbacks,
) -> List[TradeCardState]:
    """Section 1029 ("Application restart with unknown order") and Phase 6's
    "Run full startup reconciliation" (section 1070-1075): review finding
    P1-16 -- a restart must resolve every ENTRY_PENDING card's *order*
    state, not only broker *positions* (which
    :func:`run_startup_reconciliation`/``reconcile_broker_positions``
    already covers).

    Unlike EOD cleanup, this never forces a cancellation -- a still-working
    order is left alone for the normal heartbeat to keep managing; this
    only applies what reconciliation already reveals (a fill, or a
    broker-confirmed terminal status), and still refuses to assume "no
    order" from anything less than a complete discovery query (P1-15).
    """
    changed: List[TradeCardState] = []
    for card in cards:
        if card.board_status != BoardStatus.ENTRY_PENDING:
            continue
        order = callbacks.find_open_entry_order(card)
        if order is None:
            discovery = callbacks.discover_all_orders(card)
            if not discovery.complete:
                if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                    card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                    changed.append(card)
                continue
            # Review finding P0: a *complete* discovery query confirms the
            # local ledger has nothing open for this symbol, but that is
            # not the same as confirming the broker has nothing either --
            # the previous version moved straight to Buylist without ever
            # inspecting discovery.snapshots, which would silently orphan
            # a real broker order (filled or still working) that the local
            # ledger simply lost track of (a lost write, a restart mid-
            # submission, a record from another device/process, ...).
            match = _find_matching_broker_entry_snapshot(
                discovery, card, get_current_holding=lambda: callbacks.get_current_holding(card)
            )
            if match is None:
                # Confirmed by a complete broker-wide query, not merely a
                # local lookup miss -- genuinely nothing to recover.
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                changed.append(card)
                continue
            if _apply_discovered_entry_match(
                card, match, position_manager=position_manager, callbacks=callbacks
            ):
                changed.append(card)
            continue

        refreshed = callbacks.reconcile_order(order)
        if refreshed.status in _UNRESOLVED_STATUSES:
            if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
                changed.append(card)
            continue

        if refreshed.filled_quantity > 0:
            card.board_status = BoardStatus.OPEN_POSITION
            card.broker_quantity = refreshed.filled_quantity
            card.orderable_quantity = refreshed.filled_quantity
            card.average_entry_price = refreshed.avg_fill_price
            # Review finding P0 (same pattern as the discovery-recovered
            # branch above): only a genuinely *terminal* fill means
            # completion has actually stopped. A still-open
            # PARTIALLY_FILLED order (say 30/100 filled, 70 still working)
            # must keep its real remaining target so the card stays in
            # _reconcile_entry_orders' tracking scope -- unlike the
            # discovery-recovered case, a real local order record already
            # exists here (this is the "order is not None" branch), so the
            # normal heartbeat can safely keep reconciling this exact
            # order, and _process_entry_completion's own
            # find_open_entry_order check already prevents it from
            # submitting a duplicate for the remainder in the meantime.
            still_open = is_open_status(refreshed.status)
            card.entry_remaining_target_quantity = (
                max(0, card.target_position_quantity - refreshed.filled_quantity)
                if still_open
                else 0
            )
            card.position_runtime_status = (
                PositionRuntimeStatus.ENTRY_COMPLETING
                if still_open and card.entry_remaining_target_quantity > 0
                else PositionRuntimeStatus.OPEN
            )
            position_manager.apply_first_fill_stop(
                card,
                entry_orb_low=card.entry_orb_low or 0.0,
                entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
            )
            card.entry_runtime_status = None
            changed.append(card)
        elif refreshed.status in _ALREADY_TERMINAL_STATUSES:
            # Terminal with zero fill (CANCELLED/REJECTED/EXPIRED) --
            # confirmed gone, safe to return to Buylist.
            card.board_status = BoardStatus.BUYLIST
            card.entry_runtime_status = None
            changed.append(card)
        # else: genuinely still working (ACCEPTED/WORKING/CANCEL_REQUESTED)
        # -- leave it for the normal heartbeat; a restart must not force a
        # cancellation just because the process came back up.
    return changed


def run_startup_reconciliation(
    cards: List[TradeCardState],
    *,
    environment: str,
    account_no: str,
    position_snapshot: Optional[Dict],
    position_manager: PositionManager,
    symbol_name_lookup: Callable[[str], str] = lambda symbol: symbol,
    order_callbacks: Optional[EodActionCallbacks] = None,
) -> List[TradeCardState]:
    """Section 1070-1075 ("Run full startup reconciliation") and the
    "Application restart with open positions" / "Broker position exists
    without local card" test scenarios (section 1028-1032).

    Unlike :mod:`src.services.handoff_reconciliation` (which runs
    specifically when a device claims main-device status),
    this runs at every process startup regardless of main/secondary role --
    read-only devices still need their local card view corrected against
    broker truth before showing anything to the user. Composes
    :func:`src.services.position_manager.extract_overseas_holdings` with
    :meth:`~src.services.position_manager.PositionManager.reconcile_broker_positions`.

    "Resume queued exits after restart" needs no separate mechanism here:
    board_status/``sell_all_at_market_open``/``exit_all_required`` are
    already persisted per-card in :mod:`src.services.trade_card_repository`,
    so the next heartbeat pass (:mod:`src.services.trading_engine`) resumes
    exactly where it left off once this function has corrected quantities
    against broker truth.

    ``order_callbacks``, when supplied, additionally reconciles every
    ENTRY_PENDING card's *order* state via
    :func:`reconcile_unresolved_orders_at_startup` (review finding P1-16) --
    positions alone are not enough to safely resume: an order that filled
    or was cancelled entirely while the process was down needs the same
    broker-truth correction a live position quantity gets.
    """
    holdings: List[BrokerHolding] = extract_overseas_holdings(position_snapshot)
    changed = position_manager.reconcile_broker_positions(
        cards,
        holdings,
        environment=environment,
        account_no=account_no,
        symbol_name_lookup=symbol_name_lookup,
    )
    if order_callbacks is not None:
        order_changed = reconcile_unresolved_orders_at_startup(
            cards, position_manager=position_manager, callbacks=order_callbacks
        )
        seen = {id(card) for card in changed}
        for card in order_changed:
            if id(card) not in seen:
                seen.add(id(card))
                changed.append(card)
    return changed
