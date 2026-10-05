"""Session-scoped Buy Today feedback shared by desktop and mobile views."""

from datetime import date

from src.core.exit_policy import market_session_date, market_session_date_from_value
from src.core.trade_card_state import TradeCardState


def buy_today_feedback_is_current(
    card: TradeCardState,
    *,
    current_session_date: date | None = None,
) -> bool:
    """Keep durable rejection history without showing yesterday's result today."""
    if not (card.buy_today_note or card.rejected_orb_snapshot):
        return False
    feedback_session = card.last_buy_today_session_date
    if feedback_session is None:
        feedback_session = market_session_date_from_value(
            card.rejected_orb_snapshot.get("session_date")
        )
    if feedback_session is None:
        observed = getattr(card, "board_status_updated_at", None)
        if observed is not None:
            feedback_session = market_session_date(observed)
    if feedback_session is None:
        # Keep malformed legacy feedback until a new explicit activation.
        return True
    today = current_session_date or market_session_date()
    return feedback_session >= today
