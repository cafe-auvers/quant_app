"""Background thread driving the Kanban execution engine's heartbeat.
``buydashboard_to_kanban.md`` Phase 5 ("running on its own thread"); code
review finding P0-1.

:func:`src.services.buyboard_runtime.build_buyboard_runtime` assembles
every engine piece but starts nothing and runs nothing -- something has to
actually call ``trading_engine.run_heartbeat()``/``evaluate_quote()`` on a
cadence, off the UI thread (every callback the assembled engine calls
performs real KIS network I/O), load the authoritative cards each cycle,
persist the ones that changed with optimistic concurrency, keep quote
subscriptions in sync with which cards actually need a live price, and
bridge the legacy ORB/execution-queue candidate selection onto BUY_TODAY
cards (:mod:`src.services.trade_card_orb_bridge`, review finding P0-2).
This is that caller.

Mirrors the existing :class:`src.ui.workers.KisOrderWorker`/
:class:`~src.ui.workers.KisAccountWorker` ``QThread`` pattern already used
for every other background KIS call in this app.

Nothing here is started automatically. ``src/ui/main_window.py`` only
constructs and starts one when
:func:`src.core.execution_config.is_buyboard_engine_enabled` is true, on
the main device, exactly mirroring how the legacy 60-second Buy Dashboard
monitor is gated -- the difference is this worker additionally checks the
flag again on every loop iteration, so flipping it off mid-session stops
new engine activity on the next tick without requiring an app restart.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal
from sqlalchemy.engine import Engine

from src.core import execution_config
from src.core.execution_config import is_buyboard_engine_enabled
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import buyboard_runtime as buyboard_runtime_module
from src.services import trade_card_repository as repo
from src.services.eod_trading_service import EodActionCallbacks, run_startup_reconciliation
from src.services.execution_authority import ExecutionAuthority, LeaseExpiredError, LeaseHandle
from src.services.trade_card_orb_bridge import TradeCardOrbEvaluator

logger = logging.getLogger(__name__)

# Board columns whose cards need a live quote to do anything useful this
# tick (entries pricing off it, positions/exits evaluating a stop).
# WATCHLIST/BUYLIST (no live plan yet) and CLOSED (done) do not.
_QUOTE_SUBSCRIBED_STATUSES = {
    BoardStatus.BUY_TODAY,
    BoardStatus.ENTRY_PENDING,
    BoardStatus.OPEN_POSITION,
    BoardStatus.PARTIAL_SELL,
    BoardStatus.SELL_ALL,
}

# Cards whose ORB plan the bridge is allowed to touch -- mirrors
# src.services.trade_card_orb_bridge's own pre-entry guard; checked again
# here so this module doesn't even bother looking up an execution-queue
# item for a card the bridge would ignore anyway.
_ORB_SYNCED_STATUSES = {BoardStatus.WATCHLIST, BoardStatus.BUYLIST, BoardStatus.BUY_TODAY}


class BuyboardRuntimeWorker(QThread):
    """Owns the assembled :class:`~src.services.buyboard_runtime.BuyboardRuntime`
    for its entire lifetime. Built lazily inside :meth:`run` (on the worker
    thread itself -- ``KisBroker()`` and friends should never be
    constructed on the UI thread), never auto-started by importing this
    module. The caller (``MainWindow``) decides when to construct and
    ``.start()`` one.

    ``self.runtime`` is set once :meth:`run` has built it and is read by
    the board UI (:func:`src.ui.buyboard.board._quote_lookup_for`) for live
    P&L -- safe to read from the UI thread since it is assigned exactly
    once and never mutated afterward.
    """

    board_changed = pyqtSignal()
    alert = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        *,
        db_engine: Engine,
        environment: str,
        account_no: str,
        buying_power_provider: Callable[[str, str], float],
        account_equity_provider: Optional[Callable[[str, str], float]] = None,
        broker=None,
        execution_authority: Optional[ExecutionAuthority] = None,
        execution_lease: Optional[LeaseHandle] = None,
        lease_engine: Optional[Engine] = None,
        capital_reservation_engine: Optional[Engine] = None,
        execution_queue_item_lookup: Optional[Callable[[str, str], object]] = None,
        heartbeat_seconds: Optional[float] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db_engine = db_engine
        self._environment = environment
        self._account_no = account_no
        self._buying_power_provider = buying_power_provider
        self._account_equity_provider = account_equity_provider
        self._broker = broker
        self._execution_authority = execution_authority
        self._execution_lease = execution_lease
        self._lease_engine = lease_engine
        self._capital_reservation_engine = capital_reservation_engine
        # How this worker finds the legacy execution queue's already-computed
        # ORB candidate for a symbol (review finding P0-2) -- typically
        # ``lambda symbol, env: main_window.execution_queue_manager.get_item(symbol, env)``.
        # Optional: without it, cards are still visible/moveable but never
        # progress past ORB_FORMING toward EXECUTE_READY.
        self._execution_queue_item_lookup = execution_queue_item_lookup
        self._heartbeat_seconds = (
            heartbeat_seconds if heartbeat_seconds is not None else execution_config.ENGINE_HEARTBEAT_SECONDS
        )
        self._orb_evaluator = TradeCardOrbEvaluator()
        self._stop_requested = False
        self.runtime: Optional[buyboard_runtime_module.BuyboardRuntime] = None

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self) -> None:
        """Thread-safe: ask the loop to exit on its next wait boundary.
        Does not join -- callers that need to block until the thread has
        actually exited should follow this with ``QThread.wait()``.
        """
        self._stop_requested = True

    def run(self) -> None:  # noqa: D401 - Qt override
        if not is_buyboard_engine_enabled():
            return
        try:
            self.runtime = buyboard_runtime_module.build_buyboard_runtime(
                buying_power_provider=self._buying_power_provider,
                card_lookup=self._card_lookup,
                account_equity_provider=self._account_equity_provider,
                capital_reservation_engine=self._capital_reservation_engine,
                execution_authority=self._execution_authority,
                execution_lease=self._execution_lease,
                lease_engine=self._lease_engine,
                broker=self._broker,
            )
            self._run_startup_reconciliation()
        except Exception as exc:  # noqa: BLE001 - must not crash the app
            logger.exception("BuyboardRuntimeWorker failed to start")
            self.error_occurred.emit(f"Buy Board engine failed to start: {exc}")
            return

        while not self._stop_requested:
            if not is_buyboard_engine_enabled():
                logger.info("BuyboardRuntimeWorker stopping: engine flag turned off")
                break
            if not self._lease_still_current():
                logger.info("BuyboardRuntimeWorker stopping: main-device lease no longer current")
                break
            try:
                self._run_one_cycle()
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("BuyboardRuntimeWorker heartbeat cycle failed")
                self.error_occurred.emit("Buy Board engine heartbeat failed -- see logs for detail.")
            self.msleep(max(1, int(self._heartbeat_seconds * 1000)))

    def _lease_still_current(self) -> bool:
        """Review finding P0-1: "Stop the worker immediately on lease
        loss." Every order submission already re-checks the lease at the
        broker boundary (``submit_guarded_overseas_order``); this
        additionally stops the *loop itself* so a demoted device does not
        keep polling quotes/hammering the DB on behalf of a board it no
        longer has authority over.
        """
        if self._execution_authority is None:
            return True
        try:
            self._execution_authority.require_current_lease(self._lease_engine, self._execution_lease)
            return True
        except LeaseExpiredError:
            return False

    def _card_lookup(self, environment: str, account_no: str, symbol: str) -> Optional[TradeCardState]:
        return repo.get_trade_card(self._db_engine, environment, account_no, symbol)

    # -- startup reconciliation ----------------------------------------------

    def _run_startup_reconciliation(self) -> None:
        """Restores retry bookkeeping (review finding P0-4's predecessor,
        section 1070-1075's "Run full startup reconciliation") and corrects
        every card's positions/orders against broker truth before the first
        heartbeat tick runs.
        """
        assert self.runtime is not None
        cards = repo.list_trade_cards(self._db_engine, environment=self._environment)
        for card in cards:
            self.runtime.entry_attempt_manager.restore_symbol_state(
                card.environment,
                card.account_no,
                card.symbol,
                cooldown_until=card.next_retry_at,
                attempt_group_id=card.entry_attempt_group_id,
                attempt_count=card.entry_attempt_count,
            )

        position_snapshot = self.runtime.broker.get_positions(
            environment=self._environment, account_no=self._account_no
        )
        broker = self.runtime.broker
        order_callbacks = EodActionCallbacks(
            find_open_entry_order=buyboard_runtime_module._find_open_entry_order,
            reconcile_order=lambda order: buyboard_runtime_module._reconcile_order(order, broker=broker),
            cancel_order=lambda client_order_id: buyboard_runtime_module._cancel_order(
                client_order_id, broker=broker
            ),
            discover_all_orders=lambda card: buyboard_runtime_module._discover_all_orders(card, broker=broker),
        )
        changed = run_startup_reconciliation(
            cards,
            environment=self._environment,
            account_no=self._account_no,
            position_snapshot=position_snapshot,
            position_manager=self.runtime.position_manager,
            order_callbacks=order_callbacks,
        )
        self._persist_changed(changed)
        if changed:
            self.board_changed.emit()

    # -- per-cycle heartbeat --------------------------------------------------

    def _run_one_cycle(self) -> None:
        assert self.runtime is not None
        cards = repo.list_trade_cards(self._db_engine, environment=self._environment)
        if self._account_no:
            cards = [card for card in cards if card.account_no == self._account_no]

        changed_ids: set = set()
        changed: List[TradeCardState] = []

        def _track(touched: List[TradeCardState]) -> None:
            for card in touched:
                if id(card) not in changed_ids:
                    changed_ids.add(id(card))
                    changed.append(card)

        _track(self._sync_orb_plans(cards))
        self._sync_quote_subscriptions(cards)

        for quote in self.runtime.market_data.poll_once():
            _track(self.runtime.trading_engine.evaluate_quote(cards, quote))

        _track(self.runtime.trading_engine.run_heartbeat(cards))

        self._persist_changed(changed)
        if changed:
            self.board_changed.emit()

    def _sync_orb_plans(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Review finding P0-2: without this, a card dragged to Buy Today
        never progresses past ORB_FORMING -- nothing else recomputes
        entry_trigger/planned_quantity/etc. Reads whatever the legacy
        execution queue's own (unchanged, still independently running)
        refresh cycle most recently computed rather than triggering a
        second, competing ORB recalculation.
        """
        if self._execution_queue_item_lookup is None:
            return []
        changed: List[TradeCardState] = []
        for card in cards:
            if card.board_status not in _ORB_SYNCED_STATUSES:
                continue
            try:
                item = self._execution_queue_item_lookup(card.symbol, card.environment)
            except Exception:
                logger.exception("execution_queue_item_lookup failed for %s", card.symbol)
                continue
            if item is None:
                continue
            before = (
                card.entry_runtime_status,
                card.entry_trigger,
                card.planned_quantity,
                card.entry_block_reason,
                card.selected_orb_window,
            )
            self._orb_evaluator.update_card(card, item)
            after = (
                card.entry_runtime_status,
                card.entry_trigger,
                card.planned_quantity,
                card.entry_block_reason,
                card.selected_orb_window,
            )
            if before != after:
                changed.append(card)
        return changed

    def _sync_quote_subscriptions(self, cards: List[TradeCardState]) -> None:
        market_data = self.runtime.market_data
        subscribed = getattr(market_data, "subscribed_symbols", None)
        if not callable(subscribed):
            return  # backend does not expose its current subscription set
        desired = {card.symbol for card in cards if card.board_status in _QUOTE_SUBSCRIBED_STATUSES}
        current = set(subscribed())
        to_add = desired - current
        to_remove = current - desired
        if to_add:
            market_data.subscribe(to_add)
        if to_remove:
            market_data.unsubscribe(to_remove)

    def _persist_changed(self, cards: List[TradeCardState]) -> None:
        for card in cards:
            try:
                try:
                    repo.update_trade_card(self._db_engine, card, expected_version=card.version)
                except repo.TradeCardNotFoundError:
                    # A newly *discovered* card -- e.g. a manually-purchased
                    # broker position with no prior local card
                    # (PositionManager.discover_manual_position, surfaced
                    # via startup/full-account reconciliation) -- has never
                    # been persisted. Create it instead of dropping it.
                    repo.create_trade_card(self._db_engine, card)
            except Exception:
                # A stale version (another device changed this card
                # concurrently) or a transient DB error must not stop the
                # rest of this cycle's cards from being persisted -- the
                # next cycle re-loads authoritative state and simply tries
                # again.
                logger.exception("BuyboardRuntimeWorker failed to persist %s", card.symbol)
