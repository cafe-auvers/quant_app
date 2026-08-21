from __future__ import annotations

import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.board_workflow import (
    BoardActionContext,
    ClearBreakoutPrice,
    SetBreakoutPrice,
)
from src.core.execution_order_record import ExecutionOrderRecord
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import trade_card_repository as card_repo
from src.services.execution_workflow_service import (
    BoardCommandRejectedError,
    request_board_action,
)
from src.services.execution_order_repository import record_execution_order
from src.services.operator_command_service import (
    deserialize_board_command,
    enqueue_board_operator_command,
    operator_command_type_for_board_command,
    process_next_board_operator_command,
    serialize_board_command,
)
from src.services.operator_commands import OperatorCommandStatus, OperatorCommandType
from src.services.state_sync import (
    LocalDeviceRole,
    claim_main_device,
    set_operator_control,
)


@pytest.fixture
def engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'breakout-plan.db'}",
        future=True,
        poolclass=NullPool,
    )


@pytest.fixture(autouse=True)
def isolate_card_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(
        card_repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "trade_cards.json"
    )


def _context(*, market_open: bool | None = False, operator: bool = True):
    return BoardActionContext(
        regular_session_open=market_open,
        local_operator_control=operator,
    )


def _seed(engine, **overrides):
    values = {
        "environment": "PROD",
        "account_no": "1",
        "symbol": "AAPL",
        "board_status": BoardStatus.BUYLIST,
        "buylist_member": True,
    }
    values.update(overrides)
    return card_repo.create_trade_card(engine, TradeCardState(**values))


def _set(card=None, *, price=101.5, buffer_pct=0.001, version=None):
    return SetBreakoutPrice(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        expected_card_version=(
            int(version) if version is not None else int(card.version if card else 0)
        ),
        price=price,
        buffer_pct=buffer_pct,
    )


def _clear(card):
    return ClearBreakoutPrice(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        expected_card_version=card.version,
    )


def test_set_missing_symbol_atomically_creates_passive_buylist(engine):
    result = request_board_action(
        engine, _set(buffer_pct=0.005), context=_context()
    )

    assert result.card.version == 1
    assert result.card.board_status == BoardStatus.BUYLIST
    assert result.card.buylist_member is True
    assert result.card.watchlist_member is False
    assert result.card.breakout_price == 101.5
    assert result.card.buffer_pct == pytest.approx(0.005)
    assert result.card.entry_runtime_status is None


def test_existing_watchlist_is_normalized_to_buylist(engine):
    card = _seed(
        engine,
        board_status=BoardStatus.WATCHLIST,
        buylist_member=False,
        watchlist_member=True,
    )

    result = request_board_action(engine, _set(card), context=_context())

    assert result.card.board_status == BoardStatus.BUYLIST
    assert result.card.watchlist_member is False
    assert result.card.buylist_member is True


@pytest.mark.parametrize(
    "context, expected_text",
    [
        (_context(operator=False), "Operator Control"),
        (_context(market_open=None), "session state is unavailable"),
    ],
)
def test_breakout_mutation_fails_closed_without_authority_or_session_truth(
    engine, context, expected_text
):
    with pytest.raises(BoardCommandRejectedError, match=expected_text):
        request_board_action(engine, _set(), context=context)

    assert card_repo.get_trade_card(engine, "PROD", "1", "AAPL") is None


@pytest.mark.parametrize("price", [0.0, -1.0, math.inf, math.nan])
def test_set_rejects_non_finite_or_nonpositive_price(engine, price):
    with pytest.raises(BoardCommandRejectedError, match="finite positive"):
        request_board_action(engine, _set(price=price), context=_context())


@pytest.mark.parametrize("buffer_pct", [-0.001, 1.001, math.inf, math.nan])
def test_set_rejects_invalid_buffer(engine, buffer_pct):
    with pytest.raises(BoardCommandRejectedError, match="ORB buffer"):
        request_board_action(
            engine, _set(buffer_pct=buffer_pct), context=_context()
        )


def test_existing_target_revision_keeps_its_frozen_buffer(engine):
    card = _seed(engine, breakout_price=100.0, buffer_pct=0.002)

    result = request_board_action(
        engine,
        _set(card, price=102.0, buffer_pct=0.01),
        context=_context(),
    )

    assert result.card.breakout_price == 102.0
    assert result.card.buffer_pct == pytest.approx(0.002)


def test_passive_buylist_target_can_be_planned_during_market_hours(engine):
    card = _seed(engine, breakout_price=100.0)

    result = request_board_action(
        engine,
        _set(card, price=102.0),
        context=_context(market_open=True),
    )

    assert result.card.board_status == BoardStatus.BUYLIST
    assert result.card.breakout_price == 102.0
    assert result.card.entry_runtime_status is None


@pytest.mark.parametrize("command_kind", ["set", "clear"])
def test_published_buy_today_target_is_immutable_during_market_hours(
    engine, command_kind
):
    card = _seed(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=100.0,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
        entry_trigger=101.0,
        entry_orb_high=101.0,
        entry_orb_low=95.0,
        planned_quantity=20,
        target_position_quantity=20,
    )
    command = _set(card, price=102.0) if command_kind == "set" else _clear(card)

    with pytest.raises(BoardCommandRejectedError, match="immutable"):
        request_board_action(
            engine, command, context=_context(market_open=True)
        )

    stored = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.version == card.version
    assert stored.breakout_price == 100.0
    assert stored.entry_trigger == 101.0
    assert stored.planned_quantity == 20


