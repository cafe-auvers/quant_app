"""Workflow-level execution results shared by legacy and guarded modes."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.core.execution_order_record import ExecutionOrderRecord, ExecutionOrderStatus
from src.core.order_state import BrokerOrder, OrderStatus


class UnifiedExecutionStatus(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


_BROKER_STATUS_MAP = {
    OrderStatus.CREATED: UnifiedExecutionStatus.PREPARED,
    OrderStatus.SUBMITTING: UnifiedExecutionStatus.SUBMITTING,
    OrderStatus.ACCEPTED: UnifiedExecutionStatus.ACKNOWLEDGED,
    OrderStatus.WORKING: UnifiedExecutionStatus.WORKING,
    OrderStatus.PARTIALLY_FILLED: UnifiedExecutionStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED: UnifiedExecutionStatus.FILLED,
    OrderStatus.CANCEL_REQUESTED: UnifiedExecutionStatus.CANCEL_PENDING,
    OrderStatus.CANCELLED: UnifiedExecutionStatus.CANCELLED,
    OrderStatus.REJECTED: UnifiedExecutionStatus.REJECTED,
    OrderStatus.EXPIRED: UnifiedExecutionStatus.EXPIRED,
    OrderStatus.UNKNOWN_SUBMISSION_STATE: UnifiedExecutionStatus.UNKNOWN,
    OrderStatus.UNKNOWN: UnifiedExecutionStatus.UNKNOWN,
}

_EXECUTION_STATUS_MAP = {
    ExecutionOrderStatus.PREPARED: UnifiedExecutionStatus.PREPARED,
    ExecutionOrderStatus.SUBMITTING: UnifiedExecutionStatus.SUBMITTING,
    ExecutionOrderStatus.ACKNOWLEDGED: UnifiedExecutionStatus.ACKNOWLEDGED,
    ExecutionOrderStatus.WORKING: UnifiedExecutionStatus.WORKING,
    ExecutionOrderStatus.PARTIALLY_FILLED: UnifiedExecutionStatus.PARTIALLY_FILLED,
    ExecutionOrderStatus.FILLED: UnifiedExecutionStatus.FILLED,
    ExecutionOrderStatus.CANCEL_PENDING: UnifiedExecutionStatus.CANCEL_PENDING,
    ExecutionOrderStatus.CANCELLED: UnifiedExecutionStatus.CANCELLED,
    ExecutionOrderStatus.CANCELLED_LOCALLY: UnifiedExecutionStatus.CANCELLED,
    ExecutionOrderStatus.REJECTED: UnifiedExecutionStatus.REJECTED,
    ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED: UnifiedExecutionStatus.REJECTED,
    ExecutionOrderStatus.EXPIRED: UnifiedExecutionStatus.EXPIRED,
    ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE: UnifiedExecutionStatus.UNKNOWN,
}

_EXECUTION_TO_BROKER_STATUS = {
    ExecutionOrderStatus.PREPARED: OrderStatus.CREATED,
    ExecutionOrderStatus.SUBMITTING: OrderStatus.SUBMITTING,
    ExecutionOrderStatus.ACKNOWLEDGED: OrderStatus.ACCEPTED,
    ExecutionOrderStatus.WORKING: OrderStatus.WORKING,
    ExecutionOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    ExecutionOrderStatus.FILLED: OrderStatus.FILLED,
    ExecutionOrderStatus.CANCEL_PENDING: OrderStatus.CANCEL_REQUESTED,
    ExecutionOrderStatus.CANCELLED: OrderStatus.CANCELLED,
    ExecutionOrderStatus.CANCELLED_LOCALLY: OrderStatus.CANCELLED,
    ExecutionOrderStatus.REJECTED: OrderStatus.REJECTED,
    ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED: OrderStatus.REJECTED,
    ExecutionOrderStatus.EXPIRED: OrderStatus.EXPIRED,
    ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE: OrderStatus.UNKNOWN_SUBMISSION_STATE,
}


def broker_order_from_execution_record(record: ExecutionOrderRecord) -> BrokerOrder:
    """Project guarded persistence into the legacy orchestration read model."""
    return BrokerOrder(
        client_order_id=record.client_order_id,
        environment=record.environment,
        account_no=record.account_no,
        symbol=record.symbol,
        side=record.side,
        intent=record.intent,
        quantity_requested=record.submitted_quantity,
        limit_price=record.submitted_limit_price,
        exchange=record.exchange,
        status=_EXECUTION_TO_BROKER_STATUS.get(record.status, OrderStatus.UNKNOWN),
        execution_policy=record.execution_policy,
        broker_order_id=record.broker_order_id,
        filled_quantity=record.filled_quantity,
        remaining_quantity=record.remaining_quantity,
        avg_fill_price=record.average_fill_price,
        attempt_group_id=record.attempt_group_id,
        attempt_number=record.attempt_number,
        attempt_deadline_at=record.attempt_deadline_at,
        capital_reservation_id=record.capital_reservation_id,
    )


@dataclass(frozen=True)
class ExecutionSubmissionResult:
    """Stable result shape consumed by entry and exit orchestration."""

    client_order_id: str
    status: UnifiedExecutionStatus
    broker_order_id: str
    submitted_quantity: int
    filled_quantity: int
    remaining_quantity: int
    capital_reservation_id: str
    ambiguous: bool = False
    error_message: str = ""

    @classmethod
    def from_broker_order(cls, order: BrokerOrder) -> "ExecutionSubmissionResult":
        status = _BROKER_STATUS_MAP.get(order.status, UnifiedExecutionStatus.UNKNOWN)
        return cls(
            client_order_id=order.client_order_id,
            status=status,
            broker_order_id=order.broker_order_id,
            submitted_quantity=order.quantity_requested,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
            capital_reservation_id=order.capital_reservation_id,
            ambiguous=order.status in (
                OrderStatus.UNKNOWN_SUBMISSION_STATE,
                OrderStatus.UNKNOWN,
            ),
            error_message=order.error_message,
        )

    @classmethod
    def from_execution_order(
        cls, record: ExecutionOrderRecord
    ) -> "ExecutionSubmissionResult":
        status = _EXECUTION_STATUS_MAP.get(record.status, UnifiedExecutionStatus.UNKNOWN)
        return cls(
            client_order_id=record.client_order_id,
            status=status,
            broker_order_id=record.broker_order_id,
            submitted_quantity=record.submitted_quantity,
            filled_quantity=record.filled_quantity,
            remaining_quantity=record.remaining_quantity,
            capital_reservation_id=record.capital_reservation_id,
            ambiguous=record.status in (
                ExecutionOrderStatus.SUBMITTING,
                ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
            ),
        )
