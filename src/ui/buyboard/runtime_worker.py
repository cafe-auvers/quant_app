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
import platform
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QThread, pyqtSignal
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from src.api.kis_account_snapshot_dual import (
    KisRateLimitError,
    KisTransientApiError,
)
from src.core import execution_config
from src.core.account_broker_snapshot import AccountBrokerSnapshot, ReconciliationAction
from src.core.board_workflow import BoardActionContext
from src.core.execution_config import is_buyboard_engine_enabled
from src.core.execution_mode import ExecutionLease, ExecutionSource
from src.core.runtime_readiness import EngineReadiness, RuntimeDeviceState
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_state import OrderSide
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import buyboard_runtime as buyboard_runtime_module
from src.services import capital_reservation_repository
from src.services import discovered_external_order_repository
from src.services import execution_order_repository
from src.services import trade_card_repository as repo
from src.services.account_reconciliation import (
    AccountReconciliationResult,
    ReconciliationAlertSeverity,
    ReconciliationCommandType,
    run_account_reconciliation_pass,
)
from src.services.execution_command_gateway import (
    AmbiguousPostBrokerPersistenceError,
    ExecutionCommandGateway,
    ExecutionOwnershipMismatchError,
    GuardedCancellationRejectedError,
    GuardedSubmissionAmbiguousError,
    GuardedSubmissionRejectedError,
    build_guarded_execution_gateway,
)
from src.services.execution_command_repository import DuplicateCommandError
from src.services.execution_order_repository import fetch_execution_order
from src.services.execution_authority import ExecutionAuthority, LeaseExpiredError, LeaseHandle
from src.services.execution_lease_protocol import DefaultExecutionLeaseProtocol
from src.services.kis_request_scheduler import KisRequestScheduler
from src.services.kis_request_boundary import install_process_kis_request_scheduler
from src.services.controlled_live_policy import (
    live_entry_symbol_allowed,
    require_controlled_live_configuration,
)
from src.utils.market_calendar import is_regular_session_open
from src.services.external_alerting import (
    CriticalAlertType,
    ExternalAlertingService,
)
from src.services.schema_migration import (
    MigrationPhase,
    SchemaMigrationManager,
)
from src.utils.redaction import scrub_sensitive_text
from src.utils.device_identity import detect_local_device_kind
from src.services.runtime_device_state_repository import (
    publish_runtime_device_state_transition,
    refresh_runtime_device_state,
    require_compatible_runtime_schema,
    save_runtime_device_state,
)
from src.services.state_sync import (
    LocalDeviceRole,
    get_main_device,
    get_synced_state_revisions,
)
from src.services.kis_realtime_market_data import StopRule, SubscriptionPriority
from src.services.stop_change_coordinator import stop_change_coordinator_for
from src.services.trade_card_orb_bridge import (
    TradeCardOrbEvaluator,
    queue_has_execution_order_lock,
)

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

# ORB is an active-session entry concern.  Watchlist and Buylist remain
# planning-only and retain only their configured breakout target.
_ORB_SYNCED_STATUSES = {BoardStatus.BUY_TODAY}


def _ambiguous_buy_today_orb_keys(
    cards: List[TradeCardState],
) -> set[tuple[str, str]]:
    """Find symbols whose legacy ORB queue row cannot identify an account."""

    accounts_by_symbol: Dict[tuple[str, str], set[str]] = {}
    for card in cards:
        if card.board_status != BoardStatus.BUY_TODAY:
            continue
        key = (
            str(card.environment or "").strip().upper(),
            str(card.symbol or "").strip().upper(),
        )
        accounts_by_symbol.setdefault(key, set()).add(
            str(card.account_no or "").strip()
        )
    return {
        key for key, accounts in accounts_by_symbol.items() if len(accounts) > 1
    }


