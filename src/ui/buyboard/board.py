"""Assembles the operator-facing Buy Board columns and refresh bar.

Widget assembly and the drag-drop -> command translation live here;
Every gesture is submitted through :mod:`src.services.execution_workflow_service`;
the widgets are rebuilt only from its authoritative projection.
"""
from __future__ import annotations

import logging
import getpass
from typing import Callable, Dict, List, Optional

from PyQt5.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.core.execution_config import is_buyboard_engine_enabled
from src.core.board_workflow import (
    BoardCardProjection,
    BoardExecutionOrderProjection,
    BoardExternalOrderProjection,
)
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import buying_power_cache, execution_workflow_service

from . import dialogs
from .card import board_interaction_fingerprint, card_drag_payload
from .columns import BOARD_COLUMN_ORDER, BOARD_COLUMN_TITLES, BoardColumnList
from .drag_commands import (
    ActivateForToday,
    CancelEntry,
    CancelPartialSell,
    CancelQueuedSellAll,
    MoveToBuylist,
    MoveToWatchlist,
    ReorderCard,
    RequestPartialSell,
    RequestSellAll,
    SetBreakevenStop,
    SetManualStop,
    SetOrbStop,
)

logger = logging.getLogger(__name__)

# Commands whose real effect is a broker action (cancel/submit an order,
# change a live stop). While the engine cutover flag is off, these are
# refused with an explanatory message instead of silently moving the card to
# a column that implies something is happening at the broker when nothing
# is (see execution_config.is_buyboard_engine_enabled's docstring).
_ENGINE_GATED_TARGET_COLUMNS = {
    BoardStatus.PARTIAL_SELL,
    BoardStatus.SELL_ALL,
}


def build_buyboard_widget(main_window) -> None:
    root_layout = QVBoxLayout(main_window.buyboard_widget)
    root_layout.setContentsMargins(6, 4, 6, 4)

    header = QHBoxLayout()
    title = QLabel("<b>Buy Board</b>")
    header.addWidget(title)
    header.addStretch()
    engine_status = QLabel(
        "Engine: ENABLED" if is_buyboard_engine_enabled() else "Engine: OFF (board is read-only for order actions)"
    )
    engine_status.setStyleSheet(
        "color: #2e7d32; font-weight: bold;"
        if is_buyboard_engine_enabled()
        else "color: #888;"
    )
    header.addWidget(engine_status)

    # P1-10: uniqueness is (environment, account_no, symbol), not symbol
    # alone -- without a filter, two accounts holding the same symbol
    # render as two visually near-identical cards with no way to isolate
    # one account's view.
    header.addWidget(QLabel("Account:"))
    account_filter_combo = QComboBox()
    account_filter_combo.addItem("All Accounts", None)
    account_filter_combo.currentIndexChanged.connect(main_window.refresh_buyboard)
    header.addWidget(account_filter_combo)

    refresh_btn = QPushButton("Refresh")
    refresh_btn.clicked.connect(main_window.refresh_buyboard)
    header.addWidget(refresh_btn)
    root_layout.addLayout(header)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    columns_widget = QWidget()
    columns_layout = QHBoxLayout(columns_widget)
    columns_layout.setSpacing(6)

    lists: Dict[BoardStatus, BoardColumnList] = {}
    for board_status in BOARD_COLUMN_ORDER:
        group = QGroupBox(BOARD_COLUMN_TITLES[board_status])
        group_layout = QVBoxLayout(group)
        column_list = BoardColumnList(
            board_status,
            lambda payload, target, mw=main_window: _handle_card_dropped(mw, payload, target),
            lambda payload, global_pos, mw=main_window: _handle_card_context_menu(mw, payload, global_pos),
            lambda order, mw=main_window: _handle_external_order_adopt(mw, order),
            lambda active, mw=main_window: mw._set_buyboard_interaction_active(active),
        )
        group_layout.addWidget(column_list)
        columns_layout.addWidget(group, 1)
        lists[board_status] = column_list

    scroll.setWidget(columns_widget)
    root_layout.addWidget(scroll, 1)

    main_window.buyboard_columns = lists
    main_window._buyboard_engine_status_label = engine_status
    main_window._buyboard_account_filter_combo = account_filter_combo


