"""Tests for src.ui.buyboard.board (review findings P1-7, P1-8)."""
from __future__ import annotations

import copy
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMenu, QMessageBox, QWidget
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.board_workflow import BoardCardProjection, BoardExternalOrderProjection
from src.core.discovered_external_order import new_discovered_external_order
from src.core.execution_order_record import ExecutionOrderStatus
from src.core.order_state import OrderSide
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import trade_card_repository as repo
from src.ui.buyboard import board as board_module
from src.ui.buyboard.card import card_drag_payload
from src.ui.buyboard.columns import BOARD_COLUMN_ORDER, BoardColumnList
from src.ui.buyboard.drag_commands import (
    CancelEntry,
    CancelPartialSell,
    ReorderCard,
    RequestPartialSell,
)

_APP = None


def _ensure_app():
    global _APP
    _APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_local_trade_card_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "trade_cards.json")


def _card(**overrides):
    fields = dict(environment="PROD", account_no="1", symbol="AAPL", kanban_priority=0)
    fields.update(overrides)
    return TradeCardState(**fields)


def test_operator_board_hides_watchlist_and_closed_columns():
    assert BOARD_COLUMN_ORDER == [
        BoardStatus.BUYLIST,
        BoardStatus.BUY_TODAY,
        BoardStatus.ENTRY_PENDING,
        BoardStatus.OPEN_POSITION,
        BoardStatus.PARTIAL_SELL,
        BoardStatus.SELL_ALL,
    ]


def test_hidden_lifecycle_cards_are_not_rendered_but_remain_in_projection_cache():
    rendered = {status: [] for status in BOARD_COLUMN_ORDER}

    class _Column:
        def __init__(self, status):
            self.status = status

        def set_cards(self, values, _quote_lookup, _equity_lookup):
            rendered[self.status] = list(values)

    watchlist = _card(symbol="WATCH", board_status=BoardStatus.WATCHLIST)
    buylist = _card(symbol="BUY", board_status=BoardStatus.BUYLIST)
    closed = _card(symbol="DONE", board_status=BoardStatus.CLOSED)
    window = SimpleNamespace(
        buyboard_columns={status: _Column(status) for status in BOARD_COLUMN_ORDER}
    )

    board_module.populate_buyboard_columns(window, [watchlist, buylist, closed])

    assert rendered[BoardStatus.BUYLIST] == [buylist]
    assert all(
        value not in values
        for value in (watchlist, closed)
        for values in rendered.values()
    )
    assert window._buyboard_current_projections == (watchlist, buylist, closed)


def test_standalone_broker_warning_remains_visible_when_watchlist_is_hidden():
    rendered = {status: [] for status in BOARD_COLUMN_ORDER}

    class _Column:
        def __init__(self, status):
            self.status = status

        def set_cards(self, values, _quote_lookup, _equity_lookup):
            rendered[self.status] = list(values)

    external = BoardExternalOrderProjection(
        order=new_discovered_external_order(
            environment="PROD",
            account_no="1",
            symbol="EXT",
            side=OrderSide.SELL,
            broker_order_id="BROKER-1",
            quantity_requested=1,
            broker_status=ExecutionOrderStatus.WORKING,
        )
    )
    window = SimpleNamespace(
        buyboard_columns={status: _Column(status) for status in BOARD_COLUMN_ORDER}
    )

    board_module.populate_buyboard_columns(window, [external])

    assert rendered[BoardStatus.OPEN_POSITION] == [external]


def test_projection_filter_skips_hidden_cards_before_per_card_queries(
    tmp_path, monkeypatch
):
    engine = _make_engine(tmp_path)
    repo.create_trade_card(
        engine,
        _card(symbol="WATCH", board_status=BoardStatus.WATCHLIST),
    )
    visible = repo.create_trade_card(
        engine,
        _card(symbol="BUY", board_status=BoardStatus.BUYLIST),
    )
    projected_symbols = []

    def project(_engine, card, *, context=None):
        projected_symbols.append(card.symbol)
        return BoardCardProjection(card=card)

    monkeypatch.setattr(
        board_module.execution_workflow_service,
        "project_board_card",
        project,
    )

    projections = board_module.execution_workflow_service.list_board_projections(
        engine,
        board_statuses=BOARD_COLUMN_ORDER,
    )

    assert projected_symbols == [visible.symbol]
    assert [projection.card.symbol for projection in projections] == [visible.symbol]


