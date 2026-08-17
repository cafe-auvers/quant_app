"""Premarket Kanban planning must not depend on live runtime readiness."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.board_workflow import ActivateForToday
from src.core.execution_config import KANBAN_STRATEGY_INSTANCE_ID
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.services import trade_card_repository as card_repo
from src.services.execution_ownership_repository import (
    assign_ownership,
    get_ownership,
)
from src.ui.buyboard.controller import (
    BuyboardMixin,
    CommandRejectedError,
    _claim_kanban_planning_ownership,
)


@pytest.fixture(autouse=True)
def _isolate_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(
        card_repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json"
    )


def _engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'planning.db'}",
        future=True,
        poolclass=NullPool,
    )


class _Window(BuyboardMixin):
    def __init__(self, engine):
        self.pc_db_engine = engine
        self.refresh_count = 0

    def refresh_buyboard(self):
        self.refresh_count += 1


def _seed_buylist(engine):
    return card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            board_status=BoardStatus.BUYLIST,
            buylist_member=True,
        ),
    )


def _command(card):
    return ActivateForToday(
        environment=card.environment,
        account_no=card.account_no,
        symbol=card.symbol,
        expected_card_version=card.version,
    )


def test_buy_today_can_be_planned_before_runtime_worker_exists(tmp_path):
    engine = _engine(tmp_path)
    card = _seed_buylist(engine)
    window = _Window(engine)

    assert window._buyboard_dispatch_command(_command(card)) is True

    stored = card_repo.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    ownership = get_ownership(
        engine,
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
    )
    assert stored is not None
    assert stored.board_status == BoardStatus.BUY_TODAY
    assert ownership.owner == ExecutionOwner.KANBAN
    assert ownership.strategy_instance_id == KANBAN_STRATEGY_INSTANCE_ID
    assert window.refresh_count == 1


def test_buy_today_drag_does_not_steal_manual_ownership(tmp_path):
    engine = _engine(tmp_path)
    card = _seed_buylist(engine)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            owner=ExecutionOwner.MANUAL,
            assigned_by="manual-test",
        ),
    )

    with pytest.raises(CommandRejectedError, match="MANUAL-owned"):
        _claim_kanban_planning_ownership(engine, _command(card))

    ownership = get_ownership(
        engine,
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
    )
    assert ownership.owner == ExecutionOwner.MANUAL


def test_buy_today_drag_does_not_steal_other_kanban_strategy(tmp_path):
    engine = _engine(tmp_path)
    card = _seed_buylist(engine)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="12345678-01",
            symbol="AAPL",
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="different-strategy",
            assigned_by="other-strategy",
        ),
    )

    with pytest.raises(CommandRejectedError, match="another Kanban strategy"):
        _claim_kanban_planning_ownership(engine, _command(card))
