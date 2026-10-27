"""Current-candidate broker-confirmed entry exposure during holdings lag."""
from datetime import datetime

from src.core.execution_order_record import BrokerIdentityStatus, OrderOrigin
from src.core.order_state import OrderIntent, OrderSide
from src.utils.market_calendar import US_MARKET_ZONE


def candidate_fill_exposure(card, orders):
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
    return tuple(order for order, _ in buys), max(0, sum(order.filled_quantity for order, _ in buys) - sold)
