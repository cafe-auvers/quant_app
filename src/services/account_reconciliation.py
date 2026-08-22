"""Account-scoped broker reconciliation for Workstream 4 (PR3).

The network boundary and the decision boundary are deliberately separate:
``fetch_account_broker_snapshot`` performs one holdings call and one order
discovery call for an account, while ``reduce_account_reconciliation`` is a
pure function over that immutable snapshot and cloned local state.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.engine import Engine

from src.core import execution_config
from src.core.account_broker_snapshot import (
    AccountBrokerSnapshot,
    AccountHoldingSnapshot,
    ReconciliationAction,
    SnapshotCompleteness,
)
from src.core.capital_reservation import (
    CapitalReservation,
    CapitalReservationStatus,
)
from src.core.discovered_external_order import (
    DiscoveredExternalOrder,
    ExternalOrderDisposition,
    new_discovered_external_order,
    validate_disposition_transition,
)
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    OrderOrigin,
    allowed_status_transitions,
    apply_status_transition,
)
from src.core.order_recovery_state import (
    OrderRecoveryState,
    allowed_recovery_transitions,
)
from src.core.order_state import (
    RESERVED_MOO_EXECUTION,
    BrokerOrderDiscoveryResult,
    BrokerOrderStatusSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    is_open_status,
)
from src.core.trade_card_state import (
    BoardStatus,
    EntryRuntimeStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services.position_manager import PositionManager
from src.utils.market_calendar import US_MARKET_ZONE, previous_nyse_trading_day

logger = logging.getLogger(__name__)


class ReconciliationCategory(str, Enum):
    ENTRY_BUY = "ENTRY_BUY"
    ENTRY_COMPLETION_BUY = "ENTRY_COMPLETION_BUY"
    PARTIAL_SELL = "PARTIAL_SELL"
    SELL_ALL = "SELL_ALL"
    STOP_LOSS_SELL = "STOP_LOSS_SELL"
    RESERVED_MOO_SELL = "RESERVED_MOO_SELL"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"
    TERMINAL_ORDER = "TERMINAL_ORDER"
    MANUAL_BROKER_POSITION = "MANUAL_BROKER_POSITION"
    DISCOVERED_EXTERNAL_ORDER = "DISCOVERED_EXTERNAL_ORDER"
    ORPHAN_CAPITAL_RESERVATION = "ORPHAN_CAPITAL_RESERVATION"
    LIVE_ORDER_WITHOUT_RESERVATION = "LIVE_ORDER_WITHOUT_RESERVATION"
    UNRECOGNIZED = "UNRECOGNIZED"


class ReconciliationCommandType(str, Enum):
    CANCEL_KNOWN_ORDER = "CANCEL_KNOWN_ORDER"
    EMERGENCY_SELL_ALL = "EMERGENCY_SELL_ALL"


class ReconciliationAlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class ReconciliationClassification:
    category: ReconciliationCategory
    subject_id: str


@dataclass(frozen=True)
class ReconciliationCommand:
    command_type: ReconciliationCommandType
    environment: str
    account_no: str
    symbol: str
    client_order_id: str = ""
    quantity: int = 0
    reason: str = ""


@dataclass(frozen=True)
class ReconciliationAlert:
    code: str
    severity: ReconciliationAlertSeverity
    message: str
    symbol: str = ""
    client_order_id: str = ""
    broker_order_id: str = ""


@dataclass(frozen=True)
class AccountLocalState:
    cards: Tuple[TradeCardState, ...] = ()
    execution_orders: Tuple[ExecutionOrderRecord, ...] = ()
    capital_reservations: Tuple[CapitalReservation, ...] = ()
    external_orders: Tuple[DiscoveredExternalOrder, ...] = ()


@dataclass(frozen=True)
class ReconciliationPlan:
    snapshot_id: str
    card_creates: Tuple[TradeCardState, ...] = ()
    card_updates: Tuple[TradeCardState, ...] = ()
    order_updates: Tuple[ExecutionOrderRecord, ...] = ()
    reservation_updates: Tuple[CapitalReservation, ...] = ()
    external_order_creates: Tuple[DiscoveredExternalOrder, ...] = ()
    external_order_updates: Tuple[DiscoveredExternalOrder, ...] = ()
    commands: Tuple[ReconciliationCommand, ...] = ()
    alerts: Tuple[ReconciliationAlert, ...] = ()
    classifications: Tuple[ReconciliationClassification, ...] = ()

    @property
    def changed_cards(self) -> Tuple[TradeCardState, ...]:
        return self.card_creates + self.card_updates


@dataclass(frozen=True)
class AccountReconciliationResult:
    snapshot: AccountBrokerSnapshot
    plan: ReconciliationPlan


@dataclass(frozen=True)
class EmergencySellDecision:
    quantity: int = 0
    cancel_client_order_id: str = ""
    manual_intervention_required: bool = False
    reason: str = ""


_TERMINAL_EXECUTION_STATUSES = {
    ExecutionOrderStatus.FILLED,
    ExecutionOrderStatus.CANCELLED,
    ExecutionOrderStatus.REJECTED,
    ExecutionOrderStatus.EXPIRED,
    ExecutionOrderStatus.CANCELLED_LOCALLY,
    ExecutionOrderStatus.NOT_ACCEPTED_CONFIRMED,
}

_OPEN_EXECUTION_STATUSES = {
    ExecutionOrderStatus.PREPARED,
    ExecutionOrderStatus.SUBMITTING,
    ExecutionOrderStatus.ACKNOWLEDGED,
    ExecutionOrderStatus.WORKING,
    ExecutionOrderStatus.PARTIALLY_FILLED,
    ExecutionOrderStatus.CANCEL_PENDING,
    ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
}

_POSITION_SNAPSHOT_NOT_PROVIDED = object()

_BROKER_TO_EXECUTION_STATUS = {
    OrderStatus.ACCEPTED: ExecutionOrderStatus.ACKNOWLEDGED,
    OrderStatus.WORKING: ExecutionOrderStatus.WORKING,
    OrderStatus.PARTIALLY_FILLED: ExecutionOrderStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED: ExecutionOrderStatus.FILLED,
    OrderStatus.CANCEL_REQUESTED: ExecutionOrderStatus.CANCEL_PENDING,
    OrderStatus.CANCELLED: ExecutionOrderStatus.CANCELLED,
    OrderStatus.REJECTED: ExecutionOrderStatus.REJECTED,
    OrderStatus.EXPIRED: ExecutionOrderStatus.EXPIRED,
}


def _parse_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _parse_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _extract_account_holdings(position_snapshot: Optional[Mapping]) -> Tuple[AccountHoldingSnapshot, ...]:
    rows = ((position_snapshot or {}).get("overseas") or {}).get("holdings") or ()
    holdings = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol") or raw.get("ovrs_pdno") or "").strip().upper()
        quantity = _parse_int(raw.get("quantity", raw.get("ovrs_cblc_qty")))
        if not symbol or quantity is None or quantity <= 0:
            continue
        sellable = None
        for key in (
            "sellable_quantity",
            "orderable_quantity",
            "ord_psbl_qty",
            "ovrs_ord_psbl_qty",
        ):
            if key in raw:
                sellable = _parse_int(raw.get(key))
                break
        holdings.append(
            AccountHoldingSnapshot(
                symbol=symbol,
                quantity=quantity,
                average_price=_parse_float(
                    raw.get("average_price", raw.get("pchs_avg_pric", 0.0))
                ),
                sellable_quantity=sellable,
            )
        )
    return tuple(holdings)


def _us_market_session_date(observed_at: datetime) -> date:
    """Return the session label in the exchange's calendar, never UTC's date."""
    market_day = observed_at.astimezone(US_MARKET_ZONE).date()
    return previous_nyse_trading_day(market_day)


def fetch_account_broker_snapshot(
    *,
    broker,
    environment: str,
    account_no: str,
    account_balance_provider: Optional[Callable[[str, str], float]] = None,
    position_balance_extractor: Optional[
        Callable[[Mapping], Tuple[float, float]]
    ] = None,
    position_snapshot=_POSITION_SNAPSHOT_NOT_PROVIDED,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> AccountBrokerSnapshot:
    """Fetch each account source once and retain independent completeness."""
    errors = []
    holdings = ()
    holdings_complete = False
    try:
        if position_snapshot is _POSITION_SNAPSHOT_NOT_PROVIDED:
            position_snapshot = broker.get_positions(
                environment=environment, account_no=account_no
            )
        if not isinstance(position_snapshot, Mapping):
            raise ValueError("position snapshot was not a mapping")
        overseas = position_snapshot.get("overseas")
        if not isinstance(overseas, Mapping) or not isinstance(
            overseas.get("holdings"), (list, tuple)
        ):
            raise ValueError("position snapshot did not contain overseas holdings")
        holdings = _extract_account_holdings(position_snapshot)
        holdings_complete = True
    except Exception as exc:  # one failed source must not erase the others
        errors.append(f"holdings: {exc}")

    discovery = BrokerOrderDiscoveryResult()
    try:
        fetched_discovery = broker.discover_orders(
            environment=environment, account_no=account_no
        )
        if not isinstance(fetched_discovery, BrokerOrderDiscoveryResult):
            raise ValueError("order discovery returned an invalid result")
        discovery = fetched_discovery
    except Exception as exc:
        errors.append(f"orders: {exc}")

    balance = None
    equity = None
    balance_complete = False
    if holdings_complete and position_balance_extractor is not None:
        try:
            balance, equity = position_balance_extractor(position_snapshot)
            balance = float(balance)
            equity = float(equity)
            balance_complete = True
        except Exception as exc:
            errors.append(f"account_balance: {exc}")
    elif account_balance_provider is not None:
        try:
            balance = float(account_balance_provider(environment, account_no))
            balance_complete = True
        except Exception as exc:
            errors.append(f"account_balance: {exc}")

    observed_at = clock()
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return AccountBrokerSnapshot(
        environment=environment,
        account_no=account_no,
        holdings=holdings,
        orders=tuple(discovery.snapshots),
        account_buying_power=balance,
        account_equity=equity,
        observed_at=observed_at,
        session_date=_us_market_session_date(observed_at),
        errors=tuple(errors) + tuple(discovery.errors),
        completeness=SnapshotCompleteness(
            holdings_complete=holdings_complete,
            open_orders_complete=bool(discovery.open_orders_complete),
            history_complete=bool(discovery.history_complete),
            reserved_orders_complete=bool(discovery.reserved_orders_complete),
            account_balance_complete=balance_complete,
        ),
    )


def classify_execution_order(
    order: ExecutionOrderRecord, card: Optional[TradeCardState]
) -> ReconciliationCategory:
    if order.status in (
        ExecutionOrderStatus.SUBMITTING,
        ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
    ) or order.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS:
        return ReconciliationCategory.UNKNOWN_SUBMISSION
    if order.status in _TERMINAL_EXECUTION_STATUSES:
        return ReconciliationCategory.TERMINAL_ORDER
    if order.execution_policy == RESERVED_MOO_EXECUTION:
        return ReconciliationCategory.RESERVED_MOO_SELL
    if order.side == OrderSide.BUY and order.intent == OrderIntent.ENTRY:
        if card is not None and (
            card.board_status == BoardStatus.OPEN_POSITION
            or card.entry_remaining_target_quantity > 0
        ):
            return ReconciliationCategory.ENTRY_COMPLETION_BUY
        return ReconciliationCategory.ENTRY_BUY
    if order.side == OrderSide.SELL:
        if order.intent == OrderIntent.STOP_LOSS:
            return ReconciliationCategory.STOP_LOSS_SELL
        if order.intent in (
            OrderIntent.PARTIAL_EXIT,
            OrderIntent.PARTIAL_TAKE_PROFIT,
        ):
            return ReconciliationCategory.PARTIAL_SELL
        if card is not None and card.board_status == BoardStatus.SELL_ALL:
            return ReconciliationCategory.SELL_ALL
    return ReconciliationCategory.UNRECOGNIZED


def _known_owned_working_sells(
    orders: Iterable[ExecutionOrderRecord], symbol: str
) -> Tuple[ExecutionOrderRecord, ...]:
    wanted = str(symbol or "").upper()
    return tuple(
        order
        for order in orders
        if order.symbol == wanted
        and order.side == OrderSide.SELL
        and order.status in _OPEN_EXECUTION_STATUSES
        and order.origin in (OrderOrigin.APPLICATION, OrderOrigin.USER_ADOPTED)
        and order.broker_identity_status == BrokerIdentityStatus.EXACT
        and bool(order.broker_order_id)
    )


def decide_emergency_sell(
    snapshot: AccountBrokerSnapshot,
    *,
    symbol: str,
    execution_orders: Sequence[ExecutionOrderRecord],
) -> EmergencySellDecision:
    """Return the only safe emergency action; never use raw holdings blindly."""
    if not snapshot.completeness.allows(ReconciliationAction.EMERGENCY_SELL_ALL):
        return EmergencySellDecision(
            manual_intervention_required=True,
            reason="Fresh holdings are unavailable",
        )
    holding = snapshot.holding_for(symbol)
    if holding is None or holding.quantity <= 0:
        return EmergencySellDecision(reason="No broker holding remains")

    unresolved_owned_sells = tuple(
        order
        for order in execution_orders
        if order.symbol == str(symbol or "").upper()
        and order.side == OrderSide.SELL
        and order.status in _OPEN_EXECUTION_STATUSES
        and order.origin in (OrderOrigin.APPLICATION, OrderOrigin.USER_ADOPTED)
        and (
            order.broker_identity_status != BrokerIdentityStatus.EXACT
            or not order.broker_order_id
        )
    )
    if unresolved_owned_sells:
        return EmergencySellDecision(
            manual_intervention_required=True,
            reason=(
                "An owned SELL submission has unresolved broker identity; never "
                "submit another liquidation while its outcome is ambiguous"
            ),
        )

    known_broker_ids = {
        order.broker_order_id
        for order in execution_orders
        if order.broker_identity_status == BrokerIdentityStatus.EXACT
        and order.broker_order_id
    }
    external_working_orders = tuple(
        broker_order
        for broker_order in snapshot.orders
        if broker_order.symbol == str(symbol or "").upper()
        and is_open_status(broker_order.status)
        and broker_order.broker_order_id not in known_broker_ids
    )
    if external_working_orders:
        has_external_sell = any(
            order.side == OrderSide.SELL for order in external_working_orders
        )
        sellable_note = (
            f" Broker sellable quantity is {holding.sellable_quantity}."
            if holding.sellable_quantity is not None
            else ""
        )
        return EmergencySellDecision(
            manual_intervention_required=True,
            reason=(
                "An unowned working broker order fences this symbol; "
                "automatic liquidation is fenced until it is terminal or adopted."
                f"{sellable_note if has_external_sell else ''}"
            ),
        )

    known_sells = _known_owned_working_sells(execution_orders, symbol)
    if known_sells:
        unresolved = next(
            (
                order
                for order in known_sells
                if order.status == ExecutionOrderStatus.CANCEL_PENDING
                or order.recovery_state != OrderRecoveryState.NONE
            ),
            None,
        )
        if unresolved is not None:
            return EmergencySellDecision(
                manual_intervention_required=True,
                reason=(
                    "An owned SELL mutation is unresolved; preserve its command "
                    "identity and do not issue another broker call"
                ),
            )
        # Prefer controlling the exact existing order instead of creating
        # competing sell exposure.  ``quantity`` still exposes the maximum
        # safe additional amount for diagnostics/tests.
        outstanding = sum(max(0, order.remaining_quantity) for order in known_sells)
        return EmergencySellDecision(
            quantity=max(0, holding.quantity - outstanding),
            cancel_client_order_id=known_sells[0].client_order_id,
            reason="Prefer cancel/replace of the exact known working sell",
        )

    if snapshot.completeness.open_orders_complete:
        return EmergencySellDecision(quantity=holding.quantity)
    if holding.sellable_quantity is not None:
        return EmergencySellDecision(
            quantity=holding.sellable_quantity,
            reason="Open-order discovery incomplete; using broker sellable quantity",
        )
    return EmergencySellDecision(
        manual_intervention_required=True,
        reason=(
            "Outstanding sell exposure is uncertain and the broker did not provide "
            "sellable quantity"
        ),
    )


def _set_recovery_state(order: ExecutionOrderRecord, target: OrderRecoveryState) -> bool:
    if order.recovery_state == target:
        return False
    if target not in allowed_recovery_transitions(order.recovery_state):
        target = OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED
        if target not in allowed_recovery_transitions(order.recovery_state):
            return False
    order.recovery_state = target
    return True


def _clear_absence(order: ExecutionOrderRecord) -> None:
    order.absence_count = 0
    order.last_absence_snapshot_id = ""
    order.last_absence_observed_at = None
    order.last_absence_session_date = None
    order.last_absence_broker_order_id = ""
    order.last_absence_holding_quantity = None


def _parse_utc_timestamp(value: object) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stamp_exact_order_observation_if_due(
    order: ExecutionOrderRecord,
    before: ExecutionOrderRecord,
    checked_at: str,
) -> None:
    """Persist semantic broker changes immediately; coalesce audit-only time.

    ``last_broker_seen_at`` and ``last_reconciled_at`` are not authorization
    or freshness inputs anywhere in the execution path.  Rewriting an
    otherwise identical row on every broker poll consumed TiDB writes and
    invalidated the full Buy Board projection.  Active orders retain a
    hourly durable audit trail; terminal rows remain stable after their
    terminal evidence was first committed.
    """

    ignored = {"last_broker_seen_at", "last_reconciled_at", "version"}
    semantic_change = any(
        value != getattr(before, name)
        for name, value in vars(order).items()
        if name not in ignored
    )
    if semantic_change or not order.last_reconciled_at:
        due = True
    elif order.status in _TERMINAL_EXECUTION_STATUSES:
        due = False
    else:
        observed = _parse_utc_timestamp(checked_at)
        prior = _parse_utc_timestamp(order.last_reconciled_at)
        due = bool(
            observed is None
            or prior is None
            or (observed - prior).total_seconds() < 0.0
            or (observed - prior).total_seconds()
            >= execution_config.DURABLE_ORDER_OBSERVATION_SECONDS
        )
    if due:
        order.last_broker_seen_at = checked_at
        order.last_reconciled_at = checked_at


def _snapshot_status(
    snapshot: BrokerOrderStatusSnapshot,
) -> Optional[ExecutionOrderStatus]:
    return _BROKER_TO_EXECUTION_STATUS.get(snapshot.status)


def _apply_exact_order_snapshot(
    order: ExecutionOrderRecord, snapshot: BrokerOrderStatusSnapshot
) -> Optional[str]:
    """Mutate a cloned order from exact broker evidence; return contradiction text."""
    before = copy.deepcopy(order)
    _clear_absence(order)
    order.filled_quantity = max(order.filled_quantity, snapshot.filled_quantity)
    if snapshot.remaining_quantity or snapshot.status == OrderStatus.FILLED:
        order.remaining_quantity = max(0, snapshot.remaining_quantity)
    else:
        order.remaining_quantity = max(
            0, order.submitted_quantity - order.filled_quantity
        )
    if snapshot.avg_fill_price:
        order.average_fill_price = snapshot.avg_fill_price

    target = _snapshot_status(snapshot)
    if target is None:
        _set_recovery_state(order, OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED)
        _stamp_exact_order_observation_if_due(order, before, snapshot.checked_at)
        return f"Broker returned unrecognized order status {snapshot.status.value}"
    preserve_ambiguous_cancel = bool(
        order.status == ExecutionOrderStatus.CANCEL_PENDING
        and order.recovery_state == OrderRecoveryState.DISCOVERING
        and target in _OPEN_EXECUTION_STATUSES
    )
    if target != order.status and not preserve_ambiguous_cancel:
        if target in allowed_status_transitions(order.status):
            apply_status_transition(order, target)
        elif (
            target == ExecutionOrderStatus.CANCELLED
            and ExecutionOrderStatus.CANCEL_PENDING
            in allowed_status_transitions(order.status)
        ):
            # A complete broker snapshot may skip the local observation of
            # CANCEL_PENDING (for example after a restart).  Preserve the
            # validated state machine by advancing through that real
            # intermediate state instead of assigning CANCELLED directly.
            apply_status_transition(order, ExecutionOrderStatus.CANCEL_PENDING)
            apply_status_transition(order, ExecutionOrderStatus.CANCELLED)
        else:
            _set_recovery_state(
                order, OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED
            )
            _stamp_exact_order_observation_if_due(order, before, snapshot.checked_at)
            return (
                f"Broker status {target.value} contradicts local transition from "
                f"{order.status.value}"
            )
    if (
        order.recovery_state == OrderRecoveryState.DISCOVERING
        and not preserve_ambiguous_cancel
    ):
        _set_recovery_state(order, OrderRecoveryState.NONE)
    elif (
        target in _TERMINAL_EXECUTION_STATUSES
        and order.recovery_state == OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION
    ):
        _set_recovery_state(order, OrderRecoveryState.TERMINAL_RECONCILED)
    _stamp_exact_order_observation_if_due(order, before, snapshot.checked_at)
    return None


def _absence_evidence_complete(
    order: ExecutionOrderRecord, snapshot: AccountBrokerSnapshot
) -> bool:
    """Every C3 generation must carry the evidence for that order type."""
    completeness = snapshot.completeness
    if order.execution_policy == RESERVED_MOO_EXECUTION:
        return bool(
            completeness.holdings_complete
            and completeness.open_orders_complete
            and completeness.history_complete
            and completeness.reserved_orders_complete
        )
    return completeness.allows(ReconciliationAction.TERMINAL_ORDER_CONCLUSION)


def _heuristic_candidate(
    snapshot: BrokerOrderStatusSnapshot, order: ExecutionOrderRecord
) -> bool:
    if order.broker_identity_status != BrokerIdentityStatus.AMBIGUOUS:
        return False
    if snapshot.symbol != order.symbol or snapshot.side != order.side:
        return False
    if snapshot.quantity_requested and snapshot.quantity_requested != order.submitted_quantity:
        return False
    if snapshot.limit_price and order.submitted_limit_price:
        tolerance = max(0.01, order.submitted_limit_price * 0.001)
        if abs(snapshot.limit_price - order.submitted_limit_price) > tolerance:
            return False
    # A4a requires the persisted *actual* submission-time fingerprint.
    # checked_at is query time and is never an acceptable substitute.  The
    # production KIS adapter deliberately leaves submitted_at empty until
    # Workstream 0 verifies a real broker field, so the safe current
    # behavior is manual A4a + separate A4b unless a normalized protocol
    # implementation/test double supplies both timestamps.
    if not snapshot.submitted_at or not order.submission_started_at:
        return False
    try:
        broker_started = datetime.fromisoformat(snapshot.submitted_at)
        local_started = datetime.fromisoformat(order.submission_started_at)
    except (TypeError, ValueError):
        return False
    if broker_started.tzinfo is None:
        broker_started = broker_started.replace(tzinfo=timezone.utc)
    if local_started.tzinfo is None:
        local_started = local_started.replace(tzinfo=timezone.utc)
    if (
        abs((broker_started - local_started).total_seconds())
        > execution_config.AMBIGUOUS_SUBMISSION_CANDIDATE_WINDOW_SECONDS
    ):
        return False
    return True


def _record_absence(
    order: ExecutionOrderRecord,
    snapshot: AccountBrokerSnapshot,
    *,
    holding_quantity: int,
) -> str:
    """Apply C3's generation/interval/session/contradiction rules."""
    identity = order.broker_order_id
    contradiction = (
        identity in snapshot.execution_notice_broker_order_ids
        or (
            order.last_absence_holding_quantity is not None
            and holding_quantity != order.last_absence_holding_quantity
        )
    )
    if contradiction:
        _clear_absence(order)
        return "CONTRADICTION_RESET"

    if order.absence_count <= 0:
        order.absence_count = 1
        order.last_absence_snapshot_id = snapshot.snapshot_id
        order.last_absence_observed_at = snapshot.observed_at.isoformat()
        order.last_absence_session_date = snapshot.session_date.isoformat()
        order.last_absence_broker_order_id = identity
        order.last_absence_holding_quantity = holding_quantity
        return "FIRST_ABSENCE"

    if snapshot.snapshot_id == order.last_absence_snapshot_id:
        return "SAME_GENERATION"
    if order.last_absence_broker_order_id != identity:
        _clear_absence(order)
        return "IDENTITY_CHANGED_RESET"
    if order.last_absence_session_date != snapshot.session_date.isoformat():
        _clear_absence(order)
        return "SESSION_CHANGED_RESET"
    try:
        first_at = datetime.fromisoformat(str(order.last_absence_observed_at))
    except (TypeError, ValueError):
        _clear_absence(order)
        return "INVALID_FIRST_OBSERVATION_RESET"
    elapsed = (snapshot.observed_at - first_at).total_seconds()
    if elapsed < execution_config.MIN_ABSENCE_CONFIRMATION_INTERVAL_SECONDS:
        return "TOO_SOON"
    if not snapshot.completeness.holdings_complete:
        return "HOLDINGS_NOT_FRESH"

    order.absence_count = 2
    order.last_absence_snapshot_id = snapshot.snapshot_id
    order.last_absence_observed_at = snapshot.observed_at.isoformat()
    if order.recovery_state == OrderRecoveryState.AWAITING_CANCEL_CONFIRMATION:
        _set_recovery_state(order, OrderRecoveryState.TERMINAL_RECONCILED)
        return "TERMINAL_RECONCILED"
    _set_recovery_state(order, OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED)
    return "MANUAL_INTERVENTION_REQUIRED"


