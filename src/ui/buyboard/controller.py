"""Board-command handlers and the ``BuyboardMixin`` Qt controller.

``buydashboard_to_kanban.md`` section 295: "Dragging must send commands to
the backend. The UI must not directly mutate state or submit broker orders."

:func:`apply_board_command` is the backend referred to there. It is a plain
function with no Qt dependency (the ``BuyboardMixin`` methods at the bottom
of this module are the only Qt-facing pieces), so it is directly unit
testable. It only ever changes ``board_status`` and the small set of fields
each command concerns -- it never talks to KIS. Phase 3/5's entry and
position engines observe the resulting board_status/flags
(``exit_all_required``, ``sell_all_at_market_open``,
``pending_partial_sell_quantity``, ``stop_type``/``active_stop_price``) on
their own heartbeat and are responsible for the actual order submission,
exactly as section 296-304 describes for the account-level execution
boundary.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.engine import Engine

from src.core.kanban_transitions import (
    InvalidBoardTransitionError,
    validate_board_transition,
)
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import trade_card_repository as repo
from src.services.position_manager import PositionManager, minimum_manual_stop_price
from src.services.trade_card_repository import (
    TradeCardNotFoundError,
    TradeCardVersionConflictError,
)

from .drag_commands import (
    ActivateForToday,
    AnyBoardCommand,
    CancelEntry,
    CancelQueuedSellAll,
    MoveToBuylist,
    MoveToWatchlist,
    RequestPartialSell,
    RequestSellAll,
    SetBreakevenStop,
    SetManualStop,
)

logger = logging.getLogger(__name__)


class CommandRejectedError(RuntimeError):
    """A well-formed command that cannot be applied to the card's current
    state (illegal Kanban transition, invalid quantity/price, etc.)."""


# Commands whose only effect is a direct board_status move, keyed by type.
# CancelEntry/SetBreakevenStop/SetManualStop/RequestPartialSell/
# CancelQueuedSellAll have extra validation and are handled by their own
# functions below.
_SIMPLE_MOVE_TARGET_STATUS = {
    MoveToWatchlist: BoardStatus.WATCHLIST,
    MoveToBuylist: BoardStatus.BUYLIST,
    ActivateForToday: BoardStatus.BUY_TODAY,
    RequestSellAll: BoardStatus.SELL_ALL,
}


def apply_board_command(
    engine: Optional[Engine], command: AnyBoardCommand
) -> TradeCardState:
    """Validate and apply one command. Raises ``TradeCardNotFoundError``,
    ``TradeCardVersionConflictError`` (stale command, spec section 317-319),
    or ``CommandRejectedError`` (well-formed but illegal right now).
    """
    card = repo.get_trade_card(
        engine, command.environment, command.account_no, command.symbol
    )
    if card is None:
        raise TradeCardNotFoundError(
            f"No trade card for {command.environment}:{command.account_no}:"
            f"{command.symbol}"
        )
    if card.version != command.expected_card_version:
        raise TradeCardVersionConflictError(
            f"Command {command.command_id} expected version "
            f"{command.expected_card_version}, stored version is {card.version}"
        )

    if isinstance(command, CancelEntry):
        _apply_cancel_entry(card)
    elif isinstance(command, RequestPartialSell):
        _apply_partial_sell(card, command)
    elif isinstance(command, SetBreakevenStop):
        _apply_set_breakeven_stop(card)
    elif isinstance(command, SetManualStop):
        _apply_set_manual_stop(card, command)
    elif isinstance(command, CancelQueuedSellAll):
        _apply_cancel_queued_sell_all(card)
    else:
        target_status = _SIMPLE_MOVE_TARGET_STATUS.get(type(command))
        if target_status is None:
            raise CommandRejectedError(
                f"Unrecognized command type: {type(command).__name__}"
            )
        _apply_simple_move(card, target_status, command)

    return repo.update_trade_card(
        engine, card, expected_version=command.expected_card_version
    )


def _move(card: TradeCardState, target_status: BoardStatus) -> None:
    try:
        validate_board_transition(card.board_status, target_status)
    except InvalidBoardTransitionError as exc:
        raise CommandRejectedError(str(exc)) from exc
    card.previous_board_status = card.board_status
    card.board_status = target_status


def _apply_simple_move(
    card: TradeCardState, target_status: BoardStatus, command: AnyBoardCommand
) -> None:
    _move(card, target_status)
    if isinstance(command, MoveToWatchlist):
        card.watchlist_member = True
    elif isinstance(command, MoveToBuylist):
        card.buylist_member = True
        # Leaving Buy Today/Entry Pending without a filled order clears any
        # stale entry-runtime badge and block reason.
        card.entry_runtime_status = None
        card.entry_block_reason = ""
    elif isinstance(command, ActivateForToday):
        card.buylist_member = True
    elif isinstance(command, RequestSellAll):
        card.exit_all_required = True


def _apply_cancel_entry(card: TradeCardState) -> None:
    if card.board_status == BoardStatus.BUY_TODAY:
        # No order can exist yet for a BUY_TODAY card in this design --
        # trading_engine moves a card straight from BUY_TODAY to
        # ENTRY_PENDING in the same heartbeat pass it submits an order, so
        # there is never a window where BUY_TODAY has a live order to
        # orphan. Safe to move immediately.
        _move(card, BoardStatus.BUYLIST)
        card.entry_runtime_status = None
        card.entry_block_reason = ""
    elif card.board_status == BoardStatus.ENTRY_PENDING:
        # An order may be working at the broker right now -- do not move
        # the card (and thus do not let the user believe it's safely back
        # in Buylist) until the engine has actually cancelled/reconciled
        # it. src.services.trading_engine watches for this flag on its next
        # heartbeat, cancels the order, and moves the card once the broker
        # confirms zero/partial fill (mirrors the EOD zero-fill path).
        card.entry_block_reason = "cancel_requested"
    else:
        raise CommandRejectedError(
            f"Cannot cancel an entry from {card.board_status.value}"
        )


def _apply_partial_sell(card: TradeCardState, command: RequestPartialSell) -> None:
    if card.board_status != BoardStatus.OPEN_POSITION:
        raise CommandRejectedError(
            "Partial sell can only be requested from Open Positions"
        )
    if command.quantity <= 0:
        raise CommandRejectedError("Partial-sell quantity must be positive")
    orderable = card.orderable_quantity or card.broker_quantity
    if orderable <= 0:
        raise CommandRejectedError("No orderable quantity to sell")
    if command.quantity >= orderable:
        # Spec section 576-579: a request at/above the full orderable size is
        # a Sell All, not a partial sell.
        _move(card, BoardStatus.SELL_ALL)
        card.exit_all_required = True
        card.pending_partial_sell_quantity = 0
        return
    _move(card, BoardStatus.PARTIAL_SELL)
    card.pending_partial_sell_quantity = command.quantity


def _apply_set_breakeven_stop(card: TradeCardState) -> None:
    if card.position_runtime_status == PositionRuntimeStatus.NONE:
        raise CommandRejectedError("No open position to set a stop on")
    PositionManager().apply_breakeven_stop(card)


def _apply_set_manual_stop(card: TradeCardState, command: SetManualStop) -> None:
    if card.position_runtime_status == PositionRuntimeStatus.NONE:
        raise CommandRejectedError("No open position to set a stop on")
    minimum = minimum_manual_stop_price(card)
    if command.price < minimum:
        raise CommandRejectedError(
            f"Manual stop {command.price} cannot widen risk below the minimum "
            f"{minimum} -- the greater of breakeven and the current active stop "
            f"(spec section 646-659)"
        )
    PositionManager().apply_manual_stop(card, command.price)
    card.active_stop_price = command.price


def _apply_cancel_queued_sell_all(card: TradeCardState) -> None:
    if card.board_status != BoardStatus.SELL_ALL or not card.sell_all_at_market_open:
        raise CommandRejectedError("No queued market-open Sell All to cancel")
    _move(card, BoardStatus.OPEN_POSITION)
    card.sell_all_at_market_open = False
    card.exit_all_required = False


# --- Qt controller mixin ----------------------------------------------------
#
# Deliberately thin: every handler below just builds a command and calls
# apply_board_command, then repaints. All decision logic lives above, where
# it can be unit tested without a QApplication.


class BuyboardMixin:
    """Composed onto MainWindow alongside BuylistMixin (see
    ``src/ui/buyboard/__init__.py``). Builds the new "Buy Board" tab and
    wires drag/drop and dialog actions to ``apply_board_command``.
    """

    def _buyboard_engine(self):
        return self.__dict__.get("pc_db_engine")

    def _build_buyboard_tab(self) -> None:
        from .board import build_buyboard_widget

        build_buyboard_widget(self)
        self.refresh_buyboard()

    def refresh_buyboard(self) -> None:
        """Reload every card from the repository and repaint all columns."""
        from .board import populate_buyboard_columns

        cards = repo.list_trade_cards(self._buyboard_engine(), environment="PROD")
        populate_buyboard_columns(self, cards)

    def _buyboard_dispatch_command(self, command: AnyBoardCommand) -> bool:
        """Apply one command; show a message box and refresh on rejection,
        otherwise refresh silently. Returns True on success."""
        from PyQt5.QtWidgets import QMessageBox

        try:
            apply_board_command(self._buyboard_engine(), command)
        except TradeCardVersionConflictError:
            QMessageBox.warning(
                self,
                "Buy Board",
                "This card changed on another device since it was loaded. "
                "Refreshing the board -- please retry.",
            )
            self.refresh_buyboard()
            return False
        except TradeCardNotFoundError:
            QMessageBox.warning(self, "Buy Board", "This card no longer exists.")
            self.refresh_buyboard()
            return False
        except CommandRejectedError as exc:
            QMessageBox.warning(self, "Buy Board", str(exc))
            return False
        self.refresh_buyboard()
        return True