class _FakeMainWindow(QWidget):
    """Just enough surface for _renumber_column_after_swap /
    _handle_card_context_menu. A real (if invisible) QWidget subclass --
    board.py constructs QMenu(main_window), which requires a real
    QWidget-or-None parent.
    """

    def __init__(self, engine, *, cards=()):
        _ensure_app()
        super().__init__()
        self._engine = engine
        self.dispatched = []
        self.refresh_count = 0
        # Applies a dispatched ReorderCard's target_priority directly onto
        # the matching card object, the same effect the real
        # apply_board_command -> repository round trip has -- so tests can
        # assert on the resulting priorities without re-implementing that
        # plumbing themselves.
        self._cards_by_symbol = {card.symbol: card for card in cards}
        self._buyboard_current_projections = tuple(
            BoardCardProjection(card=card) for card in cards
        )

    def _buyboard_engine(self):
        return self._engine

    def refresh_buyboard(self):
        self.refresh_count += 1

    def _buyboard_dispatch_command(self, command, **_kwargs):
        self.dispatched.append(command)
        from src.ui.buyboard.drag_commands import ReorderCard

        if isinstance(command, ReorderCard):
            card = self._cards_by_symbol.get(command.symbol)
            if card is not None:
                card.kanban_priority = command.target_priority
        return True


# --- P1-7: renumbering never produces duplicate priorities ------------------


def test_renumber_after_swap_gives_every_sibling_a_distinct_priority():
    """The historical bug: every card defaults to kanban_priority=0, so
    neighbor+/-1 arithmetic collides the moment a *second* card is moved
    past a still-zero neighbor."""
    low = _card(symbol="LOW", kanban_priority=0)
    mid = _card(symbol="MID", kanban_priority=0)
    high = _card(symbol="HIGH", kanban_priority=0)
    window = _FakeMainWindow(engine=None, cards=[high, mid, low])
    siblings = [high, mid, low]  # already sorted -kanban_priority (all tied at 0)

    # Move LOW (index 2) up past MID (index 1).
    board_module._renumber_column_after_swap(window, siblings, 2, 1)

    # Now move HIGH (index 0 in the *original* list -- simulating a second,
    # independent right-click) down past whatever is now above it.
    window.dispatched.clear()
    board_module._renumber_column_after_swap(window, [high, mid, low], 0, 1)

    final_priorities = [high.kanban_priority, mid.kanban_priority, low.kanban_priority]
    assert len(set(final_priorities)) == 3  # never a duplicate


def test_renumber_after_swap_dispatches_only_for_cards_whose_priority_changed():
    # base = 3 * 10 = 30 -> target priorities by position are 30/20/10.
    # HIGH already sits at position 0's target (20 is NOT 30 though -- pick
    # values where exactly one card's target coincidentally already
    # matches its current value, to prove that one is skipped).
    high = _card(symbol="HIGH", kanban_priority=20)  # position 1 after the swap -> target 20, unchanged
    mid = _card(symbol="MID", kanban_priority=10)  # position 0 after the swap -> target 30, changed
    low = _card(symbol="LOW", kanban_priority=0)  # position 2, unaffected by the swap -> target 10, changed
    window = _FakeMainWindow(engine=None, cards=[high, mid, low])
    siblings = [high, mid, low]  # display order: HIGH, MID, LOW

    # Swap index 1 (MID) and index 0 (HIGH) -- MID moves to the top.
    board_module._renumber_column_after_swap(window, siblings, 1, 0)

    dispatched_symbols = {cmd.symbol for cmd in window.dispatched}
    assert dispatched_symbols == {"MID", "LOW"}
    assert "HIGH" not in dispatched_symbols  # its target priority (20) already matched