def _card_key(card: TradeCardState) -> Tuple[str, str, str]:
    return (card.environment, card.account_no, card.symbol)


def _order_key(order: ExecutionOrderRecord) -> Tuple[str, str, str]:
    return (order.environment, order.account_no, order.symbol)


def _card_tracks_order(
    card: Optional[TradeCardState], order: ExecutionOrderRecord
) -> bool:
    """Return whether ``card`` durably links this exact lifecycle order.

    Symbol/account equality only identifies the card row; it does not prove
    that a historical order belongs to the card's current trade cycle.  The
    caller must have retained either the exact client ID or the current
    attempt-group correlation before broker history may project onto the
    card.
    """
    if card is None:
        return False
    if order.side == OrderSide.BUY:
        return bool(
            card.entry_client_order_id == order.client_order_id
            or (
                order.attempt_group_id
                and card.entry_attempt_group_id == order.attempt_group_id
            )
        )
    return bool(
        card.exit_client_order_id == order.client_order_id
        or (
            order.attempt_group_id
            and card.exit_attempt_group_id == order.attempt_group_id
        )
    )


def _clear_entry_card_tracking(
    card: TradeCardState, *, retire_attempt_group: bool = False
) -> None:
    card.entry_client_order_id = ""
    card.entry_cancel_command_id = ""
    card.entry_cancel_in_flight = False
    card.entry_cancel_reason = ""
    card.entry_pending_attempt_number = 0
    card.entry_submission_unresolved = False
    if retire_attempt_group:
        card.entry_attempt_group_id = ""


