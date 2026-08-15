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
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QThread, pyqtSignal
from sqlalchemy.engine import Engine

from src.core import execution_config
from src.core.execution_config import is_buyboard_engine_enabled
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import buyboard_runtime as buyboard_runtime_module
from src.services import trade_card_repository as repo
from src.services.eod_trading_service import (
    EodActionCallbacks,
    reconcile_unresolved_orders_at_startup,
    run_startup_reconciliation,
)
from src.services.execution_authority import ExecutionAuthority, LeaseExpiredError, LeaseHandle
from src.services.position_manager import extract_overseas_holdings
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
        account_discovery: Optional[Callable[[], List[str]]] = None,
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
        # Review finding: "accounts without existing cards remain
        # undiscoverable" -- the unscoped production worker derived query
        # targets purely from already-loaded cards, so a manually-purchased
        # position on a configured KIS account with zero TradeCards was
        # never found. Injected (not a hard import inside
        # _distinct_account_numbers) so tests stay hermetic against
        # whatever KIS accounts happen to be configured in the developer's
        # own .env -- defaults to the real KIS-config-backed discovery.
        self._account_discovery = account_discovery or self._default_account_discovery
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
        # Per real account_no (never this worker's own possibly-blank
        # self._account_no -- review finding: this worker is intentionally
        # account-unscoped and processes every PROD account's cards).
        # Tracks when each account's KIS balance/buying-power was last
        # refreshed (ACTIVE_ACCOUNT_REFRESH_SECONDS/IDLE_ACCOUNT_REFRESH_SECONDS
        # cadence -- previously unused constants; nothing populated the
        # buying-power cache on any cadence at all) and when each account's
        # positions were last fully reconciled against broker truth
        # (FULL_RECONCILIATION_SECONDS cadence -- previously only ever run
        # once at startup).
        self._account_balance_refreshed_at: Dict[str, datetime] = {}
        self._account_reconciled_at: Dict[str, datetime] = {}
        # Review: "legacy execution suppression depends only on the feature
        # flag" -- it did not verify the new engine was actually running,
        # holding its lease, and producing recent heartbeats, so a silently
        # stopped/failed worker with the flag still on left no automatic
        # engine protecting positions at all. main_window.py's
        # _buyboard_engine_healthy() reads these to require confirmed
        # health before suppressing the legacy monitor -- safe to read from
        # the UI thread (simple attribute reads/writes are GIL-atomic in
        # CPython; this is an advisory health signal, not a lock).
        self.last_heartbeat_at: Optional[datetime] = None
        # Review finding P0: last_heartbeat_at only updates once a full
        # cycle *completes* -- a cycle performs several sequential KIS
        # calls (account snapshot, quote polling, order/position
        # reconciliation) and a single slow one can legitimately take
        # longer than a tight health threshold even though the worker is
        # actively working, not stuck. Set at the *start* of every cycle
        # attempt (before any network call), so a health check can tell
        # "demonstrably still iterating, just slow this cycle" apart from
        # "the loop has not run in a long time" (crashed/hung/stopped).
        self.last_cycle_started_at: Optional[datetime] = None
        # True once _run_startup_reconciliation has completed a full pass
        # over every account it discovered, *regardless* of whether any
        # individual account failed -- distinct from
        # startup_reconciliation_complete (which additionally requires
        # zero errors *globally*). main_window._buyboard_engine_healthy
        # needs this one alone for a per-account check (review finding P0:
        # "partial account failure still creates cross-account dual
        # execution" -- a different account's outstanding failure must not
        # make a healthy account look like startup never ran at all).
        self.startup_reconciliation_ran: bool = False
        self.startup_reconciliation_complete: bool = False
        # Populated by _run_startup_reconciliation: account_no -> error
        # message for any account whose broker truth could not be fetched
        # at startup. Non-empty implies startup_reconciliation_complete is
        # False -- no automatic order execution should begin for an
        # unreconciled account.
        self.startup_reconciliation_errors: Dict[str, str] = {}
        # warning name -> card_key set already alerted via self.alert for
        # that warning -- see _emit_stalled_liquidation_alerts.
        self._alerted_card_keys_by_warning: Dict[str, set] = {}

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
            self.last_cycle_started_at = datetime.now(timezone.utc)
            try:
                self._run_one_cycle()
                self.last_heartbeat_at = datetime.now(timezone.utc)
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

    def _distinct_account_numbers(self, cards: List[TradeCardState]) -> List[str]:
        """Real account numbers this worker must query broker state for:
        every account already referenced by a loaded card, this worker's
        own configured ``self._account_no`` when it is a specific
        (non-blank) account, and -- for the unscoped production worker --
        every account configured in KIS at all, so a manually-purchased
        position on an account with zero pre-existing cards is still
        discoverable (review finding: "accounts without existing cards
        remain undiscoverable").

        Previously, startup reconciliation called
        ``broker.get_positions(account_no="")`` once (the unscoped
        production worker's own blank ``self._account_no``) and reconciled
        *every* account's cards against whatever single account that empty
        string happened to resolve to (``load_config``'s config-default
        account) -- for any card whose real ``account_no`` differed,
        ``PositionManager.reconcile_broker_positions`` would find no
        matching existing card for each of that other account's real
        holdings (its internal match requires ``card.account_no ==
        account_no``, and no real card ever has an empty ``account_no``)
        and spuriously treat every one of them as a newly-discovered manual
        position under a phantom blank-account-no card, while never
        validating the querying account's own cards against its own real
        holdings at all.
        """
        seen: List[str] = []
        if self._account_no:
            seen.append(self._account_no)
        for card in cards:
            if card.account_no and card.account_no not in seen:
                seen.append(card.account_no)
        if not self._account_no:
            # Only meaningful for the unscoped production worker -- a
            # specifically-scoped worker already covers its own account via
            # the seed above and has no business reaching into every other
            # configured account.
            for account_no in self._account_discovery():
                if account_no and account_no not in seen:
                    seen.append(account_no)
        return seen

    @staticmethod
    def _default_account_discovery() -> List[str]:
        try:
            from src.api.kis_account_snapshot_dual import discover_account_profiles

            return [
                str(profile.get("account_no") or "")
                for profile in discover_account_profiles()
                if profile.get("account_no")
            ]
        except Exception:
            logger.exception("Failed to discover configured KIS account profiles")
            return []

    @staticmethod
    def _build_order_callbacks(broker) -> EodActionCallbacks:
        """Shared by startup and periodic reconciliation -- both need the
        exact same broker-boundary order callbacks."""
        return EodActionCallbacks(
            find_open_entry_order=buyboard_runtime_module._find_open_entry_order,
            reconcile_order=lambda order: buyboard_runtime_module._reconcile_order(order, broker=broker),
            cancel_order=lambda client_order_id: buyboard_runtime_module._cancel_order(
                client_order_id, broker=broker
            ),
            discover_all_orders=lambda card: buyboard_runtime_module._discover_all_orders(card, broker=broker),
            # Review finding P0: a historical filled order discovered from
            # KIS's ~14-day order-history window must not resurrect a
            # position the account no longer actually holds -- confirm
            # against a fresh current-holdings lookup before trusting one.
            get_current_holding=lambda card: buyboard_runtime_module._refresh_broker_position(
                card, broker=broker
            ),
        )

    def _run_startup_reconciliation(self) -> None:
        """Restores retry bookkeeping (review finding P0-4's predecessor,
        section 1070-1075's "Run full startup reconciliation") and corrects
        every card's positions/orders against broker truth before the first
        heartbeat tick runs -- looping per real account_no (see
        :meth:`_distinct_account_numbers`) rather than issuing one
        broker call for the worker's own blank account scope.
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

        broker = self.runtime.broker
        order_callbacks = self._build_order_callbacks(broker)

        now = datetime.now(timezone.utc)
        changed_ids: set = set()
        changed: List[TradeCardState] = []
        expected_accounts = self._distinct_account_numbers(cards)
        self.startup_reconciliation_errors = {}
        for account_no in expected_accounts:
            try:
                position_snapshot = broker.get_positions(
                    environment=self._environment, account_no=account_no
                )
                account_changed = run_startup_reconciliation(
                    cards,
                    environment=self._environment,
                    account_no=account_no,
                    position_snapshot=position_snapshot,
                    position_manager=self.runtime.position_manager,
                    order_callbacks=order_callbacks,
                )
            except Exception as exc:
                # Review finding P0: this account did NOT get reconciled --
                # startup_reconciliation_complete below must reflect that,
                # not report blanket success while one account's broker
                # truth was never actually checked.
                logger.exception(
                    "Startup reconciliation failed for account %s", account_no
                )
                self.startup_reconciliation_errors[account_no] = str(exc)
                continue
            for card in account_changed:
                if id(card) not in changed_ids:
                    changed_ids.add(id(card))
                    changed.append(card)
            self._record_buying_power(account_no, position_snapshot)
            self._account_balance_refreshed_at[account_no] = now
            self._account_reconciled_at[account_no] = now

        self._persist_changed(changed)
        if changed:
            self.board_changed.emit()
        # startup_reconciliation_ran is unconditional (a full pass over
        # every discovered account happened, whatever its outcome);
        # startup_reconciliation_complete additionally requires zero
        # errors globally. Review finding P0: "startup reconciliation
        # reports success after account failures" -- this previously was
        # set unconditionally, so one account's get_positions failure
        # still left the health check believing every account (including
        # the failed one) had been validated against broker truth,
        # silently suppressing legacy protection for a symbol nothing had
        # actually reconciled.
        self.startup_reconciliation_ran = True
        self.startup_reconciliation_complete = not self.startup_reconciliation_errors

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

        # The periodic refresh runs over *every* account regardless of
        # readiness -- it is the recovery path for a failed one -- but
        # everything after it (ORB sync, quote subscriptions, entry/exit
        # evaluation) must not process a card whose account_no is still in
        # startup_reconciliation_errors. Review finding P0: "the worker
        # still enters its normal runtime loop after
        # _run_startup_reconciliation() regardless of whether
        # reconciliation completed successfully" -- Buy Board deciding
        # entries or exits for an account it has never actually confirmed
        # broker truth for (or whose latest refresh just failed) would be
        # acting on a local view that might not match the broker at all.
        # This is a global (per-worker), not per-card, health signal
        # already -- the legacy monitor's own health check
        # (main_window._buyboard_engine_healthy) already fails open for
        # every account's protective exits whenever this dict is
        # non-empty, so excluding these cards here does not leave them
        # unprotected.
        _track(self._refresh_account_state_if_due(cards))
        ready_cards = [
            card for card in cards if card.account_no not in self.startup_reconciliation_errors
        ]

        _track(self._sync_orb_plans(ready_cards))
        self._sync_quote_subscriptions(ready_cards)

        for quote in self.runtime.market_data.poll_once():
            _track(self.runtime.trading_engine.evaluate_quote(ready_cards, quote))

        _track(self.runtime.trading_engine.run_heartbeat(ready_cards))
        # The full card set, not just ready_cards: UNRECONCILED_BROKER_ORDER
        # can be set by _refresh_account_state_if_due's order reconciliation,
        # which (deliberately) still runs for every account, including ones
        # excluded from ready_cards.
        self._emit_stalled_liquidation_alerts(cards)

        self._persist_changed(changed)
        if changed:
            self.board_changed.emit()

    _EXIT_CANCEL_STALLED_WARNING = "EXIT_CANCEL_STALLED"
    _UNRECONCILED_BROKER_ORDER_WARNING = "UNRECONCILED_BROKER_ORDER"

    # (warning name, alert-message builder) -- every critical, card-level
    # warning that must reach the user outside the app's own log pane, not
    # only EXIT_CANCEL_STALLED. Review finding P1: "UNRECONCILED_BROKER_ORDER
    # should be a critical notification... not merely a card warning" -- a
    # real broker order discovered with nothing local tracking it (see
    # src.services.eod_trading_service) is exactly as urgent as a stalled
    # liquidation cancel.
    _CRITICAL_CARD_WARNINGS = (
        (
            _EXIT_CANCEL_STALLED_WARNING,
            lambda card: (
                f"CRITICAL: liquidation cancel unconfirmed for {card.symbol} "
                f"({card.environment}:{card.account_no}) beyond "
                f"EXIT_CANCEL_CONFIRMATION_TIMEOUT_SECONDS -- broker order "
                f"may need manual attention."
            ),
        ),
        (
            _UNRECONCILED_BROKER_ORDER_WARNING,
            lambda card: (
                f"CRITICAL: a real broker order for {card.symbol} "
                f"({card.environment}:{card.account_no}) was discovered with "
                f"nothing local tracking it -- it cannot yet be automatically "
                f"cancelled, repriced, or reconciled. Manual review required."
            ),
        ),
    )

    def _emit_stalled_liquidation_alerts(self, cards: List[TradeCardState]) -> None:
        """Review: "a card warning/log condition... is insufficient when
        the user is asleep." ``TradingEngine``/``EodTradingService`` stay
        broker/UI-agnostic by design (they already set these warnings
        purely on the card); this is the seam that actually has a
        notification channel (``self.alert``, wired by ``main_window.py``
        to both the log and a native OS notification). Fires once per
        warning per card when it first appears, and again if it reappears
        after having cleared -- never on every tick while it persists, to
        avoid alert fatigue.
        """
        for warning_name, build_message in self._CRITICAL_CARD_WARNINGS:
            alerted = self._alerted_card_keys_by_warning.setdefault(warning_name, set())
            present_now = {
                card.card_key for card in cards if warning_name in card.warnings
            }
            newly_present = present_now - alerted
            for card in cards:
                if card.card_key in newly_present:
                    self.alert.emit(build_message(card))
            self._alerted_card_keys_by_warning[warning_name] = present_now

    # -- periodic per-account KIS refresh (review: no cadence populated the --
    # -- buying-power cache or re-reconciled positions after startup) --------

    def _refresh_account_state_if_due(self, cards: List[TradeCardState]) -> List[TradeCardState]:
        """Refreshes each real account's KIS buying power
        (``ACTIVE_ACCOUNT_REFRESH_SECONDS``/``IDLE_ACCOUNT_REFRESH_SECONDS``
        -- previously-unused constants; nothing populated
        :mod:`src.services.buying_power_cache` on any cadence, so its 15s
        freshness window could only ever be re-armed by whatever the legacy
        dashboard's manual/reactive triggers happened to do, silently
        capital-blocking every automatic entry once that window lapsed) and
        fully reconciles each account's positions against broker truth
        (``FULL_RECONCILIATION_SECONDS`` -- previously only ever run once,
        at worker startup, so a manual purchase/sale, an externally
        cancelled order, or a late fill made mid-session was never
        discovered). Both need the same per-account KIS snapshot, so this
        fetches it once per account per due cycle and feeds both.
        """
        changed: List[TradeCardState] = []
        now = datetime.now(timezone.utc)
        by_account: Dict[str, List[TradeCardState]] = {}
        for card in cards:
            if card.account_no:
                by_account.setdefault(card.account_no, []).append(card)

        order_callbacks = self._build_order_callbacks(self.runtime.broker)

        # Review finding: "accounts without existing cards remain
        # undiscoverable" -- _distinct_account_numbers (not a plain
        # card-account grouping) also covers every configured-but-cardless
        # account, with an empty account_cards list; reconcile_broker_positions
        # correctly treats every holding found there as newly discovered.
        for account_no in self._distinct_account_numbers(cards):
            account_cards = by_account.get(account_no, [])
            has_active_entry_candidate = any(
                card.board_status in (BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING)
                for card in account_cards
            )
            balance_interval = (
                execution_config.ACTIVE_ACCOUNT_REFRESH_SECONDS
                if has_active_entry_candidate
                else execution_config.IDLE_ACCOUNT_REFRESH_SECONDS
            )
            balance_age = self._age_seconds(self._account_balance_refreshed_at.get(account_no), now)
            reconcile_age = self._age_seconds(self._account_reconciled_at.get(account_no), now)
            balance_due = balance_age is None or balance_age >= balance_interval
            reconcile_due = (
                reconcile_age is None
                or reconcile_age >= execution_config.FULL_RECONCILIATION_SECONDS
            )
            if not balance_due and not reconcile_due:
                continue

            try:
                position_snapshot = self.runtime.broker.get_positions(
                    environment=self._environment, account_no=account_no
                )
            except Exception:
                # Neither timestamp is updated on failure -- both stay due
                # and are retried next cycle; the buying-power cache
                # continues aging toward its own fail-closed threshold
                # rather than being refreshed with a guess.
                logger.exception(
                    "Periodic account refresh: get_positions failed for account %s", account_no
                )
                continue

            if balance_due:
                self._record_buying_power(account_no, position_snapshot)
                self._account_balance_refreshed_at[account_no] = now

            if reconcile_due:
                holdings = extract_overseas_holdings(position_snapshot)
                account_changed = self.runtime.position_manager.reconcile_broker_positions(
                    account_cards,
                    holdings,
                    environment=self._environment,
                    account_no=account_no,
                )
                changed.extend(account_changed)
                # Review finding P1: "full reconciliation still reconciles
                # positions, not the full account" -- this reruns the same
                # order-ledger discovery startup already does (an
                # ENTRY_PENDING card whose order lookup comes back empty
                # gets a full broker-order discovery query rather than
                # being assumed cancelled), on the same 60s cadence, so an
                # order-state drift that occurred mid-session is not left
                # undiscovered until the next application restart. This
                # does not yet cover reserved/cancelled/rejected order
                # history or capital-reservation repair (still open).
                order_changed = reconcile_unresolved_orders_at_startup(
                    account_cards,
                    position_manager=self.runtime.position_manager,
                    callbacks=order_callbacks,
                )
                changed.extend(order_changed)
                self._account_reconciled_at[account_no] = now
                # Review finding P0: "no periodic recovery path that
                # removes an account from startup_reconciliation_errors...
                # this can leave the application permanently reporting the
                # Buy Board as unhealthy" -- a startup failure was
                # specifically a failure to fetch/reconcile this exact
                # account's broker truth; a later periodic cycle doing
                # that same fetch+reconcile successfully is genuine
                # recovery, so clear it and let health be recomputed.
                if self.startup_reconciliation_errors.pop(account_no, None) is not None:
                    self.startup_reconciliation_complete = not self.startup_reconciliation_errors
                    logger.info(
                        "Account %s recovered from a prior startup reconciliation failure",
                        account_no,
                    )

        return changed

    @staticmethod
    def _age_seconds(then: Optional[datetime], now: datetime) -> Optional[float]:
        if then is None:
            return None
        return (now - then).total_seconds()

    def _record_buying_power(self, account_no: str, position_snapshot: dict) -> None:
        from src.services import buying_power_cache

        usable_usd, equity_usd = self._extract_account_balance(position_snapshot)
        buying_power_cache.record_snapshot(
            environment=self._environment,
            account_no=account_no,
            usable_buying_power_usd=usable_usd,
            total_equity_usd=equity_usd,
            source="buyboard_runtime_periodic_refresh",
        )

    @staticmethod
    def _extract_account_balance(snapshot: dict) -> Tuple[float, float]:
        """(usable_buying_power_usd, total_equity_usd) from a raw KIS
        account snapshot -- reuses
        ``DashboardMixin._extract_kis_account_value_krw`` directly rather
        than re-implementing this parsing (it has already been hardened
        against several KIS response quirks; see its own docstring) and
        ``FxRateWorker._extract_usd_krw_from_snapshot`` to find a KIS-
        embedded USD/KRW rate, so this periodic background refresh never
        needs a UI widget or an extra yfinance network call from this
        thread. Without an embedded rate, ``total_equity_usd`` falls back
        to the overseas-only (pure USD, no conversion needed) cash+stock
        total, which understates -- never overstates -- equity when the
        account also holds KRW-denominated assets, the safe direction to
        err for a risk-sizing base.
        """
        from src.ui.mixins.dashboard_mixin import DashboardMixin
        from src.ui.workers import FxRateWorker

        fx_rate = FxRateWorker._extract_usd_krw_from_snapshot(snapshot) or 0.0
        breakdown = DashboardMixin._extract_kis_account_value_krw(
            snapshot, fx_rate=fx_rate if fx_rate > 0 else 1.0, return_breakdown=True
        )
        if not breakdown:
            return 0.0, 0.0
        usable_usd = float(breakdown.get("ovrs_cash_usd", 0.0) or 0.0)
        if fx_rate > 0:
            equity_usd = float(breakdown.get("total_krw", 0.0) or 0.0) / fx_rate
        else:
            equity_usd = usable_usd + float(breakdown.get("ovrs_stock_usd", 0.0) or 0.0)
        return usable_usd, equity_usd

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