def _quote_lookup_for(main_window) -> Optional[Callable[[str], Optional[float]]]:
    """Live last-price lookup for card P&L (P1-7), sourced from the running
    engine's market-data service if one is attached to the window (see
    :mod:`src.services.buyboard_runtime`). Returns ``None`` -- meaning "no
    live price available" -- until the engine is actually running; cards
    then show plain position facts instead of a fabricated P&L.
    """
    worker = getattr(main_window, "_buyboard_runtime_worker", None)
    runtime = getattr(worker, "runtime", None)
    market_data = getattr(runtime, "market_data", None) if runtime is not None else None
    if market_data is None:
        return None

    def lookup(symbol: str) -> Optional[float]:
        try:
            quote = market_data.latest_quote(symbol)
            return quote.last_price if quote is not None else None
        except Exception:
            # A UI repaint must never affect the market-data/runtime thread.
            logger.debug("Buy Board quote lookup failed for %s", symbol, exc_info=True)
            return None

    return lookup


def _account_equity_lookup_for(
    _main_window,
) -> Callable[[str, str], Optional[float]]:
    """Read the existing in-memory account snapshot; never query KIS/DB here."""

    def lookup(environment: str, account_no: str) -> Optional[float]:
        snapshot = buying_power_cache.get_snapshot(environment, account_no)
        if snapshot is None or snapshot.total_equity_usd <= 0:
            return None
        return float(snapshot.total_equity_usd)

    return lookup


def refresh_buyboard_live_metrics(main_window) -> int:
    """Refresh current-price metrics in-place, independently of DB projection."""

    quote_lookup = _quote_lookup_for(main_window)
    equity_lookup = _account_equity_lookup_for(main_window)
    updated = 0
    for column_list in getattr(main_window, "buyboard_columns", {}).values():
        updated += column_list.refresh_live_metrics(quote_lookup, equity_lookup)
    return updated


def _state(value):
    if isinstance(value, (BoardExternalOrderProjection, BoardExecutionOrderProjection)):
        return None
    return value.card if isinstance(value, BoardCardProjection) else value


def _account_no(value) -> str:
    state = _state(value)
    return state.account_no if state is not None else value.order.account_no


def _sync_account_filter_options(main_window, cards) -> Optional[str]:
    """Refreshes the account-filter combo's entries from the live card set
    and returns the currently selected account_no (``None`` = All Accounts)."""
    combo = getattr(main_window, "_buyboard_account_filter_combo", None)
    if combo is None:
        return None
    accounts = sorted({_account_no(card) for card in cards if _account_no(card)})
    desired_values = [None, *accounts]
    current_values = [combo.itemData(index) for index in range(combo.count())]
    if current_values == desired_values:
        return combo.currentData()
    previous_selection = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("All Accounts", None)
    for account_no in accounts:
        combo.addItem(account_no, account_no)
    restored_index = combo.findData(previous_selection) if previous_selection else 0
    combo.setCurrentIndex(restored_index if restored_index >= 0 else 0)
    combo.blockSignals(False)
    return combo.currentData()


def populate_buyboard_columns(main_window, cards) -> None:
    cards = list(cards)
    main_window._buyboard_current_projections = tuple(cards)
    visible_statuses = set(BOARD_COLUMN_ORDER)
    visible_cards = [
        value
        for value in cards
        if _state(value) is None or _state(value).board_status in visible_statuses
    ]
    selected_account = _sync_account_filter_options(main_window, visible_cards)
    if selected_account:
        visible_cards = [
            card for card in visible_cards if _account_no(card) == selected_account
        ]
    grouped: Dict[BoardStatus, List[TradeCardState]] = {
        status: [] for status in BOARD_COLUMN_ORDER
    }
    for card in visible_cards:
        state = _state(card)
        # Standalone unowned/unlinked broker orders must remain visible even
        # though Watchlist is hidden. Open Positions is the safety/exposure
        # surface; these rows remain non-draggable observation-only widgets.
        target_status = (
            state.board_status if state is not None else BoardStatus.OPEN_POSITION
        )
        grouped[target_status].append(card)
    quote_lookup = _quote_lookup_for(main_window)
    equity_lookup = _account_equity_lookup_for(main_window)
    for status, column_list in main_window.buyboard_columns.items():
        column_list.set_cards(
            grouped.get(status, []),
            quote_lookup,
            equity_lookup,
        )
        pending_setter = getattr(column_list, "set_pending_card_keys", None)
        if callable(pending_setter):
            pending_setter(
                set(
                    main_window.__dict__.get(
                        "_buyboard_pending_command_counts", {}
                    )
                )
            )
    if hasattr(main_window, "_buyboard_engine_status_label"):
        enabled = is_buyboard_engine_enabled()
        main_window._buyboard_engine_status_label.setText(
            "Engine: ENABLED" if enabled else "Engine: OFF (board is read-only for order actions)"
        )
        main_window._buyboard_engine_status_label.setStyleSheet(
            "color: #2e7d32; font-weight: bold;" if enabled else "color: #888;"
        )


