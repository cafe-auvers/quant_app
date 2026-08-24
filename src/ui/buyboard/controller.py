"""Thin Qt adapter for the shared execution workflow service.

Kanban gestures are typed durable user intent.  The UI never calls a broker or
execution gateway directly; the authoritative PC runtime consumes those intents
later under its lease, reconciliation, market-data, kill-switch, and execution
safety fences.  This distinction is intentional: a laptop must be able to plan
Buy Today / exits / stops before market open or while it is pull-only.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import datetime as dt
import logging
from queue import Empty, Queue
import time
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal
from sqlalchemy.exc import SQLAlchemyError

from src.core.board_workflow import (
    AnyBoardCommand,
    BoardActionContext,
    BoardCardProjection,
    BoardProjectionContext,
)
from src.core import execution_config
from src.core.execution_config import is_buyboard_engine_enabled
from src.core.runtime_readiness import RuntimeDeviceState
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import execution_workflow_service
from src.services.execution_workflow_service import BoardCommandRejectedError
from src.services.trade_card_repository import (
    TradeCardNotFoundError,
    TradeCardVersionConflictError,
)
from src.utils.config import get_env_value
from src.utils.market_calendar import (
    current_or_next_nyse_session_date,
    is_regular_session_open,
)

from .card import board_interaction_fingerprint, card_drag_payload

logger = logging.getLogger(__name__)

# Compatibility name used by existing extensions/tests.
CommandRejectedError = BoardCommandRejectedError


@dataclass(frozen=True)
class BuyboardProjectionRequest:
    """Immutable inputs copied on the GUI thread for a projection read."""

    engine: object
    context: BoardProjectionContext
    buylist_manager: object
    watchlist: object
    default_account_no: str
    account_snapshots: dict
    account_snapshot_fetched_at: dict
    runtime_running: bool
    generation: int
    revision_only: bool = False
    expected_revision: object = None


class BuyboardProjectionWorker(QThread):
    """Build the canonical board projection without blocking Qt."""

    completed = pyqtSignal(object, str, int)

    def __init__(self, request: BuyboardProjectionRequest) -> None:
        super().__init__()
        self.request = request
        self.resolved_revision = None

    def run(self) -> None:
        started_at = time.perf_counter()
        request = self.request
        try:
            from .columns import BOARD_COLUMN_ORDER
            from src.services.trade_card_bootstrap import (
                bootstrap_trade_cards_from_current_state,
            )

            current_revision = None
            if request.revision_only:
                current_revision = (
                    execution_workflow_service.get_board_projection_revision(
                        request.engine, environment="PROD"
                    ),
                    request.context,
                )
                self.resolved_revision = current_revision
                if current_revision == request.expected_revision:
                    self.completed.emit(None, "", request.generation)
                    return

            kwargs = {}
            prefetched_cards = None
            if not request.runtime_running:
                kwargs = {
                    "account_snapshots": request.account_snapshots,
                    "account_snapshot_fetched_at": (
                        request.account_snapshot_fetched_at
                    ),
                }
            # A periodic revision refresh responds to canonical database
            # changes and must not re-import unchanged local planning mirrors.
            # Explicit/local planning actions already request a full refresh,
            # which retains the compatibility bootstrap below.  Skipping it
            # here removes one duplicate full TradeCard read per minute.
            if not request.revision_only:
                try:
                    bootstrap_result = bootstrap_trade_cards_from_current_state(
                        request.engine,
                        buylist_manager=request.buylist_manager,
                        watchlist=request.watchlist,
                        default_account_no=request.default_account_no,
                        **kwargs,
                    )
                    prefetched_cards = getattr(
                        bootstrap_result, "canonical_cards", None
                    )
                except SQLAlchemyError:
                    logger.debug(
                        "Buy Board bootstrap paused because the canonical "
                        "database is unavailable"
                    )
                except Exception:
                    logger.exception("Buy Board bootstrap refresh failed")

            projection_kwargs = {
                "environment": "PROD",
                "context": request.context,
                # Watchlist is intentionally hidden from the Kanban columns,
                # but remains in the shared projection snapshot so lightweight
                # chart/sidebar actions can address the exact versioned card.
                "board_statuses": (*BOARD_COLUMN_ORDER, BoardStatus.WATCHLIST),
            }
            if prefetched_cards is not None:
                projection_kwargs["prefetched_cards"] = prefetched_cards
            projections = execution_workflow_service.list_board_projections(
                request.engine,
                **projection_kwargs,
            )
            if request.revision_only:
                # No compatibility bootstrap ran, so the pre-read token is
                # the exact revision that triggered this projection.  Avoid a
                # second four-table aggregate query after downloading it.
                self.resolved_revision = current_revision
            self.completed.emit(projections, "", request.generation)
        except SQLAlchemyError as exc:
            logger.warning(
                "Buy Board canonical database is unavailable; showing the "
                "read-only recovery snapshot"
            )
            self.completed.emit([], str(exc), request.generation)
        except Exception as exc:
            logger.exception("Buy Board projection refresh failed")
            self.completed.emit([], str(exc), request.generation)
        finally:
            logger.debug(
                "Buy Board projection generation=%d completed in %.1f ms",
                request.generation,
                (time.perf_counter() - started_at) * 1000.0,
            )


@dataclass(frozen=True)
class BuyboardCommandRequest:
    """Everything a board command needs after leaving the Qt thread."""

    engine: object
    command: AnyBoardCommand
    action_context: BoardActionContext
    projection_context: BoardProjectionContext
    interaction_fingerprint: str = ""
    enqueued_at: float = 0.0
    route_via_operator_queue: bool = False
    requester_role: object = None

    @property
    def card_key(self) -> str:
        command = self.command
        return f"{command.environment}:{command.account_no}:{command.symbol}"


@dataclass(frozen=True)
class BuyboardCommandResult:
    """Thread-safe command outcome consumed by the Qt-thread completion slot."""

    request: BuyboardCommandRequest
    succeeded: bool
    error_kind: str = ""
    message: str = ""
    elapsed_ms: float = 0.0
    queue_wait_ms: float = 0.0
    operator_command_queued: bool = False


class BoardCommandWorker(QThread):
    """Execute all Kanban commands serially, away from the Qt event loop."""

    completed = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._requests: Queue[Optional[BuyboardCommandRequest]] = Queue()

    def enqueue(self, request: BuyboardCommandRequest) -> None:
        self._requests.put(request)

    def request_stop(self) -> None:
        self._requests.put(None)

    @staticmethod
    def _execute(request: BuyboardCommandRequest) -> BuyboardCommandResult:
        started_at = time.perf_counter()
        queue_wait_ms = max(0.0, (started_at - request.enqueued_at) * 1000.0)
        current_command = request.command
        try:
            if request.route_via_operator_queue:
                from src.services.operator_command_service import (
                    enqueue_board_operator_command,
                )

                inserted = enqueue_board_operator_command(
                    request.engine,
                    request.requester_role,
                    current_command,
                )
                return BuyboardCommandResult(
                    request=request,
                    succeeded=True,
                    operator_command_queued=True,
                    message=(
                        "Live command queued for the Execution Owner."
                        if inserted.created
                        else "This live command was already queued."
                    ),
                    elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                    queue_wait_ms=queue_wait_ms,
                )
            for attempt in range(2):
                try:
                    execution_workflow_service.request_board_action(
                        request.engine,
                        current_command,
                        context=request.action_context,
                        claim_kanban_ownership=_claims_kanban_ownership(
                            current_command
                        ),
                    )
                    return BuyboardCommandResult(
                        request=request,
                        succeeded=True,
                        elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                        queue_wait_ms=queue_wait_ms,
                    )
                except TradeCardVersionConflictError:
                    if attempt == 0 and request.interaction_fingerprint:
                        try:
                            projection = (
                                execution_workflow_service.get_board_projection(
                                    request.engine,
                                    environment=current_command.environment,
                                    account_no=current_command.account_no,
                                    symbol=current_command.symbol,
                                    context=request.projection_context,
                                )
                            )
                        except Exception:
                            projection = None
                        if (
                            projection is not None
                            and board_interaction_fingerprint(projection)
                            == request.interaction_fingerprint
                        ):
                            fresh = card_drag_payload(projection)
                            current_command = replace(
                                current_command,
                                expected_card_version=fresh["version"],
                                expected_readiness_generation=fresh[
                                    "readiness_generation"
                                ],
                                expected_ownership_version=fresh[
                                    "ownership_version"
                                ],
                                expected_execution_owner=fresh[
                                    "execution_owner"
                                ],
                                expected_strategy_instance_id=fresh[
                                    "strategy_instance_id"
                                ],
                            )
                            continue
                    return BuyboardCommandResult(
                        request=request,
                        succeeded=False,
                        error_kind="version_conflict",
                        message=(
                            "This card changed since it was loaded. The board has "
                            "been refreshed; please retry."
                        ),
                        elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                        queue_wait_ms=queue_wait_ms,
                    )
                except TradeCardNotFoundError:
                    return BuyboardCommandResult(
                        request=request,
                        succeeded=False,
                        error_kind="not_found",
                        message="This card no longer exists.",
                        elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                        queue_wait_ms=queue_wait_ms,
                    )
                except BoardCommandRejectedError as exc:
                    return BuyboardCommandResult(
                        request=request,
                        succeeded=False,
                        error_kind="rejected",
                        message=str(exc),
                        elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                        queue_wait_ms=queue_wait_ms,
                    )
        except Exception as exc:
            from src.services.operator_commands import (
                OperatorCommandError,
                OperatorControlNotOwnedError,
            )

            if isinstance(exc, (OperatorControlNotOwnedError, OperatorCommandError)):
                return BuyboardCommandResult(
                    request=request,
                    succeeded=False,
                    error_kind="operator_control",
                    message=str(exc),
                    elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                    queue_wait_ms=queue_wait_ms,
                )
            logger.exception(
                "Buy Board command %s failed unexpectedly",
                type(request.command).__name__,
            )
            return BuyboardCommandResult(
                request=request,
                succeeded=False,
                error_kind="unexpected",
                message=f"Could not save the board change: {exc}",
                elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                queue_wait_ms=queue_wait_ms,
            )

        raise AssertionError("unreachable command execution state")

    def run(self) -> None:
        while True:
            try:
                request = self._requests.get(timeout=0.1)
            except Empty:
                if self.isInterruptionRequested():
                    break
                continue
            if request is None:
                break
            result = self._execute(request)
            logger.info(
                "Buy Board command=%s card=%s success=%s queue=%.1f ms "
                "execution=%.1f ms",
                type(request.command).__name__,
                request.card_key,
                result.succeeded,
                result.queue_wait_ms,
                result.elapsed_ms,
            )
            self.completed.emit(result)


def apply_board_command(engine, command: AnyBoardCommand, *, context=None):
    """Compatibility wrapper around the one authoritative workflow entry."""
    return execution_workflow_service.request_board_action(
        engine, command, context=context
    ).card


def _worker_for(main_window):
    return main_window.__dict__.get("_buyboard_runtime_worker")


def _projection_context(main_window) -> BoardProjectionContext:
    worker = _worker_for(main_window)
    global_restrictions = []
    if not is_buyboard_engine_enabled():
        global_restrictions.append("Execution engine disabled")
    if worker is None:
        global_restrictions.append("Runtime worker unavailable")
        return BoardProjectionContext(global_restrictions=tuple(global_restrictions))

    state = getattr(worker, "device_state", RuntimeDeviceState.STARTING)
    if state != RuntimeDeviceState.ACTIVE:
        global_restrictions.append(f"Device state is {state.value}")
    if not bool(getattr(worker, "_database_writable", False)):
        global_restrictions.append(
            "Kanban operational store is not confirmed writable"
        )

    errors = dict(getattr(worker, "startup_reconciliation_errors", {}) or {})
    reconciled = set(getattr(worker, "startup_reconciled_accounts", set()) or set())
    blocked = set(errors)
    account_restrictions = []
    for account, reason in errors.items():
        account_restrictions.append((str(account), (str(reason),)))
    for card in list(getattr(worker, "_cached_cards", ()) or ()):
        if card.account_no and card.account_no not in reconciled:
            blocked.add(card.account_no)

    return BoardProjectionContext(
        readiness_generation=int(getattr(worker, "readiness_generation", 0) or 0),
        reconciliation_blocked_accounts=tuple(sorted(blocked)),
        global_restrictions=tuple(global_restrictions),
        account_restrictions=tuple(account_restrictions),
    )


def _safe_regular_session_open() -> bool | None:
    try:
        return bool(is_regular_session_open())
    except Exception:
        return None


def _action_context(main_window, command: AnyBoardCommand) -> BoardActionContext:
    checker = getattr(main_window, "_has_cached_local_operator_control", None)
    try:
        local_operator_control = bool(checker()) if callable(checker) else False
    except Exception:
        local_operator_control = False
    worker = _worker_for(main_window)
    if worker is None:
        return BoardActionContext(
            enforce_runtime_fences=True,
            engine_enabled=is_buyboard_engine_enabled(),
            action_ready=False,
            device_active=False,
            regular_session_open=_safe_regular_session_open(),
            session_date=current_or_next_nyse_session_date(),
            local_operator_control=local_operator_control,
            restriction_reasons=("Runtime worker unavailable",),
        )

    from src.core.board_workflow import ActivateForToday, CancelEntry

    action = "PROTECTIVE_EXIT"
    if isinstance(command, ActivateForToday):
        action = "NEW_ENTRY"
    elif isinstance(command, CancelEntry):
        action = "KNOWN_CANCEL"
    try:
        ready = bool(worker.account_action_ready(command.account_no, command.symbol, action))
    except Exception:
        ready = False
    errors = getattr(worker, "startup_reconciliation_errors", {}) or {}
    reasons = []
    if command.account_no in errors:
        reasons.append(str(errors[command.account_no]))
    if not ready:
        reasons.append(f"{action.lower().replace('_', ' ')} readiness is incomplete")
    regular_session_open = None
    runtime = getattr(worker, "runtime", None)
    trading_engine = getattr(runtime, "trading_engine", None)
    market_open = getattr(trading_engine, "_market_is_open", None)
    if callable(market_open):
        try:
            regular_session_open = bool(market_open())
        except Exception:
            regular_session_open = None
    if regular_session_open is None:
        regular_session_open = _safe_regular_session_open()
    return BoardActionContext(
        enforce_runtime_fences=True,
        engine_enabled=is_buyboard_engine_enabled(),
        readiness_generation=int(getattr(worker, "readiness_generation", 0) or 0),
        reconciliation_in_progress=bool(
            command.account_no
            in set(getattr(worker, "reconciliation_accounts_in_progress", set()) or set())
        ),
        action_ready=ready,
        device_active=getattr(worker, "device_state", None) == RuntimeDeviceState.ACTIVE,
        regular_session_open=regular_session_open,
        session_date=current_or_next_nyse_session_date(),
        local_operator_control=local_operator_control,
        restriction_reasons=tuple(dict.fromkeys(reasons)),
    )


def _claims_kanban_ownership(command: AnyBoardCommand) -> bool:
    from src.core.board_workflow import (
        ActivateForToday,
        CancelPartialSell,
        CancelQueuedSellAll,
        RequestPartialSell,
        RequestSellAll,
        SetBreakevenStop,
        SetManualStop,
        SetOrbStop,
    )

    return isinstance(
        command,
        (
            ActivateForToday,
            CancelPartialSell,
            RequestPartialSell,
            RequestSellAll,
            CancelQueuedSellAll,
            SetOrbStop,
            SetBreakevenStop,
            SetManualStop,
        ),
    )


def _route_live_command_via_operator_queue(main_window) -> bool:
    """Use direct execution only for a verified same-device topology."""

    sync_mode = getattr(
        main_window, "_operator_executor_sync_mode", lambda: "unknown"
    )()
    local_operator = getattr(
        main_window, "_has_cached_local_operator_control", lambda: False
    )()
    return not (sync_mode == "same" and bool(local_operator))


class BuyboardMixin:
    """Build the board and route all gestures through the workflow service."""

    _BUYBOARD_PROJECTION_REFRESH_MS = int(
        execution_config.COORDINATION_BOARD_PROJECTION_SECONDS * 1000
    )
    _BUYBOARD_LIVE_METRIC_REFRESH_MS = 750
    _BUYBOARD_ORB_DATA_REFRESH_MS = 60_000
    _BUYBOARD_BROKER_TRUTH_REFRESH_MS = 30_000
    _BUYBOARD_BROKER_SNAPSHOT_MAX_AGE_SECONDS = 120.0
    _BUYBOARD_SLOW_RENDER_WARNING_MS = 1000.0
    _BUYBOARD_SLOW_RENDER_WARNING_INTERVAL_SECONDS = 60.0

    def _buyboard_engine(self):
        resolver = getattr(self, "_execution_state_engine", None)
        if callable(resolver):
            return resolver()
        engine = self.__dict__.get("operational_db_engine")
        return engine if engine is not None else self.__dict__.get("pc_db_engine")

    def _build_buyboard_tab(self) -> None:
        from PyQt5.QtCore import QTimer

        from .board import build_buyboard_widget

        build_buyboard_widget(self)
        self.refresh_buyboard()

        # The window is intentionally rendered before the asynchronous PC-DB
        # probe finishes. The first refresh can therefore be empty even though
        # canonical state becomes available moments later. Keep one lightweight
        # projection timer so DB readiness, cross-device changes, legacy
        # Watchlist/Buylist edits, and fresh cached KIS holdings become visible
        # without a manual refresh/restart. Runtime execution changes still
        # trigger immediate board_changed refreshes.
        timer = QTimer(self)
        timer.setInterval(self._BUYBOARD_PROJECTION_REFRESH_MS)
        timer.timeout.connect(lambda: self.refresh_buyboard(revision_only=True))
        timer.start()
        self._buyboard_projection_timer = timer

        # Quote-derived values are intentionally repainted on a faster,
        # independent cadence. This timer performs no DB/network query and
        # never destroys card widgets, so live Current/P&L/To Breakout values
        # do not reintroduce the drag lag caused by projection rebuilds.
        live_timer = QTimer(self)
        live_timer.setInterval(self._BUYBOARD_LIVE_METRIC_REFRESH_MS)
        live_timer.timeout.connect(self._refresh_buyboard_live_metrics)
        live_timer.start()
        self._buyboard_live_metric_timer = live_timer

        # Kanban entry planning must not depend on the operator enabling the
        # retired Buy Dashboard monitor or the optional watchlist auto-refresh
        # checkbox. This timer only refreshes market observations/ORB plans;
        # broker submissions remain exclusively inside the guarded runtime.
        orb_data_timer = QTimer(self)
        orb_data_timer.setInterval(self._BUYBOARD_ORB_DATA_REFRESH_MS)
        orb_data_timer.timeout.connect(self._refresh_buyboard_orb_data)
        orb_data_timer.start()
        self._buyboard_orb_data_timer = orb_data_timer

        # Continue polling KIS account truth whenever the execution runtime is
        # absent, so filled/sold/manual positions never depend on history DBs.
        broker_timer = QTimer(self)
        broker_timer.setInterval(self._BUYBOARD_BROKER_TRUTH_REFRESH_MS)
        broker_timer.timeout.connect(self._refresh_buyboard_broker_truth)
        broker_timer.start()
        self._buyboard_broker_truth_timer = broker_timer
        QTimer.singleShot(5_000, self._refresh_buyboard_broker_truth)

    def _buyboard_orb_buffer_pct(self) -> float:
        from .board import buyboard_orb_buffer_pct

        return buyboard_orb_buffer_pct(self)

    def _save_buyboard_orb_buffer_pct(self) -> None:
        """Persist the planning default without rewriting published cards."""

        from src.services.app_state import SETTINGS_FILE
        from src.utils.storage import save_json

        from .board import buyboard_orb_buffer_pct

        fraction = buyboard_orb_buffer_pct(self)
        percent = fraction * 100.0
        widget = self.__dict__.get("buyboard_orb_buffer_pct_input")
        if widget is not None:
            widget.setText(f"{percent:g}")
        settings = self.__dict__.setdefault("settings", {})
        if settings.get("orb_buffer_percent") == percent:
            return
        settings["orb_buffer_percent"] = percent
        save_json(SETTINGS_FILE, settings)
        append_log = getattr(self, "append_log", None)
        if callable(append_log):
            append_log(
                f"ORB planning buffer set to {percent:g}% for newly queued "
                "symbols. Existing plans keep their saved buffer."
            )

    def _buyboard_projection_values(self):
        values = tuple(
            self.__dict__.get("_buyboard_current_projections", ()) or ()
        )
        if not values:
            loader = getattr(self, "_buyboard_recovery_source", None)
            if callable(loader):
                values = tuple(loader() or ())
        return values

    def _buy_today_orb_symbols(self) -> list[str]:
        symbols: list[str] = []
        for value in self._buyboard_projection_values():
            card = getattr(value, "card", value)
            if getattr(card, "board_status", None) != BoardStatus.BUY_TODAY:
                continue
            symbol = str(getattr(card, "symbol", "") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _buyboard_monitored_symbols(self) -> list[str]:
        monitored_statuses = {
            BoardStatus.BUY_TODAY,
            BoardStatus.ENTRY_PENDING,
            BoardStatus.OPEN_POSITION,
            BoardStatus.PARTIAL_SELL,
            BoardStatus.SELL_ALL,
        }
        symbols: list[str] = []
        for value in self._buyboard_projection_values():
            card = getattr(value, "card", value)
            if getattr(card, "board_status", None) not in monitored_statuses:
                continue
            symbol = str(getattr(card, "symbol", "") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _refresh_buyboard_broker_truth(self) -> None:
        """Refresh read-only KIS holdings even when execution cannot start."""

        runtime_worker = _worker_for(self)
        if runtime_worker is not None:
            try:
                if runtime_worker.isRunning():
                    return
            except RuntimeError:
                pass
        worker = self.__dict__.get("kis_startup_worker")
        if worker is not None:
            try:
                if worker.isRunning():
                    return
            except RuntimeError:
                pass
        now = time.monotonic()
        last_started = float(
            self.__dict__.get(
                "_buyboard_broker_truth_refresh_started_at", 0.0
            )
            or 0.0
        )
        if (
            now - last_started
            < self._BUYBOARD_BROKER_TRUTH_REFRESH_MS / 1000.0
        ):
            return
        refresher = getattr(self, "preload_kis_accounts_on_startup", None)
        if not callable(refresher):
            return
        self._buyboard_broker_truth_refresh_started_at = now
        refresher()

    def _refresh_buyboard_orb_data(self) -> None:
        """Fetch current KIS minute bars for Buy Today independently of readiness."""

        if not is_buyboard_engine_enabled():
            return
        try:
            if not is_regular_session_open():
                return
        except Exception:
            return
        # ORB minute bars are needed only until a Buy Today entry leaves the
        # planning stage.  Positions and working orders use their separate
        # live trade/quote subscriptions and must not keep consuming the
        # comparatively expensive intraday-history refresh budget.
        symbols = self._buy_today_orb_symbols()
        if not symbols:
            return
        worker = self.__dict__.get("intraday_bulk_worker")
        if worker is not None:
            try:
                if worker.isRunning():
                    return
            except RuntimeError:
                pass
        now = time.monotonic()
        last_started = float(
            self.__dict__.get("_buyboard_orb_data_refresh_started_at", 0.0) or 0.0
        )
        if now - last_started < self._BUYBOARD_ORB_DATA_REFRESH_MS / 1000.0:
            return
        refresher = getattr(self, "refresh_watchlist_intraday_cache", None)
        if not callable(refresher):
            return
        self._buyboard_orb_data_refresh_started_at = now
        refresher(
            show_messages=False,
            triggered_by_live=True,
            source="Buy Today ORB",
            symbols=symbols,
            purpose="buyboard_orb",
        )

    def _buyboard_recovery_source(self):
        """Return a stable last-known card set while the canonical DB is down."""

        cached = self.__dict__.get("_buyboard_recovery_source_cards")
        if cached is not None:
            return tuple(cached)

        current = tuple(
            self.__dict__.get("_buyboard_current_projections", ()) or ()
        )
        if current and not bool(
            self.__dict__.get("_buyboard_recovery_snapshot_active", False)
        ):
            source = copy.deepcopy(current)
        else:
            try:
                from src.services import trade_card_repository

                source = tuple(
                    trade_card_repository.load_local_trade_cards_snapshot(
                        path=trade_card_repository.LOCAL_TRADE_CARDS_FILE
                    )
                )
            except Exception:
                logger.exception("Could not load the local Buy Board recovery snapshot")
                source = ()
        configured_accounts = self._buyboard_configured_account_keys()
        if configured_accounts:
            filtered = []
            for value in source:
                card = getattr(value, "card", value)
                if card is None:
                    filtered.append(value)
                    continue
                account_no = str(getattr(card, "account_no", "") or "").strip()
                environment = str(
                    getattr(card, "environment", "") or "PROD"
                ).upper()
                if account_no and (environment, account_no) not in configured_accounts:
                    continue
                filtered.append(value)
            source = tuple(filtered)
        self._buyboard_recovery_source_cards = tuple(source)
        return tuple(source)

    def _buyboard_configured_account_keys(self):
        cached = self.__dict__.get("_buyboard_configured_accounts")
        if cached is not None:
            return set(cached)
        resolver = getattr(self, "configured_kis_account_keys", None)
        try:
            accounts = set(resolver()) if callable(resolver) else set()
        except Exception:
            logger.exception("Could not resolve configured accounts for Buy Board recovery")
            accounts = set()
        self._buyboard_configured_accounts = tuple(sorted(accounts))
        return accounts

    @staticmethod
    def _buyboard_positive_number(value) -> float:
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return number if number > 0 else 0.0

    def _buyboard_recovery_holdings(self):
        snapshots = dict(self.__dict__.get("kis_account_snapshots", {}) or {})
        fetched_map = dict(
            self.__dict__.get("kis_account_snapshot_fetched_at", {}) or {}
        )
        now = dt.datetime.now(dt.timezone.utc)
        verified_accounts = set()
        holdings = {}
        for key, snapshot in snapshots.items():
            if not isinstance(key, tuple) or len(key) < 2 or not isinstance(snapshot, dict):
                continue
            environment = str(key[0] or "").upper()
            account_no = str(key[1] or "").strip()
            fetched_at = fetched_map.get(key)
            if not isinstance(fetched_at, dt.datetime):
                continue
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=dt.timezone.utc)
            age = (now - fetched_at.astimezone(dt.timezone.utc)).total_seconds()
            if age < 0 or age > self._BUYBOARD_BROKER_SNAPSHOT_MAX_AGE_SECONDS:
                continue
            account_key = (environment, account_no)
            verified_accounts.add(account_key)
            for section_name in ("domestic", "overseas"):
                section = snapshot.get(section_name)
                if not isinstance(section, dict):
                    continue
                for row in section.get("holdings", ()) or ():
                    if not isinstance(row, dict):
                        continue
                    symbol = str(row.get("symbol") or "").strip().upper()
                    quantity = self._buyboard_positive_number(row.get("quantity"))
                    if not symbol or quantity <= 0:
                        continue
                    holding_key = (environment, account_no, symbol)
                    previous = holdings.get(holding_key)
                    if previous is None or quantity > previous["quantity"]:
                        holdings[holding_key] = {
                            "quantity": quantity,
                            "orderable_quantity": self._buyboard_positive_number(
                                row.get("orderable_quantity")
                            ),
                            "average_price": self._buyboard_positive_number(
                                row.get("average_price")
                            ),
                            "name": str(row.get("name") or ""),
                        }
        return verified_accounts, holdings

    def _buyboard_recovery_projections(self):
        """Project the last local state as view-only, with fresh local ORB facts."""

        source = self._buyboard_recovery_source()
        manager = self.__dict__.get("execution_queue_manager")
        if manager is None:
            ensure_manager = getattr(self, "_ensure_execution_queue_manager", None)
            if callable(ensure_manager):
                try:
                    manager = ensure_manager()
                except Exception:
                    logger.exception(
                        "Could not load execution-queue data for Buy Board recovery"
                    )
        queue_lookup = getattr(manager, "get_item", None)

        from src.services.trade_card_orb_bridge import TradeCardOrbEvaluator

        evaluator = TradeCardOrbEvaluator()
        verified_accounts, holdings = self._buyboard_recovery_holdings()
        restriction = (
            "Kanban operational store unavailable; showing the last local snapshot "
            "read-only with app execution locked. Open Recovery Procedure for "
            "safe protective-exit and restoration instructions"
        )
        projections = []
        projected_keys = set()
        for value in source:
            card = getattr(value, "card", value)
            if card is None:
                projections.append(value)
                continue
            recovery_card = copy.deepcopy(card)
            card_key = (
                str(recovery_card.environment or "").upper(),
                str(recovery_card.account_no or ""),
                str(recovery_card.symbol or "").upper(),
            )
            projected_keys.add(card_key)
            account_key = card_key[:2]
            holding = holdings.get(card_key)
            if holding is not None:
                recovery_card.broker_quantity = max(
                    0, int(round(holding["quantity"]))
                )
                recovery_card.orderable_quantity = max(
                    0,
                    int(
                        round(
                            holding["orderable_quantity"]
                            or holding["quantity"]
                        )
                    ),
                )
                if holding["average_price"] > 0:
                    recovery_card.average_entry_price = holding["average_price"]
                if holding["name"] and not recovery_card.name:
                    recovery_card.name = holding["name"]
                if recovery_card.board_status not in {
                    BoardStatus.PARTIAL_SELL,
                    BoardStatus.SELL_ALL,
                }:
                    recovery_card.previous_board_status = recovery_card.board_status
                    recovery_card.board_status = BoardStatus.OPEN_POSITION
                recovery_card.position_runtime_status = PositionRuntimeStatus.OPEN
            elif account_key in verified_accounts and recovery_card.board_status in {
                BoardStatus.OPEN_POSITION,
                BoardStatus.PARTIAL_SELL,
                BoardStatus.SELL_ALL,
            }:
                recovery_card.previous_board_status = recovery_card.board_status
                recovery_card.board_status = (
                    BoardStatus.BUYLIST
                    if recovery_card.return_to_buylist_after_close
                    else BoardStatus.CLOSED
                )
                recovery_card.position_runtime_status = PositionRuntimeStatus.CLOSED
                recovery_card.broker_quantity = 0
                recovery_card.orderable_quantity = 0
                recovery_card.exit_all_required = False
                recovery_card.reserved_sell_quantity = 0
            if callable(queue_lookup):
                try:
                    queue_item = queue_lookup(
                        recovery_card.symbol, recovery_card.environment
                    )
                except Exception:
                    queue_item = None
                    logger.debug(
                        "Could not read recovery ORB data for %s",
                        recovery_card.symbol,
                        exc_info=True,
                    )
                if queue_item is not None:
                    original_price = recovery_card.market_data_last_trusted_price
                    original_price_at = recovery_card.market_data_last_trusted_at
                    original_breakout = recovery_card.breakout_price
                    evaluator.update_card(recovery_card, queue_item)
                    if recovery_card.breakout_price is None:
                        recovery_card.breakout_price = original_breakout
                    # An old pre-open queue row is useful for its configured
                    # breakout, but it must not masquerade as the current quote.
                    if (
                        recovery_card.entry_runtime_status
                        == EntryRuntimeStatus.DATA_UNAVAILABLE
                    ):
                        recovery_card.market_data_last_trusted_price = original_price
                        recovery_card.market_data_last_trusted_at = original_price_at

            if isinstance(value, BoardCardProjection):
                restrictions = tuple(
                    dict.fromkeys((*value.engine_restrictions, restriction))
                )
                projection = replace(
                    value,
                    card=recovery_card,
                    reconciliation_blocked=True,
                    engine_restrictions=restrictions,
                )
            else:
                projection = BoardCardProjection(
                    card=recovery_card,
                    reconciliation_blocked=True,
                    engine_restrictions=(restriction,),
                )
            projections.append(projection)
        for holding_key, holding in holdings.items():
            if holding_key in projected_keys:
                continue
            environment, account_no, symbol = holding_key
            card = TradeCardState(
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                name=holding["name"],
                board_status=BoardStatus.OPEN_POSITION,
                broker_quantity=max(0, int(round(holding["quantity"]))),
                orderable_quantity=max(
                    0,
                    int(
                        round(
                            holding["orderable_quantity"]
                            or holding["quantity"]
                        )
                    ),
                ),
                average_entry_price=holding["average_price"],
                position_runtime_status=PositionRuntimeStatus.OPEN,
            )
            projections.append(
                BoardCardProjection(
                    card=card,
                    reconciliation_blocked=True,
                    engine_restrictions=(restriction,),
                )
            )
        return tuple(projections)

    def _show_buyboard_recovery_snapshot(self) -> None:
        """Keep the board visible if the Kanban operational store cannot open."""

        from .board import populate_buyboard_columns

        projections = self._buyboard_recovery_projections()
        self._buyboard_recovery_snapshot_active = True
        populate_buyboard_columns(self, projections)
        self._refresh_buyboard_orb_data()

    def _save_buyboard_canonical_recovery_snapshot(self, projections) -> None:
        """Keep the laptop's recovery tier current after operational reads."""

        cards = []
        for value in tuple(projections or ()):
            card = getattr(value, "card", None)
            if card is not None:
                cards.append(card)
        if not cards:
            return
        signature = tuple(
            sorted(
                (
                    card.card_key,
                    int(card.version or 0),
                    str(card.updated_at or ""),
                )
                for card in cards
            )
        )
        if signature == self.__dict__.get("_buyboard_local_snapshot_signature"):
            return
        try:
            from src.services import trade_card_repository

            existing = trade_card_repository.load_local_trade_cards_snapshot(
                path=trade_card_repository.LOCAL_TRADE_CARDS_FILE
            )
            merged = {card.card_key: card for card in existing}
            merged.update({card.card_key: card for card in cards})
            trade_card_repository.save_local_trade_cards_snapshot(
                merged.values(),
                path=trade_card_repository.LOCAL_TRADE_CARDS_FILE,
            )
            self._buyboard_local_snapshot_signature = signature
        except Exception:
            logger.exception("Could not refresh the local Buy Board recovery snapshot")

    def _refresh_buyboard_live_metrics(self) -> None:
        # Rewriting Qt labels while its native drag loop is active causes
        # visible cursor/card hitching. The next 750 ms tick catches up after
        # the gesture; execution truth and stale-card fences are unaffected.
        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            return
        from .board import refresh_buyboard_live_metrics

        refresh_buyboard_live_metrics(self)

    def _buyboard_projection_request(
        self, generation: int, *, revision_only: bool = False
    ):
        engine = self._buyboard_engine()
        if engine is None:
            return None

        default_account_no = str(
            get_env_value("KIS_PROD_ACCOUNT_NO", "")
            or get_env_value("KIS_ACCOUNT_NO", "")
            or ""
        ).strip()
        if not default_account_no:
            for item in list(
                getattr(self.__dict__.get("buylist_manager"), "items", ()) or ()
            ):
                account_no = str(getattr(item, "kis_account_no", "") or "").strip()
                if account_no:
                    default_account_no = account_no
                    break

        runtime_worker = _worker_for(self)
        runtime_running = False
        if runtime_worker is not None:
            try:
                runtime_running = bool(runtime_worker.isRunning())
            except RuntimeError:
                runtime_running = False

        return BuyboardProjectionRequest(
            engine=engine,
            context=_projection_context(self),
            buylist_manager=copy.deepcopy(self.__dict__.get("buylist_manager")),
            watchlist=copy.deepcopy(self.__dict__.get("watchlist")),
            default_account_no=default_account_no,
            account_snapshots=copy.deepcopy(
                self.__dict__.get("kis_account_snapshots", {})
            ),
            account_snapshot_fetched_at=copy.deepcopy(
                self.__dict__.get("kis_account_snapshot_fetched_at", {})
            ),
            runtime_running=runtime_running,
            generation=generation,
            revision_only=bool(revision_only),
            expected_revision=self.__dict__.get("_buyboard_projection_revision"),
        )

    def _bootstrap_buyboard_projection(self) -> None:
        """Create only missing cards from state the application already owns."""

        engine = self._buyboard_engine()
        if engine is None:
            return

        from src.services.trade_card_bootstrap import (
            bootstrap_trade_cards_from_current_state,
        )

        default_account_no = str(
            get_env_value("KIS_PROD_ACCOUNT_NO", "")
            or get_env_value("KIS_ACCOUNT_NO", "")
            or ""
        ).strip()
        if not default_account_no:
            for item in list(
                getattr(self.__dict__.get("buylist_manager"), "items", ()) or ()
            ):
                account_no = str(getattr(item, "kis_account_no", "") or "").strip()
                if account_no:
                    default_account_no = account_no
                    break

        worker = _worker_for(self)
        runtime_running = False
        if worker is not None:
            try:
                runtime_running = bool(worker.isRunning())
            except RuntimeError:
                runtime_running = False

        kwargs = {}
        if not runtime_running:
            kwargs = {
                "account_snapshots": self.__dict__.get("kis_account_snapshots", {}),
                "account_snapshot_fetched_at": self.__dict__.get(
                    "kis_account_snapshot_fetched_at", {}
                ),
            }

        result = bootstrap_trade_cards_from_current_state(
            engine,
            buylist_manager=self.__dict__.get("buylist_manager"),
            watchlist=self.__dict__.get("watchlist"),
            default_account_no=default_account_no,
            **kwargs,
        )
        if result.changed:
            logger.info(
                "Buy Board bootstrap created=%d buylist_promotions=%d "
                "holding_updates=%d",
                len(result.created_keys),
                len(result.buylist_promoted_keys),
                len(result.holding_updated_keys),
            )

    def refresh_buyboard(self, *, revision_only: bool = False) -> None:
        from .board import populate_buyboard_columns

        if bool(self.__dict__.get("_database_shutting_down", False)):
            return
        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            self._buyboard_refresh_pending = True
            return

        worker = self.__dict__.get("_buyboard_projection_worker")
        if worker is not None:
            try:
                if worker.isRunning():
                    self._buyboard_refresh_pending = True
                    return
            except RuntimeError:
                pass

        generation = int(
            self.__dict__.get("_buyboard_projection_generation", 0)
        ) + 1
        self._buyboard_projection_generation = generation
        request = (
            self._buyboard_projection_request(generation, revision_only=True)
            if revision_only
            else self._buyboard_projection_request(generation)
        )
        if request is None:
            self._show_buyboard_recovery_snapshot()
            return

        worker = BuyboardProjectionWorker(request)
        self._buyboard_projection_worker = worker
        worker.completed.connect(self._on_buyboard_projection_completed)
        worker.finished.connect(self._on_buyboard_projection_worker_finished)
        self._track_worker("_buyboard_projection_worker", worker)
        worker.start()

    def _on_buyboard_projection_completed(
        self, projections, error: str, generation: int
    ) -> None:
        if generation != self.__dict__.get("_buyboard_projection_generation", 0):
            return
        if error:
            if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
                self._buyboard_refresh_pending = True
                return
            self._show_buyboard_recovery_snapshot()
            return
        worker = self.__dict__.get("_buyboard_projection_worker")
        resolved_revision = getattr(worker, "resolved_revision", None)
        if resolved_revision is not None:
            self._buyboard_projection_revision = resolved_revision
        if projections is None:
            return
        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            self._buyboard_deferred_projection = (projections, error, generation)
            return
        from .board import populate_buyboard_columns

        started_at = time.perf_counter()
        self._buyboard_recovery_snapshot_active = False
        self.__dict__.pop("_buyboard_recovery_source_cards", None)
        self._save_buyboard_canonical_recovery_snapshot(projections)
        populate_buyboard_columns(self, projections)
        self._refresh_buyboard_orb_data()
        for callback_name in (
            "_update_tradingview_activate_btn",
            "_update_intraday_activate_btn",
        ):
            callback = getattr(self, callback_name, None)
            if callable(callback):
                callback()
        render_ms = (time.perf_counter() - started_at) * 1000.0
        now = time.monotonic()
        last_warning_at = float(
            self.__dict__.get("_buyboard_last_slow_render_warning_at", 0.0) or 0.0
        )
        warning_due = (
            render_ms >= self._BUYBOARD_SLOW_RENDER_WARNING_MS
            and now - last_warning_at
            >= self._BUYBOARD_SLOW_RENDER_WARNING_INTERVAL_SECONDS
        )
        if warning_due:
            self._buyboard_last_slow_render_warning_at = now
            logger.warning(
                "Buy Board projection generation=%d had a slow UI render (%.1f ms)",
                generation,
                render_ms,
            )
        else:
            logger.debug(
                "Buy Board projection generation=%d rendered in %.1f ms",
                generation,
                render_ms,
            )

    def _on_buyboard_projection_worker_finished(self) -> None:
        """Launch one coalesced refresh requested while a read was running."""

        worker = self.__dict__.get("_buyboard_projection_worker")
        if worker is not None:
            try:
                if not worker.isRunning():
                    self._buyboard_projection_worker = None
            except RuntimeError:
                self._buyboard_projection_worker = None
        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            return
        if bool(self.__dict__.pop("_buyboard_refresh_pending", False)):
            self.refresh_buyboard()

    def _set_buyboard_interaction_active(self, active: bool) -> None:
        """Prevent a timer refresh from replacing widgets during a gesture."""

        depth = int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0)
        depth = depth + 1 if active else max(0, depth - 1)
        self._buyboard_interaction_depth = depth
        if depth:
            return
        pending = bool(self.__dict__.pop("_buyboard_refresh_pending", False))
        deferred = self.__dict__.pop("_buyboard_deferred_projection", None)
        if pending:
            self.refresh_buyboard()
        elif deferred is not None:
            self._on_buyboard_projection_completed(*deferred)

    def _buyboard_dispatch_command(
        self,
        command: AnyBoardCommand,
        *,
        interaction_fingerprint: str = "",
    ) -> bool:
        engine = self._buyboard_engine()
        if engine is None:
            from PyQt5.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Buy Board", "The board database is unavailable.")
            return False

        action_context = _action_context(self, command)
        route_via_operator_queue = False
        role = self.__dict__.get("state_sync_role")
        if role is not None and is_regular_session_open():
            from src.services.operator_command_service import (
                operator_command_type_for_board_command,
            )

            if operator_command_type_for_board_command(command) is not None:
                # Same-device Operator/Executor actions can be applied through
                # the direct canonical workflow.  A split or not-yet-verified
                # topology remains fail-closed through the durable queue.
                route_via_operator_queue = _route_live_command_via_operator_queue(
                    self
                )
            elif not bool(getattr(role, "is_main", False)):
                from PyQt5.QtWidgets import QMessageBox

                QMessageBox.information(
                    self,
                    "Market-open planning lock",
                    "Market is open. A non-execution-owner device may submit "
                    "Live Intervention commands only; canonical planning state "
                    "cannot be rewritten.",
                )
                return False
        if _claims_kanban_ownership(command):
            # These gestures perform zero broker I/O here. They are valid
            # pre-market and from a pull-only laptop; the authoritative PC
            # runtime later consumes them only after its complete readiness
            # predicate and broker-boundary guards pass.
            action_context = replace(action_context, enforce_runtime_fences=False)
        request = BuyboardCommandRequest(
            engine=engine,
            command=command,
            action_context=action_context,
            projection_context=_projection_context(self),
            interaction_fingerprint=str(interaction_fingerprint or ""),
            enqueued_at=time.perf_counter(),
            route_via_operator_queue=route_via_operator_queue,
            requester_role=role,
        )
        worker = self.__dict__.get("_buyboard_command_worker")
        create_worker = worker is None
        if worker is not None:
            try:
                create_worker = bool(worker.isFinished())
            except RuntimeError:
                create_worker = True
        if create_worker:
            worker = BoardCommandWorker()
            self._buyboard_command_worker = worker
            worker.completed.connect(self._on_buyboard_command_completed)
            track_worker = getattr(self, "_track_worker", None)
            if callable(track_worker):
                track_worker("_buyboard_command_worker", worker)
        self._set_buyboard_card_pending(request.card_key, True)
        worker.enqueue(request)
        if create_worker:
            worker.start()
        return True

    def _set_buyboard_card_pending(self, card_key: str, pending: bool) -> None:
        counts = self.__dict__.setdefault("_buyboard_pending_command_counts", {})
        count = int(counts.get(card_key, 0) or 0)
        count = count + 1 if pending else max(0, count - 1)
        if count:
            counts[card_key] = count
        else:
            counts.pop(card_key, None)
        pending_keys = set(counts)
        for column in getattr(self, "buyboard_columns", {}).values():
            setter = getattr(column, "set_pending_card_keys", None)
            if callable(setter):
                setter(pending_keys)

    def _on_buyboard_command_completed(self, result: BuyboardCommandResult) -> None:
        from PyQt5.QtWidgets import QMessageBox

        self._set_buyboard_card_pending(result.request.card_key, False)
        if not result.succeeded:
            QMessageBox.warning(self, "Buy Board", result.message)
        elif result.operator_command_queued:
            append_log = getattr(self, "append_log", None)
            if callable(append_log):
                append_log(
                    f"{type(result.request.command).__name__}: {result.message}"
                )
        self.refresh_buyboard()
