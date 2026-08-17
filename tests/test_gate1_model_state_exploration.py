"""F4 deterministic exploration of both frozen lifecycle transition graphs."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import random

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from fakes.fake_execution_broker import FakeExecutionBroker
from gate1.manifest import DEFAULT_MODEL_SEED
from src.core import execution_config
from src.core.execution_order_record import (
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    InvalidExecutionOrderTransitionError,
    allowed_status_transitions,
    apply_status_transition,
    validate_consistency,
    validate_status_transition,
)
from src.core.execution_mode import ExecutionLease, ExecutionSource
from src.core.execution_ownership import ExecutionOwner, ExecutionOwnership
from src.core.execution_request import CancelExecutionRequest, SubmitExecutionRequest
from src.core.kanban_transitions import (
    ALLOWED_BOARD_TRANSITIONS,
    InvalidBoardTransitionError,
    validate_board_transition,
)
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus, EntryRuntimeStatus, TradeCardState
from src.services import buyboard_runtime
from src.services import trade_card_repository as card_repo
from src.services import trading_engine as trading_engine_module
from src.services.account_reconciliation import run_account_reconciliation_pass
from src.services.emergency_journal import EmergencyJournal, EmergencyLeaseAllowance
from src.services.execution_command_gateway import (
    ExecutionCommandGateway,
    GuardedSubmissionPreBrokerAbortedError,
    LeaseNotVerifiedError,
    OrderNotFoundForCancelError,
)
from src.services.execution_command_repository import DuplicateCommandError
from src.services.execution_lease_protocol import FakeExecutionLeaseProtocol
from src.services.execution_order_repository import fetch_execution_order
from src.services.execution_ownership_repository import assign_ownership
from src.services.mutation_budget_protocol import AllowAllMutationBudget
from src.services.realtime_market_data import QuoteSnapshot, RestPollingMarketDataService


def _record() -> ExecutionOrderRecord:
    return ExecutionOrderRecord(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        client_order_id="F4-CID",
        submitted_quantity=10,
        remaining_quantity=10,
    )


def _apply(record: ExecutionOrderRecord, target: ExecutionOrderStatus) -> None:
    kwargs = (
        {"broker_order_id": "F4-BROKER-1"}
        if target == ExecutionOrderStatus.ACKNOWLEDGED
        and not record.broker_order_id
        else {}
    )
    apply_status_transition(record, target, **kwargs)
    validate_consistency(record)


def test_f4_exhaustively_explores_every_execution_order_transition_path():
    visited_edges: set[tuple[ExecutionOrderStatus, ExecutionOrderStatus]] = set()

    def walk(record: ExecutionOrderRecord) -> None:
        current = record.status
        for target in sorted(allowed_status_transitions(current), key=lambda item: item.value):
            edge = (current, target)
            if edge in visited_edges:
                continue
            visited_edges.add(edge)
            candidate = deepcopy(record)
            _apply(candidate, target)
            walk(candidate)

    walk(_record())

    documented_edges = {
        (source, target)
        for source in ExecutionOrderStatus
        for target in allowed_status_transitions(source)
    }
    assert visited_edges == documented_edges


def test_f4_exhaustively_rejects_every_undocumented_order_transition():
    for source in ExecutionOrderStatus:
        for target in ExecutionOrderStatus:
            if target in allowed_status_transitions(source):
                validate_status_transition(source, target)
            else:
                with pytest.raises(InvalidExecutionOrderTransitionError):
                    validate_status_transition(source, target)


def test_f4_exhaustively_checks_the_complete_kanban_transition_table():
    for source in BoardStatus:
        for target in BoardStatus:
            allowed = source == target or target in ALLOWED_BOARD_TRANSITIONS[source]
            if allowed:
                validate_board_transition(source, target)
            else:
                with pytest.raises(InvalidBoardTransitionError):
                    validate_board_transition(source, target)


@pytest.mark.usefixtures("trading_enabled")
def test_f4_seeded_adversarial_actions_drive_real_sut_and_converge(
    tmp_path, monkeypatch
):
    seed = int(os.environ.get("GATE1_MODEL_SEED", DEFAULT_MODEL_SEED))
    generator = random.Random(seed)
    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)
    monkeypatch.setattr(
        trading_engine_module, "is_buyboard_engine_enabled", lambda: True
    )
    monkeypatch.setattr(buyboard_runtime, "is_regular_session_open", lambda: True)
    monkeypatch.setattr(buyboard_runtime, "_eod_window_reached", lambda: False)

    actions = [
        "stale_entry",
        "lease_loss",
        "duplicate_restart",
        "unowned_cancel",
        "partial_fill_divergence",
        "outage_recovery",
    ] * 3
    generator.shuffle(actions)
    coverage = {action: 0 for action in set(actions)}

    for sequence_number, action in enumerate(actions):
        coverage[action] += 1
        symbol = f"F{sequence_number:03d}"
        account_no = f"F4-{sequence_number:03d}"
        strategy = f"f4-{sequence_number}"
        lease = ExecutionLease("device-old", f"token-{sequence_number}", 1)
        protocol = FakeExecutionLeaseProtocol(current=lease)
        broker = FakeExecutionBroker()
        engine = create_engine(
            f"sqlite:///{tmp_path / f'f4-{sequence_number}.db'}",
            future=True,
            poolclass=NullPool,
        )
        assign_ownership(
            engine,
            ExecutionOwnership(
                environment="PROD",
                account_no=account_no,
                symbol=symbol,
                owner=ExecutionOwner.KANBAN,
                strategy_instance_id=strategy,
            ),
        )
        writable = [True]
        journal = EmergencyJournal(tmp_path / f"f4-{sequence_number}.jsonl")
        gateway = ExecutionCommandGateway(
            real_broker=broker,
            engine=engine,
            mode_override=True,
            lease_protocol=protocol,
            mutation_budget=AllowAllMutationBudget(),
            buying_power_provider=lambda *_: 100_000.0,
            database_writable_provider=lambda: writable[0],
            emergency_journal=journal,
            emergency_lease_allowance=EmergencyLeaseAllowance(max_seconds=60),
        )

        def request(
            client_order_id: str,
            *,
            side: OrderSide = OrderSide.SELL,
            intent: OrderIntent = OrderIntent.STOP_LOSS,
            emergency: bool = True,
        ) -> SubmitExecutionRequest:
            return SubmitExecutionRequest(
                client_order_id=client_order_id,
                environment="PROD",
                account_no=account_no,
                symbol=symbol,
                side=side,
                intent=intent,
                quantity=10,
                limit_price=100.0,
                lease=lease,
                source=ExecutionSource.KANBAN_BOARD,
                strategy_instance_id=strategy,
                emergency=emergency,
                attempt_group_id=f"group-{sequence_number}",
                attempt_number=1,
            )

        if action == "stale_entry":
            now = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)
            card = card_repo.create_trade_card(
                engine,
                TradeCardState(
                    environment="PROD",
                    account_no=account_no,
                    symbol=symbol,
                    board_status=BoardStatus.BUY_TODAY,
                    entry_runtime_status=EntryRuntimeStatus.EXECUTE_READY,
                    entry_trigger=100.0,
                    planned_quantity=10,
                    target_position_quantity=10,
                ),
            )
            market_data = RestPollingMarketDataService(
                quote_fetcher=lambda item: QuoteSnapshot(
                    symbol=item,
                    last_price=99.0,
                    broker_event_at=now,
                    received_at=now,
                    processed_at=now,
                ),
                clock=lambda: now,
            )
            runtime = buyboard_runtime.build_buyboard_runtime(
                buying_power_provider=lambda *_: 100_000.0,
                card_lookup=lambda environment, account, item: card_repo.get_trade_card(
                    engine, environment, account, item
                ),
                broker=gateway,
                execution_lease=lease,
                strategy_instance_id=strategy,
                persist_card_before_execution=lambda current: card_repo.update_trade_card(
                    engine, current, expected_version=current.version
                ),
                market_data=market_data,
            )
            stale = QuoteSnapshot(
                symbol=symbol,
                last_price=101.0,
                broker_event_at=now - timedelta(seconds=10),
                received_at=now - timedelta(seconds=10),
                processed_at=now,
            )
            assert not stale.is_execution_fresh(now=now)
            assert runtime.trading_engine.evaluate_entry_quote([card], stale) == []
            assert broker.submit_calls == []
            assert card.board_status == BoardStatus.BUY_TODAY

        elif action == "lease_loss":
            protocol.grant(ExecutionLease("device-new", "new-token", 2))
            with pytest.raises(
                (LeaseNotVerifiedError, GuardedSubmissionPreBrokerAbortedError)
            ):
                gateway.submit_guarded(request(f"LEASE-{sequence_number}"))
            assert broker.submit_calls == []

        elif action == "duplicate_restart":
            command = request(f"DUPLICATE-{sequence_number}")
            broker.queue_acceptance(broker_order_id=f"B-DUP-{sequence_number}")
            gateway.submit_guarded(command)
            restarted = ExecutionCommandGateway(
                real_broker=broker,
                engine=engine,
                mode_override=True,
                lease_protocol=protocol,
                mutation_budget=AllowAllMutationBudget(),
                buying_power_provider=lambda *_: 100_000.0,
            )
            with pytest.raises(DuplicateCommandError):
                restarted.submit_guarded(command)
            assert len(broker.submit_calls) == 1

        elif action == "unowned_cancel":
            with pytest.raises(OrderNotFoundForCancelError):
                gateway.cancel_guarded(
                    CancelExecutionRequest(
                        client_order_id=f"UNKNOWN-{sequence_number}",
                        cancel_command_id=f"CANCEL-UNKNOWN-{sequence_number}",
                        environment="PROD",
                        account_no=account_no,
                        lease=lease,
                        source=ExecutionSource.KANBAN_BOARD,
                        strategy_instance_id=strategy,
                    )
                )
            assert broker.cancel_calls == []

        elif action == "partial_fill_divergence":
            client_id = f"PARTIAL-{sequence_number}"
            card = card_repo.create_trade_card(
                engine,
                TradeCardState(
                    environment="PROD",
                    account_no=account_no,
                    symbol=symbol,
                    board_status=BoardStatus.ENTRY_PENDING,
                    entry_client_order_id=client_id,
                    entry_attempt_group_id=f"group-{sequence_number}",
                    entry_pending_attempt_number=1,
                    planned_quantity=10,
                    target_position_quantity=10,
                ),
            )
            broker.queue_acceptance(broker_order_id=f"B-PARTIAL-{sequence_number}")
            gateway.submit_guarded(
                request(
                    client_id,
                    side=OrderSide.BUY,
                    intent=OrderIntent.ENTRY,
                    emergency=False,
                )
            )

            class ReconciliationBroker:
                def discover_orders(self, **_kwargs):
                    from src.core.order_state import BrokerOrderDiscoveryResult, BrokerOrderStatusSnapshot, OrderStatus

                    return BrokerOrderDiscoveryResult(
                        snapshots=[
                            BrokerOrderStatusSnapshot(
                                environment="PROD",
                                account_no=account_no,
                                symbol=symbol,
                                broker_order_id=f"B-PARTIAL-{sequence_number}",
                                client_order_id=client_id,
                                side=OrderSide.BUY,
                                status=OrderStatus.PARTIALLY_FILLED,
                                quantity_requested=10,
                                filled_quantity=4,
                                remaining_quantity=6,
                                limit_price=100.0,
                            )
                        ],
                        open_orders_complete=True,
                        history_complete=True,
                        reserved_orders_complete=True,
                    )

                def get_positions(self, **_kwargs):
                    return {
                        "overseas": {
                            "holdings": [
                                {
                                    "symbol": symbol,
                                    "quantity": 4,
                                    "orderable_quantity": 4,
                                    "average_price": 100.0,
                                }
                            ]
                        }
                    }

            run_account_reconciliation_pass(
                broker=ReconciliationBroker(),
                engine=engine,
                environment="PROD",
                account_no=account_no,
                cards=[card],
                account_balance_provider=lambda *_: 100_000.0,
            )
            reconciled = card_repo.get_trade_card(
                engine, "PROD", account_no, symbol
            )
            assert reconciled.broker_quantity == 4
            assert fetch_execution_order(engine, client_id).remaining_quantity == 6

        else:
            gateway.note_canonical_lease_verified(lease)
            gateway.note_canonical_ownership_verified(
                environment="PROD",
                account_no=account_no,
                symbol=symbol,
                source=ExecutionSource.KANBAN_BOARD,
                strategy_instance_id=strategy,
            )
            writable[0] = False
            broker.queue_acceptance(broker_order_id=f"B-OUTAGE-{sequence_number}")
            gateway.submit_guarded(request(f"OUTAGE-{sequence_number}"))
            writable[0] = True
            assert gateway.reconcile_emergency_journal() == 1
            assert len(broker.submit_calls) == 1
            assert fetch_execution_order(engine, f"OUTAGE-{sequence_number}") is not None

    assert coverage == {action: 3 for action in coverage}