def _clear_exit_card_tracking(
    card: TradeCardState, *, retire_attempt_group: bool = False
) -> None:
    card.exit_client_order_id = ""
    card.exit_cancel_command_id = ""
    card.exit_cancel_in_flight = False
    card.exit_cancel_requested_at = None
    card.exit_pending_attempt_number = 0
    card.exit_submission_unresolved = False
    card.reserved_sell_quantity = 0
    if retire_attempt_group:
        card.exit_attempt_group_id = ""
        card.exit_attempt_count = 0


def _consume_exit_card_attempt(
    card: TradeCardState, order: ExecutionOrderRecord
) -> None:
    """Recover the highest used exit identity from durable order truth."""
    card.exit_attempt_count = max(
        int(card.exit_attempt_count or 0),
        int(card.exit_pending_attempt_number or 0),
        int(order.attempt_number or 0),
    )
    if order.attempt_group_id:
        card.exit_attempt_group_id = order.attempt_group_id


def _operational_category(
    order: ExecutionOrderRecord,
    card: Optional[TradeCardState],
    classified: ReconciliationCategory,
) -> ReconciliationCategory:
    """Retain the order's behavioral branch after it becomes terminal."""
    if classified != ReconciliationCategory.TERMINAL_ORDER:
        return classified
    if order.execution_policy == RESERVED_MOO_EXECUTION:
        return ReconciliationCategory.RESERVED_MOO_SELL
    if order.side == OrderSide.BUY and order.intent == OrderIntent.ENTRY:
        if card is not None and (
            card.broker_quantity > 0 or card.entry_remaining_target_quantity > 0
        ):
            return ReconciliationCategory.ENTRY_COMPLETION_BUY
        return ReconciliationCategory.ENTRY_BUY
    if order.side == OrderSide.SELL:
        if order.intent == OrderIntent.STOP_LOSS:
            return ReconciliationCategory.STOP_LOSS_SELL
        if order.intent in (OrderIntent.PARTIAL_EXIT, OrderIntent.PARTIAL_TAKE_PROFIT):
            return ReconciliationCategory.PARTIAL_SELL
        return ReconciliationCategory.SELL_ALL
    return classified


