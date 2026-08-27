"""Bridge live UI gestures onto the append-only operator command queue."""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from src.core.board_workflow import (
    ActivateForToday,
    AnyBoardCommand,
    BoardActionContext,
    CancelEntry,
    ClearBreakoutPrice,
    RequestPartialSell,
    RequestSellAll,
    SetBreakevenStop,
    SetBreakoutPrice,
    SetManualStop,
)
from src.core.trade_card_state import BoardStatus
from src.services import execution_workflow_service, trade_card_repository
from src.services.operator_commands import (
    OperatorCommandInsertResult,
    OperatorCommandRecord,
    OperatorCommandStatus,
    OperatorCommandType,
    claim_next_operator_command,
    finish_operator_command,
    start_operator_command,
    submit_operator_command,
)
from src.services.state_sync import LocalDeviceRole


_BOARD_TO_OPERATOR_TYPE = {
    ActivateForToday: OperatorCommandType.ADD_BUY_TODAY,
    SetBreakoutPrice: OperatorCommandType.SET_BREAKOUT_PRICE,
    ClearBreakoutPrice: OperatorCommandType.CLEAR_BREAKOUT_PRICE,
    CancelEntry: OperatorCommandType.CANCEL_ENTRY,
    RequestPartialSell: OperatorCommandType.SELL_PARTIAL,
    RequestSellAll: OperatorCommandType.SELL_ALL,
    SetBreakevenStop: OperatorCommandType.MOVE_STOP_BREAKEVEN,
    SetManualStop: OperatorCommandType.MOVE_STOP_MANUAL_PRICE,
}
_BOARD_COMMAND_TYPES = {item.__name__: item for item in _BOARD_TO_OPERATOR_TYPE}


def _attach_local_activation_symbol_key(
    command: AnyBoardCommand,
) -> AnyBoardCommand:
    """Attach a locally verified key without making activation depend on it.

    Missing realtime metadata must remain visible as a feed-readiness problem;
    it must not prevent the trader from publishing durable Buy Today intent.
    """

    if not isinstance(command, ActivateForToday) or command.kis_ws_symbol_key:
        return command
    from src.services.kis_ws_symbol_keys import (
        KisWsSymbolKeyStore,
        derive_symbol_key_from_kis_master,
    )

    try:
        key = KisWsSymbolKeyStore().resolve(command.symbol)
    except (OSError, RuntimeError):
        try:
            # The operator only needs to carry the authoritative mapping in
            # the command. The executor materializes it after the command is
            # accepted, so a rejected/duplicate request does not edit local
            # subscription state.
            key = derive_symbol_key_from_kis_master(command.symbol)
        except (OSError, RuntimeError):
            return command
    return replace(command, kis_ws_symbol_key=key)


def operator_command_type_for_board_command(
    command: AnyBoardCommand,
) -> Optional[OperatorCommandType]:
    return _BOARD_TO_OPERATOR_TYPE.get(type(command))


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def serialize_board_command(command: AnyBoardCommand) -> Dict[str, Any]:
    command = _attach_local_activation_symbol_key(command)
    return {
        "board_command_type": type(command).__name__,
        "board_command": {
            field.name: _json_value(getattr(command, field.name))
            for field in fields(command)
        },
    }


def deserialize_board_command(record: OperatorCommandRecord) -> AnyBoardCommand:
    command_name = str(record.payload.get("board_command_type") or "")
    command_class = _BOARD_COMMAND_TYPES.get(command_name)
    if command_class is None:
        raise ValueError(
            f"Unsupported operator board command payload: {command_name or '<missing>'}"
        )
    raw = record.payload.get("board_command")
    if not isinstance(raw, dict):
        raise ValueError("Operator board command payload is not an object")
    values = dict(raw)
    requested_at = values.get("requested_at")
    if isinstance(requested_at, str):
        values["requested_at"] = datetime.fromisoformat(requested_at)
    return command_class(**values)


def enqueue_board_operator_command(
    engine,
    requester: LocalDeviceRole,
    command: AnyBoardCommand,
) -> OperatorCommandInsertResult:
    command_type = operator_command_type_for_board_command(command)
    if command_type is None:
        raise ValueError(
            f"{type(command).__name__} is not supported as a live operator command"
        )
    return submit_operator_command(
        engine,
        requester,
        command_type,
        symbol=command.symbol,
        payload=serialize_board_command(command),
        idempotency_key=f"BOARD:{command.command_id}",
        command_id=command.command_id,
    )


