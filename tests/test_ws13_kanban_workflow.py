"""Workstream 13: revision-aware UI projection and workflow parity."""
from __future__ import annotations

import ast
import json
import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from src.core.board_workflow import (
    ActivateForToday,
    AdoptExternalOrder,
    BoardActionContext,
    BoardExecutionOrderProjection,
    BoardProjectionContext,
    CancelEntry,
    CancelPartialSell,
    MoveToBuylist,
    RequestPartialSell,
    RequestSellAll,
    SetBreakevenStop,
    SetManualStop,
)
from src.core.discovered_external_order import (
    ExternalOrderDisposition,
    new_discovered_external_order,
)
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    OrderOrigin,
)
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.order_state import (
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    StopType,
    TradeCardState,
)
from src.services import execution_workflow_service as workflow
from src.services import trade_card_repository as card_repo
from src.services.account_reconciliation import run_account_reconciliation_pass
from src.services.discovered_external_order_repository import (
    fetch_discovered_external_order,
    record_discovered_external_order,
)
from src.services.execution_order_repository import (
    fetch_execution_order,
    record_execution_order,
    save_execution_order,
)
from src.services.execution_ownership_repository import assign_ownership
from src.services.trade_card_repository import TradeCardVersionConflictError


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(card_repo, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")


def _engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'ws13.db'}", future=True, poolclass=NullPool
    )


def _seed(engine, **overrides):
    fields = dict(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        broker_quantity=100,
        orderable_quantity=100,
        average_entry_price=100.0,
        entry_orb_low=95.0,
        stop_type=StopType.ORB_LOW,
        active_stop_price=95.0,
    )
    fields.update(overrides)
    card = card_repo.create_trade_card(engine, TradeCardState(**fields))
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment=card.environment,
            account_no=card.account_no,
            symbol=card.symbol,
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="strategy-1",
            assigned_by="test",
        ),
    )
    return card


def _projection(engine, card, **context_overrides):
    context = BoardProjectionContext(**context_overrides)
    return workflow.project_board_card(engine, card, context=context)


def test_board_projection_query_count_is_constant_not_per_card(tmp_path):
    engine = _engine(tmp_path)
    for index in range(12):
        _seed(
            engine,
            symbol=f"SYM{index}",
            board_status=BoardStatus.BUYLIST,
            position_runtime_status=PositionRuntimeStatus.NONE,
            broker_quantity=0,
            orderable_quantity=0,
        )
    # Ensure all four projection tables before counting application reads.
    workflow.get_board_projection_revision(engine)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        projections = workflow.list_board_projections(engine)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(projections) == 12
    assert len(statements) == 4


def _command(command_type, projection, **kwargs):
    return command_type(
        environment=projection.card.environment,
        account_no=projection.card.account_no,
        symbol=projection.card.symbol,
        expected_card_version=projection.card.version,
        expected_readiness_generation=projection.readiness_generation,
        expected_ownership_version=projection.ownership_version,
        expected_execution_owner=projection.ownership_owner,
        expected_strategy_instance_id=projection.strategy_instance_id,
        **kwargs,
    )


def _ready(**overrides):
    fields = dict(
        enforce_runtime_fences=True,
        engine_enabled=True,
        readiness_generation=7,
        action_ready=True,
        device_active=True,
        regular_session_open=True,
    )
    fields.update(overrides)
    return BoardActionContext(**fields)


