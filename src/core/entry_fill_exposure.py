"""Current-candidate broker-confirmed entry exposure during holdings lag."""
from datetime import datetime

from src.core.execution_order_record import (
    BrokerIdentityStatus, OrderOrigin, TERMINAL_EXECUTION_ORDER_STATUSES,
)
from src.core.order_state import OrderIntent, OrderSide
from src.utils.market_calendar import US_MARKET_ZONE


def _candidate_filled_orders(card, orders):
    if card.entry_candidate_created_at is None or card.session_date is None:
        return []
    eligible = []
    for order in orders:
        if ((order.environment, order.account_no, order.symbol)
                != (card.environment, card.account_no, card.symbol)
                or order.origin not in (OrderOrigin.APPLICATION, OrderOrigin.USER_ADOPTED)
                or order.broker_identity_status != BrokerIdentityStatus.EXACT
                or order.filled_quantity <= 0):
            continue
        try:
            submitted = datetime.fromisoformat(order.submission_started_at or order.prepared_at)
            if submitted.tzinfo is None:
                continue
        except (ValueError, TypeError):
            continue
        if (card.entry_candidate_created_at <= submitted
                and (order.side == OrderSide.SELL
                     or submitted.astimezone(US_MARKET_ZONE).date() == card.session_date)):
            eligible.append((order, submitted))
    first_entry = min((at for order, at in eligible
                       if order.side == OrderSide.BUY and order.intent == OrderIntent.ENTRY), default=None)
    return [(order, at) for order, at in eligible
            if order.side != OrderSide.SELL or (first_entry is not None and at >= first_entry)]


def _exit_adjustment(card, eligible):
    checkpoint = card.entry_fill_reconciliation
    if (card.entry_candidate_created_at is None or checkpoint.get("candidate")
            != card.entry_candidate_created_at.isoformat()):
        return 0
    adjustment = max(0, int(checkpoint.get("external_exit_quantity", 0)))
    observed_sells = checkpoint.get("observed_sells", {})
    try:
        adjusted_at = datetime.fromisoformat(checkpoint["adjusted_at"])
    except (KeyError, ValueError, TypeError):
        adjusted_at = None
    for order, submitted in eligible:
        adopted_time_unknown = order.origin == OrderOrigin.USER_ADOPTED and not order.submission_started_at
        if order.side == OrderSide.SELL and (
            adjusted_at is None or adopted_time_unknown or submitted <= adjusted_at
        ):
            # A sell submitted after this holdings reduction is a distinct
            # exit. Older late-reported/adopted sells may explain it instead.
            adjustment -= max(0, order.filled_quantity - observed_sells.get(order.client_order_id, 0))
    return max(0, adjustment)


def candidate_has_manual_reduction(card):
    checkpoint = card.entry_fill_reconciliation
    return bool(card.entry_candidate_created_at is not None
                and checkpoint.get("candidate") == card.entry_candidate_created_at.isoformat()
                and checkpoint.get("external_exit_quantity", 0) > 0)


def candidate_fill_exposure(card, orders, *, apply_reconciliation=True):
    """Return proven entry records and their quantity after confirmed exits."""
    eligible = _candidate_filled_orders(card, orders)
    buys = [(order, at) for order, at in eligible
            if order.side == OrderSide.BUY and order.intent == OrderIntent.ENTRY]
    if not buys:
        return (), 0
    first_entry = min(at for _, at in buys)
    sold = sum(order.filled_quantity for order, at in eligible
               if order.side == OrderSide.SELL and at >= first_entry)
    adjustment = _exit_adjustment(card, eligible) if apply_reconciliation else 0
    return tuple(order for order, _ in buys), max(
        0, sum(order.filled_quantity for order, _ in buys) - sold - adjustment
    )


def reconcile_candidate_fill_holdings(card, orders, quantity, *, observed_at=None):
    """Retire the lag fence only after complete holdings account for fills.

    A later complete quantity reduction is authoritative for already-observed
    fills. New/increased fills still require representation in holdings; a
    projected fill on the card alone never acknowledges broker holdings.
    """
    orders = tuple(orders)
    fills, raw_net = candidate_fill_exposure(card, orders, apply_reconciliation=False)
    if not fills:
        return
    candidate = card.entry_candidate_created_at.isoformat()
    checkpoint = card.entry_fill_reconciliation
    if checkpoint.get("candidate") != candidate:
        checkpoint = {}
    observed = checkpoint.get("observed_fills", {})
    eligible = _candidate_filled_orders(card, orders)
    adjustment = _exit_adjustment(card, eligible)
    adjusted_at = checkpoint.get("adjusted_at")
    all_observed = all(
        order.filled_quantity <= observed.get(order.client_order_id, 0)
        for order in fills
    )
    pending_sell = any(
        (order.environment, order.account_no, order.symbol)
        == (card.environment, card.account_no, card.symbol)
        and order.side == OrderSide.SELL
        and order.status not in TERMINAL_EXECUTION_ORDER_STATUSES
        for order in orders
    )
    if all_observed and not pending_sell:
        inferred = max(0, raw_net - max(0, quantity))
        if inferred > adjustment:
            adjustment = inferred
            adjusted_at = observed_at.isoformat() if observed_at is not None else None
    expected = max(0, raw_net - adjustment)
    if quantity >= expected:
        observed = {**observed, **{
            order.client_order_id: max(order.filled_quantity, observed.get(order.client_order_id, 0))
            for order in fills
        }}
    if observed:
        card.entry_fill_reconciliation = {
            "candidate": candidate, "observed_fills": observed,
            "external_exit_quantity": adjustment,
            "observed_sells": {order.client_order_id: order.filled_quantity
                               for order, _ in eligible if order.side == OrderSide.SELL},
            "adjusted_at": adjusted_at,
        }