def _state_hash(card) -> str:
    payload = card.to_dict() if card is not None else {}
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_broker_confirmed_board_state(engine, command: AnyBoardCommand) -> None:
    """Apply the queue-specific validation that must precede canonical intent."""

    card = trade_card_repository.get_trade_card(
        engine,
        command.environment,
        command.account_no,
        command.symbol,
    )
    if card is None:
        if isinstance(command, SetBreakoutPrice) and command.expected_card_version == 0:
            return
        raise execution_workflow_service.BoardCommandRejectedError(
            f"No canonical card exists for {command.symbol}."
        )
    if isinstance(command, ActivateForToday) and card.board_status in {
        BoardStatus.BUY_TODAY,
        BoardStatus.ENTRY_PENDING,
        BoardStatus.OPEN_POSITION,
        BoardStatus.PARTIAL_SELL,
        BoardStatus.SELL_ALL,
    }:
        raise execution_workflow_service.BoardCommandRejectedError(
            f"{command.symbol} already has an active Buy Today, entry, or position state."
        )
    if isinstance(command, ActivateForToday):
        from src.core.trade_card_state import SYMBOL_EXECUTION_ACTIVE_STATUSES

        for other in trade_card_repository.list_trade_cards(
            engine,
            environment=command.environment,
            raise_on_error=True,
        ):
            if (
                str(other.symbol or "").strip().upper()
                == str(command.symbol or "").strip().upper()
                and str(other.account_no or "").strip()
                != str(command.account_no or "").strip()
                and other.board_status in SYMBOL_EXECUTION_ACTIVE_STATUSES
            ):
                raise execution_workflow_service.BoardCommandRejectedError(
                    f"{command.symbol} already has an active entry or position for "
                    "another account. The ORB queue is symbol-scoped, so "
                    "only one account can activate that symbol at a time."
                )
    if isinstance(command, (RequestPartialSell, RequestSellAll)):
        available = max(
            0,
            int(card.orderable_quantity or card.broker_quantity or 0),
        )
        if available <= 0:
            raise execution_workflow_service.BoardCommandRejectedError(
                f"No broker-confirmed available holding exists for {command.symbol}."
            )
        if isinstance(command, RequestPartialSell):
            if command.quantity <= 0:
                raise execution_workflow_service.BoardCommandRejectedError(
                    "Partial-sell quantity must be positive."
                )
            if command.quantity > available:
                raise execution_workflow_service.BoardCommandRejectedError(
                    f"Partial-sell quantity {command.quantity} exceeds broker-confirmed "
                    f"available holdings {available}."
                )


def process_claimed_board_operator_command(
    engine,
    executor: LocalDeviceRole,
    accepted: OperatorCommandRecord,
    *,
    context: Optional[BoardActionContext] = None,
    action_handler: Optional[Callable[..., Any]] = None,
) -> OperatorCommandRecord:
    """Validate and apply one already accepted request exactly once.

    Operator ownership is intentionally not rechecked here.  Switching the
    operator owner only affects future inserts; an accepted command remains
    assigned to its executor until it reaches a terminal outcome.
    """

    executing = start_operator_command(engine, executor, accepted.command_id)
    try:
        command = deserialize_board_command(executing)
        resolved_context = context or BoardActionContext()
        breakout_plan_command = isinstance(
            command, (SetBreakoutPrice, ClearBreakoutPrice)
        )
        if breakout_plan_command:
            # submit_operator_command already proved that the requester held
            # Operator Control.  Preserve that durable authorization when a
            # different device is the execution owner consuming the request.
            resolved_context = replace(
                resolved_context, local_operator_control=True
            )
        _validate_broker_confirmed_board_state(engine, command)
        before = trade_card_repository.get_trade_card(
            engine, command.environment, command.account_no, command.symbol
        )
        before_hash = _state_hash(before)
        handler = action_handler or execution_workflow_service.request_board_action
        result = handler(
            engine,
            command,
            context=resolved_context,
            claim_kanban_ownership=not breakout_plan_command,
        )
        after = getattr(result, "card", None)
        if after is None:
            after = trade_card_repository.get_trade_card(
                engine, command.environment, command.account_no, command.symbol
            )
        return finish_operator_command(
            engine,
            executor,
            executing.command_id,
            OperatorCommandStatus.COMPLETED,
            state_before_hash=before_hash,
            state_after_hash=_state_hash(after),
        )
    except execution_workflow_service.BoardCommandRejectedError as exc:
        return finish_operator_command(
            engine,
            executor,
            executing.command_id,
            OperatorCommandStatus.REJECTED,
            error_message=str(exc),
        )
    except Exception as exc:
        return finish_operator_command(
            engine,
            executor,
            executing.command_id,
            OperatorCommandStatus.FAILED,
            error_message=str(exc),
        )


def process_next_board_operator_command(
    engine,
    executor: LocalDeviceRole,
    *,
    context: Optional[BoardActionContext] = None,
    action_handler: Optional[Callable[..., Any]] = None,
) -> Optional[OperatorCommandRecord]:
    accepted = claim_next_operator_command(engine, executor)
    if accepted is None:
        return None
    return process_claimed_board_operator_command(
        engine,
        executor,
        accepted,
        context=context,
        action_handler=action_handler,
    )