def test_architecture_no_kanban_interaction_module_calls_broker_or_gateway_directly():
    interaction_files = [
        ROOT / "src/ui/buyboard/board.py",
        ROOT / "src/ui/buyboard/card.py",
        ROOT / "src/ui/buyboard/columns.py",
        ROOT / "src/ui/buyboard/controller.py",
        ROOT / "src/ui/buyboard/dialogs.py",
        ROOT / "src/ui/buyboard/drag_commands.py",
    ]
    forbidden_modules = (
        "src.services.execution_command_gateway",
        "src.services.execution_command_repository",
        "src.services.execution_order_repository",
        "src.services.order_reconciliation",
        "src.services.account_reconciliation",
        "src.services.broker",
        "src.api.kis",
    )
    violations = []
    for path in interaction_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and str(node.module).startswith(
                forbidden_modules
            ):
                violations.append(f"{path.name}: imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_modules):
                        violations.append(f"{path.name}: imports {alias.name}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {
                    "submit_order",
                    "cancel_order",
                    "replace_order",
                    "submit_guarded",
                    "cancel_guarded",
                    "replace_guarded",
                }:
                    violations.append(f"{path.name}: calls {node.func.attr}")
    assert violations == []


def test_buylist_to_buy_today_is_a_revision_aware_workflow_request(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.BUYLIST,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
        entry_runtime_status=EntryRuntimeStatus.DATA_UNAVAILABLE,
        entry_block_reason="Current-session ORB minute bars are unavailable",
    )
    projection = _projection(engine, card, readiness_generation=7)

    result = workflow.request_board_action(
        engine,
        _command(ActivateForToday, projection),
        context=_ready(session_date=date(2026, 8, 19)),
    )

    assert result.card.board_status == BoardStatus.BUY_TODAY
    assert result.card.session_date == date(2026, 8, 19)
    assert result.card.entry_runtime_status == EntryRuntimeStatus.ORB_FORMING
    assert result.card.entry_block_reason == ""
    assert result.card.version == card.version + 1


def test_buy_today_activation_without_context_targets_next_market_session(
    tmp_path, monkeypatch
):
    from src.utils import market_calendar

    engine = _engine(tmp_path)
    card = _seed(engine, board_status=BoardStatus.BUYLIST)
    projection = _projection(engine, card)
    next_session = date(2026, 8, 24)
    monkeypatch.setattr(
        market_calendar,
        "current_or_next_nyse_session_date",
        lambda: next_session,
    )

    result = workflow.request_board_action(
        engine,
        _command(ActivateForToday, projection),
    )

    assert result.card.board_status == BoardStatus.BUY_TODAY
    assert result.card.session_date == next_session


def test_stale_card_revision_cannot_overwrite_reconciled_truth(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    stale = _projection(engine, card, readiness_generation=7)
    card.orderable_quantity = 75
    card_repo.update_trade_card(engine, card, expected_version=card.version)

    with pytest.raises(TradeCardVersionConflictError):
        workflow.request_board_action(
            engine, _command(RequestSellAll, stale), context=_ready()
        )
    current = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert current.board_status == BoardStatus.OPEN_POSITION
    assert current.orderable_quantity == 75


def test_stale_readiness_generation_and_reconciliation_both_fail_closed(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    projection = _projection(engine, card, readiness_generation=7)
    command = _command(RequestSellAll, projection)

    with pytest.raises(workflow.BoardRuntimeFenceError, match="readiness changed"):
        workflow.request_board_action(
            engine, command, context=_ready(readiness_generation=8)
        )
    with pytest.raises(workflow.BoardRuntimeFenceError, match="reconciliation"):
        workflow.request_board_action(
            engine, command, context=_ready(reconciliation_in_progress=True)
        )


def test_ownership_revision_change_after_render_is_rejected(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    projection = _projection(engine, card, readiness_generation=7)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            owner=ExecutionOwner.KANBAN,
            strategy_instance_id="strategy-2",
            assigned_by="handoff",
        ),
    )

    with pytest.raises(workflow.BoardOwnershipMismatchError, match="revision changed"):
        workflow.request_board_action(
            engine, _command(RequestSellAll, projection), context=_ready()
        )
    assert card_repo.get_trade_card(engine, "PROD", "1", "AAPL").board_status == BoardStatus.OPEN_POSITION


def test_legacy_owned_symbol_is_observation_only_for_kanban(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    assign_ownership(
        engine,
        ExecutionOwnership(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            owner=ExecutionOwner.LEGACY,
            assigned_by="cutover-test",
        ),
    )
    current = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    projection = _projection(engine, current, readiness_generation=7)

    with pytest.raises(workflow.BoardOwnershipMismatchError, match="LEGACY-owned"):
        workflow.request_board_action(
            engine, _command(RequestSellAll, projection), context=_ready()
        )
    assert card_repo.get_trade_card(engine, "PROD", "1", "AAPL").board_status == BoardStatus.OPEN_POSITION


def test_two_sell_all_gestures_record_one_intent_and_never_declare_flat(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    projection = _projection(engine, card, readiness_generation=7)
    first = _command(RequestSellAll, projection)
    second = _command(RequestSellAll, projection)

    result = workflow.request_board_action(engine, first, context=_ready())
    with pytest.raises(TradeCardVersionConflictError):
        workflow.request_board_action(engine, second, context=_ready())

    assert result.card.board_status == BoardStatus.SELL_ALL
    assert result.card.position_runtime_status == PositionRuntimeStatus.LIQUIDATING
    assert result.card.broker_quantity == 100
    assert result.card.exit_all_required is True

    refreshed = _projection(engine, result.card, readiness_generation=7)
    with pytest.raises(workflow.BoardCommandRejectedError, match="already pending"):
        workflow.request_board_action(
            engine, _command(RequestSellAll, refreshed), context=_ready()
        )


def test_ambiguous_entry_blocks_user_cancel_until_reconciliation(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.ENTRY_PENDING,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
        entry_client_order_id="CID-1",
        entry_submission_unresolved=True,
    )
    projection = _projection(engine, card, readiness_generation=7)

    with pytest.raises(workflow.BoardCommandRejectedError, match="ambiguous"):
        workflow.request_board_action(
            engine, _command(CancelEntry, projection), context=_ready()
        )
    current = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert current.entry_block_reason == ""
    assert current.board_status == BoardStatus.ENTRY_PENDING


def test_buy_today_with_persisted_entry_identity_requires_cancel_lifecycle(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.BUY_TODAY,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
        entry_client_order_id="CID-ENTRY-1",
        entry_attempt_group_id="ENTRY-GROUP",
        entry_pending_attempt_number=1,
    )
    order = record_execution_order(
        engine,
        ExecutionOrderRecord(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.ENTRY,
            client_order_id="CID-ENTRY-1",
            attempt_group_id="ENTRY-GROUP",
            attempt_number=1,
            status=ExecutionOrderStatus.WORKING,
            broker_identity_status=BrokerIdentityStatus.EXACT,
            broker_order_id="BROKER-ENTRY-1",
            submitted_quantity=10,
            remaining_quantity=10,
        ),
    )
    projection = _projection(engine, card, readiness_generation=7)

    with pytest.raises(workflow.BoardCommandRejectedError, match="request entry cancellation"):
        workflow.request_board_action(
            engine, _command(MoveToBuylist, projection), context=_ready()
        )

    cancellation = workflow.request_board_action(
        engine, _command(CancelEntry, projection), context=_ready()
    ).card
    assert cancellation.board_status == BoardStatus.BUY_TODAY
    assert cancellation.entry_client_order_id == "CID-ENTRY-1"
    assert cancellation.entry_block_reason == "cancel_requested"

    # A terminal broker status without lifecycle reconciliation still cannot
    # move the card; the durable identity must be retired by reconciliation.
    prior_version = order.version
    order.status = ExecutionOrderStatus.CANCELLED
    order.remaining_quantity = 0
    save_execution_order(engine, order, expected_version=prior_version)
    terminal_projection = _projection(engine, cancellation, readiness_generation=7)
    with pytest.raises(workflow.BoardCommandRejectedError, match="request entry cancellation"):
        workflow.request_board_action(
            engine, _command(MoveToBuylist, terminal_projection), context=_ready()
        )

    class TerminalBroker:
        def get_positions(self, **kwargs):
            return {"overseas": {"holdings": []}}

        def discover_orders(self, **kwargs):
            return BrokerOrderDiscoveryResult(
                snapshots=(
                    BrokerOrderStatusSnapshot(
                        environment="PROD",
                        account_no="1",
                        symbol="AAPL",
                        broker_order_id="BROKER-ENTRY-1",
                        side=OrderSide.BUY,
                        status=OrderStatus.CANCELLED,
                        quantity_requested=10,
                        filled_quantity=0,
                        remaining_quantity=0,
                    ),
                ),
                open_orders_complete=True,
                history_complete=True,
                reserved_orders_complete=True,
            )

    run_account_reconciliation_pass(
        broker=TerminalBroker(),
        engine=engine,
        environment="PROD",
        account_no="1",
        cards=(cancellation,),
        account_balance_provider=lambda environment, account_no: 100_000.0,
    )
    reconciled = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert reconciled.board_status == BoardStatus.BUYLIST
    assert reconciled.entry_client_order_id == ""


def test_partial_sell_uses_broker_orderable_quantity_and_stays_pending(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine, broker_quantity=100, orderable_quantity=80)
    projection = _projection(engine, card, readiness_generation=7)

    result = workflow.request_board_action(
        engine,
        _command(RequestPartialSell, projection, quantity=30),
        context=_ready(),
    )

    assert result.card.board_status == BoardStatus.PARTIAL_SELL
    assert result.card.pending_partial_sell_quantity == 30
    assert result.card.broker_quantity == 100
    assert result.card.orderable_quantity == 80


@pytest.mark.parametrize("regular_session_open", [False, True])
def test_unsubmitted_partial_sell_withdrawal_is_not_blocked_by_market_hours(
    tmp_path, regular_session_open
):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.PARTIAL_SELL,
        position_runtime_status=PositionRuntimeStatus.PARTIAL_EXIT_PENDING,
        pending_partial_sell_quantity=30,
    )
    projection = _projection(engine, card, readiness_generation=7)

    result = workflow.request_board_action(
        engine,
        _command(CancelPartialSell, projection),
        context=BoardActionContext(
            enforce_runtime_fences=False,
            engine_enabled=False,
            action_ready=False,
            device_active=False,
            regular_session_open=regular_session_open,
        ),
    ).card

    assert result.board_status == BoardStatus.OPEN_POSITION
    assert result.pending_partial_sell_quantity == 0
    assert result.broker_quantity == 100


def test_ambiguous_partial_sell_cannot_be_withdrawn_as_if_unsubmitted(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.PARTIAL_SELL,
        position_runtime_status=PositionRuntimeStatus.PARTIAL_EXIT_PENDING,
        pending_partial_sell_quantity=30,
        exit_client_order_id="CID-AMB",
        exit_submission_unresolved=True,
    )
    projection = _projection(engine, card, readiness_generation=7)

    with pytest.raises(workflow.BoardCommandRejectedError, match="ambiguous"):
        workflow.request_board_action(
            engine,
            _command(CancelPartialSell, projection),
            context=_ready(),
        )

    current = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    assert current.board_status == BoardStatus.PARTIAL_SELL
    assert current.pending_partial_sell_quantity == 30


def test_stale_partial_sell_withdrawal_cannot_overwrite_newer_card(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        board_status=BoardStatus.PARTIAL_SELL,
        position_runtime_status=PositionRuntimeStatus.PARTIAL_EXIT_PENDING,
        pending_partial_sell_quantity=30,
    )
    stale = _projection(engine, card, readiness_generation=7)
    card.orderable_quantity = 90
    card_repo.update_trade_card(engine, card, expected_version=card.version)

    with pytest.raises(TradeCardVersionConflictError):
        workflow.request_board_action(
            engine,
            _command(CancelPartialSell, stale),
            context=_ready(),
        )


def test_stop_changes_remain_pending_until_runtime_acknowledges_each_rule(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    first_projection = _projection(engine, card, readiness_generation=7)
    breakeven = workflow.request_board_action(
        engine, _command(SetBreakevenStop, first_projection), context=_ready()
    ).card
    assert breakeven.stop_type == StopType.ORB_LOW
    assert breakeven.active_stop_price == 95.0
    assert breakeven.pending_stop_type == StopType.BREAKEVEN
    assert breakeven.pending_stop_price > breakeven.average_entry_price
    assert breakeven.pending_stop_command_id

    pending_projection = _projection(engine, breakeven, readiness_generation=7)
    with pytest.raises(workflow.BoardCommandRejectedError):
        workflow.request_board_action(
            engine,
            _command(
                SetManualStop,
                pending_projection,
                price=breakeven.pending_stop_price + 1.0,
            ),
            context=_ready(),
        )

    assert breakeven.acknowledge_pending_stop_change()
    card_repo.update_trade_card(engine, breakeven, expected_version=breakeven.version)

    second_projection = _projection(engine, breakeven, readiness_generation=7)
    manual_price = breakeven.active_stop_price + 2.0
    manual = workflow.request_board_action(
        engine,
        _command(SetManualStop, second_projection, price=manual_price),
        context=_ready(),
    ).card
    assert manual.stop_type == StopType.BREAKEVEN
    assert manual.active_stop_price == breakeven.active_stop_price
    assert manual.pending_stop_type == StopType.MANUAL_PRICE
    assert manual.pending_stop_price == manual_price
    assert manual.broker_quantity == 100


def test_premarket_sell_all_is_a_durable_next_session_instruction(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    projection = _projection(engine, card, readiness_generation=7)

    queued = workflow.request_board_action(
        engine,
        _command(RequestSellAll, projection),
        context=_ready(regular_session_open=False),
    ).card

    assert queued.board_status == BoardStatus.SELL_ALL
    assert queued.sell_all_at_market_open is True
    assert queued.position_runtime_status == PositionRuntimeStatus.QUEUED_FOR_OPEN
    assert queued.broker_quantity == 100


def test_projection_exposes_working_ambiguous_and_runtime_safety_state(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        entry_submission_unresolved=True,
        entry_client_order_id="CID-AMB",
    )
    order = ExecutionOrderRecord(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        client_order_id="CID-AMB",
        status=ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
        broker_identity_status=BrokerIdentityStatus.AMBIGUOUS,
        submitted_quantity=100,
        remaining_quantity=100,
    )
    record_execution_order(engine, order)
    projection = _projection(
        engine,
        card,
        readiness_generation=7,
        reconciliation_blocked_accounts=("1",),
        global_restrictions=("Execution engine disabled",),
    )

    assert projection.working_order_count == 1
    assert projection.ambiguous_order_count == 1
    assert projection.reconciliation_blocked is True
    assert "Execution engine disabled" in projection.engine_restrictions
    assert any("Ambiguous order" in reason for reason in projection.engine_restrictions)


def test_buylist_projection_does_not_label_legacy_ownership_as_a_restriction(tmp_path):
    engine = _engine(tmp_path)
    card = card_repo.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="MAX",
            board_status=BoardStatus.BUYLIST,
            breakout_price=13.75,
        ),
    )

    projection = _projection(engine, card)

    assert projection.ownership_owner == ExecutionOwner.LEGACY.value
    assert not any(
        "Observation only" in reason for reason in projection.engine_restrictions
    )


def test_external_order_is_distinct_fenced_and_only_explicitly_adopted(tmp_path):
    engine = _engine(tmp_path)
    card = _seed(engine)
    external = record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id="BROKER-EXT-1",
            quantity_requested=25,
            broker_status=ExecutionOrderStatus.WORKING,
        ),
    )
    projection = _projection(engine, card, readiness_generation=7)
    assert projection.external_orders == (external,)
    assert projection.has_external_order_warning is True

    with pytest.raises(workflow.BoardCommandRejectedError, match="unowned external"):
        workflow.request_board_action(
            engine, _command(RequestSellAll, projection), context=_ready()
        )
    assert fetch_execution_order(engine, external.external_order_id) is None

    adopted = workflow.request_board_action(
        engine,
        _command(
            AdoptExternalOrder,
            projection,
            external_order_id=external.external_order_id,
            adopted_by="operator",
        ),
        context=_ready(),
    )
    record = fetch_execution_order(engine, adopted.adopted_execution_client_order_id)
    assert record.origin == OrderOrigin.USER_ADOPTED
    assert record.adopted_from_external_order_id == external.external_order_id
    assert record.adoption_permissions == frozenset()
    assert (
        fetch_discovered_external_order(engine, external.external_order_id).disposition
        == ExternalOrderDisposition.USER_ADOPTED
    )
    refreshed_card = card_repo.get_trade_card(engine, "PROD", "1", "AAPL")
    after = _projection(engine, refreshed_card, readiness_generation=7)
    assert after.working_order_count == 0
    assert [item.client_order_id for item in after.unlinked_owned_orders] == [
        record.client_order_id
    ]

    with pytest.raises(workflow.BoardCommandRejectedError, match="unlinked owned"):
        workflow.request_board_action(
            engine, _command(RequestSellAll, after), context=_ready()
        )


@pytest.mark.parametrize(
    "board_status,command_type,command_kwargs",
    [
        (BoardStatus.BUYLIST, ActivateForToday, {}),
        (BoardStatus.BUY_TODAY, CancelEntry, {}),
    ],
)
def test_active_unlinked_owned_order_fences_entry_affecting_board_actions(
    tmp_path, board_status, command_type, command_kwargs
):
    engine = _engine(tmp_path)
    card = _seed(
        engine,
        board_status=board_status,
        position_runtime_status=PositionRuntimeStatus.NONE,
        broker_quantity=0,
        orderable_quantity=0,
    )
    record_execution_order(
        engine,
        ExecutionOrderRecord(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.UNKNOWN,
            client_order_id="ADOPTED-UNLINKED",
            status=ExecutionOrderStatus.WORKING,
            broker_identity_status=BrokerIdentityStatus.EXACT,
            broker_order_id="BROKER-UNLINKED",
            submitted_quantity=5,
            remaining_quantity=5,
            origin=OrderOrigin.USER_ADOPTED,
            adopted_from_external_order_id="EXTERNAL-UNLINKED",
        ),
    )
    projection = _projection(engine, card, readiness_generation=7)

    with pytest.raises(workflow.BoardCommandRejectedError, match="unlinked owned"):
        workflow.request_board_action(
            engine,
            _command(command_type, projection, **command_kwargs),
            context=_ready(),
        )


def test_discovered_external_order_renders_as_a_separate_non_draggable_row(tmp_path):
    from PyQt5.QtCore import QMimeData, Qt
    from PyQt5.QtWidgets import QApplication

    from src.ui.buyboard.card import ExternalOrderWidget
    from src.ui.buyboard.columns import BoardColumnList, _CARD_MIME_TYPE

    app = QApplication.instance() or QApplication([])
    engine = _engine(tmp_path)
    card = _seed(engine)
    record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id="EXT-ROW",
            quantity_requested=10,
            broker_status=ExecutionOrderStatus.WORKING,
        ),
    )
    projection = _projection(engine, card, readiness_generation=7)
    column = BoardColumnList(
        BoardStatus.OPEN_POSITION,
        on_card_dropped=lambda payload, target: None,
        on_external_order_adopt=lambda order: None,
    )
    column.set_cards([projection])

    assert column.count() == 2
    assert isinstance(column.itemWidget(column.item(1)), ExternalOrderWidget)
    assert not bool(column.item(1).flags() & Qt.ItemIsDragEnabled)
    app.processEvents()


