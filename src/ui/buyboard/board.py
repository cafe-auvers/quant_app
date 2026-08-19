"""Assembles the operator-facing Buy Board columns and refresh bar.

Widget assembly and the drag-drop -> command translation live here;
Every gesture is submitted through :mod:`src.services.execution_workflow_service`;
the widgets are rebuilt only from its authoritative projection.
"""
from __future__ import annotations

import logging
import getpass
import math
from dataclasses import dataclass
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

_POSITION_LIMIT = 30
_POSITION_BOARD_STATUSES = {
    BoardStatus.ENTRY_PENDING,
    BoardStatus.OPEN_POSITION,
    BoardStatus.PARTIAL_SELL,
    BoardStatus.SELL_ALL,
}


@dataclass(frozen=True)
class BuyboardPortfolioSummary:
    positions: int
    capital_percent: Optional[float]
    pnl_usd: Optional[float]


def build_buyboard_widget(main_window) -> None:
    root_layout = QVBoxLayout(main_window.buyboard_widget)
    root_layout.setContentsMargins(6, 4, 6, 4)

    header = QHBoxLayout()
    title = QLabel("<b>Buy Board</b>")
    header.addWidget(title)
    header.addSpacing(12)

    positions_label = QLabel(f"Positions: 0 / {_POSITION_LIMIT}")
    positions_label.setStyleSheet("font-weight: bold; color: #2e7d32;")
    capital_label = QLabel("Capital: -")
    pnl_label = QLabel("P&L: -")
    pnl_label.setToolTip(
        "Current filtered Buy Board P&L (entire Buy Board P&L)."
    )
    header.addWidget(positions_label)
    header.addSpacing(12)
    header.addWidget(capital_label)
    header.addSpacing(12)
    header.addWidget(pnl_label)
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

    refresh_btn = QPushButton("Refresh Board")
    refresh_btn.setToolTip(
        "Refresh the board projection. Broker orders and positions are reconciled automatically by the engine."
    )
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
    main_window._buyboard_positions_label = positions_label
    main_window._buyboard_capital_label = capital_label
    main_window._buyboard_pnl_label = pnl_label


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
    intraday_prices = dict(
        getattr(main_window, "latest_intraday_prices", {}) or {}
    )
    persisted_prices: Dict[str, float] = {}
    for value in tuple(
        getattr(main_window, "_buyboard_current_projections", ()) or ()
    ):
        card = _state(value)
        if card is None:
            continue
        price = _positive_number(card.market_data_last_trusted_price)
        if price is not None:
            persisted_prices[card.symbol] = price
    if market_data is None and not intraday_prices and not persisted_prices:
        return None

    def lookup(symbol: str) -> Optional[float]:
        try:
            quote = market_data.latest_quote(symbol) if market_data is not None else None
            live_price = _positive_number(
                quote.last_price if quote is not None else None
            )
            if live_price is not None:
                return live_price
        except Exception:
            # A UI repaint must never affect the market-data/runtime thread.
            logger.debug("Buy Board quote lookup failed for %s", symbol, exc_info=True)
        cached_price = _positive_number(intraday_prices.get(str(symbol).upper()))
        if cached_price is not None:
            return cached_price
        return persisted_prices.get(str(symbol).upper())

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


