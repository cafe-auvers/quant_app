"""Current-candidate broker-confirmed entry exposure during holdings lag."""
from datetime import datetime

from src.core.execution_order_record import BrokerIdentityStatus, OrderOrigin
from src.core.order_state import OrderIntent, OrderSide
from src.utils.market_calendar import US_MARKET_ZONE


def candidate_fill_exposure(card, orders, *, apply_reconciliation=True):
    """Return proven entry records and their quantity after confirmed exits."""
    if card.entry_candidate_created_at is None or card.session_date is None:
        return (), 0
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
    buys = [(order, at) for order, at in eligible
            if order.side == OrderSide.BUY and order.intent == OrderIntent.ENTRY]
    if not buys:
        return (), 0
    first_entry = min(at for _, at in buys)
    sold = sum(order.filled_quantity for order, at in eligible
               if order.side == OrderSide.SELL and at >= first_entry)
    adjustment = 0
    checkpoint = card.entry_fill_reconciliation
    if (apply_reconciliation and checkpoint.get("candidate")
            == card.entry_candidate_created_at.isoformat()):
        adjustment = max(0, int(checkpoint.get("external_exit_quantity", 0)))
    return tuple(order for order, _ in buys), max(
        0, sum(order.filled_quantity for order, _ in buys) - sold - adjustment
    )


def reconcile_candidate_fill_holdings(card, orders, quantity):
    """Retire the lag fence only after complete holdings account for fills.

    A later complete quantity reduction is authoritative for already-observed
    fills. New/increased fills still require representation in holdings; a
    projected fill on the card alone never acknowledges broker holdings.
    """
    fills, raw_net = candidate_fill_exposure(card, orders, apply_reconciliation=False)
    if not fills:
        return
    candidate = card.entry_candidate_created_at.isoformat()
    checkpoint = card.entry_fill_reconciliation
    if checkpoint.get("candidate") != candidate:
        checkpoint = {}
    observed = checkpoint.get("observed_fills", {})
    adjustment = max(0, int(checkpoint.get("external_exit_quantity", 0)))
    all_observed = all(
        order.filled_quantity <= observed.get(order.client_order_id, 0)
        for order in fills
    )
    if all_observed:
        adjustment = max(adjustment, raw_net - max(0, quantity))
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
        }