def test_external_order_without_a_trade_card_still_projects_and_can_be_explicitly_adopted(tmp_path):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    from src.core.board_workflow import BoardExternalOrderProjection
    from src.ui.buyboard.card import UnlinkedExecutionOrderWidget
    from src.ui.buyboard.columns import BoardColumnList

    app = QApplication.instance() or QApplication([])
    engine = _engine(tmp_path)
    external = record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment="PROD",
            account_no="9",
            symbol="MSFT",
            side=OrderSide.BUY,
            broker_order_id="EXT-NO-CARD",
            quantity_requested=5,
            broker_status=ExecutionOrderStatus.WORKING,
        ),
    )
    projections = workflow.list_board_projections(
        engine,
        context=BoardProjectionContext(readiness_generation=4),
    )
    assert len(projections) == 1
    assert isinstance(projections[0], BoardExternalOrderProjection)
    assert projections[0].order.external_order_id == external.external_order_id

    result = workflow.request_board_action(
        engine,
        AdoptExternalOrder(
            environment="PROD",
            account_no="9",
            symbol="MSFT",
            expected_card_version=0,
            external_order_id=external.external_order_id,
            adopted_by="operator",
        ),
        context=_ready(),
    )
    assert result.card is None
    assert fetch_execution_order(
        engine, result.adopted_execution_client_order_id
    ).origin == OrderOrigin.USER_ADOPTED

    after_adoption = workflow.list_board_projections(
        engine,
        context=BoardProjectionContext(readiness_generation=4),
    )
    assert len(after_adoption) == 1
    assert isinstance(after_adoption[0], BoardExecutionOrderProjection)
    assert (
        after_adoption[0].order.client_order_id
        == result.adopted_execution_client_order_id
    )
    assert after_adoption[0].order.origin == OrderOrigin.USER_ADOPTED

    column = BoardColumnList(
        BoardStatus.WATCHLIST,
        on_card_dropped=lambda payload, target: None,
    )
    column.set_cards(after_adoption)
    assert column.count() == 1
    assert isinstance(column.itemWidget(column.item(0)), UnlinkedExecutionOrderWidget)
    assert not bool(column.item(0).flags() & Qt.ItemIsDragEnabled)
    app.processEvents()


