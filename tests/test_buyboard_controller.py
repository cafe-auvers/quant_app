"""Tests for src.ui.buyboard.controller.apply_board_command.

Pure backend-command validation/application -- no QApplication needed, even
though the module the code lives in also defines a Qt mixin (importing
PyQt5 at module scope is fine headlessly; nothing here instantiates a widget).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.services import trade_card_repository as repo
from src.services.trade_card_repository import (
    TradeCardNotFoundError,
    TradeCardVersionConflictError,
)
from src.ui.buyboard.controller import CommandRejectedError, apply_board_command
from src.ui.buyboard.drag_commands import (
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
)


@pytest.fixture(autouse=True)
def _isolate_local_trade_card_snapshot(monkeypatch, tmp_path):
    """create_trade_card/update_trade_card (called both directly below and
    indirectly through every apply_board_command(...) call) write a local
    backup snapshot alongside the database write (review finding P1-13).
    Redirect it to an isolated per-test file so none of the ~20
    apply_board_command calls in this module accidentally touch the real
    production data/trade_cards.json.
    """
    monkeypatch.setattr(repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "trade_cards.json")


def _make_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'cards.db'}", future=True, poolclass=NullPool)


def _seed(engine, **overrides):
    fields = dict(environment="PROD", account_no="1", symbol="AAPL")
    fields.update(overrides)
    return repo.create_trade_card(engine, TradeCardState(**fields))


def _cmd(cls, card, **kw):
    return cls(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        expected_card_version=card.version,
        **kw,
    )


def test_watchlist_to_buylist(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.WATCHLIST)
    result = apply_board_command(engine, _cmd(MoveToBuylist, card))
    assert result.board_status == BoardStatus.BUYLIST
    assert result.buylist_member is True
    assert result.version == 2


def test_buylist_to_buy_today_activation(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUYLIST)
    result = apply_board_command(engine, _cmd(ActivateForToday, card))
    assert result.board_status == BoardStatus.BUY_TODAY


def test_illegal_transition_rejected(tmp_path):
    """Watchlist -> Buy Today directly is not on the graph (spec section 17-31)."""
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.WATCHLIST)
    with pytest.raises(CommandRejectedError):
        apply_board_command(engine, _cmd(ActivateForToday, card))


def test_stale_command_version_rejected(tmp_path):
    """Spec section 317: "The backend must reject stale commands when
    expected_card_version does not match the current version." """
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.WATCHLIST)
    apply_board_command(engine, _cmd(MoveToBuylist, card))  # bumps to version 2

    with pytest.raises(TradeCardVersionConflictError):
        apply_board_command(engine, _cmd(ActivateForToday, card))  # still says version 1


def test_command_for_missing_card_raises_not_found(tmp_path):
    engine = _make_engine(tmp_path)
    command = MoveToBuylist(
        environment="PROD", account_no="1", symbol="ZZZZ", expected_card_version=1
    )
    with pytest.raises(TradeCardNotFoundError):
        apply_board_command(engine, command)


def test_cancel_entry_from_buy_today_returns_to_buylist_immediately(tmp_path):
    """No order can exist yet for a BUY_TODAY card -- safe to move at once."""
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUY_TODAY)
    result = apply_board_command(engine, _cmd(CancelEntry, card))
    assert result.board_status == BoardStatus.BUYLIST


def test_cancel_entry_from_entry_pending_only_flags_the_request(tmp_path):
    """Section 989-990: must not orphan a possibly-still-working order by
    moving the card before the engine has actually cancelled it."""
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.ENTRY_PENDING)
    result = apply_board_command(engine, _cmd(CancelEntry, card))
    assert result.board_status == BoardStatus.ENTRY_PENDING
    assert result.entry_block_reason == "cancel_requested"


def test_move_to_buylist_from_entry_pending_is_rejected(tmp_path):
    """The one place a bare MoveToBuylist must never reach -- CancelEntry
    is the only legal way off an unresolved Entry Pending card."""
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.ENTRY_PENDING)
    with pytest.raises(CommandRejectedError):
        apply_board_command(engine, _cmd(MoveToBuylist, card))


def test_request_sell_all_sets_exit_all_required(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=100,
        orderable_quantity=100,
    )
    result = apply_board_command(engine, _cmd(RequestSellAll, card))
    assert result.board_status == BoardStatus.SELL_ALL
    assert result.exit_all_required is True