def _handle_card_dropped(main_window, payload: dict, target_status: BoardStatus) -> None:
    environment = str(payload.get("environment", ""))
    account_no = str(payload.get("account_no", ""))
    symbol = str(payload.get("symbol", ""))

    if target_status in _ENGINE_GATED_TARGET_COLUMNS and not is_buyboard_engine_enabled():
        QMessageBox.information(
            main_window,
            "Buy Board",
            "The new execution engine is not enabled yet. Use the existing "
            "Buy Dashboard tab to submit real orders until this is turned on.",
        )
        return

    projection = _lookup_projection(main_window, environment, account_no, symbol)
    if projection is None:
        QMessageBox.warning(main_window, "Buy Board", f"Could not find a card for {symbol}.")
        main_window.refresh_buyboard()
        return
    card = projection.card
    if not _payload_matches_projection(payload, projection):
        QMessageBox.warning(
            main_window,
            "Buy Board",
            "This card changed after the drag began. The board has been refreshed.",
        )
        main_window.refresh_buyboard()
        return

    current_payload = card_drag_payload(projection)
    common = _command_kwargs(current_payload)
    interaction_fingerprint = current_payload["state_fingerprint"]

    if target_status == BoardStatus.WATCHLIST:
        command = MoveToWatchlist(
            **common
        )
    elif target_status == BoardStatus.BUYLIST:
        # A backward move is presentation-only until the entry runtime has
        # consumed an identity.  From that point onward it is a cancellation
        # request and broker reconciliation alone may return the card to the
        # Buylist.
        if card.board_status == BoardStatus.ENTRY_PENDING or (
            card.board_status == BoardStatus.BUY_TODAY
            and bool(card.entry_client_order_id)
        ):
            command = CancelEntry(**common)
        else:
            command = MoveToBuylist(**common)
    elif target_status == BoardStatus.BUY_TODAY:
        command = ActivateForToday(
            **common
        )
    elif target_status == BoardStatus.OPEN_POSITION:
        if card.board_status == BoardStatus.PARTIAL_SELL:
            # With no submitted SELL this is an immediate, local withdrawal.
            # If a known SELL already exists, the runtime requests a broker
            # cancel and reconciliation alone returns the card to Open.
            command = CancelPartialSell(**common)
        else:
            command = CancelQueuedSellAll(**common)
    elif target_status == BoardStatus.PARTIAL_SELL:
        quantity = dialogs.prompt_partial_sell_quantity(main_window, card)
        if quantity is None:
            return
        command = RequestPartialSell(
            **common,
            quantity=quantity,
        )
    elif target_status == BoardStatus.SELL_ALL:
        if not dialogs.confirm_sell_all(main_window, card):
            return
        command = RequestSellAll(
            **common
        )
    else:
        QMessageBox.information(
            main_window,
            "Buy Board",
            f"{BOARD_COLUMN_TITLES[target_status]} is reached automatically once "
            "the broker confirms the corresponding event -- it cannot be set by "
            "dragging a card there.",
        )
        return

    main_window._buyboard_dispatch_command(
        command, interaction_fingerprint=interaction_fingerprint
    )


def _column_cards_sorted(main_window, board_status: BoardStatus) -> List[TradeCardState]:
    from .controller import _projection_context

    all_cards = getattr(main_window, "_buyboard_current_projections", None)
    if all_cards is None:
        all_cards = execution_workflow_service.list_board_projections(
            main_window._buyboard_engine(),
            environment="PROD",
            context=_projection_context(main_window),
        )
    return sorted(
        (
            _state(c)
            for c in all_cards
            if _state(c) is not None and _state(c).board_status == board_status
        ),
        key=lambda c: -c.kanban_priority,
    )