def _settle_capital_reservation(
    *,
    order: ExecutionOrderRecord,
    reservation: Optional[CapitalReservation],
    observed_at: datetime,
) -> None:
    """Project cumulative fill/terminal evidence onto the linked reservation."""
    if reservation is None or not reservation.is_open():
        return
    fill_price = order.average_fill_price or order.submitted_limit_price
    consumed_notional = max(0.0, order.filled_quantity * fill_price)
    if order.status in _TERMINAL_EXECUTION_STATUSES:
        reservation.remaining_reserved_notional = 0.0
        reservation.status = (
            CapitalReservationStatus.CONSUMED
            if reservation.requested_notional > 0
            and consumed_notional >= reservation.requested_notional
            else CapitalReservationStatus.RELEASED
        )
        reservation.released_at = observed_at
        return
    if order.side != OrderSide.BUY or order.intent != OrderIntent.ENTRY:
        return
    target_remaining = max(
        0.0, reservation.requested_notional - consumed_notional
    )
    if target_remaining == reservation.remaining_reserved_notional:
        return
    reservation.remaining_reserved_notional = target_remaining
    reservation.status = (
        CapitalReservationStatus.CONSUMED
        if target_remaining <= 0
        else CapitalReservationStatus.PARTIALLY_CONSUMED
    )


def _clear_reservation_absence(reservation: CapitalReservation) -> None:
    reservation.absence_count = 0
    reservation.last_absence_snapshot_id = ""
    reservation.last_absence_observed_at = None
    reservation.last_absence_session_date = None


def _record_orphan_reservation_absence(
    reservation: CapitalReservation, snapshot: AccountBrokerSnapshot
) -> str:
    """Require two durable, complete account generations before release."""
    completeness = snapshot.completeness
    complete = bool(
        completeness.holdings_complete
        and completeness.open_orders_complete
        and completeness.history_complete
        and completeness.reserved_orders_complete
    )
    if not complete:
        return "INCOMPLETE_EVIDENCE"
    if snapshot.holding_for(reservation.symbol) is not None or any(
        order.symbol == reservation.symbol for order in snapshot.orders
    ):
        if reservation.absence_count:
            _clear_reservation_absence(reservation)
        return "CONTRADICTORY_BROKER_EVIDENCE"
    session = snapshot.session_date.isoformat()
    if reservation.absence_count <= 0:
        reservation.absence_count = 1
        reservation.last_absence_snapshot_id = snapshot.snapshot_id
        reservation.last_absence_observed_at = snapshot.observed_at.isoformat()
        reservation.last_absence_session_date = session
        return "FIRST_ABSENCE"
    if reservation.last_absence_snapshot_id == snapshot.snapshot_id:
        return "SAME_GENERATION"
    if reservation.last_absence_session_date != session:
        _clear_reservation_absence(reservation)
        return "SESSION_CHANGED_RESET"
    try:
        first_at = datetime.fromisoformat(
            str(reservation.last_absence_observed_at)
        )
    except (TypeError, ValueError):
        _clear_reservation_absence(reservation)
        return "INVALID_FIRST_OBSERVATION_RESET"
    if (
        snapshot.observed_at - first_at
    ).total_seconds() < execution_config.MIN_ABSENCE_CONFIRMATION_INTERVAL_SECONDS:
        return "TOO_SOON"
    reservation.absence_count = 2
    reservation.last_absence_snapshot_id = snapshot.snapshot_id
    reservation.last_absence_observed_at = snapshot.observed_at.isoformat()
    reservation.remaining_reserved_notional = 0.0
    reservation.status = CapitalReservationStatus.RELEASED
    reservation.released_at = snapshot.observed_at
    return "RELEASED_AFTER_TWO_COMPLETE_GENERATIONS"


