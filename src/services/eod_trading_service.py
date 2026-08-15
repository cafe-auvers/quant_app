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
        order = self._callbacks.find_open_entry_order(card)
        if order is not None:
            # An order does exist -- this card's board_status is stale
            # (should already be ENTRY_PENDING); leave it for that branch
            # rather than silently discarding a live order's trail here.
            return False
        # P1-15: a single "not found" lookup is not proof nothing was
        # submitted -- confirm with a full account-wide discovery query
        # before treating this as safe. An incomplete/failed discovery
        # leaves the card exactly where it is for a later heartbeat to
        # retry, rather than silently resetting on a possibly-stale view.
        discovery = self._callbacks.discover_all_orders(card)
        if not discovery.complete:
            return False
        # Review finding P0 (same pattern as reconcile_unresolved_orders_at_startup):
        # a complete discovery query confirms the local ledger has nothing,
        # but not that the broker has nothing -- a matching snapshot means
        # this card's board_status is stale (an order genuinely exists).
        matching = _find_matching_broker_entry_snapshot(
            discovery, card, current_holding=self._callbacks.get_current_holding(card)
        )
        if matching is not None:
            # Review finding P0: "Buy Today can remain eligible despite a
            # discovered broker order" -- merely returning False here left
            # the card at BUY_TODAY, where the *next* heartbeat tick's
            # _evaluate_buy_today stage (which runs before EOD cleanup
            # every tick) would still treat it as a fresh candidate and
            # could submit a genuinely duplicate entry the instant its
            # runtime status next reads EXECUTE_READY. Move it out of
            # BUY_TODAY immediately so no further automatic submission is
            # possible, and correctly reflect that an order exists rather
            # than silently leaving the board stale.
            card.board_status = BoardStatus.ENTRY_PENDING
            card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            if "UNRECONCILED_BROKER_ORDER" not in card.warnings:
                card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
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
            matching = _find_matching_broker_entry_snapshot(
                discovery, card, current_holding=self._callbacks.get_current_holding(card)
            )
            if matching is None:
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                return True
            if matching.filled_quantity > 0:
                card.board_status = BoardStatus.OPEN_POSITION
                card.broker_quantity = matching.filled_quantity
                card.orderable_quantity = matching.filled_quantity
                card.average_entry_price = matching.avg_fill_price or card.average_entry_price
                # Review finding P0: "a partially filled working broker
                # order is treated as completed" -- see the identical
                # comment in reconcile_unresolved_orders_at_startup. Always
                # zeroed (no local order record exists for the remainder
                # to safely hand back to normal tracking), but the
                # still-open case is flagged for urgent attention rather
                # than treated as a clean, fully-settled fill.
                card.entry_remaining_target_quantity = 0
                card.position_runtime_status = PositionRuntimeStatus.OPEN
                self._position_manager.apply_first_fill_stop(
                    card,
                    entry_orb_low=card.entry_orb_low or 0.0,
                    entry_orb_window=card.entry_orb_window or card.selected_orb_window or "",
                )
                card.entry_runtime_status = None
                if "ORDER_RECOVERED_FROM_BROKER_DISCOVERY" not in card.warnings:
                    card.warnings = [*card.warnings, "ORDER_RECOVERED_FROM_BROKER_DISCOVERY"]
                if is_open_status(matching.status) and "UNRECONCILED_BROKER_ORDER" not in card.warnings:
                    card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
                return True
            if matching.status in _ALREADY_TERMINAL_STATUSES:
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                return True
            # Still open at the broker with nothing local tracking it --
            # never abandon it to Buylist right before the session ends.
            # Flag it for attention rather than attempt an automatic
            # cancel against an order this process cannot fully verify
            # ownership of from a snapshot alone.
            changed_here = card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE
            card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            if "UNRECONCILED_BROKER_ORDER" not in card.warnings:
                card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
                changed_here = True
            return changed_here

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


