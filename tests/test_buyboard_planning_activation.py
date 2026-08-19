"""Premarket Kanban planning must not depend on live runtime readiness."""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.board_workflow import ActivateForToday
from src.core.execution_config import KANBAN_STRATEGY_INSTANCE_ID
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    TradeCardState,
)
from src.services import trade_card_repository as card_repo
from src.services.execution_ownership_repository import (
    assign_ownership,
    get_ownership,
)
from src.services import execution_workflow_service
from src.ui.buyboard.controller import (
    BuyboardMixin,
    CommandRejectedError,
    _projection_context,
)
from src.ui.buyboard.card import board_interaction_fingerprint


_APP = QApplication.instance() or QApplication([])


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


def _wait_for_dispatched_command(window, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while window.__dict__.get("_buyboard_pending_command_counts"):
        _APP.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for background board command")
        time.sleep(0.005)
    _APP.processEvents()
    worker = window._buyboard_command_worker
    worker.request_stop()
    assert worker.wait(1000)


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
    _wait_for_dispatched_command(window)

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


def test_buy_today_target_allocation_is_armed_without_orb_history(tmp_path):
    engine = _engine(tmp_path)
    card = card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="12345678-01",
            symbol="WEX",
            board_status=BoardStatus.BUYLIST,
            buylist_member=True,
            breakout_price=160.0,
            position_percent=20.0,
        ),
    )

    result = execution_workflow_service.request_board_action(
        engine,
        _command(card),
        claim_kanban_ownership=True,
    )

    assert result.card.board_status == BoardStatus.BUY_TODAY
    assert result.card.entry_runtime_status == EntryRuntimeStatus.EXECUTE_READY
    assert result.card.breakout_price == 160.0
    assert result.card.position_percent == 20.0


def test_dispatch_retries_once_after_equivalent_storage_only_revision(tmp_path):
    engine = _engine(tmp_path)
    rendered = _seed_buylist(engine)
    window = _Window(engine)
    projection = execution_workflow_service.get_board_projection(
        engine,
        environment=rendered.environment,
        account_no=rendered.account_no,
        symbol=rendered.symbol,
        context=_projection_context(window),
    )
    fingerprint = board_interaction_fingerprint(projection)

    current = card_repo.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    card_repo.update_trade_card(engine, current, expected_version=current.version)

    assert window._buyboard_dispatch_command(
        _command(rendered), interaction_fingerprint=fingerprint
    ) is True
    _wait_for_dispatched_command(window)
    stored = card_repo.get_trade_card(
        engine, "PROD", "12345678-01", "AAPL"
    )
    assert stored.board_status == BoardStatus.BUY_TODAY
    assert stored.version == 3
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
        execution_workflow_service.request_board_action(
            engine,
            _command(card),
            claim_kanban_ownership=True,
        )

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
        execution_workflow_service.request_board_action(
            engine,
            _command(card),
            claim_kanban_ownership=True,
        )


def test_failed_card_write_rolls_back_ownership_claim(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    card = _seed_buylist(engine)

    def fail_update(*args, **kwargs):
        raise RuntimeError("injected card persistence failure")

    monkeypatch.setattr(card_repo, "update_trade_card_in_transaction", fail_update)

    with pytest.raises(RuntimeError, match="injected card persistence failure"):
        execution_workflow_service.request_board_action(
            engine,
            _command(card),
            claim_kanban_ownership=True,
        )

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
    assert stored.board_status == BoardStatus.BUYLIST
    assert ownership.owner == ExecutionOwner.LEGACY
    assert ownership.version == 0


def test_stale_card_rejection_does_not_claim_ownership(tmp_path):
    engine = _engine(tmp_path)
    stale = _seed_buylist(engine)
    current = card_repo.get_trade_card(engine, "PROD", "12345678-01", "AAPL")
    current.name = "newer canonical state"
    card_repo.update_trade_card(engine, current, expected_version=current.version)

    with pytest.raises(card_repo.TradeCardVersionConflictError):
        execution_workflow_service.request_board_action(
            engine,
            _command(stale),
            claim_kanban_ownership=True,
        )

    ownership = get_ownership(
        engine,
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
    )
    assert ownership.owner == ExecutionOwner.LEGACY
    assert ownership.version == 0