def test_premarket_buy_today_set_invalidates_old_orb_geometry(engine):
    card = _seed(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=100.0,
        selected_orb_window="5m",
        position_percent=20.0,
        planned_quantity=20,
        target_position_quantity=20,
        entry_orb_window="5m",
        entry_orb_high=101.0,
        entry_orb_low=95.0,
        entry_trigger=101.0,
        stop_adr=40.0,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
        market_data_last_trusted_price=100.5,
    )

    result = request_board_action(
        engine, _set(card, price=102.0), context=_context()
    )

    assert result.card.board_status == BoardStatus.BUY_TODAY
    assert result.card.breakout_price == 102.0
    assert result.card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
    assert result.card.selected_orb_window is None
    assert result.card.entry_orb_window is None
    assert result.card.entry_orb_high is None
    assert result.card.entry_orb_low is None
    assert result.card.entry_trigger is None
    assert result.card.planned_quantity == 0
    assert result.card.target_position_quantity == 0
    assert result.card.position_percent == 0.0
    assert result.card.market_data_last_trusted_price is None


def test_premarket_clear_remands_buy_today_and_removes_executable_plan(engine):
    card = _seed(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=100.0,
        session_date="2026-08-21",
        selected_orb_window="5m",
        position_percent=20.0,
        planned_quantity=20,
        target_position_quantity=20,
        entry_orb_window="5m",
        entry_orb_high=101.0,
        entry_orb_low=95.0,
        entry_trigger=101.0,
        stop_adr=40.0,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
    )

    result = request_board_action(engine, _clear(card), context=_context())

    assert result.card.board_status == BoardStatus.BUYLIST
    assert result.card.breakout_price is None
    assert result.card.session_date is None
    assert result.card.entry_runtime_status is None
    assert result.card.entry_trigger is None
    assert result.card.planned_quantity == 0
    assert result.card.target_position_quantity == 0


@pytest.mark.parametrize(
    "evidence",
    [
        {"entry_client_order_id": "entry-1"},
        {"entry_submission_unresolved": True},
        {"entry_cancel_in_flight": True},
        {"entry_remaining_target_quantity": 5},
        {
            "position_runtime_status": PositionRuntimeStatus.OPEN,
            "broker_quantity": 5,
            "orderable_quantity": 5,
        },
    ],
)
def test_clear_rejects_entry_order_or_position_evidence(engine, evidence):
    card = _seed(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=100.0,
        **evidence,
    )

    with pytest.raises(BoardCommandRejectedError, match="evidence exists"):
        request_board_action(engine, _clear(card), context=_context())

    stored = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == BoardStatus.BUY_TODAY
    assert stored.breakout_price == 100.0


def test_clear_rejects_an_active_local_order_even_without_card_identity(engine):
    card = _seed(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=100.0,
    )
    record_execution_order(
        engine,
        ExecutionOrderRecord(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            client_order_id="entry-prepared-1",
            submitted_quantity=5,
            remaining_quantity=5,
        ),
    )

    with pytest.raises(BoardCommandRejectedError, match="evidence exists"):
        request_board_action(engine, _clear(card), context=_context())

    stored = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == BoardStatus.BUY_TODAY
    assert stored.breakout_price == 100.0


def test_operator_queue_mapping_round_trips_breakout_commands():
    command = _set(price=123.45)
    payload = serialize_board_command(command)
    record = type(
        "Record",
        (),
        {"payload": payload},
    )()

    restored = deserialize_board_command(record)

    assert restored == command
    assert (
        operator_command_type_for_board_command(command)
        == OperatorCommandType.SET_BREAKOUT_PRICE
    )
    assert (
        operator_command_type_for_board_command(
            ClearBreakoutPrice(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                expected_card_version=1,
            )
        )
        == OperatorCommandType.CLEAR_BREAKOUT_PRICE
    )


def test_authorized_operator_queue_can_create_passive_market_hours_target(engine):
    executor = LocalDeviceRole("pc-id", "TRADING-PC", False)
    operator = LocalDeviceRole("laptop-id", "TRADING-LAPTOP", False)
    claim_main_device(engine, executor)
    set_operator_control(engine, executor, operator)
    queued = enqueue_board_operator_command(engine, operator, _set(price=123.45))

    outcome = process_next_board_operator_command(
        engine,
        executor,
        context=BoardActionContext(regular_session_open=True),
    )

    assert outcome.command_id == queued.command.command_id
    assert outcome.status == OperatorCommandStatus.COMPLETED
    stored = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == BoardStatus.BUYLIST
    assert stored.breakout_price == 123.45


def test_operator_queue_cannot_rewrite_buy_today_during_market_hours(engine):
    executor = LocalDeviceRole("pc-id", "TRADING-PC", False)
    operator = LocalDeviceRole("laptop-id", "TRADING-LAPTOP", False)
    claim_main_device(engine, executor)
    set_operator_control(engine, executor, operator)
    card = _seed(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        breakout_price=100.0,
        entry_runtime_status=EntryRuntimeStatus.WAITING_BREAKOUT,
    )
    queued = enqueue_board_operator_command(
        engine, operator, _set(card, price=123.45)
    )

    outcome = process_next_board_operator_command(
        engine,
        executor,
        context=BoardActionContext(regular_session_open=True),
    )

    assert outcome.command_id == queued.command.command_id
    assert outcome.status == OperatorCommandStatus.REJECTED
    assert "immutable" in outcome.error_message
    stored = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.breakout_price == 100.0