def test_a_rejected_drag_command_never_moves_the_local_widget():
    from PyQt5.QtCore import QMimeData, Qt
    from PyQt5.QtWidgets import QApplication

    from src.ui.buyboard.columns import BoardColumnList, _CARD_MIME_TYPE

    app = QApplication.instance() or QApplication([])
    card = TradeCardState(environment="PROD", account_no="1", symbol="AAPL")
    callbacks = []
    column = BoardColumnList(
        BoardStatus.WATCHLIST,
        on_card_dropped=lambda payload, target: callbacks.append((payload, target)),
    )
    column.set_cards([card])
    mime = QMimeData()
    mime.setData(
        _CARD_MIME_TYPE,
        json.dumps(
            {
                "environment": "PROD",
                "account_no": "1",
                "symbol": "AAPL",
                "version": 1,
            }
        ).encode("utf-8"),
    )

    class Event:
        accepted = False
        action = None

        def mimeData(self):
            return mime

        def setDropAction(self, action):
            self.action = action

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    event = Event()
    column.dropEvent(event)

    assert callbacks and callbacks[0][1] == BoardStatus.WATCHLIST
    assert event.action == Qt.IgnoreAction
    assert column.count() == 1
    assert column.item(0).data(Qt.UserRole)["symbol"] == "AAPL"
    app.processEvents()


def test_legacy_and_kanban_destructive_paths_both_reference_shared_workflow():
    legacy_worker = (ROOT / "src/ui/order_workers.py").read_text(encoding="utf-8")
    legacy_actions = (ROOT / "src/ui/buylist/orders.py").read_text(encoding="utf-8")
    kanban_runtime = (ROOT / "src/services/buyboard_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "request_exit_submit" in legacy_worker
    assert "execution_workflow_service import request_cancel" in legacy_actions
    assert "src.services.execution_workflow_service import" in kanban_runtime
    assert "request_submit" in kanban_runtime
    assert "request_exit_submit" in kanban_runtime
    assert "request_cancel_intent" in kanban_runtime
