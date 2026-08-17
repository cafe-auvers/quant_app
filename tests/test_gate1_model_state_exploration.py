"""F4 deterministic exploration of both frozen lifecycle transition graphs."""
from __future__ import annotations

from copy import deepcopy
import os
import random

import pytest

from gate1.contract import (
    BrokerMutationObservation,
    Gate1SystemObservation,
    evaluate_post_failure_properties,
)
from gate1.manifest import DEFAULT_MODEL_SEED
from src.core.execution_order_record import (
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    InvalidExecutionOrderTransitionError,
    allowed_status_transitions,
    apply_status_transition,
    validate_consistency,
    validate_status_transition,
)
from src.core.kanban_transitions import (
    ALLOWED_BOARD_TRANSITIONS,
    InvalidBoardTransitionError,
    validate_board_transition,
)
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus


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


def test_f4_seeded_failure_restart_model_preserves_all_frozen_properties():
    seed = int(os.environ.get("GATE1_MODEL_SEED", DEFAULT_MODEL_SEED))
    generator = random.Random(seed)

    for sequence_number in range(500):
        client_id = f"F4-{seed}-{sequence_number}"
        broker_id = f"B-{sequence_number}"
        broker_quantity = generator.randint(0, 100)
        projected_quantity = broker_quantity + generator.randint(0, 5)
        has_open_order = generator.choice((True, False))
        lease_current = True
        mutations: list[BrokerMutationObservation] = []

        for _ in range(generator.randint(1, 20)):
            event = generator.choice(
                ("observe", "restart", "handoff", "stale_quote", "fresh_quote")
            )
            if event == "handoff":
                lease_current = False
            elif event == "restart":
                # Restart discovers all broker truth before execution resumes.
                projected_quantity = max(projected_quantity, broker_quantity)
            elif event == "observe":
                projected_quantity = max(projected_quantity, broker_quantity)

        # The model permits one mutation only while its lease is current; a
        # handed-off predecessor records no broker-boundary event at all.
        if lease_current:
            mutations.append(
                BrokerMutationObservation(
                    action="SUBMIT",
                    client_order_id=client_id,
                    is_new_entry=True,
                    market_data_fresh=True,
                    lease_current=True,
                )
            )

        observation = Gate1SystemObservation(
            mutations=tuple(mutations),
            broker_open_order_ids=(frozenset({broker_id}) if has_open_order else frozenset()),
            remembered_broker_order_ids=(frozenset({broker_id}) if has_open_order else frozenset()),
            broker_holdings={"AAPL": broker_quantity},
            projected_card_quantities={"AAPL": projected_quantity},
        )
        assert evaluate_post_failure_properties(observation) == (), (
            seed,
            sequence_number,
            observation,
        )