def _project_exact_order_to_card(
    *,
    order: ExecutionOrderRecord,
    broker_snapshot: BrokerOrderStatusSnapshot,
    account_snapshot: AccountBrokerSnapshot,
    card: Optional[TradeCardState],
    category: ReconciliationCategory,
    position_manager: PositionManager,
    alerts: list[ReconciliationAlert],
) -> None:
    """C4's explicit card projection for each operational order category."""
    if card is None:
        return
    target = _snapshot_status(broker_snapshot)
    terminal = target in _TERMINAL_EXECUTION_STATUSES
    open_at_broker = is_open_status(broker_snapshot.status)
    holding = (
        account_snapshot.holding_for(order.symbol)
        if account_snapshot.completeness.holdings_complete
        else None
    )
    operational = _operational_category(order, card, category)
    if order.side == OrderSide.SELL:
        # The order row is persisted before the broker call. It is therefore
        # the authoritative restart-boundary proof that this logical attempt
        # number was consumed, even if the process crashed before the normal
        # end-of-heartbeat card persistence.
        _consume_exit_card_attempt(card, order)

    if operational in (
        ReconciliationCategory.ENTRY_BUY,
        ReconciliationCategory.ENTRY_COMPLETION_BUY,
    ):
        if broker_snapshot.filled_quantity > 0:
            quantity = (
                holding.quantity
                if holding is not None
                else max(card.broker_quantity, broker_snapshot.filled_quantity)
            )
            card.board_status = BoardStatus.OPEN_POSITION
            card.broker_quantity = quantity
            card.orderable_quantity = (
                holding.sellable_quantity
                if holding is not None and holding.sellable_quantity is not None
                else quantity
            )
            card.average_entry_price = (
                holding.average_price
                if holding is not None and holding.average_price
                else broker_snapshot.avg_fill_price
            )
            remaining_target = max(0, card.target_position_quantity - quantity)
            card.entry_remaining_target_quantity = remaining_target
            card.position_runtime_status = (
                PositionRuntimeStatus.ENTRY_COMPLETING
                if open_at_broker and remaining_target > 0
                else PositionRuntimeStatus.OPEN
            )
            card.entry_runtime_status = (
                EntryRuntimeStatus.ORDER_PENDING if open_at_broker else None
            )
            card.entry_client_order_id = order.client_order_id if open_at_broker else ""
            if card.stop_type is None and card.entry_orb_low:
                position_manager.apply_first_fill_stop(
                    card,
                    entry_orb_low=card.entry_orb_low,
                    entry_orb_window=(
                        card.entry_orb_window or card.selected_orb_window or ""
                    ),
                )
            else:
                card.stop_quantity = quantity
            if terminal:
                retry_remainder = (
                    card.entry_cancel_reason == "TTL_REPRICE"
                    and card.entry_remaining_target_quantity > 0
                )
                if not retry_remainder:
                    card.entry_remaining_target_quantity = 0
                    card.position_runtime_status = PositionRuntimeStatus.OPEN
                _clear_entry_card_tracking(
                    card, retire_attempt_group=not retry_remainder
                )
        elif terminal:
            if card.broker_quantity > 0:
                card.board_status = BoardStatus.OPEN_POSITION
                card.position_runtime_status = PositionRuntimeStatus.OPEN
            else:
                card.board_status = BoardStatus.BUYLIST
            card.entry_runtime_status = None
            card.entry_remaining_target_quantity = 0
            _clear_entry_card_tracking(card, retire_attempt_group=True)
        elif open_at_broker:
            card.board_status = BoardStatus.ENTRY_PENDING
            card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
            card.entry_client_order_id = order.client_order_id
        return

    if operational == ReconciliationCategory.PARTIAL_SELL:
        if open_at_broker:
            card.board_status = BoardStatus.PARTIAL_SELL
            card.position_runtime_status = PositionRuntimeStatus.PARTIAL_EXIT_PENDING
            card.reserved_sell_quantity = max(0, broker_snapshot.remaining_quantity)
            card.exit_client_order_id = order.client_order_id
            card.exit_pending_attempt_number = max(
                card.exit_pending_attempt_number, order.attempt_number
            )
            return
        if not terminal:
            return
        if not account_snapshot.completeness.holdings_complete:
            card.exit_submission_unresolved = True
            alerts.append(
                ReconciliationAlert(
                    "PARTIAL_EXIT_TERMINAL_WITHOUT_HOLDINGS",
                    ReconciliationAlertSeverity.CRITICAL,
                    "Terminal partial exit cannot be projected without fresh holdings",
                    order.symbol,
                    order.client_order_id,
                    order.broker_order_id,
                )
            )
            return
        remaining = holding.quantity if holding is not None else 0
        card.pending_partial_sell_quantity = 0
        _clear_exit_card_tracking(card, retire_attempt_group=True)
        if remaining <= 0:
            card.broker_quantity = 0
            card.orderable_quantity = 0
            position_manager.confirm_flat(card)
        elif broker_snapshot.filled_quantity > 0:
            position_manager.on_partial_exit_filled(
                card, refreshed_broker_quantity=remaining
            )
        else:
            card.broker_quantity = remaining
            card.orderable_quantity = (
                holding.sellable_quantity
                if holding is not None and holding.sellable_quantity is not None
                else remaining
            )
            card.board_status = BoardStatus.OPEN_POSITION
            card.position_runtime_status = PositionRuntimeStatus.OPEN
        return

    if operational in (
        ReconciliationCategory.SELL_ALL,
        ReconciliationCategory.STOP_LOSS_SELL,
        ReconciliationCategory.RESERVED_MOO_SELL,
    ):
        if open_at_broker:
            card.board_status = BoardStatus.SELL_ALL
            card.exit_all_required = True
            card.sell_all_at_market_open = (
                operational == ReconciliationCategory.RESERVED_MOO_SELL
            )
            card.position_runtime_status = (
                PositionRuntimeStatus.QUEUED_FOR_OPEN
                if card.sell_all_at_market_open
                else PositionRuntimeStatus.LIQUIDATING
            )
            card.reserved_sell_quantity = max(0, broker_snapshot.remaining_quantity)
            card.exit_client_order_id = order.client_order_id
            card.exit_pending_attempt_number = max(
                card.exit_pending_attempt_number, order.attempt_number
            )
            return
        if not terminal:
            return
        if not account_snapshot.completeness.holdings_complete:
            card.exit_submission_unresolved = True
            alerts.append(
                ReconciliationAlert(
                    "LIQUIDATION_TERMINAL_WITHOUT_HOLDINGS",
                    ReconciliationAlertSeverity.CRITICAL,
                    "Terminal liquidation cannot be projected without fresh holdings",
                    order.symbol,
                    order.client_order_id,
                    order.broker_order_id,
                )
            )
            return
        remaining = holding.quantity if holding is not None else 0
        _clear_exit_card_tracking(card, retire_attempt_group=remaining <= 0)
        card.sell_all_at_market_open = False
        card.broker_quantity = remaining
        card.orderable_quantity = (
            holding.sellable_quantity
            if holding is not None and holding.sellable_quantity is not None
            else remaining
        )
        if remaining <= 0:
            position_manager.confirm_flat(card)
        else:
            card.board_status = BoardStatus.SELL_ALL
            card.exit_all_required = True
            card.position_runtime_status = PositionRuntimeStatus.LIQUIDATING


