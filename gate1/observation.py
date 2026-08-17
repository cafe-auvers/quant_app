"""Evidence-derived Gate-1 observations.

The builder deliberately accepts no caller-authored safety booleans. It
correlates actual fake-broker boundary calls with durable execution rows,
external-order audit rows, canonical cards, broker truth, and evidence
captured from the real lease/market-data providers at the call boundary.
Unknown correlation or authority is unsafe by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from sqlalchemy.engine import Engine

from gate1.contract import BrokerMutationObservation, Gate1SystemObservation
from src.core.execution_mode import ExecutionLease
from src.core.execution_order_record import (
    AdoptedOrderPermission,
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    OrderOrigin,
)
from src.core.order_state import OrderIntent, OrderSide, OrderStatus
from src.services.discovered_external_order_repository import (
    list_discovered_external_orders_for_account,
)
from src.services.execution_command_repository import (
    ExecutionCommand,
    list_execution_commands_for_account,
)
from src.services.execution_order_repository import list_execution_orders_for_account
from src.services.trade_card_repository import list_trade_cards


@dataclass(frozen=True)
class MutationBoundaryEvidence:
    """Authority/data facts sampled by the broker double at mutation time."""

    lease: Optional[ExecutionLease]
    market_data_fresh: Optional[bool]


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _matches_submit(record: ExecutionOrderRecord, call: dict[str, Any]) -> bool:
    return all(
        (
            record.environment == str(call.get("environment") or "").upper(),
            record.account_no == str(call.get("account_no") or ""),
            record.symbol == str(call.get("symbol") or "").upper(),
            record.side.value == _enum_value(call.get("side")),
            record.submitted_quantity == int(call.get("quantity") or 0),
            abs(record.submitted_limit_price - float(call.get("limit_price") or 0.0))
            < 1e-9,
        )
    )


def _lease_matches(
    command: Optional[ExecutionCommand], evidence: Optional[MutationBoundaryEvidence]
) -> bool:
    if command is None or evidence is None or evidence.lease is None:
        return False
    lease = evidence.lease
    return bool(
        command.owner_device_id
        and command.lease_token
        and command.lease_epoch > 0
        and command.owner_device_id == lease.device_id
        and command.lease_token == lease.lease_token
        and command.lease_epoch == lease.lease_epoch
    )


def _logical_operation_id(
    record: Optional[ExecutionOrderRecord],
) -> tuple[str, int, str, str, str]:
    if record is None or not record.attempt_group_id:
        return ()
    return (
        record.attempt_group_id,
        record.attempt_number,
        record.side.value,
        record.intent.value,
        record.symbol,
    )


def _cancel_was_owned(record: Optional[ExecutionOrderRecord]) -> bool:
    if (
        record is None
        or record.broker_identity_status != BrokerIdentityStatus.EXACT
        or not record.broker_order_id
    ):
        return False
    if record.origin == OrderOrigin.APPLICATION:
        return True
    return bool(
        record.origin == OrderOrigin.USER_ADOPTED
        and AdoptedOrderPermission.CANCEL in record.adoption_permissions
    )


def build_gate1_system_observation(
    *,
    engine: Engine,
    broker: Any,
    environment: str,
    account_no: str,
) -> Gate1SystemObservation:
    """Build the frozen observation solely from integrated system evidence."""

    orders = list_execution_orders_for_account(
        engine, environment=environment, account_no=account_no
    )
    commands = list_execution_commands_for_account(
        engine, environment=environment, account_no=account_no
    )
    external_orders = list_discovered_external_orders_for_account(
        engine, environment=environment, account_no=account_no
    )
    cards = list_trade_cards(engine, environment=environment, account_no=account_no)

    submit_evidence: Sequence[MutationBoundaryEvidence] = tuple(
        getattr(broker, "submit_boundary_evidence", ())
    )
    cancel_evidence: Sequence[MutationBoundaryEvidence] = tuple(
        getattr(broker, "cancel_boundary_evidence", ())
    )
    mutations: list[BrokerMutationObservation] = []
    unmatched = list(orders)
    for index, call in enumerate(tuple(getattr(broker, "submit_calls", ()))):
        candidates = [record for record in unmatched if _matches_submit(record, call)]
        record = candidates[0] if candidates else None
        if record is not None:
            unmatched.remove(record)
        evidence = submit_evidence[index] if index < len(submit_evidence) else None
        command = next(
            (
                item
                for item in commands
                if record is not None
                and item.idempotency_key == f"SUBMIT:{record.client_order_id}"
            ),
            None,
        )
        mutations.append(
            BrokerMutationObservation(
                action="SUBMIT",
                client_order_id=(record.client_order_id if record is not None else ""),
                logical_operation_id=_logical_operation_id(record),
                is_new_entry=bool(
                    record is not None
                    and record.side == OrderSide.BUY
                    and record.intent == OrderIntent.ENTRY
                ),
                market_data_fresh=(
                    evidence.market_data_fresh if evidence is not None else None
                ),
                lease_current=_lease_matches(command, evidence),
            )
        )

    by_broker_id = {
        record.broker_order_id: record for record in orders if record.broker_order_id
    }
    unused_cancel_commands = [
        command for command in commands if command.command_type == "cancel"
    ]
    for index, call in enumerate(tuple(getattr(broker, "cancel_calls", ()))):
        broker_order_id = str(call.get("broker_order_id") or "").strip()
        record = by_broker_id.get(broker_order_id)
        command = next(
            (
                item
                for item in unused_cancel_commands
                if item.target_broker_order_id == broker_order_id
            ),
            None,
        )
        if command is not None:
            unused_cancel_commands.remove(command)
        evidence = cancel_evidence[index] if index < len(cancel_evidence) else None
        mutations.append(
            BrokerMutationObservation(
                action="CANCEL",
                client_order_id=(record.client_order_id if record is not None else ""),
                target_broker_order_id=broker_order_id,
                logical_operation_id=_logical_operation_id(record),
                exact_order_owned=_cancel_was_owned(record),
                lease_current=_lease_matches(command, evidence),
            )
        )

    remembered_ids = {
        record.broker_order_id
        for record in orders
        if record.broker_identity_status == BrokerIdentityStatus.EXACT
        and record.broker_order_id
    }
    for record in orders:
        remembered_ids.update(record.recovery_candidate_broker_order_ids)
    remembered_ids.update(order.broker_order_id for order in external_orders)

    terminal_statuses = {
        OrderStatus.CANCELLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
    broker_open_ids = {
        str(snapshot.broker_order_id or "").strip()
        for snapshot in tuple(getattr(broker, "order_snapshots", ()))
        if snapshot.broker_order_id and snapshot.status not in terminal_statuses
    }
    holdings = {
        str(symbol).upper(): max(0, int(values[0]))
        for symbol, values in dict(getattr(broker, "holdings", {})).items()
    }
    projected = {
        card.symbol: max(0, int(card.broker_quantity or 0)) for card in cards
    }
    return Gate1SystemObservation(
        mutations=tuple(mutations),
        broker_open_order_ids=frozenset(broker_open_ids),
        remembered_broker_order_ids=frozenset(remembered_ids),
        broker_holdings=holdings,
        projected_card_quantities=projected,
    )