def test_renumber_after_swap_actually_reorders_the_column():
    high = _card(symbol="HIGH", kanban_priority=20)
    mid = _card(symbol="MID", kanban_priority=10)
    low = _card(symbol="LOW", kanban_priority=0)
    window = _FakeMainWindow(engine=None, cards=[high, mid, low])
    siblings = [high, mid, low]  # display order: HIGH, MID, LOW (highest priority first)

    # Swap index 2 (LOW) and index 1 (MID): LOW moves ahead of MID in
    # display order, HIGH is untouched and stays on top.
    board_module._renumber_column_after_swap(window, siblings, 2, 1)

    assert high.kanban_priority > low.kanban_priority > mid.kanban_priority


def test_renumber_commands_carry_the_correct_card_version_for_optimistic_concurrency():
    a = _card(symbol="AAA", kanban_priority=5, version=3)
    b = _card(symbol="BBB", kanban_priority=0, version=7)
    window = _FakeMainWindow(engine=None, cards=[a, b])

    board_module._renumber_column_after_swap(window, [a, b], 0, 1)

    by_symbol = {cmd.symbol: cmd for cmd in window.dispatched}
    assert by_symbol["AAA"].expected_card_version == 3
    assert by_symbol["BBB"].expected_card_version == 7
    assert all(isinstance(cmd, ReorderCard) for cmd in window.dispatched)


def test_priority_menu_uses_rendered_projection_cache_without_another_db_read(
    monkeypatch,
):
    high = _card(symbol="HIGH", board_status=BoardStatus.BUYLIST, kanban_priority=20)
    low = _card(symbol="LOW", board_status=BoardStatus.BUYLIST, kanban_priority=10)
    window = _FakeMainWindow(engine=None, cards=[high, low])
    window._buyboard_current_projections = (
        BoardCardProjection(card=high),
        BoardCardProjection(card=low),
    )
    monkeypatch.setattr(
        board_module.execution_workflow_service,
        "list_board_projections",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("context-menu priority lookup hit the database")
        ),
    )

    assert board_module._column_cards_sorted(window, BoardStatus.BUYLIST) == [
        high,
        low,
    ]


# --- P1-8: Cancel Entry / Remove from Today are exposed in the UI ----------


def _make_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'cards.db'}", future=True, poolclass=NullPool)


def _find_action_by_text(menu: QMenu, text: str):
    for action in menu.actions():
        if action.text() == text:
            return action
    return None


def test_context_menu_offers_remove_from_today_for_buy_today_card(tmp_path, monkeypatch):
    _ensure_app()
    engine = _make_engine(tmp_path)
    card = repo.create_trade_card(engine, _card(board_status=BoardStatus.BUY_TODAY))
    window = _FakeMainWindow(engine, cards=[card])

    captured_menu = {}

    def fake_exec(self, pos):
        captured_menu["menu"] = self
        return _find_action_by_text(self, "Remove from Today")

    monkeypatch.setattr(QMenu, "exec_", fake_exec)

    payload = {
        "environment": card.environment, "account_no": card.account_no,
        "symbol": card.symbol, "version": card.version,
    }
    board_module._handle_card_context_menu(window, payload, None)

    assert _find_action_by_text(captured_menu["menu"], "Remove from Today") is not None
    assert len(window.dispatched) == 1
    assert isinstance(window.dispatched[0], CancelEntry)


