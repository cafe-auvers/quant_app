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
import logging
from queue import Empty, Queue
import time
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal

from src.core.board_workflow import (
    AnyBoardCommand,
    BoardActionContext,
    BoardProjectionContext,
)
from src.core.execution_config import (
    is_buyboard_engine_enabled,
)
from src.core.runtime_readiness import RuntimeDeviceState
from src.services import execution_workflow_service
from src.services.execution_workflow_service import BoardCommandRejectedError
from src.services.trade_card_repository import (
    TradeCardNotFoundError,
    TradeCardVersionConflictError,
)
from src.utils.config import get_env_value
from src.utils.market_calendar import is_regular_session_open

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


class BuyboardProjectionWorker(QThread):
    """Build the canonical board projection without blocking Qt."""

    completed = pyqtSignal(object, str, int)

    def __init__(self, request: BuyboardProjectionRequest) -> None:
        super().__init__()
        self.request = request

    def run(self) -> None:
        started_at = time.perf_counter()
        request = self.request
        try:
            from .columns import BOARD_COLUMN_ORDER
            from src.services.trade_card_bootstrap import (
                bootstrap_trade_cards_from_current_state,
            )

            kwargs = {}
            if not request.runtime_running:
                kwargs = {
                    "account_snapshots": request.account_snapshots,
                    "account_snapshot_fetched_at": (
                        request.account_snapshot_fetched_at
                    ),
                }
            try:
                bootstrap_trade_cards_from_current_state(
                    request.engine,
                    buylist_manager=request.buylist_manager,
                    watchlist=request.watchlist,
                    default_account_no=request.default_account_no,
                    **kwargs,
                )
            except Exception:
                logger.exception("Buy Board bootstrap refresh failed")

            projections = execution_workflow_service.list_board_projections(
                request.engine,
                environment="PROD",
                context=request.context,
                board_statuses=BOARD_COLUMN_ORDER,
            )
            self.completed.emit(projections, "", request.generation)
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
        global_restrictions.append("Canonical database is not confirmed writable")

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
    worker = _worker_for(main_window)
    if worker is None:
        return BoardActionContext(
            enforce_runtime_fences=True,
            engine_enabled=is_buyboard_engine_enabled(),
            action_ready=False,
            device_active=False,
            regular_session_open=_safe_regular_session_open(),
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


class BuyboardMixin:
    """Build the board and route all gestures through the workflow service."""

    _BUYBOARD_PROJECTION_REFRESH_MS = 3000
    _BUYBOARD_LIVE_METRIC_REFRESH_MS = 750
    _BUYBOARD_SLOW_RENDER_WARNING_MS = 1000.0
    _BUYBOARD_SLOW_RENDER_WARNING_INTERVAL_SECONDS = 60.0

    def _buyboard_engine(self):
        return self.__dict__.get("pc_db_engine")

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
        timer.timeout.connect(self.refresh_buyboard)
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

    def _refresh_buyboard_live_metrics(self) -> None:
        # Rewriting Qt labels while its native drag loop is active causes
        # visible cursor/card hitching. The next 750 ms tick catches up after
        # the gesture; execution truth and stale-card fences are unaffected.
        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            return
        from .board import refresh_buyboard_live_metrics

        refresh_buyboard_live_metrics(self)

    def _buyboard_projection_request(self, generation: int):
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

    def refresh_buyboard(self) -> None:
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
        request = self._buyboard_projection_request(generation)
        if request is None:
            populate_buyboard_columns(self, [])
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
            return
        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            self._buyboard_deferred_projection = (projections, error, generation)
            return
        from .board import populate_buyboard_columns

        started_at = time.perf_counter()
        populate_buyboard_columns(self, projections)
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
        self.refresh_buyboard()