# --- Partial sell (spec section 563-579) ------------------------------------


def test_partial_sell_within_range_moves_to_partial_sell_column(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=300,
        orderable_quantity=300,
    )
    result = apply_board_command(engine, _cmd(RequestPartialSell, card, quantity=100))
    assert result.board_status == BoardStatus.PARTIAL_SELL
    assert result.pending_partial_sell_quantity == 100


def test_partial_sell_at_full_quantity_converts_to_sell_all(tmp_path):
    """Spec section 576-579: requested_quantity >= orderable -> Sell All."""
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=300,
        orderable_quantity=300,
    )
    result = apply_board_command(engine, _cmd(RequestPartialSell, card, quantity=300))
    assert result.board_status == BoardStatus.SELL_ALL
    assert result.exit_all_required is True
    assert result.pending_partial_sell_quantity == 0


def test_partial_sell_over_orderable_also_converts_to_sell_all(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        broker_quantity=300,
        orderable_quantity=300,
    )
    result = apply_board_command(engine, _cmd(RequestPartialSell, card, quantity=999))
    assert result.board_status == BoardStatus.SELL_ALL


def test_partial_sell_zero_quantity_rejected(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine, board_status=BoardStatus.OPEN_POSITION, broker_quantity=100, orderable_quantity=100
    )
    with pytest.raises(CommandRejectedError):
        apply_board_command(engine, _cmd(RequestPartialSell, card, quantity=0))


def test_partial_sell_from_non_open_position_rejected(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUYLIST)
    with pytest.raises(CommandRejectedError):
        apply_board_command(engine, _cmd(RequestPartialSell, card, quantity=10))


def test_unsubmitted_partial_sell_can_be_withdrawn_to_open_position(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.PARTIAL_SELL,
        position_runtime_status=PositionRuntimeStatus.PARTIAL_EXIT_PENDING,
        broker_quantity=300,
        orderable_quantity=300,
        pending_partial_sell_quantity=100,
    )

    result = apply_board_command(engine, _cmd(CancelPartialSell, card))

    assert result.board_status == BoardStatus.OPEN_POSITION
    assert result.position_runtime_status == PositionRuntimeStatus.OPEN
    assert result.pending_partial_sell_quantity == 0
    assert result.broker_quantity == 300


def test_partial_sell_with_durable_identity_waits_for_cancel_reconciliation(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.PARTIAL_SELL,
        position_runtime_status=PositionRuntimeStatus.PARTIAL_EXIT_PENDING,
        broker_quantity=300,
        orderable_quantity=300,
        pending_partial_sell_quantity=100,
        reserved_sell_quantity=100,
        exit_attempt_group_id="G",
        exit_client_order_id="CID-1",
        exit_pending_attempt_number=1,
    )

    result = apply_board_command(engine, _cmd(CancelPartialSell, card))

    assert result.board_status == BoardStatus.PARTIAL_SELL
    assert result.pending_partial_sell_quantity == 0
    assert result.exit_client_order_id == "CID-1"


def test_partial_sell_can_be_replaced_by_sell_all_without_stale_quantity(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.PARTIAL_SELL,
        position_runtime_status=PositionRuntimeStatus.PARTIAL_EXIT_PENDING,
        broker_quantity=300,
        orderable_quantity=300,
        pending_partial_sell_quantity=100,
    )

    result = apply_board_command(engine, _cmd(RequestSellAll, card))

    assert result.board_status == BoardStatus.SELL_ALL
    assert result.exit_all_required is True
    assert result.pending_partial_sell_quantity == 0


def test_unsubmitted_sell_all_can_be_reduced_directly_to_partial_sell(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.SELL_ALL,
        position_runtime_status=PositionRuntimeStatus.QUEUED_FOR_OPEN,
        broker_quantity=300,
        orderable_quantity=300,
        sell_all_at_market_open=True,
        exit_all_required=True,
        exit_attempt_group_id="OLD-SELL-ALL",
        exit_attempt_count=2,
        next_exit_retry_at="2026-08-18T13:30:00+00:00",
        last_exit_error="prior explicit rejection",
    )

    result = apply_board_command(engine, _cmd(RequestPartialSell, card, quantity=100))

    assert result.board_status == BoardStatus.PARTIAL_SELL
    assert result.position_runtime_status == PositionRuntimeStatus.PARTIAL_EXIT_PENDING
    assert result.pending_partial_sell_quantity == 100
    assert result.sell_all_at_market_open is False
    assert result.exit_all_required is False
    assert result.exit_attempt_group_id == ""
    assert result.exit_attempt_count == 0
    assert result.next_exit_retry_at is None
    assert result.last_exit_error == ""


