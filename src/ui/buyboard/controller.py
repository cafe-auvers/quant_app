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
        request = self.request
        try:
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
            )
            self.completed.emit(projections, "", request.generation)
        except Exception as exc:
            logger.exception("Buy Board projection refresh failed")
            self.completed.emit([], str(exc), request.generation)


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


class BuyboardMixin:
    """Build the board and route all gestures through the workflow service."""

    _BUYBOARD_PROJECTION_REFRESH_MS = 3000
    _BUYBOARD_LIVE_METRIC_REFRESH_MS = 750

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
                "Buy Board bootstrap created=%d holding_updates=%d",
                len(result.created_keys),
                len(result.holding_updated_keys),
            )

    def refresh_buyboard(self) -> None:
        from .board import populate_buyboard_columns

        if int(self.__dict__.get("_buyboard_interaction_depth", 0) or 0) > 0:
            self._buyboard_refresh_pending = True
            return

        worker = self.__dict__.get("_buyboard_projection_worker")
        if worker is not None:
            try:
                if worker.isRunning():
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

        populate_buyboard_columns(self, projections)

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
        from PyQt5.QtWidgets import QMessageBox
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

        intent_only_types = (
            ActivateForToday,
            CancelPartialSell,
            RequestPartialSell,
            RequestSellAll,
            CancelQueuedSellAll,
            SetOrbStop,
            SetBreakevenStop,
            SetManualStop,
        )

        def request(current_command: AnyBoardCommand) -> None:
            context = _action_context(self, current_command)
            if isinstance(current_command, intent_only_types):
                # These gestures perform zero broker I/O here. They are valid
                # pre-market and from a pull-only laptop; the authoritative PC
                # runtime later consumes them only after its complete readiness
                # predicate and broker-boundary guards pass.
                context = replace(context, enforce_runtime_fences=False)
            execution_workflow_service.request_board_action(
                self._buyboard_engine(),
                current_command,
                context=context,
                claim_kanban_ownership=isinstance(
                    current_command, intent_only_types
                ),
            )

        current_command = command
        for attempt in range(2):
            try:
                request(current_command)
                break
            except TradeCardVersionConflictError:
                if attempt == 0 and interaction_fingerprint:
                    try:
                        projection = execution_workflow_service.get_board_projection(
                            self._buyboard_engine(),
                            environment=current_command.environment,
                            account_no=current_command.account_no,
                            symbol=current_command.symbol,
                            context=_projection_context(self),
                        )
                    except Exception:
                        projection = None
                    if (
                        projection is not None
                        and board_interaction_fingerprint(projection)
                        == interaction_fingerprint
                    ):
                        fresh = card_drag_payload(projection)
                        current_command = replace(
                            current_command,
                            expected_card_version=fresh["version"],
                            expected_readiness_generation=fresh[
                                "readiness_generation"
                            ],
                            expected_ownership_version=fresh["ownership_version"],
                            expected_execution_owner=fresh["execution_owner"],
                            expected_strategy_instance_id=fresh[
                                "strategy_instance_id"
                            ],
                        )
                        continue
                QMessageBox.warning(
                    self,
                    "Buy Board",
                    "This card changed since it was loaded. The board has been refreshed; please retry.",
                )
                self.refresh_buyboard()
                return False
            except TradeCardNotFoundError:
                QMessageBox.warning(self, "Buy Board", "This card no longer exists.")
                self.refresh_buyboard()
                return False
            except BoardCommandRejectedError as exc:
                QMessageBox.warning(self, "Buy Board", str(exc))
                self.refresh_buyboard()
                return False
        self.refresh_buyboard()
        return True