def reduce_account_reconciliation(
    snapshot: AccountBrokerSnapshot,
    local_state: AccountLocalState,
) -> ReconciliationPlan:
    """Pure reducer: no broker, database, filesystem, or clock calls."""
    original_cards = tuple(local_state.cards)
    original_orders = tuple(local_state.execution_orders)
    original_reservations = tuple(local_state.capital_reservations)
    original_external = tuple(local_state.external_orders)
    cards = [copy.deepcopy(card) for card in original_cards]
    orders = [copy.deepcopy(order) for order in original_orders]
    reservations = [copy.deepcopy(item) for item in original_reservations]
    external_orders = [copy.deepcopy(item) for item in original_external]

    card_by_key = {_card_key(card): card for card in cards}
    order_by_broker_id = {
        order.broker_order_id: order
        for order in orders
        if order.broker_identity_status == BrokerIdentityStatus.EXACT
        and order.broker_order_id
    }
    external_by_broker_id = {
        order.broker_order_id: order for order in external_orders
    }
    reservation_by_id = {item.reservation_id: item for item in reservations}
    alerts = []
    commands = []
    classifications = []
    category_by_client_order_id = {}
    consumed_broker_ids = set()
    position_manager = PositionManager()

    # C4: every local order is categorized before any decision is made.
    for order in orders:
        card = card_by_key.get(_order_key(order))
        category = classify_execution_order(order, card)
        category_by_client_order_id[order.client_order_id] = category
        classifications.append(
            ReconciliationClassification(category, order.client_order_id)
        )
        if (
            card is not None
            and _card_tracks_order(card, order)
            and category in (
                ReconciliationCategory.ENTRY_BUY,
                ReconciliationCategory.ENTRY_COMPLETION_BUY,
            )
            and order.status in _OPEN_EXECUTION_STATUSES
            and card.board_status == BoardStatus.BUY_TODAY
        ):
            card.board_status = BoardStatus.ENTRY_PENDING
            card.entry_runtime_status = EntryRuntimeStatus.ORDER_PENDING
            card.entry_attempt_group_id = order.attempt_group_id
            card.entry_attempt_count = order.attempt_number
        if category == ReconciliationCategory.UNRECOGNIZED:
            alerts.append(
                ReconciliationAlert(
                    "UNRECOGNIZED_LOCAL_ORDER_COMBINATION",
                    ReconciliationAlertSeverity.CRITICAL,
                    "Local order combination has no reconciliation branch",
                    order.symbol,
                    order.client_order_id,
                )
            )

    # C3 primary path: exact broker-order identity.
    for broker_snapshot in snapshot.orders:
        exact = order_by_broker_id.get(broker_snapshot.broker_order_id)
        if exact is None:
            continue
        consumed_broker_ids.add(broker_snapshot.broker_order_id)
        contradiction = _apply_exact_order_snapshot(exact, broker_snapshot)
        if contradiction:
            alerts.append(
                ReconciliationAlert(
                    "BROKER_STATUS_CONTRADICTION",
                    ReconciliationAlertSeverity.CRITICAL,
                    contradiction,
                    exact.symbol,
                    exact.client_order_id,
                    exact.broker_order_id,
                )
            )
        card = card_by_key.get(_order_key(exact))
        linked_card = card if _card_tracks_order(card, exact) else None
        _project_exact_order_to_card(
            order=exact,
            broker_snapshot=broker_snapshot,
            account_snapshot=snapshot,
            card=linked_card,
            category=category_by_client_order_id[exact.client_order_id],
            position_manager=position_manager,
            alerts=alerts,
        )
        _settle_capital_reservation(
            order=exact,
            reservation=reservation_by_id.get(exact.capital_reservation_id),
            observed_at=snapshot.observed_at,
        )

    # A4a conservative path.  A heuristic candidate consumes the broker
    # snapshot for classification only; it never grants ownership/exact ID.
    ambiguous_orders = [
        order
        for order in orders
        if order.broker_identity_status == BrokerIdentityStatus.AMBIGUOUS
        and order.status in (
            ExecutionOrderStatus.SUBMITTING,
            ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE,
        )
    ]
    candidate_ids = set()
    for broker_snapshot in snapshot.orders:
        if broker_snapshot.broker_order_id in consumed_broker_ids:
            continue
        candidates = [
            order
            for order in ambiguous_orders
            if order.client_order_id not in candidate_ids
            and _heuristic_candidate(broker_snapshot, order)
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item.submission_started_at or item.prepared_at, item.client_order_id))
        candidate = candidates[0]
        candidate_ids.add(candidate.client_order_id)
        consumed_broker_ids.add(broker_snapshot.broker_order_id)
        if candidate.recovery_state == OrderRecoveryState.NONE:
            _set_recovery_state(candidate, OrderRecoveryState.DISCOVERING)
        _set_recovery_state(candidate, OrderRecoveryState.BROKER_IDENTITY_UNCERTAIN)
        candidate.recovery_candidate_broker_order_ids = tuple(
            dict.fromkeys(
                (
                    *candidate.recovery_candidate_broker_order_ids,
                    broker_snapshot.broker_order_id,
                )
            )
        )
        alerts.append(
            ReconciliationAlert(
                "AMBIGUOUS_SUBMISSION_HEURISTIC_CANDIDATE",
                ReconciliationAlertSeverity.CRITICAL,
                "Candidate retained for manual resolution; broker identity was not claimed",
                candidate.symbol,
                candidate.client_order_id,
            )
        )
        if len(candidates) > 1:
            alerts.append(
                ReconciliationAlert(
                    "AMBIGUOUS_CANDIDATE_COLLISION",
                    ReconciliationAlertSeverity.CRITICAL,
                    "One broker order matched multiple ambiguous local submissions",
                    candidate.symbol,
                    candidate.client_order_id,
                )
            )

    if snapshot.completeness.open_orders_complete and snapshot.completeness.history_complete:
        for order in ambiguous_orders:
            if order.client_order_id in candidate_ids:
                continue
            if order.recovery_state == OrderRecoveryState.NONE:
                _set_recovery_state(order, OrderRecoveryState.DISCOVERING)
            _set_recovery_state(order, OrderRecoveryState.MANUAL_INTERVENTION_REQUIRED)
            alerts.append(
                ReconciliationAlert(
                    "AMBIGUOUS_SUBMISSION_MANUAL_INTERVENTION",
                    ReconciliationAlertSeverity.CRITICAL,
                    "No verified external correlation capability exists; never resubmit",
                    order.symbol,
                    order.client_order_id,
                )
            )

    # A4b precedence step 4: only snapshots unclaimed by exact or heuristic
    # local matching become external orders.
    for broker_snapshot in snapshot.orders:
        broker_id = broker_snapshot.broker_order_id
        if not broker_id or broker_id in consumed_broker_ids:
            continue
        existing_external = external_by_broker_id.get(broker_id)
        mapped_status = _snapshot_status(broker_snapshot)
        if mapped_status is None:
            classifications.append(
                ReconciliationClassification(
                    ReconciliationCategory.UNRECOGNIZED,
                    broker_id,
                )
            )
            alerts.append(
                ReconciliationAlert(
                    "UNRECOGNIZED_BROKER_ORDER_STATUS",
                    ReconciliationAlertSeverity.CRITICAL,
                    "Broker order status could not be normalized safely",
                    broker_snapshot.symbol,
                    broker_order_id=broker_id,
                )
            )
            # UNKNOWN/CREATED/SUBMITTING are broker-open states even when
            # they cannot be projected onto the normal execution status
            # table. Persist the A4b fence instead of merely alerting and
            # then allowing the heartbeat to submit beside them.
            mapped_status = ExecutionOrderStatus.UNKNOWN_SUBMISSION_STATE
        if existing_external is None:
            external = new_discovered_external_order(
                environment=snapshot.environment,
                account_no=snapshot.account_no,
                symbol=broker_snapshot.symbol,
                side=broker_snapshot.side,
                broker_order_id=broker_id,
                quantity_requested=broker_snapshot.quantity_requested,
                filled_quantity=broker_snapshot.filled_quantity,
                limit_price=broker_snapshot.limit_price,
                broker_status=mapped_status,
                raw_response=broker_snapshot.raw_response,
                external_order_id=uuid5(
                    NAMESPACE_URL,
                    (
                        "quant-app://discovered-order/"
                        f"{snapshot.environment}/{snapshot.account_no}/{broker_id}"
                    ),
                ).hex,
                discovered_at=snapshot.observed_at.isoformat(),
            )
            terminal_external = mapped_status in _TERMINAL_EXECUTION_STATUSES
            if terminal_external:
                validate_disposition_transition(
                    external.disposition,
                    ExternalOrderDisposition.DISMISSED_TERMINAL,
                )
                external.disposition = ExternalOrderDisposition.DISMISSED_TERMINAL
            external_orders.append(external)
            external_by_broker_id[broker_id] = external
            classifications.append(
                ReconciliationClassification(
                    ReconciliationCategory.DISCOVERED_EXTERNAL_ORDER,
                    external.external_order_id,
                )
            )
            if not terminal_external:
                alerts.append(
                    ReconciliationAlert(
                        "DISCOVERED_UNOWNED_BROKER_ORDER",
                        ReconciliationAlertSeverity.CRITICAL,
                        (
                            "Broker order is not claimed by any local record; all "
                            "mutations for this account and symbol are fenced"
                        ),
                        broker_snapshot.symbol,
                        broker_order_id=broker_id,
                    )
                )
        else:
            existing_external.broker_status = mapped_status
            existing_external.filled_quantity = broker_snapshot.filled_quantity
            if (
                existing_external.disposition
                == ExternalOrderDisposition.DISCOVERED_UNOWNED
                and existing_external.broker_status in _TERMINAL_EXECUTION_STATUSES
            ):
                validate_disposition_transition(
                    existing_external.disposition,
                    ExternalOrderDisposition.DISMISSED_TERMINAL,
                )
                existing_external.disposition = ExternalOrderDisposition.DISMISSED_TERMINAL
            elif (
                existing_external.disposition
                == ExternalOrderDisposition.DISCOVERED_UNOWNED
                and mapped_status in _OPEN_EXECUTION_STATUSES
            ):
                alerts.append(
                    ReconciliationAlert(
                        "DISCOVERED_UNOWNED_BROKER_ORDER",
                        ReconciliationAlertSeverity.CRITICAL,
                        (
                            "Active unowned broker order continues to fence all "
                            "mutations for this account and symbol"
                        ),
                        broker_snapshot.symbol,
                        broker_order_id=broker_id,
                    )
                )

    alerted_external_ids = {
        alert.broker_order_id
        for alert in alerts
        if alert.code == "DISCOVERED_UNOWNED_BROKER_ORDER"
    }
    for external in external_orders:
        if (
            external.disposition == ExternalOrderDisposition.DISCOVERED_UNOWNED
            and external.broker_status in _OPEN_EXECUTION_STATUSES
            and external.broker_order_id not in alerted_external_ids
        ):
            alerts.append(
                ReconciliationAlert(
                    "DISCOVERED_UNOWNED_BROKER_ORDER",
                    ReconciliationAlertSeverity.CRITICAL,
                    (
                        "Durable active unowned broker order continues to fence "
                        "all mutations for this account and symbol"
                    ),
                    external.symbol,
                    broker_order_id=external.broker_order_id,
                )
            )

    # Holdings reconciliation is independent from reserved/history failures.
    if snapshot.completeness.allows(ReconciliationAction.POSITION_QUANTITY_UPDATE):
        holding_by_symbol = {holding.symbol: holding for holding in snapshot.holdings}
        for holding in snapshot.holdings:
            key = (snapshot.environment, snapshot.account_no, holding.symbol)
            card = card_by_key.get(key)
            if card is None:
                card = TradeCardState(
                    environment=snapshot.environment,
                    account_no=snapshot.account_no,
                    symbol=holding.symbol,
                    name=holding.symbol,
                    board_status=BoardStatus.OPEN_POSITION,
                    watchlist_member=True,
                    buylist_member=True,
                    return_to_buylist_after_close=True,
                    broker_quantity=holding.quantity,
                    orderable_quantity=(
                        holding.sellable_quantity
                        if holding.sellable_quantity is not None
                        else holding.quantity
                    ),
                    average_entry_price=holding.average_price,
                    position_runtime_status=PositionRuntimeStatus.OPEN,
                    entry_block_reason="manual_position_requires_stop",
                    warnings=["STOP_REQUIRED"],
                    board_status_updated_at=snapshot.observed_at,
                    session_date=snapshot.session_date,
                    created_at=snapshot.observed_at,
                    updated_at=snapshot.observed_at,
                )
                cards.append(card)
                card_by_key[key] = card
                classifications.append(
                    ReconciliationClassification(
                        ReconciliationCategory.MANUAL_BROKER_POSITION,
                        f"{snapshot.environment}:{snapshot.account_no}:{holding.symbol}",
                    )
                )
            else:
                card.broker_quantity = holding.quantity
                card.orderable_quantity = (
                    holding.sellable_quantity
                    if holding.sellable_quantity is not None
                    else holding.quantity
                )
                if holding.average_price:
                    card.average_entry_price = holding.average_price
                if card.board_status in (
                    BoardStatus.WATCHLIST,
                    BoardStatus.BUYLIST,
                    BoardStatus.BUY_TODAY,
                    BoardStatus.ENTRY_PENDING,
                ):
                    card.board_status = BoardStatus.OPEN_POSITION
                    card.position_runtime_status = PositionRuntimeStatus.OPEN
                    card.entry_runtime_status = None
                    if card.stop_type is None:
                        if "STOP_REQUIRED" not in card.warnings:
                            card.warnings = [*card.warnings, "STOP_REQUIRED"]
                        card.entry_block_reason = "broker_position_requires_stop"
        for card in cards:
            if (
                card.environment == snapshot.environment
                and card.account_no == snapshot.account_no
                and card.broker_quantity > 0
                and card.symbol not in holding_by_symbol
            ):
                card.broker_quantity = 0
                card.orderable_quantity = 0
                if card.board_status in (
                    BoardStatus.OPEN_POSITION,
                    BoardStatus.PARTIAL_SELL,
                    BoardStatus.SELL_ALL,
                ):
                    position_manager.confirm_flat(card)

        # Under A1 a guarded broker call cannot exist without its durable
        # ExecutionOrderRecord.  Therefore a stale ENTRY_PENDING card with
        # no local record, no position, and complete open/history evidence
        # is safe to return to Buylist; it is not an ambiguous submission.
        local_order_keys = {_order_key(order) for order in orders}
        broker_symbols = {order.symbol for order in snapshot.orders}
        if snapshot.completeness.allows(
            ReconciliationAction.TERMINAL_ORDER_CONCLUSION
        ):
            for card in cards:
                if (
                    card.board_status == BoardStatus.ENTRY_PENDING
                    and _card_key(card) not in local_order_keys
                    and card.symbol not in holding_by_symbol
                    and card.symbol not in broker_symbols
                ):
                    card.board_status = BoardStatus.BUYLIST
                    card.entry_runtime_status = None
                    _clear_entry_card_tracking(card, retire_attempt_group=True)

    # C3 fallback: every generation must include complete evidence for the
    # specific execution policy (regular versus broker-reserved MOO).
    for order in orders:
        if (
            not order.broker_order_id
            or order.status in _TERMINAL_EXECUTION_STATUSES
            or order.broker_order_id in consumed_broker_ids
            or not _absence_evidence_complete(order, snapshot)
        ):
            continue
        holding = snapshot.holding_for(order.symbol)
        outcome = _record_absence(
            order,
            snapshot,
            holding_quantity=holding.quantity if holding is not None else 0,
        )
        if outcome in (
            "MANUAL_INTERVENTION_REQUIRED",
            "CONTRADICTION_RESET",
        ):
            alerts.append(
                ReconciliationAlert(
                    f"ORDER_ABSENCE_{outcome}",
                    ReconciliationAlertSeverity.CRITICAL,
                    "Broker absence could not be resolved automatically",
                    order.symbol,
                    order.client_order_id,
                    order.broker_order_id,
                )
            )

    # Capital mismatches are explicit categories, never silent repairs.
    live_orders = [order for order in orders if order.status in _OPEN_EXECUTION_STATUSES]
    referenced_reservations = {
        order.capital_reservation_id
        for order in orders
        if order.capital_reservation_id
    }
    for order in orders:
        if order.status in _TERMINAL_EXECUTION_STATUSES:
            _settle_capital_reservation(
                order=order,
                reservation=reservation_by_id.get(order.capital_reservation_id),
                observed_at=snapshot.observed_at,
            )
    for reservation in reservations:
        if reservation.is_open() and reservation.reservation_id not in referenced_reservations:
            classifications.append(
                ReconciliationClassification(
                    ReconciliationCategory.ORPHAN_CAPITAL_RESERVATION,
                    reservation.reservation_id,
                )
            )
            outcome = _record_orphan_reservation_absence(reservation, snapshot)
            alerts.append(
                ReconciliationAlert(
                    "ORPHAN_CAPITAL_RESERVATION_REQUIRES_REVIEW",
                    (
                        ReconciliationAlertSeverity.INFO
                        if outcome == "RELEASED_AFTER_TWO_COMPLETE_GENERATIONS"
                        else ReconciliationAlertSeverity.CRITICAL
                    ),
                    (
                        "Orphan reservation evidence outcome: "
                        f"{outcome}. Capital is released only after two complete, "
                        "independent account generations in the same US session."
                    ),
                    reservation.symbol,
                )
            )
    for order in live_orders:
        # Capital reservations are an entry-buy invariant.  Exit orders do
        # not reserve buying power and must not be mislabeled as corrupt.
        if order.side != OrderSide.BUY or order.intent != OrderIntent.ENTRY:
            continue
        if order.capital_reservation_id and order.capital_reservation_id in reservation_by_id:
            continue
        classifications.append(
            ReconciliationClassification(
                ReconciliationCategory.LIVE_ORDER_WITHOUT_RESERVATION,
                order.client_order_id,
            )
        )
        alerts.append(
            ReconciliationAlert(
                "LIVE_ORDER_WITHOUT_CAPITAL_RESERVATION",
                ReconciliationAlertSeverity.CRITICAL,
                "Live order has no matching durable capital reservation",
                order.symbol,
                order.client_order_id,
            )
        )

    # Emergency Sell All is an action-completeness decision, not a global
    # snapshot completeness check.
    for card in cards:
        if card.board_status != BoardStatus.SELL_ALL and not card.exit_all_required:
            continue
        decision = decide_emergency_sell(
            snapshot, symbol=card.symbol, execution_orders=orders
        )
        if decision.cancel_client_order_id:
            commands.append(
                ReconciliationCommand(
                    ReconciliationCommandType.CANCEL_KNOWN_ORDER,
                    snapshot.environment,
                    snapshot.account_no,
                    card.symbol,
                    client_order_id=decision.cancel_client_order_id,
                    reason=decision.reason,
                )
            )
        elif decision.quantity > 0:
            commands.append(
                ReconciliationCommand(
                    ReconciliationCommandType.EMERGENCY_SELL_ALL,
                    snapshot.environment,
                    snapshot.account_no,
                    card.symbol,
                    quantity=decision.quantity,
                    reason=decision.reason,
                )
            )
        elif decision.manual_intervention_required:
            alerts.append(
                ReconciliationAlert(
                    "EMERGENCY_SELL_QUANTITY_UNCERTAIN",
                    ReconciliationAlertSeverity.CRITICAL,
                    decision.reason,
                    card.symbol,
                )
            )

    original_card_by_key = {_card_key(card): card for card in original_cards}
    card_creates = tuple(card for card in cards if _card_key(card) not in original_card_by_key)
    card_updates = tuple(
        card
        for card in cards
        if _card_key(card) in original_card_by_key
        and card != original_card_by_key[_card_key(card)]
    )
    original_order_by_id = {order.client_order_id: order for order in original_orders}
    order_updates = tuple(
        order
        for order in orders
        if order != original_order_by_id[order.client_order_id]
    )
    original_reservation_by_id = {
        item.reservation_id: item for item in original_reservations
    }
    reservation_updates = tuple(
        item
        for item in reservations
        if item != original_reservation_by_id[item.reservation_id]
    )
    original_external_by_id = {
        item.external_order_id: item for item in original_external
    }
    external_creates = tuple(
        item
        for item in external_orders
        if item.external_order_id not in original_external_by_id
    )
    external_updates = tuple(
        item
        for item in external_orders
        if item.external_order_id in original_external_by_id
        and item != original_external_by_id[item.external_order_id]
    )
    return ReconciliationPlan(
        snapshot_id=snapshot.snapshot_id,
        card_creates=card_creates,
        card_updates=card_updates,
        order_updates=order_updates,
        reservation_updates=reservation_updates,
        external_order_creates=external_creates,
        external_order_updates=external_updates,
        commands=tuple(commands),
        alerts=tuple(alerts),
        classifications=tuple(classifications),
    )


