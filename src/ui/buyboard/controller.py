"""Thin Qt adapter for the shared execution workflow service.

Kanban gestures are typed requests.  This module neither applies domain
mutations nor talks to a repository/gateway; it submits the request and then
rebuilds the board from a fresh authoritative projection.
"""
from __future__ import annotations

from src.core.board_workflow import (
    AnyBoardCommand,
    BoardActionContext,
    BoardProjectionContext,
)
from src.core.execution_config import is_buyboard_engine_enabled
from src.core.runtime_readiness import RuntimeDeviceState
from src.services import execution_workflow_service
from src.services.execution_workflow_service import BoardCommandRejectedError
from src.services.trade_card_repository import (
    TradeCardNotFoundError,
    TradeCardVersionConflictError,
)

# Compatibility name used by existing extensions/tests.
CommandRejectedError = BoardCommandRejectedError


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
    # An unknown account is handled by action-time readiness.  Known accounts
    # without positive startup evidence are included here when worker state
    # exposes them through its cached cards.
    for card in list(getattr(worker, "_cached_cards", ()) or ()):
        if card.account_no and card.account_no not in reconciled:
            blocked.add(card.account_no)

    return BoardProjectionContext(
        readiness_generation=int(getattr(worker, "readiness_generation", 0) or 0),
        reconciliation_blocked_accounts=tuple(sorted(blocked)),
        global_restrictions=tuple(global_restrictions),
        account_restrictions=tuple(account_restrictions),
    )


def _action_context(main_window, command: AnyBoardCommand) -> BoardActionContext:
    worker = _worker_for(main_window)
    if worker is None:
        return BoardActionContext(
            enforce_runtime_fences=True,
            engine_enabled=is_buyboard_engine_enabled(),
            action_ready=False,
            device_active=False,
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

    def _buyboard_engine(self):
        return self.__dict__.get("pc_db_engine")

    def _build_buyboard_tab(self) -> None:
        from .board import build_buyboard_widget

        build_buyboard_widget(self)
        self.refresh_buyboard()

    def refresh_buyboard(self) -> None:
        from .board import populate_buyboard_columns

        projections = execution_workflow_service.list_board_projections(
            self._buyboard_engine(),
            environment="PROD",
            context=_projection_context(self),
        )
        populate_buyboard_columns(self, projections)

    def _buyboard_dispatch_command(self, command: AnyBoardCommand) -> bool:
        from PyQt5.QtWidgets import QMessageBox

        try:
            execution_workflow_service.request_board_action(
                self._buyboard_engine(),
                command,
                context=_action_context(self, command),
            )
        except TradeCardVersionConflictError:
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
            # Repaint on every rejection: the drag widget never performs a
            # local row move, and this guarantees the user sees current truth.
            self.refresh_buyboard()
            return False
        self.refresh_buyboard()
        return True
