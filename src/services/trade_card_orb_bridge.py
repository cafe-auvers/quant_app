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

from src.core.execution_queue import ExecutionQueueItem, OrbCandidateStatus
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState

# A card only has an entry *plan* to sync while it is still pre-position --
# section 620 freezes entry_orb_low/entry_orb_window/entry_trigger/stop_adr
# at first fill ("the position manager should not repeatedly recalculate
# the historical entry ORB after the position has been opened"), and this
# bridge must never overwrite that frozen record once a card has moved
# past BUY_TODAY.
_PRE_ENTRY_STATUSES = {BoardStatus.WATCHLIST, BoardStatus.BUYLIST, BoardStatus.BUY_TODAY}

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


class TradeCardOrbEvaluator:
    """Copies the execution queue's already-computed candidate selection
    onto a card. Never recomputes ORB/breakout/risk numbers itself --
    :mod:`src.core.orb`/:mod:`src.core.execution_queue`'s existing
    calculation remains the single source of truth for those.
    """

    def update_card(
        self,
        card: TradeCardState,
        execution_queue_item: ExecutionQueueItem,
    ) -> TradeCardState:
        """Mutates and returns ``card``. A no-op once the card has moved
        past BUY_TODAY (see ``_PRE_ENTRY_STATUSES``)."""
        if card.board_status not in _PRE_ENTRY_STATUSES:
            return card

        if execution_queue_item.name and not card.name:
            card.name = execution_queue_item.name
        card.breakout_price = execution_queue_item.breakout_price

        candidate = execution_queue_item.selected_candidate
        if candidate is None:
            # No ORB window has produced a usable candidate yet (e.g. still
            # forming, or the whole symbol has no valid window today) --
            # nothing to size an entry off of.
            card.entry_runtime_status = EntryRuntimeStatus.ORB_FORMING
            card.entry_block_reason = ""
            return card

        card.selected_orb_window = candidate.window or execution_queue_item.selected_window
        card.entry_orb_high = candidate.orb_high
        card.entry_orb_low = candidate.orb_low
        card.entry_trigger = (
            candidate.entry_trigger or candidate.breakout_trigger or candidate.breakout_price
        )
        card.stop_adr = candidate.stop_adr
        if candidate.risk_percent:
            card.risk_percent = candidate.risk_percent
        if candidate.shares:
            card.planned_quantity = int(candidate.shares)
            card.target_position_quantity = int(candidate.shares)

        card.entry_runtime_status = _CANDIDATE_STATUS_TO_ENTRY_RUNTIME_STATUS.get(
            candidate.status, EntryRuntimeStatus.ORB_FORMING
        )
        card.entry_block_reason = (
            candidate.reason if candidate.status in _BLOCKED_CANDIDATE_STATUSES else ""
        )
        # Deliberately does not touch card.warnings -- STOP_REQUIRED/
        # DATA_STALE and any other warning set elsewhere (position_manager,
        # trading_engine) must not be clobbered by an ORB-only refresh.
        return card