@pytest.mark.parametrize(
    "durable_field,durable_value",
    [
        ("exit_client_order_id", "SELL-CID-1"),
        ("exit_pending_attempt_number", 1),
        ("reserved_sell_quantity", 300),
        ("exit_cancel_command_id", "CANCEL-1"),
    ],
)
def test_sell_all_to_partial_rejects_a_durable_exit_lifecycle(
    tmp_path, durable_field, durable_value
):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.SELL_ALL,
        position_runtime_status=PositionRuntimeStatus.LIQUIDATING,
        broker_quantity=300,
        orderable_quantity=300,
        exit_all_required=True,
        **{durable_field: durable_value},
    )

    with pytest.raises(CommandRejectedError, match="durable execution lifecycle"):
        apply_board_command(engine, _cmd(RequestPartialSell, card, quantity=100))


# --- Stops (spec section 605-659) -------------------------------------------


def test_set_breakeven_stop_requires_open_position(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUYLIST)
    with pytest.raises(CommandRejectedError):
        apply_board_command(engine, _cmd(SetBreakevenStop, card))


def test_set_breakeven_stop_records_intent(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        average_entry_price=100.0,
    )
    result = apply_board_command(engine, _cmd(SetBreakevenStop, card))
    assert result.stop_type is None
    assert result.pending_stop_type == StopType.BREAKEVEN
    assert result.pending_stop_price > result.average_entry_price


def test_manual_stop_cannot_widen_risk(tmp_path):
    """Spec section 646-659: reject requested_manual_stop < minimum_manual_stop."""
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )
    with pytest.raises(CommandRejectedError):
        apply_board_command(engine, _cmd(SetManualStop, card, price=90.0))


def test_manual_stop_can_tighten_risk(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )
    result = apply_board_command(engine, _cmd(SetManualStop, card, price=98.0))
    assert result.stop_type == StopType.ORB_LOW
    assert result.active_stop_price == 95.0
    assert result.pending_stop_type == StopType.MANUAL_PRICE
    assert result.pending_stop_price == 98.0


# --- Cancel queued Sell All (spec section 302-304, 719-724) -----------------


def test_cancel_queued_sell_all_returns_to_open_position(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.SELL_ALL,
        sell_all_at_market_open=True,
        exit_all_required=True,
    )
    result = apply_board_command(engine, _cmd(CancelQueuedSellAll, card))
    assert result.board_status == BoardStatus.OPEN_POSITION
    assert result.sell_all_at_market_open is False
    assert result.exit_all_required is False


def test_cancel_queued_sell_all_refuses_when_actively_working(tmp_path):
    """A Sell All that is not premarket-queued is actively working at the
    broker -- cancelling that path is a different, engine-owned flow, not a
    board-level drag."""
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.SELL_ALL, sell_all_at_market_open=False)
    with pytest.raises(CommandRejectedError):
        apply_board_command(engine, _cmd(CancelQueuedSellAll, card))


# --- Reorder (spec section 379, code review finding P1-8) ------------------


def test_reorder_card_sets_kanban_priority(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUY_TODAY, kanban_priority=0)
    result = apply_board_command(engine, _cmd(ReorderCard, card, target_priority=5))
    assert result.kanban_priority == 5


def test_reorder_card_does_not_change_board_status(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUY_TODAY, kanban_priority=0)
    result = apply_board_command(engine, _cmd(ReorderCard, card, target_priority=5))
    assert result.board_status == BoardStatus.BUY_TODAY


def test_reorder_card_respects_stale_version(tmp_path):
    engine = _make_engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUY_TODAY, kanban_priority=0)
    apply_board_command(engine, _cmd(ReorderCard, card, target_priority=1))  # bumps version
    with pytest.raises(TradeCardVersionConflictError):
        apply_board_command(engine, _cmd(ReorderCard, card, target_priority=2))  # stale version