def load_account_local_state(
    engine: Engine,
    *,
    environment: str,
    account_no: str,
    cards: Sequence[TradeCardState],
) -> AccountLocalState:
    """Load every local source once for the same account-scoped pass."""
    from src.services.capital_reservation_repository import list_active_reservations
    from src.services.discovered_external_order_repository import (
        list_discovered_external_orders_for_account,
    )
    from src.services.execution_order_repository import (
        list_execution_orders_for_account,
    )

    return AccountLocalState(
        cards=tuple(
            card
            for card in cards
            if card.environment == str(environment or "").upper()
            and card.account_no == str(account_no or "")
        ),
        execution_orders=tuple(
            list_execution_orders_for_account(
                engine, environment=environment, account_no=account_no
            )
        ),
        capital_reservations=tuple(
            list_active_reservations(
                engine, environment=environment, account_no=account_no
            )
        ),
        external_orders=tuple(
            list_discovered_external_orders_for_account(
                engine, environment=environment, account_no=account_no
            )
        ),
    )


def apply_reconciliation_plan(engine: Engine, plan: ReconciliationPlan) -> None:
    """Atomically persist one account plan without re-running decisions."""
    from src.services import trade_card_repository
    from src.services.capital_reservation_repository import (
        ensure_capital_reservations_table,
        update_reservation,
    )
    from src.services.discovered_external_order_repository import (
        ensure_discovered_external_orders_table,
        get_discovered_external_order_by_broker_id,
        insert_discovered_external_order,
        update_discovered_external_order,
    )
    from src.services.execution_order_repository import (
        ensure_execution_orders_table,
        update_execution_order,
    )
    from src.services.stop_change_coordinator import stop_change_coordinator_for

    # DDL/table discovery stays outside the account transaction so SQLite
    # and MySQL never open an implicit second connection while it is active.
    trade_card_repository.ensure_trade_cards_table(engine)
    ensure_execution_orders_table(engine)
    ensure_capital_reservations_table(engine)
    ensure_discovered_external_orders_table(engine)

    versioned = [
        *plan.card_creates,
        *plan.card_updates,
        *plan.order_updates,
        *plan.reservation_updates,
        *plan.external_order_creates,
        *plan.external_order_updates,
    ]
    original_versions = {id(item): item.version for item in versioned}
    original_card_updated_at = {
        id(card): card.updated_at for card in (*plan.card_creates, *plan.card_updates)
    }
    coordinator = stop_change_coordinator_for(engine)
    changed_cards = tuple(plan.changed_cards)
    with coordinator.lock_cards(card.card_key for card in changed_cards):
        try:
            with engine.begin() as conn:
                for card in plan.card_creates:
                    trade_card_repository.insert_trade_card(conn, card)
                for card in plan.card_updates:
                    trade_card_repository.update_trade_card_in_transaction(
                        conn, card, expected_version=original_versions[id(card)]
                    )
                for order in plan.order_updates:
                    update_execution_order(
                        conn, order, expected_version=original_versions[id(order)]
                    )
                for reservation in plan.reservation_updates:
                    update_reservation(
                        conn,
                        reservation,
                        expected_version=original_versions[id(reservation)],
                    )
                for external in plan.external_order_creates:
                    existing = get_discovered_external_order_by_broker_id(
                        conn,
                        environment=external.environment,
                        account_no=external.account_no,
                        broker_order_id=external.broker_order_id,
                    )
                    if existing is None:
                        insert_discovered_external_order(conn, external)
                for external in plan.external_order_updates:
                    update_discovered_external_order(
                        conn,
                        external,
                        expected_version=original_versions[id(external)],
                    )
        except Exception:
            # These are cloned plan values, but resetting their optimistic
            # versions keeps a caller from accidentally replaying a rolled-back
            # in-memory version after a transient database failure.
            for item in versioned:
                item.version = original_versions[id(item)]
            for card in (*plan.card_creates, *plan.card_updates):
                card.updated_at = original_card_updated_at[id(card)]
            raise
        for card in changed_cards:
            coordinator.reconcile_durable(card)

    # The database is authoritative. Refresh the local recovery snapshot
    # only after the whole account transaction commits.
    for card in changed_cards:
        try:
            trade_card_repository.sync_trade_card_local_snapshot(card)
        except Exception:
            logger.exception(
                "Failed to refresh local card snapshot after atomic account plan"
            )