def _renumber_column_after_swap(
    main_window, siblings: List[TradeCardState], from_index: int, to_index: int
) -> None:
    """Fully renumbers every card in this column after swapping the
    positions of ``siblings[from_index]``/``siblings[to_index]`` (review
    finding P1-7).

    The previous version set the moved card's priority to
    ``neighbor.kanban_priority +/- 1``, which collides the instant two
    siblings already share a priority -- every card defaults to
    ``kanban_priority=0``, so that is the common case (e.g. two untouched
    cards in the same column), not a rare edge case: moving card A up past
    card B (both still 0) sets A to 1; moving a *different* card C up past
    B (still 0) later also sets C to 1, silently duplicating A's priority.
    A full renumbering to clean, strictly-descending values can never
    produce a duplicate, and self-heals any duplicates already on the
    board from before this fix.
    """
    reordered = list(siblings)
    reordered[from_index], reordered[to_index] = reordered[to_index], reordered[from_index]
    base = len(reordered) * 10
    for position, sibling in enumerate(reordered):
        target_priority = base - position * 10
        if sibling.kanban_priority == target_priority:
            continue
        command = ReorderCard(
            environment=sibling.environment,
            account_no=sibling.account_no,
            symbol=sibling.symbol,
            expected_card_version=sibling.version,
            target_priority=target_priority,
        )
        main_window._buyboard_dispatch_command(
            command,
            interaction_fingerprint=board_interaction_fingerprint(sibling),
        )