def _find_matching_broker_entry_snapshot(
    discovery: BrokerOrderDiscoveryResult,
    card: TradeCardState,
    *,
    current_holding: Optional[BrokerHolding] = None,
) -> Optional[BrokerOrderStatusSnapshot]:
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
    ``current_holding`` independently confirms the account still actually
    holds shares of this symbol right now -- unknown/absent confirmation
    (``current_holding`` is ``None`` or zero) means the match is rejected
    rather than guessed at either way, matching every other fail-closed
    gate in this module.
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
        return max(open_candidates, key=lambda c: c.checked_at)
    terminal_with_fill = [c for c in candidates if c.filled_quantity > 0]
    holding_confirmed = current_holding is not None and current_holding.quantity > 0
    if terminal_with_fill and not holding_confirmed:
        # A filled match exists but nothing confirms the shares are still
        # actually held -- fall back to the zero-fill terminal candidates
        # only (a plain CANCELLED/REJECTED confirmation is harmless either
        # way; only trusting a *fill* without confirmation is the danger).
        zero_fill = [c for c in candidates if c.filled_quantity <= 0]
        if not zero_fill:
            return None
        return max(zero_fill, key=lambda c: c.checked_at)
    return max(candidates, key=lambda c: c.checked_at)


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
            matching = _find_matching_broker_entry_snapshot(
                discovery, card, current_holding=callbacks.get_current_holding(card)
            )
            if matching is None:
                # Confirmed by a complete broker-wide query, not merely a
                # local lookup miss -- genuinely nothing to recover.
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                changed.append(card)
                continue
            if matching.filled_quantity > 0:
                # Protect the fill immediately, exactly like a locally-
                # tracked order's fill is protected below -- an untracked
                # filled position is the dangerous case this guards
                # against.
                card.board_status = BoardStatus.OPEN_POSITION
                card.broker_quantity = matching.filled_quantity
                card.orderable_quantity = matching.filled_quantity
                card.average_entry_price = matching.avg_fill_price or card.average_entry_price
                # Review finding P0: "a partially filled working broker
                # order is treated as completed" -- matching.status can
                # itself be an *open* status (e.g. a PARTIALLY_FILLED
                # snapshot, 30/100 filled, 70 still live at the broker).
                # entry_remaining_target_quantity is still deliberately
                # zeroed either way -- with no local order record for the
                # remainder (reconstruction not implemented), letting the
                # normal completion machinery see a nonzero remaining
                # target would risk submitting a *second*, duplicate
                # completion order on top of the one already working.
                # Zeroing it only stops *this app* from submitting
                # anything further; it does not mean the broker's own
                # remaining order is gone, which is exactly why the
                # still-open case is flagged for urgent manual attention
                # below rather than treated as a clean, fully-settled fill.
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
                if is_open_status(matching.status) and "UNRECONCILED_BROKER_ORDER" not in card.warnings:
                    # The broker's own remainder is still genuinely live
                    # and untracked, not merely "recovered."
                    card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
                changed.append(card)
                continue
            if matching.status in _ALREADY_TERMINAL_STATUSES:
                # Terminal with zero fill (CANCELLED/REJECTED/EXPIRED) --
                # confirmed gone, safe to return to Buylist.
                card.board_status = BoardStatus.BUYLIST
                card.entry_runtime_status = None
                changed.append(card)
                continue
            # A real broker order is still open/working with nothing local
            # tracking it -- never abandon it to Buylist. It cannot yet be
            # automatically cancelled/repriced (the local order record
            # itself still needs reconstruction, not implemented here), so
            # this only flags it for attention rather than pretending it
            # doesn't exist.
            if card.entry_runtime_status != EntryRuntimeStatus.DATA_UNAVAILABLE:
                card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            if "UNRECONCILED_BROKER_ORDER" not in card.warnings:
                card.warnings = [*card.warnings, "UNRECONCILED_BROKER_ORDER"]
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