def test_context_menu_offers_cancel_entry_for_entry_pending_card(tmp_path, monkeypatch):
    _ensure_app()
    engine = _make_engine(tmp_path)
    card = repo.create_trade_card(engine, _card(board_status=BoardStatus.ENTRY_PENDING))
    window = _FakeMainWindow(engine, cards=[card])

    monkeypatch.setattr(QMenu, "exec_", lambda self, pos: _find_action_by_text(self, "Cancel Entry"))

    payload = {
        "environment": card.environment, "account_no": card.account_no,
        "symbol": card.symbol, "version": card.version,
    }
    board_module._handle_card_context_menu(window, payload, None)

    assert len(window.dispatched) == 1
    assert isinstance(window.dispatched[0], CancelEntry)


def test_context_menu_has_no_cancel_entry_action_for_open_position(tmp_path, monkeypatch):
    _ensure_app()
    engine = _make_engine(tmp_path)
    card = repo.create_trade_card(
        engine, _card(board_status=BoardStatus.OPEN_POSITION, broker_quantity=10, average_entry_price=100.0)
    )
    window = _FakeMainWindow(engine, cards=[card])

    captured_menu = {}

    def fake_exec(self, pos):
        captured_menu["menu"] = self
        return None  # user dismisses the menu

    monkeypatch.setattr(QMenu, "exec_", fake_exec)

    payload = {
        "environment": card.environment, "account_no": card.account_no,
        "symbol": card.symbol, "version": card.version,
    }
    board_module._handle_card_context_menu(window, payload, None)

    assert _find_action_by_text(captured_menu["menu"], "Cancel Entry") is None
    assert _find_action_by_text(captured_menu["menu"], "Remove from Today") is None
    assert _find_action_by_text(captured_menu["menu"], "Move Stop to Breakeven") is not None


def test_dragging_partial_sell_to_open_dispatches_partial_withdrawal(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    card = repo.create_trade_card(
        engine,
        _card(
            board_status=BoardStatus.PARTIAL_SELL,
            broker_quantity=300,
            orderable_quantity=300,
            pending_partial_sell_quantity=100,
        ),
    )
    window = _FakeMainWindow(engine)
    monkeypatch.setattr(
        board_module,
        "_lookup_projection",
        lambda *args: BoardCardProjection(card=card),
    )

    board_module._handle_card_dropped(
        window,
        {
            "environment": card.environment,
            "account_no": card.account_no,
            "symbol": card.symbol,
            "version": card.version,
        },
        BoardStatus.OPEN_POSITION,
    )

    assert len(window.dispatched) == 1
    assert isinstance(window.dispatched[0], CancelPartialSell)


def test_drop_uses_rendered_projection_without_database_read(monkeypatch):
    card = _card(board_status=BoardStatus.BUYLIST, version=4)
    projection = BoardCardProjection(card=card)
    window = _FakeMainWindow(engine=object(), cards=[card])
    window._buyboard_current_projections = (projection,)
    monkeypatch.setattr(
        board_module.execution_workflow_service,
        "get_board_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("drop handler queried the database")
        ),
    )

    board_module._handle_card_dropped(
        window,
        card_drag_payload(projection),
        BoardStatus.BUY_TODAY,
    )

    assert len(window.dispatched) == 1
    assert window.dispatched[0].symbol == "AAPL"


def test_dragging_sell_all_to_partial_sell_prompts_and_dispatches_reduction(
    tmp_path, monkeypatch
):
    _ensure_app()
    engine = _make_engine(tmp_path)
    card = repo.create_trade_card(
        engine,
        _card(
            board_status=BoardStatus.SELL_ALL,
            broker_quantity=300,
            orderable_quantity=300,
            sell_all_at_market_open=True,
            exit_all_required=True,
        ),
    )
    window = _FakeMainWindow(engine)
    monkeypatch.setattr(
        board_module,
        "_lookup_projection",
        lambda *args: BoardCardProjection(card=card),
    )
    monkeypatch.setattr(
        board_module.dialogs,
        "prompt_partial_sell_quantity",
        lambda *_args: 100,
    )
    monkeypatch.setattr(board_module, "is_buyboard_engine_enabled", lambda: True)

    board_module._handle_card_dropped(
        window,
        card_drag_payload(BoardCardProjection(card=card)),
        BoardStatus.PARTIAL_SELL,
    )

    assert len(window.dispatched) == 1
    assert isinstance(window.dispatched[0], RequestPartialSell)
    assert window.dispatched[0].quantity == 100


