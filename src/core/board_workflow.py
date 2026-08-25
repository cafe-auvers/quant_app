"""Typed Kanban workflow requests and read-only UI projections.

The types in this module deliberately have no Qt, repository, or broker
dependencies.  A board gesture carries the revision and runtime generation it
was rendered from to :mod:`src.services.execution_workflow_service`; the UI
never treats the gesture itself as authoritative state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional, Tuple, Union
from uuid import uuid4

from src.core.discovered_external_order import DiscoveredExternalOrder
from src.core.execution_order_record import ExecutionOrderRecord, ExecutionOrderStatus
from src.core.trade_card_state import TradeCardState


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_command_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class BoardCommand:
    """Identity and optimistic fences common to every board request."""

    environment: str
    account_no: str
    symbol: str
    expected_card_version: int
    command_id: str = field(default_factory=new_command_id)
    requested_at: datetime = field(default_factory=_utc_now)
    expected_readiness_generation: int = 0
    expected_ownership_version: int = 0
    expected_execution_owner: str = ""
    expected_strategy_instance_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(
            self, "expected_card_version", int(self.expected_card_version or 0)
        )
        object.__setattr__(
            self,
            "expected_readiness_generation",
            int(self.expected_readiness_generation or 0),
        )
        object.__setattr__(
            self, "expected_ownership_version", int(self.expected_ownership_version or 0)
        )
        object.__setattr__(
            self,
            "expected_execution_owner",
            str(self.expected_execution_owner or "").upper(),
        )
        object.__setattr__(
            self,
            "expected_strategy_instance_id",
            str(self.expected_strategy_instance_id or ""),
        )


@dataclass(frozen=True)
class MoveToWatchlist(BoardCommand):
    pass


@dataclass(frozen=True)
class MoveToBuylist(BoardCommand):
    pass


@dataclass(frozen=True)
class ActivateForToday(BoardCommand):
    # The KIS realtime key is operational symbol metadata, not a credential.
    # Carrying the verified value with the durable activation lets a split
    # Operator/Execution topology subscribe on the executor without copying a
    # workstation-local file for every new Buy Today symbol.
    kis_ws_symbol_key: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "kis_ws_symbol_key",
            str(self.kis_ws_symbol_key or "").strip(),
        )


@dataclass(frozen=True)
class CancelEntry(BoardCommand):
    pass


@dataclass(frozen=True)
class RequestPartialSell(BoardCommand):
    quantity: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "quantity", int(self.quantity or 0))


@dataclass(frozen=True)
class CancelPartialSell(BoardCommand):
    """Withdraw a partial-exit objective or request cancellation of its order.

    With no durable SELL lifecycle, the workflow returns the card to Open
    Position immediately.  Once a known order exists, the card stays pending
    until the runtime cancels it and broker reconciliation proves terminal
    state.  Ambiguous orders remain non-withdrawable.
    """


@dataclass(frozen=True)
class RequestSellAll(BoardCommand):
    pass


@dataclass(frozen=True)
class CancelQueuedSellAll(BoardCommand):
    pass


@dataclass(frozen=True)
class SetOrbStop(BoardCommand):
    """Select the frozen entry ORB low without recalculating it."""


@dataclass(frozen=True)
class SetBreakevenStop(BoardCommand):
    pass


@dataclass(frozen=True)
class SetManualStop(BoardCommand):
    price: float = 0.0

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "price", float(self.price or 0.0))


@dataclass(frozen=True)
class SetBreakoutPrice(BoardCommand):
    """Create or revise the canonical, non-broker breakout target."""

    price: float = 0.0
    buffer_pct: float = 0.001

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "price", float(self.price or 0.0))
        object.__setattr__(self, "buffer_pct", float(self.buffer_pct))


@dataclass(frozen=True)
class ClearBreakoutPrice(BoardCommand):
    """Remove a canonical target without leaving an executable entry plan."""


@dataclass(frozen=True)
class ReorderCard(BoardCommand):
    target_priority: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "target_priority", int(self.target_priority or 0))


@dataclass(frozen=True)
class AdoptExternalOrder(BoardCommand):
    """Explicit audited adoption; never generated by drag/drop."""

    external_order_id: str = ""
    adopted_by: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "external_order_id", str(self.external_order_id or ""))
        object.__setattr__(self, "adopted_by", str(self.adopted_by or ""))


AnyBoardCommand = Union[
    MoveToWatchlist,
    MoveToBuylist,
    ActivateForToday,
    CancelEntry,
    RequestPartialSell,
    CancelPartialSell,
    RequestSellAll,
    CancelQueuedSellAll,
    SetOrbStop,
    SetBreakevenStop,
    SetManualStop,
    SetBreakoutPrice,
    ClearBreakoutPrice,
    ReorderCard,
    AdoptExternalOrder,
]


@dataclass(frozen=True)
class BoardActionContext:
    """Current runtime facts rechecked when a UI request is handled.

    ``enforce_runtime_fences`` is false for offline migration/unit tooling and
    for the narrow durable-intent path (Buy Today, exits, and stop requests).
    Those gestures perform no broker I/O; the authoritative runtime still
    enforces every readiness and lease fence before consuming them.
    """

    enforce_runtime_fences: bool = False
    engine_enabled: bool = False
    readiness_generation: int = 0
    reconciliation_in_progress: bool = False
    action_ready: bool = True
    device_active: bool = True
    regular_session_open: Optional[bool] = None
    session_date: Optional[date] = None
    # True only after the caller matched its local device identity against a
    # fresh cached Operator Control record.  Accepted operator-queue requests
    # carry the same authorization durably and are promoted to this state by
    # the queue consumer before applying their canonical mutation.
    local_operator_control: bool = False
    restriction_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BoardCardProjection:
    """One card plus independently-derived execution/safety facts."""

    card: TradeCardState
    ownership_owner: str = "LEGACY"
    ownership_version: int = 0
    strategy_instance_id: str = ""
    readiness_generation: int = 0
    reconciliation_blocked: bool = False
    engine_restrictions: Tuple[str, ...] = ()
    owned_order_statuses: Tuple[ExecutionOrderStatus, ...] = ()
    working_order_count: int = 0
    ambiguous_order_count: int = 0
    unlinked_owned_orders: Tuple[ExecutionOrderRecord, ...] = ()
    external_orders: Tuple[DiscoveredExternalOrder, ...] = ()

    @property
    def has_external_order_warning(self) -> bool:
        return bool(self.external_orders)

    @property
    def action_restricted(self) -> bool:
        return bool(self.engine_restrictions or self.reconciliation_blocked)


@dataclass(frozen=True)
class BoardExternalOrderProjection:
    """Standalone external order when no trade card exists for its symbol."""

    order: DiscoveredExternalOrder
    readiness_generation: int = 0
    engine_restrictions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BoardExecutionOrderProjection:
    """Standalone active owned order when no trade card exists for its symbol.

    Adoption must not fabricate a card or make the broker order disappear.
    This projection keeps the durable execution record visible as a separate,
    non-draggable audit row until it becomes terminal or is explicitly linked.
    """

    order: ExecutionOrderRecord
    readiness_generation: int = 0
    engine_restrictions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BoardProjectionContext:
    readiness_generation: int = 0
    reconciliation_blocked_accounts: Tuple[str, ...] = ()
    global_restrictions: Tuple[str, ...] = ()
    account_restrictions: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

    def restrictions_for(self, account_no: str) -> Tuple[str, ...]:
        key = str(account_no or "")
        for account, reasons in self.account_restrictions:
            if account == key:
                return tuple(reasons)
        return ()

    def reconciliation_blocked_for(self, account_no: str) -> bool:
        return str(account_no or "") in self.reconciliation_blocked_accounts


@dataclass(frozen=True)
class BoardWorkflowResult:
    """Confirmed service outcome returned to the UI."""

    card: Optional[TradeCardState]
    command_id: str
    adopted_execution_client_order_id: Optional[str] = None