def _positive_number(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def calculate_portfolio_summary(
    values,
    quote_lookup: Optional[Callable[[str], Optional[float]]],
    equity_lookup: Callable[[str, str], Optional[float]],
) -> BuyboardPortfolioSummary:
    """Aggregate the visible, broker-confirmed positions on the board."""

    cards = []
    for value in values:
        card = _state(value)
        if card is None or card.board_status not in _POSITION_BOARD_STATUSES:
            continue
        if max(0, int(card.broker_quantity or 0)) <= 0:
            continue
        cards.append(card)

    if not cards:
        return BuyboardPortfolioSummary(0, 0.0, 0.0)

    accounts = {
        (card.environment, card.account_no)
        for card in cards
        if card.account_no
    }
    equities = [
        _positive_number(equity_lookup(environment, account_no))
        for environment, account_no in accounts
    ]
    total_equity = (
        sum(value for value in equities if value is not None)
        if equities and all(value is not None for value in equities)
        else None
    )

    cost_basis = 0.0
    pnl = 0.0
    capital_complete = total_equity is not None
    pnl_complete = quote_lookup is not None
    for card in cards:
        quantity = max(0, int(card.broker_quantity or 0))
        average_entry = _positive_number(card.average_entry_price)
        if average_entry is None:
            capital_complete = False
            pnl_complete = False
            continue
        cost_basis += average_entry * quantity
        if quote_lookup is None:
            continue
        try:
            current_price = _positive_number(quote_lookup(card.symbol))
        except Exception:
            current_price = None
        if current_price is None:
            pnl_complete = False
            continue
        pnl += (current_price - average_entry) * quantity

    capital_percent = (
        cost_basis / total_equity * 100.0
        if capital_complete and total_equity is not None and total_equity > 0
        else None
    )
    return BuyboardPortfolioSummary(
        positions=len(cards),
        capital_percent=capital_percent,
        pnl_usd=pnl if pnl_complete else None,
    )


def _update_portfolio_summary(
    main_window,
    visible_cards,
    all_cards=None,
    quote_lookup: Optional[Callable[[str], Optional[float]]] = None,
    equity_lookup: Optional[Callable[[str, str], Optional[float]]] = None,
) -> BuyboardPortfolioSummary:
    quote_lookup = quote_lookup if quote_lookup is not None else _quote_lookup_for(main_window)
    equity_lookup = equity_lookup or _account_equity_lookup_for(main_window)
    visible_cards = list(visible_cards)
    all_cards = visible_cards if all_cards is None else list(all_cards)
    summary = calculate_portfolio_summary(
        visible_cards,
        quote_lookup,
        equity_lookup,
    )
    entire_summary = calculate_portfolio_summary(
        all_cards,
        quote_lookup,
        equity_lookup,
    )

    positions_label = getattr(main_window, "_buyboard_positions_label", None)
    if positions_label is not None:
        positions_label.setText(
            f"Positions: {summary.positions} / {_POSITION_LIMIT}"
        )
        positions_label.setStyleSheet(
            "font-weight: bold; color: "
            + ("#c62828;" if summary.positions >= _POSITION_LIMIT else "#2e7d32;")
        )
    capital_label = getattr(main_window, "_buyboard_capital_label", None)
    if capital_label is not None:
        capital_label.setText(
            "Capital: -"
            if summary.capital_percent is None
            else f"Capital: {summary.capital_percent:.1f}%"
        )
    pnl_label = getattr(main_window, "_buyboard_pnl_label", None)
    if pnl_label is not None:
        def format_pnl(value: Optional[float]) -> str:
            if value is None:
                return "-"
            sign = "+" if value >= 0 else "-"
            return f"{sign}${abs(value):,.0f}"

        pnl_label.setText(
            f"P&L: {format_pnl(summary.pnl_usd)} "
            f"({format_pnl(entire_summary.pnl_usd)})"
        )
        if summary.pnl_usd is None:
            pnl_label.setStyleSheet("")
        else:
            pnl_label.setStyleSheet(
                "font-weight: bold; color: "
                + ("#2e7d32;" if summary.pnl_usd >= 0 else "#c62828;")
            )
    return summary


def _currently_visible_cards(main_window):
    selected_account = None
    combo = getattr(main_window, "_buyboard_account_filter_combo", None)
    if combo is not None:
        selected_account = combo.currentData()
    visible = []
    for value in tuple(
        getattr(main_window, "_buyboard_current_projections", ()) or ()
    ):
        state = _state(value)
        if state is not None and state.board_status not in set(BOARD_COLUMN_ORDER):
            continue
        if selected_account and _account_no(value) != selected_account:
            continue
        visible.append(value)
    return visible


def _all_current_board_cards(main_window):
    visible_statuses = set(BOARD_COLUMN_ORDER)
    return [
        value
        for value in tuple(
            getattr(main_window, "_buyboard_current_projections", ()) or ()
        )
        if _state(value) is None or _state(value).board_status in visible_statuses
    ]


def refresh_buyboard_live_metrics(main_window) -> int:
    """Refresh current-price metrics in-place, independently of DB projection."""

    quote_lookup = _quote_lookup_for(main_window)
    equity_lookup = _account_equity_lookup_for(main_window)
    updated = 0
    for column_list in getattr(main_window, "buyboard_columns", {}).values():
        updated += column_list.refresh_live_metrics(quote_lookup, equity_lookup)
    _update_portfolio_summary(
        main_window,
        _currently_visible_cards(main_window),
        _all_current_board_cards(main_window),
        quote_lookup,
        equity_lookup,
    )
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
    all_board_cards = list(visible_cards)
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
    _update_portfolio_summary(
        main_window,
        visible_cards,
        all_board_cards,
        quote_lookup,
        equity_lookup,
    )
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
        recovery_active = bool(
            getattr(main_window, "_buyboard_recovery_snapshot_active", False)
        )
        if recovery_active:
            main_window._buyboard_engine_status_label.setText(
                "Board: LOCAL SNAPSHOT — Kanban store unavailable; read-only"
            )
            main_window._buyboard_engine_status_label.setStyleSheet(
                "color: #ad6704; font-weight: bold;"
            )
        else:
            main_window._buyboard_engine_status_label.setText(
                "Engine: ENABLED"
                if enabled
                else "Engine: OFF (board is read-only for order actions)"
            )
            main_window._buyboard_engine_status_label.setStyleSheet(
                "color: #2e7d32; font-weight: bold;"
                if enabled
                else "color: #888;"
            )


def _handle_card_dropped(main_window, payload: dict, target_status: BoardStatus) -> None:
    environment = str(payload.get("environment", ""))
    account_no = str(payload.get("account_no", ""))
    symbol = str(payload.get("symbol", ""))

    if target_status in _ENGINE_GATED_TARGET_COLUMNS and not is_buyboard_engine_enabled():
        QMessageBox.information(
            main_window,
            "Buy Board",
            "Live order actions are unavailable because the Buy Board execution "
            "engine is not enabled on this device.",
        )
        return

    projection = _lookup_projection(main_window, environment, account_no, symbol)
    if projection is None:
        QMessageBox.warning(main_window, "Buy Board", f"Could not find a card for {symbol}.")
        main_window.refresh_buyboard()
        return
    if _is_recovery_projection(projection):
        QMessageBox.information(
            main_window,
            "Buy Board",
            "This is a read-only recovery snapshot. Board changes resume after "
            "the Kanban operational store is available.",
        )
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


def _queue_item_for_card(main_window, card: TradeCardState):
    manager = getattr(main_window, "execution_queue_manager", None)
    getter = getattr(manager, "get_item", None)
    if not callable(getter):
        return None
    return getter(card.symbol, card.environment)


def _sync_orb_plan_change(main_window, card: TradeCardState) -> None:
    refresher = getattr(main_window, "refresh_execution_queue", None)
    if callable(refresher):
        refresher(
            card.environment,
            show_log=False,
            symbols=[card.symbol],
            create_missing=False,
        )
    refresh_board = getattr(main_window, "refresh_buyboard", None)
    if callable(refresh_board):
        refresh_board()


def _show_orb_plans(main_window, card: TradeCardState) -> None:
    """Refresh and show the existing queue's ORB plans for one Today card."""

    refresher = getattr(main_window, "refresh_execution_queue", None)
    if callable(refresher):
        refresher(
            card.environment,
            symbols=[card.symbol],
            create_missing=False,
        )
    queue_item = _queue_item_for_card(main_window, card)
    if queue_item is None:
        QMessageBox.warning(
            main_window,
            "ORB Plans",
            f"No execution-queue ORB plans were found for {card.symbol}.",
        )
        return

    def lock_window(window: str) -> None:
        candidate = (getattr(queue_item, "candidates", {}) or {}).get(window)
        if candidate is None:
            return
        queue_item.locked = True
        queue_item.manual_window_lock = True
        queue_item.locked_reason = "Manual ORB window lock from Buy Board"
        queue_item.selected_window = window
        queue_item.selected_candidate = candidate
        saver = getattr(main_window, "_save_execution_queue_state", None)
        if callable(saver):
            saver()
        _sync_orb_plan_change(main_window, card)

    def unlock_auto() -> None:
        unlocker = getattr(
            main_window, "_unlock_execution_queue_item_for_auto", None
        )
        if callable(unlocker):
            unlocker(queue_item)
        else:
            from src.core.execution_queue import select_best_orb_candidate

            queue_item.locked = False
            queue_item.manual_window_lock = False
            queue_item.locked_reason = None
            selected = select_best_orb_candidate(
                getattr(queue_item, "candidates", {}) or {},
                getattr(queue_item, "selected_window", None),
                False,
            )
            queue_item.selected_candidate = selected
            queue_item.selected_window = selected.window if selected else None
            saver = getattr(main_window, "_save_execution_queue_state", None)
            if callable(saver):
                saver()
        _sync_orb_plan_change(main_window, card)

    dialogs.show_orb_plan_dialog(
        main_window,
        queue_item,
        lock_window=lock_window,
        unlock_auto=unlock_auto,
    )


def _open_card_in_tradingview(main_window, symbol: str) -> None:
    set_chart_symbol = getattr(main_window, "_set_chart_symbol", None)
    if callable(set_chart_symbol):
        set_chart_symbol(symbol)
    set_tradingview_symbol = getattr(main_window, "_set_tradingview_symbol", None)
    if callable(set_tradingview_symbol):
        set_tradingview_symbol(symbol)
    tabs = getattr(main_window, "tabs", None)
    tradingview_widget = getattr(main_window, "tradingview_widget", None)
    if tabs is not None and tradingview_widget is not None:
        tabs.setCurrentWidget(tradingview_widget)
    load_chart = getattr(main_window, "load_tradingview_chart", None)
    if callable(load_chart):
        load_chart(force=True)


def _handle_card_context_menu(main_window, payload: dict, global_pos) -> None:
    """Expose every operator action on the authoritative Kanban card."""
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
    if _is_recovery_projection(projection):
        menu = QMenu(main_window)
        chart_action = menu.addAction("Open TradingView Chart")
        if menu.exec_(global_pos) is chart_action:
            _open_card_in_tradingview(main_window, card.symbol)
        return
    current_payload = card_drag_payload(projection)
    common = _command_kwargs(current_payload)
    interaction_fingerprint = current_payload["state_fingerprint"]

    menu = QMenu(main_window)
    actions = {}
    if card.board_status == BoardStatus.BUYLIST:
        actions["activate"] = menu.addAction("Activate for Buy Today")
        actions["remove"] = menu.addAction("Move to Watchlist")
        menu.addSeparator()
    elif card.board_status == BoardStatus.BUY_TODAY:
        actions["orb_plans"] = menu.addAction("Refresh / Select ORB Plans...")
        actions["remove_today"] = menu.addAction("Remove from Today")
        menu.addSeparator()
    elif card.board_status == BoardStatus.ENTRY_PENDING:
        actions["cancel_entry"] = menu.addAction("Cancel Entry")
        menu.addSeparator()

    if card.board_status == BoardStatus.OPEN_POSITION:
        actions["partial_sell"] = menu.addAction("Partial Sell...")
        actions["sell_all"] = menu.addAction("Sell All")
        menu.addSeparator()
    elif card.board_status == BoardStatus.PARTIAL_SELL:
        actions["cancel_partial"] = menu.addAction("Cancel Partial Sell")
        menu.addSeparator()
    elif card.board_status == BoardStatus.SELL_ALL:
        actions["cancel_sell_all"] = menu.addAction("Cancel Sell All")
        menu.addSeparator()

    if card.board_status in (BoardStatus.OPEN_POSITION, BoardStatus.PARTIAL_SELL):
        actions["orb_stop"] = menu.addAction("Use Frozen ORB-Low Stop")
        actions["breakeven"] = menu.addAction("Move Stop to Breakeven")
        actions["manual_stop"] = menu.addAction("Set Manual Stop...")
        menu.addSeparator()

    actions["chart"] = menu.addAction("Open TradingView Chart")
    menu.addSeparator()

    siblings = _column_cards_sorted(main_window, card.board_status)
    index = next((i for i, sibling in enumerate(siblings) if sibling.card_key == card.card_key), None)
    actions["move_up"] = menu.addAction("Move Up Priority")
    actions["move_up"].setEnabled(index is not None and index > 0)
    actions["move_down"] = menu.addAction("Move Down Priority")
    actions["move_down"].setEnabled(
        index is not None and index < len(siblings) - 1
    )

    chosen = menu.exec_(global_pos)
    if chosen is None:
        return

    if chosen is actions.get("activate"):
        command = ActivateForToday(**common)
        main_window._buyboard_dispatch_command(
            command, interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is actions.get("remove"):
        command = MoveToWatchlist(**common)
        main_window._buyboard_dispatch_command(
            command, interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is actions.get("orb_plans"):
        _show_orb_plans(main_window, card)
    elif chosen in (
        actions.get("remove_today"),
        actions.get("cancel_entry"),
    ):
        command = CancelEntry(**common)
        main_window._buyboard_dispatch_command(
            command, interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is actions.get("partial_sell"):
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window,
                "Buy Board",
                "The Buy Board execution engine is not enabled.",
            )
            return
        quantity = dialogs.prompt_partial_sell_quantity(main_window, card)
        if quantity is None:
            return
        main_window._buyboard_dispatch_command(
            RequestPartialSell(**common, quantity=quantity),
            interaction_fingerprint=interaction_fingerprint,
        )
    elif chosen is actions.get("sell_all"):
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window,
                "Buy Board",
                "The Buy Board execution engine is not enabled.",
            )
            return
        if not dialogs.confirm_sell_all(main_window, card):
            return
        main_window._buyboard_dispatch_command(
            RequestSellAll(**common),
            interaction_fingerprint=interaction_fingerprint,
        )
    elif chosen is actions.get("cancel_partial"):
        main_window._buyboard_dispatch_command(
            CancelPartialSell(**common),
            interaction_fingerprint=interaction_fingerprint,
        )
    elif chosen is actions.get("cancel_sell_all"):
        main_window._buyboard_dispatch_command(
            CancelQueuedSellAll(**common),
            interaction_fingerprint=interaction_fingerprint,
        )
    elif chosen is actions.get("orb_stop"):
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The Buy Board execution engine is not enabled."
            )
            return
        main_window._buyboard_dispatch_command(
            SetOrbStop(**common), interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is actions.get("breakeven"):
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The Buy Board execution engine is not enabled."
            )
            return
        command = SetBreakevenStop(
            **common
        )
        main_window._buyboard_dispatch_command(
            command, interaction_fingerprint=interaction_fingerprint
        )
    elif chosen is actions.get("manual_stop"):
        if not is_buyboard_engine_enabled():
            QMessageBox.information(
                main_window, "Buy Board", "The Buy Board execution engine is not enabled."
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
    elif chosen is actions.get("chart"):
        _open_card_in_tradingview(main_window, card.symbol)
    elif chosen is actions.get("move_up") and index is not None and index > 0:
        _renumber_column_after_swap(main_window, siblings, index, index - 1)
    elif (
        chosen is actions.get("move_down")
        and index is not None
        and index < len(siblings) - 1
    ):
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


def _is_recovery_projection(projection: BoardCardProjection) -> bool:
    return any(
        "last local snapshot" in str(reason).casefold()
        for reason in projection.engine_restrictions
    )


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