def test_drag_tolerates_equivalent_revision_and_uses_current_fences(
    tmp_path, monkeypatch
):
    engine = _make_engine(tmp_path)
    rendered = repo.create_trade_card(
        engine,
        _card(
            board_status=BoardStatus.PARTIAL_SELL,
            broker_quantity=300,
            orderable_quantity=300,
            pending_partial_sell_quantity=100,
        ),
    )
    payload = card_drag_payload(BoardCardProjection(card=copy.deepcopy(rendered)))
    current = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    repo.update_trade_card(engine, current, expected_version=current.version)
    assert current.version == rendered.version + 1

    window = _FakeMainWindow(engine)
    monkeypatch.setattr(
        board_module,
        "_lookup_projection",
        lambda *args: BoardCardProjection(card=current),
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    board_module._handle_card_dropped(
        window, payload, BoardStatus.OPEN_POSITION
    )

    assert warnings == []
    assert len(window.dispatched) == 1
    assert window.dispatched[0].expected_card_version == current.version


def test_drag_still_rejects_a_real_lifecycle_change(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)
    rendered = repo.create_trade_card(
        engine,
        _card(
            board_status=BoardStatus.PARTIAL_SELL,
            broker_quantity=300,
            orderable_quantity=300,
            pending_partial_sell_quantity=100,
        ),
    )
    payload = card_drag_payload(BoardCardProjection(card=copy.deepcopy(rendered)))
    current = repo.get_trade_card(engine, "PROD", "1", "AAPL")
    current.pending_partial_sell_quantity = 90
    repo.update_trade_card(engine, current, expected_version=current.version)

    window = _FakeMainWindow(engine)
    monkeypatch.setattr(
        board_module,
        "_lookup_projection",
        lambda *args: BoardCardProjection(card=current),
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    board_module._handle_card_dropped(
        window, payload, BoardStatus.OPEN_POSITION
    )

    assert len(warnings) == 1
    assert window.dispatched == []


def test_equivalent_refresh_reuses_widget_and_updates_payload_and_quote():
    _ensure_app()
    column = BoardColumnList(BoardStatus.OPEN_POSITION, lambda *_args: None)
    rendered = _card(
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=10,
        orderable_quantity=10,
        average_entry_price=100.0,
        version=7,
    )

    assert column.set_cards([rendered], lambda _symbol: 100.0) is True
    item = column.item(0)
    widget = column.itemWidget(item)
    assert "+0.00%" in widget._info_label.text()

    refreshed = copy.deepcopy(rendered)
    refreshed.version = 8
    refreshed.updated_at = refreshed.updated_at.replace(microsecond=1)
    refreshed.market_data_last_trusted_price = 110.0
    refreshed.market_data_last_trusted_at = refreshed.updated_at
    assert column.set_cards([refreshed], lambda _symbol: 110.0) is False

    assert column.itemWidget(column.item(0)) is widget
    assert column.item(0).data(Qt.UserRole)["version"] == 8
    assert "+10.00%" in widget._info_label.text()


def test_pending_card_shows_saving_state_and_disables_repeat_drag():
    _ensure_app()
    column = BoardColumnList(BoardStatus.BUYLIST, lambda *_args: None)
    card = _card(board_status=BoardStatus.BUYLIST)
    column.set_cards([card])
    item = column.item(0)
    widget = column.itemWidget(item)

    column.set_pending_card_keys({card.card_key})

    assert widget._pending is True
    assert widget._pending_label.isHidden() is False
    assert not bool(item.flags() & Qt.ItemIsDragEnabled)

    column.set_pending_card_keys(set())

    assert widget._pending is False
    assert widget._pending_label.isHidden() is True
    assert bool(item.flags() & Qt.ItemIsDragEnabled)