def _buy_today_subscription_priority(card: TradeCardState) -> SubscriptionPriority:
    if card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY:
        return SubscriptionPriority.ENTRY_READY
    if card.entry_runtime_status in {
        EntryRuntimeStatus.WAITING_BREAKOUT,
        EntryRuntimeStatus.ARMED,
    }:
        return SubscriptionPriority.ENTRY_ARMED
    return SubscriptionPriority.BUY_TODAY


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

    _QUEUE_DRAIN_FAILURE_CONFIRMATIONS = 3
    _ACCOUNT_RECONCILIATION_FAILURE_CONFIRMATIONS = 3
    _ACCOUNT_REFRESH_FAILURE_COOLDOWN_SECONDS = 30.0

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
        request_scheduler=None,
        market_data=None,
        execution_authority: Optional[ExecutionAuthority] = None,
        execution_lease: Optional[LeaseHandle] = None,
        lease_engine: Optional[Engine] = None,
        capital_reservation_engine: Optional[Engine] = None,
        execution_queue_item_lookup: Optional[Callable[[str, str], object]] = None,
        heartbeat_seconds: Optional[float] = None,
        account_discovery: Optional[Callable[[], List[str]]] = None,
        strategy_instance_id: str = "",
        journal_flush: Optional[Callable[[], None]] = None,
        standby_only: bool = False,
        cross_device_operator_sync: bool = True,
        device_id: str = "",
        hostname: str = "",
        external_alerting: Optional[ExternalAlertingService] = None,
        schema_migration_manager: Optional[SchemaMigrationManager] = None,
        regular_session_open: Optional[Callable[[], bool]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db_engine = db_engine
        self._environment = environment
        self._account_no = account_no
        self._buying_power_provider = buying_power_provider
        self._account_equity_provider = account_equity_provider
        self._broker = broker
        self.request_scheduler = request_scheduler or KisRequestScheduler(
            max_confirmed_mutation_attempts=(
                execution_config.KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS
            ),
            min_request_spacing_seconds=(
                execution_config.KIS_REQUEST_MIN_SPACING_SECONDS
            ),
            min_mutation_spacing_seconds=(
                execution_config.KIS_MUTATION_MIN_SPACING_SECONDS
            ),
        )
        install_process_kis_request_scheduler(self.request_scheduler)
        self._market_data = market_data
        self._stop_change_coordinator = stop_change_coordinator_for(db_engine)
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
        self._strategy_instance_id = str(strategy_instance_id or "")
        self._journal_flush = journal_flush or self._flush_execution_journal
        self._standby_only = bool(standby_only)
        self._cross_device_operator_sync = bool(cross_device_operator_sync)
        self._device_id = str(
            device_id or getattr(execution_lease, "device_id", "") or ""
        )
        self._hostname = str(hostname or platform.node())
        self._device_kind = detect_local_device_kind(self._hostname)
        self._external_alerting = external_alerting
        self._schema_migration_manager = schema_migration_manager
        self._regular_session_open = regular_session_open or is_regular_session_open
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
        # Directly-driven workers in unit/diagnostic contexts retain the
        # historical default. ``run`` closes this gate before startup and
        # only reopens it at ACTIVE.
        self._accepting_commands = True
        self.shutdown_prepared = False
        self.shutdown_errors: List[str] = []
        self.runtime: Optional[buyboard_runtime_module.BuyboardRuntime] = None
        self.device_state = RuntimeDeviceState.STARTING
        self.readiness_generation = 0
        self._lease_current = False
        self._database_writable = False
        self._database_probe_completed = False
        self._market_data_queue_confirmed_healthy = False
        self._market_data_queue_failure_streak = 0
        self._market_data_queue_last_failure_cycle_started_at: Optional[
            datetime
        ] = None
        self.execution_gateway: Optional[ExecutionCommandGateway] = None
        self._cached_cards: List[TradeCardState] = []
        self._card_cache_initialized = False
        self._card_collection_revision = None
        self._last_card_revision_checked_at: Optional[datetime] = None
        self._local_card_change_generation = (
            repo.local_trade_card_change_generation(self._db_engine)
        )
        from src.services.coordination_change_pulse import (
            coordination_table_change_generation,
        )

        pulse_generation = coordination_table_change_generation(
            self._db_engine, {"trade_cards"}
        )
        self._last_card_change_pulse_generation = pulse_generation
        self._last_lease_change_pulse_generation = -1
        self._last_operator_change_pulse_generation = -1
        self._last_state_revision_change_pulse_generation = None
        self._cached_synced_state_revisions: Optional[Dict[str, int]] = None
        self._last_database_probe_at: Optional[datetime] = None
        self._last_lease_checked_at: Optional[datetime] = None
        self._last_device_state_published_at: Optional[datetime] = None
        self._last_device_state_details: Optional[Dict[str, object]] = None
        self._last_ownership_proof_at: Optional[datetime] = None
        self._last_alert_poll_at: Optional[datetime] = None
        self._last_operator_command_poll_at: Optional[datetime] = None
        self.last_market_data_drain_at: Optional[datetime] = None
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
        self._account_refresh_retry_not_before: Dict[str, datetime] = {}
        # Operator readiness is intentionally steadier than the per-command
        # safety gate.  A routine broker pass can outlast its 60-second
        # freshness window, but merely being in progress is not a failure.
        # Preserve proof that an account has reconciled successfully before,
        # and only surface a board-wide blocker after three genuinely failed
        # passes in a row.  Action readiness continues to use the strict
        # timestamps/snapshots below and is never relaxed by this debounce.
        self._reconciliation_required_accounts: set[str] = set()
        self._account_reconciliation_confirmed_accounts: set[str] = set()
        self._account_reconciliation_failure_streaks: Dict[str, int] = {}
        self.routine_reconciliation_accounts_in_progress: set[str] = set()
        self._latest_reconciliation_snapshots: Dict[str, AccountBrokerSnapshot] = {}
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
        # Read-only UI fence: a gesture rendered before/during an account
        # reconciliation must not enqueue an intent against moving truth.
        self.reconciliation_accounts_in_progress: set[str] = set()
        # Positive confirmation set: account numbers that have actually
        # completed a successful reconciliation at least once (startup or
        # periodic). Review finding P0: "unknown accounts can be
        # incorrectly considered healthy" -- checking only "not in
        # startup_reconciliation_errors" treats an account that was never
        # discovered/processed at all (a stale or unconfigured
        # kis_account_no, or one genuinely not reached yet) the same as a
        # cleanly-reconciled one. Health must require *positive*
        # membership here, not merely absence from the error set.
        self.startup_reconciled_accounts: set = set()
        # warning name -> card_key set already alerted via self.alert for
        # that warning -- see _emit_stalled_liquidation_alerts.
        self._alerted_card_keys_by_warning: Dict[str, set] = {}
        # warning name -> card keys present on the previous observation.
        # Kept separately so a fresh worker can resolve durable incidents
        # that recovered before this process started.
        self._card_alert_presence_by_warning: Dict[str, set] = {}
        self._active_reconciliation_incidents: set = set()
        self._reconciliation_incident_generations: Dict[tuple, int] = {}
        # Workstream 5: runtime-only stop generations. A generation advances
        # only when that card's stop definition changes, not on unrelated
        # optimistic-version writes.
        self._market_stop_signatures: Dict[str, tuple] = {}
        self._market_stop_generations: Dict[str, int] = {}
        self._market_stop_symbols: set[str] = set()
        self._recovery_reconciliation_required = False

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self) -> None:
        """Thread-safe: ask the loop to exit on its next wait boundary.
        Does not join -- callers that need to block until the thread has
        actually exited should follow this with ``QThread.wait()``.
        """
        # First close the mutation gate.  The loop may still be between
        # cycles, but no later cycle can derive/submit another command.
        self._accepting_commands = False
        self._stop_requested = True

    def _set_device_state(
        self,
        state: RuntimeDeviceState,
        *,
        handoff_confirmed: bool = False,
    ):
        if not self._device_id:
            self.device_state = state
            return
        details = self._runtime_readiness_details(state)
        if state != RuntimeDeviceState.STANDBY_READY and not handoff_confirmed:
            publish_runtime_device_state_transition(
                self._db_engine,
                device_id=self._device_id,
                hostname=self._hostname,
                state=state,
                details=details,
            )
            self.device_state = state
            self._last_device_state_published_at = datetime.now(timezone.utc)
            self._last_device_state_details = details
            return None
        record = save_runtime_device_state(
            self._db_engine,
            device_id=self._device_id,
            hostname=self._hostname,
            state=state,
            handoff_confirmed=handoff_confirmed,
            details=details,
        )
        # Durable state is the authorization source. Local state changes only
        # after that write succeeds, especially for ACTIVE.
        self.device_state = state
        self.readiness_generation = int(record.readiness_generation or 0)
        self._last_device_state_published_at = datetime.now(timezone.utc)
        self._last_device_state_details = details
        return record

    def _publish_device_state_if_due(self, state: RuntimeDeviceState) -> None:
        """Refresh durable readiness at a safe cadence, not every local tick."""

        now = datetime.now(timezone.utc)
        last = self._last_device_state_published_at
        if state == self.device_state and last is not None and (
            now - last
        ).total_seconds() < execution_config.COORDINATION_DEVICE_HEARTBEAT_SECONDS:
            return
        details = self._runtime_readiness_details(state)
        heartbeat_only = bool(
            state == self.device_state
            and self._last_device_state_details is not None
            and details == self._last_device_state_details
        )
        if state == self.device_state and refresh_runtime_device_state(
            self._db_engine,
            device_id=self._device_id,
            hostname=self._hostname,
            state=state,
            details=details,
            heartbeat_only=heartbeat_only,
        ):
            self._last_device_state_published_at = now
            self._last_device_state_details = details
            return
        self._set_device_state(state)

    def _demote_standby_readiness(self) -> None:
        """Close the local claim path immediately, then clear durable readiness."""

        self._accepting_commands = False
        self.device_state = RuntimeDeviceState.STANDBY
        if not self._device_id:
            return
        publish_runtime_device_state_transition(
            self._db_engine,
            device_id=self._device_id,
            hostname=self._hostname,
            state=RuntimeDeviceState.STANDBY,
            details=self._runtime_readiness_details(RuntimeDeviceState.STANDBY),
        )

    def _runtime_readiness_details(
        self, state: RuntimeDeviceState
    ) -> Dict[str, object]:
        """Publish the execution-switch facts checked by the other machine."""

        readiness = self.engine_readiness(include_device_state=False)
        revisions = (
            {}
            if self._database_probe_completed and not self._database_writable
            else self._readiness_state_revisions()
        )
        stable_market_data = all(
            (
                readiness.websocket_connected,
                readiness.critical_trade_subscriptions_acked,
                readiness.critical_quote_subscriptions_acked,
                readiness.accumulator_draining_within_budget,
            )
        )
        reconciliation_ready = bool(
            readiness.startup_reconciliation_complete
            and readiness.account_reconciliation_fresh
        )
        command_consumer_ready = bool(
            self.runtime is not None
            and state
            not in {
                RuntimeDeviceState.SHUTTING_DOWN,
                RuntimeDeviceState.STOPPED,
                RuntimeDeviceState.FAILED,
            }
        )
        state_revisions_current = bool(revisions) and all(
            key in revisions
            for key in ("watchlist", "buylist", "trade_plans", "execution_queue")
        )
        executor_ready = bool(
            state in {RuntimeDeviceState.STANDBY_READY, RuntimeDeviceState.ACTIVE}
            and readiness.standby_ready
            and stable_market_data
            and reconciliation_ready
            and command_consumer_ready
            and state_revisions_current
        )
        return {
            "device_id": self._device_id,
            "hostname": self._hostname,
            "device_kind": self._device_kind,
            "main_py_alive": state
            not in {RuntimeDeviceState.STOPPED, RuntimeDeviceState.FAILED},
            "db_connected": bool(self._database_writable),
            "kis_ready": reconciliation_ready,
            "broker_account": str(self._account_no or "all configured accounts"),
            "environment": self._environment,
            "coordination_ru_profile": execution_config.COORDINATION_RU_PROFILE,
            "account_environment_ready": bool(
                self._environment in {"PROD", "PAPER"} and reconciliation_ready
            ),
            "market_data_ready": stable_market_data,
            "command_consumer_ready": command_consumer_ready,
            "order_reconciliation_ready": reconciliation_ready,
            "latest_watchlist_revision": int(revisions.get("watchlist", 0) or 0),
            "latest_buylist_revision": int(revisions.get("buylist", 0) or 0),
            "latest_trade_plans_revision": int(
                revisions.get("trade_plans", 0) or 0
            ),
            "latest_execution_queue_revision": int(
                revisions.get("execution_queue", 0) or 0
            ),
            "state_revisions_current": state_revisions_current,
            "no_stale_local_state": state_revisions_current,
            "power_state": (
                "SHUTTING_DOWN"
                if state == RuntimeDeviceState.SHUTTING_DOWN
                else "AWAKE"
            ),
            "sleep_blocker_active": state
            not in {
                RuntimeDeviceState.SHUTTING_DOWN,
                RuntimeDeviceState.STOPPED,
                RuntimeDeviceState.FAILED,
            },
            "executor_ready": executor_ready,
            "executor_not_ready_reason": (
                ""
                if executor_ready
                else ", ".join(readiness.standby_blockers)
                or f"runtime state is {state.value}"
            ),
        }

    def _readiness_state_revisions(self) -> Dict[str, int]:
        """Reuse canonical plan revisions until ``app_state_sync`` changes."""

        from src.services.coordination_change_pulse import (
            coordination_table_change_generation,
        )

        pulse_generation = coordination_table_change_generation(
            self._db_engine, {"app_state_sync"}
        )
        if (
            self._cached_synced_state_revisions is not None
            and pulse_generation
            == self._last_state_revision_change_pulse_generation
        ):
            return dict(self._cached_synced_state_revisions)
        revisions = get_synced_state_revisions(self._db_engine)
        # A failed read returns an empty mapping. Do not cache that failure;
        # the next readiness attempt must be allowed to prove recovery.
        if revisions:
            self._cached_synced_state_revisions = dict(revisions)
            self._last_state_revision_change_pulse_generation = pulse_generation
        return revisions

    def _probe_database_writable(self, *, force: bool = False) -> bool:
        """Exercise a write statement without changing application data."""

        now = datetime.now(timezone.utc)
        if (
            not force
            and self._last_database_probe_at is not None
            and (now - self._last_database_probe_at).total_seconds()
            < execution_config.COORDINATION_DATABASE_PROBE_SECONDS
        ):
            return self._database_writable
        self._last_database_probe_at = now

        # A successful runtime-state heartbeat is a stronger real write-path
        # proof than ``UPDATE ... WHERE 1 = 0``.  Reuse that recent evidence
        # during normal ACTIVE/STANDBY_READY operation; the no-op transaction
        # remains for startup, recovery, and any worker that has not published
        # readiness recently.  Every actual order mutation still performs its
        # own authoritative transaction and lease checks.
        last_state_write = self._last_device_state_published_at
        if (
            not force
            and self._database_writable
            and last_state_write is not None
            and 0.0 <= (now - last_state_write).total_seconds()
            < execution_config.COORDINATION_DATABASE_PROBE_SECONDS
        ):
            self._database_probe_completed = True
            return True

        had_prior_probe = self._database_probe_completed
        was_writable = self._database_writable
        self._database_probe_completed = True
        try:
            with self._db_engine.begin() as conn:
                # This is a real write-path prepare/execute and therefore
                # catches read-only connections; the false predicate keeps
                # all rows untouched.
                conn.execute(text("UPDATE trade_cards SET version = version WHERE 1 = 0"))
            self._database_writable = True
            if self.execution_gateway is not None:
                self.execution_gateway.reconcile_emergency_journal()
            if had_prior_probe and not was_writable:
                # Journal folding restores command/order/card correlation,
                # but broker truth may have advanced during the outage. No
                # normal heartbeat is permitted until a complete fresh pass.
                self._recovery_reconciliation_required = True
        except Exception as exc:
            outage_started = not had_prior_probe or was_writable
            if outage_started:
                logger.warning(
                    "Buyboard runtime database is unavailable: %s",
                    scrub_sensitive_text(exc, account_no=self._account_no),
                )
            else:
                logger.debug(
                    "Buyboard runtime database remains unavailable: %s",
                    scrub_sensitive_text(exc, account_no=self._account_no),
                )
            self._database_writable = False
            if self.execution_gateway is not None:
                self.execution_gateway.note_canonical_database_unavailable()
            if outage_started:
                self._raise_external_alert(
                    CriticalAlertType.DATABASE_UNAVAILABLE,
                    f"{self._environment}:{self._device_id}",
                    "The canonical trading database is unavailable; new "
                    "entries are closed and only bounded protective actions "
                    "remain eligible.",
                )
        return self._database_writable

    def _flush_execution_journal(self) -> None:
        """Cross a final database transaction boundary.

        Commands are written synchronously before broker calls, so there is
        no in-memory queue to drain.  Completing this transaction proves all
        earlier journal commits are visible before final reconciliation.
        """

        with self._db_engine.begin() as conn:
            conn.execute(text("SELECT 1"))

    def _execution_lease_value(self) -> Optional[ExecutionLease]:
        lease = self._execution_lease
        if lease is None:
            return None
        return ExecutionLease(
            device_id=lease.device_id,
            lease_token=lease.lease_token,
            lease_epoch=int(getattr(lease, "lease_epoch", 0) or 0),
        )

    def _require_standby_migration_ready(
        self, migration_manager: SchemaMigrationManager
    ) -> None:
        """Fence incomplete cutovers without deadlocking first ownership.

        A brand-new coordination store has neither a migration nor an
        Execution Owner.  Its read-only runtime must be allowed to reconcile
        broker truth and publish ``STANDBY_READY``; otherwise the ownership
        switch cannot acquire the first readiness-fenced lease, and the
        lease-owned migration can never start.  This exception authorizes no
        mutation: the worker remains observation-only.  Any existing owner or
        any migration phase beyond ``NOT_STARTED`` keeps the normal entry
        readiness fence in place.
        """

        state = migration_manager.state
        if state.phase == MigrationPhase.NOT_STARTED:
            ownership = get_main_device(self._lease_engine or self._db_engine)
            if ownership.success and ownership.main_device is None:
                logger.info(
                    "Unclaimed coordination store is awaiting its first "
                    "Execution Owner; standby reconciliation remains read-only"
                )
                return
        migration_manager.require_entries_ready()

    def run(self) -> None:  # noqa: D401 - Qt override
        if not is_buyboard_engine_enabled():
            return
        try:
            self._accepting_commands = False
            require_compatible_runtime_schema(
                self._db_engine,
                device_id=self._device_id,
                lease_engine=self._lease_engine or self._db_engine,
                required_coordination_profile=(
                    execution_config.COORDINATION_RU_PROFILE
                ),
            )
            migration_manager = (
                self._schema_migration_manager
                or SchemaMigrationManager(
                    self._db_engine,
                    lease_protocol=DefaultExecutionLeaseProtocol(
                        engine=self._lease_engine or self._db_engine
                    ),
                )
            )
            self._schema_migration_manager = migration_manager
            if self._standby_only:
                self._require_standby_migration_ready(migration_manager)
            else:
                lease = self._execution_lease_value()
                migration_manager.prepare_cutover(
                    device_id=getattr(lease, "device_id", ""),
                    lease_token=getattr(lease, "lease_token", ""),
                    lease_epoch=int(getattr(lease, "lease_epoch", 0) or 0),
                )
            self._set_device_state(RuntimeDeviceState.STARTING)
            runtime_broker = self._broker
            if (
                not self._standby_only
                and runtime_broker is None
                and is_buyboard_engine_enabled()
            ):
                # The real production composition cannot start with a
                # half-configured pilot. Injected test brokers remain under
                # their explicit test policies.
                require_controlled_live_configuration(
                    environment=self._environment,
                    scheduler=self.request_scheduler,
                )
            if not self._standby_only and not isinstance(
                runtime_broker, ExecutionCommandGateway
            ):
                runtime_broker = build_guarded_execution_gateway(
                    engine=self._db_engine,
                    lease_protocol=DefaultExecutionLeaseProtocol(
                        engine=self._lease_engine or self._db_engine
                    ),
                    request_scheduler=self.request_scheduler,
                    buying_power_provider=self._buying_power_provider,
                    real_broker=runtime_broker,
                    database_writable_provider=lambda: self._database_writable,
                    handoff_pending_provider=lambda: bool(
                        self.shutdown_prepared or self._stop_requested
                    ),
                    critical_alert_sink=(
                        self._external_alerting.sink
                        if self._external_alerting is not None
                        else None
                    ),
                    schema_migration_manager=migration_manager,
                )
            if isinstance(runtime_broker, ExecutionCommandGateway):
                self.execution_gateway = runtime_broker
            self.runtime = buyboard_runtime_module.build_buyboard_runtime(
                buying_power_provider=self._buying_power_provider,
                card_lookup=self._card_lookup,
                account_equity_provider=self._account_equity_provider,
                portfolio_cards_provider=lambda environment, account_no: repo.list_trade_cards(
                    self._db_engine,
                    environment=environment,
                    account_no=account_no,
                    raise_on_error=True,
                ),
                portfolio_orders_provider=lambda environment, account_no: execution_order_repository.list_execution_orders_for_account(
                    self._db_engine,
                    environment=environment,
                    account_no=account_no,
                ),
                portfolio_reservations_provider=lambda environment, account_no: capital_reservation_repository.list_active_reservations(
                    self._capital_reservation_engine,
                    environment=environment,
                    account_no=account_no,
                ),
                portfolio_external_orders_provider=lambda environment, account_no: discovered_external_order_repository.list_discovered_external_orders_for_account(
                    self._db_engine,
                    environment=environment,
                    account_no=account_no,
                ),
                capital_reservation_engine=self._capital_reservation_engine,
                execution_authority=self._execution_authority,
                execution_lease=self._execution_lease_value(),
                lease_engine=self._lease_engine,
                broker=runtime_broker,
                market_data=self._market_data,
                strategy_instance_id=self._strategy_instance_id,
                persist_card_before_execution=self._persist_execution_identity,
                observation_only=self._standby_only,
            )
            if execution_config.KIS_WS_ENABLED:
                start_market_data = getattr(self.runtime.market_data, "start", None)
                if callable(start_market_data):
                    start_market_data()
            # Establish the exact database/lease proof before caching cards
            # for outage protection. If MySQL disappears during startup
            # reconciliation, those last-known cards and ownership proofs
            # remain available to the bounded emergency path instead of an
            # empty read replacing them.
            database_writable = self._probe_database_writable(force=True)
            if database_writable and not self._standby_only:
                self._lease_current = self._lease_still_current(force=True)
                if not self._lease_current:
                    raise LeaseExpiredError(
                        "The execution lease expired before startup reconciliation"
                    )
            if database_writable:
                # Startup reconciliation is projection-only. Reducer commands
                # remain journaled but cannot cross the broker boundary until
                # the device reaches ACTIVE after the final pass.
                self._run_startup_reconciliation(execute_commands=False)
                if (
                    migration_manager.state.phase
                    == MigrationPhase.AWAITING_RECONCILIATION
                    and self.startup_reconciliation_complete
                ):
                    migration_manager.mark_reconciliation_complete()
                database_writable = self._probe_database_writable(force=True)
            if database_writable:
                self._set_device_state(RuntimeDeviceState.STANDBY)
            else:
                # There is no safe durable state write while canonical MySQL
                # is absent. Keep the worker alive but locally non-active so
                # it can recover automatically; no startup-time mutation is
                # authorized from an unreconciled cold start.
                self.device_state = RuntimeDeviceState.STANDBY
                self.error_occurred.emit(
                    "Canonical trading database is offline. New entries are "
                    "closed; the runtime will retry and reconcile automatically."
                )
        except SQLAlchemyError as exc:
            logger.warning(
                "Buyboard runtime startup paused because the canonical "
                "database is unavailable: %s",
                scrub_sensitive_text(exc, account_no=self._account_no),
            )
            try:
                self._close_market_data()
            except Exception:
                logger.exception("Could not close market data after startup failure")
            self.device_state = RuntimeDeviceState.FAILED
            self.error_occurred.emit(
                "Buy Board execution stayed closed because the canonical "
                "trading database is offline."
            )
            return
        except Exception as exc:  # noqa: BLE001 - must not crash the app
            logger.exception("BuyboardRuntimeWorker failed to start")
            try:
                self._close_market_data()
            except Exception:
                logger.exception("Could not close market data after startup failure")
            try:
                self._set_device_state(RuntimeDeviceState.FAILED)
            except Exception:
                logger.exception("Could not persist failed runtime state")
            self.error_occurred.emit(f"Buy Board engine failed to start: {exc}")
            return

        try:
            while not self._stop_requested:
                if not is_buyboard_engine_enabled():
                    logger.info("BuyboardRuntimeWorker stopping: engine flag turned off")
                    break
                database_writable = self._probe_database_writable()
                if database_writable:
                    self._lease_current = (
                        False if self._standby_only else self._lease_still_current()
                    )
                if (
                    not self._standby_only
                    and database_writable
                    and not self._lease_current
                ):
                    self._accepting_commands = False
                    logger.info("BuyboardRuntimeWorker stopping: main-device lease no longer current")
                    self._raise_external_alert(
                        CriticalAlertType.EXECUTION_LEASE_LOST,
                        f"{self._environment}:{self._account_no or '*'}:{self._device_id}",
                        "The authoritative execution lease is no longer current",
                    )
                    break
                if database_writable and not self._complete_database_recovery():
                    self.msleep(max(1, int(self._heartbeat_seconds * 1000)))
                    continue
                self.last_cycle_started_at = datetime.now(timezone.utc)
                try:
                    allow_mutations = self.device_state == RuntimeDeviceState.ACTIVE
                    self._run_one_cycle(allow_mutations=allow_mutations)
                    self.last_heartbeat_at = datetime.now(timezone.utc)
                    if (
                        database_writable
                        and self.device_state == RuntimeDeviceState.ACTIVE
                    ):
                        # ACTIVE is not a one-time declaration. Publish the
                        # current readiness/revision facts on the independent
                        # durable-device cadence so handoff never relies on a
                        # startup-era snapshot without writing every tick.
                        self._publish_device_state_if_due(RuntimeDeviceState.ACTIVE)
                    if self._external_alerting is not None:
                        publish_async = getattr(
                            self._external_alerting,
                            "publish_heartbeat_async_if_due",
                            None,
                        )
                        if callable(publish_async):
                            publish_async()
                        else:
                            # Compatibility for focused test doubles and
                            # older adapters. Production uses the nonblocking
                            # publisher above.
                            self._external_alerting.publish_heartbeat_if_due()
                    self._process_due_external_alerts()
                    if not allow_mutations:
                        self._advance_startup_readiness()
                except SQLAlchemyError as exc:
                    # MySQL can disappear after the successful probe but
                    # before a strict card/reconciliation read. Mark the
                    # outage immediately and preserve the prior card cache;
                    # the next cycle can continue bounded protection without
                    # another canonical read.
                    self._database_writable = False
                    if self.execution_gateway is not None:
                        self.execution_gateway.note_canonical_database_unavailable()
                    logger.warning(
                        "Buyboard runtime cycle paused because the canonical "
                        "database became unavailable: %s",
                        scrub_sensitive_text(exc, account_no=self._account_no),
                    )
                    self._raise_external_alert(
                        CriticalAlertType.DATABASE_UNAVAILABLE,
                        f"{self._environment}:{self._device_id}",
                        "The canonical trading database became unavailable; "
                        "new entries are closed and only bounded protective "
                        "actions remain eligible.",
                    )
                    self.error_occurred.emit(
                        "Canonical trading database went offline. New entries "
                        "are closed; cached position protection remains bounded."
                    )
                except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                    logger.exception("BuyboardRuntimeWorker heartbeat cycle failed")
                    if self.device_state == RuntimeDeviceState.STANDBY_READY:
                        try:
                            self._demote_standby_readiness()
                        except Exception:
                            logger.exception(
                                "Could not persist standby demotion after cycle failure"
                            )
                    self.error_occurred.emit("Buy Board engine heartbeat failed -- see logs for detail.")
                self.msleep(max(1, int(self._heartbeat_seconds * 1000)))
        finally:
            self._accepting_commands = False
            self._perform_shutdown_sequence()

    def _complete_database_recovery(self) -> bool:
        """Reconcile broker truth before reopening a recovered canonical DB."""

        if not self._recovery_reconciliation_required:
            return True
        self._accepting_commands = False
        self._run_startup_reconciliation(execute_commands=False)
        if not self.startup_reconciliation_complete:
            self.error_occurred.emit(
                "Database recovered, but full broker reconciliation is incomplete; "
                "execution remains closed."
            )
            return False
        self._recovery_reconciliation_required = False
        self._resolve_external_alert(
            CriticalAlertType.DATABASE_UNAVAILABLE,
            f"{self._environment}:{self._device_id}",
        )
        if self.device_state == RuntimeDeviceState.ACTIVE:
            self._accepting_commands = True
        return True

    def _advance_startup_readiness(self) -> None:
        readiness = self.engine_readiness(include_device_state=False)
        handoff_ready = self.lease_handoff_ready(readiness)
        required_ready = handoff_ready if self._standby_only else readiness.standby_ready
        if not required_ready:
            if self.device_state == RuntimeDeviceState.STANDBY_READY:
                self._demote_standby_readiness()
            return
        if self._standby_only:
            if self.device_state != RuntimeDeviceState.STANDBY_READY:
                # A standby publishes readiness only after its own final
                # projection-only broker reconciliation and a second complete
                # dependency check.
                self._run_startup_reconciliation(execute_commands=False)
                self._refresh_observation_after_final_reconciliation()
                readiness = self.engine_readiness(include_device_state=False)
                if not self.lease_handoff_ready(readiness):
                    self._demote_standby_readiness()
                    return
            # Subsequent writes are successor-owned heartbeats. They preserve
            # this readiness generation and do not refresh confirmation time.
            self._publish_device_state_if_due(RuntimeDeviceState.STANDBY_READY)
            return
        # The lease was acquired against the prior standby generation. Perform
        # the immediate activation reconciliation with the command gate shut.
        self._run_startup_reconciliation(execute_commands=False)
        # A real account reconciliation can outlast the strict accumulator
        # drain budget.  Refresh observation state *after* that blocking pass
        # while the mutation gate is still closed; otherwise an otherwise
        # healthy successor can remain STANDBY forever because every final
        # reconciliation makes the preceding drain stale before readiness is
        # rechecked.  KIS stop breaches remain latched until the engine
        # acknowledges them, so draining here cannot lose protective intent.
        self._refresh_observation_after_final_reconciliation()
        # Force a fresh database read while preserving the no-argument seam
        # used by readiness diagnostics/tests.
        self._last_lease_checked_at = None
        self._lease_current = self._lease_still_current()
        readiness = self.engine_readiness(include_device_state=False)
        if not (self._lease_current and readiness.standby_ready):
            if self.device_state == RuntimeDeviceState.STANDBY_READY:
                self._demote_standby_readiness()
            return
        if self.device_state != RuntimeDeviceState.STANDBY_READY:
            self._set_device_state(RuntimeDeviceState.STANDBY_READY)
        # Persist ACTIVE before either local ACTIVE or the mutation gate can
        # become observable. A failed write leaves both closed.
        self._set_device_state(RuntimeDeviceState.ACTIVE)
        self._accepting_commands = True

    def lease_handoff_ready(
        self, readiness: Optional[EngineReadiness] = None
    ) -> bool:
        """Return whether this read-only successor can own the Main lease.

        Stable global infrastructure is mandatory in every session.  Volatile
        quote freshness is enforced for the exact symbol at each execution
        boundary, so one quiet symbol cannot demote the entire successor.
        Broker reconciliation, subscription ACKs, queue health, and canonical
        database access all remain fail-closed.
        """

        current = readiness or self.engine_readiness(include_device_state=False)
        return current.standby_ready

    def _refresh_observation_after_final_reconciliation(self) -> None:
        """Refresh the local feed-drain timestamp after a blocking REST pass."""

        if self.runtime is None:
            return
        # A standby may have intentionally polled the shared card collection
        # only once per five minutes. Ownership activation is an exceptional
        # safety boundary: force a full canonical read and install every new
        # quote subscription/stop before declaring this successor ACTIVE.
        cards = self._load_cards_if_changed(force=True)
        self._sync_quote_subscriptions(cards)
        self._sync_market_stop_rules(cards, apply_pending_changes=False)
        self.runtime.market_data.poll_once()
        self.last_market_data_drain_at = datetime.now(timezone.utc)

    def _perform_shutdown_sequence(self) -> None:
        """E4: gate commands, flush, reconcile, close feed, then stop."""

        self.shutdown_errors = []
        try:
            self._set_device_state(RuntimeDeviceState.SHUTTING_DOWN)
        except Exception as exc:
            self.shutdown_errors.append(f"device-state: {exc}")
        try:
            self._journal_flush()
        except Exception as exc:
            self.shutdown_errors.append(f"journal flush: {exc}")
        if self.runtime is not None:
            try:
                self._run_startup_reconciliation(execute_commands=False)
            except Exception as exc:
                self.shutdown_errors.append(f"final reconciliation: {exc}")
            try:
                self._close_market_data()
            except Exception as exc:
                self.shutdown_errors.append(f"market-data close: {exc}")
        self.shutdown_prepared = not self.shutdown_errors
        try:
            self._set_device_state(
                RuntimeDeviceState.STOPPED if self.shutdown_prepared else RuntimeDeviceState.FAILED
            )
        except Exception as exc:
            self.shutdown_errors.append(f"final device-state: {exc}")
            self.shutdown_prepared = False

    def _close_market_data(self) -> None:
        if self.runtime is None:
            return
        market_data = self.runtime.market_data
        configure = getattr(market_data, "configure_desired_channels", None)
        if callable(configure):
            configure(trade_priorities={}, quote_priorities={})
        else:
            subscribed = getattr(market_data, "subscribed_symbols", lambda: [])
            symbols = list(subscribed())
            if symbols:
                market_data.unsubscribe(symbols)
        stop_market_data = getattr(market_data, "stop", None)
        if callable(stop_market_data):
            stop_market_data()

    def set_cross_device_operator_sync(self, enabled: bool) -> None:
        """Switch the remote plan/command consumer without restarting runtime."""

        enabled = bool(enabled)
        if enabled == self._cross_device_operator_sync:
            return
        self._cross_device_operator_sync = enabled
        if enabled:
            # Make the next one-second runtime cycle perform the first split-
            # role check immediately; later checks use the fixed five-second
            # cadence.
            self._last_card_revision_checked_at = None
            self._last_operator_command_poll_at = None

    def _lease_still_current(self, *, force: bool = False) -> bool:
        """Review finding P0-1: "Stop the worker immediately on lease
        loss." Every order submission already re-checks the lease at the
        broker boundary (``submit_guarded_overseas_order``); this
        additionally stops the *loop itself* so a demoted device does not
        keep polling quotes/hammering the DB on behalf of a board it no
        longer has authority over.
        """
        now = datetime.now(timezone.utc)
        from src.services.coordination_change_pulse import (
            change_notifications_available,
            coordination_table_change_generation,
            remote_peer_confirmed_off,
        )

        pulse_generation = coordination_table_change_generation(
            self._db_engine, {"app_state_sync"}
        )
        fallback_seconds = (
            execution_config.COORDINATION_REMOTE_FALLBACK_SECONDS
            if change_notifications_available(self._db_engine)
            else execution_config.COORDINATION_LEASE_POLL_SECONDS
        )
        peer_offline = remote_peer_confirmed_off(self._db_engine)
        if (
            not force
            and pulse_generation == self._last_lease_change_pulse_generation
            and self._last_lease_checked_at is not None
            and (
                peer_offline
                or (now - self._last_lease_checked_at).total_seconds()
                < fallback_seconds
            )
        ):
            return self._lease_current
        self._last_lease_checked_at = now
        self._last_lease_change_pulse_generation = pulse_generation
        if self._execution_authority is None:
            if (
                self.execution_gateway is not None
                and self._database_writable
                and self._execution_lease_value() is not None
            ):
                self.execution_gateway.note_canonical_lease_verified(
                    self._execution_lease_value()
                )
            self._lease_current = True
            return True
        try:
            self._execution_authority.require_current_lease(self._lease_engine, self._execution_lease)
            if self.execution_gateway is not None and self._database_writable:
                self.execution_gateway.note_canonical_lease_verified(
                    self._execution_lease_value()
                )
            self._lease_current = True
            return True
        except LeaseExpiredError:
            self._lease_current = False
            return False

    def _card_lookup(self, environment: str, account_no: str, symbol: str) -> Optional[TradeCardState]:
        return repo.get_trade_card(
            self._db_engine,
            environment,
            account_no,
            symbol,
            raise_on_error=True,
        )

    def _persist_execution_identity(self, card: TradeCardState) -> None:
        """Durably commit command identity before a guarded gateway call."""
        if (
            self.execution_gateway is not None
            and self._database_probe_completed
            and not self._database_writable
        ):
            # An emergency-only command will fsync this identity and full
            # order payload in the gateway's local journal before its broker
            # call. Ordinary actions remain blocked by the gateway.
            return
        repo.update_trade_card(self._db_engine, card, expected_version=card.version)

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

    def _configure_verified_mutation_budgets(
        self, cards: List[TradeCardState]
    ) -> None:
        """Activate only live-verified WS0 budgets, once per account/endpoint."""

        configure = getattr(
            self.request_scheduler, "configure_verified_mutation_budget", None
        )
        if not callable(configure) or not execution_config.KIS_MUTATION_BUDGET_VERIFIED:
            return
        policies = {
            "submit_order": execution_config.KIS_SUBMIT_MUTATION_CAPACITY,
            "cancel_order": execution_config.KIS_CANCEL_MUTATION_CAPACITY,
            "replace_order": execution_config.KIS_REPLACE_MUTATION_CAPACITY,
        }
        for account_no in self._distinct_account_numbers(cards):
            for endpoint, capacity in policies.items():
                if int(capacity) <= 0:
                    continue
                configure(
                    account_no=account_no,
                    endpoint=endpoint,
                    capacity=int(capacity),
                    window_seconds=(
                        execution_config.KIS_MUTATION_BUDGET_WINDOW_SECONDS
                    ),
                )

    def _cache_emergency_ownership_proofs(
        self, cards: List[TradeCardState]
    ) -> None:
        gateway = self.execution_gateway
        if gateway is None or not self._database_writable:
            return
        now = datetime.now(timezone.utc)
        if (
            self._last_ownership_proof_at is not None
            and (now - self._last_ownership_proof_at).total_seconds()
            < execution_config.COORDINATION_OWNERSHIP_PROOF_SECONDS
        ):
            return
        self._last_ownership_proof_at = now
        # Offline proof exists only to protect a real position. Buylist and
        # Buy Today cards cannot use the emergency mutation path, so querying
        # ownership for every candidate wastes one round-trip per card.
        protected_cards = [
            card
            for card in cards
            if int(card.broker_quantity or 0) > 0
            or card.board_status
            in {
                BoardStatus.OPEN_POSITION,
                BoardStatus.PARTIAL_SELL,
                BoardStatus.SELL_ALL,
            }
        ]
        if not protected_cards:
            return
        from src.services.execution_ownership_repository import (
            list_execution_ownership,
        )

        ownership_by_key = {
            (row.environment, row.account_no, row.symbol): row
            for row in list_execution_ownership(
                self._db_engine, environment=self._environment
            )
        }
        for card in protected_cards:
            try:
                ownership = ownership_by_key.get(
                    (card.environment, card.account_no, card.symbol)
                )
                if ownership is None:
                    continue
                gateway.note_canonical_ownership_snapshot_verified(
                    ownership,
                    source=ExecutionSource.KANBAN_BOARD,
                    strategy_instance_id=self._strategy_instance_id,
                )
            except ExecutionOwnershipMismatchError:
                # Non-Kanban cards are intentionally not eligible for the
                # bounded offline mutation path.
                continue

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

    def _run_startup_reconciliation(self, *, execute_commands: bool = True) -> None:
        """Restores retry bookkeeping (review finding P0-4's predecessor,
        section 1070-1075's "Run full startup reconciliation") and corrects
        every card's positions/orders against broker truth before the first
        heartbeat tick runs -- looping per real account_no (see
        :meth:`_distinct_account_numbers`) rather than issuing one
        broker call for the worker's own blank account scope.
        """
        assert self.runtime is not None
        # Seed the normal revisioned runtime cache during startup.  Loading
        # directly here left the cache uninitialized, so the first one-second
        # cycle immediately downloaded the same full TradeCard payload again.
        cards = self._load_cards_if_changed(force=True)
        self._configure_verified_mutation_budgets(cards)
        self._cache_emergency_ownership_proofs(cards)
        for card in cards:
            self.runtime.entry_attempt_manager.restore_symbol_state(
                card.environment,
                card.account_no,
                card.symbol,
                cooldown_until=card.next_retry_at,
                attempt_group_id=card.entry_attempt_group_id,
                attempt_count=card.entry_attempt_count,
            )

        now = datetime.now(timezone.utc)
        changed_ids: set = set()
        changed: List[TradeCardState] = []
        expected_accounts = self._distinct_account_numbers(cards)
        self._reconciliation_required_accounts.update(expected_accounts)
        self.startup_reconciliation_errors = {}
        for account_no in expected_accounts:
            self.reconciliation_accounts_in_progress.add(account_no)
            try:
                account_cards = [card for card in cards if card.account_no == account_no]
                result = run_account_reconciliation_pass(
                    broker=self.runtime.broker,
                    engine=self._db_engine,
                    environment=self._environment,
                    account_no=account_no,
                    cards=account_cards,
                    position_balance_extractor=self._extract_account_balance,
                )
                self._latest_reconciliation_snapshots[account_no] = result.snapshot
                account_changed = list(result.plan.changed_cards)
                if execute_commands and self._accepting_commands:
                    account_changed.extend(
                        self._execute_reconciliation_commands(result)
                    )
                self._handle_reconciliation_result(result)
            except (KisRateLimitError, KisTransientApiError) as exc:
                # Read-only KIS outages are expected to clear.  Preserve the
                # fail-closed reconciliation fence without rendering a full
                # traceback as though the runtime itself crashed.
                logger.warning(
                    "Startup reconciliation temporarily unavailable for account "
                    "%s: %s",
                    account_no,
                    exc,
                )
                self._invalidate_account_reconciliation(
                    account_no,
                    str(exc),
                    confirm_for_operator=True,
                )
                continue
            except SQLAlchemyError as exc:
                # A canonical outage is an expected infrastructure failure,
                # not a Python crash. Keep it concise; readiness still fails
                # closed and the external incident uses its offline spool.
                logger.warning(
                    "Startup reconciliation paused for account %s because "
                    "the canonical database is unavailable: %s",
                    account_no,
                    scrub_sensitive_text(exc, account_no=account_no),
                )
                self._invalidate_account_reconciliation(
                    account_no,
                    str(exc),
                    confirm_for_operator=True,
                )
                continue
            except Exception as exc:
                # Review finding P0: this account did NOT get reconciled --
                # startup_reconciliation_complete below must reflect that,
                # not report blanket success while one account's broker
                # truth was never actually checked.
                logger.exception(
                    "Startup reconciliation failed for account %s", account_no
                )
                self._invalidate_account_reconciliation(
                    account_no,
                    str(exc),
                    confirm_for_operator=True,
                )
                continue
            finally:
                self.reconciliation_accounts_in_progress.discard(account_no)
            for card in account_changed:
                if id(card) not in changed_ids:
                    changed_ids.add(id(card))
                    changed.append(card)
            if result.snapshot.completeness.account_balance_complete:
                self._record_reconciliation_balance(account_no, result)
                self._account_balance_refreshed_at[account_no] = now
            if not self._reconciliation_snapshot_complete(result):
                self.startup_reconciliation_errors[account_no] = (
                    "; ".join(result.snapshot.errors)
                    or "account broker snapshot was incomplete"
                )
                self._record_account_reconciliation_failure(
                    account_no, confirm_for_operator=True
                )
                continue
            self._record_account_reconciliation_success(account_no, now)

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

    def _process_due_external_alerts(
        self, *, now: Optional[datetime] = None
    ) -> bool:
        """Poll durable alerts without keeping an idle TiDB instance awake.

        When the Tailscale status probe confirms the other device is off,
        there is no remote alert producer to observe.  Align the remaining
        retry scan with this device's four-minute readiness heartbeat so the
        database sees one compact burst followed by a genuinely quiet gap.
        Local alert creation still writes immediately, and the independent
        external watchdog heartbeat remains on its fast webhook cadence.
        """

        if not self._database_writable or self._external_alerting is None:
            return False
        from src.services.coordination_change_pulse import remote_peer_confirmed_off

        interval = float(execution_config.COORDINATION_ALERT_POLL_SECONDS)
        if remote_peer_confirmed_off(self._db_engine):
            interval = max(
                interval,
                float(execution_config.COORDINATION_DEVICE_HEARTBEAT_SECONDS),
            )
        reference = now or datetime.now(timezone.utc)
        if (
            self._last_alert_poll_at is not None
            and (reference - self._last_alert_poll_at).total_seconds() < interval
        ):
            return False
        self._last_alert_poll_at = reference
        self._external_alerting.process_due()
        return True

    def _load_cards_if_changed(self, *, force: bool = False) -> List[TradeCardState]:
        """Refresh card payloads only after a compact revision token changes."""

        now = datetime.now(timezone.utc)
        local_generation = repo.local_trade_card_change_generation(self._db_engine)
        from src.services.coordination_change_pulse import (
            change_notifications_available,
            coordination_table_change_generation,
        )

        pulse_generation = coordination_table_change_generation(
            self._db_engine, {"trade_cards"}
        )
        if self._cross_device_operator_sync:
            interval = execution_config.COORDINATION_SPLIT_ROLE_SYNC_SECONDS
        elif change_notifications_available(self._db_engine):
            interval = execution_config.COORDINATION_REMOTE_FALLBACK_SECONDS
        else:
            interval = (
                execution_config.COORDINATION_STANDBY_CARD_POLL_SECONDS
                if self._standby_only
                else execution_config.COORDINATION_ACTIVE_CARD_POLL_SECONDS
            )
        if (
            not force
            and self._card_cache_initialized
            and local_generation == self._local_card_change_generation
            and (
                not self._cross_device_operator_sync
                or pulse_generation == self._last_card_change_pulse_generation
            )
            and self._last_card_revision_checked_at is not None
            and (
                not self._cross_device_operator_sync
                or (now - self._last_card_revision_checked_at).total_seconds()
                < interval
            )
        ):
            return self._cached_cards
        revision = repo.get_trade_card_collection_revision(
            self._db_engine,
            environment=self._environment,
            account_no=self._account_no or None,
            raise_on_error=True,
        )
        self._last_card_revision_checked_at = now
        self._local_card_change_generation = local_generation
        self._last_card_change_pulse_generation = pulse_generation
        if (
            not force
            and self._card_cache_initialized
            and revision == self._card_collection_revision
        ):
            return self._cached_cards
        cards = repo.list_trade_cards(
            self._db_engine,
            environment=self._environment,
            account_no=self._account_no or None,
            raise_on_error=True,
        )
        self._cached_cards = cards
        self._card_cache_initialized = True
        self._card_collection_revision = revision
        return cards

    def _run_one_cycle(self, *, allow_mutations: bool = True) -> None:
        assert self.runtime is not None
        canonical_available = bool(
            self._database_writable or not self._database_probe_completed
        )
        if canonical_available:
            cards = self._load_cards_if_changed()
            self._configure_verified_mutation_budgets(cards)
            self._cache_emergency_ownership_proofs(cards)
        else:
            # Observation continues from the last canonical snapshot. Only
            # protective SELL commands can get through the gateway while
            # offline, and they must first enter the local emergency journal.
            cards = self._cached_cards

        allow_mutations = bool(allow_mutations and self._accepting_commands)
        operator_commands_changed = False
        if canonical_available and allow_mutations:
            operator_commands_changed = self._process_operator_commands()
            if operator_commands_changed:
                cards = self._load_cards_if_changed(force=True)
        reconciliation_changed = False
        if canonical_available:
            reconciliation_changed = bool(
                self._refresh_account_state_if_due(
                    cards, execute_commands=allow_mutations
                )
            )
            # Reconciliation persists cloned reducer outputs. Reload only
            # when it actually changed durable state.
            if reconciliation_changed:
                cards = self._load_cards_if_changed(force=True)

        changed_ids: set = set()
        changed: List[TradeCardState] = []
        breach_ack_candidates: set[tuple[str, str, str]] = set()

        def _track(touched: List[TradeCardState]) -> None:
            for card in touched:
                if id(card) not in changed_ids:
                    changed_ids.add(id(card))
                    changed.append(card)

        ambiguous_orb_keys = _ambiguous_buy_today_orb_keys(cards)
        if allow_mutations:
            # Synchronize ORB state before deriving action/subscription sets.
            # A card whose three ORB windows are terminal-invalid can return
            # to Buylist here and leave both evaluation and feed capacity in
            # this same cycle.
            _track(
                self._sync_orb_plans(
                    cards,
                    ambiguous_orb_keys=ambiguous_orb_keys,
                )
            )

        # The periodic refresh runs over every account regardless of
        # readiness. Later work is gated per card/action: an unrelated
        # balance or reserved-history failure must not suppress a safe
        # protective exit, while a new entry still requires its complete
        # holdings/open-order/buying-power evidence set.
        ready_cards = (
            [card for card in cards if self._card_action_ready(card)]
            if canonical_available
            else []
        )
        execution_ready_cards = [
            card
            for card in ready_cards
            if self._card_in_execution_scope(card)
            and (
                str(card.environment or "").strip().upper(),
                str(card.symbol or "").strip().upper(),
            )
            not in ambiguous_orb_keys
        ]
        observation_cards = [
            card for card in cards if card.board_status in _QUOTE_SUBSCRIBED_STATUSES
        ]
        # Installing a durable stop request into the local market-data rule
        # set is not a broker mutation.  The exact Main lease must be able to
        # complete that handoff before the first regular-session quote makes
        # the runtime ACTIVE; otherwise one premarket request remains pending
        # indefinitely and fences every later stop edit.  A pull-only
        # successor still cannot acknowledge canonical stop state.
        allow_stop_change_ack = bool(
            canonical_available
            and not self._standby_only
            and (allow_mutations or self._lease_current)
        )
        durably_liquidating_card_keys = {
            card.card_key for card in observation_cards if card.exit_all_required
        }
        stop_card_keys = [card.card_key for card in observation_cards]

        # Observation readiness is intentionally broader than mutation
        # readiness: a reconciliation failure may block a command but must
        # never make the feed stop watching an existing position.
        self._sync_quote_subscriptions(observation_cards)
        with self._stop_change_coordinator.lock_cards(stop_card_keys):
            # A stop can commit after this cycle loaded ``cards``. Overlay
            # that exact durable request before rotating/draining so the
            # stale object cannot consume a post-request event using only
            # its old stop.
            self._stop_change_coordinator.overlay_pending(observation_cards)
            self._sync_market_stop_rules(
                observation_cards,
                apply_pending_changes=allow_stop_change_ack,
            )

            quotes = self.runtime.market_data.poll_once()
            self.last_market_data_drain_at = datetime.now(timezone.utc)
            if allow_mutations:
                for quote in quotes:
                    _track(self.runtime.trading_engine.evaluate_quote(observation_cards, quote))
                    pending_handoff = (
                        self.runtime.trading_engine.evaluate_pending_stop_handoff(
                            observation_cards, quote
                        )
                    )
                    _track(pending_handoff)
                    self._latch_pending_stop_breaches(
                        quote, pending_handoff, breach_ack_candidates
                    )
                    _track(
                        self.runtime.trading_engine.evaluate_entry_quote(
                            execution_ready_cards,
                            quote,
                            prepare_entry_plan=self._prepare_crossed_orb_entry_plan,
                        )
                    )
                    self._collect_market_breach_ack_candidates(
                        quote, observation_cards, breach_ack_candidates
                    )
            if allow_stop_change_ack:
                _track(self._acknowledge_pending_stop_changes(observation_cards))

        if allow_mutations:
            heartbeat_cards = execution_ready_cards
            if not canonical_available:
                heartbeat_cards = [
                    card
                    for card in observation_cards
                    if card.board_status
                    in {
                        BoardStatus.OPEN_POSITION,
                        BoardStatus.PARTIAL_SELL,
                        BoardStatus.SELL_ALL,
                    }
                ]
            _track(self.runtime.trading_engine.run_heartbeat(heartbeat_cards))
            if canonical_available:
                # Buy Today is valid for one regular session only.  Expiry is
                # a local board transition, not an entry submission, so run it
                # across the full card set even when account readiness kept a
                # card out of ``execution_ready_cards`` above.
                _track(
                    self.runtime.trading_engine.expire_buy_today_cards_if_due(
                        cards
                    )
                )
        # Stops can change inside the heartbeat (first-fill ORB stop,
        # completion-to-breakeven). Rotate under the feed's shared lock and
        # immediately evaluate any events detached from the old version.
        if allow_mutations:
            with self._stop_change_coordinator.lock_cards(stop_card_keys):
                # Catch a UI stop request that committed while heartbeat
                # work was running, even though this cycle's card list is
                # older than the request.
                self._stop_change_coordinator.overlay_pending(observation_cards)
                if self._sync_market_stop_rules(
                    observation_cards, apply_pending_changes=True
                ):
                    rotated_quotes = self.runtime.market_data.poll_once()
                    self.last_market_data_drain_at = datetime.now(timezone.utc)
                    for quote in rotated_quotes:
                        _track(self.runtime.trading_engine.evaluate_quote(observation_cards, quote))
                        pending_handoff = (
                            self.runtime.trading_engine.evaluate_pending_stop_handoff(
                                observation_cards, quote
                            )
                        )
                        _track(pending_handoff)
                        self._latch_pending_stop_breaches(
                            quote, pending_handoff, breach_ack_candidates
                        )
                        _track(
                            self.runtime.trading_engine.evaluate_entry_quote(
                                execution_ready_cards,
                                quote,
                                prepare_entry_plan=self._prepare_crossed_orb_entry_plan,
                            )
                        )
                        self._collect_market_breach_ack_candidates(
                            quote, observation_cards, breach_ack_candidates
                        )
                _track(self._acknowledge_pending_stop_changes(observation_cards))
        # The full card set, not just ready_cards: UNRECONCILED_BROKER_ORDER
        # can be set by _refresh_account_state_if_due's order reconciliation,
        # which (deliberately) still runs for every account, including ones
        # excluded from ready_cards.
        self._emit_stalled_liquidation_alerts(cards)

        persisted: List[TradeCardState] = []
        if canonical_available:
            persisted = self._persist_changed(changed)
        durable_breach_keys = durably_liquidating_card_keys | {
            card.card_key for card in persisted if card.exit_all_required
        }
        self._acknowledge_market_breach_candidates(
            breach_ack_candidates, durable_breach_keys
        )
        if changed or reconciliation_changed or operator_commands_changed:
            self.board_changed.emit()

    def _process_operator_commands(self, *, limit: int = 20) -> bool:
        """Apply live human requests only while this runtime owns execution."""

        if not self._cross_device_operator_sync:
            return False

        now = datetime.now(timezone.utc)
        from src.services.coordination_change_pulse import (
            coordination_table_change_generation,
        )

        pulse_generation = coordination_table_change_generation(
            self._db_engine, {"operator_commands"}
        )
        interval = execution_config.COORDINATION_SPLIT_ROLE_SYNC_SECONDS
        if (
            pulse_generation == self._last_operator_change_pulse_generation
            and self._last_operator_command_poll_at is not None
            and (now - self._last_operator_command_poll_at).total_seconds()
            < interval
        ):
            return False
        self._last_operator_command_poll_at = now
        self._last_operator_change_pulse_generation = pulse_generation

        from src.services.operator_command_service import (
            process_next_board_operator_command,
        )

        executor = LocalDeviceRole(
            device_id=self._device_id,
            hostname=self._hostname,
            is_main=True,
        )
        changed = False
        context = BoardActionContext(
            enforce_runtime_fences=False,
            engine_enabled=True,
            readiness_generation=int(self.readiness_generation or 0),
            reconciliation_in_progress=bool(
                self.reconciliation_accounts_in_progress
            ),
            action_ready=True,
            device_active=True,
            regular_session_open=is_regular_session_open(),
        )
        for _ in range(max(1, int(limit))):
            result = process_next_board_operator_command(
                self._db_engine,
                executor,
                context=context,
            )
            if result is None:
                break
            changed = True
            logger.info(
                "Operator command %s %s -> %s%s",
                result.command_type.value,
                result.command_id,
                result.status.value,
                f": {result.error_message}" if result.error_message else "",
            )
        return changed

    _EXIT_CANCEL_STALLED_WARNING = "EXIT_CANCEL_STALLED"
    _UNRECONCILED_BROKER_ORDER_WARNING = "UNRECONCILED_BROKER_ORDER"
    _TRADING_HALT_EXIT_WARNING = "TRADING_HALT_EXIT_PENDING"
    _DATA_STALE_WARNING = "DATA_STALE"
    _MARKET_DATA_OUTAGE_HIGH_WARNING = "MARKET_DATA_OUTAGE_HIGH"
    _MARKET_DATA_OUTAGE_LOW_WARNING = "MARKET_DATA_OUTAGE_LOW"
    _POSITION_RISK_BOARD_STATUSES = {
        BoardStatus.OPEN_POSITION,
        BoardStatus.PARTIAL_SELL,
        BoardStatus.SELL_ALL,
    }
    _POSITION_RISK_RUNTIME_STATUSES = set(PositionRuntimeStatus) - {
        PositionRuntimeStatus.NONE,
        PositionRuntimeStatus.CLOSED,
    }
    _POSITION_RISK_WARNINGS = {
        _EXIT_CANCEL_STALLED_WARNING,
        _TRADING_HALT_EXIT_WARNING,
        _DATA_STALE_WARNING,
        _MARKET_DATA_OUTAGE_HIGH_WARNING,
        _MARKET_DATA_OUTAGE_LOW_WARNING,
    }

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
        (
            _TRADING_HALT_EXIT_WARNING,
            lambda card: (
                f"CRITICAL: liquidation for {card.symbol} "
                f"({card.environment}:{card.account_no}) is retained but "
                "broker submission is paused by a verified trading halt."
            ),
        ),
        (
            _MARKET_DATA_OUTAGE_HIGH_WARNING,
            lambda card: (
                f"CRITICAL: high-risk market-data outage for {card.symbol} "
                f"({card.environment}:{card.account_no})."
            ),
        ),
        (
            _MARKET_DATA_OUTAGE_LOW_WARNING,
            lambda card: (
                f"CRITICAL: market-data outage for {card.symbol} "
                f"({card.environment}:{card.account_no})."
            ),
        ),
    )
    _RECOVERABLE_CARD_ALERTS = (
        (
            _EXIT_CANCEL_STALLED_WARNING,
            CriticalAlertType.CANCEL_CONFIRMATION_TIMEOUT,
        ),
        (_DATA_STALE_WARNING, CriticalAlertType.STALE_CRITICAL_SYMBOL),
        (_MARKET_DATA_OUTAGE_HIGH_WARNING, CriticalAlertType.MARKET_DATA_OUTAGE),
        (_MARKET_DATA_OUTAGE_LOW_WARNING, CriticalAlertType.MARKET_DATA_OUTAGE),
    )

    @classmethod
    def _warning_is_actionable(
        cls, card: TradeCardState, warning_name: str
    ) -> bool:
        """Ignore persisted position alarms after broker truth is flat.

        A previous process can stop between confirming the final fill and
        persisting removal of a warning.  On the next startup, the alert
        bridge observes the old card before the engine's cleanup write.  A
        warning string alone must not reopen a durable incident when the
        card is already flat and closed.
        """

        if warning_name not in cls._POSITION_RISK_WARNINGS:
            return True
        return bool(
            int(card.broker_quantity or 0) > 0
            or int(card.orderable_quantity or 0) > 0
            or card.board_status in cls._POSITION_RISK_BOARD_STATUSES
            or card.position_runtime_status in cls._POSITION_RISK_RUNTIME_STATUSES
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
                card.card_key
                for card in cards
                if warning_name in card.warnings
                and self._warning_is_actionable(card, warning_name)
            }
            newly_present = present_now - alerted
            for card in cards:
                if card.card_key in newly_present:
                    message = build_message(card)
                    self.alert.emit(message)
                    if warning_name == self._EXIT_CANCEL_STALLED_WARNING:
                        alert_type = CriticalAlertType.CANCEL_CONFIRMATION_TIMEOUT
                    elif warning_name == self._UNRECONCILED_BROKER_ORDER_WARNING:
                        alert_type = CriticalAlertType.DISCOVERED_EXTERNAL_ORDER
                    elif warning_name in {
                        self._MARKET_DATA_OUTAGE_HIGH_WARNING,
                        self._MARKET_DATA_OUTAGE_LOW_WARNING,
                    }:
                        alert_type = CriticalAlertType.MARKET_DATA_OUTAGE
                    else:
                        alert_type = CriticalAlertType.ACCOUNT_RECONCILIATION_FAILED
                    self._raise_external_alert(
                        alert_type,
                        f"{card.card_key}:{warning_name}",
                        message,
                    )
            self._alerted_card_keys_by_warning[warning_name] = present_now
        self._resolve_cleared_recoverable_alerts(cards)

    def _resolve_cleared_recoverable_alerts(
        self, cards: List[TradeCardState]
    ) -> None:
        """Stop retrying durable card incidents after their risk clears.

        The first observation also reconciles incidents left OPEN by an older
        process.  Later observations resolve only present-to-absent warning
        transitions, avoiding database work on every heartbeat.
        """

        card_keys = {card.card_key for card in cards}
        first_observation = any(
            warning_name not in self._card_alert_presence_by_warning
            for warning_name, _alert_type in self._RECOVERABLE_CARD_ALERTS
        )
        durable_open_keys = None
        if first_observation and self._external_alerting is not None:
            lookup = getattr(self._external_alerting, "open_incident_keys", None)
            if callable(lookup):
                try:
                    durable_open_keys = set(
                        lookup(
                            alert_type
                            for _warning_name, alert_type in self._RECOVERABLE_CARD_ALERTS
                        )
                    )
                except Exception:
                    # Do not mark the first observation complete when the
                    # canonical lookup fails.  A later heartbeat retries the
                    # recovery sweep instead of silently stranding incidents.
                    logger.exception(
                        "Could not load recoverable external-alert incidents"
                    )
                    return
        for warning_name, alert_type in self._RECOVERABLE_CARD_ALERTS:
            present_now = {
                card.card_key
                for card in cards
                if warning_name in card.warnings
                and self._warning_is_actionable(card, warning_name)
            }
            previous = self._card_alert_presence_by_warning.get(warning_name)
            if previous is None:
                candidates = card_keys - present_now
                if durable_open_keys is not None:
                    alert_value = alert_type.value
                    cleared = {
                        card_key
                        for card_key in candidates
                        if (
                            alert_value,
                            f"{card_key}:{warning_name}",
                        )
                        in durable_open_keys
                    }
                else:
                    # Compatibility for lightweight alert adapters that have
                    # not implemented the bulk read seam.
                    cleared = candidates
            else:
                cleared = previous - present_now
            for card_key in cleared:
                self._resolve_external_alert(
                    alert_type,
                    f"{card_key}:{warning_name}",
                )
            self._card_alert_presence_by_warning[warning_name] = present_now

    # -- periodic per-account KIS refresh (review: no cadence populated the --
    # -- buying-power cache or re-reconciled positions after startup) --------

    def _refresh_account_state_if_due(
        self,
        cards: List[TradeCardState],
        *,
        execute_commands: bool = True,
    ) -> List[TradeCardState]:
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

        # Review finding: "accounts without existing cards remain
        # undiscoverable" -- _distinct_account_numbers (not a plain
        # card-account grouping) also covers every configured-but-cardless
        # account, with an empty account_cards list; reconcile_broker_positions
        # correctly treats every holding found there as newly discovered.
        account_numbers = self._distinct_account_numbers(cards)
        self._reconciliation_required_accounts.update(account_numbers)
        for account_no in account_numbers:
            retry_not_before = self._account_refresh_retry_not_before.get(account_no)
            if retry_not_before is not None and now < retry_not_before:
                continue
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

            if reconcile_due:
                self.reconciliation_accounts_in_progress.add(account_no)
                self.routine_reconciliation_accounts_in_progress.add(account_no)
                try:
                    result = run_account_reconciliation_pass(
                        broker=self.runtime.broker,
                        engine=self._db_engine,
                        environment=self._environment,
                        account_no=account_no,
                        cards=account_cards,
                        position_balance_extractor=self._extract_account_balance,
                    )
                except Exception as exc:
                    self._defer_account_refresh(account_no, now)
                    if isinstance(exc, (KisRateLimitError, KisTransientApiError)):
                        logger.warning(
                            "Periodic account reconciliation temporarily unavailable "
                            "for account %s; retry deferred for %.0fs: %s",
                            account_no,
                            self._ACCOUNT_REFRESH_FAILURE_COOLDOWN_SECONDS,
                            exc,
                        )
                    else:
                        logger.exception(
                            "Periodic account reconciliation failed for account %s",
                            account_no,
                        )
                    self._invalidate_account_reconciliation(
                        account_no,
                        "periodic account reconciliation failed",
                    )
                    continue
                finally:
                    self.routine_reconciliation_accounts_in_progress.discard(
                        account_no
                    )
                    self.reconciliation_accounts_in_progress.discard(account_no)
                self._latest_reconciliation_snapshots[account_no] = result.snapshot
                changed.extend(result.plan.changed_cards)
                if execute_commands and self._accepting_commands:
                    changed.extend(self._execute_reconciliation_commands(result))
                self._handle_reconciliation_result(result)
                if result.snapshot.completeness.account_balance_complete:
                    self._record_reconciliation_balance(account_no, result)
                    self._account_balance_refreshed_at[account_no] = now
                if not self._reconciliation_snapshot_complete(result):
                    reason = (
                        "; ".join(result.snapshot.errors)
                        or "account broker snapshot was incomplete"
                    )
                    self._invalidate_account_reconciliation(account_no, reason)
                    self._defer_account_refresh(account_no, now)
                    continue
                self._record_account_reconciliation_success(account_no, now)
                self._account_refresh_retry_not_before.pop(account_no, None)
                # Review finding P0: "unknown accounts can be incorrectly
                # considered healthy" -- a full position+order
                # reconciliation just succeeded for this account (whether
                # or not it had ever run before), so it now has positive
                # confirmation too.
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
                continue

            # A buying-power-only refresh is intentionally not a
            # reconciliation pass, so it performs the one holdings query
            # it needs and no order discovery.
            try:
                position_snapshot = self.runtime.broker.get_positions(
                    environment=self._environment, account_no=account_no
                )
            except Exception as exc:
                self._defer_account_refresh(account_no, now)
                if isinstance(exc, (KisRateLimitError, KisTransientApiError)):
                    logger.warning(
                        "Periodic account refresh temporarily unavailable for account "
                        "%s; retry deferred for %.0fs: %s",
                        account_no,
                        self._ACCOUNT_REFRESH_FAILURE_COOLDOWN_SECONDS,
                        exc,
                    )
                else:
                    logger.exception(
                        "Periodic account refresh: get_positions failed for account %s",
                        account_no,
                    )
                continue
            self._record_buying_power(account_no, position_snapshot)
            self._account_balance_refreshed_at[account_no] = now
            self._account_refresh_retry_not_before.pop(account_no, None)

        return changed

    def _defer_account_refresh(self, account_no: str, now: datetime) -> None:
        self._account_refresh_retry_not_before[account_no] = now + timedelta(
            seconds=self._ACCOUNT_REFRESH_FAILURE_COOLDOWN_SECONDS
        )

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

    def _record_reconciliation_balance(
        self, account_no: str, result: AccountReconciliationResult
    ) -> None:
        from src.services import buying_power_cache

        snapshot = result.snapshot
        buying_power_cache.record_snapshot(
            environment=self._environment,
            account_no=account_no,
            usable_buying_power_usd=float(snapshot.account_buying_power or 0.0),
            total_equity_usd=float(snapshot.account_equity or 0.0),
            source="buyboard_runtime_account_reconciliation",
        )

    @staticmethod
    def _reconciliation_snapshot_complete(result: AccountReconciliationResult) -> bool:
        completeness = result.snapshot.completeness
        return bool(
            completeness.holdings_complete
            and completeness.open_orders_complete
            and completeness.history_complete
            and completeness.reserved_orders_complete
            and completeness.account_balance_complete
        )

    def _invalidate_account_reconciliation(
        self,
        account_no: str,
        reason: str,
        *,
        confirm_for_operator: bool = False,
    ) -> None:
        """Fail closed after a due pass cannot produce current broker truth."""
        self._record_account_reconciliation_failure(
            account_no, confirm_for_operator=confirm_for_operator
        )
        self._latest_reconciliation_snapshots.pop(account_no, None)
        self._account_reconciled_at.pop(account_no, None)
        self.startup_reconciled_accounts.discard(account_no)
        self.startup_reconciliation_errors[account_no] = str(
            reason or "account reconciliation failed"
        )
        self.startup_reconciliation_complete = False
        self._raise_external_alert(
            CriticalAlertType.ACCOUNT_RECONCILIATION_FAILED,
            f"{self._environment}:{account_no}",
            self.startup_reconciliation_errors[account_no],
        )

    def _record_account_reconciliation_failure(
        self, account_no: str, *, confirm_for_operator: bool = False
    ) -> None:
        account = str(account_no or "")
        if not account:
            return
        streak = (
            self._account_reconciliation_failure_streaks.get(account, 0) + 1
        )
        if confirm_for_operator:
            streak = max(
                streak, self._ACCOUNT_RECONCILIATION_FAILURE_CONFIRMATIONS
            )
        self._account_reconciliation_failure_streaks[account] = streak

    def _record_account_reconciliation_success(
        self, account_no: str, observed_at: datetime
    ) -> None:
        account = str(account_no or "")
        if not account:
            return
        self._account_reconciled_at[account] = observed_at
        self.startup_reconciled_accounts.add(account)
        self._account_reconciliation_confirmed_accounts.add(account)
        self._account_reconciliation_failure_streaks.pop(account, None)

    def _raise_external_alert(
        self,
        alert_type: CriticalAlertType,
        dedupe_key: str,
        message: str,
    ) -> None:
        if self._external_alerting is None:
            return
        try:
            sink_offline = getattr(self._external_alerting, "sink_offline", None)
            sink = getattr(self._external_alerting, "sink", None)
            if not self._database_writable and callable(sink_offline):
                # Never spend the execution thread's short emergency lease
                # allowance waiting on an alert webhook. The fsynced local
                # spool is durable; the external watchdog independently sees
                # the missing runtime heartbeat during the outage.
                sink_offline(
                    alert_type,
                    dedupe_key,
                    message,
                    deliver_directly=False,
                )
            elif callable(sink):
                sink(alert_type, dedupe_key, message)
            else:
                # Compatibility for focused test doubles and older adapters.
                self._external_alerting.raise_alert(
                    alert_type, dedupe_key, message
                )
        except Exception:
            logger.exception("Could not persist external alert %s", alert_type.value)

    def _resolve_external_alert(
        self,
        alert_type: CriticalAlertType,
        dedupe_key: str,
    ) -> None:
        if self._external_alerting is None:
            return
        resolve = getattr(self._external_alerting, "resolve_alert", None)
        if not callable(resolve):
            return
        try:
            resolve(
                alert_type,
                dedupe_key,
                resolved_by=f"runtime-recovery:{self._device_id or 'unknown-device'}",
            )
        except Exception:
            logger.exception("Could not resolve external alert %s", alert_type.value)

    def _card_action_ready(self, card: TradeCardState) -> bool:
        """Gate only the broker evidence needed by this card's next action."""
        if card.board_status in (
            BoardStatus.WATCHLIST,
            BoardStatus.BUYLIST,
            BoardStatus.BUY_TODAY,
        ):
            action = "NEW_ENTRY"
        elif card.board_status == BoardStatus.ENTRY_PENDING:
            action = "KNOWN_CANCEL"
        elif card.board_status in (
            BoardStatus.OPEN_POSITION,
            BoardStatus.PARTIAL_SELL,
            BoardStatus.SELL_ALL,
        ):
            action = "PROTECTIVE_EXIT"
        else:
            return True
        return self.account_action_ready(card.account_no, card.symbol, action)

    def _card_in_execution_scope(self, card: TradeCardState) -> bool:
        """Keep planning-only controlled-live entries away from mutation paths."""

        if card.board_status != BoardStatus.BUY_TODAY:
            return True
        return live_entry_symbol_allowed(
            environment=card.environment,
            symbol=card.symbol,
        )

    def account_action_ready(
        self, account_no: str, symbol: str, action: str
    ) -> bool:
        """Public health seam used to prevent cross-engine dual execution."""
        snapshot = self._latest_reconciliation_snapshots.get(account_no)
        if snapshot is None:
            return False
        completeness = snapshot.completeness
        normalized = str(action or "").upper()
        if normalized == "NEW_ENTRY":
            return completeness.allows(ReconciliationAction.NEW_ENTRY)
        if normalized == "KNOWN_CANCEL":
            return completeness.allows(ReconciliationAction.CANCEL_KNOWN_ORDER)
        if normalized == "PROTECTIVE_EXIT":
            if not completeness.holdings_complete:
                return False
            holding = snapshot.holding_for(symbol)
            safe_sell_exposure = bool(
                completeness.open_orders_complete
                or (holding is not None and holding.sellable_quantity is not None)
            )
            return safe_sell_exposure
        return False

    def _account_reconciliation_is_fresh(
        self,
        account_no: Optional[str] = None,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        reference = now or datetime.now(timezone.utc)
        if account_no is not None:
            observed = self._account_reconciled_at.get(str(account_no or ""))
            age = self._age_seconds(observed, reference)
            return bool(
                account_no
                and account_no in self.startup_reconciled_accounts
                and account_no not in self.startup_reconciliation_errors
                and age is not None
                and 0.0 <= age <= execution_config.FULL_RECONCILIATION_SECONDS
            )
        if not self.startup_reconciliation_complete:
            return False
        return all(
            (age := self._age_seconds(self._account_reconciled_at.get(item), reference))
            is not None
            and 0.0 <= age <= execution_config.FULL_RECONCILIATION_SECONDS
            for item in self.startup_reconciled_accounts
        )

    def engine_readiness(
        self,
        account_no: Optional[str] = None,
        *,
        symbol: str = "",
        action: str = "",
        include_device_state: bool = True,
        now: Optional[datetime] = None,
    ) -> EngineReadiness:
        """Return the complete E1 predicate as independently visible facts."""

        reference = now or datetime.now(timezone.utc)
        startup_complete = (
            bool(
                account_no
                and account_no in self.startup_reconciled_accounts
                and account_no not in self.startup_reconciliation_errors
            )
            if account_no is not None
            else bool(
                self.startup_reconciliation_ran
                and self.startup_reconciliation_complete
                and not self.startup_reconciliation_errors
            )
        )
        account_fresh = self._account_reconciliation_is_fresh(
            account_no, now=reference
        )
        if account_no is not None and action:
            account_fresh = account_fresh and self.account_action_ready(
                account_no, symbol, action
            )

        connected = False
        trade_acked = False
        quote_acked = False
        quotes_fresh = False
        queue_within_budget = False
        market_data = self.runtime.market_data if self.runtime is not None else None
        if market_data is not None:
            health_metrics = getattr(market_data, "health_metrics", None)
            if callable(health_metrics):
                try:
                    metrics = health_metrics(now=reference)
                except TypeError as exc:
                    if "unexpected keyword argument" not in str(exc):
                        raise
                    metrics = health_metrics()
                connected = bool(metrics.ws_connected)
                trade_acked = not bool(metrics.critical_trade_channels_missing)
                quote_acked = not bool(metrics.critical_quote_channels_missing)
                quotes_fresh = not bool(metrics.stale_symbols)
            else:
                connected = bool(market_data.is_connected())
                symbols = [symbol] if symbol else list(
                    getattr(market_data, "subscribed_symbols", lambda: [])()
                )
                symbol_ready = getattr(market_data, "is_symbol_execution_ready", None)
                quotes_fresh = bool(
                    callable(symbol_ready)
                    and all(symbol_ready(item, now=reference) for item in symbols)
                )
                trade_acked = connected and quotes_fresh
                quote_acked = connected and quotes_fresh

            if symbol and action:
                # An action for AAPL must not be blocked because an unrelated,
                # quiet critical symbol such as STIM lacks a three-second
                # event.  Revalidate the exact symbol here; the execution
                # gateway performs the same fail-closed check at submission.
                symbol_ready = getattr(
                    market_data, "is_symbol_execution_ready", None
                )
                quotes_fresh = bool(
                    callable(symbol_ready)
                    and symbol_ready(str(symbol).upper(), now=reference)
                )
                feed_available = getattr(
                    market_data, "is_symbol_feed_available", None
                )
                channel_ready = bool(
                    feed_available(str(symbol).upper())
                    if callable(feed_available)
                    else quotes_fresh
                )
                trade_acked = connected and channel_ready
                quote_acked = connected and channel_ready

            drain_age = self._age_seconds(self.last_market_data_drain_at, reference)
            drain_budget = max(
                float(execution_config.MAX_MARKET_DATA_QUEUE_DELAY_SECONDS),
                float(self._heartbeat_seconds)
                + float(execution_config.MAX_MARKET_DATA_QUEUE_DELAY_SECONDS),
            )
            queue_within_budget = bool(
                drain_age is not None and 0.0 <= drain_age <= drain_budget
            )
            queue_within_budget = self._confirmed_queue_drain_readiness(
                queue_within_budget,
                checked_at=reference,
            )

        return EngineReadiness(
            lease_current=bool(self._lease_current),
            startup_reconciliation_complete=startup_complete,
            account_reconciliation_fresh=account_fresh,
            websocket_connected=connected,
            critical_trade_subscriptions_acked=trade_acked,
            critical_quote_subscriptions_acked=quote_acked,
            critical_quotes_fresh=quotes_fresh,
            accumulator_draining_within_budget=queue_within_budget,
            database_writable=bool(self._database_writable),
            device_active=(
                self.device_state == RuntimeDeviceState.ACTIVE
                if include_device_state
                else True
            ),
        )

    def _operator_reconciliation_debounce_active(self) -> bool:
        """Whether a previously healthy routine pass is still unconfirmed.

        This affects only the progress projection.  ``engine_readiness`` and
        every command gateway continue using the strict broker timestamps and
        completeness evidence.
        """

        required = set(self._reconciliation_required_accounts)
        if not required or not required.issubset(
            self._account_reconciliation_confirmed_accounts
        ):
            return False
        if any(
            self._account_reconciliation_failure_streaks.get(account, 0)
            >= self._ACCOUNT_RECONCILIATION_FAILURE_CONFIRMATIONS
            for account in required
        ):
            return False
        retrying = any(
            0
            < self._account_reconciliation_failure_streaks.get(account, 0)
            < self._ACCOUNT_RECONCILIATION_FAILURE_CONFIRMATIONS
            for account in required
        )
        routine_in_progress = bool(
            required & self.routine_reconciliation_accounts_in_progress
        )
        return retrying or routine_in_progress

    def readiness_for_operator_display(
        self, readiness: Optional[EngineReadiness] = None
    ) -> EngineReadiness:
        """Return a stable UI projection without relaxing execution safety."""

        current = readiness or self.engine_readiness(include_device_state=False)
        if not self._operator_reconciliation_debounce_active():
            return current
        return replace(
            current,
            startup_reconciliation_complete=True,
            account_reconciliation_fresh=True,
        )

    def reconciliation_accounts_for_operator_display(self) -> Tuple[str, ...]:
        """Hide successful routine refreshes until failures are confirmed."""

        accounts = set(self.reconciliation_accounts_in_progress)
        if self._operator_reconciliation_debounce_active():
            accounts.difference_update(
                self.routine_reconciliation_accounts_in_progress
            )
        return tuple(sorted(accounts))

    def _confirmed_queue_drain_readiness(
        self,
        raw_within_budget: bool,
        *,
        checked_at: datetime,
    ) -> bool:
        """Debounce transient drain jitter without weakening startup safety.

        Before the queue has ever drained successfully, readiness remains
        fail-closed. After that initial proof, up to two consecutive late
        heartbeat cycles are tolerated; readiness blocks only on the third
        genuinely distinct failed cycle. Repeated UI reads during one slow
        broker call cannot manufacture extra failures. Any healthy observation
        resets the streak.
        """

        if raw_within_budget:
            self._market_data_queue_confirmed_healthy = True
            self._market_data_queue_failure_streak = 0
            self._market_data_queue_last_failure_cycle_started_at = None
            return True
        if not self._market_data_queue_confirmed_healthy:
            return False

        # ``engine_readiness`` is polled by the UI more often than the worker
        # can finish a KIS reconciliation. Count the worker cycle identity,
        # not elapsed wall-clock reads of the same unchanged drain timestamp.
        # Directly-driven tests/diagnostics have no cycle marker, so their
        # explicit ``checked_at`` value remains the sample identity.
        failure_cycle = self.last_cycle_started_at or checked_at
        if failure_cycle != self._market_data_queue_last_failure_cycle_started_at:
            self._market_data_queue_failure_streak += 1
            self._market_data_queue_last_failure_cycle_started_at = failure_cycle
        return (
            self._market_data_queue_failure_streak
            < self._QUEUE_DRAIN_FAILURE_CONFIRMATIONS
        )

    def _execute_reconciliation_commands(
        self, result: AccountReconciliationResult
    ) -> List[TradeCardState]:
        """Run reducer commands through the runtime's shared guarded workflow."""
        assert self.runtime is not None
        changed: List[TradeCardState] = []
        terminal_statuses = {
            ExecutionOrderStatus.FILLED,
            ExecutionOrderStatus.CANCELLED,
            ExecutionOrderStatus.REJECTED,
            ExecutionOrderStatus.EXPIRED,
            ExecutionOrderStatus.CANCELLED_LOCALLY,
            ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED,
        }

        def persist(card: TradeCardState) -> None:
            repo.update_trade_card(
                self._db_engine, card, expected_version=card.version
            )
            changed.append(card)

        for command in result.plan.commands:
            card = repo.get_trade_card(
                self._db_engine,
                command.environment,
                command.account_no,
                command.symbol,
            )
            if card is None:
                message = (
                    f"Reconciliation command {command.command_type.value} has no "
                    f"durable card for {command.environment}/{command.account_no}/"
                    f"{command.symbol}"
                )
                logger.error(message)
                self.alert.emit(f"CRITICAL: {message}")
                continue

            if command.command_type == ReconciliationCommandType.CANCEL_KNOWN_ORDER:
                try:
                    self.runtime.reconciliation_cancel_order(
                        card, command.client_order_id
                    )
                except GuardedCancellationRejectedError as exc:
                    # request_cancel_with_lifecycle already cleared and
                    # persisted the failed caller-owned cancel identity.
                    logger.warning(
                        "Reconciliation cancel explicitly rejected for %s: %s",
                        command.client_order_id,
                        exc,
                    )
                    changed.append(card)
                    continue
                except Exception as exc:
                    # The workflow persists command identity before reaching
                    # the gateway. Keep it for safe replay/reconciliation.
                    logger.exception(
                        "Reconciliation cancel failed for %s",
                        command.client_order_id,
                    )
                    self.alert.emit(
                        f"CRITICAL: reconciliation cancel failed for "
                        f"{command.symbol}: {exc}"
                    )
                    changed.append(card)
                    continue

                after = fetch_execution_order(
                    self._db_engine, command.client_order_id
                )
                if after is not None and after.status in terminal_statuses:
                    if after.side == OrderSide.BUY:
                        card.entry_client_order_id = ""
                        card.entry_cancel_command_id = ""
                        card.entry_cancel_in_flight = False
                        card.entry_cancel_reason = ""
                        card.entry_pending_attempt_number = 0
                    else:
                        card.exit_client_order_id = ""
                        card.exit_cancel_command_id = ""
                        card.exit_cancel_in_flight = False
                        card.exit_cancel_requested_at = None
                        card.exit_pending_attempt_number = 0
                        card.reserved_sell_quantity = 0
                    persist(card)
                else:
                    # Ambiguous cancellation is swallowed by the lifecycle
                    # helper after it persists the in-flight marker and ID.
                    changed.append(card)
                continue

            if command.command_type == ReconciliationCommandType.EMERGENCY_SELL_ALL:
                try:
                    self.runtime.reconciliation_emergency_sell(
                        card, command.quantity
                    )
                except (
                    GuardedSubmissionAmbiguousError,
                    AmbiguousPostBrokerPersistenceError,
                    DuplicateCommandError,
                ) as exc:
                    card.exit_submission_unresolved = True
                    card.next_exit_retry_at = None
                    card.last_exit_error = f"UNRESOLVED: {exc}"[:500]
                    persist(card)
                    continue
                except GuardedSubmissionRejectedError as exc:
                    card.exit_attempt_count += 1
                    card.exit_client_order_id = ""
                    card.exit_pending_attempt_number = 0
                    card.exit_submission_unresolved = False
                    card.last_exit_error = str(exc)[:500]
                    card.next_exit_retry_at = datetime.now(timezone.utc) + timedelta(
                        seconds=execution_config.EXIT_RETRY_COOLDOWN_SECONDS
                    )
                    persist(card)
                    continue
                except Exception as exc:
                    # A pre-broker fence (notably DISCOVERED_UNOWNED) keeps
                    # the already-persisted stable ID. The gateway remains
                    # the final authority and made no broker mutation.
                    logger.exception(
                        "Reconciliation emergency SELL failed for %s",
                        command.symbol,
                    )
                    self.alert.emit(
                        f"CRITICAL: reconciliation emergency SELL failed for "
                        f"{command.symbol}: {exc}"
                    )
                    changed.append(card)
                    continue
                card.reserved_sell_quantity = command.quantity
                card.exit_submission_unresolved = False
                card.next_exit_retry_at = None
                card.last_exit_error = ""
                persist(card)
        return changed

    def _handle_reconciliation_result(
        self, result: AccountReconciliationResult
    ) -> None:
        current_incidents = set()
        for alert in result.plan.alerts:
            logger.warning(
                "Account reconciliation %s for %s/%s/%s: %s",
                alert.code,
                result.snapshot.environment,
                result.snapshot.account_no,
                alert.symbol or "-",
                alert.message,
            )
            if alert.severity != ReconciliationAlertSeverity.CRITICAL:
                continue
            base = (
                result.snapshot.environment,
                result.snapshot.account_no,
                alert.code,
                alert.symbol,
                alert.broker_order_id or alert.client_order_id,
            )
            if base in current_incidents:
                continue
            current_incidents.add(base)
            if base in self._active_reconciliation_incidents:
                continue
            generation = self._reconciliation_incident_generations.get(base, 0) + 1
            self._reconciliation_incident_generations[base] = generation
            message = (
                f"CRITICAL: account reconciliation {alert.code} for "
                f"{alert.symbol or result.snapshot.account_no} "
                f"(incident {generation}): {alert.message}"
            )
            self.alert.emit(message)
            normalized_code = str(alert.code or "").upper()
            alert_type = (
                CriticalAlertType.UNKNOWN_SUBMISSION_STATE
                if "UNKNOWN_SUBMISSION" in normalized_code
                else CriticalAlertType.DISCOVERED_EXTERNAL_ORDER
                if "EXTERNAL" in normalized_code
                else CriticalAlertType.ACCOUNT_RECONCILIATION_FAILED
            )
            self._raise_external_alert(
                alert_type,
                ":".join(str(item or "-") for item in base),
                message,
            )
        account_prefix = (
            result.snapshot.environment,
            result.snapshot.account_no,
        )
        self._active_reconciliation_incidents = {
            key
            for key in self._active_reconciliation_incidents
            if key[:2] != account_prefix
        } | current_incidents

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

    def _sync_orb_plans(
        self,
        cards: List[TradeCardState],
        *,
        ambiguous_orb_keys: Optional[set[tuple[str, str]]] = None,
    ) -> List[TradeCardState]:
        """Review finding P0-2: without this, a card dragged to Buy Today
        never progresses past ORB_FORMING -- nothing else recomputes
        entry_trigger/planned_quantity/etc. Reads whatever the legacy
        execution queue's own (unchanged, still independently running)
        refresh cycle most recently computed rather than triggering a
        second, competing ORB recalculation.
        """
        changed: List[TradeCardState] = []

        def block_unverified_plan(card: TradeCardState, reason: str) -> None:
            before = (
                card.entry_runtime_status,
                card.entry_block_reason,
                card.entry_trigger,
                card.entry_orb_high,
                card.entry_orb_low,
                card.stop_adr,
                card.planned_quantity,
                card.target_position_quantity,
                card.selected_orb_window,
            )
            card.entry_runtime_status = EntryRuntimeStatus.DATA_UNAVAILABLE
            card.entry_block_reason = reason
            card.entry_trigger = None
            card.entry_orb_high = None
            card.entry_orb_low = None
            card.stop_adr = None
            card.planned_quantity = 0
            card.target_position_quantity = 0
            card.selected_orb_window = None
            after = (
                card.entry_runtime_status,
                card.entry_block_reason,
                card.entry_trigger,
                card.entry_orb_high,
                card.entry_orb_low,
                card.stop_adr,
                card.planned_quantity,
                card.target_position_quantity,
                card.selected_orb_window,
            )
            if before != after:
                changed.append(card)

        ambiguous = (
            _ambiguous_buy_today_orb_keys(cards)
            if ambiguous_orb_keys is None
            else set(ambiguous_orb_keys)
        )
        for card in cards:
            if card.board_status not in _ORB_SYNCED_STATUSES:
                continue
            symbol_key = (
                str(card.environment or "").strip().upper(),
                str(card.symbol or "").strip().upper(),
            )
            if symbol_key in ambiguous:
                reason = (
                    "ORB execution blocked: the same symbol is active in "
                    "multiple accounts, but the ORB queue is not account-scoped"
                )
                if (
                    card.entry_runtime_status != EntryRuntimeStatus.RISK_INVALID
                    or card.entry_block_reason != reason
                ):
                    card.entry_runtime_status = EntryRuntimeStatus.RISK_INVALID
                    card.entry_block_reason = reason
                    changed.append(card)
                continue
            if self._execution_queue_item_lookup is None:
                block_unverified_plan(
                    card,
                    "Current-session ORB plan is unavailable",
                )
                continue
            try:
                item = self._execution_queue_item_lookup(card.symbol, card.environment)
            except Exception:
                logger.exception("execution_queue_item_lookup failed for %s", card.symbol)
                block_unverified_plan(
                    card,
                    "Current-session ORB plan lookup failed",
                )
                continue
            if item is None:
                block_unverified_plan(
                    card,
                    "Current-session ORB plan is unavailable",
                )
                continue
            before = (
                card.board_status,
                card.board_status_updated_at,
                card.name,
                card.breakout_price,
                card.entry_runtime_status,
                card.entry_trigger,
                card.entry_orb_high,
                card.entry_orb_low,
                card.stop_adr,
                card.risk_percent,
                card.planned_quantity,
                card.target_position_quantity,
                card.entry_block_reason,
                card.selected_orb_window,
                card.buy_today_note,
            )
            self._orb_evaluator.update_card(card, item)
            after = (
                card.board_status,
                card.board_status_updated_at,
                card.name,
                card.breakout_price,
                card.entry_runtime_status,
                card.entry_trigger,
                card.entry_orb_high,
                card.entry_orb_low,
                card.stop_adr,
                card.risk_percent,
                card.planned_quantity,
                card.target_position_quantity,
                card.entry_block_reason,
                card.selected_orb_window,
                card.buy_today_note,
            )
            # Live price/timestamp are observation data, not a durable state
            # transition.  Keep them current in memory, and include their
            # latest values whenever a plan/status field really changes, but
            # never write a full TradeCard JSON row for price-only movement.
            if before != after:
                changed.append(card)
        return changed

    def _prepare_crossed_orb_entry_plan(self, card, quote) -> bool:
        """Select any crossed current-session ORB before immediate entry."""

        if self._execution_queue_item_lookup is None:
            return False
        try:
            item = self._execution_queue_item_lookup(card.symbol, card.environment)
        except Exception:
            logger.exception(
                "execution_queue_item_lookup failed during live crossing for %s",
                card.symbol,
            )
            return False
        if item is None:
            return False
        if queue_has_execution_order_lock(item):
            reason = "ORB execution blocked: the queue has an active order lock"
            changed = (
                card.entry_runtime_status != EntryRuntimeStatus.ORDER_PENDING
                or card.entry_block_reason != reason
            )
            card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
            card.entry_block_reason = reason
            return changed
        return self._orb_evaluator.select_crossed_candidate(
            card,
            item,
            last_price=quote.last_price,
        )

    def _sync_quote_subscriptions(self, cards: List[TradeCardState]) -> None:
        market_data = self.runtime.market_data
        adopt_keys = getattr(market_data, "adopt_canonical_symbol_keys", None)
        if callable(adopt_keys):
            canonical_keys: Dict[str, str] = {}
            conflicting_symbols: set[str] = set()
            for card in cards:
                if card.board_status not in _QUOTE_SUBSCRIBED_STATUSES:
                    continue
                key = str(getattr(card, "kis_ws_symbol_key", "") or "").strip()
                if not key:
                    continue
                prior = canonical_keys.get(card.symbol)
                if prior is not None and prior != key:
                    conflicting_symbols.add(card.symbol)
                    canonical_keys.pop(card.symbol, None)
                    continue
                if card.symbol not in conflicting_symbols:
                    canonical_keys[card.symbol] = key
            try:
                adopt_keys(canonical_keys)
            except Exception:
                # A file-system/configuration problem must not tear down
                # already healthy feeds for other symbols.
                logger.exception("Could not adopt canonical KIS WebSocket symbol keys")
        configure = getattr(market_data, "configure_desired_channels", None)
        if callable(configure):
            trade_priorities: Dict[str, int] = {}
            quote_priorities: Dict[str, int] = {}
            for card in cards:
                # Planning/archive cards do not participate in execution and
                # must not consume KIS WebSocket resolution or subscription
                # capacity.  Passing them as DISPLAY_ONLY still makes the
                # coordinator resolve both channels before it applies its
                # capacity limits.
                if card.board_status not in _QUOTE_SUBSCRIBED_STATUSES:
                    continue
                symbol = card.symbol
                if card.board_status in {BoardStatus.SELL_ALL, BoardStatus.PARTIAL_SELL} or card.exit_all_required:
                    trade_priority = SubscriptionPriority.CRITICAL_EXIT
                elif card.board_status == BoardStatus.OPEN_POSITION:
                    trade_priority = SubscriptionPriority.OPEN_POSITION
                elif card.board_status == BoardStatus.ENTRY_PENDING:
                    trade_priority = SubscriptionPriority.ENTRY_PENDING
                elif card.board_status == BoardStatus.BUY_TODAY:
                    trade_priority = (
                        _buy_today_subscription_priority(card)
                        if self._card_in_execution_scope(card)
                        else SubscriptionPriority.DISPLAY_ONLY
                    )
                else:
                    trade_priority = SubscriptionPriority.DISPLAY_ONLY

                if card.board_status in {
                    BoardStatus.SELL_ALL,
                    BoardStatus.PARTIAL_SELL,
                    BoardStatus.OPEN_POSITION,
                }:
                    quote_priority = SubscriptionPriority.CRITICAL_EXIT
                elif card.board_status == BoardStatus.ENTRY_PENDING:
                    quote_priority = SubscriptionPriority.ENTRY_PENDING
                elif card.board_status == BoardStatus.BUY_TODAY:
                    quote_priority = (
                        _buy_today_subscription_priority(card)
                        if self._card_in_execution_scope(card)
                        else SubscriptionPriority.DISPLAY_ONLY
                    )
                else:
                    quote_priority = SubscriptionPriority.DISPLAY_ONLY
                trade_priorities[symbol] = min(
                    int(trade_priority), trade_priorities.get(symbol, 999)
                )
                quote_priorities[symbol] = min(
                    int(quote_priority), quote_priorities.get(symbol, 999)
                )
            configure(
                trade_priorities=trade_priorities,
                quote_priorities=quote_priorities,
            )
            return
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

    def _sync_market_stop_rules(
        self,
        cards: List[TradeCardState],
        *,
        apply_pending_changes: bool = False,
    ) -> bool:
        replace_rules = getattr(self.runtime.market_data, "replace_stop_rules", None)
        if not callable(replace_rules):
            return False
        by_symbol: Dict[str, List[StopRule]] = {}
        active_keys: set[str] = set()
        for card in cards:
            stop_type = card.stop_type
            stop_price = card.active_stop_price
            stop_quantity = card.stop_quantity
            if apply_pending_changes and card.pending_stop_command_id:
                stop_type = card.pending_stop_type
                stop_price = card.pending_stop_price
                stop_quantity = card.pending_stop_quantity
            if (
                card.board_status not in {BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL}
                or stop_price is None
                or stop_price <= 0
            ):
                continue
            active_keys.add(card.card_key)
            signature = (
                stop_type.value if stop_type is not None else "",
                float(stop_price),
                int(stop_quantity),
            )
            if self._market_stop_signatures.get(card.card_key) != signature:
                self._market_stop_signatures[card.card_key] = signature
                self._market_stop_generations[card.card_key] = (
                    self._market_stop_generations.get(card.card_key, 0) + 1
                )
            by_symbol.setdefault(card.symbol, []).append(
                StopRule(
                    card_key=card.card_key,
                    price=float(stop_price),
                    version=str(self._market_stop_generations[card.card_key]),
                )
            )
        for card_key in set(self._market_stop_signatures) - active_keys:
            self._market_stop_signatures.pop(card_key, None)

        rotated = False
        symbols = set(by_symbol) | self._market_stop_symbols
        subscribed = getattr(self.runtime.market_data, "subscribed_symbols", lambda: [])
        symbols.update(subscribed())
        for symbol in symbols:
            if replace_rules(symbol, by_symbol.get(symbol, [])) is not None:
                rotated = True
        self._market_stop_symbols = set(by_symbol)
        return rotated

    def _acknowledge_pending_stop_changes(
        self, cards: List[TradeCardState]
    ) -> List[TradeCardState]:
        """Promote a pending stop only after its exact feed rule is live."""

        changed: List[TradeCardState] = []
        for card in cards:
            if not card.pending_stop_command_id or card.pending_stop_price is None:
                continue
            signature = (
                card.pending_stop_type.value
                if card.pending_stop_type is not None
                else "",
                float(card.pending_stop_price),
                int(card.pending_stop_quantity),
            )
            if self._market_stop_signatures.get(card.card_key) != signature:
                continue
            if card.acknowledge_pending_stop_change():
                changed.append(card)
        return changed

    def _latch_pending_stop_breaches(
        self,
        quote,
        cards: List[TradeCardState],
        candidates: set[tuple[str, str, str]],
    ) -> None:
        latch = getattr(self.runtime.market_data, "latch_stop_breach", None)
        if not callable(latch):
            return
        for card in cards:
            if not card.exit_all_required or card.pending_stop_price is None:
                continue
            version = str(self._market_stop_generations.get(card.card_key, 0))
            if version == "0":
                continue
            latch(
                card.symbol,
                card.card_key,
                version,
                quote,
                float(card.pending_stop_price),
            )
            candidates.add((card.symbol, card.card_key, version))

    @staticmethod
    def _collect_market_breach_ack_candidates(
        quote,
        cards: List[TradeCardState],
        candidates: set[tuple[str, str, str]],
    ) -> None:
        if not quote.breached_stop_versions:
            return
        cards_by_key = {card.card_key: card for card in cards}
        for card_key, version in quote.breached_stop_versions:
            card = cards_by_key.get(card_key)
            if card is not None and card.exit_all_required:
                candidates.add((card.symbol, card_key, version))

    def _acknowledge_market_breach_candidates(
        self,
        candidates: set[tuple[str, str, str]],
        durable_card_keys: set[str],
    ) -> None:
        acknowledge = getattr(
            self.runtime.market_data, "acknowledge_stop_breach", None
        )
        if not callable(acknowledge):
            return
        for symbol, card_key, version in sorted(candidates):
            if card_key in durable_card_keys:
                acknowledge(symbol, card_key, version)

    def _persist_changed(
        self, cards: List[TradeCardState]
    ) -> List[TradeCardState]:
        persisted: List[TradeCardState] = []
        for card in cards:
            try:
                with self._stop_change_coordinator.lock_cards([card.card_key]):
                    try:
                        repo.update_trade_card(
                            self._db_engine, card, expected_version=card.version
                        )
                    except repo.TradeCardNotFoundError:
                        # A newly *discovered* card -- e.g. a manually-purchased
                        # broker position with no prior local card
                        # (PositionManager.discover_manual_position, surfaced
                        # via startup/full-account reconciliation) -- has never
                        # been persisted. Create it instead of dropping it.
                        repo.create_trade_card(self._db_engine, card)
                    self._stop_change_coordinator.reconcile_durable(card)
                    persisted.append(card)
            except Exception:
                # A stale version (another device changed this card
                # concurrently) or a transient DB error must not stop the
                # rest of this cycle's cards from being persisted -- the
                # next cycle re-loads authoritative state and simply tries
                # again.
                logger.exception("BuyboardRuntimeWorker failed to persist %s", card.symbol)
        return persisted
