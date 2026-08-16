"""Explicit ``GUARDED_ENGINE``-mode command request models (Workstream 3,
PR2 second pass -- review finding 2).

The gateway's ``Broker``-protocol methods (``submit_order``/``cancel_order``)
exist so ``LEGACY_COMPATIBILITY`` mode is a byte-for-byte transparent
pass-through wherever legacy code already injects a ``Broker`` -- see
``execution_command_gateway``'s module docstring. That flat, fixed-keyword
protocol has no room for a caller-generated *stable* command identity, a
lease, or a command's own attempt/replace lineage, and forcing
``GUARDED_ENGINE`` mode through it was exactly what let a fresh, non-
deterministic ``client_order_id`` get minted on every call (finding 1) and
let cancellation lose the local order identity entirely (finding 2).

These request models are what ``GUARDED_ENGINE`` mode actually takes:
built once by the caller (:mod:`src.services.execution_workflow_service`),
carrying whatever identity that caller intends to be stable across a retry
or restart of the *same* logical decision -- this module does not itself
generate or persist that identity; it only carries it.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Optional

from src.core.execution_mode import ExecutionSource
from src.core.order_state import REGULAR_LIMIT_EXECUTION, OrderIntent, OrderSide
from src.services.execution_lease_protocol import ExecutionLease


def derive_execution_client_order_id(
    *,
    attempt_group_id: str,
    attempt_number: int,
    environment: str,
    account_no: str,
    symbol: str,
    intent: OrderIntent | str,
) -> str:
    """Derive a restart-stable ID from durable logical-attempt state."""
    intent_value = intent.value if isinstance(intent, OrderIntent) else str(intent or "")
    canonical = "|".join(
        (
            str(attempt_group_id or "").strip(),
            str(int(attempt_number)),
            str(environment or "").strip().upper(),
            str(account_no or "").strip(),
            str(symbol or "").strip().upper(),
            intent_value.strip().upper(),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20].upper()
    prefix = re.sub(r"[^A-Z0-9_-]+", "-", canonical.upper()).strip("-")[:100]
    return f"{prefix}-{digest}"


@dataclass(frozen=True, eq=False)
class CancelIntent:
    """Complete, restart-replayable context for a tracked cancellation."""

    client_order_id: str
    cancel_command_id: str
    environment: str
    account_no: str
    lease: Optional[ExecutionLease]
    strategy_instance_id: str
    source: ExecutionSource
    emergency: bool = False
    symbol: str = ""
    broker_order_id: str = ""
    quantity: int = 0
    side: str = ""
    exchange: str = "NASD"

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", str(self.client_order_id or "").strip())
        object.__setattr__(self, "cancel_command_id", str(self.cancel_command_id or "").strip())
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "strategy_instance_id", str(self.strategy_instance_id or ""))
        object.__setattr__(self, "emergency", bool(self.emergency))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "broker_order_id", str(self.broker_order_id or ""))
        object.__setattr__(self, "quantity", max(0, int(self.quantity or 0)))
        object.__setattr__(self, "side", str(self.side or "").upper())
        object.__setattr__(self, "exchange", str(self.exchange or "NASD").upper())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.client_order_id == other
        if not isinstance(other, CancelIntent):
            return False
        return (
            self.client_order_id,
            self.cancel_command_id,
            self.environment,
            self.account_no,
            self.lease,
            self.strategy_instance_id,
            self.source,
            self.emergency,
            self.symbol,
            self.broker_order_id,
            self.quantity,
            self.side,
            self.exchange,
        ) == (
            other.client_order_id,
            other.cancel_command_id,
            other.environment,
            other.account_no,
            other.lease,
            other.strategy_instance_id,
            other.source,
            other.emergency,
            other.symbol,
            other.broker_order_id,
            other.quantity,
            other.side,
            other.exchange,
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.client_order_id,
                self.cancel_command_id,
                self.environment,
                self.account_no,
                self.lease,
                self.strategy_instance_id,
                self.source,
                self.emergency,
                self.symbol,
                self.broker_order_id,
                self.quantity,
                self.side,
                self.exchange,
            )
        )


@dataclass(frozen=True)
class SubmitExecutionRequest:
    """``client_order_id`` is the caller's stable identity for this exact
    logical submission -- replaying this request with the same
    ``client_order_id`` must be rejected as a duplicate (A5), never
    resubmitted to the broker a second time."""

    client_order_id: str
    environment: str
    account_no: str
    symbol: str
    side: OrderSide
    intent: OrderIntent
    quantity: int
    limit_price: float
    exchange: str = "NASD"
    execution_policy: str = REGULAR_LIMIT_EXECUTION
    attempt_group_id: str = ""
    attempt_number: int = 1
    attempt_deadline_at: Optional[str] = None
    lease: Optional[ExecutionLease] = None
    source: ExecutionSource = ExecutionSource.SYSTEM
    replaces_execution_order_id: str = ""
    # H1 (Workstream 9): required, non-blank whenever source is
    # KANBAN_BOARD and the symbol's persisted execution_owner is KANBAN --
    # the gateway checks this against ExecutionOwnership.strategy_instance_id
    # so one Kanban strategy instance can never act on a symbol assigned to
    # a different one (review finding 3, third pass).
    strategy_instance_id: str = ""
    emergency: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", str(self.client_order_id or "").strip())
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "strategy_instance_id", str(self.strategy_instance_id or ""))
        object.__setattr__(self, "emergency", bool(self.emergency))


@dataclass(frozen=True)
class CancelExecutionRequest:
    """``client_order_id`` identifies the *existing* order being cancelled
    (must already be a persisted ``ExecutionOrderRecord``).
    ``cancel_command_id`` is the caller's stable identity for *this cancel
    decision* -- distinct from ``client_order_id`` and distinct from the
    order's own submission attempt number (finding 8): replaying the same
    cancel decision (e.g. after a timeout) reuses the same
    ``cancel_command_id``; a genuinely new, later cancel decision (e.g.
    after an earlier cancel was explicitly rejected and the order resumed
    working) must use a new one, or it is indistinguishable from a replay
    and permanently blocked.
    """

    client_order_id: str
    cancel_command_id: str
    environment: str
    account_no: str
    lease: Optional[ExecutionLease] = None
    source: ExecutionSource = ExecutionSource.SYSTEM
    strategy_instance_id: str = ""
    emergency: bool = False
    symbol: str = ""
    broker_order_id: str = ""
    quantity: int = 0
    side: str = ""
    exchange: str = "NASD"

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", str(self.client_order_id or "").strip())
        object.__setattr__(self, "cancel_command_id", str(self.cancel_command_id or "").strip())
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "strategy_instance_id", str(self.strategy_instance_id or ""))
        object.__setattr__(self, "emergency", bool(self.emergency))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "broker_order_id", str(self.broker_order_id or ""))
        object.__setattr__(self, "quantity", max(0, int(self.quantity or 0)))
        object.__setattr__(self, "side", str(self.side or "").upper())
        object.__setattr__(self, "exchange", str(self.exchange or "NASD").upper())


@dataclass(frozen=True)
class ReplaceExecutionRequest:
    """``replace_command_id`` is the caller's stable identity for this
    replace decision (used to derive the internal cancel's
    ``cancel_command_id``, so a replayed replace doesn't attempt a second,
    conflicting cancel of the same order). ``new_client_order_id`` is the
    stable identity for the *replacement* order this creates."""

    client_order_id: str
    replace_command_id: str
    new_client_order_id: str
    new_quantity: int
    new_limit_price: float
    environment: str
    account_no: str
    lease: Optional[ExecutionLease] = None
    source: ExecutionSource = ExecutionSource.SYSTEM
    strategy_instance_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", str(self.client_order_id or "").strip())
        object.__setattr__(self, "replace_command_id", str(self.replace_command_id or "").strip())
        object.__setattr__(self, "new_client_order_id", str(self.new_client_order_id or "").strip())
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "strategy_instance_id", str(self.strategy_instance_id or ""))