def run_account_reconciliation_pass(
    *,
    broker,
    engine: Engine,
    environment: str,
    account_no: str,
    cards: Sequence[TradeCardState],
    account_balance_provider: Optional[Callable[[str, str], float]] = None,
    position_balance_extractor: Optional[
        Callable[[Mapping], Tuple[float, float]]
    ] = None,
    position_snapshot=_POSITION_SNAPSHOT_NOT_PROVIDED,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    persist: bool = True,
) -> AccountReconciliationResult:
    """Fetch broker truth once and retry one local optimistic-write race."""
    snapshot = fetch_account_broker_snapshot(
        broker=broker,
        environment=environment,
        account_no=account_no,
        account_balance_provider=account_balance_provider,
        position_balance_extractor=position_balance_extractor,
        position_snapshot=position_snapshot,
        clock=clock,
    )
    attempt_cards = cards
    for attempt in range(2):
        local_state = load_account_local_state(
            engine,
            environment=environment,
            account_no=account_no,
            cards=attempt_cards,
        )
        plan = reduce_account_reconciliation(snapshot, local_state)
        if not persist:
            return AccountReconciliationResult(snapshot=snapshot, plan=plan)
        try:
            apply_reconciliation_plan(engine, plan)
        except _reconciliation_version_conflict_types():
            if attempt:
                raise
            from src.services import trade_card_repository

            logger.info(
                "Account %s local state changed during reconciliation; "
                "reloading and retrying once",
                account_no,
            )
            attempt_cards = trade_card_repository.list_trade_cards(
                engine,
                environment=environment,
                account_no=account_no,
                raise_on_error=True,
            )
            continue
        return AccountReconciliationResult(snapshot=snapshot, plan=plan)
    raise AssertionError("unreachable reconciliation retry state")


def _reconciliation_version_conflict_types() -> tuple[type[RuntimeError], ...]:
    """Import repository conflicts lazily to keep service dependencies acyclic."""
    from src.services.capital_reservation_repository import (
        CapitalReservationVersionConflictError,
    )
    from src.services.discovered_external_order_repository import (
        ExternalOrderVersionConflictError,
    )
    from src.services.execution_order_repository import (
        ExecutionOrderVersionConflictError,
    )
    from src.services.trade_card_repository import TradeCardVersionConflictError

    return (
        TradeCardVersionConflictError,
        ExecutionOrderVersionConflictError,
        CapitalReservationVersionConflictError,
        ExternalOrderVersionConflictError,
    )