def _handle_card_context_menu(main_window, payload: dict, global_pos) -> None:
    """Right-click actions: cancelling a still-pending entry (review
    finding P1-8), stop management (review finding P1-9), and
    Kanban-priority reordering (review finding P1-8/P1-7). Commands for
    all of these already existed (:mod:`src.ui.buyboard.drag_commands`,
    :mod:`src.ui.buyboard.controller`) but nothing in the board UI ever
    exposed a Cancel Entry action, and reordering could silently create
    duplicate priorities.
    """
    environment = str(payload.get("environment", ""))
    account_no = str(payload.get("account_no", ""))
    symbol = str(payload.get("symbol", ""))

    projection = _lookup_projection(main_window, environment, account_no, symbol)
    if projection is None:
        return
    card = projection.card
    if not _payload_matches_projection(payload, projection):
        QMessageBox.warning(
            main_window, "Buy Board", "This card changed; the board has been refreshed."
        )
        main_window.refresh_buyboard()
        return
    current_payload = card_drag_payload(projection)
    common = _command_kwargs(current_payload)
    interaction_fingerprint = current_payload["state_fingerprint"]

    menu = QMenu(main_window)
    cancel_entry_action = orb_action = breakeven_action = manual_stop_action = None
    if card.board_status in (BoardStatus.BUY_TODAY, BoardStatus.ENTRY_PENDING):
        # Section 989-990 / review finding P1-8: Entry Pending is a
        # system-only drop target (no drag can reach it, and none can
        # leave it either), so a right-click action is the only way to
        # ever cancel a working entry from the board -- without this the
        # only escape was the legacy Buy Dashboard tab.
        cancel_entry_action = menu.addAction(
            "Remove from Today" if card.board_status == BoardStatus.BUY_TODAY else "Cancel Entry"
        )
        menu.addSeparator()
    if card.board_status in (BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL):
        orb_action = menu.addAction("Use Frozen ORB-Low Stop")
        breakeven_action = menu.addAction("Move Stop to Breakeven")
        manual_stop_action = menu.addAction("Set Manual Stop…")
        menu.addSeparator()

    siblings = _column_cards_sorted(main_window, card.board_status)
    index = next((i for i, sibling in enumerate(siblings) if sibling.card_key == card.card_key), None)
    move_up_action = menu.addAction("Move Up Priority")
    move_up_action.setEnabled(index is not None and index > 0)
    move_down_action = menu.addAction("Move Down Priority")
    move_down_action.setEnabled(index is not None and index < len(siblings) - 1)

    chosen = menu.exec_(global_pos)
    if chosen is None:
        return

    if chosen is cancel_entry_action:
        command = CancelEntry(
            **common
        )
        main_window._buyboard_dispatch_command(
            command, interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is orb_action:
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The new execution engine is not enabled yet."
            )
            return
        main_window._buyboard_dispatch_command(
            SetOrbStop(**common), interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is breakeven_action:
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The new execution engine is not enabled yet."
            )
            return
        command = SetBreakevenStop(
            **common
        )
        main_window._buyboard_dispatch_command(
            command, interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is manual_stop_action:
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The new execution engine is not enabled yet."
            )
            return
        price = dialogs.prompt_manual_stop_price(main_window, card)
        if price is None:
            return
        command = SetManualStop(
            **common,
            price=price,
        )
        main_window._buyboard_dispatch_command(
            command, interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is move_up_action and index is not None and index > 0:
        _renumber_column_after_swap(main_window, siblings, index, index - 1)
    elif chosen is move_down_action and index is not None and index < len(siblings) - 1:
        _renumber_column_after_swap(main_window, siblings, index, index + 1)


def _command_kwargs(payload: dict) -> dict:
    return {
        "environment": str(payload.get("environment", "")),
        "account_no": str(payload.get("account_no", "")),
        "symbol": str(payload.get("symbol", "")),
        "expected_card_version": int(payload.get("version", 0)),
        "expected_readiness_generation": int(payload.get("readiness_generation", 0)),
        "expected_ownership_version": int(payload.get("ownership_version", 0)),
        "expected_execution_owner": str(payload.get("execution_owner", "")),
        "expected_strategy_instance_id": str(payload.get("strategy_instance_id", "")),
    }


def _payload_matches_projection(payload: dict, projection) -> bool:
    """Accept storage-only revision churn, never changed actionable state."""

    rendered_fingerprint = str(payload.get("state_fingerprint", "") or "")
    if rendered_fingerprint:
        return rendered_fingerprint == board_interaction_fingerprint(projection)
    return projection.card.version == int(payload.get("version", 0) or 0)


def _lookup_projection(main_window, environment: str, account_no: str, symbol: str):
    """Resolve a gesture against the exact projection already shown to the user.

    Canonical CAS, ownership, reconciliation, and fingerprint checks still run
    in :class:`BoardCommandWorker`. Keeping this lookup in memory ensures a
    drop or context-menu selection never starts a database read on Qt's thread.
    """

    expected = (
        str(environment or "").upper(),
        str(account_no or ""),
        str(symbol or "").upper(),
    )
    for value in tuple(
        getattr(main_window, "_buyboard_current_projections", ()) or ()
    ):
        state = _state(value)
        if state is None:
            continue
        identity = (
            str(state.environment or "").upper(),
            str(state.account_no or ""),
            str(state.symbol or "").upper(),
        )
        if identity != expected:
            continue
        return (
            value
            if isinstance(value, BoardCardProjection)
            else BoardCardProjection(card=value)
        )
    return None


def _handle_external_order_adopt(main_window, external_order) -> None:
    """The sole Kanban path that can explicitly adopt an unowned order."""
    from .drag_commands import AdoptExternalOrder

    projection = _lookup_projection(
        main_window,
        external_order.environment,
        external_order.account_no,
        external_order.symbol,
    )
    answer = QMessageBox.question(
        main_window,
        "Adopt Unowned Broker Order",
        f"Adopt broker order {external_order.broker_order_id} as an audited, "
        "restricted external order? This does not silently link it to the card "
        "or grant cancel/replace permission.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    if answer != QMessageBox.Yes:
        return
    payload = (
        card_drag_payload(projection)
        if projection is not None
        else {
            "environment": external_order.environment,
            "account_no": external_order.account_no,
            "symbol": external_order.symbol,
            "version": 0,
        }
    )
    main_window._buyboard_dispatch_command(
        AdoptExternalOrder(
            **_command_kwargs(payload),
            external_order_id=external_order.external_order_id,
            adopted_by=getpass.getuser() or "unknown-operator",
        ),
        interaction_fingerprint=(
            board_interaction_fingerprint(projection)
            if projection is not None
            else ""
        ),
    )
